# Build-Plan Precision Patch P2a — Attempt vs Usable-Cycle Reporting

**Applies to:** Developer Execution Plan (v0.1 + P1 + P2). Touches **only** pull-failure reporting granularity. Nothing else changes.

**Invariants preserved:** Claude never prices / sizes / decides. The log is the product. Raw-first storage. Repeated sightings cannot inflate the sample. CLV only on graded unique candidates.

-----

## 0. Why a `pull_cycles` table (not a redesign)

P2 established the attempt/cycle model and a *runtime* `consecutive_failed_cycles` counter, but stored no record of a cycle. The metrics P2a requires — `failed_cycles`, `total_scheduled_cycles`, `usable_cycle_failure_rate` — cannot be computed reliably by inferring cycles from the clock (the very failure conditions being measured distort clock-based inference). The faithful implementation is to persist each scheduled cycle as a fact. `pull_cycles` is that minimal substrate; it adds no behavior, only bookkeeping.

**Definitions (as specified, made queryable):**

- **failed_cycle** = a scheduled pull cycle that produced **zero** usable `raw_api_pulls` after retries.
- **resolved failure** = a failed *attempt* inside a cycle that later produced a usable pull (the cycle recovered).
- `attempt_failure_rate = failed_attempts / total_pull_attempts`
- `usable_cycle_failure_rate = failed_cycles / total_scheduled_cycles` ← **stronger sampling-quality metric** (measures actual missed market windows; recovered cycles still sampled the window).

-----

## 1. Schema patch

**Migration `010_pull_cycles.sql`** (after `009_pull_failures.sql`; FK targets exist by 001/003):

```sql
CREATE TABLE pull_cycles (
    cycle_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_run_id    INTEGER NOT NULL REFERENCES audit_runs(audit_run_id),
    sport_key       TEXT    NOT NULL,
    market_key      TEXT    NOT NULL,
    scheduled_ts    TEXT    NOT NULL,        -- tick's scheduled fire time (UTC)
    started_ts      TEXT    NOT NULL,        -- when the cycle actually began
    finished_ts     TEXT,                    -- when it concluded
    attempts        INTEGER NOT NULL DEFAULT 0,
    outcome         TEXT    NOT NULL DEFAULT 'PENDING'
        CHECK (outcome IN ('PENDING','SUCCESS','FAILED')),
    usable_pull_id  INTEGER REFERENCES raw_api_pulls(pull_id)   -- set on SUCCESS, NULL on FAILED
);
CREATE INDEX ix_pc_run_ts  ON pull_cycles(audit_run_id, scheduled_ts);
CREATE INDEX ix_pc_outcome ON pull_cycles(outcome, scheduled_ts);
CREATE INDEX ix_pc_sport   ON pull_cycles(sport_key, market_key, scheduled_ts);

-- link each failed attempt to its cycle (additive)
ALTER TABLE pull_failures ADD COLUMN cycle_id INTEGER REFERENCES pull_cycles(cycle_id);
CREATE INDEX ix_pf_cycle ON pull_failures(cycle_id);
```

`scripts/init_db.py` migration list gains `010_pull_cycles.sql` at the end.

-----

## 2. Writer + harness integration

**`repo.py` signatures:**

```python
def open_cycle(conn, audit_run_id: int, sport_key: str, market_key: str,
               scheduled_ts: str, started_ts: str) -> int:
    """Insert a PENDING cycle; returns cycle_id."""

def close_cycle(conn, cycle_id: int, outcome: str, attempts: int,
                finished_ts: str, usable_pull_id: int | None) -> None:
    """Set SUCCESS (with usable_pull_id) or FAILED (usable_pull_id NULL)."""

def resolve_pull_failures(conn, cycle_id: int) -> int:   # signature refined from P2
    """Mark resolved=1 for a cycle's failed attempts after that cycle succeeds."""
# insert_pull_failure() gains a cycle_id argument (was added in P2; now required).
```

**Flow (extends T7/T11; no behavior change beyond logging the cycle):**

```
on each scheduled tick:
  cid = open_cycle(...PENDING...)
  n = 0
  for attempt in 1..(api.max_retries + 1):
      n += 1
      try pull:
          success -> store_raw_pull() -> usable_pull_id; break
      except failure:
          insert_pull_failure(cycle_id=cid, retry_count=n, code, ...)  # resolved=0
          backoff(failure_policy.retry_backoff_seconds)
  if success:
      close_cycle(cid, 'SUCCESS', attempts=n, usable_pull_id)
      resolve_pull_failures(cid)            # earlier failed attempts -> resolved=1
      consecutive_failed_cycles = 0
  else:
      close_cycle(cid, 'FAILED', attempts=n, usable_pull_id=None)   # failures stay resolved=0
      consecutive_failed_cycles += 1        # feeds P2 consecutive-failure halt
```

-----

## 3. Reporting patch

Extend P2’s **Pull Health** block into two clearly separated sub-blocks (still separate from rejection counts; CLV still leads; P/L still last). **Rename** P2’s `pull_failure_rate` → `attempt_failure_rate`.

