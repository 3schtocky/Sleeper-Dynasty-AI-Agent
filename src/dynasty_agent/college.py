"""Phase 4 completion: college production data, pulled from sportsdataverse's
public sportsdataverse-data GitHub releases (the same no-key, static-file
pattern nflverse.py and prospects.py already use, a different GitHub org).
Unblocks the college-production question CLAUDE.md/PLANNING.md left open:
the College Football Data API itself requires an email-registered key and
was rejected for exactly that reason (see PLANNING.md); sportsdataverse
republishes CFBD's own data without requiring the consumer to hold a key.

Verified against the live releases before writing this, the same discipline
nflverse.py and prospects.py's own docstrings already establish, asset
names and column shapes are not assumed here, they drift and differ from
what any plan guesses. Real, confirmed findings:

- cfb_recruits, cfb_team_talent, cfb_returning_production, and
  cfb_team_info are all per-season files (cfb_team_info_{season}.parquet
  and so on), not whole-history flat files like nflverse's draft_picks or
  combine.
- None of cfb_recruits, cfb_team_talent, or cfb_returning_production carry
  a plain school name field, they key off a numeric team_id. cfb_team_info
  (team_id, school) is the real crosswalk, confirmed live, not guessed.
  A second real gotcha found the same way TEAM_ALIASES was: cfb_team_info's
  own team_id column comes back as an INTEGER, while the other three files'
  team_id columns are VARCHAR, same real values, different types, so every
  lookup here is done on the string form of both sides.
- College box-score production (dominator rating, breakout age) is NOT
  ingested by this module, a confirmed dead end, not a guessed workaround:
  see the comment above ingest_college_season for the full finding.
  metrics.dominator_rating/age_in_college_season/breakout_age stay in this
  codebase regardless, pure and tested, ready for whenever a real, joinable
  college box-score source turns up.

School-name crosswalk (normalize_school_name below): nfl_draft_picks.college
ships PFR-style abbreviated names ("Ohio St.", "Boston Col.", "Arizona
St."), a third naming convention distinct from both cfb_team_info.school
("Ohio State", "Boston College") and cfb_recruits/cfb_team_talent's own
"School Mascot" team field. Built by diffing the two real value sets
directly for every college that shows up in a 2015-or-later real draft
pick, the same process TEAM_ALIASES and DRAFT_TEAM_ALIASES already used,
not guessed in advance: a generic " St." -> " State" / " Col." -> " College"
suffix expansion resolves the large majority (confirmed: 44 of 74 real
mismatches in that sample), and SCHOOL_ALIASES below holds the smaller set
of genuine one-off differences, confirmed the same way. Older, very small,
or non-US schools (junior colleges, Division III programs, Canadian
schools) are not in CFBD's coverage at all and stay unresolved, a real
coverage gap, not a mapping bug, and not a problem for this project's
actual use: a rookie prospect board only needs recent draft classes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import duckdb
import httpx

from dynasty_agent.config import NFLVERSE_CACHE_DIR
from dynasty_agent.db import utcnow

RELEASE_BASE = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"

# (release tag, filename template) for each per-season file this module ingests.
FILES = {
    "cfb_team_info": ("cfb_team_info", "cfb_team_info_{season}.parquet"),
    "cfb_recruits": ("cfb_recruits", "cfb_recruits_{season}.parquet"),
    "cfb_team_talent": ("cfb_team_talent", "cfb_team_talent_{season}.parquet"),
    "cfb_returning_production": ("cfb_returning_production", "cfb_returning_production_{season}.parquet"),
}

# Genuine one-off name differences between nfl_draft_picks.college (PFR
# style) and cfb_team_info.school (CFBD style), confirmed live. Keyed on the
# RAW nfl_draft_picks.college string, checked before suffix expansion runs.
SCHOOL_ALIASES: dict[str, str] = {
    "Hawaii": "Hawai'i",
    "Mississippi": "Ole Miss",
    "Connecticut": "UConn",
    "Miami (FL)": "Miami",
    "Central Florida": "UCF",
    "North Carolina St.": "NC State",
    "Sam Houston St.": "Sam Houston",
}


def normalize_school_name(name: str | None) -> str | None:
    """nfl_draft_picks.college's PFR-style name, translated toward
    cfb_team_info.school's CFBD-style name. Checks SCHOOL_ALIASES first for
    the confirmed one-off cases, then falls back to a generic ' St.' ->
    ' State' / ' Col.' -> ' College' suffix expansion. Passes an unresolved
    name through unchanged rather than guessing further: a school this
    doesn't resolve just won't join to a college_production_season row,
    which prospect_board reports as no data, never a wrong one."""
    if name is None:
        return None
    if name in SCHOOL_ALIASES:
        return SCHOOL_ALIASES[name]
    if name.endswith(" St."):
        return name[: -len(" St.")] + " State"
    if name.endswith(" Col."):
        return name[: -len(" Col.")] + " College"
    return name


def _url(kind: str, season: int) -> str:
    tag, template = FILES[kind]
    return f"{RELEASE_BASE}/{tag}/{template.format(season=season)}"


def ensure_cached(kind: str, season: int, force: bool = False) -> Path:
    """Download a file to data/nflverse/ if not already cached. Same
    download-then-cache pattern nflverse.ensure_cached uses, kept as its own
    copy here rather than imported: the URL shape (per-season, but a
    different GitHub org and tag naming scheme) is genuinely different, the
    same reasoning prospects.py already gives for not sharing
    nflverse.ensure_cached itself. Prefixed college_ so these never collide
    with nflverse's own cached filenames in the same directory."""
    NFLVERSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = NFLVERSE_CACHE_DIR / f"college_{kind}_{season}.parquet"
    if dest.exists() and not force:
        return dest

    url = _url(kind, season)
    with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
    return dest


