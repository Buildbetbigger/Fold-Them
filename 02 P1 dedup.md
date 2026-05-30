# v0.1 Precision Patch P1 — Sample Independence & Confirm Lifecycle

**Applies to:** Sports Betting Market-Translation System v0.1.
**Scope guard:** No redesign. No agents, weather, props, spreads, totals, bet sizing, human approval, or real-money betting. This patch touches only de-duplication, observation history, the confirm-pull lifecycle, the affected schema, reporting, and pass/fail.

**Invariants preserved:** Claude never prices / sizes / decides. The log is the product. Stale data → rejection. Missing sharp → no candidate. CLV leads. P/L is not a pass criterion.

-----

## 1. Unique candidate definition

**One unique candidate = one `opportunity_key`.** A candidate is a *priced* opportunity: a specific selection at a specific soft price, refereed by a specific sharp book, within one audit run. Repeated pulls that observe that same priced opportunity are **observations of the same candidate**, never new candidates.

**`opportunity_key`** (composite TEXT; the soft price is canonicalized to 4 dp so float equality is deterministic):

```
opportunity_key = "{audit_run_id}|{event_id}|{market_key}|{selection_canonical_id}"
                  "|{soft_book}|{sharp_book}|{soft_decimal:.4f}|{threshold_used:.4f}"
```

Stored verbatim on `candidates` under a **UNIQUE** constraint — that constraint is the structural guarantee against duplicate candidates. Note what is **in** the key (soft price) and what is **out** (sharp no-vig prob): a soft-price change mints a new candidate; a sharp-prob move does not.

**Resolution of every required case:**

