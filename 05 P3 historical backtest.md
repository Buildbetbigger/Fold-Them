# Patch P3 — Historical Backtest Mode

**Applies to:** Market-Translation System v0.1 + P1 (dedup) + P2 (pull failures / clock skew) + P2a (attempt vs cycle) + build plan. No redesign; this patch adds a second run mode and the bookkeeping it requires.

**Core principle (governs everything below):** Historical data may *accelerate signal discovery, but it does not replace live capturability testing.* A historical pass only ever unlocks the 3–5 day live audit; it never substitutes for it.

**Invariants preserved:** Claude never prices / sizes / decides. No lookahead. Walk-forward only. Candidate generation uses only information available at the historical timestamp. Closing lines are used only at grading. Repeated sightings cannot inflate the sample. CLV only on graded unique candidates. The log remains the product.

-----

## 1. P3 scope lock

**Allowed:** pull historical odds snapshots; replay them strictly chronologically; apply the *same* sharp-source rule, data-quality gates, and `opportunity_key` logic; generate historical candidates, observations, and rejections; assign historical closing lines; compute historical CLV; produce historical reports.

**Forbidden:** lookahead candidate selection; threshold tuning inside the same backtest window; using closing lines (or any future snapshot) to decide candidates; treating historical success as proof of live capturability; real-money betting; Claude pricing/sizing/deciding.

-----

## 2. Historical-mode architecture

A `mode` flag (`LIVE | HISTORICAL`) selects the **data source and the time cursor**, not the decision logic. The deterministic pipeline is identical in both modes.

**Refactor (the key to “same pipeline”):** the gate chain, de-vig/edge math, `opportunity_key`, candidate upsert/observation dedup, confirm evaluation, close finalize, and CLV grading are extracted into mode-agnostic core functions that operate on a **normalized snapshot bundle** — a sport/event/book/outcome structure with a single `snapshot_ts`. Two drivers feed that core:

- **LIVE** (`detect.py` + `confirm.py` + `closing.py`): bundle comes from `api_client` pulls; the time cursor is the wall clock; confirm fires off `confirm_due_ts` polled by the confirm worker.
- **HISTORICAL** (`replay.py`): bundle comes from stored historical snapshots; the time cursor is `historical_snapshot_ts`; confirm fires when the chronological walk reaches the first snapshot at/after the confirm target.

|Component                                                                                                              |Shared                              |Historical-specific                                                    |
|-----------------------------------------------------------------------------------------------------------------------|------------------------------------|-----------------------------------------------------------------------|
|`formulas.py`, `opportunity_key.py`, `gates.py`, `normalize.py`, `sharp_source.py`, `clv.py`, `repo.py`, `constants.py`|✅ reused unchanged                  |—                                                                      |
|Candidate lifecycle + dedup (P1)                                                                                       |✅ same states/keys                  |confirm/grade are snapshot-driven, not clock-driven                    |
|Failure accounting (P2/P2a)                                                                                            |✅ same `pull_failures`/`pull_cycles`|a “cycle” = one historical snapshot fetch; adds coverage-gap accounting|
|Report (P2a)                                                                                                           |✅ same funnel                       |adds snapshot/gap metrics + capturability warning; filtered by `mode`  |
|Ingestion                                                                                                              |—                                   |`historical_ingest.py` (chaining fetcher)                              |
|Driver                                                                                                                 |`detect/confirm/closing` (LIVE)     |`replay.py`, `run_replay.py` (HISTORICAL)                              |

**One DB, `mode`-tagged.** Historical and live rows never mix in reports (same separation discipline as P2’s pull-vs-rejection split). `opportunity_key` already embeds the run id, so historical and live candidates never collide.

-----

## 3. Historical API ingestion

The Odds API historical endpoint returns the **single snapshot nearest the requested time**, with `timestamp` (actual), `previous_timestamp`, and `next_timestamp`. Ingestion exploits this rather than blindly hammering a fixed grid.

**Strategy — chain via `next_timestamp` (cheaper, dedup-safe):** seed at `historical_start_date`; fetch the returned snapshot; record its actual `timestamp`; jump to `next_timestamp`; repeat to `historical_end_date`. Optionally **subsample** to the configured interval by skipping `next_timestamp`s closer than `historical_snapshot_interval_minutes`. This fetches each actual snapshot **once** (no duplicate credits) and exposes gaps directly as large `next_timestamp` jumps.

