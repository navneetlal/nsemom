# nsemom — NSE momentum screener

A deterministic, rules-based momentum screener and backtester for Indian equities,
built to run as a scheduled job on a Raspberry Pi 4.

Every rule in the signal path is explicit arithmetic. There is no machine learning,
no fitted model and no trained scoring function anywhere in the pipeline — the
signal-to-noise ratio in daily equity bars is low enough that a fitted model
produces a flattering backtest and a disappointing live account.

## How it works

```
[1] Ingest       NSE bhavcopy (dual-format) ──► DuckDB, raw + unadjusted
[2] Adjust       corporate actions ──► adjusted price view
[3] Indicators   EMA / RSI / ADX / ATR / momentum / turnover
[4] Screen       deterministic filters ──► ranked shortlist
                                              │
[5] Daily output │  CSV + table + DuckDB. NOT a buy list —
                 │  an evidence pack with the context an LLM needs
                 ▼
        ┌──────────────────────────────────┐
        │  YOU + LLM  (outside this repo)  │  qualitative research,
        │  pick the final 2–5 names        │  "how much move is left"
        └──────────────────────────────────┘
                 │  you record what you bought
                 ▼
[6] Position     positions table + daily exit-rule evaluation.
    tracking     Today's output also says: HOLD / EXIT for what you own.

[7] Backtest     measures the shortlist's expectancy and the exit rules

     UI          `nsemom serve` — a local read-only view of the same DuckDB
                 file, plus adding and closing positions. Nothing else: it
                 cannot run a backtest, an ingest, or edit config.
```

The split matters: **the deterministic layer decides what is worth looking at,
you and an LLM decide what to buy, and the deterministic layer decides when to
sell.** Entries are discretionary. Exits are not.

### Build status

All five stages are built and tested.

| Stage | What it does | Command |
|---|---|---|
| 1 — Ingest | dual-format bhavcopy, resumable | `nsemom backfill` / `update` |
| 2 — Store + corporate actions | DuckDB, ratio cross-validation | `nsemom corpactions` / `adjust` |
| 3 — Indicators | EMA/RSI/ADX/ATR + evidence pack | `nsemom indicators` |
| 4 — Screen + backtest | presets, costs, exits, walk-forward | `nsemom screen` / `backtest` |
| 5 — Schedule + output | systemd timer, CSV + position exits | `nsemom daily` |
| Local UI (optional) | read-only browser view + position entry | `nsemom serve` |

## First-time setup

Requires Python 3.11+ (developed and tested on 3.14).

```bash
git clone <this repo> && cd momentum-trading
python3 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/pip install -e .
./.venv/bin/python -m pytest        # 106 tests, all offline
```

On the Pi, `duckdb`, `pandas` and `numpy` all publish `cp314` aarch64 manylinux
wheels, so nothing compiles from source.

## Backfill

```bash
./.venv/bin/nsemom backfill                      # config history_start .. today
./.venv/bin/nsemom backfill --from 2015-01-01 --to 2020-12-31
./.venv/bin/nsemom backfill --limit 20           # smoke test
./.venv/bin/nsemom status
```

Backfill is **resumable, idempotent and rate-limited**. Every weekday attempted
gets a row in `ingest_log`, so a re-run re-downloads nothing and interrupting it
with Ctrl-C loses at most the day in flight. Re-running a stored session replaces
it rather than appending — `nsemom status` reports a duplicate count that must
always read zero.

Expect roughly 2,900 weekdays between 2015 and today. At the default 0.4s
politeness interval, a full backfill takes about 25–35 minutes and produces a
few hundred MB of DuckDB.

### Two formats, one table

NSE replaced the legacy bhavcopy with UDiFF on 2024-07-08, but the legacy archive
is still served for historical dates. Neither format alone spans 2015–today:

| | legacy | UDiFF |
|---|---|---|
| available | 2000 → 2024-07-08 | 2024-01-02 → today |
| host | `nsearchives.nseindia.com` | `nsearchives.nseindia.com` |

