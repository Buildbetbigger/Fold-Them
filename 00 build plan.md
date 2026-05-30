# Developer Execution Plan — Market-Translation System v0.1 + Patch P1

**Controlling documents:** v0.1 Technical Specification and Precision Patch P1 (both adopted as authoritative). This plan implements them and changes nothing in them.

**Invariants (must hold in every ticket):** Claude never prices · never sizes · never decides. The log is the product. Repeated sightings cannot inflate the sample. CLV is computed only on **graded unique candidates**. Stale data → rejection. Missing sharp → no candidate.

**Concurrency note (load-bearing):** P1 splits the system into three long-running processes that all write one SQLite file (detection, confirm worker, closing+grading). Every connection sets `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`, `PRAGMA foreign_keys=ON`. Writes serialize; keep every transaction short (already the design). Jobs must retry on `SQLITE_BUSY`.

-----

## 1. Final file / module structure

```
market-translation/
├── README.md
├── requirements.txt
├── .env.example                     # ODDS_API_KEY=...
├── config.yaml                      # §4
├── pyproject.toml / setup.cfg
├── migrations/
│   ├── 001_core.sql
│   ├── 002_events_entities.sql
│   ├── 003_snapshots_outcomes.sql
│   ├── 004_candidates_p1.sql
│   ├── 005_candidate_observations.sql
│   ├── 006_clv_results.sql
│   ├── 007_rejections.sql
│   └── 008_indexes.sql
├── src/
│   ├── __init__.py
│   ├── constants.py                 # STATUS_*, REJECTION_* enums, TRANSIENT reasons
│   ├── config_loader.py             # load, validate, hash, snapshot, lock guards
│   ├── db.py                        # connect (PRAGMAs), migrate, tx helpers
│   ├── repo.py                      # all writers: audit_runs, errors, candidate upsert,
│   │                                #   observation insert, rejection insert, clv, closing
│   ├── formulas.py                  # PURE math (odds, de-vig, edge, clv) — no DB, no IO
│   ├── opportunity_key.py           # build_opportunity_key
│   ├── api_client.py                # api_pull + store_raw_pull (raw-first)
│   ├── normalize.py                 # name/market normalization, NAME_NORM_FAIL
│   ├── sharp_source.py              # NO_SHARP / SHARP_DISAGREE / STALE_SHARP
│   ├── gates.py                     # all §6 gates in order, incl. TWO_SIDED_EDGE
│   ├── detect.py                    # Job A: detection loop (dedup, fast commit, no sleep)
│   ├── confirm.py                   # Job B: confirm worker (scheduled, multi-confirm)
│   ├── closing.py                   # Job C: close capture + grading + expire-unconfirmed
│   ├── clv.py                       # grading helpers (closing_novig, clv_pct, beat_close)
│   ├── report.py                    # daily + cumulative funnel report (CLV on graded unique)
│   └── claude_summary.py            # OPTIONAL, deferred; read-only, cites row IDs (§T19)
├── scripts/
│   ├── init_db.py                   # apply migrations in order
│   ├── run_inventory.py             # confirm sport/sharp/books/market exist
│   ├── run_harness.py               # Job A loop
│   ├── run_confirm.py               # Job B loop
│   ├── run_closing.py               # Job C loop
│   ├── run_report.py                # daily/cumulative report
│   └── run_dryrun.py                # fixture-driven full pipeline, no network
├── tests/
│   ├── test_formulas.py
│   ├── test_opportunity_key.py
│   ├── test_normalize.py
│   ├── test_sharp_source.py
│   ├── test_gates.py
│   ├── test_candidate_upsert.py     # dedup / observation-not-new-candidate
│   ├── test_confirm.py              # lifecycle transitions
│   ├── test_clv.py
│   ├── test_report_funnel.py
│   ├── test_rejection_logging.py    # no silent drops
│   └── test_integration_dryrun.py
├── fixtures/
│   ├── scenario_basic.yaml          # tick -> payload mapping, deterministic clock
│   ├── pull_*.json                  # detection-loop payloads
│   ├── confirm_*.json               # confirm re-pull payloads
│   └── close_*.json                 # closing payloads
├── data/
│   └── market_translation.sqlite    # THE PRODUCT
└── reports/
    ├── daily/
    └── cumulative/
```

