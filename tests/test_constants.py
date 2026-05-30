"""T1 acceptance tests: the constants enums are importable and their members match the
spec exactly (spec/00_build_plan.md §2 T1).

The most important assertions here are the errata regression guards:
  - E4: ``API_FAIL`` must NOT be a rejection code (it is a pull-failure code).
  - E5: ``CLOSE_MISSING`` must NOT be a rejection code (it is a grade outcome).
If a future edit reintroduces either as a rejection, this suite fails loudly.
"""

from __future__ import annotations

from src.constants import Phase, RejectionCode, Status, TransientReason

# Expected member sets, written out verbatim from the spec (not derived from the code
# under test) so the test is an independent oracle.
EXPECTED_STATUSES = {
    "DETECTED",
    "PENDING_CONFIRM",
    "CONFIRMED",
    "TRANSIENT",
    "PENDING_GRADE",
    "GRADED",
    "UNGRADED",
}

EXPECTED_REJECTION_CODES = {
    "EVENT_FIELDS_MISSING",
    "NO_SHARP",
    "SHARP_DISAGREE",
    "STALE_SHARP",
    "STALE_SOFT",
    "NOT_TWO_WAY",
    "MARKET_MISMATCH",
    "NAME_NORM_FAIL",
    "PRICE_MISSING",
    "DUP_OUTCOME",
    "PRICE_SANITY",
    "BELOW_THRESHOLD",
    "TRANSIENT",
    "TWO_SIDED_EDGE",
}

EXPECTED_TRANSIENT_REASONS = {
    "VANISHED",
    "WENT_STALE",
    "REPULL_ERROR",
    "OFF_KEY_PRICE",
    "CONFIRM_EXPIRED",
}

EXPECTED_PHASES = {"DETECTION", "CONFIRM"}


def test_status_members_exact() -> None:
    """The lifecycle has exactly the 7 P1 states — no more, no fewer."""
    assert {s.value for s in Status} == EXPECTED_STATUSES
    assert len(Status) == len(EXPECTED_STATUSES)  # also guards against alias members


def test_rejection_code_members_exact() -> None:
    """Rejection codes match base §6 opportunity gates plus P1 TWO_SIDED_EDGE, minus the
    two superseded entries API_FAIL (E4) and CLOSE_MISSING (E5)."""
    assert {c.value for c in RejectionCode} == EXPECTED_REJECTION_CODES
    assert len(RejectionCode) == len(EXPECTED_REJECTION_CODES)


def test_api_fail_is_not_a_rejection_code() -> None:
    """Errata E4 / P2 §0: API_FAIL is a pull-failure code, never a rejection code."""
    assert "API_FAIL" not in {c.value for c in RejectionCode}
    assert not hasattr(RejectionCode, "API_FAIL")


def test_close_missing_out_but_transient_stays_in_rejection_codes() -> None:
    """Errata E5, including the explicit over-correction guard.

    CLOSE_MISSING moves out (a CONFIRMED candidate that *passed* confirm but can't be
    graded is a grade outcome, not a rejection). But TRANSIENT *stays* — a confirm
    failure is a genuine rejected opportunity, dual-tracked as status=TRANSIENT plus a
    TRANSIENT rejection. A test that only checked the first could let TRANSIENT be
    dropped by accident."""
    codes = {c.value for c in RejectionCode}
    assert "CLOSE_MISSING" not in codes
    assert not hasattr(RejectionCode, "CLOSE_MISSING")
    assert "TRANSIENT" in codes  # must NOT be swept out with CLOSE_MISSING
    assert hasattr(RejectionCode, "TRANSIENT")


def test_transient_reason_members_exact() -> None:
    """T1 owns the v0.1 + P1 transient reasons; historical reasons arrive at T20."""
    assert {r.value for r in TransientReason} == EXPECTED_TRANSIENT_REASONS
    assert len(TransientReason) == len(EXPECTED_TRANSIENT_REASONS)
    # Historical-mode reasons must not be present yet (errata E3 ticket order).
    assert "CONFIRM_GAP_TOO_LARGE" not in {r.value for r in TransientReason}
    assert "CONFIRM_NO_SNAPSHOT" not in {r.value for r in TransientReason}


def test_phase_members_exact() -> None:
    assert {p.value for p in Phase} == EXPECTED_PHASES
    assert len(Phase) == len(EXPECTED_PHASES)


def test_transient_appears_as_both_status_and_rejection_code() -> None:
    """TRANSIENT is a terminal lifecycle state (P1 §3) and the gate-13 rejection code
    (base §6). Both representations must exist and share the same string value."""
    assert Status.TRANSIENT.value == RejectionCode.TRANSIENT.value == "TRANSIENT"


def test_enum_values_are_strings_equal_to_their_names() -> None:
    """StrEnum members compare equal to their string value (used directly in SQLite
    CHECK constraints and JSON), and value == name for every member."""
    for enum_cls in (Status, RejectionCode, TransientReason, Phase):
        for member in enum_cls:
            assert isinstance(member, str)
            assert member.value == member.name
            assert member == member.value
