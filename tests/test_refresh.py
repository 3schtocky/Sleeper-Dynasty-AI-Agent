import sqlite3
from datetime import date

import pytest

from dynasty_agent import refresh
from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations


def test_latest_complete_season_follows_sleepers_season_type():
    assert refresh.latest_complete_season(2026, "regular") == 2025
    assert refresh.latest_complete_season(2026, "pre") == 2025
    assert refresh.latest_complete_season(2026, "off") == 2026  # after the Super Bowl, before the new league year


def test_college_season_runs_august_through_january():
    assert refresh.college_season_for(date(2026, 9, 28)) == 2026
    assert refresh.college_season_for(date(2027, 1, 10)) == 2026  # bowls and playoff belong to the fall season
    assert refresh.college_season_for(date(2027, 4, 1)) is None


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c, MIGRATIONS_DIR)
    return c


def test_one_failing_step_is_reported_and_the_rest_still_run(conn, monkeypatch):
    calls = []

    def sync_fails(*a, **kw):
        raise ConnectionError("Sleeper unreachable")

    class FakeClient:
        def __init__(self, conn):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        sync_all = sync_fails

        def sync_matchups(self, week):
            calls.append(("matchups", week))

    # State from an earlier sync is still in the database, so the later
    # steps can run even though today's sync fails.
    conn.execute("INSERT INTO nfl_state (fetched_at, season, week, season_type) VALUES ('t', '2026', 4, 'regular')")
    conn.execute(
        "INSERT INTO league (league_id, season, scoring_settings_json, roster_positions_json, settings_json, fetched_at) "
        "VALUES ('L1', '2026', '{}', '[]', '{}', 't')"
    )
    conn.execute("INSERT INTO weekly_stats (player_id, season, week, fantasy_points, fetched_at) VALUES ('p', '2025', 1, 1.0, 't')")
    conn.execute("INSERT INTO nfl_draft_picks (season, round, pick, fetched_at) VALUES (2026, 1, 1, 't')")
    monkeypatch.setattr(refresh, "SleeperClient", FakeClient)
    monkeypatch.setattr(refresh.nflverse, "ingest_season", lambda conn, season, scoring, force=False: calls.append(("stats", season, force)) or "ok")
    monkeypatch.setattr(refresh.nflverse, "ensure_games_cached", lambda force=False: calls.append(("games",)) or refresh.nflverse.NFLVERSE_CACHE_DIR / "games.parquet")
    monkeypatch.setattr(refresh.college, "ingest_season", lambda conn, season, force=False: calls.append(("college", season)) or "ok")
    monkeypatch.setattr(refresh.prospect_model, "load_model", lambda conn, v: {"training_classes": "2018-2023"})

    results = refresh.run(conn, today=date(2026, 9, 28))
    by_step = {r["step"]: r for r in results}

    assert by_step["sync"]["ok"] is False and "Sleeper unreachable" in by_step["sync"]["detail"]
    assert ("stats", 2026, True) in calls  # the current season, re-downloaded
    assert ("matchups", 4) in calls
    assert ("college", 2026) in calls
    assert "NFL draft, combine, player ids" not in by_step  # September: not draft season, data already present
    assert "prospect model" not in by_step  # stored fit already covers 2018-2023