**Attempt level:**

```
failed_attempts            = COUNT(pull_failures)
resolved_failed_attempts   = COUNT(pull_failures WHERE resolved = 1)
unresolved_failed_attempts = COUNT(pull_failures WHERE resolved = 0)
total_pull_attempts        = successful_cycles + failed_attempts
attempt_failure_rate       = failed_attempts / total_pull_attempts
failed_attempts_by_code    = COUNT(pull_failures) GROUP BY failure_code
```

**Cycle level (primary sampling quality):**

```
successful_cycles          = COUNT(pull_cycles WHERE outcome = 'SUCCESS')
failed_cycles              = COUNT(pull_cycles WHERE outcome = 'FAILED')
total_scheduled_cycles     = successful_cycles + failed_cycles      -- completed cycles in window
usable_cycle_failure_rate  = failed_cycles / total_scheduled_cycles
consecutive_pull_failures  = window max run of outcome='FAILED' cycles
```

**`pull_health_counts()` returns all of the above.** Note `successful_cycles == successful_pulls` (P2’s exclusion definition) — asserted as an integrity check, not computed twice.

**Trustworthiness verdict — extended:** consider both rates, with the **cycle rate primary**:

- `UNTRUSTWORTHY` if `usable_cycle_failure_rate > failure_policy.usable_cycle_failure_rate_untrustworthy` (true missed windows).
- `CAUTION` (secondary) if `attempt_failure_rate > failure_policy.attempt_failure_rate_untrustworthy` (provider flakiness/cost without necessarily missing data).
- Combined with the existing P1 transient-rate and close-missing-rate flags.

-----

## 4. Config patch

```yaml
failure_policy:
  # renamed from P2 (pull_failure_rate_* -> attempt_failure_rate_*):
  attempt_failure_rate_untrustworthy: 0.10
  attempt_failure_rate_halt: 0.25
  attempt_failure_rate_window_minutes: 30
  # NEW cycle-level thresholds (primary; stricter, since failed cycles are true gaps):
  usable_cycle_failure_rate_untrustworthy: 0.05
  usable_cycle_failure_rate_halt: 0.20
  usable_cycle_failure_rate_window_minutes: 30
  # unchanged from P2 (now precisely = consecutive pull_cycles with outcome='FAILED'):
  max_consecutive_pull_failures: 5
```

(All thresholds are starting points; tune after one real window.)

**Stop-condition delta (additive to P2 §3):** halt when `usable_cycle_failure_rate > usable_cycle_failure_rate_halt` over the trailing window. The consecutive-cycle halt (`max_consecutive_pull_failures`) and attempt-rate halt remain.

-----

## 5. Integrity invariants (the self-checking part)

Because resolved attempts belong to recovered cycles and unresolved attempts belong to failed cycles, the bookkeeping verifies itself:

- `successful_cycles == successful_pulls` (cycle table vs raw-exclusion count agree).
- `total_scheduled_cycles == successful_cycles + failed_cycles`.
- Every `resolved = 1` attempt’s `cycle_id` has `outcome = 'SUCCESS'`; every `resolved = 0` attempt’s `cycle_id` has `outcome = 'FAILED'`.
- `COUNT(DISTINCT cycle_id WHERE resolved = 0) == failed_cycles`.
- `total_pull_attempts == failed_attempts + successful_cycles` (each successful cycle contributes exactly one successful attempt).

-----

## 6. Test patch

**test_pull_cycles.py (new)**

- **Clean success** (1 attempt): `pull_cycles` SUCCESS, `usable_pull_id` set, `attempts=1`, zero `pull_failures`; `successful_cycles=1`.
- **Recovered cycle** (fail → success): SUCCESS, `attempts=2`, one `pull_failures` row `resolved=1` linked to the cycle; counts as a successful cycle and a resolved failed attempt; `consecutive_failed_cycles` reset to 0.
- **Failed cycle** (all attempts fail): FAILED, `usable_pull_id` NULL, all its `pull_failures` `resolved=0`; `failed_cycles=1`; `consecutive_failed_cycles` incremented.
- **Invariants** (assert all of §5).

**test_report_funnel.py (extended)**

- Report shows all seven required metrics: failed attempts, resolved/unresolved failed attempts, successful cycles, failed cycles, `attempt_failure_rate`, `usable_cycle_failure_rate`.
- Rates computed exactly per the definitions; `usable_cycle_failure_rate` drives the trustworthiness verdict as primary, `attempt_failure_rate` as secondary.
- Pull-health metrics remain disjoint from rejection-by-reason counts (no overlap).

**test_pull_failures.py (extended from P2)**

- Consecutive-failure halt now triggers on `max_consecutive_pull_failures` consecutive `pull_cycles` with `outcome='FAILED'`; a recovered cycle in between resets the counter.
- New halt: `usable_cycle_failure_rate > usable_cycle_failure_rate_halt` over the window → run aborts.