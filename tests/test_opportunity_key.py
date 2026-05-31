"""T6 tests: the opportunity_key composite (build-plan §6; P1 §1).

Exact-format + stability/distinctness, with the granularity guard the review flagged:
.4f rounds, so equal-after-rounding prices MUST map to the same key (2.05 == 2.0500 ==
2.05004), and the Hypothesis "distinct under any field change" property must compare on
the rounded value, never raw floats.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from src.opportunity_key import build_opportunity_key

BASE_ARGS: dict[str, object] = {
    "audit_run_id": 7,
    "event_id": "E1",
    "market_key": "h2h",
    "selection_canonical_id": "nyy",
    "soft_book": "draftkings",
    "sharp_book": "pinnacle",
    "soft_decimal": 2.05,
    "threshold_used": 2.0,
}


def _key(**overrides: object) -> str:
    args = {**BASE_ARGS, **overrides}
    return build_opportunity_key(**args)  # type: ignore[arg-type]  # homogeneous test kwargs


# --- exact format -------------------------------------------------------------------


def test_exact_composite_format() -> None:
    assert _key() == "7|E1|h2h|nyy|draftkings|pinnacle|2.0500|2.0000"


def test_prices_formatted_to_four_dp() -> None:
    assert _key(soft_decimal=1.9, threshold_used=2.5).endswith("|1.9000|2.5000")


# --- stability + price granularity --------------------------------------------------


def test_identical_inputs_identical_key() -> None:
    assert _key() == _key()


def test_soft_decimal_equal_after_rounding_is_same_key() -> None:
    assert _key(soft_decimal=2.05) == _key(soft_decimal=2.0500)
    assert _key(soft_decimal=2.05) == _key(soft_decimal=2.05004)  # sub-1e-4 collapses


def test_soft_decimal_round_distinct_is_different_key() -> None:
    assert _key(soft_decimal=2.05) != _key(soft_decimal=2.06)


# --- distinctness under any field change --------------------------------------------


def test_each_string_field_change_yields_distinct_key() -> None:
    for field, other in [
        ("event_id", "E2"),
        ("market_key", "spreads"),
        ("selection_canonical_id", "bos"),
        ("soft_book", "fanduel"),
        ("sharp_book", "circasports"),
    ]:
        assert _key(**{field: other}) != _key()


def test_audit_run_and_threshold_change_yields_distinct_key() -> None:
    assert _key(audit_run_id=8) != _key()
    assert _key(threshold_used=2.5) != _key()


# --- bounded property tests (respecting .4f granularity) ----------------------------

PRICES = st.floats(min_value=1.01, max_value=51.0, allow_nan=False, allow_infinity=False)


@given(PRICES, PRICES)
def test_key_depends_only_on_rounded_prices(soft_a: float, soft_b: float) -> None:
    """Two soft prices give the same key iff they are equal at 4dp, and distinct otherwise.

    The property compares on the rounded value (.4f), so tiny float perturbations below
    the key's granularity do not false-fail "distinct under any field change".
    """
    key_a = _key(soft_decimal=soft_a)
    key_b = _key(soft_decimal=soft_b)
    same_at_4dp = f"{soft_a:.4f}" == f"{soft_b:.4f}"
    assert (key_a == key_b) == same_at_4dp


@given(st.integers(min_value=1, max_value=10_000))
def test_key_is_raw_pipe_composite_not_hashed(audit_run_id: int) -> None:
    """The key stays human-readable: 7 pipe separators, plain field values, no hex digest."""
    key = _key(audit_run_id=audit_run_id)
    assert key.count("|") == 7
    assert key.startswith(f"{audit_run_id}|E1|h2h|nyy|")