def _team_id_to_school(con: duckdb.DuckDBPyConnection, team_info_path: Path) -> dict[str, str]:
    rows = con.execute("SELECT team_id, school FROM read_parquet(?)", [str(team_info_path)]).fetchall()
    # Confirmed live: cfb_team_info.team_id comes back as an actual INTEGER,
    # while cfb_recruits/cfb_team_talent/cfb_returning_production's own
    # team_id columns are VARCHAR, same real values ("2509" == 2509), just a
    # different type per file. Keyed as str on both sides here and at every
    # lookup site so this join isn't silently empty from a type mismatch.
    return {str(team_id): school for team_id, school in rows}


def ingest_recruits(conn: sqlite3.Connection, season: int, force: bool = False) -> int:
    """Cache and upsert one recruiting class into cfb_recruits, school
    resolved from team_id via cfb_team_info. Returns row count."""
    recruits_path = ensure_cached("cfb_recruits", season, force=force)
    team_info_path = ensure_cached("cfb_team_info", season, force=force)

    con = duckdb.connect()
    try:
        team_id_to_school = _team_id_to_school(con, team_info_path)
        rows = con.execute(
            "SELECT recruit_id, season, player_name, position, team_id, stars, grade FROM read_parquet(?) "
            "WHERE recruit_id IS NOT NULL AND player_name IS NOT NULL",
            [str(recruits_path)],
        ).fetchall()
    finally:
        con.close()

    fetched_at = utcnow()
    upsert_rows = [
        (recruit_id, season, player_name, position, team_id_to_school.get(str(team_id)), stars, grade, fetched_at)
        for recruit_id, season, player_name, position, team_id, stars, grade in rows
    ]

    conn.executemany(
        """
        INSERT INTO cfb_recruits (recruit_id, season, player_name, position, school, stars, grade, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (recruit_id) DO UPDATE SET
            season = excluded.season, player_name = excluded.player_name, position = excluded.position,
            school = excluded.school, stars = excluded.stars, grade = excluded.grade, fetched_at = excluded.fetched_at
        """,
        upsert_rows,
    )
    conn.commit()
    return len(upsert_rows)


