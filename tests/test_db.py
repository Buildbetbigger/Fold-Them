"""T3 acceptance tests: schema, constraints, PRAGMAs, idempotency (build-plan §2 T3).

Includes the single-source-of-truth reconciliation the review asked for: each SQL CHECK
set (candidates.status, candidate_observations.phase) is asserted equal to its
constants enum (Status, Phase), and is enforced behaviorally (every member accepted, a
bogus value rejected). The same discipline will carry to cycle_type / grade_status when
those CHECKs land.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from src import db
from src.constants import Phase, Status

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = {
    "audit_runs",
    "system_errors",
    "raw_api_pulls",
    "normalized_entities",
    "events",
    "bookmaker_snapshots",
    "market_outcomes",
    "closing_lines",
    "candidates",
    "candidate_observations",
    "clv_results",
    "rejections",
}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.initialize(tmp_path / "t.sqlite")
    yield connection
    connection.close()


# --- helpers ------------------------------------------------------------------------


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in rows}


def _check_values(connection: sqlite3.Connection, table: str, column: str) -> set[str]:
    """Extract the allowed value set from a ``column IN (...)`` CHECK in a table's DDL."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    match = re.search(rf"{column} IN \(([^)]*)\)", row[0])
    assert match is not None, f"no CHECK ({column} IN (...)) found on {table}"
    return {token.strip().strip("'") for token in match.group(1).split(",")}


def _seed_parents(connection: sqlite3.Connection) -> tuple[int, int]:
    """Insert one audit_run, raw_api_pull, and event; return (audit_run_id, pull_id)."""
    cur = connection.execute(
        "INSERT INTO audit_runs(run_start_ts, config_hash, config_snapshot, status) "
        "VALUES ('2026-05-30T00:00:00Z', 'h', '{}', 'RUNNING')"
    )
    audit_run_id = int(cur.lastrowid or 0)
    cur = connection.execute(
        "INSERT INTO raw_api_pulls(audit_run_id, pull_timestamp, endpoint, sport_key, "
        "market_key, http_status, payload_hash, raw_payload) "
        "VALUES (?, '2026-05-30T00:00:00Z', '/odds', 'baseball_mlb', 'h2h', 200, 'h', '{}')",
        (audit_run_id,),
    )
    pull_id = int(cur.lastrowid or 0)
    connection.execute(
        "INSERT INTO events(event_id, sport_key, commence_time, home_team_raw, "
        "away_team_raw, first_seen_ts, last_seen_ts) "
        "VALUES ('E1', 'baseball_mlb', '2026-05-30T18:00:00Z', 'Home', 'Away', "
        "'2026-05-30T00:00:00Z', '2026-05-30T00:00:00Z')"
    )
    return audit_run_id, pull_id


def _insert_candidate(
    connection: sqlite3.Connection,
    *,
    oppkey: str,
    audit_run_id: int,
    pull_id: int,
    status: str | None = None,
) -> int:
    cols = [
        "opportunity_key",
        "audit_run_id",
        "first_pull_id",
        "event_id",
        "sport_key",
        "commence_time",
        "market_key",
        "selection_raw",
        "selection_canonical_id",
        "sharp_book",
        "soft_book",
        "soft_decimal",
        "threshold_used",
        "first_seen_ts",
        "last_seen_ts",
        "detect_sharp_decimal",
        "detect_sharp_opp_decimal",
        "detect_sharp_novig_prob",
        "detect_edge_pct",
    ]
    values: list[object] = [
        oppkey,
        audit_run_id,
        pull_id,
        "E1",
        "baseball_mlb",
        "2026-05-30T18:00:00Z",
        "h2h",
        "Home",
        "home",
        "pinnacle",
        "draftkings",
        2.5,
        2.0,
        "2026-05-30T00:00:00Z",
        "2026-05-30T00:00:00Z",
        1.9,
        2.1,
        0.52,
        3.0,
    ]
    if status is not None:
        cols.append("status")
        values.append(status)
    placeholders = ", ".join("?" for _ in cols)
    cur = connection.execute(
        f"INSERT INTO candidates({', '.join(cols)}) VALUES ({placeholders})", values
    )
    return int(cur.lastrowid or 0)


# --- schema presence ----------------------------------------------------------------


def test_all_twelve_tables_exist(conn: sqlite3.Connection) -> None:
    assert _table_names(conn) >= EXPECTED_TABLES


def test_idempotent_rerun_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite"
    first_conn = db.connect(path)
    first = db.apply_migrations(first_conn)
    first_conn.close()
    assert len(first) == 8  # 001..008

    second_conn = db.connect(path)
    before = _table_names(second_conn)
    assert db.apply_migrations(second_conn) == []  # nothing new on re-run
    assert _table_names(second_conn) == before
    second_conn.close()


# --- PRAGMAs ------------------------------------------------------------------------


