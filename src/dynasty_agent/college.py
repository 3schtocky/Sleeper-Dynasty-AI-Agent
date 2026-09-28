"""Phase 4 college production: per-player season totals and each team's
receiving totals, from sportsdataverse's ESPN college football releases.

Chosen by request over the College Football Data API (needs an email to
register). Keyless GitHub releases, the same pattern as nflverse, one file
per season. Checked live before writing this:

- espn_cfb_player_box has one row per player per game per stat category
  (receiving, rushing, passing, defensive, and so on), stats stored as
  text, and a player's receiving line only on his "receiving" row. Summing
  across categories would count nothing twice here, however it would read
  empty columns; every stat below is summed from its own category only.
- espn_cfb_game_rosters carries ESPN's position as an id inside a URL
  (position_href). Confirmed against known players: 1 = WR (Carnell Tate,
  Jeremiah Smith), 7 = TE (Kenyon Sadiq, Eli Stowers), 8 = QB (Fernando
  Mendoza), 9 = RB (Jeremiyah Love, Ahmad Hardy).
- Birth dates exist on the rosters file but are sparse (639 of 26,316
  athletes in 2025). Stored where present, never estimated.
- The same columns exist for every season 2008 through 2026, however older
  seasons carry far fewer rows (29,130 box score rows in 2008 against
  80,498 in 2025); ingest_season reports each season's games-per-team so a
  thin season is visible, not assumed complete.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import duckdb

from dynasty_agent.config import DATA_DIR
from dynasty_agent.db import utcnow
from dynasty_agent.nflverse import download

CFB_CACHE_DIR = DATA_DIR / "cfb"
RELEASE_BASE = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"

FILES = {
    "player_box": ("espn_cfb_player_box", "player_box_{season}.parquet"),
    "game_rosters": ("espn_cfb_game_rosters", "game_rosters_{season}.parquet"),
    "team_info": ("cfb_team_info", "cfb_team_info_{season}.parquet"),
    "ratings": ("cfb_ratings", "cfb_ratings_{season}.parquet"),
}

# Power conferences by season. The Pac-12 was one through 2023 and is two
# teams from 2024 on; Notre Dame (ESPN team 87) is an independent that
# schedules like one.
POWER_CONFERENCES = ("SEC", "Big Ten", "Big 12", "ACC")
NOTRE_DAME_TEAM_ID = "87"


def is_power_conference(conference: str | None, season: int, team_id: str) -> bool:
    if team_id == NOTRE_DAME_TEAM_ID:
        return True
    if conference == "Pac-12":
        return season <= 2023
    return conference in POWER_CONFERENCES

# ESPN position id -> position, for the four positions this league starts.
ESPN_POSITION_IDS: dict[str, str] = {"1": "WR", "7": "TE", "8": "QB", "9": "RB"}


def ensure_cached(kind: str, season: int, force: bool = False) -> Path:
    """Download one season's file to data/cfb/ if not already cached. Same
    refresh rule as nflverse.ensure_cached: force=True re-downloads, needed
    for the in-progress college season."""
    tag, template = FILES[kind]
    dest = CFB_CACHE_DIR / f"{kind}_{season}.parquet"
    if dest.exists() and not force:
        return dest
    return download(f"{RELEASE_BASE}/{tag}/{template.format(season=season)}", dest)


PLAYER_SEASON_QUERY = """
    WITH box AS (
        SELECT CAST(athlete_id AS VARCHAR) AS athlete_id, athlete_name,
               CAST(team_id AS VARCHAR) AS team_id, game_id, category,
               try_cast(receptions AS DOUBLE) AS receptions,
               try_cast(receivingYards AS DOUBLE) AS rec_yds,
               try_cast(receivingTouchdowns AS DOUBLE) AS rec_td,
               try_cast(rushingAttempts AS DOUBLE) AS rush_att,
               try_cast(rushingYards AS DOUBLE) AS rush_yds,
               try_cast(rushingTouchdowns AS DOUBLE) AS rush_td,
               try_cast(split_part("completions/passingAttempts", '/', 1) AS DOUBLE) AS pass_cmp,
               try_cast(split_part("completions/passingAttempts", '/', 2) AS DOUBLE) AS pass_att,
               try_cast(passingYards AS DOUBLE) AS pass_yds,
               try_cast(passingTouchdowns AS DOUBLE) AS pass_td
        FROM read_parquet(?)
        WHERE athlete_id IS NOT NULL AND team_id IS NOT NULL
    ),
    player AS (
        SELECT athlete_id, team_id, any_value(athlete_name) AS athlete_name,
               count(DISTINCT game_id) AS games,
               sum(receptions) FILTER (WHERE category = 'receiving') AS receptions,
               sum(rec_yds) FILTER (WHERE category = 'receiving') AS rec_yds,
               sum(rec_td) FILTER (WHERE category = 'receiving') AS rec_td,
               sum(rush_att) FILTER (WHERE category = 'rushing') AS rush_att,
               sum(rush_yds) FILTER (WHERE category = 'rushing') AS rush_yds,
               sum(rush_td) FILTER (WHERE category = 'rushing') AS rush_td,
               sum(pass_cmp) FILTER (WHERE category = 'passing') AS pass_cmp,
               sum(pass_att) FILTER (WHERE category = 'passing') AS pass_att,
               sum(pass_yds) FILTER (WHERE category = 'passing') AS pass_yds,
               sum(pass_td) FILTER (WHERE category = 'passing') AS pass_td
        FROM box
        GROUP BY athlete_id, team_id
        HAVING count(*) FILTER (WHERE category IN ('receiving', 'rushing', 'passing')) > 0
    ),
    team AS (
        SELECT team_id,
               count(DISTINCT game_id) AS team_games,
               coalesce(sum(rec_yds) FILTER (WHERE category = 'receiving'), 0) AS team_rec_yds,
               coalesce(sum(rec_td) FILTER (WHERE category = 'receiving'), 0) AS team_rec_td
        FROM box
        GROUP BY team_id
    ),
    roster AS (
        SELECT CAST(athlete_id AS VARCHAR) AS athlete_id, CAST(team_id AS VARCHAR) AS team_id,
               mode(regexp_extract(position_href, 'positions/([0-9]+)', 1)) AS position_id,
               arg_max(experience_abbreviation, week) AS class_year
        FROM read_parquet(?)
        WHERE athlete_id IS NOT NULL
        GROUP BY 1, 2
    )
    SELECT p.athlete_id, p.team_id, p.athlete_name, r.position_id, r.class_year, p.games,
           p.receptions, p.rec_yds, p.rec_td, p.rush_att, p.rush_yds, p.rush_td,
           p.pass_cmp, p.pass_att, p.pass_yds, p.pass_td,
           t.team_games, t.team_rec_yds, t.team_rec_td
    FROM player p
    JOIN team t USING (team_id)
    LEFT JOIN roster r ON r.athlete_id = p.athlete_id AND r.team_id = p.team_id
