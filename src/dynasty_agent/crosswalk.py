"""Player ID crosswalk from dynastyprocess/data's db_playerids.csv.

Fetched live from the upstream raw URL at sync time and never vendored into
this repo: the file is GPL-3.0, this project is MIT, and committing a copy
would pull that copyleft license into an MIT codebase. One line in README's
data-sources section says the same.

Verified against the live file before writing this, the same discipline
every other ingestion module in this project follows: the real header has
no single dynastyprocess-internal id column (no "dp_id" or similar), so
mfl_id (MyFantasyLeague's own id) is used as the primary key here, the one
column populated on effectively every real row. Missing values in this file
are the literal string "NA", not empty, converted to None on ingest so a
missing sleeper_id or gsis_id reads as NULL, not the two-character string
"NA".

Needed because roster_weekly's own sleeper_id/gsis_id crosswalk (already
used in nflverse.py) only has a row once a player has actually appeared in
a tracked NFL week. A drafted-but-not-yet-active rookie, or a taxi-squad
prospect, has no roster_weekly row yet but is already in Sleeper's player
pool and in this file.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from dynasty_agent.db import utcnow

SOURCE_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"
CACHE_KEY = "dynastyprocess:db_playerids.csv"
CACHE_TTL_SECONDS = 24 * 3600  # this file updates roughly daily upstream; a sync doesn't need it fresher than that


def _clean(value: str | None) -> str | None:
    """"NA" and empty string both mean missing in this file; neither should
    read back as a real value later."""
    if value is None or value == "" or value == "NA":
        return None
    return value


def fetch_player_ids(conn: sqlite3.Connection) -> list[dict]:
    """Fetch db_playerids.csv, cached in api_cache with the same
    fetch-then-cache pattern market.fetch_values uses (that table's
    response_json column just holds raw CSV text here, not JSON; reused as
    a generic cached-HTTP-response store, not a JSON-specific one). Parsed
    with stdlib csv.DictReader, never pandas."""
    row = conn.execute(
        "SELECT response_json, fetched_at FROM api_cache WHERE cache_key = ?", (CACHE_KEY,)
    ).fetchone()
    if row is not None:
        fetched_at = datetime.fromisoformat(row["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at < timedelta(seconds=CACHE_TTL_SECONDS):
            return list(csv.DictReader(io.StringIO(row["response_json"])))

    response = httpx.get(SOURCE_URL, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    text = response.text

    conn.execute(
        """
        INSERT INTO api_cache (cache_key, response_json, fetched_at) VALUES (?, ?, ?)
        ON CONFLICT (cache_key) DO UPDATE SET response_json = excluded.response_json, fetched_at = excluded.fetched_at
        """,
        (CACHE_KEY, text, utcnow()),
    )
    conn.commit()
    return list(csv.DictReader(io.StringIO(text)))


def sync_player_id_crosswalk(conn: sqlite3.Connection) -> int:
    """Upsert every real row into player_id_crosswalk. Returns row count."""
    entries = fetch_player_ids(conn)
    fetched_at = utcnow()

    rows = [
        (
            entry.get("mfl_id"),
            _clean(entry.get("sleeper_id")),
            _clean(entry.get("gsis_id")),
            _clean(entry.get("pfr_id")),
            _clean(entry.get("cfbref_id")),
            _clean(entry.get("espn_id")),
            _clean(entry.get("yahoo_id")),
            entry.get("name"),
            entry.get("merge_name"),
            entry.get("position"),
            entry.get("college"),
            fetched_at,
        )
        for entry in entries
        if entry.get("mfl_id")
    ]

    conn.executemany(
        """
        INSERT INTO player_id_crosswalk (
            mfl_id, sleeper_id, gsis_id, pfr_id, cfbref_id, espn_id, yahoo_id,
            name, merge_name, position, college, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (mfl_id) DO UPDATE SET
            sleeper_id = excluded.sleeper_id, gsis_id = excluded.gsis_id, pfr_id = excluded.pfr_id,
            cfbref_id = excluded.cfbref_id, espn_id = excluded.espn_id, yahoo_id = excluded.yahoo_id,
            name = excluded.name, merge_name = excluded.merge_name, position = excluded.position,
            college = excluded.college, fetched_at = excluded.fetched_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def sleeper_id_for_gsis(conn: sqlite3.Connection, gsis_id: str) -> str | None:
    row = conn.execute(
        "SELECT sleeper_id FROM player_id_crosswalk WHERE gsis_id = ? AND sleeper_id IS NOT NULL", (gsis_id,)
    ).fetchone()
    return row["sleeper_id"] if row else None


def gsis_id_for_sleeper(conn: sqlite3.Connection, sleeper_id: str) -> str | None:
    row = conn.execute(
        "SELECT gsis_id FROM player_id_crosswalk WHERE sleeper_id = ? AND gsis_id IS NOT NULL", (sleeper_id,)
    ).fetchone()
    return row["gsis_id"] if row else None
