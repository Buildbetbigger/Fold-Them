"""Deterministic odds math — the pure oracle. Claude produces NONE of these numbers.

Ticket: T5 (spec/00_build_plan.md §2, §5; base spec §5, §10).

Pure functions: no IO, no clock, no randomness, no DB (CLAUDE.md §7). Invalid odds raise
:class:`InvalidOddsError` (fail-closed) — never return a fudged value.

Sign/domain conventions (base §5):
  - decimal odds are > 1.0; implied/no-vig probabilities are in (0, 1).
  - ``edge_pct`` / ``clv_pct`` are percentages: ``(value - 1) * 100``.
"""

from __future__ import annotations

_MIN_AMERICAN_MAGNITUDE = 100
_MIN_DECIMAL = 1.0


class InvalidOddsError(ValueError):
    """Odds outside their valid domain (American |a| < 100, or decimal <= 1.0)."""


def american_to_decimal(american: int) -> float:
    """Convert American odds to decimal. Raises if ``|american| < 100`` (base §5)."""
    if abs(american) < _MIN_AMERICAN_MAGNITUDE:
        raise InvalidOddsError(f"american odds must have |a| >= 100, got {american}")
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def decimal_to_implied(decimal_odds: float) -> float:
    """Implied probability of a decimal price: ``1 / d``. Raises if ``d <= 1.0``."""
    if decimal_odds <= _MIN_DECIMAL:
        raise InvalidOddsError(f"decimal odds must be > 1.0, got {decimal_odds}")
    return 1.0 / decimal_odds


def devig_two_way(d1: float, d2: float) -> tuple[float, float]:
    """De-vig a two-way market into fair no-vig probabilities ``(p1, p2)`` summing to 1.

    Raises if either side is ``<= 1.0``.
    """
    if d1 <= _MIN_DECIMAL or d2 <= _MIN_DECIMAL:
        raise InvalidOddsError(f"both decimals must be > 1.0, got ({d1}, {d2})")
    q1 = 1.0 / d1
    q2 = 1.0 / d2
    overround = q1 + q2  # > 1.0
    return q1 / overround, q2 / overround


def edge_pct(p_fair: float, d_soft: float) -> float:
    """Edge percent of a soft price vs the sharp fair probability: ``(p_fair*d_soft - 1)*100``.

    A candidate requires ``edge_pct >= edge_threshold_pct``. Pure arithmetic — price-domain
    validation is the gate's job (PRICE_SANITY), not here.

    Precondition (validated upstream, not asserted here per base §5): ``p_fair in (0, 1)``
    (from :func:`devig_two_way`) and ``d_soft > 1`` (PRICE_SANITY at T10).
    """
    return (p_fair * d_soft - 1.0) * 100.0


def closing_novig(d_sel: float, d_opp: float) -> float:
    """No-vig probability of the selection side at close.

    Delegates to :func:`devig_two_way` so there is exactly ONE de-vig implementation: a
    future switch to Shin/log de-vig (a v0.1 "later refinement") then changes one place.
    Raises if either side is ``<= 1.0``.
    """
    return devig_two_way(d_sel, d_opp)[0]


def clv_pct(d_taken: float, p_close: float) -> float:
    """Closing Line Value percent: ``(d_taken * p_close - 1) * 100``.

    Precondition (validated upstream, not asserted here): ``d_taken > 1`` and
    ``p_close in (0, 1)`` (from :func:`closing_novig`).
    """
    return (d_taken * p_close - 1.0) * 100.0


def beat_close(clv_value: float) -> bool:
    """True iff CLV is strictly positive (the bet beat the close)."""
    return clv_value > 0.0


__all__ = [
    "InvalidOddsError",
    "american_to_decimal",
    "beat_close",
    "closing_novig",
    "clv_pct",
    "decimal_to_implied",
    "devig_two_way",
    "edge_pct",
]
