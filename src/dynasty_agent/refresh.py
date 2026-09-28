"""One command that brings everything current: `dynasty-agent refresh`.

Before this, staying current meant knowing which of several ingest
commands to run and when, and the 2026 season's stats sat un-ingested
three weeks in. refresh knows the calendar instead:

- always: Sleeper (players, league, rosters, picks, drafts, nfl state)
  and FantasyCalc values, with the league-renewal check;
- NFL stats: the current season while games are being played (re-
  downloaded, nflverse updates in place), and last season once if it has
  never been ingested, it's the blend's prior (see blend.py);
- this week's Sleeper matchups during the regular season;
- college stats August through January, while a college season is live;
- NFL draft and combine data February through May (combine, then draft);
- the prospect model, refit when a newer draft class has completed three
  NFL seasons than the stored fit was trained on.

Every step runs on its own: one failing (a network blip, a file nflverse
hasn't published yet) is reported and the rest still run, which matters
for the scheduled daily run nobody is watching. The run ends with how old
each source is.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
from datetime import date, datetime, timezone
from typing import Callable

from dynasty_agent import college, config, market, nflverse, prospect_model, prospects, sleeper
from dynasty_agent.sleeper import SleeperClient

COLLEGE_MONTHS = (8, 9, 10, 11, 12, 1)
DRAFT_DATA_MONTHS = (2, 3, 4, 5)
IN_SEASON_TYPES = ("regular", "post")


def latest_complete_season(state_season: int, season_type: str | None) -> int:
    """The most recent NFL season with every game played. Sleeper keeps
    reporting the old season as "off" after the Super Bowl; anything else
    means that season is still ahead or under way."""
    return state_season if season_type == "off" else state_season - 1


def college_season_for(today: date) -> int | None:
    """The college season in progress, or None outside August-January.
    January belongs to the season that started the previous August (bowls
    and the playoff)."""
    if today.month not in COLLEGE_MONTHS:
        return None
    return today.year - 1 if today.month == 1 else today.year


def _step(results: list[dict], name: str, fn: Callable[[], str | None]) -> None:
    try:
        detail = fn()
        results.append({"step": name, "ok": True, "detail": detail or "done"})
    except Exception as e:  # noqa: BLE001 - reported, never swallowed silently, the other steps still run
        results.append({"step": name, "ok": False, "detail": f"{type(e).__name__}: {e}",
                        "traceback": traceback.format_exc()})


def run(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    today = today or datetime.now(timezone.utc).date()
    results: list[dict] = []

    def sync() -> str:
        with SleeperClient(conn) as client:
            client.sync_all()
        rows = market.sync_market_values(conn)
        notice = ""
        league = conn.execute("SELECT season FROM league WHERE league_id = ?", (config.LEAGUE_ID,)).fetchone()
        if league is not None and league["season"]:
            successor = sleeper.find_successor_league(config.SLEEPER_USER_ID, config.LEAGUE_ID, league["season"])
            if successor is not None:
                notice = (
                    f"; NOTICE: renewed for {successor['season']} as league {successor['league_id']}, run "
                    f"`dynasty-agent init --username {config.SLEEPER_USERNAME} --league-id {successor['league_id']}`"
                )
        return f"Sleeper synced, {rows} FantasyCalc values{notice}"

    _step(results, "sync", sync)

    state = conn.execute("SELECT season, week, season_type FROM nfl_state ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if state is None or state["season"] is None:
        results.append({"step": "nfl state", "ok": False, "detail": "no NFL state, the sync step must succeed first"})
        return results
    season, week, season_type = int(state["season"]), state["week"], state["season_type"]
    league = conn.execute("SELECT scoring_settings_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    scoring = json.loads(league["scoring_settings_json"]) if league else {}
    ingested = {int(r[0]) for r in conn.execute("SELECT DISTINCT season FROM weekly_stats")}

    if season - 1 not in ingested:
        _step(results, f"NFL stats {season - 1} (blend prior)", lambda: nflverse.ingest_season(conn, season - 1, scoring))
    if season_type in IN_SEASON_TYPES:
        _step(results, f"NFL stats {season}", lambda: nflverse.ingest_season(conn, season, scoring, force=True))
        _step(results, f"NFL schedules and lines", lambda: str(nflverse.ensure_games_cached(force=True).name) + " refreshed")
    if season_type == "regular" and week:
        def matchups() -> str:
            with SleeperClient(conn) as client:
                client.sync_matchups(week)
            return f"week {week} matchups synced"
        _step(results, "matchups", matchups)

    cfb_season = college_season_for(today)
    if cfb_season is not None:
        _step(results, f"college stats {cfb_season}", lambda: college.ingest_season(conn, cfb_season, force=True))

    have_draft_data = conn.execute("SELECT 1 FROM nfl_draft_picks LIMIT 1").fetchone() is not None
    if today.month in DRAFT_DATA_MONTHS or not have_draft_data:
        _step(results, "NFL draft, combine, player ids", lambda: prospects.ingest_draft_data(conn, force=True))

    expected = prospect_model.training_classes_label(latest_complete_season(season, season_type))
    stored = prospect_model.load_model(conn, "baseline_draft_capital")
    if stored is None or stored["training_classes"] != expected:
        def refit() -> str:
            report = prospect_model.fit_and_store(conn, scoring, latest_complete_season(season, season_type))
            cv = report["variants"]["baseline_draft_capital"]["cv"]
            return f"refit on {expected}, draft-capital model held-out R^2 {cv['r2']:.3f}"
        _step(results, "prospect model", refit)

    return results


def freshness(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """How current each source is, for the end of a refresh run."""
    def one(sql: str, params: tuple = ()) -> str | None:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row and row[0] is not None else None

    out = [
        ("Sleeper sync", _local(one("SELECT max(fetched_at) FROM nfl_state")) or "never"),
        ("FantasyCalc values", one("SELECT max(as_of_date) FROM market_values") or "never"),
    ]
    latest_stats = conn.execute("SELECT season, max(week) FROM weekly_stats GROUP BY season ORDER BY season DESC LIMIT 1").fetchone()
    out.append(("NFL weekly stats", f"{latest_stats[0]} through week {latest_stats[1]}" if latest_stats else "none"))
    games = nflverse.NFLVERSE_CACHE_DIR / "games.parquet"
    out.append(("Schedules and lines", datetime.fromtimestamp(games.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                if games.exists() else "never"))
    cfb = conn.execute("SELECT max(season), max(fetched_at) FROM cfb_player_season").fetchone()
    out.append(("College stats", f"{cfb[0]} season, fetched {_local(cfb[1])}" if cfb and cfb[0] else "none"))
    out.append(("NFL draft data", one("SELECT 'through the ' || max(season) || ' draft' FROM nfl_draft_picks") or "none"))
    model = prospect_model.load_model(conn, "baseline_draft_capital")
    out.append(("Prospect model", f"trained on {model['training_classes']}, fit {model['fitted_at'][:10]}" if model else "not fitted"))
    return out


def _local(iso_utc: str | None) -> str | None:
    """A stored UTC timestamp as local time, minutes precision."""
    if not iso_utc:
        return None
    return datetime.fromisoformat(iso_utc).astimezone().strftime("%Y-%m-%d %H:%M")
