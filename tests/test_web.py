"""Web layer tests.

The server runs for real on an ephemeral port rather than calling route
functions directly, because two of the things most worth testing - path
traversal containment and JSON error shape - live in the handler, not the routes.
"""

import dataclasses
import datetime as dt
import json
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer

import pytest

from nsemom import web
from nsemom.adjust import rebuild_intervals, verify
from nsemom.config import Config
from nsemom.indicators import rebuild
from nsemom.ingest.bhavcopy import Bar
from nsemom.store.db import Store

START = dt.date(2024, 1, 1)


def _bars(symbol, n, base=100.0, drift=1.0):
    return [
        Bar(symbol, START + dt.timedelta(days=i), "INE1", "EQ",
            base + drift * i, (base + drift * i) * 1.02, (base + drift * i) * 0.98,
            base + drift * i, base + drift * i, 100_000,
            (base + drift * i) * 100_000, 500)
        for i in range(n)
    ]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    db = tmp_path_factory.mktemp("web") / "web.duckdb"
    cfg = dataclasses.replace(Config.load(), db_path=db)

    with Store(db) as store:
        bars = _bars("ALPHA", 120) + _bars("BETA", 120, base=300.0, drift=0.4)
        for day in sorted({b.trade_date for b in bars}):
            store.write_day(day, "udiff", [b for b in bars if b.trade_date == day], None)
        verify(store, cfg.corporate_actions.verification_tolerance)
        rebuild_intervals(store)
        rebuild(store, cfg)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(web.Handler, web.Backend(cfg)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(base, path):
    with urllib.request.urlopen(f"{base}{path}", timeout=10) as r:
        return r.status, json.loads(r.read())


def post(base, path, payload):
    request = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def fail(base, path):
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_meta_describes_the_database(server):
    status, body = get(server, "/api/meta")
    assert status == 200
    assert body["symbols"] == 2
    assert body["default_preset"] in body["presets"]
    assert body["exit_rules"]["max_hold_days"] > 0


def test_shortlist_columns_are_the_same_whether_or_not_anything_passes(server):
    # The synthetic ramp does not pass the real screen, so this exercises the
    # empty case - which must still describe the full column set, or the UI has
    # to special-case a day where nothing qualified.
    status, body = get(server, "/api/shortlist")
    assert status == 200
    assert body["date"] and body["preset"] and body["rank_by"]
    for column in ("rank", "symbol", "close", "ext_zscore", "median_turnover",
                   "suggested_stop"):
        assert column in body["columns"], column
    assert isinstance(body["rows"], list)


def test_symbol_returns_bars_oldest_first(server):
    status, body = get(server, "/api/symbol/ALPHA?days=40")
    assert status == 200
    dates = [b["trade_date"] for b in body["bars"]]
    assert dates == sorted(dates), "chart expects ascending order"
    assert body["bars"][-1]["ema_20"] is not None


def test_symbol_search_matches_substrings(server):
    _, body = get(server, "/api/symbols?q=ALP")
    assert [r["symbol"] for r in body] == ["ALPHA"]
    _, empty = get(server, "/api/symbols?q=")
    assert empty == []


def test_position_lifecycle(server):
    _, meta = get(server, "/api/meta")
    latest = meta["latest_date"]

    status, added = post(server, "/api/positions", {
        "symbol": "ALPHA", "entry_price": 150.0, "quantity": 10, "entry_date": latest})
    assert status == 201
    position_id = added["position_id"]

    _, listing = get(server, "/api/positions")
    assert [p["symbol"] for p in listing["open"]] == ["ALPHA"]
    assert listing["open"][0]["action"] == "HOLD"

    status, _ = post(server, "/api/positions/close",
                     {"position_id": position_id, "exit_price": 175.0})
    assert status == 200
    _, listing = get(server, "/api/positions")
    assert listing["open"] == []
    assert listing["closed"][0]["realised_return"] == pytest.approx(175 / 150 - 1)

    status, _ = post(server, "/api/positions/delete", {"position_id": position_id})
    assert status == 200
    _, listing = get(server, "/api/positions")
    assert listing["closed"] == []


def test_a_position_needs_a_bar_on_its_entry_date(server):
    # Exits are parameterised by the entry bar's ATR, so an entry date with no
    # bar would produce a position whose stop can never be computed.
    status, body = post(server, "/api/positions", {
        "symbol": "ALPHA", "entry_price": 150.0, "quantity": 10,
        "entry_date": "2019-01-01"})
    assert status == 400
    assert "no bar for ALPHA" in body["error"]


@pytest.mark.parametrize("payload,expected", [
    ({"symbol": "ALPHA", "entry_price": 10, "quantity": 0}, "positive"),
    ({"symbol": "ALPHA", "entry_price": -1, "quantity": 5}, "positive"),
    ({"symbol": "../etc", "entry_price": 10, "quantity": 5}, "bad symbol"),
    ({"symbol": "ALPHA"}, "required"),
])
def test_bad_position_payloads_are_rejected(server, payload, expected):
    status, body = post(server, "/api/positions", payload)
    assert status == 400 and expected in body["error"]


def test_closing_or_deleting_an_unknown_position_is_404(server):
    status, _ = post(server, "/api/positions/close",
                     {"position_id": 9999, "exit_price": 10})
    assert status == 404
    status, _ = post(server, "/api/positions/delete", {"position_id": 9999})
    assert status == 404


def test_unknown_preset_and_symbol_are_diagnosable(server):
    status, body = fail(server, "/api/shortlist?preset=nope")
    assert status == 400 and "unknown preset" in body["error"]
    status, body = fail(server, "/api/symbol/NOSUCHTHING")
    assert status == 404 and "no data" in body["error"]


def test_symbol_path_cannot_be_used_to_traverse(server):
    status, body = fail(server, "/api/symbol/..%2f..%2fetc%2fpasswd")
    assert status == 400 and body["error"] == "bad symbol"


def test_unknown_api_route_is_404_json(server):
    status, body = fail(server, "/api/nothing-here")
    assert status == 404 and "no route" in body["error"]