def ingest_team_talent(conn: sqlite3.Connection, season: int, force: bool = False) -> int:
    """Cache and upsert one season of team talent composite into
    cfb_team_talent, school resolved from team_id via cfb_team_info, not
    this file's own "team" column: that field ships in "School Mascot"
    format ("Alabama Crimson Tide"), a fourth naming convention, confirmed
    live, and would silently break the join to nfl_draft_picks.college via
    normalize_school_name if stored as-is. Returns row count."""
    path = ensure_cached("cfb_team_talent", season, force=force)
    team_info_path = ensure_cached("cfb_team_info", season, force=force)

    con = duckdb.connect()
    try:
        team_id_to_school = _team_id_to_school(con, team_info_path)
        rows = con.execute(
            "SELECT season, team_id, talent_composite, talent_rank FROM read_parquet(?) WHERE team_id IS NOT NULL",
            [str(path)],
        ).fetchall()
    finally:
        con.close()

    fetched_at = utcnow()
    upsert_rows = [
        (season, team_id, team_id_to_school.get(str(team_id)), talent_composite, talent_rank, fetched_at)
        for season, team_id, talent_composite, talent_rank in rows
    ]

    conn.executemany(
        """
        INSERT INTO cfb_team_talent (season, team_id, school, talent_composite, talent_rank, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (season, team_id) DO UPDATE SET
            school = excluded.school, talent_composite = excluded.talent_composite,
            talent_rank = excluded.talent_rank, fetched_at = excluded.fetched_at
        """,
        upsert_rows,
    )
    conn.commit()
    return len(upsert_rows)


def ingest_returning_production(conn: sqlite3.Connection, season: int, force: bool = False) -> int:
    """Cache and upsert one season of returning-production into
    cfb_returning_production, school resolved from team_id via
    cfb_team_info (this file carries no school field of its own at all).
    Returns row count."""
    prod_path = ensure_cached("cfb_returning_production", season, force=force)
    team_info_path = ensure_cached("cfb_team_info", season, force=force)

    con = duckdb.connect()
    try:
        team_id_to_school = _team_id_to_school(con, team_info_path)
        rows = con.execute(
            "SELECT season, team_id, off_returning, n_returning FROM read_parquet(?) WHERE team_id IS NOT NULL",
            [str(prod_path)],
        ).fetchall()
    finally:
        con.close()

    fetched_at = utcnow()
    upsert_rows = [
        (season, team_id, team_id_to_school.get(str(team_id)), off_returning, n_returning, fetched_at)
        for season, team_id, off_returning, n_returning in rows
    ]

    conn.executemany(
        """
        INSERT INTO cfb_returning_production (season, team_id, school, off_returning, n_returning, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (season, team_id) DO UPDATE SET
            school = excluded.school, off_returning = excluded.off_returning,
            n_returning = excluded.n_returning, fetched_at = excluded.fetched_at
        """,
        upsert_rows,
    )
    conn.commit()
    return len(upsert_rows)


# College box-score production (dominator rating, breakout age) is NOT
# ingested this pass, a real, live-verified dead end, not a guessed
# workaround: ncaa_mfb_player_stats' own team_id column is a per-game
# participation id, not a stable school id, confirmed by querying the live
# file directly, every real value appears in exactly one contest, and each
# contest has exactly two team_ids, one per side. It cannot be joined to a
# real school at all, and summing it across a season (the whole point of
# college_production_season) is not meaningful either, since there is no
# way to tell which games belong to the same real team. The two dedicated
# ESPN college boxscore release tags this project also checked live,
# espn_cfb_player_boxscores and espn_cfb_team_boxscores, exist but carry no
# published assets at all. metrics.dominator_rating, age_in_college_season,
# and breakout_age stay in this codebase, pure, tested, and ready to use
# the moment a real, joinable college box-score source turns up; this is an
# open question, left open deliberately, the same honesty standard
# PLANNING.md already applied to the original CFBD-key rejection, not
# silently worked around with a guessed team match. college_production_season
# (migration 0005) stays defined and empty for that future source.


def ingest_college_season(conn: sqlite3.Connection, season: int, force: bool = False) -> str:
    """Ingest recruits, team talent, and returning production for one
    college football season. Returns a human-readable summary."""
    recruit_rows = ingest_recruits(conn, season, force=force)
    talent_rows = ingest_team_talent(conn, season, force=force)
    returning_rows = ingest_returning_production(conn, season, force=force)
    return f"{season}: {recruit_rows} recruits, {talent_rows} team talent rows, {returning_rows} returning production rows."


def ingest_college_data(conn: sqlite3.Connection, start_season: int, end_season: int, force: bool = False) -> str:
    """Ingest ingest_college_season across a range of seasons."""
    summaries = [ingest_college_season(conn, season, force=force) for season in range(start_season, end_season + 1)]
    return "\n".join(summaries)