**Required inputs/fields per fetch:** `sport_key`, `market_key`, `region`, bookmaker list, `historical_requested_ts`, `historical_snapshot_ts` (actual returned), `historical_previous_ts`, `historical_next_ts`, `historical_gap_seconds` (= |requested − returned|), `raw_payload_hash`, `snapshot_sequence_num`.

**Storage rules:** raw-first (store the payload + hash before any derived processing). **Never re-pull a snapshot already stored** — enforced by a partial UNIQUE index on `(sport_key, region, historical_snapshot_ts) WHERE mode='HISTORICAL'`; a cache hit short-circuits the fetch.

**Handling:**

- **Missing snapshot** (no data in window) → record a coverage gap for the requested tick; no candidate possible there.
- **Returned earlier than requested** → if within `max_historical_gap_seconds`, use it (store `historical_gap_seconds`); if beyond, treat as coverage gap.
- **Gaps between snapshots** (large `next_timestamp` jump) → flag via `historical_gap_seconds`; affects confirm and close (below).
- **Provider response failures** (timeout/429/500/parse/shape) → `pull_failures` exactly as P2, tagged `mode='HISTORICAL'` with `historical_requested_ts`.
- **Rate/credit limits** → backoff per `failure_policy`; the cost governor (§13) hard-stops before exceeding budget.

-----

## 4. Historical snapshot schedule (staged)

Do **not** start with a blind 90-day full-resolution pull.

|Stage |Window |Scope                 |Resolution                                 |Gate to proceed                                           |
|------|-------|----------------------|-------------------------------------------|----------------------------------------------------------|
|**H1**|14 days|MLB ML, selected books|every 15 min, game-window only if practical|baseline                                                  |
|**H2**|30 days|same                  |same                                       |only if H1 parses cleanly **and** yields usable candidates|
|**H3**|90 days|same                  |same                                       |only if cost/runtime acceptable **and** H1/H2 justify it  |

**Estimation formulas** (compute and log before each run):

```
snapshot_calls ≈ window_days × covered_hours_per_day × 60 / interval_minutes
                 (game-window-only sets covered_hours_per_day to the slate's game span)
credit_usage   ≈ snapshot_calls × credits_per_historical_call    # provider-specific multiplier
expected_runtime ≈ snapshot_calls × (avg_fetch_latency + processing) + rate_limit_pauses
expected_db_size ≈ snapshot_calls × avg_raw_payload_bytes + derived_row_overhead
```

**Worked H1 (illustrative):** 14 days × ~12 h game window × 4 calls/h ≈ **672 calls** (24h coverage ≈ 1,344). Runtime at ~1–2 s/call with rate-limit pauses ≈ minutes to low tens of minutes. DB ≈ `672 × avg_payload`. **Credit cost per historical call is provider-specific and typically billed at a higher multiplier than live — confirm against your plan; do not assume.** The §13 governor enforces the budget regardless.

-----

## 5. Lookahead-bias prevention

The replay processes snapshots in strict chronological order; `historical_snapshot_ts` (with `snapshot_sequence_num` as tiebreak) is the **only** time cursor:

```
snapshot_t1 → detect candidates (using t1 data only)
snapshot_t2 → observations / confirm persistence (using t2 data only)
snapshot_t3 → continued observations
pre-commence close snapshot → grade CLV ONLY after the candidate already exists
```

**At candidate-generation time (snapshot Tn), the system is NOT allowed to know:**

- the closing line, or any snapshot with `snapshot_ts > Tn`;
- the game result;
- the **contents** of `next_timestamp`’s snapshot (its existence/timing may be used for gap detection; its prices may **not** influence detection);
- any aggregate computed over future snapshots;
- the eventual CLV.

Detection may use only: the current snapshot’s de-vigged sharp price and soft prices at `Tn`, plus static config (park lookup; no weather in scope). Confirm uses only the confirm snapshot’s data at its own `Tn`. Close is read only at grading. **Using a future snapshot to *validate persistence* (confirm) is allowed and is the historical analog of the live confirm-pull; using future data to *select or grade-decide* a candidate is forbidden.**

-----

## 6. Historical candidate lifecycle

Same 7 states (`DETECTED → PENDING_CONFIRM → CONFIRMED → TRANSIENT | PENDING_GRADE → GRADED | UNGRADED`), same `opportunity_key`, same dedup. Only the confirm trigger changes.