Both are parsed onto one `Bar` record. They overlap between 2024-01-02 and
2024-07-08, and `tests/test_bhavcopy.py` uses that overlap to assert the two
parsers produce **identical output for the same session** — verified across 1,782
symbols with zero mismatches on any field.

A note on access: NSE drops non-browser user agents at the edge, so a realistic
`user_agent` (a config value) is required. The archive host does **not** need the
homepage cookie handshake — that is only needed for `www.nseindia.com/api/`
endpoints, which corporate actions will use in stage 2.

## Daily use

```bash
./.venv/bin/nsemom daily       # ingest, adjust, recompute, report
```

That runs the whole chain and leads its output with HOLD / EXIT for anything you
hold, before listing new candidates. It exits non-zero if any date failed or a
health check trips, so the scheduled job fails loudly rather than leaving you
with a stale shortlist. Individual stages are also available on their own
(`update`, `corpactions`, `adjust`, `indicators`, `screen`, `backtest`,
`compact`, `position`).

### Storage

The database holds 4.87M price bars and 4.87M indicator rows in **~685 MB**.

Indicators are dropped and rebuilt in full on each run, and DuckDB does not
return space from replaced tables to the filesystem — left alone the file grows
by roughly its own size every day. `nsemom daily` therefore compacts once the
file passes `--compact-above-mb` (default 900), rewriting it into a fresh file:

```bash
./.venv/bin/nsemom compact     # 1,972 MB -> 685 MB in under 3 seconds
```

Derived indicators are stored as 32-bit floats, which halves the largest table
and changes no result — the full backtest is bit-identical either way. OHLC,
turnover and median turnover stay 64-bit because they become fill prices and a
liquidity threshold.

## Local UI

An optional browser view of the same database, for when reading a wide table over
SSH gets tiring.

```bash
cd ui && npm install && npm run build     # once
./.venv/bin/nsemom serve                  # http://127.0.0.1:8787
```

Three tabs: the **shortlist** for any session and preset, sortable by any column;
**positions**, showing HOLD / EXIT against the deterministic exit rules with
add, close and delete; and a **symbol** view with an adjusted-price chart, the
four EMAs, volume, RSI, and that symbol's corporate actions.

### What it deliberately does not do

The server is a DuckDB connector and nothing more. It cannot run a backtest,
trigger an ingest, or change config — those stay on the command line where their
output is reviewable and their parameters are in version control. The only writes
it performs are adding, closing and deleting rows in `positions`.

### Two constraints worth knowing

**It never holds the database open.** DuckDB allows a single writer and no
concurrent readers from other processes, so a server holding even a read-only
handle would block `nsemom daily` from taking its write lock — the scheduled job
would fail every night the UI was running. Every request opens and closes its own
connection instead. On a 685 MB database that costs about 6 ms to open and 2 ms
to query, which is irrelevant for one person clicking around. If the daily job is
mid-run, the UI returns a clear 503 rather than hanging.

**There is no authentication**, by request. It binds to `127.0.0.1` by default.
`--host 0.0.0.0` makes it reachable from the LAN, which is fine on a home network
and unwise anywhere else — it can write to the positions table.

```bash
./.venv/bin/nsemom serve --host 0.0.0.0 --port 8787   # reachable from the LAN
cd ui && npm run dev                                   # hot reload, proxies /api
```

No Python dependencies were added for any of this — the server is stdlib
`ThreadingHTTPServer`, because the project pins every dependency to a version
with a verified aarch64 wheel and a dashboard is not a good reason to add a web
framework and a Rust-compiled validator to that set.

## Corporate actions

Bhavcopy is unadjusted, so an unhandled 1:5 split reads as an 80% single-day
crash that trips every filter and poisons every EMA for the next 200 bars.

