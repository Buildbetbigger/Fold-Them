# Sports Betting Market-Translation System — v0.1 Technical Specification

**Type:** Moneyline-only, two-way-market, soft-vs-sharp divergence measurement harness
**Mode:** Paper only. No real-money betting. No bet sizing. No human-approval step. No agents. No weather/props/spreads/totals. No web app.
**Governance (non-negotiable):** Claude never prices, never sizes, never decides whether to bet. Missing sharp source → no candidate. Stale data → rejection, never approximation. Rejections are logged as carefully as candidates. **The log is the product.**

> **Data conventions:** All timestamps are UTC, stored as ISO-8601 `TEXT` (e.g. `2026-05-30T18:42:11Z`). “Age” is `pull_timestamp − api_last_update` in seconds. The edge threshold is **committed before the run and locked**; it may not change mid-run.

-----

## 1. System overview

v0.1 is a read-only, paper-only harness that ingests moneyline (`h2h`, two-way) odds for selected sports from a single odds API at fixed intervals, designates **one** sharp book as the fair-price reference, de-vigs that book into a no-vig probability, and flags soft-book prices that diverge beyond a pre-committed edge threshold. Each flag is immediately **confirm-pulled** to discard latency artifacts, then written as either a **candidate** or a **rejection with a coded reason and trigger values**. Near each event’s start it captures the sharp **closing line**, grades every candidate by **Closing Line Value (CLV)**, and produces CLV-centric daily and cumulative reports. It places no bets, sizes nothing, and decides nothing; the persistent, auditable database is the deliverable.

**Exact success question v0.1 must answer:**

> At the pre-committed edge threshold, do soft-vs-sharp moneyline divergences in *this* odds API’s data (a) appear at a usable frequency, (b) **survive an immediate re-pull**, and (c) show **positive, statistically credible CLV** when graded against the captured sharp close — i.e., is there a mechanical signal worth building on, *independent of profit/loss*?

-----

## 2. Build components

Each component lists **Input → Output → Failure behavior**. “Failure” always means: write to `system_errors`, fail closed, and never partially commit derived data.

|Component                   |Input                                                 |Output                                                                                                      |Failure behavior                                                                                                                  |
|----------------------------|------------------------------------------------------|------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------|
|**API inventory script**    |config (base URL, region), API key (env)              |persisted list of available sports / bookmaker keys / market keys; console table                            |log to `system_errors`; exit non-zero; block downstream run                                                                       |
|**Raw odds ingestion job**  |config (sport_key, market_key, region, books), API key|one `raw_api_pulls` row (full payload, hashed) + parsed `events` / `bookmaker_snapshots` / `market_outcomes`|on HTTP/parse error → `system_errors` + `API_FAIL` rejection; abort this cycle inside one transaction (no partial derived rows)   |
|**Immutable raw storage**   |raw payload + pull metadata                           |append-only `raw_api_pulls` row with `payload_hash`                                                         |if raw write fails → abort cycle; **never** process derived data without stored raw                                               |
|**Normalization layer**     |raw team strings, market keys                         |`normalized_entities` mappings + canonical IDs attached to outcomes                                         |unresolved name → `NAME_NORM_FAIL` rejection; never guess a mapping                                                               |
|**Sharp-source validator**  |parsed snapshots for one event                        |validated sharp two-way price set + freshness                                                               |missing → `NO_SHARP`; stale → `STALE_SHARP`; primary+fallback disagree beyond tolerance → `SHARP_DISAGREE`; all yield no candidate|
|**Data-quality gate**       |in-progress opportunity context                       |`PASS` or rejection code + `trigger_values`                                                                 |first gate that trips writes a `rejections` row and stops processing that opportunity                                             |
|**Edge calculator**         |sharp no-vig prob + soft decimal                      |`edge_pct`                                                                                                  |invalid odds (≤0, out of range) → `PRICE_SANITY` rejection                                                                        |
|**Confirm-pull checker**    |flagged opportunity + delay list                      |`survived` bool + `post_edge_pct`                                                                           |re-pull error → treat as **not survived** (conservative) + log; edge gone → `TRANSIENT` rejection                                 |
|**Candidate writer**        |PASS opportunity, all fields                          |`candidates` row, `status=PENDING_GRADE`                                                                    |write failure → `system_errors`; abort cycle                                                                                      |
|**Rejection writer**        |rejection code + context + `trigger_values`           |`rejections` row                                                                                            |write failure → `system_errors` (a lost rejection is a defect)                                                                    |
|**Closing-line capture job**|events nearing commence + window/schedule             |`closing_lines` row (sharp two-way close, de-vigged)                                                        |no valid close in window → row with `close_source_flag=MISSING`                                                                   |
|**CLV calculator**          |candidate + matching `closing_lines`                  |`clv_results` row (`clv_pct`, `beat_close`)                                                                 |missing close → `clv_pct=NULL`, `grade_status=UNGRADED_CLOSE_MISSING`                                                             |
|**Summary report generator**|candidates / rejections / clv_results over window     |daily + cumulative CLV-led report                                                                           |log + emit partial report with explicit “INCOMPLETE” banner; no silent gaps                                                       |

