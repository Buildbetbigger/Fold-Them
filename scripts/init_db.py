#!/usr/bin/env python
"""Apply database migrations (T3).

Usage: python scripts/init_db.py [--config config.yaml]

Thin CLI wrapper; all logic lives in src/db.py (which the test suite covers). Reads only
``run.db_path`` from the resolved config; the API key is not needed to migrate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python scripts/init_db.py` to import the `src` package regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db  # noqa: E402
from src.config_loader import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize/migrate the SQLite database.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    Path(cfg.run.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(cfg.run.db_path)
    try:
        applied = db.apply_migrations(conn)
    finally:
        conn.close()

    if applied:
        print(f"DB ready at {cfg.run.db_path}; applied {len(applied)} migration(s): {applied}")
    else:
        print(f"DB ready at {cfg.run.db_path}; already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