**Confirm in historical mode:** on detection at snapshot `T0`, set `confirm_due_ts = T0 + confirm_delay`. The confirm is evaluated against the **first historical snapshot with `snapshot_ts ≥ confirm_due_ts`** (the chronological walk fires it on arrival). Example: detection 12:00, delay 60 s → confirm uses the first snapshot at/after 12:01. Multi-confirm reschedules `confirm_due_ts = current_ts + next_delay` after each pass (identical to P1).

**No confirm snapshot exists** (gap or end-of-data before the target, or target past commence):

- target falls past `commence_time` → `TRANSIENT(CONFIRM_EXPIRED)`;
- gap to the next snapshot exceeds `max_historical_gap_seconds` → `TRANSIENT(CONFIRM_GAP_TOO_LARGE)`;
- no snapshot at all before data ends → `TRANSIENT(CONFIRM_NO_SNAPSHOT)`.
  (All terminal under the existing `TRANSIENT` state; reason recorded in `trigger_values`.)

> **Honest limitation:** historical confirm at a 15-min cadence is **coarser** than the live 45 s confirm, so it cannot catch sub-cadence stale-price artifacts the way live can. Historical transient-rejection is therefore a weaker safeguard than live — another reason historical ≠ live.

-----

## 7. Historical closing-line protocol

`Close` = the **last valid sharp snapshot before `commence_time` within `close_capture_window_minutes`**, found over stored historical snapshots (look-back only; never lookahead). De-vig it (`closing_novig`) to the two-way no-vig close, store both sides.

Rules:

- **Missing close** (no sharp snapshot in the window) → `close_source_flag=MISSING`, candidate graded `UNGRADED`.
- **Stale close** (last sharp snapshot older than the window) → treated as missing (`CLOSE_MISSING`).
- **Suspended market** at the last pre-commence snapshot → use last valid pre-suspension sharp price, `close_source_flag=FROM_SUSPENSION`.
- **No sharp source near close** → `MISSING`.
- **Snapshot gap too large before commence** (`close_gap_seconds > max_historical_gap_seconds`) → `MISSING` (don’t trust a far-away “close”).
- Store `close_snapshot_ts` and `close_gap_seconds`. `CLOSE_MISSING` rate is a first-class trustworthiness metric.

-----

## 8. Database schema patch

All additive; existing live rows carry `mode='LIVE'` and `replay_run_id=NULL`.

