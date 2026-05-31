"""T5: the pure deterministic oracle (src/formulas.py).

Three layers: exact spec values (base §5/§10, build-plan §6), fail-closed validation
raises, and bounded property tests (Hypothesis). Property domains are clamped to the sane
odds range (decimal in [1.01, 51], American |a| >= 100) so degenerate floats can't
false-fail invariants like "de-vig sums to 1".
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.formulas import (
    InvalidOddsError,
    american_to_decimal,
    beat_close,
    closing_novig,
    clv_pct,
    decimal_to_implied,
    devig_two_way,
    edge_pct,
)

# --- exact values -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("american", "expected"),
    [(150, 2.5), (-200, 1.5), (100, 2.0), (250, 3.5)],
)
def test_american_to_decimal_exact(american: int, expected: float) -> None:
    assert american_to_decimal(american) == pytest.approx(expected)


def test_american_to_decimal_minus_110() -> None:
    assert american_to_decimal(-110) == pytest.approx(1.9090909090909092)


@pytest.mark.parametrize("bad", [0, 50, -50, 99, -99])
def test_american_to_decimal_rejects_small_magnitude(bad: int) -> None:
    with pytest.raises(InvalidOddsError):
        american_to_decimal(bad)


@pytest.mark.parametrize(
    ("decimal_odds", "expected"),
    [(2.0, 0.5), (1.5, 2.0 / 3.0), (4.0, 0.25)],
)
def test_decimal_to_implied_exact(decimal_odds: float, expected: float) -> None:
    assert decimal_to_implied(decimal_odds) == pytest.approx(expected)


@pytest.mark.parametrize("bad", [1.0, 0.9])
def test_decimal_to_implied_rejects_non_positive_vig(bad: float) -> None:
    with pytest.raises(InvalidOddsError):
        decimal_to_implied(bad)


def test_devig_even_market() -> None:
    assert devig_two_way(1.90909, 1.90909) == pytest.approx((0.5, 0.5), abs=1e-4)


def test_devig_known_market() -> None:
    p1, p2 = devig_two_way(1.66667, 2.30)
    assert p1 == pytest.approx(0.5798, abs=1e-3)
    assert p2 == pytest.approx(0.4202, abs=1e-3)
    assert p1 + p2 == pytest.approx(1.0)


@pytest.mark.parametrize("d1, d2", [(1.0, 2.0), (2.0, 1.0), (0.9, 0.9)])
def test_devig_rejects_non_positive_vig(d1: float, d2: float) -> None:
    with pytest.raises(InvalidOddsError):
        devig_two_way(d1, d2)


@pytest.mark.parametrize(
    ("p_fair", "d_soft", "expected"),
    [(0.55, 2.0, 10.0), (0.50, 1.95, -2.5)],
)
def test_edge_pct_exact(p_fair: float, d_soft: float, expected: float) -> None:
    assert edge_pct(p_fair, d_soft) == pytest.approx(expected)


def test_closing_novig_even_and_known() -> None:
    assert closing_novig(2.0, 2.0) == pytest.approx(0.5)
    assert closing_novig(1.90909, 1.90909) == pytest.approx(0.5, abs=1e-4)


@pytest.mark.parametrize("d_sel, d_opp", [(1.0, 2.0), (2.0, 1.0)])
def test_closing_novig_rejects_non_positive_vig(d_sel: float, d_opp: float) -> None:
    with pytest.raises(InvalidOddsError):
        closing_novig(d_sel, d_opp)


def test_clv_pct_and_beat_close_positive() -> None:
    value = clv_pct(2.5, 0.4545)
    assert value == pytest.approx(13.625)  # spec's "~+13.6"
    assert beat_close(value) is True


def test_clv_pct_and_beat_close_negative() -> None:
    value = clv_pct(1.9, 0.5)
    assert value == pytest.approx(-5.0)
    assert beat_close(value) is False


def test_beat_close_zero_is_false() -> None:
    assert beat_close(0.0) is False


# --- bounded property tests ---------------------------------------------------------

DECIMALS = st.floats(min_value=1.01, max_value=51.0, allow_nan=False, allow_infinity=False)
PROBS = st.floats(min_value=0.01, max_value=0.99, allow_nan=False, allow_infinity=False)
AMERICANS = st.integers(min_value=100, max_value=10_000) | st.integers(
    min_value=-10_000, max_value=-100
)


@given(DECIMALS, DECIMALS)
def test_devig_sums_to_one_and_in_unit_interval(d1: float, d2: float) -> None:
    p1, p2 = devig_two_way(d1, d2)
    assert math.isclose(p1 + p2, 1.0)
    assert 0.0 < p1 < 1.0
    assert 0.0 < p2 < 1.0


@given(PROBS, DECIMALS, DECIMALS)
def test_edge_monotonic_in_soft_price(p_fair: float, da: float, db: float) -> None:
    lo, hi = sorted((da, db))
    assert edge_pct(p_fair, hi) >= edge_pct(p_fair, lo)


@given(AMERICANS)
def test_american_decimal_implied_consistency(american: int) -> None:
    """american -> decimal -> implied prob matches the direct American implied prob."""
    implied = decimal_to_implied(american_to_decimal(american))
    if american > 0:
        expected = 100.0 / (american + 100.0)
    else:
        expected = abs(american) / (abs(american) + 100.0)
    assert math.isclose(implied, expected, rel_tol=1e-9)


@given(DECIMALS, PROBS)
def test_clv_sign_matches_beat_close(d_taken: float, p_close: float) -> None:
    value = clv_pct(d_taken, p_close)
    assert beat_close(value) == (value > 0.0)
