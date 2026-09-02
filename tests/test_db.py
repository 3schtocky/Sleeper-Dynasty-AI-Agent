import sqlite3

from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations


def test_migrations_apply_cleanly_and_are_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    first_pass = apply_migrations(conn, MIGRATIONS_DIR)
    assert first_pass, "expected at least one migration to apply on a fresh database"

    second_pass = apply_migrations(conn, MIGRATIONS_DIR)
    assert second_pass == [], "a migration already recorded must not run twice"

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for expected in (
        "players",
        "league",
        "rosters",
        "roster_players",
        "traded_picks",
        "weekly_stats",
        "market_values",
        "api_cache",
    ):
        assert expected in tables
