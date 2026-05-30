"""SQLite connection + idempotent migration runner.

Ticket: T3 (spec/00_build_plan.md §2, §3).

Every connection sets the load-bearing PRAGMAs (CLAUDE.md §7; build-plan §3 / concurrency
note): ``foreign_keys=ON`` (FKs enforced), ``journal_mode=WAL`` (three writers share one
file), ``busy_timeout=5000`` (retry on SQLITE_BUSY). Migrations are applied in filename
order, each once, tracked in ``schema_migrations`` so a re-run is a no-op.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MIGRATIONS_DIR = _REPO_ROOT / "migrations"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the required PRAGMAs set.

    PRAGMAs are applied before any transaction (``foreign_keys`` cannot change mid-tx).
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def apply_migrations(
    conn: sqlite3.Connection, migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR
) -> list[str]:
    """Apply every ``*.sql`` in ``migrations_dir`` (sorted) not yet recorded, once each.

    Returns the filenames applied on this call (empty on a no-op re-run). Each migration
    runs in its own committed step and is recorded in ``schema_migrations``.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "    filename   TEXT PRIMARY KEY,"
        "    applied_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    already = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}

    applied: list[str] = []
    for path in sorted(Path(migrations_dir).glob("*.sql")):
        if path.name in already:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations(filename) VALUES (?)", (path.name,))
        conn.commit()
        applied.append(path.name)
    return applied


def initialize(
    db_path: str | Path, migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR
) -> sqlite3.Connection:
    """Create the parent dir if needed, connect, and apply all migrations. Returns the
    open connection (caller closes)."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    apply_migrations(conn, migrations_dir)
    return conn


__all__ = ["DEFAULT_MIGRATIONS_DIR", "apply_migrations", "connect", "initialize"]
