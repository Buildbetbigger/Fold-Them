"""T4 acceptance tests for src/repo.py.

Emphasis (per review): the status-transition guard is exhaustively tested as a matrix
(every allowed move accepted, every other move rejected) for both the candidate and run
lifecycles, and ``upsert_candidate`` is tested on its conflict path (concurrent duplicate
must NOT mint a second candidate). Every writer is exercised for coverage.
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from src import db, repo
from src.constants import Phase, RejectionCode, RunStatus, Severity, Status


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.initialize(tmp_path / "t.sqlite")
    yield connection
    connection.close()


def _seed(connection: sqlite3.Connection) -> tuple[int, int]:
    """Create one audit_run + raw_pull + event via the writers; return (run_id, pull_id)."""
    run_id = repo.start_audit_run(
        connection,
        run_start_ts="2026-05-30T00:00:00Z",
        config_hash="h",
        config_snapshot="{}",
        run_label="t",
    )
    pull_id = repo.insert_raw_pull(
        connection,
        audit_run_id=run_id,
        pull_timestamp="2026-05-30T00:00:00Z",
        endpoint="/odds",
        sport_key="baseball_mlb",
        market_key="h2h",
        region="us",
        http_status=200,
        payload='{"ok": true}',
    )
    repo.upsert_event(
        connection,
        event_id="E1",
        sport_key="baseball_mlb",
        commence_time="2026-05-30T18:00:00Z",
        home_team_raw="Home",
        away_team_raw="Away",
        first_seen_ts="2026-05-30T00:00:00Z",
        last_seen_ts="2026-05-30T00:00:00Z",
    )
    return run_id, pull_id


def _cfields(run_id: int, pull_id: int) -> repo.CandidateFields:
    return repo.CandidateFields(
        audit_run_id=run_id,
        first_pull_id=pull_id,
        event_id="E1",
        sport_key="baseball_mlb",
        commence_time="2026-05-30T18:00:00Z",
        market_key="h2h",
        selection_raw="Home",
        selection_canonical_id="home",
        sharp_book="pinnacle",
        soft_book="draftkings",
        soft_decimal=2.5,
        threshold_used=2.0,
        first_seen_ts="2026-05-30T00:00:00Z",
        detect_sharp_decimal=1.9,
        detect_sharp_opp_decimal=2.1,
        detect_sharp_novig_prob=0.52,
        detect_edge_pct=3.0,
        confirm_due_ts="2026-05-30T00:01:00Z",
    )


def _obs() -> repo.ObservationFields:
    return repo.ObservationFields(
        observed_ts="2026-05-30T00:00:00Z",
        sharp_decimal=1.9,
        sharp_opp_decimal=2.1,
        sharp_novig_prob=0.52,
        sharp_age_s=1.0,
        soft_decimal=2.5,
        soft_implied_prob=0.4,
        soft_age_s=1.0,
        edge_pct=3.0,
    )


def _status(connection: sqlite3.Connection, candidate_id: int) -> str:
    row = connection.execute(
        "SELECT status FROM candidates WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    return str(row[0])


# --- transition guard: exhaustive matrices ------------------------------------------


def test_candidate_transition_matrix_pure() -> None:
    for current, target in itertools.product(Status, Status):
        if target in repo.ALLOWED_CANDIDATE_TRANSITIONS[current]:
            repo.assert_candidate_transition(current, target)  # must not raise
        else:
            with pytest.raises(repo.IllegalTransitionError):
                repo.assert_candidate_transition(current, target)


def test_run_transition_matrix_pure() -> None:
    for current, target in itertools.product(RunStatus, RunStatus):
        if target in repo.ALLOWED_RUN_TRANSITIONS[current]:
            repo.assert_run_transition(current, target)
        else:
            with pytest.raises(repo.IllegalTransitionError):
                repo.assert_run_transition(current, target)


def test_candidate_transition_map_matches_p1_lifecycle() -> None:
    """The map is the authoritative P1 §3 lifecycle — assert it verbatim."""
    assert {
        Status.DETECTED: frozenset({Status.PENDING_CONFIRM, Status.TRANSIENT}),
        Status.PENDING_CONFIRM: frozenset({Status.CONFIRMED, Status.TRANSIENT}),
        Status.CONFIRMED: frozenset({Status.PENDING_GRADE}),
        Status.PENDING_GRADE: frozenset({Status.GRADED, Status.UNGRADED}),
        Status.TRANSIENT: frozenset(),
        Status.GRADED: frozenset(),
        Status.UNGRADED: frozenset(),
    } == repo.ALLOWED_CANDIDATE_TRANSITIONS


# --- candidate-status choke point (DB) ----------------------------------------------


def test_transition_candidate_status_happy_path(conn: sqlite3.Connection) -> None:
    run_id, pull_id = _seed(conn)
    cand_id, _ = repo.upsert_candidate(conn, "K", _cfields(run_id, pull_id))
    for target in (Status.PENDING_CONFIRM, Status.CONFIRMED, Status.PENDING_GRADE, Status.GRADED):
        repo.transition_candidate_status(conn, cand_id, target)
        assert _status(conn, cand_id) == target.value


def test_transition_candidate_status_illegal_raises(conn: sqlite3.Connection) -> None:
    run_id, pull_id = _seed(conn)
    cand_id, _ = repo.upsert_candidate(conn, "K", _cfields(run_id, pull_id))
    with pytest.raises(repo.IllegalTransitionError):
        repo.transition_candidate_status(conn, cand_id, Status.GRADED)  # DETECTED -> GRADED
    assert _status(conn, cand_id) == Status.DETECTED.value  # unchanged


def test_transition_on_missing_candidate_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(LookupError):
        repo.transition_candidate_status(conn, 999, Status.PENDING_CONFIRM)


def test_detected_to_transient_allowed(conn: sqlite3.Connection) -> None:
    """Job C expires unconfirmed stragglers DETECTED -> TRANSIENT (CONFIRM_EXPIRED)."""
    run_id, pull_id = _seed(conn)
    cand_id, _ = repo.upsert_candidate(conn, "K", _cfields(run_id, pull_id))
    repo.transition_candidate_status(conn, cand_id, Status.TRANSIENT)
    assert _status(conn, cand_id) == Status.TRANSIENT.value


# --- upsert_candidate atomicity -----------------------------------------------------


def test_upsert_candidate_conflict_path(conn: sqlite3.Connection) -> None:
    run_id, pull_id = _seed(conn)
    id1, created1 = repo.upsert_candidate(conn, "DUP", _cfields(run_id, pull_id))
    id2, created2 = repo.upsert_candidate(conn, "DUP", _cfields(run_id, pull_id))
    assert created1 is True
    assert created2 is False  # the racing duplicate is NOT a new candidate
    assert id1 == id2
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1


def test_upsert_candidate_creates_with_detected_status(conn: sqlite3.Connection) -> None:
    run_id, pull_id = _seed(conn)
    cand_id, created = repo.upsert_candidate(conn, "K", _cfields(run_id, pull_id))
    assert created is True
    row = conn.execute(
        "SELECT status, observation_count, last_seen_ts, first_seen_ts FROM candidates "
        "WHERE candidate_id = ?",
        (cand_id,),
    ).fetchone()
    assert row[0] == Status.DETECTED.value
    assert row[1] == 1
    assert row[2] == row[3]  # last_seen_ts == first_seen_ts on create


# --- run lifecycle ------------------------------------------------------------------


def test_finish_audit_run_allowed(conn: sqlite3.Connection) -> None:
    run_id = repo.start_audit_run(conn, run_start_ts="t", config_hash="h", config_snapshot="{}")
    repo.finish_audit_run(conn, run_id, run_end_ts="t2", status=RunStatus.COMPLETED)
    row = conn.execute(
        "SELECT status, run_end_ts FROM audit_runs WHERE audit_run_id = ?", (run_id,)
    ).fetchone()
    assert row[0] == RunStatus.COMPLETED.value
    assert row[1] == "t2"


def test_finish_audit_run_illegal_from_terminal(conn: sqlite3.Connection) -> None:
    run_id = repo.start_audit_run(conn, run_start_ts="t", config_hash="h", config_snapshot="{}")
    repo.finish_audit_run(conn, run_id, run_end_ts="t2", status=RunStatus.COMPLETED)
    with pytest.raises(repo.IllegalTransitionError):  # COMPLETED -> ABORTED not allowed
        repo.finish_audit_run(conn, run_id, run_end_ts="t3", status=RunStatus.ABORTED)


def test_finish_missing_run_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(LookupError):
        repo.finish_audit_run(conn, 999, run_end_ts="t", status=RunStatus.COMPLETED)


# --- remaining writers (coverage + basic behavior) ----------------------------------


def test_log_error_with_and_without_context(conn: sqlite3.Connection) -> None:
    run_id, _ = _seed(conn)
    eid1 = repo.log_error(
        conn,
        error_ts="t",
        component="clock",
        severity=Severity.WARN,
        message="skew",
        audit_run_id=run_id,
        context={"skew_s": 3},
    )
    eid2 = repo.log_error(conn, error_ts="t", component="db", severity=Severity.FATAL, message="x")
    rows = conn.execute("SELECT severity, context FROM system_errors ORDER BY error_id").fetchall()
    assert eid1 != eid2
    assert rows[0][0] == "WARN"
    assert rows[0][1] == '{"skew_s":3}'  # canonical JSON
    assert rows[1][1] is None


def test_upsert_event_refreshes_last_seen(conn: sqlite3.Connection) -> None:
    _seed(conn)  # first insert at last_seen 00:00
    repo.upsert_event(
        conn,
        event_id="E1",
        sport_key="baseball_mlb",
        commence_time="2026-05-30T18:00:00Z",
        home_team_raw="Home",
        away_team_raw="Away",
        first_seen_ts="ignored",
        last_seen_ts="2026-05-30T01:00:00Z",
    )
    row = conn.execute(
        "SELECT first_seen_ts, last_seen_ts FROM events WHERE event_id = 'E1'"
    ).fetchone()
    assert row[0] == "2026-05-30T00:00:00Z"  # first_seen unchanged
    assert row[1] == "2026-05-30T01:00:00Z"  # last_seen refreshed
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_upsert_entity_idempotent(conn: sqlite3.Connection) -> None:
    id1 = repo.upsert_entity(
        conn,
        sport_key="baseball_mlb",
        raw_name="NY Yankees",
        canonical_name="New York Yankees",
        canonical_id="nyy",
    )
    id2 = repo.upsert_entity(
        conn,
        sport_key="baseball_mlb",
        raw_name="NY Yankees",
        canonical_name="New York Yankees",
        canonical_id="nyy",
    )
    assert id1 == id2
    assert conn.execute("SELECT COUNT(*) FROM normalized_entities").fetchone()[0] == 1


def test_insert_snapshot_and_outcome(conn: sqlite3.Connection) -> None:
    _run_id, pull_id = _seed(conn)
    snap_id = repo.insert_snapshot(
        conn,
        pull_id=pull_id,
        event_id="E1",
        bookmaker_key="pinnacle",
        market_key="h2h",
        api_last_update="t",
        pull_timestamp="t",
        bookmaker_title="Pinnacle",
    )
    out_id = repo.insert_outcome(
        conn,
        snapshot_id=snap_id,
        event_id="E1",
        bookmaker_key="pinnacle",
        market_key="h2h",
        outcome_name_raw="Home",
        price_decimal=1.9,
        price_american=-110,
    )
    assert snap_id > 0
    assert (
        conn.execute(
            "SELECT price_decimal FROM market_outcomes WHERE outcome_id = ?", (out_id,)
        ).fetchone()[0]
        == 1.9
    )


def test_insert_observation_idempotent_and_count_bump(conn: sqlite3.Connection) -> None:
    run_id, pull_id = _seed(conn)
    cand_id, _ = repo.upsert_candidate(conn, "K", _cfields(run_id, pull_id))
    o1 = repo.insert_observation(conn, cand_id, "K", pull_id, Phase.DETECTION, _obs())
    o2 = repo.insert_observation(conn, cand_id, "K", pull_id, Phase.DETECTION, _obs())
    assert o1 == o2  # idempotent on (pull_id, opportunity_key)
    assert conn.execute("SELECT COUNT(*) FROM candidate_observations").fetchone()[0] == 1

    repo.increment_observation_count(conn, cand_id, "2026-05-30T00:02:00Z")
    row = conn.execute(
        "SELECT observation_count, last_seen_ts FROM candidates WHERE candidate_id = ?", (cand_id,)
    ).fetchone()
    assert row[0] == 2
    assert row[1] == "2026-05-30T00:02:00Z"


def test_insert_rejection(conn: sqlite3.Connection) -> None:
    _run_id, pull_id = _seed(conn)
    rej_id = repo.insert_rejection(
        conn,
        RejectionCode.NO_SHARP,
        "sharp_source",
        {"event_id": "E1", "books_present": ["draftkings"]},
        repo.RejectionContext(
            rejected_ts="t", sport_key="baseball_mlb", pull_id=pull_id, event_id="E1"
        ),
    )
    row = conn.execute(
        "SELECT rejection_code, trigger_values FROM rejections WHERE rejection_id = ?", (rej_id,)
    ).fetchone()
    assert row[0] == "NO_SHARP"
    assert row[1] == '{"books_present":["draftkings"],"event_id":"E1"}'  # canonical (sorted)


def test_insert_closing_and_clv(conn: sqlite3.Connection) -> None:
    run_id, pull_id = _seed(conn)
    cand_id, _ = repo.upsert_candidate(conn, "K", _cfields(run_id, pull_id))
    close_id = repo.insert_closing(
        conn,
        repo.ClosingFields(
            event_id="E1",
            sport_key="baseball_mlb",
            commence_time="2026-05-30T18:00:00Z",
            sharp_book="pinnacle",
            close_source_flag="NORMAL",
            close_pull_id=pull_id,
            outcome_a_id="home",
            outcome_a_decimal=1.95,
            outcome_a_novig=0.51,
            outcome_b_id="away",
            outcome_b_decimal=2.05,
            outcome_b_novig=0.49,
        ),
    )
    clv_id = repo.insert_clv(
        conn,
        repo.ClvFields(
            candidate_id=cand_id,
            grade_status="GRADED",
            close_id=close_id,
            d_taken=2.5,
            p_close_novig=0.49,
            fair_close_decimal=2.04,
            clv_pct=22.5,
            beat_close=True,
            graded_ts="t",
        ),
    )
    assert close_id > 0
    assert conn.execute(
        "SELECT grade_status, beat_close FROM clv_results WHERE clv_id = ?", (clv_id,)
    ).fetchone() == ("GRADED", 1)