NSE states corporate-action ratios only as free text (`"Face Value Split
(Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"`), so parsing is
unavoidable. What makes it trustworthy is a **second, independent source for the
same number**: on the ex-date the price gaps by that ratio, so `prev_close /
open` implies the factor without reading any text at all.

```
nsemom corpactions --from 2015-01-01     # fetch + parse
nsemom adjust                            # cross-validate, then build the view
```

Agreement within tolerance marks the action `verified` and it is applied.
Disagreement marks it `mismatch`, it is **not** applied, and the symbol is
quarantined out of the universe around the event. Demergers are quarantined
outright — value splits across entities with no single derivable ratio.

Raw prices are never rewritten. `prices_adjusted` is a view over a small step
function of cumulative factors, so adjustment is reproducible, auditable and
reversible, and re-running it after fixing a ratio simply yields the right
answer.

Two cases this design was built around, both real:

- **BAJFINANCE, 2025-06-16** — a 2:1 face-value split *and* a 4:1 bonus sharing
  one ex-date. They compound to 10x. Applying either alone leaves the series
  wrong by 5x or 2x, and only the product matches the observed gap. Grouping by
  `(symbol, ex_date)` before comparing is what catches this.
- **VERTOZ, 2025-06-25** — a 10:1 consolidation, factor 0.1, which adjusts
  history *upward*. Handled by the same code path as a split.

## Indicators

Recursive indicators use pandas `ewm(alpha=1/period, adjust=False)`, which is
Wilder smoothing — not a rolling mean, which is a different and wrong indicator.
Everything non-recursive is a DuckDB window function.

Beyond the screen's own inputs, each shortlist row carries the context the
research step needs:

| column | what it answers |
|---|---|
| `ext_zscore` | how stretched is this against **its own** trailing year? |
| `ext_atr` | how many ATRs above the 50 EMA |
| `pct_from_high` | room to the 52-week high |
| `trend_age_days` | how long the EMA stack has held — fresh trend or late one |
| `up_day_ratio` | trend quality: a grind, or two gap days |
| `rsi_divergence` | new price high without a new RSI high |
| `median_turnover` | can you actually get in and out |

## Screening and backtesting

Three presets ship, so the design questions get settled by measurement rather
than argument:

```bash
nsemom screen                                    # today, default preset
nsemom backtest --preset hybrid --preset chartink --preset spec
nsemom backtest --preset hybrid --split 2023-01-01   # walk-forward
```

| preset | what it is |
|---|---|
| `hybrid` | **default.** EMA stacking, ADX, median-turnover floor, volume expansion as a *ranking* input rather than a gate |
| `chartink` | the live Chartink screener, reproduced exactly |
| `spec` | the literal original written spec |

Two differences worth knowing about, both measured rather than assumed:

- **Liquidity must use the 20-day median, not same-day turnover.** The volume
  spike the screen selects on also inflates that day's turnover. Measured over
  44 sessions of real bhavcopy: of 3,713 bars passing a same-day ₹10 cr filter,
  **44% had a 20-day median turnover below ₹10 cr** — one name showed ₹28.97 cr
  on its spike day against a ₹0.40 cr median.
- **Volume expansion is a ranking input, not a gate.** With ~15 slots and a
  30–90 day hold you make roughly 60 buys a year while the screen offers far
  more qualifiers than that, so a binary gate discards information instead of
  filtering.

### Costs

Modelled and on by default: STT both legs, stamp duty on buy, exchange
transaction charges, SEBI turnover fee, GST on the fee components (**not** on
STT), flat DP charges per scrip on the sell side, and slippage tiered by
liquidity rather than one optimistic midcap number.

DP charges are reported as their own line because they do not scale with
position size, and that is what decides whether a structure is viable at all:

| position size | round-trip cost | of which DP |
|---|---|---|
| ₹1,667 | 1.18% | 0.96% |
| ₹16,667 | 0.32% | 0.10% |
| ₹67,000 | 0.25% | 0.02% |