-----

## 3. Database schema (SQLite)

```sql
-- 1. Immutable raw API payloads (source of truth; append-only)
CREATE TABLE raw_api_pulls (
    pull_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_run_id   INTEGER NOT NULL REFERENCES audit_runs(audit_run_id),
    pull_timestamp TEXT    NOT NULL,        -- UTC, your clock
    endpoint       TEXT    NOT NULL,
    sport_key      TEXT    NOT NULL,
    region         TEXT,
    market_key     TEXT    NOT NULL,
    http_status    INTEGER NOT NULL,
    payload_hash   TEXT    NOT NULL,        -- sha256 of raw_payload
    raw_payload    TEXT    NOT NULL         -- full JSON, never mutated
);
CREATE INDEX ix_raw_sport_ts ON raw_api_pulls(sport_key, pull_timestamp);

-- 2. Canonical events
CREATE TABLE events (
    event_id       TEXT PRIMARY KEY,        -- provider event id
    sport_key      TEXT NOT NULL,
    commence_time  TEXT NOT NULL,           -- UTC
    home_team_raw  TEXT NOT NULL,
    away_team_raw  TEXT NOT NULL,
    home_entity_id INTEGER REFERENCES normalized_entities(map_id),
    away_entity_id INTEGER REFERENCES normalized_entities(map_id),
    first_seen_ts  TEXT NOT NULL,
    last_seen_ts   TEXT NOT NULL
);
CREATE INDEX ix_events_sport_commence ON events(sport_key, commence_time);

-- 3. Per-pull, per-book market snapshots
CREATE TABLE bookmaker_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    pull_id         INTEGER NOT NULL REFERENCES raw_api_pulls(pull_id),
    event_id        TEXT    NOT NULL REFERENCES events(event_id),
    bookmaker_key   TEXT    NOT NULL,
    bookmaker_title TEXT,
    market_key      TEXT    NOT NULL,
    api_last_update TEXT    NOT NULL,       -- provider per-book timestamp
    pull_timestamp  TEXT    NOT NULL        -- denormalized for fast freshness math
);
CREATE INDEX ix_snap_lookup ON bookmaker_snapshots(event_id, bookmaker_key, market_key, pull_timestamp);

-- 4. Individual outcomes (prices)
CREATE TABLE market_outcomes (
    outcome_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id       INTEGER NOT NULL REFERENCES bookmaker_snapshots(snapshot_id),
    event_id          TEXT    NOT NULL,     -- denormalized
    bookmaker_key     TEXT    NOT NULL,     -- denormalized
    market_key        TEXT    NOT NULL,
    outcome_name_raw  TEXT    NOT NULL,
    outcome_entity_id INTEGER REFERENCES normalized_entities(map_id),
    price_american    INTEGER,             -- nullable if API returns decimal only
    price_decimal     REAL    NOT NULL,
    point             REAL                 -- always NULL in v0.1; schema-forward
);
CREATE INDEX ix_out_snap  ON market_outcomes(snapshot_id);
CREATE INDEX ix_out_event ON market_outcomes(event_id, bookmaker_key);

-- 5. Name normalization map (alias -> canonical, per sport)
CREATE TABLE normalized_entities (
    map_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sport_key      TEXT NOT NULL,
    raw_name       TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    canonical_id   TEXT NOT NULL,           -- stable slug
    UNIQUE(sport_key, raw_name)
);
CREATE INDEX ix_norm_canon ON normalized_entities(sport_key, canonical_id);

-- 6. Candidates (passed all gates)
CREATE TABLE candidates (
    candidate_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_ts             TEXT    NOT NULL,
    pull_id                 INTEGER NOT NULL REFERENCES raw_api_pulls(pull_id),
    event_id                TEXT    NOT NULL REFERENCES events(event_id),
    sport_key               TEXT    NOT NULL,
    commence_time           TEXT    NOT NULL,
    market_key              TEXT    NOT NULL,
    selection_raw           TEXT    NOT NULL,
    selection_canonical_id  TEXT    NOT NULL,
    sharp_book              TEXT    NOT NULL,
    sharp_decimal           REAL    NOT NULL,    -- selection side
    sharp_opp_decimal       REAL    NOT NULL,    -- other side (de-vig transparency)
    sharp_novig_prob        REAL    NOT NULL,
    sharp_last_update       TEXT    NOT NULL,
    sharp_age_s             REAL    NOT NULL,
    soft_book               TEXT    NOT NULL,
    soft_decimal            REAL    NOT NULL,
    soft_implied_prob       REAL    NOT NULL,
    soft_last_update        TEXT    NOT NULL,
    soft_age_s              REAL    NOT NULL,
    edge_pct                REAL    NOT NULL,    -- soft_decimal * sharp_novig_prob - 1, x100
    threshold_used          REAL    NOT NULL,
    confirm_pull_passed     INTEGER NOT NULL,    -- bool
    confirm_pull_post_edge_pct REAL,
    dq_status               TEXT    NOT NULL DEFAULT 'PASS',
    status                  TEXT    NOT NULL DEFAULT 'PENDING_GRADE', -- PENDING_GRADE|GRADED|UNGRADED
    notional_stake          REAL    NOT NULL DEFAULT 1.0,            -- bookkeeping only
    claude_note             TEXT                                     -- commentary only, nullable
);
CREATE INDEX ix_cand_event  ON candidates(event_id);
CREATE INDEX ix_cand_status ON candidates(status, sport_key, detected_ts);

-- 7. Rejections (logged with equal care)
CREATE TABLE rejections (
    rejection_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    rejected_ts    TEXT NOT NULL,
    stage          TEXT NOT NULL,           -- gate name
    pull_id        INTEGER REFERENCES raw_api_pulls(pull_id),
    event_id       TEXT,                    -- may be NULL at event-level failures
    sport_key      TEXT NOT NULL,
    commence_time  TEXT,
    market_key     TEXT,
    selection_raw  TEXT,
    sharp_book     TEXT,
    soft_book      TEXT,
    rejection_code TEXT NOT NULL,           -- see §6
    trigger_values TEXT NOT NULL            -- JSON, see §6
);
CREATE INDEX ix_rej_code  ON rejections(rejection_code, rejected_ts);
CREATE INDEX ix_rej_event ON rejections(event_id);

-- 8. Closing lines (sharp two-way close, de-vigged)
CREATE TABLE closing_lines (
    close_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                TEXT NOT NULL REFERENCES events(event_id),
    sport_key               TEXT NOT NULL,
    commence_time           TEXT NOT NULL,
    sharp_book              TEXT NOT NULL,
    close_pull_id           INTEGER REFERENCES raw_api_pulls(pull_id),
    close_capture_ts        TEXT,
    minutes_before_commence REAL,
    outcome_a_id            TEXT,           -- canonical_id
    outcome_a_decimal       REAL,
    outcome_a_novig         REAL,
    outcome_b_id            TEXT,
    outcome_b_decimal       REAL,
    outcome_b_novig         REAL,
    close_source_flag       TEXT NOT NULL,  -- NORMAL|FROM_SUSPENSION|MISSING
    UNIQUE(event_id, sharp_book)
);
CREATE INDEX ix_close_event ON closing_lines(event_id);

-- 9. CLV results (one per candidate)
CREATE TABLE clv_results (
    clv_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id      INTEGER NOT NULL UNIQUE REFERENCES candidates(candidate_id),
    close_id          INTEGER REFERENCES closing_lines(close_id),
    d_taken           REAL,                 -- soft_decimal at detection
    p_close_novig     REAL,                 -- selection side at close
    fair_close_decimal REAL,
    clv_pct           REAL,                 -- d_taken * p_close_novig - 1, x100
    beat_close        INTEGER,              -- bool: clv_pct > 0
    graded_ts         TEXT,
    grade_status      TEXT NOT NULL         -- GRADED|UNGRADED_CLOSE_MISSING
);
CREATE INDEX ix_clv_cand ON clv_results(candidate_id);

-- 10. Audit runs (reproducibility)
CREATE TABLE audit_runs (
    audit_run_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_label       TEXT,
    run_start_ts    TEXT NOT NULL,
    run_end_ts      TEXT,
    config_hash     TEXT NOT NULL,
    config_snapshot TEXT NOT NULL,          -- resolved config JSON
    code_version    TEXT,                   -- git sha
    status          TEXT NOT NULL           -- RUNNING|COMPLETED|ABORTED
);
CREATE INDEX ix_audit_start ON audit_runs(run_start_ts);

-- 11. System errors (every failure, no silent drops)
CREATE TABLE system_errors (
    error_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    error_ts     TEXT NOT NULL,
    audit_run_id INTEGER REFERENCES audit_runs(audit_run_id),
    component    TEXT NOT NULL,
    severity     TEXT NOT NULL,             -- WARN|ERROR|FATAL
    context      TEXT,                      -- JSON (pull_id/event_id/etc)
    message      TEXT NOT NULL,
    stack_trace  TEXT
);
CREATE INDEX ix_err_ts ON system_errors(error_ts, component);
```

