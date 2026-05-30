# IA-1 — Implementation Invariants Addendum

**Applies to the full controlling stack:** v0.1 + P1 + P2 + P2a + P3 + P3a.
**Status:** Implementation rules ruled final in review but not yet written into a patch file. These are boot checks, a serialization rule, and a reporting rule — **not** conceptual changes. Apply during implementation; they do not alter any locked invariant.

Notation: `interval_s = historical_snapshot_interval_minutes × 60`; `close_window_s = close_capture_window_minutes × 60`.

-----

## IA-1.1 Boot-time configuration invariants

Enforced in `config_loader` at startup (HISTORICAL mode). Refuse to start — abort with a clear message and a `system_errors` FATAL — if either fails. (LIVE mode is unaffected; these are historical-cadence concerns.)

```python
assert interval_s <= max_historical_gap_seconds, \
    "interval > max_historical_gap_seconds: subsampling would manufacture false coverage gaps"   # P3a §2
assert interval_s <= close_window_s, \
    "interval > close_capture_window: cadence would manufacture CLOSE_MISSING even on a clean archive"
# RECOMMENDED (boundary safety): close_window_s >= 1.5 * interval_s
```

Why **B** needs a margin: `interval_s ≤ close_window_s` only guarantees a snapshot in the close window *in expectation*. Archive snapshots are not aligned to `commence_time`, so exact-boundary games can still miss with equality. Setting `close_window_s ≥ 1.5 × interval_s` removes the boundary risk.

**Test:** each violation (and the equality boundary case for B) refuses to start.

-----

## IA-1.2 Canonical request-signature hash

`request_signature_hash` (the historical cache key, P3a §4) must be derived from a **canonically serialized object with sorted keys**, never a loose concatenated string.

```python
import json, hashlib
norm_books = sorted({b.strip().lower() for b in bookmaker_set})   # normalize -> dedupe -> sort

canonical = json.dumps({
    "sport_key":        sport_key,
    "market_key":       market_key,
    "region":           region,
    "bookmakers":       norm_books,
    "odds_format":      odds_format,
    "endpoint_version": endpoint_version,
    "mode":             mode,                 # 'HISTORICAL'
}, sort_keys=True, separators=(",", ":"))     # separators strip whitespace drift

request_signature_hash = hashlib.sha256(canonical.encode()).hexdigest()
bookmaker_set_hash      = hashlib.sha256(",".join(norm_books).encode()).hexdigest()   # diagnostic only
```

Rules:

- **Normalize bookmakers (lowercase, trim, dedupe) before sorting** — else `["DK","FD"]`, `["fd","dk"]`, `["FD","DK","FD"]` hash differently for the same request.
- **The timestamp is NOT in the signature.** The snapshot is identified by `(request_signature_hash, historical_snapshot_ts)`; the timestamp is the *second* column of the unique index, not part of the “what data” identity.
- `mode` in the signature keeps LIVE and HISTORICAL from ever sharing a cache key; `endpoint_version` prevents a provider API bump from serving stale-shaped cache.
- Unique index unchanged: `CREATE UNIQUE INDEX ux_hist_snapshot ON raw_api_pulls(request_signature_hash, historical_snapshot_ts) WHERE mode='HISTORICAL';`

**Test:** identical request under any book ordering/case → identical hash; different book set or market → different hash; changing `mode` or `endpoint_version` → different hash; timestamp absent from the hash; v0.1 fixed config → constant hash (one row per `snapshot_ts`).

-----

## IA-1.3 Confirm-gap honesty (historical report)

The configured confirm delay (e.g., 60 s) is **not** the historical confirm reality at a 15-min cadence — the confirm is a ~one-interval persistence check. The historical report must surface this, not average it away.

- Record the **actual** confirm gap per candidate: `confirm_gap_seconds = confirm_snapshot_ts − detection_snapshot_ts` (available from the `CONFIRM`-phase observation timestamps / `snapshot_sequence_num`).
- Historical report (`report.py`, additive to P3 §11 / P3a §7) must include: `median_confirm_gap_seconds`, `max_confirm_gap_seconds`, and **CLV bucketed by confirm gap** (`≤60s / >60s–5m / >5m–15m / >15m`).
- **Interpretation rule (state it in the report):** historical confirm is a coarse proxy; the live 45 s confirm is the real capturability filter. A candidate “confirmed” across a 15-minute gap is materially weaker evidence than one confirmed across 60 s. Do not label a large-gap historical confirm as a 60-second capturability test.

**Test:** report shows the gap distribution and CLV-by-confirm-gap buckets; a candidate confirmed across a large gap is visibly attributed to its bucket (not folded into a flattering mean).

-----

## Where each attaches

|Invariant                   |Module                                              |Stack reference               |
|----------------------------|----------------------------------------------------|------------------------------|
|IA-1.1 boot checks          |`config_loader`                                     |P3a §2; new close-window check|
|IA-1.2 canonical hash       |`historical_ingest` (cache key), `db` (unique index)|P3a §4                        |
|IA-1.3 confirm-gap reporting|`report.py`                                         |P3 §11, P3a §7                |

With IA-1 folded in, the stack is implementation-complete: nothing essential lives only in chat.