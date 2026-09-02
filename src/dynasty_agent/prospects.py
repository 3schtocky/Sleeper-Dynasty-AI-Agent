"""Phase 4 data layer: real NFL draft capital and athletic testing, pulled
from nflverse's draft_picks and combine releases.

Verified against the live releases before writing this, the same discipline
nflverse.py used for its own release names: both are single flat files
covering every season (draft_picks back to 1980, combine back to 2000), not
per-season files like stats_player_week, so ensure_cached here takes no
season argument. draft_picks already has the real, already-drafted 2026
class; it has nothing for 2027, because that draft has not happened yet.
Same for combine (tops out at 2026; the 2027 combine runs next February).
This league's next real rookie draft is the 2027 class, so this module's
tables stay empty for the players who actually matter to the next draft
until spring 2027. Confirmed live, not assumed: see PLANNING.md Phase 4.

Real, live-verified team-code mismatch, the same shape of bug already found
once in valuation.py (Sleeper's "LAR" vs nflverse's "LA"): nflverse's
draft_picks file itself ships PFR-style team codes (GNB, KAN, LAR, LVR,
NOR, NWE, SFO, TAM for the 8 teams that differ; the other 24 already match),
not the GB/KC/LA/LV/NO/NE/SF/TB codes weekly_stats and everything else in
this project use. to_nflverse_team_from_draft_code normalizes at ingest
time, so nfl_draft_picks.team is directly comparable to weekly_stats.team
without a second lookup at read time. combine.draft_team ships as a full
franchise name ("San Francisco 49ers") instead, confirmed live; it is kept
as informational only, not normalized or treated as a second source of
truth, since nfl_draft_picks.team (crosswalked from the same PFR pick) is
the authoritative landing spot.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import duckdb
import httpx

from dynasty_agent.config import NFLVERSE_CACHE_DIR
from dynasty_agent.db import utcnow

RELEASE_BASE = "https://github.com/nflverse/nflverse-data/releases/download"

# (release tag, filename) for each flat file this module ingests.
FILES = {
    "draft_picks": ("draft_picks", "draft_picks.parquet"),
    "combine": ("combine", "combine.parquet"),
}

# PFR-style codes draft_picks.team actually ships, mapped to this project's
# standard nflverse team code. Confirmed live against every distinct code in
# the real file (season >= 2020): only these 8 of 32 differ, the rest pass
# through unchanged.
DRAFT_TEAM_ALIASES: dict[str, str] = {
    "GNB": "GB",
    "KAN": "KC",
    "LAR": "LA",
    "LVR": "LV",
    "NOR": "NO",
    "NWE": "NE",
    "SFO": "SF",
    "TAM": "TB",
}


def to_nflverse_team_from_draft_code(team: str | None) -> str | None:
    if team is None:
        return None
    return DRAFT_TEAM_ALIASES.get(team, team)


def _url(kind: str) -> str:
    tag, filename = FILES[kind]
    return f"{RELEASE_BASE}/{tag}/{filename}"


def ensure_cached(kind: str, force: bool = False) -> Path:
    """Download a file to data/nflverse/ if it is not already there. Returns
    the local path. Unlike nflverse.ensure_cached, these files are not
    season-scoped: one download covers every season, re-run with force=True
    to pick up nflverse's periodic updates (draft_picks and combine are both
    updated in place, not re-released under a new tag)."""
    NFLVERSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = NFLVERSE_CACHE_DIR / f"{kind}.parquet"
    if dest.exists() and not force:
        return dest

    url = _url(kind)
    with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
    return dest


def ingest_draft_picks(conn: sqlite3.Connection, force: bool = False) -> int:
    """Cache and upsert every real NFL draft pick into nfl_draft_picks, team
    normalized to this project's standard nflverse code. Returns row count."""
    path = ensure_cached("draft_picks", force=force)
    con = duckdb.connect()
    try:
        rows = con.execute(
            """
            SELECT season, round, pick, team, gsis_id, pfr_player_id, cfb_player_id,
                   pfr_player_name, position, college, age
            FROM read_parquet(?)
            WHERE season IS NOT NULL AND round IS NOT NULL AND pick IS NOT NULL
            """,
            [str(path)],
        ).fetchall()
    finally:
        con.close()

    fetched_at = utcnow()
    upsert_rows = [
        (
            season, round_, pick, to_nflverse_team_from_draft_code(team), gsis_id,
            pfr_player_id, cfb_player_id, player_name, position, college, age, fetched_at,
        )
        for season, round_, pick, team, gsis_id, pfr_player_id, cfb_player_id, player_name, position, college, age in rows
    ]

    conn.executemany(
        """
        INSERT INTO nfl_draft_picks (
            season, round, pick, team, gsis_id, pfr_player_id, cfb_player_id,
            player_name, position, college, age, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (season, round, pick) DO UPDATE SET
            team = excluded.team, gsis_id = excluded.gsis_id,
            pfr_player_id = excluded.pfr_player_id, cfb_player_id = excluded.cfb_player_id,
            player_name = excluded.player_name, position = excluded.position,
            college = excluded.college, age = excluded.age, fetched_at = excluded.fetched_at
        """,
        upsert_rows,
    )
    conn.commit()
    return len(upsert_rows)


