# Contributing

Thanks for looking at this. Before changing anything, please read the four
non-negotiables below — they are the reason the project is shaped the way it is,
and a change that violates one will be declined however well written it is.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/pip install -e .
./.venv/bin/python -m pytest        # 90 tests, all offline, ~9 seconds
```

Tests need no network and no database. Fixtures are two real bhavcopy files
committed under `tests/fixtures/`.

To work against real data you need a backfill first. It takes about an hour
against NSE's rate limit and produces ~685 MB:

```bash
./.venv/bin/nsemom backfill && ./.venv/bin/nsemom corpactions \
  && ./.venv/bin/nsemom adjust && ./.venv/bin/nsemom indicators
```

## The four non-negotiables

**1. No machine learning in the signal path.** No fitted models, no neural nets,
no trained scoring functions, no hyperparameter search over a learned objective.
This is not stylistic. The signal-to-noise ratio in daily equity bars is low
enough that a fitted model produces a flattering backtest and a disappointing
live account. Every rule must be arithmetic a person can read and reason about.

An LLM used *downstream* of the shortlist, driven by hand, is fine — that is the
documented workflow. An LLM or fitted model *inside* the screen, the ranking or
the exits is not.

**2. Every threshold lives in `config/config.toml`.** If you write a numeric
constant into a module, the review will ask you to move it. The exception is
arithmetic that is definitionally fixed — 100 in the RSI formula, 10,000 for
basis points.

**3. The three correctness properties.** Each one, when broken, biases results in
the *flattering* direction, which is why each has dedicated tests:

| property | enforced in | tested in |
|---|---|---|
| corporate actions adjusted, never mis-applied | `adjust.py` | `tests/test_adjust.py` |
| point-in-time universe, no survivorship bias | `screen.py` | `tests/test_screen.py` |
| no look-ahead | `backtest.py` | `tests/test_lookahead.py` |

**4. Raw data is immutable.** `prices` holds exactly what NSE published.
Adjustment is derived through a view over `adj_intervals`, so it stays
reproducible, auditable and reversible. Do not rewrite stored prices in place.

## Two habits worth copying

**Get the number two independent ways.** NSE states corporate action ratios only
as free text, so parsing is unavoidable — but the ex-date price gap implies the
same number without reading any text. Agreement means the parse is right;
disagreement is quarantined, never silently applied. This caught two real bugs.
If you add a parsed quantity and a second source exists, cross-check it.

**Fail loudly, never quietly.** A malformed file raises `ParseError` and marks
one session failed. A thin session fails the health check rather than producing a
confident-looking shortlist. Blank numerics become `None`, never `0`, because a
zero silently poisons an average.

## When NSE changes something

It will. We have already hit a two-digit-year timestamp on a single 2020 session
and two corporate actions packed into one subject string.

The fix is always to **add a branch and a test carrying the real string**, never
to loosen a check until the error stops. If you cannot parse something, raise a
diagnosable error naming the field — a crash mid-backfill is worse than one
failed date, and a silent wrong answer is worse than both.

## What a change needs

| change | also needs |
|---|---|
| a new indicator | a test against an independently written reference, not another library call |
| a new exit rule | a look-ahead test proving it cannot read the bar it reacts to |
| a new cost | a test pinning which components it applies to |
| a new screen preset | an entry in `config.toml`, not a code branch |
| a parser change | a test carrying the real NSE string that motivated it |

Backtest results in a PR description should say which period they came from and
whether the parameters were chosen on that same period. A number tuned on the
data it is measured on is not evidence.

## Style

Boring and readable over clever. Match the surrounding code. Comments should
explain *why* a thing is done, especially where the obvious approach is wrong —
those comments are load-bearing and several of them record real bugs.

## Raspberry Pi constraints

The target is a Pi 4 on an SD card, so:

- no write-amplifying patterns; bulk-insert in transactions, never row by row
- watch database growth — `indicators` is rebuilt in full each run and DuckDB
  does not reclaim replaced space, which is what `nsemom compact` exists for
- CPU-cheap only, no GPU, and prefer vectorised work over Python loops
- pinned dependencies must have `aarch64` manylinux wheels so nothing compiles