-----

## 2. Build tickets

> Order is dependency order; do not start a ticket until its predecessors pass. Each ticket’s tests must be green before the next.

**T1 — Scaffold + constants**

- Purpose: repo skeleton, deps, single source of truth for statuses and rejection codes.
- Files: project files, `src/constants.py`.
- Inputs: none. Outputs: `STATUS` (7 lifecycle values), `REJECTION_CODE` (all §6 + `TWO_SIDED_EDGE`), `TRANSIENT_REASON` (`VANISHED|WENT_STALE|REPULL_ERROR|OFF_KEY_PRICE|CONFIRM_EXPIRED`), `PHASE` (`DETECTION|CONFIRM`).
- Acceptance: importable enums; values match P1 exactly.
- Failure: any status/code defined outside `constants.py`.
- Tests: enum membership assertions.

**T2 — Config loader**

- Purpose: load/validate config, snapshot it, enforce locks.
- Files: `src/config_loader.py`.
- Inputs: `config.yaml`, env `ODDS_API_KEY`. Outputs: resolved config object + `config_hash` + JSON snapshot string.
- Acceptance: rejects missing required keys; `threshold_locked=true` blocks any in-process edge-threshold mutation; `dry_run` flag surfaced; API key read only from env (never from file).
- Failure: API key found in `config.yaml` → hard error; unknown sport in `excluded_sports` overlap with `sport_keys` → error.
- Tests: valid load; missing key; key-in-file rejection; threshold-lock enforcement.

**T3 — DB schema + migrations**

- Purpose: create the database in correct FK order with PRAGMAs.
- Files: `migrations/*.sql`, `src/db.py`, `scripts/init_db.py`.
- Inputs: empty DB path. Outputs: fully migrated SQLite file.
- Acceptance: all 12 tables exist; `candidates.opportunity_key` UNIQUE; `candidate_observations` UNIQUE(pull_id, opportunity_key); status `CHECK` present; `PRAGMA foreign_keys=ON`, WAL enabled; idempotent re-run is a no-op.
- Failure: any FK target created after its referrer; missing UNIQUE on `opportunity_key`.
- Tests: schema introspection; UNIQUE/CHECK enforcement (duplicate oppkey insert fails; bad status fails).

**T4 — Writers / repository**

- Purpose: all DB write paths in one audited module.
- Files: `src/repo.py`.
- Inputs: conn + typed args. Outputs: `start_audit_run`, `finish_audit_run`, `log_error`, `insert_raw_pull`, `upsert_event`, `upsert_entity`, `insert_snapshot`, `insert_outcome`, `upsert_candidate`, `insert_observation`, `insert_rejection`, `insert_closing`, `insert_clv`, status-transition helpers.
- Acceptance: every write is inside a short transaction; `upsert_candidate` returns `(candidate_id, created: bool)`; transitions validate allowed source→target.
- Failure: write error → `log_error` (FATAL) + raise; no partial commits.
- Tests: covered via T11/T12/T13 + a direct transition-guard test (illegal transition raises).

**T5 — Formulas (pure)**

- Purpose: deterministic math, no DB/IO. Claude touches none of this.
- Files: `src/formulas.py`. Inputs/Outputs: see §5.
- Acceptance: pure functions; input validation raises on bad odds.
- Failure: `american_to_decimal` with `|a|<100` or 0 → raise; `decimal_to_implied`/de-vig with `d<=1.0` → raise.
- Tests: `test_formulas.py` (§6).

**T6 — opportunity_key builder**

- Purpose: deterministic dedup key.
- Files: `src/opportunity_key.py`. Output: composite TEXT per P1 §1 (soft price `.4f`, threshold `.4f`).
- Acceptance: identical inputs → identical key; `soft_decimal` 2.05 vs 2.0500 → same; vs 2.0600 → different.
- Failure: non-canonicalized float interpolation.
- Tests: `test_opportunity_key.py` (§6).

**T7 — API client + raw-first storage**