def ingest_combine(conn: sqlite3.Connection, force: bool = False) -> int:
    """Cache and upsert every real combine testing result into nfl_combine.
    draft_team is kept as-is (a full franchise name, not a code) since it is
    informational only, see the module docstring. Returns the number of raw
    rows processed, which can run a few rows ahead of the table's final
    count: confirmed live, 3 (season, pfr_id) pairs repeat in nflverse's own
    file (a real PFR id collision on their end, not an ingestion bug here),
    and the primary key's ON CONFLICT keeps the later one."""
    path = ensure_cached("combine", force=force)
    con = duckdb.connect()
    try:
        rows = con.execute(
            """
            SELECT season, draft_year, draft_team, draft_round, draft_ovr,
                   pfr_id, cfb_id, player_name, pos, school,
                   ht, wt, forty, bench, vertical, broad_jump, cone, shuttle
            FROM read_parquet(?)
            WHERE season IS NOT NULL AND pfr_id IS NOT NULL
            """,
            [str(path)],
        ).fetchall()
    finally:
        con.close()

    fetched_at = utcnow()
    upsert_rows = [tuple(r) + (fetched_at,) for r in rows]

    conn.executemany(
        """
        INSERT INTO nfl_combine (
            season, draft_year, draft_team, draft_round, draft_ovr,
            pfr_id, cfb_id, player_name, position, college,
            height_in, weight_lb, forty, bench, vertical, broad_jump, cone, shuttle, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (season, pfr_id) DO UPDATE SET
            draft_year = excluded.draft_year, draft_team = excluded.draft_team,
            draft_round = excluded.draft_round, draft_ovr = excluded.draft_ovr,
            cfb_id = excluded.cfb_id, player_name = excluded.player_name,
            position = excluded.position, college = excluded.college,
            height_in = excluded.height_in, weight_lb = excluded.weight_lb,
            forty = excluded.forty, bench = excluded.bench, vertical = excluded.vertical,
            broad_jump = excluded.broad_jump, cone = excluded.cone, shuttle = excluded.shuttle,
            fetched_at = excluded.fetched_at
        """,
        upsert_rows,
    )
    conn.commit()
    return len(upsert_rows)


def ingest_draft_data(conn: sqlite3.Connection, force: bool = False) -> str:
    """Ingest both nflverse draft_picks and combine. Returns a human-readable
    summary. Both are whole-history files, there is no season to check for
    availability the way nflverse.ingest_season does."""
    pick_rows = ingest_draft_picks(conn, force=force)
    combine_rows = ingest_combine(conn, force=force)
    return f"Ingested {pick_rows} real NFL draft picks and {combine_rows} combine testing rows."
