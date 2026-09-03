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

from dynasty_agent import valuation
from dynasty_agent.config import NFLVERSE_CACHE_DIR
from dynasty_agent.db import utcnow
from dynasty_agent.metrics import COMBINE_METRICS_BY_POSITION, athleticism_score

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


# -- prospect board -----------------------------------------------------------


def _combine_population(conn: sqlite3.Connection, position: str) -> dict[str, list[float | None]]:
    """Real nfl_combine drill values for every tested prospect at this
    position, across the whole cached history, the population
    athleticism_score percentile-ranks one candidate against."""
    population: dict[str, list[float | None]] = {}
    for drill in COMBINE_METRICS_BY_POSITION.get(position, ()):
        rows = conn.execute(f"SELECT {drill} FROM nfl_combine WHERE position = ?", (position,)).fetchall()
        population[drill] = [r[0] for r in rows]
    return population


def _post_draft_board(conn: sqlite3.Connection, draft_year: int, position: str | None, limit: int) -> list[dict]:
    """Real draft capital (round, pick) plus athletic testing. Requires
    nfl_draft_picks to actually have rows for draft_year, raises rather than
    returning an empty board when that draft hasn't happened yet or hasn't
    been ingested."""
    query = "SELECT * FROM nfl_draft_picks WHERE season = ?"
    params: list = [draft_year]
    if position:
        query += " AND position = ?"
        params.append(position)
    query += " ORDER BY round, pick"
    picks = conn.execute(query, params).fetchall()
    if not picks:
        raise ValueError(
            f"No real draft picks for {draft_year}. Either that draft hasn't happened yet, or "
            f"`dynasty-agent ingest-draft-data` hasn't been run."
        )

    population_cache: dict[str, dict] = {}
    board = []
    for pick in picks:
        pos = pick["position"] or ""
        combine_row = conn.execute(
            "SELECT * FROM nfl_combine WHERE pfr_id = ? ORDER BY season DESC LIMIT 1", (pick["pfr_player_id"],)
        ).fetchone()

        athleticism = None
        if combine_row is not None:
            if pos not in population_cache:
                population_cache[pos] = _combine_population(conn, pos)
            player_metrics = {drill: combine_row[drill] for drill in COMBINE_METRICS_BY_POSITION.get(pos, ())}
            athleticism = athleticism_score(pos, player_metrics, population_cache[pos])

        board.append(
            {
                "player_name": pick["player_name"],
                "position": pos,
                "college": pick["college"],
                "round": pick["round"],
                "pick": pick["pick"],
                "team": pick["team"],
                "age_at_draft": pick["age"],
                "athleticism_score": athleticism,
            }
        )
    return board[:limit]


def _pre_draft_board(conn: sqlite3.Connection, draft_year: int, position: str | None, limit: int) -> list[dict]:
    """Ranks by real 247Sports composite recruiting grade (cfb_recruits) in
    the most recently ingested recruiting class at or before draft_year.

    College box-score production (dominator rating, breakout age) is NOT
    part of this ranking, a confirmed dead end, not a guessed workaround:
    see the comment above college.ingest_college_season for the full
    finding. metrics.dominator_rating/breakout_age stay in this codebase,
    tested and ready, for whenever a real, joinable college box-score
    source turns up; dominator_rating and breakout_age are reported here as
    None, not silently dropped from the shape, so a caller can see the
    field exists and is not yet computed rather than inferring its absence.

    Not filtered by actual declared-for-the-draft status either, this
    project has no source for that: recruiting-class year is a rough
    multi-year-early proxy for an eventual draft class, not a precise
    eligibility list. Stated here, not glossed over."""
    season_row = conn.execute(
        "SELECT max(season) AS season FROM cfb_recruits WHERE season <= ?", (draft_year,)
    ).fetchone()
    season = season_row["season"] if season_row else None
    if season is None:
        raise ValueError(
            f"No recruiting data for {draft_year} or earlier. Run "
            f"`dynasty-agent ingest-college-data --start-season <N> --end-season {draft_year}` first."
        )

    query = "SELECT * FROM cfb_recruits WHERE season = ? AND grade IS NOT NULL"
    params: list = [season]
    if position:
        query += " AND position = ?"
        params.append(position)
    query += " ORDER BY grade DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()

    return [
        {
            "player_name": row["player_name"],
            "position": row["position"],
            "school": row["school"],
            "season": row["season"],
            "recruit_stars": row["stars"],
            "recruit_grade": row["grade"],
            "dominator_rating": None,
            "breakout_age": None,
        }
        for row in rows
    ]


