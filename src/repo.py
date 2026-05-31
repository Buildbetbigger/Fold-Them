"""All database write paths, in one audited module, plus the status-transition guard.

Ticket: T4 (spec/00_build_plan.md §2, §5). Schema: T3 migrations.

Design:
  - Writers operate **within the caller's transaction and do NOT commit** — the detection,
    confirm, and closing jobs wrap a unit of work in one short transaction (P1 §7), so
    transaction boundaries belong to the caller. Connection-level ``busy_timeout`` (T3)
    handles SQLITE_BUSY; loop-level retry is T18.
  - ``status`` is mutated ONLY through :func:`transition_candidate_status` /
    :func:`finish_audit_run`, the single choke points, which validate the move against
    the P1 §3 lifecycle map before writing. No other writer touches ``status``.
  - ``upsert_candidate`` is atomic against the ``opportunity_key`` UNIQUE
    (``INSERT ... ON CONFLICT DO NOTHING`` + ``SELECT``), so concurrent detection writers
    cannot mint duplicate candidates (P1 sample-independence guarantee).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from src.canonical import canonical_json, sha256_hex
from src.constants import Phase, RejectionCode, RunStatus, Severity, Status


class IllegalTransitionError(Exception):
    """A status change not permitted by the lifecycle map was attempted."""


# --- lifecycle transition maps (authoritative; P1 §3 / base §3) ---------------------

ALLOWED_CANDIDATE_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.DETECTED: frozenset({Status.PENDING_CONFIRM, Status.TRANSIENT}),
    Status.PENDING_CONFIRM: frozenset({Status.CONFIRMED, Status.TRANSIENT}),
    Status.CONFIRMED: frozenset({Status.PENDING_GRADE}),
    Status.PENDING_GRADE: frozenset({Status.GRADED, Status.UNGRADED}),
    Status.TRANSIENT: frozenset(),
    Status.GRADED: frozenset(),
    Status.UNGRADED: frozenset(),
}

ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RUNNING: frozenset({RunStatus.COMPLETED, RunStatus.ABORTED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.ABORTED: frozenset(),
}


def assert_candidate_transition(current: Status, target: Status) -> None:
    """Raise :class:`IllegalTransitionError` unless ``current -> target`` is allowed."""
    if target not in ALLOWED_CANDIDATE_TRANSITIONS[current]:
        raise IllegalTransitionError(f"illegal candidate status transition {current} -> {target}")


def assert_run_transition(current: RunStatus, target: RunStatus) -> None:
    """Raise :class:`IllegalTransitionError` unless ``current -> target`` is allowed."""
    if target not in ALLOWED_RUN_TRANSITIONS[current]:
        raise IllegalTransitionError(f"illegal run status transition {current} -> {target}")


# --- transaction boundary -----------------------------------------------------------


@contextmanager
def unit_of_work(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a unit of work as one transaction: commit on success, roll back fully on any
    exception.

    Fail-closed: a mid-pull error leaves ZERO partial rows (P1 §7 one-transaction-per-
    pull). The three jobs wrap each pull's writers in this, so a partial commit cannot
    leak even if one job forgot a bare ``with conn:``.
    """
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --- grouped writer inputs (the column-heavy rows; §5) ------------------------------


@dataclass(frozen=True)
class CandidateFields:
    audit_run_id: int
    first_pull_id: int
    event_id: str
    sport_key: str
    commence_time: str
    market_key: str
    selection_raw: str
    selection_canonical_id: str
    sharp_book: str
    soft_book: str
    soft_decimal: float
    threshold_used: float
    first_seen_ts: str
    detect_sharp_decimal: float
    detect_sharp_opp_decimal: float
    detect_sharp_novig_prob: float
    detect_edge_pct: float
    confirm_due_ts: str | None = None
    confirms_required: int = 1


@dataclass(frozen=True)
class ObservationFields:
    observed_ts: str
    sharp_decimal: float
    sharp_opp_decimal: float
    sharp_novig_prob: float
    sharp_age_s: float
    soft_decimal: float
    soft_implied_prob: float
    soft_age_s: float
    edge_pct: float


@dataclass(frozen=True)
class RejectionContext:
    rejected_ts: str
    sport_key: str
    pull_id: int | None = None
    event_id: str | None = None
    commence_time: str | None = None
    market_key: str | None = None
    selection_raw: str | None = None
    sharp_book: str | None = None
    soft_book: str | None = None
    opportunity_key: str | None = None


