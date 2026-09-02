"""SQLite connection and migration runner.

Migrations live in migrations/ as numbered .sql files. Each one is applied
exactly once, tracked in the schema_migrations table. No DDL lives inline in
Python; the schema lives entirely in those files.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dynasty_agent.config import DB_PATH, MIGRATIONS_DIR


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_migrations(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply any migration file not yet recorded. Returns the versions applied."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    newly_applied = []
    for path in sorted(migrations_dir.glob("*.sql")):
        version = path.stem
        if version in applied:
            continue
        conn.executescript(path.read_text())
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, utcnow()),
        )
        conn.commit()
        newly_applied.append(version)
    return newly_applied


def get_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Connect and make sure migrations are applied. The normal entry point."""
    conn = connect(db_path)
    apply_migrations(conn)
    return conn