- Purpose: pull odds and store raw before anything derived.
- Files: `src/api_client.py`. Inputs: config, sport_key. Outputs: `PullResult` (status, parsed payload) and a persisted `raw_api_pulls` row (returns `pull_id`).
- Acceptance: HTTP/parse failure → `API_FAIL` rejection + return without derived processing; raw row written with sha256 hash; `dry_run` routes to fixture loader, never the network.
- Failure: derived processing attempted before raw row committed.
- Tests: covered in dry-run/integration; unit test for hash + dry-run routing.

**T8 — Normalization layer**

- Purpose: map raw team/market strings to canonical IDs.
- Files: `src/normalize.py`. Inputs: sport_key, raw strings. Output: canonical IDs or `NAME_NORM_FAIL`.
- Acceptance: known alias → canonical; unknown → `NAME_NORM_FAIL` (never a guessed map); home/away never swapped.
- Failure: silent fuzzy match.
- Tests: `test_normalize.py` (§6).

**T9 — Sharp-source validator**

- Purpose: select/validate the fair reference.
- Files: `src/sharp_source.py`. Inputs: per-event snapshots, config sharp primary/fallback/tolerance. Output: validated sharp two-way set + freshness, or rejection.
- Acceptance: absent → `NO_SHARP`; primary+fallback no-vig differ > tolerance → `SHARP_DISAGREE`; chosen sharp stale → `STALE_SHARP`; never averages disagreeing sharps.
- Failure: producing a candidate when sharp is missing/stale/disagreeing.
- Tests: `test_sharp_source.py` (§6).

**T10 — Data-quality gates**

- Purpose: ordered gate chain, fail-closed, never impute.
- Files: `src/gates.py`. Inputs: in-progress opportunity context. Output: `GateResult(PASS)` or `(code, trigger_values)`.
- Acceptance: gates run in §6 order; first trip stops processing that opportunity and yields a rejection; `TWO_SIDED_EDGE` rejects **both** sides when one (event, soft_book, sharp_book) crosses on both selections; every `trigger_values` contains the specified keys.
- Failure: any opportunity exits a gate with neither candidate nor rejection.
- Tests: `test_gates.py` + `test_rejection_logging.py` (§6).

**T11 — Detection loop (Job A)**

- Purpose: pull → gates → dedup → candidate/observation/rejection; fast commit, no sleep.
- Files: `src/detect.py`, `scripts/run_harness.py`.
- Inputs: config, conn, audit_run. Outputs: candidate upserts, observation inserts, rejection inserts.
- Acceptance: new oppkey → `upsert_candidate` created=True (status `DETECTED`, `confirm_due_ts` set, `confirms_required=len(delays)`); existing oppkey → created=False, observation inserted, `observation_count++`, `last_seen_ts` updated, **no re-confirm, no reopening TRANSIENT/CONFIRMED**; transaction commits with **zero sleeps** inside.
- Failure: a `sleep()` anywhere in the loop; duplicate candidate creation; partial commit on error.
- Tests: `test_candidate_upsert.py` (repeat sighting, sharp-moved-same-soft, soft-price-change, reappearance, two-sided) + integration.

**T12 — Confirm worker (Job B)**

- Purpose: scheduled confirm, decoupled from detection.
- Files: `src/confirm.py`, `scripts/run_confirm.py`.
- Inputs: conn, config, pull function, clock. Output: status transitions + `CONFIRM` observations + own raw pull row.
- Acceptance: claims `status IN (DETECTED,PENDING_CONFIRM) AND confirm_due_ts<=now`; sets `PENDING_CONFIRM`; re-pull writes its own `raw_api_pulls` row; pass increments `confirms_passed` and reschedules `confirm_due_ts` until `confirms_required` reached → `CONFIRMED`; any fail → `TRANSIENT` with reason; event past commence → `TRANSIENT(CONFIRM_EXPIRED)`; off-key soft price → `TRANSIENT(OFF_KEY_PRICE)`; polling `sleep` is **outside** any transaction.
- Failure: sleep inside transaction; re-confirming an already-terminal candidate; missing raw pull on re-pull.
- Tests: `test_confirm.py` (§6).

**T13 — Closing capture + grading (Job C)**