```sql
-- audit_runs: distinguish mode + capture replay config
ALTER TABLE audit_runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'LIVE'
    CHECK (mode IN ('LIVE','HISTORICAL'));
ALTER TABLE audit_runs ADD COLUMN historical_start_date TEXT;
ALTER TABLE audit_runs ADD COLUMN historical_end_date TEXT;
ALTER TABLE audit_runs ADD COLUMN historical_snapshot_interval_minutes INTEGER;
ALTER TABLE audit_runs ADD COLUMN replay_speed REAL;
ALTER TABLE audit_runs ADD COLUMN credit_budget INTEGER;
ALTER TABLE audit_runs ADD COLUMN estimated_credits INTEGER;
ALTER TABLE audit_runs ADD COLUMN credits_used INTEGER NOT NULL DEFAULT 0;

-- raw_api_pulls: historical provenance
ALTER TABLE raw_api_pulls ADD COLUMN mode TEXT NOT NULL DEFAULT 'LIVE';
ALTER TABLE raw_api_pulls ADD COLUMN data_source TEXT NOT NULL DEFAULT 'odds_api_live';
ALTER TABLE raw_api_pulls ADD COLUMN replay_run_id INTEGER REFERENCES audit_runs(audit_run_id);
ALTER TABLE raw_api_pulls ADD COLUMN historical_requested_ts TEXT;
ALTER TABLE raw_api_pulls ADD COLUMN historical_snapshot_ts TEXT;
ALTER TABLE raw_api_pulls ADD COLUMN historical_previous_ts TEXT;
ALTER TABLE raw_api_pulls ADD COLUMN historical_next_ts TEXT;
ALTER TABLE raw_api_pulls ADD COLUMN historical_gap_seconds REAL;
ALTER TABLE raw_api_pulls ADD COLUMN snapshot_sequence_num INTEGER;

-- pull_cycles: a historical "cycle" = one snapshot fetch
ALTER TABLE pull_cycles ADD COLUMN mode TEXT NOT NULL DEFAULT 'LIVE';
ALTER TABLE pull_cycles ADD COLUMN replay_run_id INTEGER REFERENCES audit_runs(audit_run_id);
ALTER TABLE pull_cycles ADD COLUMN historical_requested_ts TEXT;
ALTER TABLE pull_cycles ADD COLUMN historical_snapshot_ts TEXT;
ALTER TABLE pull_cycles ADD COLUMN historical_gap_seconds REAL;
ALTER TABLE pull_cycles ADD COLUMN snapshot_sequence_num INTEGER;
ALTER TABLE pull_cycles ADD COLUMN coverage_gap INTEGER NOT NULL DEFAULT 0;  -- bool

-- candidates / observations / rejections / closing / clv / pull_failures: mode + replay tag
ALTER TABLE candidates            ADD COLUMN mode TEXT NOT NULL DEFAULT 'LIVE';
ALTER TABLE candidates            ADD COLUMN replay_run_id INTEGER REFERENCES audit_runs(audit_run_id);
ALTER TABLE candidate_observations ADD COLUMN mode TEXT NOT NULL DEFAULT 'LIVE';
ALTER TABLE candidate_observations ADD COLUMN replay_run_id INTEGER REFERENCES audit_runs(audit_run_id);
ALTER TABLE candidate_observations ADD COLUMN snapshot_sequence_num INTEGER;
ALTER TABLE rejections            ADD COLUMN mode TEXT NOT NULL DEFAULT 'LIVE';
ALTER TABLE rejections            ADD COLUMN replay_run_id INTEGER REFERENCES audit_runs(audit_run_id);
ALTER TABLE closing_lines         ADD COLUMN mode TEXT NOT NULL DEFAULT 'LIVE';
ALTER TABLE closing_lines         ADD COLUMN replay_run_id INTEGER REFERENCES audit_runs(audit_run_id);
ALTER TABLE closing_lines         ADD COLUMN close_snapshot_ts TEXT;
ALTER TABLE closing_lines         ADD COLUMN close_gap_seconds REAL;
ALTER TABLE clv_results           ADD COLUMN mode TEXT NOT NULL DEFAULT 'LIVE';
ALTER TABLE clv_results           ADD COLUMN replay_run_id INTEGER REFERENCES audit_runs(audit_run_id);
ALTER TABLE pull_failures         ADD COLUMN mode TEXT NOT NULL DEFAULT 'LIVE';
ALTER TABLE pull_failures         ADD COLUMN replay_run_id INTEGER REFERENCES audit_runs(audit_run_id);
ALTER TABLE pull_failures         ADD COLUMN historical_requested_ts TEXT;

-- indexes for replay speed + cache dedup
CREATE UNIQUE INDEX ux_hist_snapshot
    ON raw_api_pulls(sport_key, region, historical_snapshot_ts) WHERE mode='HISTORICAL';
CREATE INDEX ix_raw_replay_seq   ON raw_api_pulls(replay_run_id, snapshot_sequence_num);
CREATE INDEX ix_raw_hist_lookup  ON raw_api_pulls(sport_key, market_key, region, historical_snapshot_ts);
CREATE INDEX ix_obs_replay_seq   ON candidate_observations(replay_run_id, snapshot_sequence_num);
CREATE INDEX ix_cand_replay_conf ON candidates(replay_run_id, status, confirm_due_ts);
CREATE INDEX ix_close_replay_evt ON closing_lines(replay_run_id, event_id);
```

Migration name: **`011_historical_mode.sql`** (after `010_pull_cycles.sql`).

-----

## 9. Config patch