-----

## 4. Configuration file

`config.yaml` (resolved copy is snapshotted into `audit_runs.config_snapshot`; **API key lives in an env var, never in this file**).

```yaml
run:
  run_label: "v0.1_feasibility_window_01"
  dry_run: true                 # paper only — hard guard against any bet placement
  threshold_locked: true        # forbids mid-run edge_threshold changes

api:
  base_url: "https://<provider>"
  region: "us"
  # api_key: read from env ODDS_API_KEY — NOT stored here

target:
  sport_keys: ["baseball_mlb"]  # start with ONE liquid 2-way sport
  market_key: "h2h"
  allowed_two_way_only: true
  excluded_sports: ["soccer_epl", "soccer_uefa_champs_league"]  # 3-way; out of scope

sharp_source:
  sharp_book_primary: "pinnacle"
  sharp_book_fallback: "circasports"
  sharp_disagree_tolerance_prob: 0.010   # >1.0 pt no-vig disagreement -> SHARP_DISAGREE

soft_books:
  - "draftkings"
  - "fanduel"
  - "betmgm"
  - "caesars"

timing:
  pull_interval_seconds: 90
  freshness_window_seconds:
    sharp: 120
    soft: 120
  confirm_pull_delays_seconds: [45]       # list allows multi-stage confirms later
  close_capture_window_minutes: 10        # last valid sharp price within 10m of commence
  close_polling_schedule:                 # minutes_before_commence -> poll interval (s)
    - { from_min: 60, to_min: 15, interval_s: 120 }
    - { from_min: 15, to_min: 0,  interval_s: 30 }

signal:
  edge_threshold_pct: 2.0                  # COMMITTED + LOCKED before the run

sanity:
  price_decimal_min: 1.01
  price_decimal_max: 51.0                  # ~ +5000

time:
  storage_timezone: "UTC"                  # all DB writes are UTC
  display_timezone: "America/New_York"     # report rendering only

logging:
  level: "INFO"                            # DEBUG|INFO|WARN|ERROR
```