- Purpose: capture sharp close, expire stragglers, grade CONFIRMED candidates.
- Files: `src/closing.py`, `src/clv.py`, `scripts/run_closing.py`.
- Inputs: conn, config, clock. Outputs: `closing_lines` row per event; `clv_results` row per CONFIRMED candidate.
- Acceptance: close = last valid sharp price within `close_capture_window_minutes` before commence; at commence, `{DETECTED,PENDING_CONFIRM}` for the event → `TRANSIENT(CONFIRM_EXPIRED)`; `CONFIRMED` → `PENDING_GRADE` → `GRADED` (CLV) or `UNGRADED` (close missing); exactly one CLV row per candidate; `d_taken = candidate.soft_decimal`.
- Failure: grading non-CONFIRMED candidates; >1 CLV per candidate; imputing a missing close.
- Tests: `test_clv.py` + integration (NORMAL, MISSING, FROM_SUSPENSION).

**T14 — Report generator**

- Purpose: CLV-led daily + cumulative funnel reports.
- Files: `src/report.py`, `scripts/run_report.py`.
- Inputs: conn, window. Outputs: `reports/daily/YYYY-MM-DD.md` + `.csv`, `reports/cumulative/cumulative.md` + `.csv`.
- Acceptance: leads with the §5 funnel counts; rates use **unique** denominators; mean/median/beat-close computed over `status='GRADED'` only; trustworthiness verdict per P1; P/L last and labeled non-significant; bootstrap CI on cumulative.
- Failure: any CLV stat computed over non-graded rows; P/L above the fold.
- Tests: `test_report_funnel.py` (§6).

**T15 — Dry-run harness**

- Purpose: run the full pipeline with fixtures, deterministic clock, no network.
- Files: `scripts/run_dryrun.py`, `fixtures/*`.
- Inputs: scenario file. Output: populated test DB + assertions.
- Acceptance: detection, confirm, and grading all driven from fixtures; simulated clock advances ticks; produces a known end-state.
- Failure: any real network call when `dry_run=true`.
- Tests: `test_integration_dryrun.py` (§6).

**T16 — Inventory script**

- Purpose: confirm feed supports the target before any run.
- Files: `scripts/run_inventory.py`.
- Inputs: config. Output: persisted/printed list of available sports, books, markets.
- Acceptance: confirms designated sharp + soft books + `h2h` present for `sport_keys`; exits non-zero if sharp absent.
- Failure: proceeding to a live run without a confirmed sharp book.
- Tests: dry-run against an inventory fixture.

**T17 — End-to-end integration (fixtures)**

- Purpose: prove the funnel and lifecycle on a scripted scenario.
- Files: `tests/test_integration_dryrun.py`.
- Acceptance: a scenario with steady-state repeats, a soft-price change, a reappearance, a transient, a two-sided anomaly, and one graded close yields exact expected counts (unique vs raw vs duplicate vs confirmed vs graded) and one correct CLV.
- Tests: this ticket *is* the test.

**T18 — Run scripts hardening**

- Purpose: long-running loops survive a 3–5 day run.
- Files: `scripts/run_harness.py`, `run_confirm.py`, `run_closing.py`.
- Acceptance: `SQLITE_BUSY` retry with backoff; graceful shutdown writes `finish_audit_run`; uncaught exceptions go to `system_errors` (FATAL) and exit non-zero; no silent loop death.
- Tests: induced-busy + induced-exception unit tests.

**T19 — (OPTIONAL, deferred) Claude summarizer**

- Purpose: prose summary of report rows. Build only after T1–T18 pass and a clean dry-run.
- Files: `src/claude_summary.py`.
- Acceptance: read-only on report dataset; every number cites a row ID (`candidate_id`/`rejection_id`/`clv_id`); writes only to a report appendix or `candidates.claude_note`; cannot emit any probability/edge/stake/decision.
- Failure: any computed value not copied from the DB; any write to computed fields.
- Tests: assert no numeric output lacks a citing ID; assert no writes outside allowed fields. **Prefer not building this in v0.1.**

-----

## 3. Database migration order

Run `scripts/init_db.py`, which applies migrations in this exact sequence (parents before children so FK targets always exist):