```yaml
run:
  mode: "HISTORICAL"               # LIVE | HISTORICAL
  threshold_locked: true
  db_path: "data/market_translation.sqlite"

historical:
  historical_start_date: "2026-05-01T00:00:00Z"
  historical_end_date:   "2026-05-15T00:00:00Z"     # H1 = 14 days
  historical_snapshot_interval_minutes: 15
  historical_game_window_enabled: true
  historical_game_window_hours_before: 6
  historical_game_window_minutes_after_commence: 0
  max_historical_gap_seconds: 1800                  # 30 min; gaps beyond = coverage gap
  replay_speed: 0                                   # 0 = as fast as possible
  resume: true                                      # skip already-stored snapshots

cost:
  max_credit_budget: 5000                           # hard stop (see §13)

target:
  sport_keys: ["baseball_mlb"]
  market_key: "h2h"
  allowed_two_way_only: true
  region: "us"

sharp_source:
  sharp_book_primary: "pinnacle"
  sharp_book_fallback: "circasports"
  sharp_disagree_tolerance_prob: 0.010

soft_books: ["draftkings", "fanduel", "betmgm", "caesars"]

timing:
  confirm_pull_delays_seconds: [60]                 # historical confirm target offset
  close_capture_window_minutes: 10
  freshness_window_seconds: { sharp: 900, soft: 900 } # widened to snapshot cadence

signal:
  edge_threshold_pct: 2.0                           # COMMITTED + LOCKED (no within-window tuning)

time:
  storage_timezone: "UTC"
  max_clock_skew_seconds: 10                         # benign in historical (provider timestamps)
```

> `freshness_window_seconds` is widened to the snapshot cadence in historical mode (a 15-min-old snapshot is “fresh” relative to the grid); otherwise every snapshot would fail `STALE_*`.

-----

## 10. Historical replay execution flow

```text
# A. historical inventory
confirm sport_key + market_key + region + sharp/soft books exist in the historical endpoint
  for a probe date in [start,end]; abort if sharp absent. Estimate calls/credits (§4); if
  estimated_credits > cost.max_credit_budget -> HALT before any paid fetch (§13).

# B. historical snapshot ingestion (chaining, cache-safe)
ts = historical_start_date; seq = 0
while ts < historical_end_date:
    if game_window_enabled and ts not in any [commence-Hbefore, commence+Mafter]: ts = next slate window; continue
    if snapshot already stored for (sport,region, nearest actual ts):   # cache / resume
        load it; ts = stored.next_ts; continue
    resp = GET historical_odds(sport,market,region,books, date=ts)      # pull_cycles row
        on failure -> pull_failures(mode=HISTORICAL, requested_ts=ts); backoff; continue
    pull_id = store_raw_pull(...historical_*..., snapshot_sequence_num=seq); credits_used += cost
    if estimated_or_running credits_used > budget -> HALT (§13)
    gap = |ts - resp.timestamp|; coverage_gap = (gap > max_historical_gap_seconds)
    seq += 1
    ts = resp.next_timestamp (or ts + interval if subsampling)

# C. chronological replay (the ONLY time cursor is historical_snapshot_ts)
for S in SELECT raw_api_pulls WHERE replay_run_id=R ORDER BY historical_snapshot_ts, snapshot_sequence_num:
    Tn = S.historical_snapshot_ts
    bundle = normalize(parse(S))                  # gates 2..11 as in live; rejections tagged mode/replay
    if S.coverage_gap: continue                   # nothing reliable to detect on a gap tick

    # D + E. candidate generation + dedup (uses ONLY S)
    for (event, soft_book, sharp side) crossing edge_threshold at Tn:
        oppkey = build_opportunity_key(R, event_id, market_key, sel_canon, soft_book, sharp_book, soft_dS, threshold)
        cand, created = upsert_candidate(oppkey, status='DETECTED' if new else unchanged,
                                         confirm_due_ts=Tn+confirm_delays[0] if new, mode='HISTORICAL', replay_run_id=R)
        insert_observation(cand, oppkey, pull_id=S.pull_id, phase='DETECTION', snapshot_sequence_num=S.seq, edge=e)
        if not created: last_seen_ts=Tn; observation_count += 1     # repeat sightings = observations only
      # two-sided guard: both sides cross for one (event,soft_book,sharp_book) -> TWO_SIDED_EDGE, no candidate

    # F. confirm using future snapshots WITHOUT lookahead decisioning
    for cand in candidates(R) WHERE status IN ('DETECTED','PENDING_CONFIRM') AND confirm_due_ts <= Tn:
        if Tn >= cand.commence_time: cand -> TRANSIENT(CONFIRM_EXPIRED); continue
        if gap_since_prev_snapshot > max_historical_gap_seconds: cand -> TRANSIENT(CONFIRM_GAP_TOO_LARGE); continue
        post = edge_pct(devig(S.sharp_sel,S.sharp_opp)[side], S.soft_decimal)   # S = first snapshot >= target
        insert_observation(cand, phase='CONFIRM', edge=post, snapshot_sequence_num=S.seq)
        if S.soft_off_key or S.stale or S.name_mismatch or S.not_two_way: cand -> TRANSIENT(<reason>); continue
        if post >= cand.threshold_used:
            confirms_passed += 1
            cand -> CONFIRMED if confirms_passed==confirms_required else reschedule confirm_due_ts=Tn+next_delay
        else: cand -> TRANSIENT(VANISHED)

    # G + H. close + grade when we pass an event's commence (look-back only; grade only after candidate exists)
    for E with commence_time <= Tn and no closing_lines row yet:
        close = last valid sharp snapshot for E with snapshot_ts < commence_time within close window & gap tolerance
        finalize_close(E, close)                  # NORMAL | FROM_SUSPENSION | MISSING ; store close_snapshot_ts, gap
        for cand in candidates(R) WHERE event_id=E AND status='CONFIRMED':
            cand -> PENDING_GRADE
            if close MISSING: clv NULL; grade_status='UNGRADED_CLOSE_MISSING'; cand -> UNGRADED
            else: p_close=closing_novig(side); clv=clv_pct(cand.soft_decimal,p_close); cand -> GRADED
        for cand in candidates(R) WHERE event_id=E AND status IN ('DETECTED','PENDING_CONFIRM'):
            cand -> TRANSIENT(CONFIRM_EXPIRED)    # never reached confirm before commence

# I. report generation
generate_historical_report(R)                     # §11, filtered to mode='HISTORICAL', replay_run_id=R
```