def prospect_board(
    conn: sqlite3.Connection, draft_year: int, mode: str, position: str | None = None, limit: int = 20
) -> list[dict]:
    """mode: 'post-draft' (real draft capital, landing spot, athletic
    testing) or 'pre-draft' (college production and recruiting pedigree).
    Raises ValueError if the requested mode has no real data for draft_year
    yet, never returns a guessed or silently empty board."""
    if mode == "post-draft":
        return _post_draft_board(conn, draft_year, position, limit)
    if mode == "pre-draft":
        return _pre_draft_board(conn, draft_year, position, limit)
    raise ValueError(f"mode must be 'pre-draft' or 'post-draft', got {mode!r}")


def taxi_stash_recommendations(conn: sqlite3.Connection, roster_id: int, draft_year: int) -> dict:
    """Real open taxi slots (3 minus this roster's current taxi count) and
    which taxi-eligible rostered rookies (years_exp <= 1, from the real
    players table) rank highest on the post-draft prospect_board right
    now."""
    taxi_count = conn.execute(
        "SELECT count(*) AS n FROM roster_players WHERE roster_id = ? AND slot = 'taxi'", (roster_id,)
    ).fetchone()["n"]
    open_slots = max(0, 3 - taxi_count)

    rookies = conn.execute(
        """
        SELECT p.player_id, p.full_name, p.position FROM roster_players rp
        JOIN players p ON p.player_id = rp.player_id
        WHERE rp.roster_id = ? AND rp.slot IN ('bench', 'taxi') AND p.years_exp IS NOT NULL AND p.years_exp <= 1
        """,
        (roster_id,),
    ).fetchall()

    board_by_name = {}
    if rookies:
        board_by_name = {row["player_name"]: row for row in _post_draft_board(conn, draft_year, None, 500)}

    ranked = sorted(
        rookies,
        key=lambda r: (
            board_by_name.get(r["full_name"], {}).get("round") or 99,
            board_by_name.get(r["full_name"], {}).get("pick") or 999,
        ),
    )
    return {
        "roster_id": roster_id,
        "open_taxi_slots": open_slots,
        "taxi_eligible_rostered": [
            {"player_id": r["player_id"], "full_name": r["full_name"], "position": r["position"]} for r in ranked
        ],
    }


def prospect_pick_cross_reference(conn: sqlite3.Connection, draft_year: int, round_num: int, discount_rate: float) -> dict:
    """Buy/hold/sell read on an owned future pick: valuation.pick_value_estimate's
    FantasyCalc-anchored price for that pick versus this prospect_board's
    top available name at that round. Post-draft mode only: FantasyCalc
    doesn't price an unnamed future rookie pre-draft, so there is nothing
    to cross-reference against yet, the CLI refuses this in pre-draft
    mode rather than calling in with a name that doesn't exist."""
    pick_estimate = valuation.pick_value_estimate(conn, draft_year, round_num, discount_rate)
    board = _post_draft_board(conn, draft_year, None, 500)
    at_round = [p for p in board if p["round"] == round_num]
    top_name = at_round[0]["player_name"] if at_round else None
    return {
        "season": draft_year,
        "round": round_num,
        "pick_value_estimate": pick_estimate,
        "top_available_at_round": top_name,
    }