This is why the portfolio is slot-based. Daily signals × 20 names × a 90-day
hold is 1,800 concurrent slots — ₹555 a position at ₹10 lakh, where flat DP
charges alone would eat roughly 3%.

### Exit plan

Deterministic, evaluated daily against what you actually hold. First to trigger
wins:

```
Initial stop      entry − 2.5 × ATR(14) as at the signal bar
Trailing stop     highest close since entry − 3 × ATR, arms after +1R
Structural exit   close below the 50 EMA for 2 consecutive sessions
Time stop         90 days (configurable 30–90)
```

A stop fills at `min(stop level, that bar's open)`, so a gap through the stop
fills at the gap rather than at a price that never traded. A structural exit is
a close-based signal, so it fills at the **next** open.

## Recording what you bought

```bash
nsemom position add --symbol TITAN --price 3450 --quantity 20
nsemom position list
nsemom position close --id 3 --price 3720 --reason "trailing stop"
```

`nsemom daily` then leads its output with HOLD / EXIT for every open position,
before the new candidates. Every shortlist row ever emitted is also kept in
`shortlist_log` — comparing what you bought against the full shortlist is the
only way to find out later whether the research step adds or destroys value.

## Measured results

Default configuration: `hybrid` preset, 6/8 ATR stops, 60-session hold, 15 slots,
₹10 lakh, **all costs on**. Data: 4.87M bars, 3,765 symbols, 2015-01-01 to
2026-08-28.

Exit parameters were selected on **2015-2020 only**, from a 16-point grid over
stop width x hold length. The 2021-2026 period was then measured once.

| | in-sample 2015-2020 | out-of-sample 2021-2026 | full period |
|---|---|---|---|
| net CAGR | 14.62% | **26.76%** | 15.37% |
| max drawdown | −45.6% | −41.8% | −46.4% |
| hit rate | 49.2% | 52.7% | 49.1% |

Full period, net: **+371.8% total**, 738 trades, 51.6-session average hold,
avg win +26.8% against avg loss −15.2%, turnover 22.6x/yr.

### What a random start date would have returned

| hold | worst | p10 | **median** | mean | p90 | best | % positive |
|---|---|---|---|---|---|---|---|
| 1 month | −24.8% | −5.6% | **+0.31%** | +1.53% | +9.0% | +50.1% | 53.3% |
| 3 month | −29.0% | −15.0% | **+2.21%** | +4.94% | +31.1% | +82.5% | 58.2% |
| 1 year | −33.0% | −27.5% | **+6.86%** | +29.46% | +120.8% | +196.3% | 55.7% |
| 3 year | −17.5% | +8.9% | **+84.19%** | +112.7% | +271.2% | +410.7% | 94.4% |

Note the gap between median and mean: the average is carried by a fat right
tail, not by a typical year. Year by year, net:

```
2016  -30.8%     2019   -2.6%     2022   -0.6%     2025  -29.4%
2017  +85.9%     2020  +17.3%     2023  +71.4%     2026   +4.4%
2018  -19.9%     2021 +121.0%     2024  +42.9%
```

Three years lost 20-31%. Two years made most of the money. A −46% drawdown is in
the record. This is not a steady income stream.

### Why the exit rules are what they are

The original spec's 2.5 ATR stop produced **0.42% net CAGR against 9.04% gross** —
costs ate the entire edge. It fired on 88% of trades, cut the average hold to 24
sessions against a 30-90 day target, and drove turnover to 16x a year. Every 2.5
ATR variant in the grid was negative in-sample.

Widening the stop fixed it, and the same lesson appeared three times over: fewer
and wider exits beat tighter ones. Holds under 21 sessions were also negative
in-sample; 50-90 sessions form a stable plateau near 12-14%, which is worth more
than a sharp optimum. Enabling the structural exit from day 30 cost 5 points of
in-sample CAGR.

### What this does and does not tell you