@dataclass(frozen=True)
class ClosingFields:
    event_id: str
    sport_key: str
    commence_time: str
    sharp_book: str
    close_source_flag: str  # constants.CloseSourceFlag.value, supplied by closing.py (T13)
    close_pull_id: int | None = None
    close_capture_ts: str | None = None
    minutes_before_commence: float | None = None
    outcome_a_id: str | None = None
    outcome_a_decimal: float | None = None
    outcome_a_novig: float | None = None
    outcome_b_id: str | None = None
    outcome_b_decimal: float | None = None
    outcome_b_novig: float | None = None


@dataclass(frozen=True)
class ClvFields:
    candidate_id: int
    grade_status: str  # constants.GradeStatus.value, supplied by clv.py (T13/T14)
    close_id: int | None = None
    d_taken: float | None = None
    p_close_novig: float | None = None
    fair_close_decimal: float | None = None
    clv_pct: float | None = None
    beat_close: bool | None = None
    graded_ts: str | None = None


# --- small internal helpers ---------------------------------------------------------


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[object, ...], what: str) -> Any:
    """Return the first column of the single matching row, or raise if none. Returns Any
    because SQLite column types are dynamic; callers narrow (int(...), Status(...))."""
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise LookupError(f"{what} not found")
    return row[0]


# --- audit runs + errors ------------------------------------------------------------


def start_audit_run(
    conn: sqlite3.Connection,
    *,
    run_start_ts: str,
    config_hash: str,
    config_snapshot: str,
    run_label: str | None = None,
    code_version: str | None = None,
) -> int:
    """A run is always born RUNNING — a fixed birth state, never caller-supplied."""
    cur = conn.execute(
        "INSERT INTO audit_runs(run_label, run_start_ts, config_hash, config_snapshot, "
        "code_version, status) VALUES (?, ?, ?, ?, ?, ?)",
        (
            run_label,
            run_start_ts,
            config_hash,
            config_snapshot,
            code_version,
            RunStatus.RUNNING.value,
        ),
    )
    return int(cur.lastrowid or 0)


def finish_audit_run(
    conn: sqlite3.Connection, audit_run_id: int, *, run_end_ts: str, status: RunStatus
) -> None:
    current = RunStatus(
        _scalar(
            conn,
            "SELECT status FROM audit_runs WHERE audit_run_id = ?",
            (audit_run_id,),
            "audit_run",
        )
    )
    assert_run_transition(current, status)
    conn.execute(
        "UPDATE audit_runs SET run_end_ts = ?, status = ? WHERE audit_run_id = ?",
        (run_end_ts, status.value, audit_run_id),
    )


def log_error(
    conn: sqlite3.Connection,
    *,
    error_ts: str,
    component: str,
    severity: Severity,
    message: str,
    audit_run_id: int | None = None,
    context: Mapping[str, object] | None = None,
    stack_trace: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO system_errors(error_ts, audit_run_id, component, severity, context, "
        "message, stack_trace) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            error_ts,
            audit_run_id,
            component,
            severity.value,
            canonical_json(context) if context is not None else None,
            message,
            stack_trace,
        ),
    )
    return int(cur.lastrowid or 0)


# --- raw pulls / events / entities / snapshots / outcomes ---------------------------


def insert_raw_pull(
    conn: sqlite3.Connection,
    *,
    audit_run_id: int,
    pull_timestamp: str,
    endpoint: str,
    sport_key: str,
    market_key: str,
    region: str | None,
    http_status: int,
    payload: str,
) -> int:
    """Append-only raw row, raw-FIRST. ``payload_hash`` is sha256 of the raw payload."""
    cur = conn.execute(
        "INSERT INTO raw_api_pulls(audit_run_id, pull_timestamp, endpoint, sport_key, region, "
        "market_key, http_status, payload_hash, raw_payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            audit_run_id,
            pull_timestamp,
            endpoint,
            sport_key,
            region,
            market_key,
            http_status,
            sha256_hex(payload),
            payload,
        ),
    )
    return int(cur.lastrowid or 0)


def upsert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    sport_key: str,
    commence_time: str,
    home_team_raw: str,
    away_team_raw: str,
    first_seen_ts: str,
    last_seen_ts: str,
    home_entity_id: int | None = None,
    away_entity_id: int | None = None,
) -> None:
    """Insert the event or, if seen before, refresh ``last_seen_ts`` (base §7)."""
    conn.execute(
        "INSERT INTO events(event_id, sport_key, commence_time, home_team_raw, away_team_raw, "
        "home_entity_id, away_entity_id, first_seen_ts, last_seen_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(event_id) DO UPDATE SET last_seen_ts = excluded.last_seen_ts",
        (
            event_id,
            sport_key,
            commence_time,
            home_team_raw,
            away_team_raw,
            home_entity_id,
            away_entity_id,
            first_seen_ts,
            last_seen_ts,
        ),
    )


