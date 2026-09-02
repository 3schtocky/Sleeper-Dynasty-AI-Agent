"""Read-only Sleeper API client.

No auth needed for any of this. Every call goes through a small SQLite cache
(the api_cache table) keyed by URL, honoring a per-endpoint TTL, and a rate
limiter that keeps us under Sleeper's 1000-calls-per-minute ceiling. The
player directory (GET /v1/players/nfl) gets special treatment: it is
roughly 5MB, so it is cached straight to the players table and refreshed at
most once a day, never fetched inline by another method.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from dynasty_agent.config import LEAGUE_ID
from dynasty_agent.db import utcnow

BASE_URL = "https://api.sleeper.app/v1"
MAX_CALLS_PER_MINUTE = 1000
PLAYERS_REFRESH_INTERVAL = timedelta(days=1)


class RateLimiter:
    """Keeps calls under a per-minute ceiling with a sliding window. At our
    actual call volumes this almost never sleeps; it exists as a floor, not
    because we expect to hit it."""

    def __init__(self, max_calls: int, per_seconds: float = 60.0) -> None:
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self._calls: deque[float] = deque()

    def wait_for_slot(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > self.per_seconds:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            sleep_for = self.per_seconds - (now - self._calls[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
        self._calls.append(time.monotonic())


class SleeperClient:
    def __init__(self, conn: sqlite3.Connection, league_id: str = LEAGUE_ID) -> None:
        self.conn = conn
        self.league_id = league_id
        self._http = httpx.Client(base_url=BASE_URL, timeout=15.0)
        self._limiter = RateLimiter(MAX_CALLS_PER_MINUTE)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SleeperClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low-level fetch, cached and rate-limited ---------------------------

    def _get(self, path: str, ttl_seconds: float) -> Any:
        cache_key = f"sleeper:{path}"
        row = self.conn.execute(
            "SELECT response_json, fetched_at FROM api_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is not None:
            fetched_at = datetime.fromisoformat(row["fetched_at"])
            if datetime.now(timezone.utc) - fetched_at < timedelta(seconds=ttl_seconds):
                return json.loads(row["response_json"])

        self._limiter.wait_for_slot()
        response = self._http.get(path)
        response.raise_for_status()
        data = response.json()

        fetched_at = utcnow()
        self.conn.execute(
            """
            INSERT INTO api_cache (cache_key, response_json, fetched_at) VALUES (?, ?, ?)
            ON CONFLICT (cache_key) DO UPDATE SET
                response_json = excluded.response_json, fetched_at = excluded.fetched_at
            """,
            (cache_key, json.dumps(data), fetched_at),
        )
        self.conn.commit()
        return data

    # -- raw endpoint wrappers -----------------------------------------------

    def get_league(self) -> dict:
        return self._get(f"/league/{self.league_id}", ttl_seconds=3600)

    def get_rosters(self) -> list[dict]:
        return self._get(f"/league/{self.league_id}/rosters", ttl_seconds=300)

    def get_users(self) -> list[dict]:
        return self._get(f"/league/{self.league_id}/users", ttl_seconds=86400)

    def get_matchups(self, week: int) -> list[dict]:
        return self._get(f"/league/{self.league_id}/matchups/{week}", ttl_seconds=300)

    def get_transactions(self, week: int) -> list[dict]:
        return self._get(f"/league/{self.league_id}/transactions/{week}", ttl_seconds=300)

    def get_traded_picks(self) -> list[dict]:
        return self._get(f"/league/{self.league_id}/traded_picks", ttl_seconds=3600)

    def get_draft(self, draft_id: str) -> dict:
        return self._get(f"/draft/{draft_id}", ttl_seconds=3600)

    def get_draft_picks(self, draft_id: str) -> list[dict]:
        return self._get(f"/draft/{draft_id}/picks", ttl_seconds=3600)

    def get_trending(self, kind: str = "add", lookback_hours: int = 24, limit: int = 25) -> list[dict]:
        if kind not in ("add", "drop"):
            raise ValueError("kind must be 'add' or 'drop'")
        return self._get(
            f"/players/nfl/trending/{kind}?lookback_hours={lookback_hours}&limit={limit}",
            ttl_seconds=1800,
        )

    def get_nfl_state(self) -> dict:
        return self._get("/state/nfl", ttl_seconds=1800)

    # -- players directory: special-cased, cached to its own table ----------

    def refresh_players(self, force: bool = False) -> int:
        """Refresh the players table from /v1/players/nfl. At most once a
        day unless force=True. Returns the number of players written, or 0
        if the cache was still fresh and nothing was fetched."""
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'players_last_refreshed_at'"
        ).fetchone()
        if not force and row is not None:
            last = datetime.fromisoformat(row["value"])
            if datetime.now(timezone.utc) - last < PLAYERS_REFRESH_INTERVAL:
                return 0

        self._limiter.wait_for_slot()
        response = self._http.get("/players/nfl")
        response.raise_for_status()
        players = response.json()

        fetched_at = utcnow()
        rows = [
            (
                player_id,
                p.get("full_name") or " ".join(filter(None, [p.get("first_name"), p.get("last_name")])),
                p.get("first_name"),
                p.get("last_name"),
                p.get("position"),
                p.get("team"),
                p.get("age"),
                p.get("years_exp"),
                p.get("status"),
                p.get("injury_status"),
                fetched_at,
            )
            for player_id, p in players.items()
            if isinstance(p, dict)
        ]
        self.conn.executemany(
            """
            INSERT INTO players (player_id, full_name, first_name, last_name, position, team, age, years_exp, status, injury_status, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (player_id) DO UPDATE SET
                full_name = excluded.full_name, first_name = excluded.first_name, last_name = excluded.last_name,
                position = excluded.position, team = excluded.team, age = excluded.age, years_exp = excluded.years_exp,
                status = excluded.status, injury_status = excluded.injury_status, fetched_at = excluded.fetched_at
            """,
            rows,
        )
        self.conn.execute(
            """
            INSERT INTO meta (key, value) VALUES ('players_last_refreshed_at', ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (fetched_at,),
        )
        self.conn.commit()
        return len(rows)

    # -- sync: fetch and normalize into the structured tables ----------------

    def sync_league(self) -> dict:
        league = self.get_league()
        fetched_at = utcnow()
        self.conn.execute(
            """
            INSERT INTO league (league_id, name, season, status, previous_league_id, num_teams,
                                 scoring_settings_json, roster_positions_json, settings_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (league_id) DO UPDATE SET
                name = excluded.name, season = excluded.season, status = excluded.status,
                previous_league_id = excluded.previous_league_id, num_teams = excluded.num_teams,
                scoring_settings_json = excluded.scoring_settings_json,
                roster_positions_json = excluded.roster_positions_json,
                settings_json = excluded.settings_json, fetched_at = excluded.fetched_at
            """,
            (
                league["league_id"],
                league.get("name"),
                league.get("season"),
                league.get("status"),
                league.get("previous_league_id"),
                league.get("settings", {}).get("num_teams"),
                json.dumps(league.get("scoring_settings", {})),
                json.dumps(league.get("roster_positions", [])),
                json.dumps(league.get("settings", {})),
                fetched_at,
            ),
        )
        self.conn.commit()
        return league

    def sync_users(self) -> list[dict]:
        users = self.get_users()
        fetched_at = utcnow()
        rows = [
            (u["user_id"], self.league_id, u.get("display_name"), (u.get("metadata") or {}).get("team_name"), fetched_at)
            for u in users
        ]
        self.conn.executemany(
            """
            INSERT INTO users (user_id, league_id, display_name, team_name, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                display_name = excluded.display_name, team_name = excluded.team_name, fetched_at = excluded.fetched_at
            """,
            rows,
        )
        self.conn.commit()
        return users

    def sync_rosters(self) -> list[dict]:
        rosters = self.get_rosters()
        fetched_at = utcnow()
        roster_rows = []
        player_rows = []
        for r in rosters:
            settings = r.get("settings", {})
            roster_rows.append(
                (
                    r["roster_id"],
                    self.league_id,
                    r.get("owner_id"),
                    settings.get("wins"),
                    settings.get("losses"),
                    settings.get("ties"),
                    settings.get("fpts"),
                    settings.get("fpts_against"),
                    settings.get("waiver_position"),
                    settings.get("waiver_budget_used"),
                    fetched_at,
                )
            )
            starters = set(r.get("starters") or [])
            taxi = set(r.get("taxi") or [])
            reserve = set(r.get("reserve") or [])
            for player_id in r.get("players") or []:
                if player_id in starters:
                    slot = "starter"
                elif player_id in taxi:
                    slot = "taxi"
                elif player_id in reserve:
                    slot = "reserve"
                else:
                    slot = "bench"
                player_rows.append((r["roster_id"], player_id, slot, fetched_at))

        self.conn.executemany(
            """
            INSERT INTO rosters (roster_id, league_id, owner_id, wins, losses, ties, fpts, fpts_against,
                                  waiver_position, waiver_budget_used, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (roster_id) DO UPDATE SET
                owner_id = excluded.owner_id, wins = excluded.wins, losses = excluded.losses, ties = excluded.ties,
                fpts = excluded.fpts, fpts_against = excluded.fpts_against,
                waiver_position = excluded.waiver_position, waiver_budget_used = excluded.waiver_budget_used,
                fetched_at = excluded.fetched_at
            """,
            roster_rows,
        )
        roster_ids = [r["roster_id"] for r in rosters]
        if roster_ids:
            placeholders = ",".join("?" * len(roster_ids))
            self.conn.execute(f"DELETE FROM roster_players WHERE roster_id IN ({placeholders})", roster_ids)
        self.conn.executemany(
            "INSERT INTO roster_players (roster_id, player_id, slot, fetched_at) VALUES (?, ?, ?, ?)",
            player_rows,
        )
        self.conn.commit()
        return rosters

    def sync_traded_picks(self) -> list[dict]:
        picks = self.get_traded_picks()
        fetched_at = utcnow()
        self.conn.execute("DELETE FROM traded_picks WHERE league_id = ?", (self.league_id,))
        rows = [
            (
                self.league_id,
                p["season"],
                p["round"],
                p["roster_id"],
                p.get("previous_owner_id"),
                p["owner_id"],
                fetched_at,
            )
            for p in picks
        ]
        self.conn.executemany(
            """
            INSERT INTO traded_picks (league_id, season, round, roster_id, previous_owner_id, owner_id, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return picks

    def sync_nfl_state(self) -> dict:
        state = self.get_nfl_state()
        fetched_at = utcnow()
        self.conn.execute(
            "INSERT INTO nfl_state (fetched_at, season, week, season_type, display_week) VALUES (?, ?, ?, ?, ?)",
            (fetched_at, state.get("season"), state.get("week"), state.get("season_type"), state.get("display_week")),
        )
        self.conn.commit()
        return state

    def sync_all(self) -> None:
        """Refresh everything Phase 1's acceptance test needs: players,
        league, users, rosters, traded picks, and nfl state."""
        self.refresh_players()
        self.sync_league()
        self.sync_users()
        self.sync_rosters()
        self.sync_traded_picks()
        self.sync_nfl_state()
