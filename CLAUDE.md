# CLAUDE.md — Build Brief: Market-Translation System v0.1

You (Claude Code) are implementing the **Market-Translation System**: a moneyline-only, soft-vs-sharp divergence **measurement harness** that validates Closing Line Value (CLV) over historical and then live data. It is **not** a betting bot. It never places, sizes, or decides a bet. The authoritative specification lives in `/spec`. Implement strictly to spec, **ticket by ticket, test-first, fail-closed**. The log is the product.

-----

## 1. Controlling documents (authoritative — read in order)

```
/spec/00_build_plan.md                      # structure, tickets T1–T26, migrations, config, signatures, tests, procedures
/spec/01_v0.1_base_spec.md                  # scope, sharp-source rule, gates, edge/CLV formulas, schema, records
/spec/02_P1_dedup.md                        # opportunity_key, observations, 7-state confirm lifecycle
/spec/03_P2_pull_failures_clock_skew.md     # pull_failures vs rejections vs system_errors; clock-skew tolerance
/spec/04_P2a_attempt_vs_cycle.md            # pull_cycles; attempt-vs-usable-cycle reporting; integrity invariants
/spec/05_P3_historical_backtest.md          # historical mode: walk-forward replay, no lookahead
/spec/06_P3a_gap_cachekey.md                # gap accounting (request vs inter-snapshot), request-signature cache key
/spec/07_IA1_implementation_invariants.md   # final boot/serialization/reporting invariants
/spec/08_implementation_errata.md           # HIGHEST PRECEDENCE: pre-build errata (E1–E4)
```

**These override your priors.** If code conflicts with spec, the spec wins. If two spec docs conflict, the **later patch wins, and the errata wins over all**: `08-errata > IA-1 > P3a > P3 > P2a > P2 > P1 > base`. `00_build_plan` is procedural; `01–08` are authoritative on behavior.

-----

## 2. Prime directives (non-negotiable)

- **No redesign. No new scope.** Do NOT add: agents, weather, props, spreads/totals, bet sizing, human approval, dashboards/web UI, real-money execution, or any Claude/LLM reasoning-pricing layer.
- **Fail closed.** Never impute, never swallow an error, never add a “helpful” fallback. Missing sharp → no candidate. Stale data → rejection. **A failing test is not permission to relax a rule — fix the code, not the rule or the test.**
- **The log is the product.** Every evaluated opportunity ends as exactly one observation **or** one rejection; every pull cycle is exactly one success **or** recorded failure. No silent drops.
- **Claude never prices, sizes, or decides.** Every probability, edge, stake, and CLV comes from a hard-coded deterministic function — never from an LLM.
- **Walk-forward only** in historical mode; **no lookahead**. **Repeated sightings cannot inflate the sample.** **CLV only on graded unique candidates.**

-----

## 3. Frozen decisions (implement exactly; do NOT “optimize” or reinterpret)

- `edge_threshold_pct` — from config, **locked** for a run; no within-window tuning path may exist.
- `opportunity_key` — exactly `audit_run_id|event_id|market_key|selection_canonical_id|soft_book|sharp_book|soft_decimal:.4f|threshold_used:.4f` (P1). The `UNIQUE` constraint on it is the dedup guarantee.
- The **7-state lifecycle** and its transitions (P1): `DETECTED → PENDING_CONFIRM → CONFIRMED → TRANSIENT | PENDING_GRADE → GRADED | UNGRADED`.
- The **sharp-source rule** and `sharp_disagree_tolerance` (base §2): never average disagreeing sharps.
- **Gate order and codes** (P2 §0/§6) and the three-way separation: `rejections` (opportunities) vs `pull_failures` (failed pulls) vs `system_errors` (infra). `API_FAIL` is a pull-failure code, **not** a rejection.
- The **attempt/cycle model** and the **integrity invariants** (P2a §5) — they must hold and be tested.
- **Window-boundary reset** (P3a §1): an intentional off-window jump is **not** a coverage gap.
- The **three IA-1 invariants** (§4 below).

**If you believe a frozen decision is wrong, STOP and ask.** Do not change it unilaterally.

-----

## 4. IA-1 invariants (must be enforced — see `/spec/07`)

**4.1 Boot config** (`config_loader`; abort startup if violated):
`interval_s ≤ max_historical_gap_seconds` (strict — violation aborts) **and** `interval_s ≤ close_window_s` (equality allowed, emits a boundary-risk warning; prefer `close_window_s ≥ 1.5 × interval_s`). See errata E1.

**4.2 Canonical request-signature hash** (historical cache key): `sha256` of canonically serialized, sorted-key JSON `{sport_key, market_key, region, normalized+deduped+sorted bookmakers, odds_format, endpoint_version, mode}`. The **timestamp is NOT in the hash** (it is the second column of the unique index `(request_signature_hash, historical_snapshot_ts) WHERE mode='HISTORICAL'`).

**4.3 Confirm-gap honesty** (report): record `confirm_gap_seconds = confirm_snapshot_ts − detection_snapshot_ts`; report `median`/`max` + **CLV bucketed by confirm gap**; never present a coarse historical confirm as a 60-second capturability test.

-----