def upsert_entity(
    conn: sqlite3.Connection,
    *,
    sport_key: str,
    raw_name: str,
    canonical_name: str,
    canonical_id: str,
) -> int:
    """Insert the alias mapping if new (never overwrite); return its ``map_id``."""
    conn.execute(
        "INSERT INTO normalized_entities(sport_key, raw_name, canonical_name, canonical_id) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(sport_key, raw_name) DO NOTHING",
        (sport_key, raw_name, canonical_name, canonical_id),
    )
    return int(
        _scalar(
            conn,
            "SELECT map_id FROM normalized_entities WHERE sport_key = ? AND raw_name = ?",
            (sport_key, raw_name),
            "normalized_entity",
        )
    )


def insert_snapshot(
    conn: sqlite3.Connection,
    *,
    pull_id: int,
    event_id: str,
    bookmaker_key: str,
    market_key: str,
    api_last_update: str,
    pull_timestamp: str,
    bookmaker_title: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO bookmaker_snapshots(pull_id, event_id, bookmaker_key, bookmaker_title, "
        "market_key, api_last_update, pull_timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            pull_id,
            event_id,
            bookmaker_key,
            bookmaker_title,
            market_key,
            api_last_update,
            pull_timestamp,
        ),
    )
    return int(cur.lastrowid or 0)


def insert_outcome(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    event_id: str,
    bookmaker_key: str,
    market_key: str,
    outcome_name_raw: str,
    price_decimal: float,
    price_american: int | None = None,
    outcome_entity_id: int | None = None,
    point: float | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO market_outcomes(snapshot_id, event_id, bookmaker_key, market_key, "
        "outcome_name_raw, outcome_entity_id, price_american, price_decimal, point) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            event_id,
            bookmaker_key,
            market_key,
            outcome_name_raw,
            outcome_entity_id,
            price_american,
            price_decimal,
            point,
        ),
    )
    return int(cur.lastrowid or 0)


# --- candidates / observations / rejections -----------------------------------------


def upsert_candidate(
    conn: sqlite3.Connection, oppkey: str, fields: CandidateFields
) -> tuple[int, bool]:
    """Insert a new candidate (status DETECTED) or return the existing one for ``oppkey``.

    Atomic against the ``opportunity_key`` UNIQUE: ``ON CONFLICT DO NOTHING`` then read
    back the id. Returns ``(candidate_id, created)`` — ``created`` is True only for the
    writer that actually inserted, so a racing duplicate becomes an observation, never a
    second candidate.
    """
    cur = conn.execute(
        "INSERT INTO candidates(opportunity_key, audit_run_id, first_pull_id, event_id, "
        "sport_key, commence_time, market_key, selection_raw, selection_canonical_id, "
        "sharp_book, soft_book, soft_decimal, threshold_used, first_seen_ts, last_seen_ts, "
        "observation_count, detect_sharp_decimal, detect_sharp_opp_decimal, "
        "detect_sharp_novig_prob, detect_edge_pct, status, confirm_due_ts, confirms_required) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(opportunity_key) DO NOTHING",
        (
            oppkey,
            fields.audit_run_id,
            fields.first_pull_id,
            fields.event_id,
            fields.sport_key,
            fields.commence_time,
            fields.market_key,
            fields.selection_raw,
            fields.selection_canonical_id,
            fields.sharp_book,
            fields.soft_book,
            fields.soft_decimal,
            fields.threshold_used,
            fields.first_seen_ts,
            fields.first_seen_ts,  # last_seen_ts == first_seen_ts on create
            fields.detect_sharp_decimal,
            fields.detect_sharp_opp_decimal,
            fields.detect_sharp_novig_prob,
            fields.detect_edge_pct,
            Status.DETECTED.value,
            fields.confirm_due_ts,
            fields.confirms_required,
        ),
    )
    created = cur.rowcount == 1
    candidate_id = int(
        _scalar(
            conn,
            "SELECT candidate_id FROM candidates WHERE opportunity_key = ?",
            (oppkey,),
            "candidate",
        )
    )
    return candidate_id, created


def increment_observation_count(
    conn: sqlite3.Connection, candidate_id: int, last_seen_ts: str
) -> None:
    """Repeat sighting bookkeeping: ++observation_count and refresh last_seen_ts (P1 §7)."""
    conn.execute(
        "UPDATE candidates SET observation_count = observation_count + 1, last_seen_ts = ? "
        "WHERE candidate_id = ?",
        (last_seen_ts, candidate_id),
    )


