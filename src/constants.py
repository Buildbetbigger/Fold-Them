"""Single source of truth for lifecycle statuses, rejection codes, transient reasons,
and observation phases.

Ticket: T1 (spec/00_build_plan.md §2).
Spec:
  - 7-state lifecycle .......... spec/02_P1_dedup.md §3
  - rejection codes ............ spec/01_v0.1_base_spec.md §6 (gates 2..14)
  - TWO_SIDED_EDGE ............. spec/02_P1_dedup.md §1, §4c
  - TRANSIENT reasons .......... spec/02_P1_dedup.md §4c
  - observation phase .......... spec/02_P1_dedup.md §2
Errata (highest precedence): spec/08_implementation_errata.md E4 —
  ``API_FAIL`` is a PULL-FAILURE code, NOT a rejection code. It is therefore
  intentionally absent from :class:`RejectionCode` (the gate chain begins at
  ``EVENT_FIELDS_MISSING``; see spec/03_P2_pull_failures_clock_skew.md §0).

Contract (T1 acceptance / failure mode): every status, code, reason, and phase used
anywhere in the system is defined *here* and nowhere else. Defining one outside this
module is a T1 acceptance failure ("no magic", CLAUDE.md §7).

Scope note (CLAUDE.md §6 / errata E3 ticket order): this module covers the v0.1 + P1
constants that T1 owns. Codes introduced by later tickets are added when those tickets
are built, to avoid implementing ahead of the ticket:
  - ``PULL_FAILURE_CODE`` (incl. ``API_FAIL``) and clock-skew system codes -> T7 (P2).
  - run-status / system-severity enums -> T3/T4 (they back ``audit_runs`` / ``system_errors``).
  - historical TRANSIENT reasons (``CONFIRM_GAP_TOO_LARGE``, ``CONFIRM_NO_SNAPSHOT``),
    ``mode`` and ``coverage_gap`` constants -> T20 (P3 mode plumbing).
"""

from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    """The 7-state candidate lifecycle (spec/02_P1_dedup.md §3).

    Transitions (P1 §3) — enforced by the repository's transition guard (T4), not here:
      DETECTED -> PENDING_CONFIRM -> CONFIRMED -> PENDING_GRADE -> GRADED
      PENDING_CONFIRM -> TRANSIENT
      PENDING_GRADE -> UNGRADED
      {DETECTED|PENDING_CONFIRM} -> TRANSIENT (event reached commence; CONFIRM_EXPIRED)
    """

    DETECTED = "DETECTED"
    PENDING_CONFIRM = "PENDING_CONFIRM"
    CONFIRMED = "CONFIRMED"
    TRANSIENT = "TRANSIENT"
    PENDING_GRADE = "PENDING_GRADE"
    GRADED = "GRADED"
    UNGRADED = "UNGRADED"


class RejectionCode(StrEnum):
    """Opportunity-level rejection codes (spec/01_v0.1_base_spec.md §6 gates 2..14,
    plus ``TWO_SIDED_EDGE`` from spec/02_P1_dedup.md §1).

    A rejection is *data*, not an error (base §6): only true exceptions go to
    ``system_errors``, and failed API pulls go to ``pull_failures`` (P2 §0).

    ``API_FAIL`` is deliberately NOT a member — see the module docstring (errata E4).
    """

    EVENT_FIELDS_MISSING = "EVENT_FIELDS_MISSING"  # gate 2
    NO_SHARP = "NO_SHARP"  # gate 3
    SHARP_DISAGREE = "SHARP_DISAGREE"  # gate 3b
    STALE_SHARP = "STALE_SHARP"  # gate 4
    STALE_SOFT = "STALE_SOFT"  # gate 5
    NOT_TWO_WAY = "NOT_TWO_WAY"  # gate 6
    MARKET_MISMATCH = "MARKET_MISMATCH"  # gate 7
    NAME_NORM_FAIL = "NAME_NORM_FAIL"  # gate 8
    PRICE_MISSING = "PRICE_MISSING"  # gate 9
    DUP_OUTCOME = "DUP_OUTCOME"  # gate 10
    PRICE_SANITY = "PRICE_SANITY"  # gate 11
    BELOW_THRESHOLD = "BELOW_THRESHOLD"  # gate 12
    TRANSIENT = "TRANSIENT"  # gate 13 (reason recorded in trigger_values)
    CLOSE_MISSING = "CLOSE_MISSING"  # gate 14
    TWO_SIDED_EDGE = "TWO_SIDED_EDGE"  # P1: both sides cross -> reject both, no candidate


class TransientReason(StrEnum):
    """Reason recorded inside a ``TRANSIENT`` rejection's ``trigger_values``
    (spec/02_P1_dedup.md §4c).

    Historical-mode reasons (``CONFIRM_GAP_TOO_LARGE``, ``CONFIRM_NO_SNAPSHOT``) are
    added at T20, per errata E3 ticket order.
    """

    VANISHED = "VANISHED"
    WENT_STALE = "WENT_STALE"
    REPULL_ERROR = "REPULL_ERROR"
    OFF_KEY_PRICE = "OFF_KEY_PRICE"
    CONFIRM_EXPIRED = "CONFIRM_EXPIRED"


class Phase(StrEnum):
    """Which loop observed a sighting (spec/02_P1_dedup.md §2,
    ``candidate_observations.phase``)."""

    DETECTION = "DETECTION"
    CONFIRM = "CONFIRM"


__all__ = ["Phase", "RejectionCode", "Status", "TransientReason"]
