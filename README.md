# Market-Translation System v0.1 — Documentation Bundle

The complete, final specification stack to hand to **Claude Code** for implementation. The system is a **moneyline-only, soft-vs-sharp divergence measurement harness** that validates Closing Line Value (CLV) over historical and then live data. It is **not** a betting bot — it never places, sizes, or decides bets.

## Repo placement

```
<your-repo>/
├── CLAUDE.md                 # <- this bundle's CLAUDE.md, at the REPO ROOT (build brief)
└── spec/                     # <- this bundle's /spec folder
    ├── 00_build_plan.md
    ├── 01_v0.1_base_spec.md
    ├── 02_P1_dedup.md
    ├── 03_P2_pull_failures_clock_skew.md
    ├── 04_P2a_attempt_vs_cycle.md
    ├── 05_P3_historical_backtest.md
    ├── 06_P3a_gap_cachekey.md
    ├── 07_IA1_implementation_invariants.md
    └── 08_implementation_errata.md
```

Claude Code then generates `src/`, `tests/`, `migrations/`, `scripts/`, `fixtures/` per the build plan.

## Documents (read in order; later patches override earlier on conflict)

|File                                   |Defines                                                                                                                                                   |
|---------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------|
|**CLAUDE.md**                          |Authoritative build brief: prime directives, frozen decisions, IA-1 invariants, provider-schema rule, workflow, and the highest coding + testing standards|
|**00_build_plan.md**                   |File/module structure, build tickets T1–T26, migration order, config, function signatures, full test suite, dry-run + live procedures                     |
|**01_v0.1_base_spec.md**               |Base system: scope lock, sharp-source rule, data-quality gates, edge/CLV formulas, SQLite schema, candidate/rejection/close/CLV records                   |
|**02_P1_dedup.md**                     |Candidate de-duplication / sample independence: `opportunity_key`, `candidate_observations`, decoupled confirm lifecycle                                  |
|**03_P2_pull_failures_clock_skew.md**  |Pull-level failure accounting (`pull_failures`), three-way failure separation, clock-skew tolerance                                                       |
|**04_P2a_attempt_vs_cycle.md**         |`pull_cycles`; attempt-vs-usable-cycle reporting; self-checking integrity invariants                                                                      |
|**05_P3_historical_backtest.md**       |Historical backtest mode: walk-forward replay, no lookahead, ingestion, schema, pass/fail, cost control                                                   |
|**06_P3a_gap_cachekey.md**             |Historical gap accounting (request alignment vs inter-snapshot coverage) + request-signature cache key                                                    |
|**07_IA1_implementation_invariants.md**|Final boot/serialization/reporting invariants                                                                                                             |
|**08_implementation_errata.md**        |Pre-build errata resolving handoff ambiguities (E1–E4); **highest precedence**                                                                            |

**Precedence on any conflict:** `08-errata > IA-1 > P3a > P3 > P2a > P2 > P1 > base spec`. `00_build_plan` is procedural; `01–08` are authoritative on behavior.

## How to drive Claude Code

1. Place the files as above; open the repo in Claude Code.
1. It reads `CLAUDE.md` and treats `/spec` as authoritative.
1. Implement **one ticket at a time** in build order, starting with `formulas.py` (T5) — a pure, deterministic oracle.
1. Gate every ticket behind the full check (lint + type + tests + coverage) **and** your diff review; do not advance until approved.
1. **Before T7/T8** (provider integration), give it one real, scrubbed odds-API payload **and** the provider’s API docs. It must not invent the schema.
1. Build order across the project: **H1 historical first** (offline, cheap, full chain) → live 3–5 day audit → optional read-only viewer.

## Non-negotiables (full detail in CLAUDE.md)

No redesign; no added scope (no agents/weather/props/spreads/totals/sizing/human-approval/dashboard/real-money/LLM-pricing). Fail-closed — no silent drops, no imputation. The log is the product. Claude never prices, sizes, or decides. Walk-forward only; repeated sightings cannot inflate the sample; CLV only on graded unique candidates.

> The first deliverable is not a bet. It is a harness proven to ingest, replay, de-duplicate, confirm, grade, and report without lying to you.