"""FantasyCalc dynasty trade values.

Pulls one values/current call, parameterized for this league (12 teams,
1QB, full PPR), and stores it as a dated row per player so 7- and 30-day
trend queries diff two of our own snapshots instead of trusting
FantasyCalc's own trend30Day figure alone (kept, but not the only source of
truth once we have more than one snapshot).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx

from dynasty_agent.config import FANTASYCALC_PARAMS
from dynasty_agent.db import utcnow

BASE_URL = "https://api.fantasycalc.com/values/current"
CACHE_TTL_SECONDS = 6 * 3600


def fetch_values(conn: sqlite3.Connection) -> list[dict]:
    cache_key = "fantasycalc:values/current:" + json.dumps(FANTASYCALC_PARAMS, sort_keys=True)
    row = conn.execute(
        "SELECT response_json, fetched_at FROM api_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    if row is not None:
        fetched_at = datetime.fromisoformat(row["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at < timedelta(seconds=CACHE_TTL_SECONDS):
            return json.loads(row["response_json"])

    response = httpx.get(BASE_URL, params=FANTASYCALC_PARAMS, timeout=20.0)
    response.raise_for_status()
    values = response.json()

    fetched_at = utcnow()
    conn.execute(
        """
        INSERT INTO api_cache (cache_key, response_json, fetched_at) VALUES (?, ?, ?)
        ON CONFLICT (cache_key) DO UPDATE SET response_json = excluded.response_json, fetched_at = excluded.fetched_at
        """,
        (cache_key, json.dumps(values), fetched_at),
    )
    conn.commit()
    return values


def sync_market_values(conn: sqlite3.Connection, as_of: date | None = None) -> int:
    values = fetch_values(conn)
    as_of_date = (as_of or datetime.now(timezone.utc).date()).isoformat()
    fetched_at = utcnow()

    rows = []
    for entry in values:
        player = entry.get("player") or {}
        sleeper_id = player.get("sleeperId")
        if not sleeper_id:
            continue  # can't join back to our players table without it
        rows.append(
            (
                sleeper_id,
                "fantasycalc",
                as_of_date,
                entry.get("value"),
                entry.get("overallRank"),
                entry.get("positionRank"),
                entry.get("redraftValue"),
                entry.get("trend30Day"),
                entry.get("maybeTradeFrequency"),
                fetched_at,
            )
        )

    conn.executemany(
        """
        INSERT INTO market_values (player_id, source, as_of_date, value, overall_rank, position_rank,
                                    redraft_value, trend_30day, trade_frequency, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (player_id, source, as_of_date) DO UPDATE SET
            value = excluded.value, overall_rank = excluded.overall_rank, position_rank = excluded.position_rank,
            redraft_value = excluded.redraft_value, trend_30day = excluded.trend_30day,
            trade_frequency = excluded.trade_frequency, fetched_at = excluded.fetched_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def latest_value(conn: sqlite3.Connection, player_id: str) -> float | None:
    """The most recent stored FantasyCalc value for a player, or None if we
    have never synced a value for them (for example a player added to the
    Sleeper directory after the last sync)."""
    row = conn.execute(
        "SELECT value FROM market_values WHERE player_id = ? AND source = 'fantasycalc' ORDER BY as_of_date DESC LIMIT 1",
        (player_id,),
    ).fetchone()
    return row["value"] if row else None


_PICK_ROUND_LABELS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def pick_market_value(conn: sqlite3.Connection, season: int, round_num: int) -> float | None:
    """FantasyCalc's own unslotted market value for a future draft pick, for
    example "2027 1st", read from the same cached response sync_market_values
    already fetches. None if FantasyCalc doesn't price that year and round,
    it only prices a handful of years out."""
    label = f"{season} {_PICK_ROUND_LABELS.get(round_num, f'{round_num}th')}"
    for entry in fetch_values(conn):
        player = entry.get("player") or {}
        if player.get("position") == "PICK" and player.get("name") == label:
            return entry.get("value")
    return None


def value_trend(conn: sqlite3.Connection, player_id: str, days: int) -> float | None:
    """Value change over the last `days` days: latest snapshot minus the
    closest snapshot at or before (latest date - days). None if there is no
    old enough snapshot yet to compare against, which is expected until this
    has run daily for a while."""
    latest = conn.execute(
        """
        SELECT value, as_of_date FROM market_values
        WHERE player_id = ? AND source = 'fantasycalc'
        ORDER BY as_of_date DESC LIMIT 1
        """,
        (player_id,),
    ).fetchone()
    if latest is None or latest["value"] is None:
        return None

    cutoff = (datetime.fromisoformat(latest["as_of_date"]) - timedelta(days=days)).date().isoformat()
    older = conn.execute(
        """
        SELECT value FROM market_values
        WHERE player_id = ? AND source = 'fantasycalc' AND as_of_date <= ?
        ORDER BY as_of_date DESC LIMIT 1
        """,
        (player_id, cutoff),
    ).fetchone()
    if older is None or older["value"] is None:
        return None
    return latest["value"] - older["value"]
