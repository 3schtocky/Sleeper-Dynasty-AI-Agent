"""nflverse ingestion: play-by-play, weekly player stats, snap counts,
weekly rosters, and depth charts, pulled from the public nflverse-data
GitHub releases, cached locally as parquet, queried with DuckDB.

Verified against the live releases before writing this (asset names drift):
weekly player stats live under the "stats_player" release tag as
stats_player_week_{season}.parquet, not the older, frozen "player_stats" tag
(that one stopped updating after the 2024 season). Play-by-play already
carries per-play pass_oe / xpass, so team pass rate over expected is a
straight aggregate, no separate model needed.

Route participation and true yards-per-route-run are not in any of these
files, they are paywalled at PFF and Fantasy Points, confirmed by checking
nextgen_stats and pfr_advstats too. yards_per_route_run here is an estimate:
receiving yards divided by offensive snaps, not true routes run. It is
flagged is_estimated=1 in weekly_stats, and route_participation is left
NULL rather than faked from snap_share.

depth_charts is cached here for a later phase (Phase 2's situation score)
but not yet joined into weekly_stats; its snapshots are dated, not
week-numbered, and reconciling that is a situation-score problem, not a data
layer one.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import duckdb
import httpx

from dynasty_agent.config import NFLVERSE_CACHE_DIR
from dynasty_agent.db import utcnow
from dynasty_agent.metrics import (
    compute_fantasy_points,
    map_snapshot_to_week,
    weighted_opportunity,
    yards_per_route_run_estimate,
)

RELEASE_BASE = "https://github.com/nflverse/nflverse-data/releases/download"

# (release tag, filename template) for each file this module ingests.
FILES = {
    "stats_player_week": ("stats_player", "stats_player_week_{season}.parquet"),
    "snap_counts": ("snap_counts", "snap_counts_{season}.parquet"),
    "roster_weekly": ("weekly_rosters", "roster_weekly_{season}.parquet"),
    "depth_charts": ("depth_charts", "depth_charts_{season}.parquet"),
    "pbp": ("pbp", "play_by_play_{season}.parquet"),
}


def _url(kind: str, season: int) -> str:
    tag, template = FILES[kind]
    return f"{RELEASE_BASE}/{tag}/{template.format(season=season)}"


def ensure_cached(kind: str, season: int, force: bool = False) -> Path:
    """Download a file to data/nflverse/ if it is not already there. Returns
    the local path. Raises httpx.HTTPStatusError (404) if the season is not
    published yet, for example the current season before its first games."""
    NFLVERSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = NFLVERSE_CACHE_DIR / f"{kind}_{season}.parquet"
    if dest.exists() and not force:
        return dest

    url = _url(kind, season)
    with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
    return dest


def season_is_available(season: int) -> bool:
    """Whether nflverse has published stats_player_week for this season yet."""
    try:
        response = httpx.head(_url("stats_player_week", season), timeout=15.0, follow_redirects=True)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def derive_weekly_metrics(conn: sqlite3.Connection, season: int, scoring_settings: dict) -> int:
    """Join stats_player_week, snap_counts (via the roster_weekly id
    crosswalk), and team-week pass-rate-over-expected (from play-by-play)
    into one row per player per week, compute fantasy points from this
    league's own scoring, and upsert into weekly_stats. Returns row count."""

    stats_path = ensure_cached("stats_player_week", season)
    snaps_path = ensure_cached("snap_counts", season)
    roster_path = ensure_cached("roster_weekly", season)
    pbp_path = ensure_cached("pbp", season)

    query = """
        WITH crosswalk AS (
            SELECT DISTINCT season, week, gsis_id, sleeper_id, pfr_id
            FROM read_parquet(?)
            WHERE gsis_id IS NOT NULL
        ),
        snaps AS (
            SELECT season, week, pfr_player_id, offense_snaps, offense_pct
            FROM read_parquet(?)
        ),
        team_proe AS (
            SELECT season, posteam AS team, week, avg(pass_oe) AS team_pass_rate_over_expected
            FROM read_parquet(?)
            WHERE pass_oe IS NOT NULL AND posteam IS NOT NULL
            GROUP BY season, posteam, week
        ),
        touches AS (
            SELECT season, week, player_id,
                   count(*) FILTER (WHERE yardline_100 <= 20) AS red_zone_touches,
                   count(*) FILTER (WHERE yardline_100 <= 5) AS inside_five_touches
            FROM (
                SELECT season, week, yardline_100, rusher_player_id AS player_id
                FROM read_parquet(?) WHERE rush = 1
                UNION ALL
                SELECT season, week, yardline_100, receiver_player_id AS player_id
                FROM read_parquet(?) WHERE complete_pass = 1
            )
            WHERE player_id IS NOT NULL
            GROUP BY season, week, player_id
        )
        SELECT
            s.player_id AS gsis_id,
            cw.sleeper_id,
            s.season, s.week, s.team, s.position,
            s.targets, s.receptions, s.receiving_yards, s.receiving_tds,
            s.carries, s.rushing_yards, s.rushing_tds,
            s.attempts AS pass_attempts, s.completions, s.passing_yards, s.passing_tds,
            s.passing_interceptions,
            s.sack_fumbles_lost, s.rushing_fumbles_lost, s.receiving_fumbles_lost,
            s.passing_2pt_conversions, s.rushing_2pt_conversions, s.receiving_2pt_conversions,
            sn.offense_pct AS snap_share,
            sn.offense_snaps,
            s.target_share, s.air_yards_share, s.wopr,
            t.red_zone_touches, t.inside_five_touches,
            CASE WHEN s.position = 'QB' THEN s.carries END AS qb_rush_attempts_per_game,
            tp.team_pass_rate_over_expected
        FROM read_parquet(?) s
        LEFT JOIN crosswalk cw ON cw.season = s.season AND cw.week = s.week AND cw.gsis_id = s.player_id
        LEFT JOIN snaps sn ON sn.season = s.season AND sn.week = s.week AND sn.pfr_player_id = cw.pfr_id
        LEFT JOIN team_proe tp ON tp.season = s.season AND tp.week = s.week AND tp.team = s.team
        LEFT JOIN touches t ON t.season = s.season AND t.week = s.week AND t.player_id = s.player_id
        WHERE s.season_type = 'REG' AND s.player_id IS NOT NULL
    """
    con = duckdb.connect()
    try:
        rows = con.execute(
            query,
            [str(roster_path), str(snaps_path), str(pbp_path), str(pbp_path), str(pbp_path), str(stats_path)],
        ).fetchall()
        columns = [d[0] for d in con.description]
    finally:
        con.close()

    fetched_at = utcnow()
    upsert_rows = []
    for values in rows:
        record = dict(zip(columns, values))

        fumbles_lost_total = (
            (record["sack_fumbles_lost"] or 0)
            + (record["rushing_fumbles_lost"] or 0)
            + (record["receiving_fumbles_lost"] or 0)
        )
        stat_line = {
            "passing_yards": record["passing_yards"],
            "passing_tds": record["passing_tds"],
            "passing_interceptions": record["passing_interceptions"],
            "passing_2pt_conversions": record["passing_2pt_conversions"],
            "rushing_yards": record["rushing_yards"],
            "rushing_tds": record["rushing_tds"],
            "rushing_2pt_conversions": record["rushing_2pt_conversions"],
            "receptions": record["receptions"],
            "receiving_yards": record["receiving_yards"],
            "receiving_tds": record["receiving_tds"],
            "receiving_2pt_conversions": record["receiving_2pt_conversions"],
            "fumbles_lost": fumbles_lost_total,
        }
        fantasy_points = compute_fantasy_points(stat_line, scoring_settings)
        wo = weighted_opportunity(record["carries"], record["targets"])
        yprr_est = yards_per_route_run_estimate(record["receiving_yards"], record["offense_snaps"])
        player_id = record["sleeper_id"] or record["gsis_id"]

        upsert_rows.append(
            (
                player_id, record["gsis_id"], str(record["season"]), record["week"], record["team"], record["position"],
                record["targets"], record["receptions"], record["receiving_yards"], record["receiving_tds"],
                record["carries"], record["rushing_yards"], record["rushing_tds"],
                record["pass_attempts"], record["completions"], record["passing_yards"], record["passing_tds"],
                record["passing_interceptions"], fumbles_lost_total,
                record["snap_share"], None,
                record["target_share"], record["air_yards_share"], record["wopr"],
                yprr_est, wo,
                record["red_zone_touches"], record["inside_five_touches"],
                record["qb_rush_attempts_per_game"], record["team_pass_rate_over_expected"],
                fantasy_points, 1, fetched_at,
            )
        )

    conn.executemany(
        """
        INSERT INTO weekly_stats (
            player_id, gsis_id, season, week, team, position,
            targets, receptions, receiving_yards, receiving_tds,
            carries, rushing_yards, rushing_tds,
            pass_attempts, completions, passing_yards, passing_tds,
            interceptions, fumbles_lost,
            snap_share, route_participation, target_share, air_yards_share, wopr,
            yards_per_route_run, weighted_opportunity,
            red_zone_touches, inside_five_touches, qb_rush_attempts_per_game, team_pass_rate_over_expected,
            fantasy_points, is_estimated, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (player_id, season, week) DO UPDATE SET
            gsis_id = excluded.gsis_id, team = excluded.team, position = excluded.position,
            targets = excluded.targets, receptions = excluded.receptions, receiving_yards = excluded.receiving_yards,
            receiving_tds = excluded.receiving_tds, carries = excluded.carries, rushing_yards = excluded.rushing_yards,
            rushing_tds = excluded.rushing_tds, pass_attempts = excluded.pass_attempts, completions = excluded.completions,
            passing_yards = excluded.passing_yards, passing_tds = excluded.passing_tds, interceptions = excluded.interceptions,
            fumbles_lost = excluded.fumbles_lost, snap_share = excluded.snap_share,
            route_participation = excluded.route_participation, target_share = excluded.target_share,
            air_yards_share = excluded.air_yards_share, wopr = excluded.wopr,
            yards_per_route_run = excluded.yards_per_route_run, weighted_opportunity = excluded.weighted_opportunity,
            red_zone_touches = excluded.red_zone_touches, inside_five_touches = excluded.inside_five_touches,
            qb_rush_attempts_per_game = excluded.qb_rush_attempts_per_game,
            team_pass_rate_over_expected = excluded.team_pass_rate_over_expected,
            fantasy_points = excluded.fantasy_points, is_estimated = excluded.is_estimated, fetched_at = excluded.fetched_at
        """,
        upsert_rows,
    )
    conn.commit()
    return len(upsert_rows)


