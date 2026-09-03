"""Phase 5: real per-week historical injury status, from nflverse's public
`injuries` release (2009+, per-season files, same GitHub-release pattern
`nflverse.py` already uses). Needed because the `players` table only ever
holds TODAY's injury status, useless for backtesting a real 2015 week.

Verified against the live files before writing this: real columns are
`season, week, team, gsis_id, position, report_status, practice_status`.
Real `report_status` values are `Out`, `Doubtful`, `Questionable`,
`Probable` (confirmed live, querying a full season), three of which
already match `metrics.INJURY_MEAN_MULTIPLIER`'s keys verbatim, so no
normalization function is needed here, unlike the several real crosswalks
Phase 4 needed. `Probable` has no entry in that dict and correctly falls
through to its existing neutral 1.0 default: the NFL itself retired
"Probable" in 2016 for meaning, in practice, "will play." One real gotcha:
`season` and `week` come back as FLOAT from the raw file (`2015.0`), not
INTEGER, cast explicitly here rather than assumed.
"""

from __future__ import annotations

import sqlite3

import duckdb

from dynasty_agent import nflverse
from dynasty_agent.db import utcnow


def ingest_injuries(conn: sqlite3.Connection, season: int) -> int:
    """Cache and upsert one season's real weekly injury designations into
    nfl_injuries. Returns row count."""
    path = nflverse.ensure_cached("injuries", season)
    con = duckdb.connect()
    try:
        rows = con.execute(
            """
            SELECT CAST(season AS INTEGER), CAST(week AS INTEGER), team, gsis_id, position, report_status
            FROM read_parquet(?)
            WHERE gsis_id IS NOT NULL AND week IS NOT NULL
            """,
            [str(path)],
        ).fetchall()
    finally:
        con.close()

    fetched_at = utcnow()
    upsert_rows = [(s, w, gsis_id, team, position, status, fetched_at) for s, w, team, gsis_id, position, status in rows]

    conn.executemany(
        """
        INSERT INTO nfl_injuries (season, week, gsis_id, team, position, report_status, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (season, week, gsis_id) DO UPDATE SET
            team = excluded.team, position = excluded.position,
            report_status = excluded.report_status, fetched_at = excluded.fetched_at
        """,
        upsert_rows,
    )
    conn.commit()
    return len(upsert_rows)


def report_status_for_week(conn: sqlite3.Connection, season: int, week: int, gsis_id: str) -> str | None:
    """A player's real injury report status for one specific historical
    week. None means either healthy or simply not on that week's report,
    the same "absence means healthy" convention players.injury_status
    already uses elsewhere in this project."""
    row = conn.execute(
        "SELECT report_status FROM nfl_injuries WHERE season = ? AND week = ? AND gsis_id = ?",
        (season, week, gsis_id),
    ).fetchone()
    return row["report_status"] if row else None