1. **`001_core.sql`** — `audit_runs`; `system_errors` (FK→audit_runs); `raw_api_pulls` (FK→audit_runs).
1. **`002_events_entities.sql`** — `normalized_entities`; `events` (FK→normalized_entities).
1. **`003_snapshots_outcomes.sql`** — `bookmaker_snapshots` (FK→raw_api_pulls, events); `market_outcomes` (FK→bookmaker_snapshots, normalized_entities); `closing_lines` (FK→events, raw_api_pulls).
1. **`004_candidates_p1.sql`** — **P1 revised `candidates`** (inline `UNIQUE(opportunity_key)`, status `CHECK`, confirm fields; FK→audit_runs, raw_api_pulls, events).
1. **`005_candidate_observations.sql`** — `candidate_observations` (FK→candidates, raw_api_pulls; `UNIQUE(pull_id, opportunity_key)`).
1. **`006_clv_results.sql`** — `clv_results` (FK→candidates, closing_lines; `UNIQUE(candidate_id)`).
1. **`007_rejections.sql`** — `rejections` with `opportunity_key` column included directly (fresh build, no ALTER needed; FK→raw_api_pulls).
1. **`008_indexes.sql`** — all non-unique indexes (`ix_*` from the spec + P1: `ix_cand_confirm`, `ix_obs_*`, `ix_rej_oppkey`, etc.). Unique indexes already exist via inline `UNIQUE`.

Mapping to the requested grouping: base v0.1 tables = 001–003 + 006–007; P1 patched candidate schema = 004; candidate_observations = 005; indexes = 008; constraints = inline (UNIQUE + CHECK) in 004–007, enforced at create time.

> Connection setup applied by `db.connect()` before any migration: `PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`

-----

## 4. Config file (`config.yaml`) — first 3–5 day moneyline-only audit

```yaml
run:
  run_label: "v0.1_p1_feasibility_window_01"
  dry_run: false                  # true only for fixture runs
  threshold_locked: true          # forbids mid-run edge_threshold change
  db_path: "data/market_translation.sqlite"

api:
  base_url: "https://<provider>"
  region: "us"
  # api_key: env ODDS_API_KEY only — never here
  request_timeout_s: 10
  max_retries: 2

target:
  sport_keys: ["baseball_mlb"]    # ONE liquid two-way sport to start
  market_key: "h2h"
  allowed_two_way_only: true
  excluded_sports: ["soccer_epl", "soccer_uefa_champs_league"]  # 3-way; out of scope

sharp_source:
  sharp_book_primary: "pinnacle"
  sharp_book_fallback: "circasports"
  sharp_disagree_tolerance_prob: 0.010

soft_books: ["draftkings", "fanduel", "betmgm", "caesars"]

timing:
  pull_interval_seconds: 90
  freshness_window_seconds: { sharp: 120, soft: 120 }
  confirm_pull_delays_seconds: [45]      # single confirm to start (P1 multi-confirm ready)
  confirm_worker_poll_seconds: 5
  close_capture_window_minutes: 10
  close_worker_poll_seconds: 15
  close_polling_schedule:
    - { from_min: 60, to_min: 15, interval_s: 120 }
    - { from_min: 15, to_min: 0,  interval_s: 30 }

signal:
  edge_threshold_pct: 2.0          # COMMITTED + LOCKED before run

sanity:
  price_decimal_min: 1.01
  price_decimal_max: 51.0

time:
  storage_timezone: "UTC"
  display_timezone: "America/New_York"

reporting:
  daily_run_time_local: "09:00"
  feasibility_min_graded_unique: 100   # Tier-1 checkpoint floor

logging:
  level: "INFO"

dryrun:
  scenario_path: "fixtures/scenario_basic.yaml"
```

-----

## 5. Core functions (signatures)

