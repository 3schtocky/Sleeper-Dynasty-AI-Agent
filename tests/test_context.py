"""The shared lookups the CLI and the chat both stand on. Each must raise
AgentError with a fix-it message on an empty database, never SystemExit (which
would end a chat session) and never a bare None that fails later."""

import sqlite3

import pytest

from dynasty_agent import cli, config, context
from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations
from dynasty_agent.errors import AgentError


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c, MIGRATIONS_DIR)
    return c


def test_empty_database_raises_agent_error_not_system_exit(conn):
    for lookup in (
        context.my_roster_id,
        context.stats_season,
        context.latest_complete_season,
        context.vegas_season,
        context.current_week,
        context.scoring_settings,
    ):
        with pytest.raises(AgentError, match="refresh"):
            lookup(conn)


def test_lookups_read_the_synced_league(conn, monkeypatch):
    monkeypatch.setattr(config, "SLEEPER_USER_ID", "u1")
    conn.execute("INSERT INTO rosters (roster_id, league_id, owner_id, fetched_at) VALUES (7, 'L1', 'u1', 't')")
    conn.execute("INSERT INTO nfl_state (fetched_at, season, season_type, week) VALUES ('t', '2026', 'regular', 4)")
    conn.execute(
        "INSERT INTO weekly_stats (player_id, season, week, position, fantasy_points, fetched_at) "
        "VALUES ('p1', '2026', 3, 'WR', 10, 't')"
    )
    assert context.my_roster_id(conn) == 7
    assert context.stats_season(conn) == 2026
    assert context.stats_season(conn, 2025) == 2025  # an explicit season wins
    assert context.latest_complete_season(conn) == 2025
    assert context.vegas_season(conn) == 2026
    assert context.current_week(conn) == 4


def test_null_nfl_state_season_is_a_clear_error(conn):
    conn.execute("INSERT INTO nfl_state (fetched_at, season, week) VALUES ('t', NULL, NULL)")
    with pytest.raises(AgentError, match="current season"):
        context.vegas_season(conn)


def test_draft_id_is_not_required(monkeypatch):
    # No command reads DRAFT_ID, and `init` writes it empty for a league whose
    # rookie draft Sleeper hasn't created yet; requiring it locked those users out.
    for name, value in [("SLEEPER_USERNAME", "me"), ("SLEEPER_USER_ID", "u1"), ("LEAGUE_ID", "L1"), ("DRAFT_ID", None)]:
        monkeypatch.setattr(config, name, value)
    assert config.missing_config() == []
    context.require_config()


def test_cli_turns_agent_error_into_exit_code_1(monkeypatch, capsys):
    def boom(args):
        raise AgentError("No roster found for this user. Run `dynasty-agent refresh` first.")

    monkeypatch.setattr(cli, "cmd_taxi", boom)
    monkeypatch.setattr("sys.argv", ["dynasty-agent", "taxi"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert "Error: No roster found" in capsys.readouterr().err
