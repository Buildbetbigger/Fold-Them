# Build-Plan Precision Patch P2 — Pull-Level Failure Accounting & Clock-Skew

**Applies to:** Developer Execution Plan (v0.1 + Patch P1). Controlling documents unchanged; this patch touches **only** pull-level failure accounting and clock-skew handling.

**Invariants preserved:** Claude never prices / sizes / decides. The log is the product. Raw-first storage. Repeated sightings cannot inflate the sample. CLV only on graded unique candidates.

-----

## 0. The three-way separation (the core of this patch)

`API_FAIL` is removed from the **rejection** domain. Failures are now recorded in exactly one of three places, by where they occur in the pipeline:

|Layer            |Records                                                         |Where                                |Examples                                                                                                                                                  |
|-----------------|----------------------------------------------------------------|-------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------|
|**rejections**   |evaluated opportunities / candidate-gate failures               |`detect.py`, `gates.py`, `confirm.py`|`EVENT_FIELDS_MISSING`, `NO_SHARP`, `STALE_*`, `NOT_TWO_WAY`, `NAME_NORM_FAIL`, `PRICE_*`, `DUP_OUTCOME`, `BELOW_THRESHOLD`, `TWO_SIDED_EDGE`, `TRANSIENT`|
|**pull_failures**|failed API pulls / unusable provider responses (pre-opportunity)|`api_client.py`                      |`HTTP_TIMEOUT`, `HTTP_429`, `HTTP_500`, `AUTH_FAIL`, `NETWORK_FAIL`, `PARSE_FAIL`, `PROVIDER_SHAPE_CHANGE`, `API_FAIL` (fallback)                         |
|**system_errors**|application / infrastructure failures                           |any module                           |DB write/integrity errors, config errors, uncaught exceptions, `CLOCK_SKEW_WARNING` (WARN), `CLOCK_SKEW_HALT` (FATAL)                                     |

**Boundary rule that prevents double-counting:** a 200 response whose **envelope** can’t be parsed/iterated is a *pull* failure (`PROVIDER_SHAPE_CHANGE`/`PARSE_FAIL` → the whole pull is unusable, no gate chain runs). A parsed envelope with **one malformed event** is an *opportunity* failure (`EVENT_FIELDS_MISSING` rejection → the pull succeeded). Pull failures never enter rejection counts; rejections never enter pull-failure counts.

**Raw-first preserved:** if any response *body* is received (even a 500 or malformed JSON), it is stored to `raw_api_pulls` first (so `pull_id` and `raw_response_hash` exist on the failure row). Pure network/timeout/auth-handshake failures produce no body → `pull_id` and `raw_response_hash` are `NULL`.

**Constants delta (`constants.py`):** remove `API_FAIL` from `REJECTION_CODE`; add `PULL_FAILURE_CODE` (the 8 codes); add `SYSTEM_CODE` values `CLOCK_SKEW_WARNING`, `CLOCK_SKEW_HALT`.

**Ticket delta:** T7 (api_client) now writes `pull_failures` (not a rejection) on pull failure and returns without derived processing. The §6 gate chain no longer has an `API_FAIL` gate 1; it begins at `EVENT_FIELDS_MISSING` and runs only on a usable pull.

-----

## 1. `pull_failures` — table + attempt/cycle model

**Justification over `system_errors`:** pull failures need queryable, per-attempt fields (`failure_code`, `http_status`, `retry_count`, `resolved`) to drive the failure-rate report and the consecutive-failure halts. `system_errors` stays reserved for true application/infrastructure exceptions. A dedicated table keeps the two cleanly separable, as the patch requires.

**Attempt vs cycle model:**

- A **pull attempt** = one HTTP request. Each *failed* attempt → one `pull_failures` row (`resolved=0`). A *successful* attempt writes only a `raw_api_pulls` row (no failure row).
- A **pull cycle** = the attempts for one scheduled tick (up to `api.max_retries + 1`). If a later attempt in the cycle succeeds, the cycle’s earlier failure rows are marked `resolved=1` (transient, recovered). If the whole cycle fails, they remain `resolved=0` (this tick produced no usable pull).
- **Consecutive pull failures** = consecutive cycles with zero usable pull (runtime counter `consecutive_failed_cycles`, authoritative for halts; also derivable from the table by time-ordering).

```sql
CREATE TABLE pull_failures (
    failure_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_run_id      INTEGER NOT NULL REFERENCES audit_runs(audit_run_id),
    pull_id           INTEGER REFERENCES raw_api_pulls(pull_id),   -- NULL if no body received
    failure_timestamp TEXT    NOT NULL,                            -- UTC
    sport_key         TEXT    NOT NULL,
    market_key        TEXT    NOT NULL,
    endpoint          TEXT    NOT NULL,
    failure_code      TEXT    NOT NULL CHECK (failure_code IN
        ('API_FAIL','HTTP_TIMEOUT','HTTP_429','HTTP_500',
         'PARSE_FAIL','AUTH_FAIL','NETWORK_FAIL','PROVIDER_SHAPE_CHANGE')),
    http_status       INTEGER,                                     -- NULL for network/timeout
    error_message     TEXT    NOT NULL,
    retry_count       INTEGER NOT NULL DEFAULT 0,                  -- attempt index within the cycle
    raw_response_hash TEXT,                                        -- NULL if no body received
    resolved          INTEGER NOT NULL DEFAULT 0                   -- bool: cycle later succeeded
);
```

