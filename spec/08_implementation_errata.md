# 08 — Implementation Errata (apply before build)

**Applies to the full controlling stack:** v0.1 + P1 + P2 + P2a + P3 + P3a + IA-1.
**Status:** Resolves handoff ambiguities before Claude Code implementation. **No redesign.** This errata has the **highest precedence** — on any conflict with an earlier document, the errata wins.

-----

## E1 — IA-1 close-window boot check (resolves contradictory test language)

IA-1 §1.1 stated the rule as `interval_s ≤ close_window_s` but its boundary test implied equality should refuse startup. Those conflict. Resolved rule:

```
interval_s     = historical_snapshot_interval_minutes * 60
close_window_s = close_capture_window_minutes * 60
```

- `interval_s > close_window_s` → **abort startup** (the configured historical cadence can manufacture `CLOSE_MISSING`).
- `interval_s == close_window_s` → **proceed, emit a boundary-risk warning**.
- `close_window_s ≥ 1.5 × interval_s` → recommended, not hard-required.

**Tests (replace the IA-1 §1.1 boundary test):**

- `interval_s > close_window_s` → refuses startup.
- `interval_s == close_window_s` → passes **with** a boundary-risk warning.
- `close_window_s ≥ 1.5 × interval_s` → passes **without** warning.

(The companion check `interval_s ≤ max_historical_gap_seconds` is unchanged: strict — any violation refuses startup.)

-----

## E2 — Add `cycle_type` to `pull_cycles`

`pull_cycles` (migration `010_pull_cycles.sql`) must distinguish cycle purpose so sampling health is not mixed with operational/API-task health. Add to the fresh `CREATE TABLE` (ALTER fallback if 010 was already applied):

```sql
cycle_type TEXT NOT NULL
  CHECK (cycle_type IN (
    'LIVE_DETECTION',
    'LIVE_CONFIRM',
    'LIVE_CLOSE_CAPTURE',
    'HISTORICAL_INGEST',
    'HISTORICAL_REPLAY',
    'INVENTORY'
  ))
```

```sql
CREATE INDEX ix_pc_type_run_ts ON pull_cycles(cycle_type, audit_run_id, scheduled_ts);
```

**Which driver writes which type:**

|cycle_type          |Written by                                |Fetches?|Sampling cycle?    |
|--------------------|------------------------------------------|--------|-------------------|
|`LIVE_DETECTION`    |detection loop (Job A)                    |yes     |**yes**            |
|`LIVE_CONFIRM`      |confirm worker (Job B)                    |yes     |no (operational)   |
|`LIVE_CLOSE_CAPTURE`|closing scheduler (Job C)                 |yes     |no (operational)   |
|`HISTORICAL_INGEST` |historical snapshot fetch                 |yes     |**yes**            |
|`HISTORICAL_REPLAY` |replay processing (reads stored snapshots)|no      |no (administrative)|
|`INVENTORY`         |inventory probe                           |yes     |no (operational)   |

**Reporting rule.** Primary **sampling-health** metrics — P2a `usable_cycle_failure_rate` and the sampling `attempt_failure_rate`, P3a `coverage_gap_rate` and inter-snapshot gap stats — are computed **only** over sampling cycles:

```sql
WHERE cycle_type IN ('LIVE_DETECTION', 'HISTORICAL_INGEST')
```

Do not mix detection-sampling failures with confirm, close-capture, inventory, or replay-administrative cycles **unless** the report explicitly labels a combined operational view. (Operational health may still be reported per `cycle_type`; it is just kept separate from the sampling-quality verdict.)

-----

## E3 — Ticket order

- Implement **T1–T18 first.**
- **Skip T19** (the optional Claude summarizer) unless explicitly authorized — it is **not** part of the required v0.1 harness.
- **Then implement T20–T26** for historical mode (P3).
- **T5 (`formulas.py`) is the first *pure deterministic oracle* ticket — not literally the first ticket.** Scaffold/config/schema/repository (T1–T4) precede it per the build plan.

-----

## E4 — Superseded `API_FAIL` wording

Older base / build-plan / P1 text may describe `API_FAIL` as a **rejection**. That wording is **superseded by P2**. Final rule:

- **`API_FAIL` is a `pull_failures` code, not a rejection code.**
- Opportunity-level failures → `rejections`.
- Pull / API / provider failures → `pull_failures`.
- Infrastructure / application failures → `system_errors`.

-----

## E5 — `CLOSE_MISSING` is a grade outcome, not a rejection (resolves base §6 vs §8)

Base §6 lists `CLOSE_MISSING` as gate 14 (a rejection code), but base §8, P1 §3/§5, and P3 §7 model a missing close as a **grading outcome**, not a candidate rejection. The two conflict; the **lifecycle wins** (same spirit as E4). Final rule:

- A `CONFIRMED` candidate whose close is missing/unusable is graded `status='UNGRADED'` with `clv_results.grade_status='UNGRADED_CLOSE_MISSING'` and `clv_pct=NULL`. **No `rejections` row is written.**
- `CLOSE_MISSING` is therefore **not** a `REJECTION_CODE` and is removed from it. The grade outcome lives on a **`GradeStatus`** enum (`GRADED | UNGRADED_CLOSE_MISSING`), introduced with the grading tickets (T13/T14).
- **Why it matters (not cosmetic):** the candidate is already counted as a candidate/observation; also writing a rejection would double-count it and break the P2a §5 reconciliation identity (`evaluated == observations + rejections`) and the P1 §5 funnel (`close_missing_rate = ungraded_unique / confirmed_unique`).
- `closing_lines.close_source_flag` (`NORMAL | FROM_SUSPENSION | MISSING`) is **unchanged** — that is per-event close provenance, distinct from the per-candidate grade outcome.

**Tests:** `CLOSE_MISSING` is absent from `RejectionCode` (regression guard, alongside the E4 guard for `API_FAIL`); a missing close yields exactly one `clv_results` row (`UNGRADED_CLOSE_MISSING`, `clv_pct` NULL) and **zero** `rejections` rows for that candidate.

-----

## First build instruction (per the review ruling)

> Read `CLAUDE.md` and `/spec` in precedence order. Implement **T1 only**. Write tests first. Do **not** proceed to T2 until I approve the diff and test output.

The risk now is **implementation drift, not conceptual confusion.** Do not build the whole system in one pass.