-----

## 11. Reporting patch

Historical reports are produced **separately** and clearly labeled `HISTORICAL VALIDATION` (never merged with live counts). CLV leads; P/L last and non-significant. Add to the P2a funnel:

**Coverage block:** `snapshot_count`, `missing_snapshot_count` (coverage gaps), `historical_gap_rate = coverage_gaps / requested_ticks`.
**Funnel (P1/P2a):** raw detections, unique candidates, duplicate observations, confirmed unique, transient rate (unique), graded unique, close-missing rate.
**CLV (graded unique only):** mean CLV, median CLV, beat-close rate; broken down by **soft book**, **sharp source**, **edge-threshold bucket** (descriptive only — selection across buckets is forbidden, §1), and **time-before-commence bucket** (e.g., >6h / 6–2h / 2–0.5h / <0.5h).
**Mandatory banner (top of every historical report):**

> ⚠️ Historical CLV measures whether a divergence *would have* shown value against the eventual sharp close. It does **not** prove the price was capturable live (holdable, un-limited, non-stale). A passing historical result authorizes the live audit only — never scaling.

-----

## 12. Pass/fail standards (historical)

**A. Historical feasibility pass → “worth a live audit”:**

- coverage adequate: `historical_gap_rate` low and `CLOSE_MISSING` < ~15%;
- enough **graded unique** candidates accumulated (target ≥ 100–200 over H1/H2);
- transient rate (unique) not dominant;
- mean CLV positive; beat-close rate directionally > 50% (≥ ~53%);
- **no CI requirement** (small-sample, directional).

**B. Historical statistical-confidence pass → “strong prior”:**

- larger graded-unique sample (target ≥ 500–1000, sample-dependent);
- mean CLV > 0 with bootstrap CI lower bound > 0;
- stable across sub-windows and across soft books (not one book or one week);
- beat-close robustly > 50%.

**C. Live-audit requirement (non-negotiable):** Neither A nor B substitutes for the live 3–5 day audit. Historical can only *prioritize* the angle; **capturability is tested live or not at all.** Even a Tier-B historical pass must clear the live Tier-1 (P2a-gated) before any further step.

**Kill / redesign before live if:** historical mean CLV ≤ 0; gains concentrated in a single book/week (overfit/illiquid); transient- or gap-dominated coverage; CLV driven by the smallest time-before-commence bucket (late, fast-moving, least-capturable prices). These say “do not bother spending live days.”

-----

## 13. Cost-control rules

- **`max_credit_budget`** per run (config). **Pre-run estimate** (§4) computed during inventory; **hard stop before any paid fetch** if `estimated_credits > max_credit_budget` (`audit_runs.status='ABORTED'`, reason `CREDIT_BUDGET`).
- **Running governor:** track `credits_used`; halt mid-run if it would exceed budget on the next fetch.
- **Resume behavior:** runs are idempotent — on restart, skip every snapshot already stored (cache), continue from the last `next_timestamp`. No duplicate credits.
- **Caching:** the partial UNIQUE index (`mode='HISTORICAL'`) makes re-storing a snapshot impossible; the fetcher checks the cache before spending a credit.
- **Never re-pull a stored snapshot** unless an explicit `--force-refresh` flag is passed (logged, and even then it overwrites nothing — it appends with a new pull and is excluded from default replay).