def test_pragmas_set(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    """A candidate referencing a non-existent event is rejected (FK enforcement on)."""
    audit_run_id, pull_id = _seed_parents(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO candidates(opportunity_key, audit_run_id, first_pull_id, event_id, "
            "sport_key, commence_time, market_key, selection_raw, selection_canonical_id, "
            "sharp_book, soft_book, soft_decimal, threshold_used, first_seen_ts, last_seen_ts, "
            "detect_sharp_decimal, detect_sharp_opp_decimal, detect_sharp_novig_prob, "
            "detect_edge_pct) VALUES ('k', ?, ?, 'NO_SUCH_EVENT', 'baseball_mlb', 't', 'h2h', "
            "'Home', 'home', 'pinnacle', 'draftkings', 2.5, 2.0, 't', 't', 1.9, 2.1, 0.52, 3.0)",
            (audit_run_id, pull_id),
        )


# --- UNIQUE guarantees --------------------------------------------------------------


def test_opportunity_key_unique(conn: sqlite3.Connection) -> None:
    audit_run_id, pull_id = _seed_parents(conn)
    _insert_candidate(conn, oppkey="DUP", audit_run_id=audit_run_id, pull_id=pull_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_candidate(conn, oppkey="DUP", audit_run_id=audit_run_id, pull_id=pull_id)


def test_candidate_observations_unique_pull_oppkey(conn: sqlite3.Connection) -> None:
    audit_run_id, pull_id = _seed_parents(conn)
    cand_id = _insert_candidate(conn, oppkey="K", audit_run_id=audit_run_id, pull_id=pull_id)
    insert = (
        "INSERT INTO candidate_observations(candidate_id, opportunity_key, pull_id, observed_ts, "
        "phase, sharp_decimal, sharp_opp_decimal, sharp_novig_prob, sharp_age_s, soft_decimal, "
        "soft_implied_prob, soft_age_s, edge_pct) "
        "VALUES (?, 'K', ?, 't', 'DETECTION', 1.9, 2.1, 0.52, 1.0, 2.5, 0.4, 1.0, 3.0)"
    )
    conn.execute(insert, (cand_id, pull_id))
    with pytest.raises(sqlite3.IntegrityError):  # same (pull_id, opportunity_key)
        conn.execute(insert, (cand_id, pull_id))


def test_clv_results_one_per_candidate(conn: sqlite3.Connection) -> None:
    audit_run_id, pull_id = _seed_parents(conn)
    cand_id = _insert_candidate(conn, oppkey="K", audit_run_id=audit_run_id, pull_id=pull_id)
    conn.execute(
        "INSERT INTO clv_results(candidate_id, grade_status) VALUES (?, 'GRADED')", (cand_id,)
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO clv_results(candidate_id, grade_status) VALUES (?, 'GRADED')", (cand_id,)
        )


# --- CHECK <-> enum reconciliation (single source of truth) -------------------------


def test_candidate_status_check_matches_status_enum(conn: sqlite3.Connection) -> None:
    assert _check_values(conn, "candidates", "status") == {s.value for s in Status}


def test_status_check_enforced_for_every_member_and_rejects_bogus(
    conn: sqlite3.Connection,
) -> None:
    audit_run_id, pull_id = _seed_parents(conn)
    for i, status in enumerate(Status):
        _insert_candidate(
            conn, oppkey=f"K{i}", audit_run_id=audit_run_id, pull_id=pull_id, status=status.value
        )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_candidate(
            conn, oppkey="KBAD", audit_run_id=audit_run_id, pull_id=pull_id, status="BOGUS"
        )


def test_phase_check_matches_phase_enum(conn: sqlite3.Connection) -> None:
    assert _check_values(conn, "candidate_observations", "phase") == {p.value for p in Phase}


def test_phase_check_enforced_for_every_member_and_rejects_bogus(
    conn: sqlite3.Connection,
) -> None:
    audit_run_id, pull_id = _seed_parents(conn)
    cand_id = _insert_candidate(conn, oppkey="K", audit_run_id=audit_run_id, pull_id=pull_id)
    insert = (
        "INSERT INTO candidate_observations(candidate_id, opportunity_key, pull_id, observed_ts, "
        "phase, sharp_decimal, sharp_opp_decimal, sharp_novig_prob, sharp_age_s, soft_decimal, "
        "soft_implied_prob, soft_age_s, edge_pct) "
        "VALUES (?, ?, ?, 't', ?, 1.9, 2.1, 0.52, 1.0, 2.5, 0.4, 1.0, 3.0)"
    )
    # Same pull_id (a real FK row); distinct opportunity_key keeps UNIQUE(pull_id, oppkey) happy.
    for i, phase in enumerate(Phase):
        conn.execute(insert, (cand_id, f"K{i}", pull_id, phase.value))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (cand_id, "KBAD", pull_id, "BOGUS"))


# --- init_db.py entry point (thin wrapper smoke test) -------------------------------


def test_init_db_script_creates_db(tmp_path: Path) -> None:
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["run"]["db_path"] = str(tmp_path / "out.sqlite")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    spec = importlib.util.spec_from_file_location("init_db", REPO_ROOT / "scripts" / "init_db.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main(["--config", str(cfg_path)]) == 0
    connection = db.connect(tmp_path / "out.sqlite")
    assert _table_names(connection) >= EXPECTED_TABLES
    connection.close()


def test_init_db_runnable_as_script(tmp_path: Path) -> None:
    """`python scripts/init_db.py` works standalone (self-bootstraps sys.path)."""
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["run"]["db_path"] = str(tmp_path / "cli.sqlite")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "init_db.py"), "--config", str(cfg_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "cli.sqlite").exists()
