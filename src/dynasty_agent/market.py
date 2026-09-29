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


def round_label(round_num: int) -> str:
    """FantasyCalc's round wording: 1st, 2nd, 3rd, 4th."""
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(round_num, f"{round_num}th")


def priced_pick_seasons(conn: sqlite3.Connection, round_num: int) -> list[int]:
    """Every draft season FantasyCalc currently prices a pick of this round
    for, ascending. Read from the live response, not assumed: FantasyCalc
    drops a season once that rookie draft has happened, so the earliest
    priced season moves forward every spring."""
    label_suffix = f" {round_label(round_num)}"
    seasons = []
    for entry in fetch_values(conn):
        player = entry.get("player") or {}
        name = player.get("name") or ""
        if player.get("position") == "PICK" and name.endswith(label_suffix):
            year = name[: -len(label_suffix)]
            if year.isdigit():
                seasons.append(int(year))
    return sorted(set(seasons))


def pick_price(conn: sqlite3.Connection, season: int, round_num: int, tier: str | None = None) -> tuple[float | None, str | None]:
    """FantasyCalc's price for a pick and the label it came from. With a
    tier ("Early", "Mid", "Late") it's "2027 1st (Early)" when FantasyCalc
    prices tiers for that season (the next draft only, today), else the
    untiered "2027 1st". (None, None) when FantasyCalc doesn't price that
    year and round at all; it only prices a few years out. The one pick
    price the trade evaluator and the pick report both use."""
    label = f"{season} {round_label(round_num)}"
    wanted = [f"{label} ({tier})", label] if tier else [label]
    prices = {}
    for entry in fetch_values(conn):
        player = entry.get("player") or {}
        if player.get("position") == "PICK" and player.get("name") in wanted:
            prices[player["name"]] = entry.get("value")
    used = next((w for w in wanted if w in prices), None)
    return (prices[used], used) if used else (None, None)


def pick_market_value(conn: sqlite3.Connection, season: int, round_num: int, tier: str | None = None) -> float | None:
    """pick_price's value alone."""
    return pick_price(conn, season, round_num, tier)[0]


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