-----

## 5. Deterministic formulas + pseudocode

All numbers below are produced by code. **Claude produces none of them.**

```text
function american_to_decimal(a):
    assert a is integer and abs(a) >= 100        # else PRICE_SANITY
    if a > 0:  return 1 + a / 100
    else:      return 1 + 100 / abs(a)

function decimal_to_implied(d):
    assert d > 1.0
    return 1 / d

function devig_two_way(d1, d2):                  # d1,d2 = the two sides' decimals
    q1 = 1 / d1
    q2 = 1 / d2
    overround = q1 + q2                           # > 1.0
    return (q1 / overround, q2 / overround)       # (p1_fair, p2_fair)

function edge_pct(p_fair, d_soft):               # p_fair from SHARP; d_soft from soft book
    return (p_fair * d_soft - 1) * 100            # candidate iff >= edge_threshold_pct

function confirm_pull_survives(opportunity, delay_s, threshold):
    sleep(delay_s)
    fresh = repull(opportunity.event_id, opportunity.sharp_book, opportunity.soft_book)
    if fresh.error: return (False, None)          # conservative: re-pull failure = not survived
    if not fresh.two_way or fresh.market_mismatch or fresh.name_mismatch:
        return (False, None)
    if fresh.sharp_age_s > window or fresh.soft_age_s > window:
        return (False, None)                      # went stale on confirm -> not survived
    p_fair = devig_two_way(fresh.sharp_sel, fresh.sharp_opp)[0]
    post   = edge_pct(p_fair, fresh.soft_decimal)
    return (post >= threshold, post)

function closing_novig(d_sel, d_opp):            # sharp two-way at close
    q_sel = 1 / d_sel
    q_opp = 1 / d_opp
    return q_sel / (q_sel + q_opp)                # p_close for the selection side

function clv_pct(d_taken, p_close):
    return (d_taken * p_close - 1) * 100

function beat_close(clv_pct_value):
    return clv_pct_value > 0
```