def insert_observation(
    conn: sqlite3.Connection,
    candidate_id: int,
    oppkey: str,
    pull_id: int,
    phase: Phase,
    obs: ObservationFields,
) -> int:
    """Append a sighting (idempotent on (pull_id, opportunity_key)); return its id."""
    conn.execute(
        "INSERT INTO candidate_observations(candidate_id, opportunity_key, pull_id, observed_ts, "
        "phase, sharp_decimal, sharp_opp_decimal, sharp_novig_prob, sharp_age_s, soft_decimal, "
        "soft_implied_prob, soft_age_s, edge_pct) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(pull_id, opportunity_key) DO NOTHING",
        (
            candidate_id,
            oppkey,
            pull_id,
            obs.observed_ts,
            phase.value,
            obs.sharp_decimal,
            obs.sharp_opp_decimal,
            obs.sharp_novig_prob,
            obs.sharp_age_s,
            obs.soft_decimal,
            obs.soft_implied_prob,
            obs.soft_age_s,
            obs.edge_pct,
        ),
    )
    return int(
        _scalar(
            conn,
            "SELECT observation_id FROM candidate_observations "
            "WHERE pull_id = ? AND opportunity_key = ?",
            (pull_id, oppkey),
            "observation",
        )
    )


def insert_rejection(
    conn: sqlite3.Connection,
    code: RejectionCode,
    stage: str,
    trigger_values: Mapping[str, object],
    ctx: RejectionContext,
) -> int:
    cur = conn.execute(
        "INSERT INTO rejections(rejected_ts, stage, pull_id, event_id, sport_key, commence_time, "
        "market_key, selection_raw, sharp_book, soft_book, rejection_code, trigger_values, "
        "opportunity_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ctx.rejected_ts,
            stage,
            ctx.pull_id,
            ctx.event_id,
            ctx.sport_key,
            ctx.commence_time,
            ctx.market_key,
            ctx.selection_raw,
            ctx.sharp_book,
            ctx.soft_book,
            code.value,
            canonical_json(trigger_values),
            ctx.opportunity_key,
        ),
    )
    return int(cur.lastrowid or 0)


# --- closing + clv ------------------------------------------------------------------


def insert_closing(conn: sqlite3.Connection, fields: ClosingFields) -> int:
    cur = conn.execute(
        "INSERT INTO closing_lines(event_id, sport_key, commence_time, sharp_book, close_pull_id, "
        "close_capture_ts, minutes_before_commence, outcome_a_id, outcome_a_decimal, "
        "outcome_a_novig, outcome_b_id, outcome_b_decimal, outcome_b_novig, close_source_flag) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fields.event_id,
            fields.sport_key,
            fields.commence_time,
            fields.sharp_book,
            fields.close_pull_id,
            fields.close_capture_ts,
            fields.minutes_before_commence,
            fields.outcome_a_id,
            fields.outcome_a_decimal,
            fields.outcome_a_novig,
            fields.outcome_b_id,
            fields.outcome_b_decimal,
            fields.outcome_b_novig,
            fields.close_source_flag,
        ),
    )
    return int(cur.lastrowid or 0)


def insert_clv(conn: sqlite3.Connection, fields: ClvFields) -> int:
    cur = conn.execute(
        "INSERT INTO clv_results(candidate_id, close_id, d_taken, p_close_novig, "
        "fair_close_decimal, clv_pct, beat_close, graded_ts, grade_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fields.candidate_id,
            fields.close_id,
            fields.d_taken,
            fields.p_close_novig,
            fields.fair_close_decimal,
            fields.clv_pct,
            fields.beat_close,
            fields.graded_ts,
            fields.grade_status,
        ),
    )
    return int(cur.lastrowid or 0)


# --- the candidate-status choke point -----------------------------------------------


def transition_candidate_status(
    conn: sqlite3.Connection, candidate_id: int, target: Status
) -> None:
    """The ONLY path that mutates ``candidates.status``. Validates against P1 §3 first."""
    current = Status(
        _scalar(
            conn,
            "SELECT status FROM candidates WHERE candidate_id = ?",
            (candidate_id,),
            "candidate",
        )
    )
    assert_candidate_transition(current, target)
    conn.execute(
        "UPDATE candidates SET status = ? WHERE candidate_id = ?", (target.value, candidate_id)
    )


__all__ = [
    "ALLOWED_CANDIDATE_TRANSITIONS",
    "ALLOWED_RUN_TRANSITIONS",
    "CandidateFields",
    "ClosingFields",
    "ClvFields",
    "IllegalTransitionError",
    "ObservationFields",
    "RejectionContext",
    "assert_candidate_transition",
    "assert_run_transition",
    "finish_audit_run",
    "increment_observation_count",
    "insert_closing",
    "insert_clv",
    "insert_observation",
    "insert_outcome",
    "insert_raw_pull",
    "insert_rejection",
    "insert_snapshot",
    "log_error",
    "start_audit_run",
    "transition_candidate_status",
    "unit_of_work",
    "upsert_candidate",
    "upsert_entity",
    "upsert_event",
]
