"""The opportunity_key — the dedup identity of a priced opportunity.

Ticket: T6 (spec/00_build_plan.md §2, §5). Definition: P1 §1 (frozen, CLAUDE.md §3).

The key is the raw, human-readable pipe-composite stored verbatim under the
``candidates.opportunity_key`` UNIQUE constraint — deliberately NOT hashed (it must be
greppable in the DB and is the structural dedup guarantee). This is distinct from
``canonical_json``/``sha256`` (T2), which is only the T20 historical request signature.

Format (P1 §1, exact):
    audit_run_id|event_id|market_key|selection_canonical_id|soft_book|sharp_book|
    soft_decimal:.4f|threshold_used:.4f

In the key:  soft_decimal (a soft-price change mints a new candidate).
Out of the key: sharp no-vig prob (a sharp move is an observation, not a new candidate).
The ``.4f`` formatting makes float equality deterministic (2.05 == 2.0500), and by design
collapses sub-0.0001 differences (2.05 == 2.05004).
"""

from __future__ import annotations

_PRICE_FMT = ".4f"


def build_opportunity_key(
    audit_run_id: int,
    event_id: str,
    market_key: str,
    selection_canonical_id: str,
    soft_book: str,
    sharp_book: str,
    soft_decimal: float,
    threshold_used: float,
) -> str:
    """Build the P1 §1 composite opportunity_key. Deterministic in all inputs; the two
    prices are formatted to 4 decimal places."""
    return (
        f"{audit_run_id}|{event_id}|{market_key}|{selection_canonical_id}"
        f"|{soft_book}|{sharp_book}|{soft_decimal:{_PRICE_FMT}}|{threshold_used:{_PRICE_FMT}}"
    )


__all__ = ["build_opportunity_key"]
