# Patch P3a — Historical Gap + Cache-Key Precision

**Applies to:** Patch P3 (Historical Backtest Mode), which is adopted conceptually but not yet built. P3a refines P3 before implementation. No redesign; patches only gap accounting and the historical cache key.

**Invariants preserved:** no lookahead; walk-forward only; historical never replaces live; repeated sightings cannot inflate the sample; Claude never prices/sizes/decides.

**Central correction:** P3’s single `historical_gap_seconds = |requested − returned|` conflates two unrelated things. When ingestion chains via `next_timestamp`, the requested time *is* the prior snapshot’s `next_timestamp`, so request alignment is ~0 and tells you nothing about archive coverage. **Request alignment** (did the provider hand back a snapshot near what I asked for?) and **archive coverage** (is there an actual hole in the snapshot sequence I must replay over?) are now measured by separate fields.

-----

## 1. Historical gap accounting

Replace `historical_gap_seconds` with four fields. For a returned snapshot `S` (actual `snapshot_ts`), requested at `requested_ts`, with provider metadata `previous_timestamp` / `next_timestamp`, and `prior_returned_ts` = the previous snapshot **actually stored in this replay sequence**:

```
request_gap_seconds          = abs(requested_ts - snapshot_ts)
previous_snapshot_gap_seconds = snapshot_ts - previous_timestamp        # NULL if absent
next_snapshot_gap_seconds     = next_timestamp - snapshot_ts            # NULL if absent
inter_snapshot_gap_seconds    = snapshot_ts - prior_returned_ts         # NULL for first / across window boundary
```

**Which field is authoritative for which purpose:**

|Purpose                                                               |Field                                                                                                                                                                          |Rule                                                                    |
|----------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------|
|Did the provider return an acceptable snapshot for the requested time?|`request_gap_seconds`                                                                                                                                                          |acceptable if `≤ max_historical_gap_seconds` (chaining ⇒ ~0)            |
|Does replay coverage have an actual missing interval?                 |`inter_snapshot_gap_seconds`                                                                                                                                                   |**the coverage metric** (see §2)                                        |
|Archive density diagnostics                                           |`previous_snapshot_gap_seconds`, `next_snapshot_gap_seconds`                                                                                                                   |reporting; `next_*` also flags **upcoming** coverage risk               |
|Confirm reliability                                                   |gap between **detection snapshot and confirm snapshot** = `confirm_snapshot_ts − detection_snapshot_ts`                                                                        |`> max_historical_gap_seconds` ⇒ `TRANSIENT(CONFIRM_GAP_TOO_LARGE)`     |
|Close reliability                                                     |gap between **close snapshot and commence** = `commence_time − close_snapshot_ts`, **plus** archive-density warning if `previous/next/inter` gaps at the close exceed tolerance|close window governs MISSING; archive density sets `archive_gap_warning`|


> **Window-boundary subtlety (correctness, not cosmetics):** when `historical_game_window_enabled` is on and the sequence intentionally jumps from one slate’s window to the next, `inter_snapshot_gap_seconds` is set **NULL** (and the gap is *not* a coverage gap). An off-window skip is by design, not missing data. Because candidates and their confirm/close windows all live inside a single game window, off-window boundaries never touch any candidate.

-----

## 2. Coverage-gap logic

`coverage_gap` is defined by **actual snapshot spacing**, not request alignment:

```
coverage_gap = (inter_snapshot_gap_seconds IS NOT NULL)
               AND (inter_snapshot_gap_seconds > max_historical_gap_seconds)
               # (NULL inter-gap = first snapshot or intentional window boundary -> coverage_gap = false)
```

- `next_snapshot_gap_seconds > max_historical_gap_seconds` → log an **upcoming-coverage-risk** warning (forward-looking diagnostic; does not itself reject anything).
- A confirm target falling across a gap `> max_historical_gap_seconds` → `TRANSIENT(CONFIRM_GAP_TOO_LARGE)` (§6).
- A close snapshot too far from `commence_time` → `CLOSE_MISSING` / `UNGRADED` per the existing P3 §7 rules.