Worked check (used in unit tests): `d_taken = 2.50`, `p_close = 0.4545` → `clv_pct = (2.5*0.4545 − 1)*100 ≈ +13.6`, `beat_close = true`.

-----

## 6. Data-quality gates (execution order)

Each gate, in order. First trip → write `rejections` (code + `trigger_values` JSON) → stop processing that opportunity. Gates 1–11 are pre-detection; 12–13 are detection/confirm; 14 is grading-time.

|# |Gate                           |rejection_code        |`trigger_values` must store                                             |
|--|-------------------------------|----------------------|------------------------------------------------------------------------|
|1 |API response failure           |`API_FAIL`            |`{http_status, endpoint, error_msg, attempt_ts}`                        |
|2 |Missing event fields           |`EVENT_FIELDS_MISSING`|`{event_id?, missing_fields[], raw_snippet}`                            |
|3 |Missing sharp book             |`NO_SHARP`            |`{event_id, sharp_primary, sharp_fallback, books_present[]}`            |
|3b|Sharp primary/fallback disagree|`SHARP_DISAGREE`      |`{event_id, p_primary, p_fallback, diff, tolerance}`                    |
|4 |Stale sharp book               |`STALE_SHARP`         |`{event_id, sharp_book, api_last_update, pull_ts, age_s, window_s}`     |
|5 |Stale soft book                |`STALE_SOFT`          |`{event_id, soft_book, api_last_update, pull_ts, age_s, window_s}`      |
|6 |Non-two-way market             |`NOT_TWO_WAY`         |`{event_id, book, market_key, outcome_count, outcome_names[]}`          |
|7 |Market mismatch                |`MARKET_MISMATCH`     |`{event_id, sharp_market_key, soft_market_key}`                         |
|8 |Name normalization failure     |`NAME_NORM_FAIL`      |`{sport_key, raw_name, side, nearest_known?}`                           |
|9 |Missing outcome price          |`PRICE_MISSING`       |`{event_id, book, outcome_name}`                                        |
|10|Duplicate outcomes             |`DUP_OUTCOME`         |`{event_id, book, outcome_name, count}`                                 |
|11|Price out of sanity range      |`PRICE_SANITY`        |`{event_id, book, outcome_name, price_decimal, min, max}`               |
|12|Candidate below threshold      |`BELOW_THRESHOLD`     |`{event_id, selection, edge_pct, threshold}`                            |
|13|Transient divergence           |`TRANSIENT`           |`{event_id, selection, pre_edge_pct, post_edge_pct, delay_s, threshold}`|
|14|Missing close                  |`CLOSE_MISSING`       |`{event_id, sharp_book, last_pull_before_commence_ts?, window_minutes}` |

Rule: **fail closed, never impute.** A rejection is data, not an error — only true exceptions go to `system_errors`.

-----

## 7. Execution flow (pull → candidate/rejection)