```python
# --- api_client.py ---
def api_pull(cfg: Config, sport_key: str) -> PullResult:
    """GET odds (or fixture in dry_run). Raises -> caller writes API_FAIL rejection."""

def store_raw_pull(conn, audit_run_id: int, endpoint: str, sport_key: str,
                   market_key: str, region: str, http_status: int,
                   payload: str) -> int:
    """Append-only raw_api_pulls row (sha256 hash). Returns pull_id. Raw FIRST."""

# --- formulas.py (PURE) ---
def american_to_decimal(american: int) -> float: ...
def decimal_to_implied(decimal_odds: float) -> float: ...
def devig_two_way(d1: float, d2: float) -> tuple[float, float]:  # (p1_fair, p2_fair)
def edge_pct(p_fair: float, d_soft: float) -> float:             # (p_fair*d_soft - 1)*100
def closing_novig(d_sel: float, d_opp: float) -> float:          # p_close for selection
def clv_pct(d_taken: float, p_close: float) -> float:            # (d_taken*p_close - 1)*100
def beat_close(clv_value: float) -> bool:                        # clv_value > 0

# --- opportunity_key.py ---
def build_opportunity_key(audit_run_id: int, event_id: str, market_key: str,
                          selection_canonical_id: str, soft_book: str,
                          sharp_book: str, soft_decimal: float,
                          threshold_used: float) -> str:
    """Composite TEXT; soft_decimal and threshold formatted to 4dp."""

# --- gates.py ---
def run_gates(ctx: OpportunityContext, cfg: Config) -> GateResult:
    """PASS or (rejection_code, trigger_values dict). Ordered, fail-closed."""

# --- repo.py ---
def upsert_candidate(conn, oppkey: str, fields: CandidateFields) -> tuple[int, bool]:
    """(candidate_id, created). created=False -> existing; caller logs observation only."""

def insert_observation(conn, candidate_id: int, oppkey: str, pull_id: int,
                       phase: str, obs: ObservationFields) -> int: ...

def insert_rejection(conn, code: str, stage: str, trigger_values: dict,
                     ctx: RejectionContext) -> int: ...

# --- confirm.py ---
def confirm_worker_tick(conn, cfg: Config, pull_fn, clock) -> ConfirmTickStats:
    """Claim due candidates, re-pull, transition (CONFIRMED|TRANSIENT|reschedule).
    No sleep inside any transaction."""

# --- closing.py / clv.py ---
def finalize_close(conn, cfg: Config, event_id: str, clock) -> int | None:
    """Write one closing_lines row (NORMAL|FROM_SUSPENSION|MISSING). Returns close_id."""

def grade_event(conn, event_id: str, clock) -> GradeStats:
    """Expire unconfirmed -> TRANSIENT(CONFIRM_EXPIRED); grade CONFIRMED -> GRADED|UNGRADED.
    Exactly one clv_results row per candidate; d_taken = candidate.soft_decimal."""

# --- report.py ---
def generate_daily_report(conn, window_start: str, window_end: str) -> ReportPaths: ...
def generate_cumulative_report(conn, audit_run_id: int) -> ReportPaths: ...
```

-----

## 6. Unit test suite (exact tests + expected results)

**test_formulas.py**

- `american_to_decimal`: `+150→2.5`, `-200→1.5`, `+100→2.0`, `-110→1.90909…`, `+250→3.5`; `0`, `50`, `-50` → raise.
- `decimal_to_implied`: `2.0→0.5`, `1.5→0.66667`, `4.0→0.25`; `1.0`/`0.9` → raise.
- `devig_two_way`: `(1.90909,1.90909)→(0.5,0.5)`; `(1.66667,2.30)→≈(0.5798,0.4202)`; outputs sum to 1.0 within tol.
- `edge_pct`: `(0.55, 2.0)→10.0`; `(0.50, 1.95)→-2.5`.
- `closing_novig`/`clv_pct`/`beat_close`: `clv_pct(2.5, 0.4545)→≈+13.6`, `beat=True`; `clv_pct(1.9, 0.5)→-5.0`, `beat=False`.

**test_opportunity_key.py**

- Same inputs → identical string. `soft_decimal` 2.05 == 2.0500 (same key); 2.05 vs 2.06 (different keys). Different `soft_book`/`sharp_book`/`selection` → different keys.

**test_normalize.py**

- Known alias → canonical_id. Unknown string → `NAME_NORM_FAIL` (no guess). Home/away mapping not swapped.

**test_sharp_source.py**

