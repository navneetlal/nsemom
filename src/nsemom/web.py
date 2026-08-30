"""Read-mostly HTTP server for the local UI.

Deliberately stdlib-only. The project pins every dependency to a version with a
verified aarch64 wheel so nothing compiles on the Pi, and a dashboard is not a
good reason to add a web framework and a Rust-compiled validation library to
that set. ThreadingHTTPServer is entirely adequate for one person on a LAN.

There is no authentication, by request. Bind to 127.0.0.1 unless you actually
want it reachable from the network, and do not expose it to the internet - it
can write to the positions table.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
from contextlib import contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb

from .backtest import ExitRules
from .config import Config
from .output import SHORTLIST_COLUMNS, open_position_status, shortlist
from .screen import Preset

log = logging.getLogger(__name__)

UI_DIST = Path(__file__).resolve().parents[2] / "ui" / "dist"
SYMBOL_RE = re.compile(r"^[A-Za-z0-9&_.-]{1,32}$")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json",
    ".svg": "image/svg+xml", ".ico": "image/x-icon", ".woff2": "font/woff2",
    ".map": "application/json",
}


class _CursorStore:
    """Minimal stand-in for Store, exposing only the `.con` that the shortlist
    and position helpers use. Avoids opening a second database connection per
    request just to reuse those functions."""

    def __init__(self, con) -> None:
        self.con = con


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _clean(value):
    """DuckDB/pandas values -> JSON-safe. NaN is not valid JSON."""
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]
    if hasattr(value, "item"):          # numpy scalar
        return _clean(value.item())
    return value


def _rows(cursor) -> list[dict]:
    columns = [d[0] for d in cursor.description]
    return [{c: _clean(v) for c, v in zip(columns, row)} for row in cursor.fetchall()]


class Backend:
    """Opens the database per request and closes it immediately.

    This is not a performance compromise, it is the point. DuckDB permits a
    single writer and no concurrent readers from other processes, so a server
    holding even a read-only handle blocks `nsemom daily` from taking its write
    lock - the scheduled job would fail every night while the UI was running.

    The cost is ~6ms to open and ~2ms to query a 685 MB database, which is
    irrelevant for one person clicking around a LAN page.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    @contextmanager
    def connect(self, read_only: bool = True):
        if not self.cfg.db_path.exists():
            raise ApiError(503, f"no database at {self.cfg.db_path}")
        try:
            con = duckdb.connect(str(self.cfg.db_path), read_only=read_only)
        except duckdb.Error as exc:
            raise ApiError(
                503,
                "database is locked - the daily job is probably running. "
                f"({exc})",
            ) from exc
        try:
            yield con
        finally:
            con.close()

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        with self.connect() as con:
            try:
                return _rows(con.execute(sql, params or []))
            except duckdb.Error as exc:
                raise ApiError(503, f"query failed: {exc}") from exc

    def preset(self, name: str | None) -> Preset:
        name = name or self.cfg.default_preset
        if name not in self.cfg.screen_raw:
            raise ApiError(400, f"unknown preset {name!r}")
        return Preset.from_raw(name, self.cfg.screen_raw[name])

    def rules(self) -> ExitRules:
        b = self.cfg.backtest
        return ExitRules(b.initial_stop_atr, b.trailing_stop_atr, b.trailing_arms_at_r,
                         b.structural_exit_days, b.max_hold_days, b.min_hold_days)

    @property
    def ema_mid(self) -> int:
        return sorted(self.cfg.indicators.ema_periods)[1]


# ---------------------------------------------------------------- API routes

def route_meta(backend: Backend, query: dict) -> dict:
    stats = backend.query(
        """SELECT max(trade_date) AS latest_date, min(trade_date) AS first_date,
                  count(DISTINCT symbol) AS symbols, count(*) AS bars
             FROM indicators"""
    )[0]
    cfg, bt = backend.cfg, backend.cfg.backtest
    return {
        **stats,
        "presets": sorted(cfg.screen_raw),
        "default_preset": cfg.default_preset,
        "exit_rules": {
            "initial_stop_atr": bt.initial_stop_atr,
            "trailing_stop_atr": bt.trailing_stop_atr,
            "max_hold_days": bt.max_hold_days,
            "min_hold_days": bt.min_hold_days,
        },
        "slots": bt.slots,
        "quarantined": backend.query(
            "SELECT count(DISTINCT symbol) AS n FROM quarantine")[0]["n"],
    }