```text
on each pull_interval_seconds tick (within an active audit_run):
  payload = GET odds(sport_key, market_key, region, books)        # gate 1: API_FAIL
  pull_id = INSERT raw_api_pulls(payload, hash, ts)               # raw FIRST; abort if fails

  BEGIN TRANSACTION
  for each event in payload:
      if missing required event fields: reject EVENT_FIELDS_MISSING; continue   # gate 2
      upsert events; refresh last_seen_ts
      parse bookmaker_snapshots + market_outcomes for target books

      sharp = resolve_sharp(event, primary, fallback)             # gates 3, 3b, 4
        - absent -> NO_SHARP -> continue
        - present but |p_primary - p_fallback| > tol -> SHARP_DISAGREE -> continue
        - chosen sharp stale -> STALE_SHARP -> continue
      if sharp not two-way (≠2 priced outcomes): NOT_TWO_WAY -> continue        # gate 6
      normalize sharp outcome names                                # gate 8: NAME_NORM_FAIL
      (p_sharp_a, p_sharp_b) = devig_two_way(sharp.dA, sharp.dB)

      for each soft_book present for this event:
          if soft stale: STALE_SOFT -> continue                    # gate 5
          if soft.market_key != sharp.market_key: MARKET_MISMATCH -> continue   # gate 7
          if soft not two-way: NOT_TWO_WAY -> continue
          normalize soft outcome names; map to sharp sides         # gate 8
          for each side S in {A, B}:
              if soft price for S missing: PRICE_MISSING -> continue            # gate 9
              if duplicate outcome for S: DUP_OUTCOME -> continue               # gate 10
              if soft.dS < min or > max: PRICE_SANITY -> continue               # gate 11
              p_fair = (S == A) ? p_sharp_a : p_sharp_b
              e = edge_pct(p_fair, soft.dS)
              if e < edge_threshold_pct: BELOW_THRESHOLD -> continue            # gate 12
              # flagged divergence -> confirm
              (survived, post) = confirm_pull_survives(opportunity, delay, thr) # gate 13
              if not survived: TRANSIENT(pre=e, post) -> continue
              INSERT candidates(... edge_pct=e, confirm_post=post, status=PENDING_GRADE)
  COMMIT
```

-----

## 8. Closing-line capture flow

```text
scheduler (runs continuously):
  upcoming = SELECT events WHERE commence_time within next 60 min
  for each event in upcoming:
      interval = lookup_interval(close_polling_schedule, minutes_to_commence)
      if due(event, interval):
          payload = GET odds(...)                                  # logged to raw_api_pulls
          snap = parse sharp_book two-way for event
          if snap valid and two-way and fresh:
              stash latest valid sharp snapshot for event (in-memory + raw store)

  # at/after commence_time, finalize close exactly once per event:
  for each event reaching commence_time without a closing_lines row:
      last = latest valid sharp snapshot with capture_ts in
             [commence_time - close_capture_window_minutes, commence_time)
      if last exists:
          (pA, pB) = devig_two_way(last.dA, last.dB)
          INSERT closing_lines(..., novig=pA/pB,
                               minutes_before_commence=Δ,
                               close_source_flag = last.was_suspended ? FROM_SUSPENSION : NORMAL)
      else:
          INSERT closing_lines(..., close_source_flag = MISSING)   # gate 14 surfaces at grade
      grade_clv_for_event(event)                                   # see §9 / §5

function grade_clv_for_event(event):
  for each candidate of event with status = PENDING_GRADE:
      close = SELECT closing_lines WHERE event_id = candidate.event_id
      if close is null or close.close_source_flag = MISSING:
          INSERT clv_results(candidate_id, clv_pct=NULL, beat_close=NULL,
                             grade_status='UNGRADED_CLOSE_MISSING')
          UPDATE candidate.status = 'UNGRADED'
      else:
          p_close = side_matching(candidate.selection, close)      # closing_novig already stored
          c = clv_pct(candidate.soft_decimal, p_close)
          INSERT clv_results(candidate_id, d_taken=candidate.soft_decimal,
                             p_close_novig=p_close, fair_close_decimal=1/p_close,
                             clv_pct=c, beat_close=(c>0), grade_status='GRADED')
          UPDATE candidate.status = 'GRADED'
```

-----

## 9. Reporting

Two reports (Markdown + CSV export). **Lead with CLV and trustworthiness. P/L appears last and is explicitly labeled non-significant at this sample.**

**Daily report — order is fixed:**