`API_FAIL` is the fallback code when a failure cannot be classified into a specific one.

-----

## 2. Reporting patch (pull health, kept separate from rejections)

Add a **Pull Health** block to both daily and cumulative reports, placed after the funnel counts and **before** rejection-by-reason. It must **not** be merged into candidate-rejection counts. CLV still leads; P/L still last.

**Definitions (window-scoped, computed directly from the tables):**

```
failed_attempts        = COUNT(pull_failures)
successful_pulls       = COUNT(raw_api_pulls r
                              WHERE r.pull_id NOT IN (SELECT pull_id FROM pull_failures
                                                      WHERE pull_id IS NOT NULL))
total_pull_attempts    = successful_pulls + failed_attempts
pull_failure_rate      = failed_attempts / total_pull_attempts
failed_pulls_by_code   = COUNT(pull_failures) GROUP BY failure_code
consecutive_pull_failures = max run of cycles with zero usable pull in the window
                            (runtime counter = current value; report = window max)
```

(A successful attempt writes a `raw_api_pulls` row and **no** failure row, so `successful_pulls` correctly excludes the stored bodies of 500/parse/shape failures, which *are* referenced by a `pull_failures.pull_id`.)

**Trustworthiness verdict — extended (additive to P1):** the run is also flagged `UNTRUSTWORTHY` if `pull_failure_rate > failure_policy.pull_failure_rate_untrustworthy` over the window. A high failure rate means the pool was sampled with gaps, so divergence frequency and CLV are not representative. Reported alongside the existing transient-rate and close-missing-rate flags.

Rejection-by-reason in the report draws only from `REJECTION_CODE` (no pull-level codes can appear).

-----

## 3. Stop-condition patch (immediate halt rules)

Additive to build-plan §10. The old “repeated `API_FAIL` beyond `max_retries`” item is refined into the specific codes below. New config in §4 (`failure_policy`).

Halt the run (stop all three jobs) and report — never “tune harder” — on:

- **`AUTH_FAIL`** → halt on **first** occurrence (`auth_fail_halt: true`). Bad/expired credentials; continuing is pointless.
- **`PROVIDER_SHAPE_CHANGE`** → halt on **first** occurrence (`provider_shape_change_halt: true`). The parse/freshness contract is broken; continuing yields garbage or silent gaps.
- **Repeated `HTTP_429`** → exponential backoff retry first; halt when `consecutive_http_429 >= max_consecutive_http_429`.
- **Repeated `HTTP_500`** → backoff retry first; halt when `consecutive_http_500 >= max_consecutive_http_500`.
- **`PARSE_FAIL` across consecutive pulls** → halt when `consecutive_parse_fail >= max_consecutive_parse_fail` (a single truncated response may be transient; a run of them is not).
- **Pull failure rate** → halt when `pull_failure_rate > pull_failure_rate_halt` over the trailing `pull_failure_rate_window_minutes`.
- **Consecutive failed cycles** → halt when `consecutive_failed_cycles >= max_consecutive_pull_failures` (the feed is effectively down).

On halt: write a `system_errors` FATAL with the triggering code, mark `audit_runs.status='ABORTED'`, flush, exit non-zero.

-----

## 4. Clock-skew patch (tolerance, not a hard halt on any skew)

The build-plan §10 line “`pull_timestamp` earlier than `api_last_update` → halt” is replaced by a tolerant rule.

**Config delta:**

```yaml
time:
  storage_timezone: "UTC"
  display_timezone: "America/New_York"
  max_clock_skew_seconds: 10        # NEW
```

**Rule** (evaluated per snapshot, on every usable pull — in **both** `detect.py` and the confirm worker’s re-pull):

```
skew = seconds(api_last_update - pull_timestamp)     # positive = provider clock ahead
if skew <= 0:
    normal freshness: effective_age = pull_timestamp - api_last_update
elif skew <= max_clock_skew_seconds:
    log CLOCK_SKEW_WARNING (system_errors, WARN, component='clock')
    effective_age = max(0, pull_timestamp - api_last_update)   # clamp; treat as current
else:   # skew > tolerance
    log CLOCK_SKEW_HALT (system_errors, FATAL)
    HALT — freshness math is unreliable
```

Across multiple books in one pull, the **maximum** skew governs: any single snapshot exceeding tolerance halts the run. Within tolerance, the warning is logged and the affected snapshot’s age is clamped to `0` so a tiny skew never reads as stale or errors a gate.

**`failure_policy` config block (NEW):**