> **Config invariant (prevents self-inflicted gaps):** `historical_snapshot_interval_minutes × 60 ≤ max_historical_gap_seconds`. Otherwise your own subsampling produces `inter_snapshot_gap` larger than tolerance and manufactures false coverage gaps. `config_loader` must validate this and refuse to start if violated.

-----

## 3. Schema patch

Since P3 is unbuilt, the clean path is to **define these in `011_historical_mode.sql` directly** (replacing `historical_gap_seconds`); `ALTER`/`DROP` shown for any DB where 011 was already applied (SQLite `DROP COLUMN` needs ≥ 3.35).

```sql
-- raw_api_pulls: four precise gaps + cache-key fields (replace historical_gap_seconds)
ALTER TABLE raw_api_pulls ADD COLUMN request_gap_seconds           REAL;
ALTER TABLE raw_api_pulls ADD COLUMN previous_snapshot_gap_seconds REAL;
ALTER TABLE raw_api_pulls ADD COLUMN next_snapshot_gap_seconds     REAL;
ALTER TABLE raw_api_pulls ADD COLUMN inter_snapshot_gap_seconds    REAL;
ALTER TABLE raw_api_pulls ADD COLUMN bookmaker_set_hash            TEXT;
ALTER TABLE raw_api_pulls ADD COLUMN request_signature_hash        TEXT;
ALTER TABLE raw_api_pulls DROP COLUMN historical_gap_seconds;        -- if present

-- pull_cycles: request alignment + coverage spacing (coverage_gap already added in P3; redefined per §2)
ALTER TABLE pull_cycles   ADD COLUMN request_gap_seconds        REAL;
ALTER TABLE pull_cycles   ADD COLUMN inter_snapshot_gap_seconds REAL;
ALTER TABLE pull_cycles   DROP COLUMN historical_gap_seconds;       -- if present

-- closing_lines: close_gap_seconds (= commence_time - close_snapshot_ts, already added in P3) + archive flag
ALTER TABLE closing_lines ADD COLUMN archive_gap_warning INTEGER NOT NULL DEFAULT 0;  -- bool

-- candidate_observations: snapshot_sequence_num remains REQUIRED (added in P3; unchanged)
```

`snapshot_sequence_num` stays NOT-NULL-in-practice for historical observations and is the chronological tiebreak in replay.

-----

## 4. Cache-key precision

P3’s `UNIQUE(sport_key, region, historical_snapshot_ts) WHERE mode='HISTORICAL'` is wrong once markets or bookmaker sets vary: a payload fetched for `h2h` + 4 books does **not** contain what a later `spreads` / all-books request needs, yet the index would block re-fetching it.

**Preferred (better equivalent): a single `request_signature_hash`.**

```
bookmaker_set_hash     = sha256( ",".join(sorted(bookmaker_set)) )
request_signature_hash = sha256( sport_key | market_key | region |
                                 ",".join(sorted(bookmaker_set)) | odds_format | <any other request params> )
```

```sql
DROP INDEX IF EXISTS ux_hist_snapshot;
CREATE UNIQUE INDEX ux_hist_snapshot
    ON raw_api_pulls(request_signature_hash, historical_snapshot_ts)
    WHERE mode='HISTORICAL';
```

This is superior to the explicit composite `(sport_key, market_key, region, historical_snapshot_ts, bookmaker_set_hash)` because any future request dimension folds into the one hash without an index change. `bookmaker_set_hash` is still stored for diagnostics/queryability. (If you prefer explicit columns, the composite index is an acceptable equivalent.)

**Does not break v0.1:** with MLB `h2h` and a fixed book list, `request_signature_hash` is constant, so the index degenerates to exactly one row per `historical_snapshot_ts` — identical behavior to P3’s original index. The extra dimensions only ever matter if the config changes.