1. Candidate count (graded / ungraded split)
1. Rejection count **by reason** (table; all codes from §6)
1. Transient rate = `TRANSIENT / (candidates + TRANSIENT)`
1. Stale-data rate = `(STALE_SHARP + STALE_SOFT) / total opportunities evaluated`
1. Close-missing rate = `UNGRADED_CLOSE_MISSING / candidates`
1. Mean CLV% (graded only)
1. Median CLV% (graded only)
1. Beat-close rate = `count(beat_close=true) / graded`
1. CLV breakdowns: by sport, by soft book, and at the locked threshold
1. **Trustworthiness verdict** (see logic below)
1. *(last, labeled “INFORMATIONAL — NOT A PASS CRITERION”)* flat-1u P/L

**Cumulative report:** same structure across the full run window, plus a **CLV confidence interval** (mean CLV% with its lower bound) and the running candidate total against the §10/§14 minimum.

**Trustworthiness verdict logic (auto-computed):**

- `UNTRUSTWORTHY` if transient_rate dominates (e.g. > 50%) — most “edges” are latency artifacts.
- `UNTRUSTWORTHY` if close_missing_rate > 15% — sample can’t be CLV-graded reliably.
- `CAUTION` if stale_rate is high or graded count < minimum sample.
- `TRUSTWORTHY` otherwise.

-----

## 10. Unit tests (must pass before any live run)

- **Odds conversion:** `+150→2.50`, `-200→1.50`, `+100→2.00`, `-110→1.9091`; reject `0`, reject `|a|<100`.
- **De-vig:** `1.9091/1.9091 → (0.5, 0.5)`; `-150/+130` i.e. `1.6667/2.30 → ≈(0.580, 0.420)`; overround always >1; outputs sum to 1.
- **Stale timestamp:** age = `window − 1s` → pass; `window + 1s` → `STALE_*`; boundary documented.
- **Two-way market:** 2 priced outcomes → pass; 3 outcomes (draw) → `NOT_TWO_WAY`; 1 outcome → `NOT_TWO_WAY`.
- **Name normalization:** known alias → canonical_id; unknown string → `NAME_NORM_FAIL` (never silent map); home/away not swapped.
- **Edge threshold:** edge = `threshold − 0.01` → `BELOW_THRESHOLD`; `threshold + 0.01` → candidate.
- **Confirm-pull:** persisting edge → survived=true; vanished edge → `TRANSIENT`; re-pull error → survived=false; went-stale-on-confirm → survived=false.
- **CLV:** `d_taken=2.50, p_close=0.4545 → clv≈+13.6, beat=true`; `clv<0 → beat=false`; missing close → `UNGRADED_CLOSE_MISSING`, clv NULL.
- **Rejection logging:** every gate writes the correct code **and** the specified `trigger_values` keys; assert no opportunity exits a gate without either a candidate or a rejection row (no silent drops).

-----

## 11. Folder structure

```
market-translation-v0.1/
├── README.md
├── config.yaml                 # API key NOT here (env: ODDS_API_KEY)
├── requirements.txt
├── .env.example
├── src/
│   ├── __init__.py
│   ├── inventory.py            # API inventory script
│   ├── ingest.py               # raw pull + parse + immutable store
│   ├── normalize.py            # name/market normalization
│   ├── sharp_source.py         # sharp validator (NO_SHARP/STALE/DISAGREE)
│   ├── gates.py                # all §6 data-quality gates in order
│   ├── formulas.py             # §5 deterministic math (pure functions)
│   ├── confirm_pull.py         # transient check
│   ├── detect.py               # §7 execution flow
│   ├── closing.py              # §8 close capture + grading trigger
│   ├── clv.py                  # CLV calculator
│   ├── report.py               # §9 daily/cumulative report
│   ├── db.py                   # schema init + writers (candidates/rejections/errors)
│   └── config_loader.py        # load+hash+snapshot config
├── tests/
│   ├── test_formulas.py
│   ├── test_gates.py
│   ├── test_normalize.py
│   ├── test_confirm_pull.py
│   ├── test_clv.py
│   └── test_rejection_logging.py
├── data/
│   └── market_translation.sqlite
├── reports/
│   ├── daily/
│   └── cumulative/
└── scripts/
    ├── run_inventory.py
    ├── run_harness.py          # main loop (ingest+detect)
    ├── run_closing.py          # close capture scheduler
    └── run_report.py
```

-----

## 12. Implementation sequence