"""

ATHLETE_QUERY = """
    SELECT CAST(athlete_id AS VARCHAR) AS athlete_id,
           arg_max(full_name, week) AS full_name,
           max(date_of_birth) AS date_of_birth,
           arg_max(height, week) AS height_in,
           arg_max(weight, week) AS weight_lb
    FROM read_parquet(?)
    WHERE athlete_id IS NOT NULL
    GROUP BY 1
"""


def _as_int(value: float | None) -> int | None:
    return None if value is None else int(round(value))


def ingest_season(conn: sqlite3.Connection, season: int, force: bool = False) -> str:
    """Cache one season's box scores and rosters, then replace that season's
    cfb_player_season rows and upsert cfb_athletes. Returns a summary that
    includes median games per team, the coverage check for thin seasons."""
    box_path = ensure_cached("player_box", season, force=force)
    roster_path = ensure_cached("game_rosters", season, force=force)

    con = duckdb.connect()
    try:
        player_rows = con.execute(PLAYER_SEASON_QUERY, [str(box_path), str(roster_path)]).fetchall()
        athlete_rows = con.execute(ATHLETE_QUERY, [str(roster_path)]).fetchall()
    finally:
        con.close()

    fetched_at = utcnow()
    season_rows = []
    for (athlete_id, team_id, name, position_id, class_year, games, rec, rec_yds, rec_td, rush_att, rush_yds,
         rush_td, pass_cmp, pass_att, pass_yds, pass_td, team_games, team_rec_yds, team_rec_td) in player_rows:
        season_rows.append((
            athlete_id, season, team_id, name, ESPN_POSITION_IDS.get(position_id or ""), class_year, games,
            _as_int(rec), rec_yds, _as_int(rec_td), _as_int(rush_att), rush_yds, _as_int(rush_td),
            _as_int(pass_cmp), _as_int(pass_att), pass_yds, _as_int(pass_td),
            team_games, team_rec_yds, _as_int(team_rec_td), fetched_at,
        ))

    conn.execute("DELETE FROM cfb_player_season WHERE season = ?", (season,))
    conn.executemany(
        """
        INSERT INTO cfb_player_season (
            athlete_id, season, team_id, athlete_name, position, class_year, games,
            receptions, rec_yds, rec_td, rush_att, rush_yds, rush_td,
            pass_cmp, pass_att, pass_yds, pass_td, team_games, team_rec_yds, team_rec_td, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        season_rows,
    )
    # A birth date, once known, is never overwritten with NULL by a later
    # season's roster that happens not to list it.
    conn.executemany(
        """
        INSERT INTO cfb_athletes (athlete_id, full_name, date_of_birth, dob_source, height_in, weight_lb, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (athlete_id) DO UPDATE SET
            full_name = excluded.full_name,
            date_of_birth = coalesce(cfb_athletes.date_of_birth, excluded.date_of_birth),
            dob_source = CASE WHEN cfb_athletes.date_of_birth IS NULL THEN excluded.dob_source ELSE cfb_athletes.dob_source END,
            height_in = coalesce(excluded.height_in, cfb_athletes.height_in),
            weight_lb = coalesce(excluded.weight_lb, cfb_athletes.weight_lb),
            fetched_at = excluded.fetched_at
        """,
        [
            (aid, name, dob[:10] if dob else None, "espn_roster" if dob else None, height, weight, fetched_at)
            for aid, name, dob, height, weight in athlete_rows
        ],
    )
    team_rows = _ingest_team_context(conn, season, force, fetched_at)
    conn.commit()

    team_games = sorted({(r[2], r[17]) for r in season_rows}, key=lambda t: t[1])
    median_games = team_games[len(team_games) // 2][1] if team_games else 0
    skill = sum(1 for r in season_rows if r[4] is not None)
    return (
        f"{season}: {len(season_rows)} player-team seasons ({skill} QB/RB/WR/TE), "
        f"{len(team_games)} teams, median {median_games} games per team in ESPN's box scores, "
        f"{team_rows} team context rows."
    )


def _ingest_team_context(conn: sqlite3.Connection, season: int, force: bool, fetched_at: str) -> int:
    """Replace this season's cfb_team_season rows: conference and
    classification for every team, net_z for FBS teams that have a rating."""
    info_path = ensure_cached("team_info", season, force=force)
    ratings_path = ensure_cached("ratings", season, force=force)
    con = duckdb.connect()
    try:
        rows = con.execute(
            """
            SELECT CAST(i.team_id AS VARCHAR), i.school, i.conference, i.classification,
                   CAST(r.net_z AS DOUBLE), CAST(r.net_rank AS INTEGER)
            FROM read_parquet(?) i
            LEFT JOIN read_parquet(?) r ON CAST(r.team_id AS VARCHAR) = CAST(i.team_id AS VARCHAR)
            """,
            [str(info_path), str(ratings_path)],
        ).fetchall()
    finally:
        con.close()
    conn.execute("DELETE FROM cfb_team_season WHERE season = ?", (season,))
    conn.executemany(
        "INSERT OR REPLACE INTO cfb_team_season (team_id, season, school, conference, classification, net_z, net_rank, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(tid, season, school, conf, cls, net_z, rank, fetched_at) for tid, school, conf, cls, net_z, rank in rows],
    )
    return len(rows)