A cache hit (0 credits) is: a row already exists for `(request_signature_hash, expected_actual_ts)` — where `expected_actual_ts` is the prior snapshot’s `next_timestamp` during chaining. The UNIQUE index also enforces no duplicate storage at write time.

-----

## 5. Historical ingestion pseudocode patch

Chaining via `next_timestamp` stays; gap accounting is now based on **actual returned-snapshot spacing**.

```text
sig = request_signature_hash(sport, market, region, sorted(books))
prior_returned_ts = NULL
seq = 0
ts = historical_start_date

while ts < historical_end_date:
    if game_window_enabled and ts not in any game window:
        ts = start_of_next_game_window
        prior_returned_ts = NULL            # intentional boundary -> next inter-gap is NULL, not a coverage gap
        continue

    if exists raw_api_pulls(request_signature_hash=sig, historical_snapshot_ts=ts, mode='HISTORICAL'):
        S = load(...)                        # cache hit, 0 credits
    else:
        resp = GET historical(sport, market, region, books, date=ts)   # -> pull_cycles; failure -> pull_failures(requested_ts=ts)
        if running/estimated credits would exceed budget: HALT (CREDIT_BUDGET)
        request_gap = abs(ts - resp.timestamp)
        prev_gap    = (resp.timestamp - resp.previous_timestamp) if resp.previous_timestamp else NULL
        next_gap    = (resp.next_timestamp - resp.timestamp)     if resp.next_timestamp     else NULL
        inter_gap   = (resp.timestamp - prior_returned_ts)       if prior_returned_ts       else NULL
        S = store_raw_pull(snapshot_ts=resp.timestamp, requested_ts=ts,
                           request_gap_seconds=request_gap,
                           previous_snapshot_gap_seconds=prev_gap,
                           next_snapshot_gap_seconds=next_gap,
                           inter_snapshot_gap_seconds=inter_gap,
                           bookmaker_set_hash=..., request_signature_hash=sig,
                           snapshot_sequence_num=seq)

    coverage_gap = (S.inter_snapshot_gap_seconds IS NOT NULL
                    AND S.inter_snapshot_gap_seconds > max_historical_gap_seconds)
    write pull_cycles(outcome='SUCCESS', request_gap_seconds=S.request_gap_seconds,
                      inter_snapshot_gap_seconds=S.inter_snapshot_gap_seconds,
                      coverage_gap=coverage_gap, snapshot_sequence_num=seq)
    if S.next_snapshot_gap_seconds IS NOT NULL and S.next_snapshot_gap_seconds > max_historical_gap_seconds:
        log upcoming_coverage_risk(event_window=…)

    prior_returned_ts = S.historical_snapshot_ts
    seq += 1
    ts = max(S.next_timestamp, S.historical_snapshot_ts + interval_seconds)   # chain + subsample
```

-----

## 6. Historical replay pseudocode patch

Deltas to P3 §10 (C–H); everything else unchanged.

- **Detection (reliable snapshots only):** detection runs on every snapshot that **passes the data-quality gates** (fresh within the cadence-widened window, sharp present + fresh, two-way, names normalized, prices sane). A `coverage_gap` flag does **not** block detection *at that snapshot’s own timestamp* — the prices observed at `Tn` are real and available-at-`Tn`. A gap degrades only confirm (persistence across it) and close (archive density near commence), handled below.
- **Confirm:** uses the **first snapshot with `snapshot_ts ≥ confirm_due_ts`** (chronological). Compute `confirm_gap = confirm_snapshot_ts − detection_snapshot_ts`.
  - `confirm_gap > max_historical_gap_seconds` → `TRANSIENT(CONFIRM_GAP_TOO_LARGE)`.
  - target past `commence_time` → `TRANSIENT(CONFIRM_EXPIRED)`; no snapshot before data ends → `TRANSIENT(CONFIRM_NO_SNAPSHOT)`.
  - otherwise evaluate persistence (de-vig + edge at the confirm snapshot) exactly as P1.
