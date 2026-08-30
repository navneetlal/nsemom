## What and why

<!-- What changes, and what problem it solves. Link the issue if there is one. -->

## How it was verified

<!--
Test output, or the commands you ran against real data. For anything touching
the screen, exits or costs, include before/after backtest numbers.
-->

```
```

## Checklist

- [ ] `./.venv/bin/python -m pytest` passes
- [ ] New behaviour has a test; a bug fix has a test that fails without the fix
- [ ] No numeric threshold added to a module — it went in `config/config.toml`
- [ ] No fitted model, neural net or trained scoring function in the signal path
- [ ] Raw `prices` rows are still written exactly as NSE published them

### If this touches the screen, indicators, exits or costs

- [ ] Backtest numbers below say **which period** they cover
- [ ] Parameters were **not** chosen by looking at the period being reported —
      or the PR says plainly that they were
- [ ] Gross and net are both shown, so the cost effect is visible

### If this touches a parser or NSE endpoint

- [ ] A test carries the **real NSE string** that motivated the change
- [ ] Unparseable input raises a diagnosable error naming the field, rather than
      crashing the run or silently returning a wrong value
- [ ] No existing check was loosened just to make an error stop

### If this touches storage or the daily job

- [ ] Bulk writes stay in transactions — nothing row-by-row
- [ ] Database growth was checked (`nsemom status`), since `indicators` is
      rebuilt in full each run and DuckDB does not reclaim replaced space