def route_dates(backend: Backend, query: dict) -> list:
    limit = int(query.get("limit", ["120"])[0])
    return backend.query(
        "SELECT DISTINCT trade_date FROM indicators ORDER BY trade_date DESC LIMIT ?",
        [max(1, min(limit, 2000))],
    )


def route_shortlist(backend: Backend, query: dict) -> dict:
    preset = backend.preset(query.get("preset", [None])[0])
    as_of = query.get("date", [None])[0]
    if as_of is None:
        as_of = backend.query("SELECT max(trade_date) AS d FROM indicators")[0]["d"]
    if as_of is None:
        raise ApiError(503, "no indicators; run `nsemom indicators` first")

    with backend.connect() as con:
        frame = shortlist(_CursorStore(con), preset, backend.rules(),
                          backend.cfg.backtest.min_warmup_bars,
                          dt.date.fromisoformat(str(as_of)))
    rows = [{c: _clean(v) for c, v in row.items()}
            for row in frame.to_dict("records")]
    return {"date": str(as_of), "preset": preset.name, "rank_by": preset.rank_by,
            "columns": [c for c in SHORTLIST_COLUMNS if c in frame.columns],
            "rows": rows}


def route_positions(backend: Backend, query: dict) -> dict:
    as_of = backend.query("SELECT max(trade_date) AS d FROM indicators")[0]["d"]
    with backend.connect() as con:
        status = open_position_status(_CursorStore(con), backend.rules(),
                                      backend.ema_mid,
                                      dt.date.fromisoformat(str(as_of)))
    return {
        "as_of": str(as_of),
        "open": [{k: _clean(v) for k, v in r.items()}
                 for r in status.to_dict("records")],
        "closed": backend.query(
            """SELECT position_id, symbol, entry_date, entry_price, quantity,
                      exit_date, exit_price, exit_reason,
                      exit_price / entry_price - 1 AS realised_return
                 FROM positions WHERE exit_date IS NOT NULL
                ORDER BY exit_date DESC LIMIT 100"""),
    }


def route_symbol(backend: Backend, symbol: str, query: dict) -> dict:
    if not SYMBOL_RE.match(symbol):
        raise ApiError(400, "bad symbol")
    days = max(30, min(int(query.get("days", ["260"])[0]), 2000))
    bars = backend.query(
        """SELECT trade_date, open, high, low, close, volume, turnover,
                  ema_20, ema_50, ema_100, ema_200, rsi, adx, stoch_k, stoch_d, atr,
                  median_turnover, mom_short, mom_long_skip, ext_zscore,
                  trend_age_days, pct_from_high
             FROM indicators WHERE symbol = ?
            ORDER BY trade_date DESC LIMIT ?""",
        [symbol.upper(), days],
    )
    if not bars:
        raise ApiError(404, f"no data for {symbol.upper()}")
    actions = backend.query(
        """SELECT ex_date, action_type, adj_factor, verification, subject
             FROM corporate_actions
            WHERE symbol = ? AND adj_factor IS NOT NULL
            ORDER BY ex_date DESC LIMIT 20""", [symbol.upper()])
    return {"symbol": symbol.upper(), "bars": list(reversed(bars)),
            "corporate_actions": actions,
            "quarantined": backend.query(
                "SELECT from_date, to_date, reason FROM quarantine WHERE symbol = ?",
                [symbol.upper()])}


def route_symbols(backend: Backend, query: dict) -> list:
    term = (query.get("q", [""])[0] or "").upper()[:32]
    if not term:
        return []
    return backend.query(
        """SELECT symbol, max(trade_date) AS last_seen FROM indicators
            WHERE symbol LIKE ? GROUP BY symbol
            ORDER BY (symbol = ?) DESC, symbol LIMIT 25""",
        [f"%{term}%", term],
    )


def route_add_position(backend: Backend, body: dict) -> dict:
    symbol = str(body.get("symbol", "")).upper().strip()
    if not SYMBOL_RE.match(symbol):
        raise ApiError(400, "bad symbol")
    try:
        price = float(body["entry_price"])
        quantity = int(body["quantity"])
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, "entry_price and quantity are required") from None
    if price <= 0 or quantity <= 0:
        raise ApiError(400, "entry_price and quantity must be positive")
    entry_date = body.get("entry_date") or dt.date.today().isoformat()

    with backend.connect(read_only=False) as con:
        known = con.execute("SELECT count(*) FROM indicators WHERE symbol = ? "
                            "AND trade_date = ?", [symbol, entry_date]).fetchone()[0]
        if not known:
            raise ApiError(400, f"no bar for {symbol} on {entry_date} - "
                                "exits are evaluated from the entry bar's ATR")
        next_id = con.execute(
            "SELECT coalesce(max(position_id), 0) + 1 FROM positions").fetchone()[0]
        con.execute("INSERT INTO positions (position_id, symbol, entry_date, "
                    "entry_price, quantity) VALUES (?, ?, ?, ?, ?)",
                    [next_id, symbol, entry_date, price, quantity])
        con.execute("CHECKPOINT")
    return {"position_id": next_id, "symbol": symbol}


