# 08 — Implementation Errata (apply before build)

**Applies to the full controlling stack:** v0.1 + P1 + P2 + P2a + P3 + P3a + IA-1.
**Status:** Resolves handoff ambiguities before Claude Code implementation. **No redesign.** This errata has the **highest precedence** — on any conflict with an earlier document, the errata wins.

---

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

---

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

| cycle_type | Written by | Fetches? | Sampling cycle? |
|---|---|---|---|
| `LIVE_DETECTION` | detection loop (Job A) | yes | **yes** |
| `LIVE_CONFIRM` | confirm worker (Job B) | yes | no (operational) |
| `LIVE_CLOSE_CAPTURE` | closing scheduler (Job C) | yes | no (operational) |
| `HISTORICAL_INGEST` | historical snapshot fetch | yes | **yes** |
| `HISTORICAL_REPLAY` | replay processing (reads stored snapshots) | no | no (administrative) |
| `INVENTORY` | inventory probe | yes | no (operational) |

**Reporting rule.** Primary **sampling-health** metrics — P2a `usable_cycle_failure_rate` and the sampling `attempt_failure_rate`, P3a `coverage_gap_rate` and inter-snapshot gap stats — are computed **only** over sampling cycles:

```sql
WHERE cycle_type IN ('LIVE_DETECTION', 'HISTORICAL_INGEST')
```

Do not mix detection-sampling failures with confirm, close-capture, inventory, or replay-administrative cycles **unless** the report explicitly labels a combined operational view. (Operational health may still be reported per `cycle_type`; it is just kept separate from the sampling-quality verdict.)

---

## E3 — Ticket order

- Implement **T1–T18 first.**
- **Skip T19** (the optional Claude summarizer) unless explicitly authorized — it is **not** part of the required v0.1 harness.
- **Then implement T20–T26** for historical mode (P3).
- **T5 (`formulas.py`) is the first *pure deterministic oracle* ticket — not literally the first ticket.** Scaffold/config/schema/repository (T1–T4) precede it per the build plan.

---

## E4 — Superseded `API_FAIL` wording

Older base / build-plan / P1 text may describe `API_FAIL` as a **rejection**. That wording is **superseded by P2**. Final rule:

- **`API_FAIL` is a `pull_failures` code, not a rejection code.**
- Opportunity-level failures → `rejections`.
- Pull / API / provider failures → `pull_failures`.
- Infrastructure / application failures → `system_errors`.

---

## E5 — Superseded `CLOSE_MISSING` wording

Older base §6 (gate 14) lists `CLOSE_MISSING` as a **rejection** code. That wording is **superseded by base §8 / P1 §3,§5 / P3 §7** (the candidate lifecycle). Same class of issue as E4.

**Final rule:**
- **`CLOSE_MISSING` is not a rejection code.** A missing or unusable close for a `CONFIRMED` candidate is a **grade outcome**, not a gate failure: `status → UNGRADED`, `clv_results.grade_status → UNGRADED_CLOSE_MISSING`, `clv_pct = NULL`, and **no rejection row is written**.
- Rationale: a `CONFIRMED` candidate is a validated candidate, already counted in the funnel — it cannot also be a rejected opportunity. Recording it as a rejection would misclassify it and inflate the rejection ledger with non-rejections.
- **Gate 14 leaves the rejection gate-chain** and is handled in the grading job (T13/T14) — parallel to how E4 relocated gate 1 (`API_FAIL`) to the pull layer.

**Resolved `RejectionCode` set** = base §6 **gates 2–13 + `TWO_SIDED_EDGE`** (gate 14 `CLOSE_MISSING` removed):
`EVENT_FIELDS_MISSING, NO_SHARP, SHARP_DISAGREE, STALE_SHARP, STALE_SOFT, NOT_TWO_WAY, MARKET_MISMATCH, NAME_NORM_FAIL, PRICE_MISSING, DUP_OUTCOME, PRICE_SANITY, BELOW_THRESHOLD, TRANSIENT, TWO_SIDED_EDGE` (14 members).

**Do not over-correct — `TRANSIENT` STAYS a rejection code.** A confirm failure is a genuine rejected opportunity (it failed the confirm gate), so its dual record — `status=TRANSIENT` plus a `TRANSIENT` rejection carrying a `TransientReason` — is intended. Only `CLOSE_MISSING` moves out, because it describes a candidate that *passed* confirm.

**Tests:**
- `CLOSE_MISSING` is **not** a member of `RejectionCode` (regression guard, mirroring the E4 guard).
- `TRANSIENT` **is** a member of `RejectionCode`.
- (At T13/T14) a `CONFIRMED` candidate with a missing close → `status=UNGRADED`, `grade_status=UNGRADED_CLOSE_MISSING`, `clv NULL`, and **zero** rejection rows for that candidate.

**Deferred-enum tracker (updated):** `GradeStatus` (`GRADED | UNGRADED_CLOSE_MISSING`) → `constants.py` at T13/T14; `cycle_type` (E2's six values) → `constants.py` with `pull_cycles`.

---

## First build instruction (per the review ruling)

> Read `CLAUDE.md` and `/spec` in precedence order. Implement **T1 only**. Write tests first. Do **not** proceed to T2 until I approve the diff and test output.

The risk now is **implementation drift, not conceptual confusion.** Do not build the whole system in one pass.
