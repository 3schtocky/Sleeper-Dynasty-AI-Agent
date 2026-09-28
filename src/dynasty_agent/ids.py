"""Player id crosswalk: Sleeper, ESPN (college and NFL), nflverse gsis,
and Pro Football Reference ids for one player, in one table.

Two keyless sources, merged, verified live before writing this:
DynastyProcess's db_playerids.csv is the primary (it carries sleeper_id,
which nflverse's players file does not), nflverse's players.parquet fills
any espn_id, pfr_id, or birth date DynastyProcess lacks for the same
gsis_id. Both are refreshed on every ingest; see migration 0006.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import duckdb

from dynasty_agent.config import NFLVERSE_CACHE_DIR
from dynasty_agent.db import utcnow
from dynasty_agent.nflverse import download

DYNASTYPROCESS_IDS_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"
NFLVERSE_PLAYERS_URL = "https://github.com/nflverse/nflverse-data/releases/download/players/players.parquet"


def _ensure_cached(url: str, filename: str, force: bool) -> Path:
    dest = NFLVERSE_CACHE_DIR / filename
    if dest.exists() and not force:
        return dest
    return download(url, dest)


MERGE_QUERY = """
    WITH dp AS (
        SELECT * FROM read_csv(?, all_varchar = true, nullstr = 'NA')
    ),
    np AS (
        SELECT gsis_id, CAST(espn_id AS VARCHAR) AS espn_id, pfr_id, display_name,
               position, CAST(birth_date AS VARCHAR) AS birth_date,
               draft_year, draft_round, draft_pick, college_name
        FROM read_parquet(?)
        WHERE gsis_id IS NOT NULL
    )
    SELECT coalesce(dp.gsis_id, np.gsis_id) AS gsis_id,
           coalesce(dp.espn_id, np.espn_id) AS espn_id,
           dp.sleeper_id,
           coalesce(dp.pfr_id, np.pfr_id) AS pfr_id,
           dp.cfbref_id,
           coalesce(dp.name, np.display_name) AS name,
           coalesce(dp.position, np.position) AS position,
           coalesce(dp.birthdate, np.birth_date) AS birthdate,
           CASE WHEN dp.birthdate IS NOT NULL THEN 'dynastyprocess'
                WHEN np.birth_date IS NOT NULL THEN 'nflverse_players' END AS birthdate_source,
           coalesce(try_cast(dp.draft_year AS INTEGER), np.draft_year) AS draft_year,
           coalesce(try_cast(dp.draft_round AS INTEGER), np.draft_round) AS draft_round,
           coalesce(try_cast(dp.draft_ovr AS INTEGER), np.draft_pick) AS draft_pick,
           coalesce(dp.college, np.college_name) AS college
    FROM dp
    FULL OUTER JOIN np ON np.gsis_id = dp.gsis_id
"""


def row_key(gsis_id: str | None, espn_id: str | None, sleeper_id: str | None) -> str | None:
    """player_ids' key: gsis_id when present, else a prefixed espn or
    sleeper id. None for a row with none of the three, which carries
    nothing this project can join on and is skipped."""
    if gsis_id:
        return gsis_id
    if espn_id:
        return f"espn:{espn_id}"
    if sleeper_id:
        return f"sleeper:{sleeper_id}"
    return None


def ingest_player_ids(conn: sqlite3.Connection, force: bool = False) -> int:
    """Refresh both sources, replace player_ids, and backfill cfb_athletes
    birth dates the ESPN college rosters lacked. Returns rows written."""
    dp_path = _ensure_cached(DYNASTYPROCESS_IDS_URL, "db_playerids.csv", force)
    np_path = _ensure_cached(NFLVERSE_PLAYERS_URL, "players.parquet", force)
    con = duckdb.connect()
    try:
        rows = con.execute(MERGE_QUERY, [str(dp_path), str(np_path)]).fetchall()
    finally:
        con.close()

    fetched_at = utcnow()
    by_key: dict[str, tuple] = {}
    for r in rows:
        key = row_key(r[0], r[1], r[2])
        if key is None:
            continue
        birthdate = r[7][:10] if r[7] else None
        by_key[key] = (key, *r[:7], birthdate, *r[8:], fetched_at)

    conn.execute("DELETE FROM player_ids")
    conn.executemany(
        """
        INSERT INTO player_ids (row_key, gsis_id, espn_id, sleeper_id, pfr_id, cfbref_id, name, position,
                                birthdate, birthdate_source, draft_year, draft_round, draft_pick, college, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        list(by_key.values()),
    )
    conn.execute(
        """
        UPDATE cfb_athletes
        SET date_of_birth = (SELECT pi.birthdate FROM player_ids pi WHERE pi.espn_id = cfb_athletes.athlete_id AND pi.birthdate IS NOT NULL LIMIT 1),
            dob_source = (SELECT pi.birthdate_source FROM player_ids pi WHERE pi.espn_id = cfb_athletes.athlete_id AND pi.birthdate IS NOT NULL LIMIT 1)
        WHERE date_of_birth IS NULL
          AND EXISTS (SELECT 1 FROM player_ids pi WHERE pi.espn_id = cfb_athletes.athlete_id AND pi.birthdate IS NOT NULL)
        """
    )
    conn.commit()
    return len(by_key)


def sleeper_id_for(conn: sqlite3.Connection, *, espn_id: str | None = None, gsis_id: str | None = None,
                   pfr_id: str | None = None) -> str | None:
    """The Sleeper player_id for whichever id is given, None if unmapped.
    Exactly one id should be passed."""
    for column, value in (("espn_id", espn_id), ("gsis_id", gsis_id), ("pfr_id", pfr_id)):
        if value:
            row = conn.execute(
                f"SELECT sleeper_id FROM player_ids WHERE {column} = ? AND sleeper_id IS NOT NULL LIMIT 1", (value,)
            ).fetchone()
            return row[0] if row else None
    return None