def route_close_position(backend: Backend, body: dict) -> dict:
    try:
        position_id = int(body["position_id"])
        exit_price = float(body["exit_price"])
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, "position_id and exit_price are required") from None
    exit_date = body.get("exit_date") or dt.date.today().isoformat()
    reason = str(body.get("reason", "manual"))[:64]

    with backend.connect(read_only=False) as con:
        changed = con.execute(
            "UPDATE positions SET exit_date = ?, exit_price = ?, exit_reason = ? "
            "WHERE position_id = ? AND exit_date IS NULL",
            [exit_date, exit_price, reason, position_id]).fetchone()
        con.execute("CHECKPOINT")
    if changed and changed[0] == 0:
        raise ApiError(404, f"position {position_id} not found, or already closed")
    return {"position_id": position_id, "closed": True}


def route_delete_position(backend: Backend, body: dict) -> dict:
    """Remove a position row outright.

    Distinct from closing it: closing records a real exit and keeps the trade in
    history, deleting is for an entry made by mistake.
    """
    try:
        position_id = int(body["position_id"])
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, "position_id is required") from None

    with backend.connect(read_only=False) as con:
        existing = con.execute("SELECT symbol FROM positions WHERE position_id = ?",
                               [position_id]).fetchone()
        if not existing:
            raise ApiError(404, f"position {position_id} not found")
        con.execute("DELETE FROM positions WHERE position_id = ?", [position_id])
        con.execute("CHECKPOINT")
    return {"position_id": position_id, "deleted": True, "symbol": existing[0]}


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "nsemom"
    protocol_version = "HTTP/1.1"

    def __init__(self, backend: Backend, *args, **kwargs) -> None:
        self.backend = backend
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, allow_nan=False).encode(),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1 << 20:
            raise ApiError(400, "expected a JSON body")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"malformed JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "expected a JSON object")
        return payload

    # -- static files (the built Vite bundle)
    def _serve_static(self, path: str) -> None:
        if not UI_DIST.is_dir():
            self._json(503, {"error": "UI not built. Run: cd ui && npm install && npm run build"})
            return
        relative = path.lstrip("/") or "index.html"
        target = (UI_DIST / relative).resolve()
        if not str(target).startswith(str(UI_DIST.resolve())) or not target.is_file():
            target = UI_DIST / "index.html"        # SPA fallback
        if not target.is_file():
            self._json(404, {"error": "not found"})
            return
        self._send(200, target.read_bytes(),
                   CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

    # -- dispatch
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if not path.startswith("/api/"):
                return self._serve_static(path)
            if path == "/api/meta":
                return self._json(200, route_meta(self.backend, query))
            if path == "/api/dates":
                return self._json(200, route_dates(self.backend, query))
            if path == "/api/shortlist":
                return self._json(200, route_shortlist(self.backend, query))
            if path == "/api/positions":
                return self._json(200, route_positions(self.backend, query))
            if path == "/api/symbols":
                return self._json(200, route_symbols(self.backend, query))
            if path.startswith("/api/symbol/"):
                return self._json(200, route_symbol(
                    self.backend, path[len("/api/symbol/"):], query))
            self._json(404, {"error": f"no route for {path}"})
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except Exception as exc:                    # never take the server down
            log.exception("unhandled error on %s", path)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    do_HEAD = do_GET

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/positions":
                return self._json(201, route_add_position(self.backend, self._body()))
            if path == "/api/positions/close":
                return self._json(200, route_close_position(self.backend, self._body()))
            if path == "/api/positions/delete":
                return self._json(200, route_delete_position(self.backend, self._body()))
            self._json(404, {"error": f"no route for {path}"})
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except Exception as exc:
            log.exception("unhandled error on %s", path)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def serve(cfg: Config, host: str, port: int) -> None:
    backend = Backend(cfg)
    server = ThreadingHTTPServer((host, port), partial(Handler, backend))
    log.info("serving on http://%s:%d  (no authentication - LAN only)", host, port)
    if not UI_DIST.is_dir():
        log.warning("UI bundle missing at %s - run: cd ui && npm install && npm run build",
                    UI_DIST)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