- Sharp absent → `NO_SHARP`. Primary/fallback no-vig differ by 0.02 (tol 0.01) → `SHARP_DISAGREE`. Sharp `age = window+1` → `STALE_SHARP`. Within tol + fresh → valid set returned.

**test_gates.py**

- Each gate fires its code with the specified `trigger_values` keys, in order. 3-outcome market → `NOT_TWO_WAY`. Soft price 60.0 (>max) → `PRICE_SANITY`. Both sides cross for one (event, soft_book, sharp_book) → `TWO_SIDED_EDGE` written for **both** sides, **no** candidate.

**test_candidate_upsert.py** (dedup)

- First sighting → `created=True`, status `DETECTED`, `observation_count=1`, one observation.
- Identical second sighting → `created=False`, `observation_count=2`, status unchanged, two observations, one candidate.
- Sharp prob changes, soft price same → `created=False`, observation appended (its `edge_pct` differs).
- Soft price changes → `created=True` (new candidate).
- Reappearance at same price after a gap → `created=False`, observation appended.

**test_confirm.py** (lifecycle)

- `confirms_required=1`, edge persists → `CONFIRMED`.
- Edge vanishes → `TRANSIENT(VANISHED)`. Stale on confirm → `TRANSIENT(WENT_STALE)`. Re-pull error → `TRANSIENT(REPULL_ERROR)`. Soft price drifted off key → `TRANSIENT(OFF_KEY_PRICE)`. Event past commence at claim → `TRANSIENT(CONFIRM_EXPIRED)`.
- `confirms_required=2`: first pass → still `PENDING_CONFIRM`, `confirm_due_ts` rescheduled; second pass → `CONFIRMED`.
- Confirm re-pull writes its own `raw_api_pulls` row and a `phase='CONFIRM'` observation.

**test_clv.py**

- `CONFIRMED` + close present → `GRADED`, `clv_pct = clv_pct(soft_decimal, p_close)`, one clv row.
- Close missing → `UNGRADED`, `clv_pct NULL`. Non-CONFIRMED candidate is never graded. No second clv row on re-grade.

**test_report_funnel.py**

- Seeded rows: `raw_detections = COUNT(observations)`; `unique_candidates = COUNT(candidates)`; `duplicate_sightings = raw − unique = Σ(observation_count−1)`. Mean/median CLV computed only over `status='GRADED'`. `transient_rate = transient_unique/(confirmed_unique+transient_unique)`. P/L appears last.

**test_rejection_logging.py** (no silent drops)

- For a crafted pull, assert every evaluated opportunity ends as exactly one of: a candidate observation OR a rejection row. Count reconciliation: `evaluated == observations_this_pull + rejections_this_pull`.

**test_integration_dryrun.py** — see §7 expected end-state.

-----

## 7. Dry-run procedure (no API calls)

1. Set `run.dry_run: true` (routes `api_pull` to the fixture loader; any real network call raises).
1. `fixtures/scenario_basic.yaml` maps a deterministic clock to payloads:
   
   ```yaml
   clock_start: "2026-05-30T17:00:00Z"
   ticks:
     - { at: "2026-05-30T17:00:00Z", kind: detection, file: pull_0001.json }
     - { at: "2026-05-30T17:01:30Z", kind: detection, file: pull_0002.json }  # repeat sighting
     - { at: "2026-05-30T17:00:45Z", kind: confirm,   key_event: E1, file: confirm_0001.json }
     - { at: "2026-05-30T18:55:00Z", kind: close,     event: E1, file: close_0001.json }
   ```
1. Run: `python scripts/run_dryrun.py --config config.yaml`.
- Steps the simulated clock; invokes Job A on detection ticks, Job B on confirm ticks (claiming due rows), Job C on close ticks; all reads come from fixtures.
1. The scenario must exercise: a steady-state repeat (no new candidate), a soft-price change (new candidate), a reappearance, one `TRANSIENT`, one `TWO_SIDED_EDGE`, and one `GRADED` close.
1. Assertions (the integration test): exact `raw_detections / unique_candidates / duplicate_sightings / confirmed_unique / transient_unique / graded_unique` and one correct `clv_pct`/`beat_close`.

-----

## 8. Live audit procedure (3–5 days)