def derive_depth_chart_weekly(conn: sqlite3.Connection, season: int) -> int:
    """Map depth_charts' near-daily snapshots onto the NFL week each one
    precedes (see metrics.map_snapshot_to_week), scoped to the offensive
    skill positions this league starts. Where several snapshots fall in the
    same week's prep window, the latest one wins, so each (week, team,
    position, player) row reflects the freshest depth chart available before
    that week's games. Upserts into depth_chart_weekly, returns row count."""

    depth_path = ensure_cached("depth_charts", season)
    pbp_path = ensure_cached("pbp", season)

    con = duckdb.connect()
    try:
        week_start_rows = con.execute(
            "SELECT week, MIN(game_date) FROM read_parquet(?) GROUP BY week ORDER BY week",
            [str(pbp_path)],
        ).fetchall()
        depth_rows = con.execute(
            """
            SELECT dt, team, player_name, gsis_id, pos_abb, pos_rank
            FROM read_parquet(?)
            WHERE pos_abb IN ('QB', 'RB', 'WR', 'TE', 'FB')
            ORDER BY dt ASC
            """,
            [str(depth_path)],
        ).fetchall()
    finally:
        con.close()

    week_starts = [(week, datetime.fromisoformat(str(start)).date()) for week, start in week_start_rows]

    fetched_at = utcnow()
    upsert_rows = []
    for dt_value, team, player_name, gsis_id, pos_abb, pos_rank in depth_rows:
        snapshot_dt = dt_value if isinstance(dt_value, datetime) else datetime.fromisoformat(str(dt_value))
        week = map_snapshot_to_week(snapshot_dt.date(), week_starts)
        if week is None:
            continue
        upsert_rows.append(
            (str(season), week, team, gsis_id, player_name, pos_abb, pos_rank, snapshot_dt.isoformat(), fetched_at)
        )

    # Rows are in ascending dt order, so within a week the later executemany
    # entry (the freshest snapshot) is the one ON CONFLICT keeps.
    conn.executemany(
        """
        INSERT INTO depth_chart_weekly (season, week, team, gsis_id, player_name, pos_abb, pos_rank, snapshot_dt, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (season, week, team, pos_abb, gsis_id) DO UPDATE SET
            pos_rank = excluded.pos_rank, player_name = excluded.player_name,
            snapshot_dt = excluded.snapshot_dt, fetched_at = excluded.fetched_at
        """,
        upsert_rows,
    )
    conn.commit()
    return len(upsert_rows)


def ingest_season(conn: sqlite3.Connection, season: int, scoring_settings: dict) -> str:
    """Cache this season's nflverse files and derive weekly_stats. Returns a
    human-readable summary. If the season is not published yet, the current
    season before its first games, says so instead of raising."""
    if not season_is_available(season):
        return f"nflverse has not published {season} stats yet. Nothing ingested."
    row_count = derive_weekly_metrics(conn, season, scoring_settings)
    depth_chart_rows = derive_depth_chart_weekly(conn, season)
    return (
        f"Ingested {row_count} player-week rows and {depth_chart_rows} depth chart week-rows "
        f"for the {season} season."
    )