No dashboard/report-rendering work until the log, gates, candidate writer, rejection writer, close capture, and CLV calculation all work and pass tests.

1. `db.py` — schema init + `audit_runs` / `system_errors` writers.
1. `formulas.py` + `test_formulas.py` (all conversion/de-vig/edge/CLV tests green).
1. `config_loader.py` (load, hash, snapshot, lock-threshold enforcement).
1. `inventory.py` + `run_inventory.py` — confirm sport/sharp/books/market exist.
1. `ingest.py` — raw pull + immutable store + parse (transactional).
1. `normalize.py` + `test_normalize.py`.
1. `sharp_source.py` (NO_SHARP / STALE_SHARP / SHARP_DISAGREE).
1. `gates.py` + `test_gates.py` + `test_rejection_logging.py` (no silent drops).
1. `confirm_pull.py` + `test_confirm_pull.py`.
1. `detect.py` — wire §7 end-to-end; candidate + rejection writers live.
1. `closing.py` — §8 capture + grading trigger; tested on ≥1 real event.
1. `clv.py` + `test_clv.py`.
1. **Only now:** `report.py` (§9 CLV-led reports).
1. (Out of v0.1 scope: any dashboard/web app — explicitly deferred.)

-----

## 13. Claude usage in v0.1

**Preferred: none.** No Claude calls until §10 tests pass and §11–12 are built and the deterministic pipeline runs clean for at least one real window.

If used at all, exactly **one** allowed call type:

- **Allowed:** *Log/report summarization.* Input = rows already in `candidates` / `rejections` / `clv_results` / report tables. Output = prose summary that **cites the row IDs** it references (e.g. `candidate_id=412`, `rejection_id=2207`).
- **Hard constraints:**
  - Claude may **not** generate or alter any probability, edge, stake, CLV, threshold, or bet decision.
  - Every number in its summary must be **copied from the database**, not computed by Claude; numbers without a citing row ID are forbidden.
  - Claude has **read-only** access to the report dataset; it cannot write to any table.
  - Its output lands only in `candidates.claude_note` (commentary, nullable) or a separate report appendix — structurally isolated from all computed fields.

-----

## 14. Final v0.1 go/no-go checklist

Run for 3–5 days **only if every box is checked.**

**Build & correctness**

- [ ] All §10 unit tests pass (conversion, de-vig, stale, two-way, normalization, threshold, confirm-pull, CLV, rejection-logging).
- [ ] Raw storage verified immutable (append-only; no update/delete path).
- [ ] Every gate proven to write the correct code + `trigger_values`; **no silent drops** (assertion test green).
- [ ] Gates verified to **fail closed** (missing/stale → rejection, never imputation).

**Configuration**

- [ ] `edge_threshold_pct` committed and `threshold_locked: true`.
- [ ] `dry_run: true` (no bet-placement path exists anywhere in code).
- [ ] API key in env only; not in `config.yaml`; config hashed + snapshotted to `audit_runs`.
- [ ] Storage timezone = UTC verified on real writes.

**Feasibility preconditions (from inventory)**

- [ ] Target sport, `h2h` market, and **designated sharp book** confirmed present in the feed.
- [ ] Soft books confirmed present for the target sport.
- [ ] Pull cadence + freshness windows set; sharp observed updating at least as often as soft.

**Pipeline dry-validation**

- [ ] Confirm-pull tested against ≥1 real flagged divergence.
- [ ] Closing-line capture tested on ≥1 real event (NORMAL and a forced MISSING case).
- [ ] CLV computed end-to-end on a seeded/real candidate.
- [ ] `system_errors` path exercised (induced failure logs correctly and aborts the cycle).

**Interpretation guardrails (define before running)**

- [ ] Minimum graded sample for any pass/scale/kill decision committed (target 100–200 graded candidates).
- [ ] Pass standard pre-committed and CLV-centric: beat-close rate clearly >50% (start ≥55%), mean CLV% positive (start ≥ +1–2%) with CI lower bound > 0, transient rate not dominant, close-missing < ~15%. **P/L is not a pass criterion.**

> If the 3–5 day run returns `UNTRUSTWORTHY` (transient- or close-missing-dominated), the honest verdict is *the signal is not measurable in this data* — not “tune harder.” Stop and report, per the no-silent-failure rule.