It measures **the shortlist**, not your picks — whether the top 15 by momentum is
a good pool to hunt in, assuming you bought all of them mechanically. It cannot
tell you whether the research step adds value on top; `shortlist_log` and
`positions` exist so that becomes answerable later.

And 2021-2026 was an exceptional Indian bull market. A long-only momentum book
would have done well in it almost regardless, so the 26.76% out-of-sample figure
is substantially regime rather than edge.

## The LLM research prompt

`nsemom daily` writes the day's shortlist to `data/output/shortlist_YYYY-MM-DD.csv`.
Paste that file into the prompt below. Its purpose is to keep the LLM strictly on
the qualitative half of the problem — the arithmetic is already done and asking a
model to redo it just invites it to disagree with the screener for no reason.

````text
You are assisting with the discretionary selection step of an otherwise
deterministic trading pipeline. My holding period is 30–90 days.

WHAT IS ALREADY DONE — do not redo this, and do not re-rank on the numbers:
A rules-based screener has already filtered the entire NSE cash universe on trend
structure (EMA20 > EMA50 > EMA100 > EMA200), RSI(14) inside its configured band,
ADX(14) above its threshold, 20-day median turnover above the liquidity floor,
and volume expansion against the 50-day baseline. Every name below passes all of
them. The technical work is complete and it is not where you add value.

YOUR JOB — market intention, and how much of the move is left:
For each candidate, research and judge:

1. WHY is it moving? Name the specific catalyst: results, order win, capex cycle,
   regulatory change, sector rotation, promoter action, index inclusion. A move
   with no traceable cause is itself a finding.
2. Is the catalyst SPENT or ONGOING? A stock that ran 40% on a results beat three
   weeks ago has largely priced it in. One at the start of an order cycle has not.
3. What is the SECTOR doing? A whole sector moving sustains a 30–90 day hold far
   better than a lone name, which more often mean-reverts.
4. What could BREAK it inside 90 days? Upcoming results, lock-in expiry, pledged
   promoter shares, debt refinancing, a pending regulatory decision, valuation
   stretched against peers.
5. Cross-check `ext_zscore` — how stretched the stock is against its OWN
   trailing year, not against other stocks. A high value combined with a spent
   catalyst is the classic late entry.

COLUMNS IN THE DATA:
  close              last traded price
  rsi, adx           momentum and trend strength, both already inside the screen
  mom_short          30-session return
  mom_long_skip      6-month return skipping the most recent month
  ext_atr            how many ATRs the price sits above its 50 EMA
  ext_zscore         that extension vs this stock's own trailing year
  pct_from_high      distance below the 52-week high (negative = below it)
  trend_age_days     consecutive sessions the EMA stack has held
  up_day_ratio       fraction of up days in the last 20 sessions
  rsi_divergence     1 = new price high WITHOUT a new RSI high
  vol_ratio          today's volume vs its trailing 20-day baseline
  vol_sustained      5-day vs 50-day average volume
  median_turnover    20-day median turnover in rupees
  atr                average true range, 14 period
  suggested_stop     where the deterministic initial stop would sit

DATA:
<paste shortlist CSV here>

OUTPUT — exactly this and nothing else:
A table ranked best to worst:
  symbol | catalyst | catalyst status (fresh / maturing / spent / none found) |
  sector backdrop | main risk in next 90 days | verdict (BUY / WATCH / SKIP) |
  one-line reason

Then a short section listing every SKIP and why.

RULES:
- If you cannot identify a catalyst, write "none found". Do not invent one. A name
  with no findable catalyst is SKIP or WATCH, never BUY.
- Recommend at most 5 BUYs. If fewer deserve it, return fewer. Returning zero is
  a valid and useful answer.
- Flag explicitly where your information may be stale or you could not verify a
  claim. An unverified claim is worse than no claim.
- Do NOT give position sizing, price targets or stop losses. Those are computed
  deterministically by the pipeline and are not yours to set.
