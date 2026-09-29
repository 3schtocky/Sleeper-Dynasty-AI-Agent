"""What the league looks like right now: the user's roster, the seasons to value
on, the current week. Shared by the CLI and the chat, so neither asks the user
(or a model) for something the database already knows.

Every lookup raises AgentError with a fix-it message instead of returning a
None that fails three calls later.
"""

from __future__ import annotations

import json
import sqlite3

from dynasty_agent import config, refresh
from dynasty_agent.errors import AgentError

_REFRESH_FIRST = "Run `dynasty-agent refresh` first."


def require_config() -> None:
    missing = config.missing_config()
    if missing:
        raise AgentError(
            f"Missing config: {', '.join(missing)}. Run "
            f"`dynasty-agent init --username <your sleeper username>` to set it up, "
            f"or copy .env.example to .env and fill it in by hand."
        )


def my_roster_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT roster_id FROM rosters WHERE owner_id = ?", (config.SLEEPER_USER_ID,)).fetchone()
    if row is None:
        raise AgentError(f"No roster found for this user. {_REFRESH_FIRST}")
    return row["roster_id"]


def latest_ingested_season(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT max(season) FROM weekly_stats").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def stats_season(conn: sqlite3.Connection, explicit: int | None = None) -> int:
    """The season player values are built on: explicit if given, else the
    latest season with NFL weekly stats ingested."""
    season = explicit or latest_ingested_season(conn)
    if season is None:
        raise AgentError(f"No NFL stats ingested yet. {_REFRESH_FIRST}")
    return season


def _nfl_state(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT season, season_type, week FROM nfl_state ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if row is None or row["season"] is None:
        raise AgentError(f"No synced NFL state to tell the current season. {_REFRESH_FIRST}")
    return row


def latest_complete_season(conn: sqlite3.Connection) -> int:
    """The most recent NFL season with every regular-season week played."""
    row = _nfl_state(conn)
    return refresh.latest_complete_season(int(row["season"]), row["season_type"])


def vegas_season(conn: sqlite3.Connection, explicit: int | None = None) -> int:
    """The season a week's Vegas lines belong to. The real current NFL season
    from the last sync when not given explicitly, never inferred from the
    FPPG baseline season: week 1 of a completed season already has real
    closing lines from last year's game, so any presence-based fallback
    silently prices the wrong year. See matchup.predict_matchup's docstring
    for the bug this replaced."""
    if explicit is not None:
        return explicit
    return int(_nfl_state(conn)["season"])


def current_week(conn: sqlite3.Connection) -> int:
    """This week of the NFL season per Sleeper's state, at least 1."""
    week = _nfl_state(conn)["week"]
    return max(1, int(week)) if week is not None else 1


def scoring_settings(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT scoring_settings_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if row is None:
        raise AgentError(f"No league data cached yet. {_REFRESH_FIRST}")
    return json.loads(row["scoring_settings_json"])


def team_names(conn: sqlite3.Connection) -> dict[int, str]:
    """{roster_id: the manager's team name, else display name, else "roster N"}."""
    return {
        r["roster_id"]: (r["team_name"] or r["display_name"] or f"roster {r['roster_id']}")
        for r in conn.execute(
            "SELECT ro.roster_id, u.display_name, u.team_name FROM rosters ro LEFT JOIN users u ON u.user_id = ro.owner_id"
        )
    }