-----

## 14. Unit / integration tests

- **Snapshot timestamp storage:** `historical_requested_ts`, `historical_snapshot_ts`, `previous/next`, `gap_seconds`, `snapshot_sequence_num` all persisted; `gap_seconds = |requested − returned|`.
- **Replay chronological ordering:** snapshots processed strictly by `(historical_snapshot_ts, snapshot_sequence_num)`; assert a shuffled insert order still replays in time order.
- **No-lookahead candidate generation:** a candidate’s detection observation references only its own snapshot; assert no detection reads a `snapshot_ts > Tn` and no detection touches `closing_lines`.
- **Confirm snapshot selection:** detection at 12:00, delay 60 s → confirm uses the first snapshot with `ts ≥ 12:01` (not 12:00, not a later one if an earlier qualifies).
- **Missing confirm snapshot:** gap/end/past-commence → `TRANSIENT(CONFIRM_GAP_TOO_LARGE | CONFIRM_NO_SNAPSHOT | CONFIRM_EXPIRED)`.
- **Closing-line assignment:** last valid sharp snapshot < commence within window/gap tolerance; both no-vig sides stored; `close_snapshot_ts`/`close_gap_seconds` set.
- **Stale/missing close:** beyond window → `UNGRADED`; suspended → `FROM_SUSPENSION`; gap too large → `MISSING`.
- **Dedup in historical:** same priced opportunity across snapshots → one candidate, `observation_count` grows; soft-price change → new candidate.
- **Repeated sightings across snapshots:** N identical snapshots → N observations, 1 unique candidate (sample not inflated).
- **Threshold-crossing only at generation time:** a candidate is created only when edge ≥ threshold at the *current* snapshot; a later snapshot dropping below threshold does not retroactively delete it (it simply stops gaining detection observations).
- **Historical gap handling:** `coverage_gap` ticks produce no candidates; `historical_gap_rate` computed correctly.
- **Cost-budget halt:** estimate > budget → abort before fetch; running `credits_used` over budget → mid-run halt; resume skips stored snapshots.
- **Report separation:** historical metrics filter on `mode='HISTORICAL'`/`replay_run_id`; assert zero overlap with live rows.

-----

## 15. Implementation tickets (continue build-plan numbering)

**T20 — Mode plumbing + constants**

- Purpose: add `mode` everywhere; new TRANSIENT reasons; `data_source`.
- Files: `constants.py`, `config_loader.py`, `repo.py`. Inputs: config `mode`. Outputs: mode-aware run start.
- Acceptance: `audit_runs.mode` set; LIVE path unchanged; new reasons (`CONFIRM_NO_SNAPSHOT`, `CONFIRM_GAP_TOO_LARGE`) and `coverage_gap` constant present.
- Failure: any LIVE behavior altered. Tests: LIVE regression suite still green.

**T21 — Migration `011_historical_mode.sql`**

- Purpose: apply §8 schema. Files: `migrations/011_*.sql`, `db.py`, `init_db.py`.
- Acceptance: all columns/indexes created; partial UNIQUE on `(sport_key,region,historical_snapshot_ts) WHERE mode='HISTORICAL'`; idempotent.
- Failure: index missing; live rows lose defaults. Tests: schema introspection; duplicate historical snapshot insert fails.

**T22 — Historical ingestion (chaining + cache)**

- Purpose: `historical_ingest.py` per §3. Inputs: historical config. Outputs: `raw_api_pulls` (historical) + `pull_cycles` + `pull_failures`.
- Acceptance: chains via `next_timestamp`; never re-fetches a stored snapshot; records gap fields; tags `pull_failures` with `historical_requested_ts`.
- Failure: duplicate fetch of a stored snapshot; derived processing before raw store. Tests: cache short-circuit; chaining order; failure tagging.

**T23 — Shared-core refactor**