- **Close:** `close_snapshot` = last valid sharp snapshot with `snapshot_ts < commence_time` within `close_capture_window_minutes`. Compute `close_gap_seconds = commence_time − close_snapshot_ts`.
  - `close_gap_seconds > close window` (or no sharp snapshot in window) → `CLOSE_MISSING` / `UNGRADED` (existing P3 §7 rule governs grading).
  - set `archive_gap_warning = 1` if archive density at the close is sparse (`inter`/`previous`/`next` snapshot gap at the close snapshot `> max_historical_gap_seconds`). This is a **reliability flag** that is reported; it does not by itself force MISSING — the close-window rule does.
- **Grade:** only `CONFIRMED` candidates; CLV from the close; one row per graded unique candidate; close used **only at grading** (no lookahead).

-----

## 7. Reporting patch

Add to the P3 §11 historical report **coverage block** (CLV still leads; CLV only on graded unique candidates; P/L last):

```
request_gap_rate            = COUNT(request_gap_seconds > max_historical_gap_seconds) / total_fetches
coverage_gap_rate           = COUNT(pull_cycles WHERE coverage_gap = 1) / total_cycles
median_inter_snapshot_gap_s = MEDIAN(inter_snapshot_gap_seconds WHERE NOT NULL)
max_inter_snapshot_gap_s    = MAX(inter_snapshot_gap_seconds)
confirm_gap_too_large_count = COUNT(rejections/TRANSIENT WHERE reason = 'CONFIRM_GAP_TOO_LARGE')
close_archive_gap_warnings  = COUNT(closing_lines WHERE archive_gap_warning = 1)
```

These are coverage/quality diagnostics, kept **separate** from candidate-rejection counts (same separation discipline as P2). The P3 capturability banner remains mandatory.

-----

## 8. Game-window clarification

H1 may run **either**:

- **A. game-window only** — *only if* the schedule/window planner is already implemented and tested; or
- **B. full-day coverage** — simpler, and acceptable if within `max_credit_budget`.

**Correctness of historical replay does not depend on the game-window planner.** Game-windowing is purely a cost optimization. If the planner isn’t built/tested, run **full-day** for H1 (set `historical_game_window_enabled: false`) and let the credit budget bound cost. Do not let game-window optimization delay or complicate replay correctness; add it later once H1 is validated.

-----

## Test deltas (to the P3 suite)

- **Four gaps computed correctly:** `request_gap = |requested − returned|`; `previous/next` from provider metadata; `inter` = returned − prior-returned-in-sequence; first snapshot and post-window-boundary `inter` = NULL.
- **Coverage by spacing:** `coverage_gap` trips on `inter_snapshot_gap > max` only; pure-chaining ticks (request_gap ≈ 0) with normal spacing are **not** coverage gaps.
- **Window boundary not a gap:** an intentional off-window jump yields `inter = NULL`, `coverage_gap = false`.
- **Config invariant:** `interval×60 > max_historical_gap_seconds` → `config_loader` refuses to start.
- **Cache-key dedup:** same `(request_signature_hash, snapshot_ts)` → cache hit, 0 credits, no duplicate row; a **different book set / market** (different `request_signature_hash`) for the same `snapshot_ts` is **allowed** to store; v0.1 fixed config degenerates to one row per `snapshot_ts`.
- **Confirm gap:** `confirm_snapshot_ts − detection_snapshot_ts > max` → `TRANSIENT(CONFIRM_GAP_TOO_LARGE)`.
- **Close archive warning:** sparse density at close sets `archive_gap_warning=1` without forcing MISSING; close-window breach still forces `UNGRADED`.
- **Report separation:** `request_gap_rate`, `coverage_gap_rate`, median/max `inter_snapshot_gap`, `confirm_gap_too_large_count`, `close_archive_gap_warnings` present and disjoint from rejection counts; CLV on graded unique only.