|Situation                                              |Behavior                                                                                                                                                      |Why                                                                                                                                           |
|-------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------|
|Same event/selection/book/**price** on multiple pulls  |**Same candidate**; append observation; `observation_count += 1`; update `last_seen_ts`                                                                       |Identical `opportunity_key`                                                                                                                   |
|**Soft price changes**                                 |**New candidate** (new key); old candidate keeps its own price, confirm, and grade                                                                            |Different `soft_decimal` = different bet, different `d_taken`, different CLV                                                                  |
|**Sharp no-vig prob changes, soft price unchanged**    |**Same candidate**; append observation (its `edge_pct` reflects the new sharp prob); candidate identity unchanged                                             |Sharp prob is not in the key; the time series lives in observations                                                                           |
|Opportunity **disappears then reappears** at same price|**Same candidate**; append observation; gap is visible via `observed_ts` spacing                                                                              |Identical key; a blink does not create a new bet                                                                                              |
|Same opportunity **survives multiple confirm pulls**   |One candidate; confirm runs **once per candidate** over the configured delay list (`confirms_required`); repeat *detection* sightings never re-trigger confirm|Confirm is per-candidate lifecycle, not per-sighting                                                                                          |
|Same event yields edge on **both sides** (bad data)    |**Reject both** with `TWO_SIDED_EDGE`; create no candidate                                                                                                    |A single soft book showing edge on both sides of a de-vigged two-way market implies sub-1.0 hold = data error; picking a side would launder it|


> Terminal-state reappearance rule: if a `TRANSIENT` candidate’s exact key reappears, log the observation (diagnostic) but **do not** re-confirm or grade it — it already proved transient at that price. Any price change → new key → new candidate → fresh confirm.

> Confirm-never-completed rule: if an event reaches `commence_time` while a candidate is still `DETECTED`/`PENDING_CONFIRM`, close it to `TRANSIENT` with rejection reason `CONFIRM_EXPIRED`. It never became a persisted, confirmed opportunity, so it cannot enter the graded sample.

**Reports must distinguish five counts** (defined precisely in §5): raw detections · unique candidates · duplicate sightings · confirmed unique candidates · graded unique candidates.

-----

## 2. Candidate observation history

New table **`candidate_observations`** (chosen over `raw_detections` because every row links to its de-duplicated parent candidate). It stores **every** threshold-crossing sighting — detection-loop and confirm-pull — without minting independent candidates. Linkage: `candidate_observations.candidate_id → candidates.candidate_id`; `opportunity_key` is denormalized for direct querying.

```sql
CREATE TABLE candidate_observations (
    observation_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id       INTEGER NOT NULL REFERENCES candidates(candidate_id),
    opportunity_key    TEXT    NOT NULL,                       -- denormalized link
    pull_id            INTEGER NOT NULL REFERENCES raw_api_pulls(pull_id),
    observed_ts        TEXT    NOT NULL,
    phase              TEXT    NOT NULL                        -- which loop saw it
        CHECK (phase IN ('DETECTION','CONFIRM')),
    sharp_decimal      REAL    NOT NULL,                       -- selection side
    sharp_opp_decimal  REAL    NOT NULL,
    sharp_novig_prob   REAL    NOT NULL,
    sharp_age_s        REAL    NOT NULL,
    soft_decimal       REAL    NOT NULL,                       -- == candidate's (audit)
    soft_implied_prob  REAL    NOT NULL,
    soft_age_s         REAL    NOT NULL,
    edge_pct           REAL    NOT NULL,                       -- at this sighting
    UNIQUE(pull_id, opportunity_key)                           -- one sighting per pull per opp
);
CREATE INDEX ix_obs_candidate ON candidate_observations(candidate_id);
CREATE INDEX ix_obs_oppkey    ON candidate_observations(opportunity_key);
CREATE INDEX ix_obs_pull      ON candidate_observations(pull_id);
CREATE INDEX ix_obs_ts        ON candidate_observations(observed_ts);
```

`UNIQUE(pull_id, opportunity_key)` prevents double-logging the same opportunity within one pull (idempotency on re-processing). `phase` lets confirm re-pulls be recorded as first-class observations for audit.

-----

## 3. Confirm-pull lifecycle (decoupled from the detection transaction)

The v0.1 `sleep(delay)` inside the detection loop is removed. Confirm is realized by **scheduling** (`confirm_due_ts`), and executed by a **separate confirm worker** that polls. No process ever sleeps inside a DB transaction; the detection transaction commits immediately after writing candidate/observation/rejection rows.

**States & ownership:**

|State            |Meaning                                                                                                       |Set by                                |
|-----------------|--------------------------------------------------------------------------------------------------------------|--------------------------------------|
|`DETECTED`       |Candidate row created; first confirm scheduled (`confirm_due_ts` set); confirm not yet started                |Detection loop (on insert)            |
|`PENDING_CONFIRM`|Confirm claimed and in-flight (re-pull underway)                                                              |Confirm worker                        |
|`CONFIRMED`      |Survived all required confirm pulls; awaiting event start                                                     |Confirm worker                        |
|`TRANSIENT`      |Failed confirm (edge gone / went stale / re-pull error / price drifted off key / `CONFIRM_EXPIRED`) — terminal|Confirm worker (or grader at commence)|
|`PENDING_GRADE`  |Close captured; candidate claimed by grading job (in-flight)                                                  |Grading job                           |
|`GRADED`         |CLV computed — terminal                                                                                       |Grading job                           |
|`UNGRADED`       |Close missing/unusable; `clv_pct = NULL` — terminal                                                           |Grading job                           |

**Transitions:**

```
DETECTED ──(worker claims, due)──> PENDING_CONFIRM
PENDING_CONFIRM ──(all confirms pass)──> CONFIRMED ──(close captured, claimed)──> PENDING_GRADE ──(CLV)──> GRADED
PENDING_CONFIRM ──(fail / repull error / stale / off-key)──> TRANSIENT
PENDING_GRADE ──(close missing)──> UNGRADED
{DETECTED|PENDING_CONFIRM} ──(event reached commence)──> TRANSIENT (reason CONFIRM_EXPIRED)
```

**Multi-confirm:** with `confirm_pull_delays_seconds = [d1, d2, …]`, `confirms_required = len(list)`. Each passing confirm increments `confirms_passed` and reschedules `confirm_due_ts = now + d_next`; reaching `confirms_required` → `CONFIRMED`. Any single failure → `TRANSIENT`. The confirm worker’s queue is `candidates` rows where `status IN ('DETECTED','PENDING_CONFIRM') AND confirm_due_ts <= now` (backed by an index). Each confirm re-pull writes its own `raw_api_pulls` row (raw-first invariant) and a `phase='CONFIRM'` observation.

-----

## 4. Database schema patch

### 4a. Revised `candidates` (authoritative — pre-implementation rebuild)

```sql
CREATE TABLE candidates (
    candidate_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_key          TEXT    NOT NULL UNIQUE,          -- DEDUP GUARANTEE
    audit_run_id             INTEGER NOT NULL REFERENCES audit_runs(audit_run_id),
    first_pull_id            INTEGER NOT NULL REFERENCES raw_api_pulls(pull_id),
    event_id                 TEXT    NOT NULL REFERENCES events(event_id),
    sport_key                TEXT    NOT NULL,
    commence_time            TEXT    NOT NULL,
    market_key               TEXT    NOT NULL,
    selection_raw            TEXT    NOT NULL,
    selection_canonical_id   TEXT    NOT NULL,
    sharp_book               TEXT    NOT NULL,
    soft_book                TEXT    NOT NULL,
    soft_decimal             REAL    NOT NULL,                 -- d_taken; fixed per key
    threshold_used           REAL    NOT NULL,
    -- first-detection snapshot (stable; full series lives in candidate_observations)
    first_seen_ts            TEXT    NOT NULL,
    last_seen_ts             TEXT    NOT NULL,
    observation_count        INTEGER NOT NULL DEFAULT 1,
    detect_sharp_decimal     REAL    NOT NULL,
    detect_sharp_opp_decimal REAL    NOT NULL,
    detect_sharp_novig_prob  REAL    NOT NULL,
    detect_edge_pct          REAL    NOT NULL,                 -- edge at first detection
    -- confirm lifecycle
    status                   TEXT    NOT NULL DEFAULT 'DETECTED'
        CHECK (status IN ('DETECTED','PENDING_CONFIRM','CONFIRMED',
                          'TRANSIENT','PENDING_GRADE','GRADED','UNGRADED')),
    confirm_due_ts           TEXT,
    confirms_required        INTEGER NOT NULL DEFAULT 1,
    confirms_attempted       INTEGER NOT NULL DEFAULT 0,
    confirms_passed          INTEGER NOT NULL DEFAULT 0,
    confirm_first_ts         TEXT,
    confirm_last_ts          TEXT,
    confirm_post_edge_pct    REAL,                             -- edge at last confirm
    -- bookkeeping
    notional_stake           REAL    NOT NULL DEFAULT 1.0,     -- flat; never sizing
    claude_note              TEXT                              -- commentary only
);
CREATE INDEX ix_cand_event   ON candidates(event_id);
CREATE INDEX ix_cand_status  ON candidates(status, sport_key, first_seen_ts);
CREATE INDEX ix_cand_confirm ON candidates(status, confirm_due_ts);  -- confirm-worker queue
```

(The inline `UNIQUE(opportunity_key)` already creates its own index; no separate unique index needed.)

### 4b. `candidate_observations`

As defined in §2.

### 4c. `rejections` patch (correlate dedup-aware rejections + new code)

```sql
ALTER TABLE rejections ADD COLUMN opportunity_key TEXT;        -- nullable
CREATE INDEX ix_rej_oppkey ON rejections(opportunity_key);
```

New rejection code **`TWO_SIDED_EDGE`** — `trigger_values`:
`{event_id, soft_book, sharp_book, side_a, edge_a_pct, side_b, edge_b_pct, soft_implied_sum}`.
Extended use of **`TRANSIENT`** — add optional `reason ∈ {VANISHED, WENT_STALE, REPULL_ERROR, OFF_KEY_PRICE, CONFIRM_EXPIRED}` inside `trigger_values`.

### 4d. ALTER path (if the v0.1 `candidates` was already created)

```sql
ALTER TABLE candidates ADD COLUMN opportunity_key          TEXT;
ALTER TABLE candidates ADD COLUMN first_seen_ts            TEXT;
ALTER TABLE candidates ADD COLUMN last_seen_ts             TEXT;
ALTER TABLE candidates ADD COLUMN observation_count        INTEGER NOT NULL DEFAULT 1;
ALTER TABLE candidates ADD COLUMN detect_sharp_decimal     REAL;
ALTER TABLE candidates ADD COLUMN detect_sharp_opp_decimal REAL;
ALTER TABLE candidates ADD COLUMN detect_sharp_novig_prob  REAL;
ALTER TABLE candidates ADD COLUMN detect_edge_pct          REAL;
ALTER TABLE candidates ADD COLUMN confirm_due_ts           TEXT;
ALTER TABLE candidates ADD COLUMN confirms_required        INTEGER NOT NULL DEFAULT 1;
ALTER TABLE candidates ADD COLUMN confirms_attempted       INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN confirms_passed          INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN confirm_first_ts         TEXT;
ALTER TABLE candidates ADD COLUMN confirm_last_ts          TEXT;
CREATE UNIQUE INDEX ux_cand_oppkey ON candidates(opportunity_key);
CREATE INDEX ix_cand_confirm ON candidates(status, confirm_due_ts);
-- NOTE: SQLite cannot add the status CHECK constraint via ALTER. Either enforce the
-- 7-state set in application code, or rebuild the table (4a) before the first real run.
```

-----

## 5. Reporting patch (cannot inflate the sample)

Daily and cumulative reports **lead with the funnel**, computed over the report window. P/L stays last and non-significant.

**Funnel — fixed lead order:**

```
raw_detections        = COUNT(candidate_observations)                         -- every sighting
unique_candidates     = COUNT(candidates)                                     -- distinct opportunity_keys
duplicate_sightings   = raw_detections - unique_candidates                    -- = Σ(observation_count - 1)
confirmed_unique      = COUNT(candidates WHERE confirms_passed = confirms_required)
graded_unique         = COUNT(candidates WHERE status = 'GRADED')
```

**Rates — denominators are UNIQUE opportunities, never sightings:**

```
transient_unique      = COUNT(candidates WHERE status = 'TRANSIENT')
transient_rate        = transient_unique / (confirmed_unique + transient_unique)   -- decided-confirm only
ungraded_unique       = COUNT(candidates WHERE status = 'UNGRADED')
close_missing_rate    = ungraded_unique / (confirmed_unique)                       -- of those that should grade
stale_data_rate       = (STALE_SHARP + STALE_SOFT rejections) / opportunities_evaluated  -- evaluation-level, reported separately from the funnel
```

**CLV — graded unique candidates only (one CLV per candidate):**

```
mean_CLV%   = AVG(clv_pct)    over status='GRADED'
median_CLV% = MEDIAN(clv_pct) over status='GRADED'
beat_close_rate = COUNT(beat_close=1) / graded_unique
```

Then breakdowns (by sport, soft book, threshold) **restricted to graded unique candidates.**

**Trustworthiness verdict (now unique-based):**

- `UNTRUSTWORTHY` if `transient_rate` (unique) dominates (> 0.50) — most “edges” are latency artifacts.
- `UNTRUSTWORTHY` if `close_missing_rate` > 0.15.
- `CAUTION` if `stale_data_rate` high or `graded_unique` below the §6 feasibility minimum.
- `TRUSTWORTHY` otherwise.

**Last line, labeled “INFORMATIONAL — NOT A PASS CRITERION”:** flat-1u P/L over graded unique candidates.

-----

## 6. Pass/fail patch — two tiers

The single CI-based bar is split. The first 100–200 graded unique candidates do **not** need a confidence-interval lower bound above zero; at that size CLV is directional, not validated.

### Tier 1 — Early feasibility pass → *“continue testing”*

Checkpoint: **≥ 100–200 graded *unique* candidates** (not sightings).
Pass (all):

- Trustworthiness = `TRUSTWORTHY` (transient_unique rate not dominant; close_missing < ~15%).
- `beat_close_rate` directionally > 50% (start ≥ ~53%).
- `mean_CLV%` > 0.
- **No** confidence-interval requirement.

Meaning: the signal is plausibly real and *measurable in this data* — keep running, keep accumulating. This is **not** a profitability or validation claim.
**Kill the angle** here if `mean_CLV%` ≤ 0 or the run is `UNTRUSTWORTHY` (transient- or close-missing-dominated). The honest verdict is “no measurable signal,” not “tune harder.”

### Tier 2 — Statistical confidence pass → *“validated mechanical edge”*

Checkpoint: **larger sample, sample-size-dependent — target ≥ ~500–1000 graded unique candidates** (more if the effect is small).
Pass (all):

- `mean_CLV%` > 0 with the **lower bound of its confidence interval > 0** (bootstrap preferred over normal-approx; CLV is skewed).
- `beat_close_rate` robustly > 50%.
- Result **stable across sub-windows**, not driven by a handful of outliers.
- Trustworthiness sustained across the window.

Meaning: a validated mechanical signal — the only state in which anything beyond v0.1 may be *considered*.

> Honesty caveat (unchanged): validated CLV is necessary, not sufficient, for real profit. Capturability — whether the soft book would actually let you take and keep the price — is untested in paper and remains a separate, later question.

-----

## 7. Patched execution flow (pull → confirm → grade)

Three independent jobs. **No `sleep` inside any transaction.**

### Job A — Detection loop (commits fast)

```
on each pull_interval tick (active audit_run):
  payload = GET odds(sport_key, market_key, region, books)        # gate 1 API_FAIL -> reject; return
  pull_id = INSERT raw_api_pulls(payload, hash, ts)               # raw FIRST; abort cycle if this fails

  BEGIN TRANSACTION
  for each event in payload:
      run v0.1 gates 2..11 (EVENT_FIELDS_MISSING, NO_SHARP, SHARP_DISAGREE, STALE_*,
          NOT_TWO_WAY, MARKET_MISMATCH, NAME_NORM_FAIL, PRICE_MISSING, DUP_OUTCOME, PRICE_SANITY)
          -> on trip: write rejection (+opportunity_key if computable); continue

      (p_sharp_a, p_sharp_b) = devig_two_way(sharp.dA, sharp.dB)
      for each soft_book present:
          crossings = []
          for side S in {A, B}:
              e = edge_pct(p_fair_S, soft.dS)
              if e < threshold: reject BELOW_THRESHOLD; continue
              crossings.append((S, e, soft.dS, ...))

          # two-sided bad-data guard (same event+soft_book+sharp_book)
          if len(crossings) == 2:
              reject TWO_SIDED_EDGE for BOTH sides
                  (trigger: both edges, soft_implied_sum); continue

          for (S, e, soft_dS, ...) in crossings:
              oppkey = build_opportunity_key(audit_run_id, event_id, market_key,
                                             sel_canon_S, soft_book, sharp_book,
                                             soft_dS, threshold)
              cand = SELECT candidates WHERE opportunity_key = oppkey
              if cand is NULL:
                  cand_id = INSERT candidates(
                      opportunity_key=oppkey, status='DETECTED',
                      confirm_due_ts = pull_ts + confirm_delays[0],
                      confirms_required = len(confirm_delays),
                      first_seen_ts=last_seen_ts=pull_ts, observation_count=1,
                      detect_sharp_*=..., detect_edge_pct=e, soft_decimal=soft_dS,
                      first_pull_id=pull_id, ...)
                  INSERT candidate_observations(cand_id, oppkey, pull_id,
                      phase='DETECTION', edge_pct=e, ...)
              else:
                  # repeat sighting: sharp-moved-same-soft, reappearance, or steady-state
                  INSERT candidate_observations(cand.id, oppkey, pull_id,
                      phase='DETECTION', edge_pct=e, ...)      # UNIQUE(pull_id,oppkey) dedups
                  UPDATE candidates SET last_seen_ts=pull_ts,
                      observation_count = observation_count + 1
                      WHERE candidate_id = cand.id
                  # DO NOT reschedule confirm; DO NOT reopen TRANSIENT/CONFIRMED
  COMMIT                                                          # <- no sleep anywhere above
```

### Job B — Confirm worker (separate process; polls every few seconds)

```
loop:
  due = SELECT candidates
        WHERE status IN ('DETECTED','PENDING_CONFIRM') AND confirm_due_ts <= now
  for cand in due:
      UPDATE cand SET status='PENDING_CONFIRM', confirms_attempted = confirms_attempted+1,
                      confirm_first_ts = COALESCE(confirm_first_ts, now)
      if now >= cand.commence_time:                              # event started first
          UPDATE cand SET status='TRANSIENT'
          reject TRANSIENT(reason=CONFIRM_EXPIRED, opportunity_key=cand.oppkey); continue

      fresh = repull(cand.event_id, cand.sharp_book, cand.soft_book)   # writes its own raw_api_pulls row
      fail =  fresh.error
           OR fresh.sharp_age_s > window OR fresh.soft_age_s > window
           OR not fresh.two_way OR fresh.name_mismatch
           OR fmt4(fresh.soft_decimal) != fmt4(cand.soft_decimal)     # drifted off key price
      if fail:
          UPDATE cand SET status='TRANSIENT'
          reject TRANSIENT(reason=<VANISHED|WENT_STALE|REPULL_ERROR|OFF_KEY_PRICE>,
                           pre=cand.detect_edge_pct, post=fresh.edge_or_null,
                           opportunity_key=cand.oppkey); continue

      p_fair = devig_two_way(fresh.sharp_sel, fresh.sharp_opp)[selection_side]
      post   = edge_pct(p_fair, fresh.soft_decimal)
      INSERT candidate_observations(cand.id, cand.oppkey, fresh.pull_id,
                                    phase='CONFIRM', edge_pct=post, ...)
      if post >= cand.threshold_used:
          UPDATE cand SET confirms_passed = confirms_passed+1,
                          confirm_last_ts=now, confirm_post_edge_pct=post
          if cand.confirms_passed >= cand.confirms_required:
              UPDATE cand SET status='CONFIRMED'
          else:
              UPDATE cand SET confirm_due_ts = now + confirm_delays[confirms_passed]  # next confirm
      else:
          UPDATE cand SET status='TRANSIENT'
          reject TRANSIENT(reason=VANISHED, pre=cand.detect_edge_pct, post=post,
                           opportunity_key=cand.oppkey)
  sleep(poll_interval)        # OUTSIDE any transaction; not a per-candidate delay
```

### Job C — Closing capture + grading (from §8 of v0.1)

```
on close finalize for an event (exactly one closing_lines row written):
  # expire any still-unconfirmed candidates for this event
  UPDATE candidates SET status='TRANSIENT'
    WHERE event_id=event AND status IN ('DETECTED','PENDING_CONFIRM')   # reason CONFIRM_EXPIRED

  for cand in SELECT candidates WHERE event_id=event AND status='CONFIRMED':
      UPDATE cand SET status='PENDING_GRADE'                     # claim for grading
      close = closing_lines for (event, sharp_book)
      if close is NULL or close.close_source_flag='MISSING':
          INSERT clv_results(cand.id, clv_pct=NULL, beat_close=NULL,
                             grade_status='UNGRADED_CLOSE_MISSING')
          UPDATE cand SET status='UNGRADED'
      else:
          p_close = closing_novig(side of close matching cand.selection_canonical_id)
          c = clv_pct(cand.soft_decimal, p_close)               # d_taken * p_close - 1, x100
          INSERT clv_results(cand.id, d_taken=cand.soft_decimal, p_close_novig=p_close,
                             fair_close_decimal=1/p_close, clv_pct=c,
                             beat_close=(c > 0), grade_status='GRADED')
          UPDATE cand SET status='GRADED'
```

Only `CONFIRMED` candidates are graded; each contributes **exactly one** CLV row — the graded-unique sample is, by construction, free of duplicate sightings.