## 5. Provider integration — you do NOT know the schema

Do **not** invent the odds API’s JSON shape, field names, team-name strings, or bookmaker keys. **Before implementing `api_client.py` / `normalize.py` (tickets T7/T8) you must be given a real, scrubbed sample payload and the provider’s API docs.** If you reach T7/T8 without them, **STOP and request them.** Build fixtures from the real payload, never from a guess.

-----

## 6. Workflow (agent-with-checkpoints)

1. Implement **one ticket at a time**, in this order (errata E3): **T1–T18 first; skip T19** (optional Claude summarizer) unless explicitly authorized; **then T20–T26** for historical mode. Do not skip ahead within that order.
1. After each ticket, run the full gate (`make check`) and **STOP** — present the diff + test output for human review. Do not start the next ticket until approved.
1. **T5 (`formulas.py`) is the first *pure deterministic oracle* ticket — not literally the first ticket.** Scaffold/config/schema/repository (T1–T4) precede it per the build plan. Confirm its exact expected values before anything stateful builds on them.
1. On any ambiguity or missing input: **STOP and ask.** Never guess a frozen value, and never relax an invariant to make a test pass.
1. Build order across the project: **H1 historical first** (offline, cheap, full chain) → live 3–5 day audit → optional read-only viewer last.

-----

## 7. Coding standards (highest)

- **Python ≥ 3.12**, one virtualenv, dependencies pinned in `pyproject.toml` (+ lockfile).
- **Full type hints. `mypy --strict` (or pyright strict) clean.** No `# type: ignore` without an inline justification.
- **`ruff` for lint + format** (line length, import order, complexity caps); **zero** lint errors.
- **Pure/deterministic core:** `formulas.py`, `opportunity_key.py`, `clv.py`, and the gate/decision logic are **pure functions** — no IO, no clock, no randomness. **Inject** the clock and the pull function as callables so live, historical, and tests share one code path.
- **Errors:** explicit, typed exceptions; no bare `except`; no error-swallowing. Operational failures → `system_errors` (FATAL/ERROR/WARN); data failures → `rejections`/`pull_failures` per spec.
- **Determinism:** seed any randomness (bootstrap CI); no wall-clock or network in business logic.
- **No magic:** statuses/codes in `constants.py`; settings in `config.yaml`; no hardcoded thresholds, books, codes, or paths.
- **Traceability:** every module header cites its ticket(s) + spec section; public functions carry docstrings stating the contract (inputs, outputs, failure mode).
- Short, single-responsibility functions; explicit return types / dataclasses; no hidden globals.
- **Concurrency** (P2a three-process design): every connection sets `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, `PRAGMA foreign_keys=ON`; transactions stay short; retry on `SQLITE_BUSY`.

-----

## 8. Testing standards (high test-ability)

- **Test-first:** write the test (with the spec’s exact expected values) before the implementation, every ticket.
- **Coverage:** **100% line + branch** on the deterministic/critical core (formulas, opportunity_key, gates, clv, dedup/upsert, confirm logic, integrity reconciliations); **≥ 90% overall**. Coverage is a floor, not a goal.
- **Property-based tests (Hypothesis)** for the math: de-vig probabilities sum to 1; american↔decimal round-trip; edge monotonic in soft price; `opportunity_key` stable under identical inputs and distinct under any field change; `clv` sign matches `beat_close`.
- **Behavior/integration:** fixture-driven dry-run end-state (exact `raw / unique / duplicate / confirmed / transient / graded` counts and one correct CLV); replay chronological-order and **no-lookahead** tests.
- **Integrity tests (must pass):** no-silent-drop reconciliation (per-pull `evaluated == observations + rejections`; per-cycle exactly one success or failure); `successful_cycles == successful_pulls`; `resolved ↔ success` / `unresolved ↔ failed`; one CLV per graded unique candidate.
- **No network in tests:** `dry_run` + fixtures only; a real API call in a unit test is a failure.
- **CI + pre-commit gate:** `ruff` + `mypy --strict` + `pytest` + coverage threshold must all pass; nothing merges otherwise.
- **Recommended (stretch):** mutation testing (`mutmut`) on the core modules — a surviving mutant means the tests don’t actually catch the bug; treat it as a coverage gap to close.

-----

## 9. Definition of Done (per ticket)

- Spec acceptance criteria met; all failure conditions handled fail-closed.
- Tests written first, all green; coverage thresholds met; property tests where applicable.
- `mypy --strict` + `ruff` clean.
- Diff reviewed and **approved by the human**, with extra scrutiny on data-quality modules (gates, confirm, closing, dedup).
- No frozen decision altered; no invariant relaxed; no scope added.

-----

## 10. Commands (suggested `make` targets)

```
make lint    # ruff check + ruff format --check
make type    # mypy --strict src
make test    # pytest -q
make cov     # pytest --cov --cov-branch --cov-fail-under=90
make check   # lint + type + cov   (the per-ticket gate)
make dryrun  # python scripts/run_dryrun.py --config config.yaml
```

**Reminder:** the first deliverable is not a bet. It is a harness proven to ingest, replay, de-duplicate, confirm, grade, and report **without lying to you.**