```bash
# 0. one-time
export ODDS_API_KEY=...                     # never in config.yaml
pip install -r requirements.txt
pytest -q                                   # ALL tests green (gate to proceed)

# 1. confirm the feed supports the target
python scripts/init_db.py --config config.yaml
python scripts/run_inventory.py --config config.yaml   # must confirm sharp + soft + h2h; non-zero exit aborts

# 2. start the three long-running jobs (separate processes; WAL handles concurrency)
nohup python scripts/run_harness.py  --config config.yaml >> logs/harness.log 2>&1 &
nohup python scripts/run_confirm.py  --config config.yaml >> logs/confirm.log 2>&1 &
nohup python scripts/run_closing.py  --config config.yaml >> logs/closing.log 2>&1 &

# 3. daily report (cron or manual, e.g. 09:00 local)
python scripts/run_report.py --config config.yaml --window today
python scripts/run_report.py --config config.yaml --cumulative

# 4. end of window: graceful stop (writes finish_audit_run), then final cumulative report
#    (send SIGTERM to the three jobs; they flush and exit non-zero only on error)
python scripts/run_report.py --config config.yaml --cumulative
```

> The detection loop self-paces on `pull_interval_seconds`; confirm and closing self-pace on their poll intervals. `edge_threshold_pct` is locked for the entire window.

-----

## 9. Output artifacts

- **`data/market_translation.sqlite`** — the product: raw payloads, events, snapshots, outcomes, candidates, observations, rejections, closing_lines, clv_results, audit_runs (config snapshot + hash + code version), system_errors.
- **`reports/daily/YYYY-MM-DD.md` + `.csv`** — daily funnel report (CLV-led, P/L last).
- **`reports/cumulative/cumulative.md` + `.csv`** — running funnel + bootstrap CLV CI + sample progress vs Tier-1/Tier-2 thresholds.
- **CSV exports** (from report run): `candidates.csv`, `candidate_observations.csv`, `rejections.csv`, `clv_results.csv` (for external inspection; the DB remains source of truth).
- **`logs/harness.log`, `confirm.log`, `closing.log`** — process logs (operational; `system_errors` table is the authoritative error record).
- **No dashboard, no web output, no bet file.** (Non-goal.)

-----

## 10. Stop conditions (halt immediately)

Halt the run (stop the three jobs) and report — do **not** “tune harder” — on any of:

- **Raw write failure** — a derived step would proceed without a stored raw payload (raw-first broken).
- **Repeated `API_FAIL`** beyond `max_retries` across consecutive pulls (feed down / auth expired).
- **DB integrity error** — `UNIQUE`/`CHECK`/FK violation, or `SQLITE_BUSY` not clearing after backoff.
- **Normalization breakdown** — `NAME_NORM_FAIL` spikes (e.g., a feed naming change), since unmatched events silently shrink the pool.
- **`TWO_SIDED_EDGE` becomes frequent** — implies de-vig or feed pricing is broken; the math is no longer trustworthy.
- **Clock/timezone mismatch detected** — any non-UTC write, or `pull_timestamp` earlier than `api_last_update` (freshness math would be corrupted).
- **Confirm/closing job death** — a worker exits without `finish_audit_run` and does not restart cleanly.

End-of-window (not immediate-halt) verdict is the report’s trustworthiness flag: `UNTRUSTWORTHY` if unique-based `transient_rate > 0.50` or `close_missing_rate > 0.15` → the honest conclusion is “no measurable signal in this data,” per P1 §6.

-----

## 11. Known non-goals (v0.1 + P1)

v0.1 explicitly does **not**:

- place real-money bets, size bets, or apply Kelly (flat 1u is bookkeeping only);
- include a human-approval step (v0.1 tests the mechanical signal, not judgment);
- use agents, weather, props, spreads, or totals;
- handle three-way markets, alternate lines, live/in-play, or parlays;
- build a dashboard or web app (CLI + Markdown/CSV reports only);
- change strategy, thresholds (locked), or the sharp-source rule;
- let Claude price, size, or decide — the only permitted Claude use is read-only log summarization that cites row IDs (T19), preferably not built in v0.1;
- claim or imply profitability — a passing CLV result is a measurement of signal, and capturability remains untested in paper.