- Purpose: extract mode-agnostic decision functions (gates/edge/dedup/confirm/grade) operating on a normalized snapshot bundle; both LIVE and HISTORICAL drivers call them.
- Files: `gates.py`, `detect.py`, `confirm.py`, `closing.py`, `clv.py` (+ a thin `core/decision.py` if needed). Acceptance: LIVE results bit-identical pre/post refactor; HISTORICAL driver reuses the same functions.
- Failure: logic divergence between modes. Tests: LIVE regression + a parity test feeding the same bundle through both drivers.

**T24 — Replay driver**

- Purpose: `replay.py` + `run_replay.py` per §10C–H. Inputs: `replay_run_id`. Outputs: candidates/observations/rejections/closing/clv tagged HISTORICAL.
- Acceptance: strict chronological walk; detection uses only current snapshot; confirm fires on first snapshot ≥ target; close look-back only; grade only after candidate exists; one CLV per graded unique candidate.
- Failure: any lookahead; sleep/clock dependence; duplicate CLV. Tests: §14 no-lookahead, confirm-selection, dedup, ordering.

**T25 — Cost governor**

- Purpose: pre-run estimate + hard stop + running governor + resume. Files: `historical_ingest.py`, `config_loader.py`.
- Acceptance: estimate logged to `audit_runs`; abort before fetch if over budget; mid-run halt; resume skips stored snapshots.
- Failure: any paid fetch past budget. Tests: budget-halt (pre and mid), resume idempotency.

**T26 — Historical report**

- Purpose: extend `report.py` per §11. Inputs: `replay_run_id`. Outputs: `reports/historical/<run>.md` + `.csv`.
- Acceptance: coverage + funnel + CLV breakdowns (book/sharp/threshold bucket/time-before-commence bucket); mandatory capturability banner; filtered to HISTORICAL; CLV on graded unique only; P/L last.
- Failure: historical/live counts mixed; banner missing; selection across threshold buckets. Tests: separation + banner presence + bucket descriptiveness.

-----

## 16. Non-goals (P3)

P3 does **not**: place automated bets; size bets; add human approval; add weather, props, spreads, or totals; add a Streamlit/dashboard/web UI; add a Claude reasoning/pricing/sizing/deciding layer; tune strategy or thresholds inside the same historical sample; or treat any historical result as proof of live capturability.

-----

## 17. Final go/no-go checklist (ready to run H1)

- [ ] T20–T26 implemented; LIVE regression suite still green (P3 changed nothing live).
- [ ] All §14 historical tests pass.
- [ ] Migration `011` applied; partial UNIQUE historical-snapshot index verified (re-store fails).
- [ ] Inventory confirms sharp + soft books + `h2h` exist in the historical endpoint for an in-window probe date.
- [ ] `edge_threshold_pct` committed and **locked**; no within-window tuning path exists.
- [ ] No-lookahead proven by test: detection touches no future snapshot and no `closing_lines`.
- [ ] Confirm uses the first snapshot ≥ target; close uses last sharp snapshot < commence within tolerance.
- [ ] Cost estimate for H1 computed and **≤ `max_credit_budget`**; budget hard-stop verified; resume idempotency verified.
- [ ] `freshness_window_seconds` widened to the snapshot cadence (so snapshots aren’t all `STALE_*`).
- [ ] Historical report renders separately with the capturability banner; CLV on graded unique only; P/L last.
- [ ] H2/H3 explicitly gated (do not run beyond H1 until H1 parses cleanly and yields usable candidates).

**The biggest way historical backtesting will fool us — and the safeguard:**

> **The capturability illusion.** A lookahead-free historical run can show clean positive CLV and *still* be untradeable, because the soft prices in the snapshots may never have been actually holdable: the book would have limited or voided you, or the snapshot captured a **stale/fleeting price** the book hadn’t yet corrected. The de-vig-vs-snapshot-timing interaction manufactures “divergences” that are artifacts of cadence, not real gaps — and at a 15-min historical cadence, the confirm-persistence check is too coarse to filter them the way the live 45 s confirm does. So historical CLV can look like edge that does not exist for *you*.
> 
> **Safeguards:** (1) the structural no-lookahead walk plus confirm-persistence, which removes the *easy* self-deception; (2) the locked, pre-committed threshold and the ban on within-window bucket selection, which removes overfitting (the second-biggest trap); and most importantly (3) the **core principle enforced as a hard gate** — a historical pass *only authorizes the live audit and never replaces it*, because capturability is testable solely against live, latency-realistic execution. Treat every historical number as a screen that earns you the right to spend live days, never as evidence you have an edge.