````

The last rule matters. Exit levels come from ATR and trend structure in stage 6,
and a model improvising a stop loss will quietly undo the part of the system that
is actually measurable.

## Configuration

Everything tunable lives in `config/config.toml` — endpoints, the universe
definition, rate limits, and (from stage 3 onward) every screen threshold and cost
parameter. No numeric threshold belongs in a module.

Credentials, when any exist, go in `config/secrets.toml`, which is gitignored and
never committed.

## Tests

```bash
./.venv/bin/python -m pytest -q     # 106 tests, offline, ~11s
cd ui && npm run check              # typecheck + render the components in node
```

Tests target the things that break silently rather than loudly.

**Corporate actions** (`test_adjust.py`)
- a 1:5 split makes history continuous rather than leaving an 80% cliff
- **two actions sharing an ex-date compound** — the BAJFINANCE 2:1 split plus
  4:1 bonus, which is 10x, not 5x or 2x
- applying only one of those two is *caught*, because 5x contradicts a 10x gap
- a consolidation adjusts history upward through the same code path
- a ratio disagreeing with the price gap is **not applied**
- a demerger is quarantined rather than adjusted with an invented number
- volume scales inversely; turnover is invariant

**RSI and friends** (`test_indicators.py`)
- RSI matches an **independent recursive implementation** of Wilder's definition
  written separately in the test file — comparing pandas against pandas would
  prove nothing
- Wilder smoothing is provably distinguishable from a rolling mean on the same
  data, which is the substitution that would otherwise go unnoticed
- both Wilder seedings converge over a 200-bar warmup
- RSI saturates at 100 / 0 / 50 and stays in bounds on noisy input
- ATR matches an independent loop; ADX separates trend from chop

**Look-ahead** (`test_lookahead.py`)
- **poisoning every bar after the exit changes nothing** — the trade comes out
  bit-identical
- a crash after the exit cannot retroactively stop the trade
- entry is always the bar *after* the signal, filled at that bar's open
- a stop gap fills at the open, not at a stop price that never traded
- a structural exit fills at the next open, because it is a close-based signal
- a forward window never reads the next symbol's bars
- the trailing stop uses only bars strictly before the one it is tested on

**Universe and survivorship** (`test_screen.py`)
- a symbol that later delists **is still screened on the dates it traded**
- a symbol listed late is absent before it listed
- quarantined symbols drop out for exactly their quarantine window
- a one-day volume spike does not satisfy a 20-day median liquidity floor

**Costs** (`test_costs.py`)
- GST applies to fees but **not** to STT
- stamp duty is buy-side only
- flat DP charges dominate small positions and barely register on large ones
- slippage widens monotonically as liquidity falls

**Web layer** (`test_web.py`)
- the server runs for real on an ephemeral port, not mocked
- the position lifecycle: add, list, close, delete, and 404 on an unknown id
- an entry date with no bar is rejected, because exits need that bar's ATR
- a symbol path cannot be used to traverse, and unknown routes return JSON
- the shortlist reports the same columns whether or not anything passed

**UI** (`ui/scripts/render-check.mjs`)
- the app and chart render in node without throwing
- the chart survives a single bar, all-null closes, and a flat series — the
  three shapes that produce `NaN` coordinates and a blank SVG

**Ingest** (`test_bhavcopy.py`, `test_store.py`, `test_calendar.py`)
- legacy and UDiFF parsers agree on **every field** of an overlap session
- NSE's one-off two-digit-year timestamp parses; an unknown format is a
  diagnosable error rather than a crash
- re-ingesting a session replaces rather than appends
- a run before NSE publishes cannot record today as a holiday
- blank numerics become None, not 0

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: no machine learning in
the signal path, every threshold in `config/config.toml`, raw prices stay
immutable, and the three correctness properties above each have dedicated tests
because breaking any of them flatters results rather than breaking them visibly.