```yaml
failure_policy:
  retry_backoff_seconds: [2, 8]            # backoff schedule for 429/500 within a cycle
  max_consecutive_pull_failures: 5         # cycles with zero usable pull -> halt
  max_consecutive_http_429: 5
  max_consecutive_http_500: 5
  max_consecutive_parse_fail: 3
  pull_failure_rate_halt: 0.25             # over trailing window -> halt
  pull_failure_rate_window_minutes: 30
  pull_failure_rate_untrustworthy: 0.10    # report flag only (not a halt)
  auth_fail_halt: true
  provider_shape_change_halt: true
```

(`api.max_retries` from the base config still bounds attempts per cycle; `retry_count` on each failure row is the attempt index.)

-----

## 5. Migration patch

**Migration name:** `009_pull_failures.sql` (applied after `008_indexes.sql`; FK targets `audit_runs` and `raw_api_pulls` exist by migrations 001/003).

**Contents:** the `CREATE TABLE pull_failures` from §1, plus its indexes (self-contained in this migration):

```sql
CREATE INDEX ix_pf_run_ts   ON pull_failures(audit_run_id, failure_timestamp);
CREATE INDEX ix_pf_code     ON pull_failures(failure_code, failure_timestamp);
CREATE INDEX ix_pf_resolved ON pull_failures(resolved, failure_timestamp);
CREATE INDEX ix_pf_sport    ON pull_failures(sport_key, market_key, failure_timestamp);
```

`scripts/init_db.py` migration list gains `009_pull_failures.sql` at the end. No existing table is altered.

**Repository writer signatures (`repo.py`):**

```python
def insert_pull_failure(conn, audit_run_id: int, pull_id: int | None,
                        sport_key: str, market_key: str, endpoint: str,
                        failure_code: str, http_status: int | None,
                        error_message: str, retry_count: int,
                        raw_response_hash: str | None) -> int:
    """Append one failed pull attempt (resolved=0)."""

def resolve_pull_failures(conn, failure_ids: list[int]) -> int:
    """Set resolved=1 on a cycle's earlier failures after a later attempt succeeds."""

def pull_health_counts(conn, window_start: str, window_end: str) -> PullHealth:
    """Return successful_pulls, failed_attempts, total_pull_attempts,
    pull_failure_rate, failed_pulls_by_code, consecutive_pull_failures (window max)."""
```

**Affected tests:** `test_pull_failures.py` (new, §6), `test_report_funnel.py` (extended to assert separation), `test_clock_skew.py` (new, §6), and `test_rejection_logging.py` (updated — see below).

-----

## 6. Test patch

**test_pull_failures.py**

- **HTTP timeout:** no body → `pull_failures(HTTP_TIMEOUT)`, `pull_id` NULL, `raw_response_hash` NULL; **no** rejection row; **no** `raw_api_pulls` row; retry attempted per `api.max_retries`.
- **Parse failure:** body received → `raw_api_pulls` row stored (raw-first), then `pull_failures(PARSE_FAIL)` with that `pull_id` + `raw_response_hash`; no rejection; gate chain does not run.
- **Auth failure:** `pull_failures(AUTH_FAIL)` → run **halts** immediately; `audit_runs.status='ABORTED'`; `system_errors` FATAL written.
- **Provider shape change:** 200 but envelope unparseable → `pull_failures(PROVIDER_SHAPE_CHANGE)` (raw stored) → **halt** on first occurrence.
- **Repeated 429:** backoff applied; after `max_consecutive_http_429` consecutive → **halt**; each attempt logged with incrementing `retry_count`.
- **Cycle recovery:** attempt-1 `HTTP_500` then attempt-2 success → failure row `resolved=1`; `successful_pulls += 1`; cycle counts as success (does not increment `consecutive_failed_cycles`).

**test_clock_skew.py**

- **Within tolerance:** `api_last_update` ahead by `≤ max_clock_skew_seconds` → `CLOCK_SKEW_WARNING` in `system_errors` (WARN); effective age clamped to `0`; processing continues; **no halt**.
- **Beyond tolerance:** ahead by `> max_clock_skew_seconds` → `CLOCK_SKEW_HALT` (FATAL); run halts; applies identically in detection and confirm-worker re-pull.
- **Multi-book:** one book within tolerance + one beyond → halt (max skew governs).

**test_report_funnel.py (extended)**

- `pull_failure_rate = failed_attempts / total_pull_attempts`; `successful_pulls` excludes `raw_api_pulls` referenced by any `pull_failures.pull_id`.
- Rejection-by-reason contains **no** pull-level codes; pull-health counts and rejection counts have **zero overlap**.
- A window with both pull failures and opportunity rejections reports them in separate blocks with independent totals.

**test_rejection_logging.py (updated)**

- Reconciliation is now two independent identities: (a) per usable pull, `evaluated_opportunities == observations_this_pull + opportunity_rejections_this_pull`; (b) per scheduled cycle, the cycle is recorded as exactly one of `successful_pull` or `failed_attempt(s)` in `pull_failures`. `API_FAIL` no longer appears in `rejections`.