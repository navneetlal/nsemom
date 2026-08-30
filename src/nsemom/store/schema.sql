-- Raw, UNADJUSTED bhavcopy rows exactly as published.
--
-- Corporate-action adjustment is deliberately not applied here. Adjusted prices
-- are derived in a view (stage 3) from a separate corporate_actions table, so
-- raw history stays immutable and every adjustment is reproducible and
-- reversible. Rewriting stored prices in place - as some NSE datasets do -
-- destroys the ability to recompute or audit.
--
-- No primary key: sessions are always written whole, so idempotency comes from
-- DELETE-by-date followed by INSERT inside one transaction. That is cheaper than
-- maintaining an ART index across ~7M rows during backfill. Sort order for the
-- symbol/date range scans is handled in stage 2.
CREATE TABLE IF NOT EXISTS prices (
    symbol      VARCHAR NOT NULL,
    trade_date  DATE    NOT NULL,
    isin        VARCHAR,
    series      VARCHAR NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    prev_close  DOUBLE,
    volume      BIGINT,
    turnover    DOUBLE,
    trades      BIGINT
);

-- One row per calendar weekday attempted, which is what makes backfill
-- resumable and stops re-downloading. Statuses:
--   ok       session stored
--   holiday  NSE served no file and the date is old enough to be conclusive
--   pending  no file yet, but too recent to call - retry on the next run
--   failed   fetch or parse error; the note column carries the reason
CREATE TABLE IF NOT EXISTS ingest_log (
    trade_date  DATE PRIMARY KEY,
    source      VARCHAR,
    status      VARCHAR   NOT NULL,
    row_count   INTEGER,
    content_sha VARCHAR,
    fetched_at  TIMESTAMP NOT NULL,
    note        VARCHAR
);

-- Corporate actions, with the parsed ratio AND the ratio implied by the price
-- gap on the ex-date. The NSE feed states ratios only as free text, so parsing
-- is unavoidable; cross-checking the parse against the observed gap is what
-- turns an unverifiable regex into something that fails loudly when wrong.
--
-- adj_factor semantics: prices BEFORE ex_date are divided by it, volumes
-- multiplied. A 1:5 split is 5.0; a 10:1 consolidation is 0.1.
--
-- verification:
--   verified    parsed factor agrees with the observed price gap
--   mismatch    they disagree - NOT applied, symbol quarantined
--   unverified  no usable price data on the ex-date - NOT applied by default
--   ignored     not a price-affecting action (dividend, AGM, etc.)
CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol       VARCHAR NOT NULL,
    ex_date      DATE    NOT NULL,
    isin         VARCHAR,
    series       VARCHAR,
    action_type  VARCHAR NOT NULL,
    subject      VARCHAR NOT NULL,
    face_value   DOUBLE,
    adj_factor   DOUBLE,
    observed     DOUBLE,
    deviation    DOUBLE,
    verification VARCHAR NOT NULL,
    note         VARCHAR,
    PRIMARY KEY (symbol, ex_date, subject)
);

-- Symbols excluded from the universe around an event we cannot adjust correctly:
-- demergers (value splits across entities with no single derivable ratio) and
-- any action whose parsed ratio contradicts the price gap.
CREATE TABLE IF NOT EXISTS quarantine (
    symbol     VARCHAR NOT NULL,
    from_date  DATE    NOT NULL,
    to_date    DATE    NOT NULL,
    reason     VARCHAR NOT NULL,
    PRIMARY KEY (symbol, from_date, reason)
);

-- What you actually bought. Populated by hand (or by `nsemom position add`)
-- after you act on a shortlist; the daily job evaluates exit rules against it.
CREATE TABLE IF NOT EXISTS positions (
    position_id  INTEGER PRIMARY KEY,
    symbol       VARCHAR NOT NULL,
    entry_date   DATE    NOT NULL,
    entry_price  DOUBLE  NOT NULL,
    quantity     INTEGER NOT NULL,
    exit_date    DATE,
    exit_price   DOUBLE,
    exit_reason  VARCHAR,
    note         VARCHAR
);

-- Every shortlist row ever emitted. Kept so the discretionary layer can be
-- measured later: comparing what you bought against the full shortlist is the
-- only way to know whether the LLM step adds or destroys value.
CREATE TABLE IF NOT EXISTS shortlist_log (
    as_of_date  DATE    NOT NULL,
    symbol      VARCHAR NOT NULL,
    rank        INTEGER NOT NULL,
    preset      VARCHAR NOT NULL,
    payload     VARCHAR NOT NULL,
    PRIMARY KEY (as_of_date, symbol, preset)
);

-- Step function of cumulative adjustment factors, one row per symbol per
-- contiguous span between corporate actions. Small (a few thousand rows), so
-- prices_adjusted can be a VIEW over a range join rather than a second copy of
-- every price - which matters on an SD card.
CREATE TABLE IF NOT EXISTS adj_intervals (
    symbol     VARCHAR NOT NULL,
    start_date DATE    NOT NULL,
    end_date   DATE    NOT NULL,
    cum_factor DOUBLE  NOT NULL,
    PRIMARY KEY (symbol, start_date)
);
