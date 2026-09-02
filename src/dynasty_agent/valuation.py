"""Player valuation and the contend-or-rebuild verdict.

Nothing here is persisted: it is computed fresh from what Phase 1 already
ingested, rosters and market values in SQLite, and the cached local
nflverse parquet files for the season-level team context (QB quality, pass
rate, sack rate allowed) that the weekly_stats table does not carry.
Recomputing every run is cheap at this league's size, twelve teams, roughly
twenty rostered players each, and keeps this from ever reading a stale
cache of its own.

The season used for team context and production is whatever season has
nflverse data ingested (see nflverse.ingest_season), normally the most
recently completed season until the current one has enough games played to
mean something. That is stated in every result, not left implicit.
"""

from __future__ import annotations

import sqlite3

import duckdb

from dynasty_agent import market, nflverse
from dynasty_agent.metrics import (
    discounted_pick_value,
    percentile_rank,
    production_score,
    three_year_value,
    win_now_value,
)

# This league's next rookie draft, the currency future picks trade in. The
# inaugural rookie class (2026 NFL draft) is already rostered, confirmed in
# Phase 0, so the picks that actually get traded from here on are 2027 and
# later. FantasyCalc's own "2027 1st/2nd/3rd" unslotted values anchor the
# discount model below, no draft order is known yet for an unplayed season.
PICK_VALUE_BASE_SEASON = 2027

# nflverse's own team codes occasionally differ from Sleeper's. Confirmed by
# diffing the two team-code sets directly rather than assuming: only the
# Rams differ among currently active teams (Sleeper "LAR", nflverse "LA").
TEAM_ALIASES: dict[str, str] = {"LAR": "LA"}


def _to_nflverse_team(team: str | None) -> str | None:
    if team is None:
        return None
    return TEAM_ALIASES.get(team, team)


def team_situation_scores(season: int) -> dict[str, dict]:
    """Per-NFL-team situation inputs, keyed by nflverse's team code: average
    weekly passing EPA from the team's quarterback(s) (QB quality), average
    team pass rate over expected, and sack rate allowed inverted so higher
    is better (an offensive line pass-protection proxy; true OL grades are
    paywalled, this is the public stand-in). Each is percentile ranked
    against all 32 NFL teams, then averaged into one 0-100 situation_score.
    """

    stats_path = nflverse.ensure_cached("stats_player_week", season)
    pbp_path = nflverse.ensure_cached("pbp", season)

    con = duckdb.connect()
    try:
        qb_rows = con.execute(
            """
            SELECT team, avg(passing_epa) AS qb_epa
            FROM read_parquet(?)
            WHERE position = 'QB' AND season_type = 'REG' AND passing_epa IS NOT NULL
            GROUP BY team
            """,
            [str(stats_path)],
        ).fetchall()
        pass_rate_rows = con.execute(
            """
            SELECT posteam AS team, avg(pass_oe) AS pass_rate_oe
            FROM read_parquet(?)
            WHERE pass_oe IS NOT NULL AND posteam IS NOT NULL
            GROUP BY posteam
            """,
            [str(pbp_path)],
        ).fetchall()
        sack_rows = con.execute(
            """
            SELECT posteam AS team,
                   sum(sack) * 1.0 / nullif(sum(pass_attempt) + sum(sack), 0) AS sack_rate
            FROM read_parquet(?)
            WHERE posteam IS NOT NULL
            GROUP BY posteam
            """,
            [str(pbp_path)],
        ).fetchall()
    finally:
        con.close()

    qb_epa = dict(qb_rows)
    pass_rate = dict(pass_rate_rows)
    ol_pass_pro = {team: (1 - rate) if rate is not None else None for team, rate in sack_rows}

    all_teams = sorted(set(qb_epa) | set(pass_rate) | set(ol_pass_pro))
    qb_population = list(qb_epa.values())
    pass_population = list(pass_rate.values())
    ol_population = list(ol_pass_pro.values())

    result: dict[str, dict] = {}
    for team in all_teams:
        qb_pct = percentile_rank(qb_epa.get(team), qb_population)
        pass_pct = percentile_rank(pass_rate.get(team), pass_population)
        ol_pct = percentile_rank(ol_pass_pro.get(team), ol_population)
        result[team] = {
            "qb_quality_percentile": qb_pct,
            "pass_rate_percentile": pass_pct,
            "ol_pass_pro_percentile": ol_pct,
            "situation_score": (qb_pct + pass_pct + ol_pct) / 3.0,
        }
    return result


def player_valuations(conn: sqlite3.Connection, season: int) -> dict[str, dict]:
    """One valuation per player with both a players-table entry and at
    least one weekly_stats row this season: production score, win-now
    value, and three-year value, plus every input that fed them."""

    situations = team_situation_scores(season)

    rows = conn.execute(
        """
        SELECT p.player_id, p.full_name, p.position, p.team, p.age,
               avg(ws.fantasy_points) AS fppg, count(*) AS games
        FROM players p
        JOIN weekly_stats ws ON ws.player_id = p.player_id AND ws.season = ?
        GROUP BY p.player_id
        """,
        (str(season),),
    ).fetchall()

    result: dict[str, dict] = {}
    for row in rows:
        team_situation = situations.get(_to_nflverse_team(row["team"]), {}).get("situation_score", 50.0)
        prod = production_score(row["fppg"], row["position"])
        result[row["player_id"]] = {
            "full_name": row["full_name"],
            "position": row["position"],
            "team": row["team"],
            "age": row["age"],
            "fantasy_points_per_game": row["fppg"],
            "games": row["games"],
            "situation_score": team_situation,
            "production_score": prod,
            "win_now_value": win_now_value(prod, row["position"], row["age"], team_situation),
            "three_year_value": three_year_value(prod, row["position"], row["age"], team_situation),
        }
    return result


def contend_or_rebuild(conn: sqlite3.Connection, season: int, my_roster_id: int) -> dict:
    """A verdict built from roster construction and compared against the
    other eleven teams, not from record or points, those are only a real
    signal once games have been played. Confidence is stated explicitly and
    stays low until real in-season results accumulate."""

    valuations = player_valuations(conn, season)

    starters = conn.execute("SELECT roster_id, player_id FROM roster_players WHERE slot = 'starter'").fetchall()

    team_win_now: dict[int, list[float]] = {}
    team_three_year: dict[int, list[float]] = {}
    for r in starters:
        v = valuations.get(r["player_id"])
        if v is None:
            continue
        team_win_now.setdefault(r["roster_id"], []).append(v["win_now_value"])
        team_three_year.setdefault(r["roster_id"], []).append(v["three_year_value"])

    win_now_totals = {rid: sum(vals) for rid, vals in team_win_now.items()}
    three_year_totals = {rid: sum(vals) for rid, vals in team_three_year.items()}

    my_win_now = win_now_totals.get(my_roster_id, 0.0)
    my_three_year = three_year_totals.get(my_roster_id, 0.0)
    win_now_pct = percentile_rank(my_win_now, list(win_now_totals.values()))
    three_year_pct = percentile_rank(my_three_year, list(three_year_totals.values()))

    roster_row = conn.execute(
        "SELECT wins, losses, ties FROM rosters WHERE roster_id = ?", (my_roster_id,)
    ).fetchone()
    played = ((roster_row["wins"] or 0) + (roster_row["losses"] or 0) + (roster_row["ties"] or 0)) if roster_row else 0

    if win_now_pct >= 60 and three_year_pct >= 40:
        verdict = "contend"
    elif win_now_pct < 40 and three_year_pct >= 55:
        verdict = "rebuild"
    else:
        verdict = "unclear: not a clean contender or a clean rebuild on roster construction alone"

    if played == 0:
        confidence = "low. 0 games played this season, this verdict is roster construction only, not results"
    elif played < 6:
        confidence = f"low to moderate. only {played} games played, recheck weekly through week 6 or 7"
    else:
        confidence = f"moderate to high. {played} games played, results are a real signal now"

    return {
        "season_used": season,
        "verdict": verdict,
        "confidence": confidence,
        "games_played": played,
        "my_win_now_total": my_win_now,
        "my_three_year_total": my_three_year,
        "win_now_percentile": win_now_pct,
        "three_year_percentile": three_year_pct,
        "league_win_now_totals": win_now_totals,
        "league_three_year_totals": three_year_totals,
    }


def resolve_player(conn: sqlite3.Connection, name_or_id: str) -> dict:
    """Resolve a player name or a literal Sleeper player_id to a row from
    the players table. Raises ValueError, listing candidates, on no match
    or an ambiguous one, rather than silently guessing which player was
    meant."""
    row = conn.execute("SELECT * FROM players WHERE player_id = ?", (name_or_id,)).fetchone()
    if row is not None:
        return dict(row)

    exact = conn.execute("SELECT * FROM players WHERE lower(full_name) = lower(?)", (name_or_id,)).fetchall()
    if len(exact) == 1:
        return dict(exact[0])
    if len(exact) > 1:
        names = ", ".join(f"{r['full_name']} ({r['position']} {r['team']})" for r in exact)
        raise ValueError(f"'{name_or_id}' matches more than one player: {names}. Use the player_id instead.")

    fuzzy = conn.execute(
        "SELECT * FROM players WHERE full_name LIKE ? ORDER BY full_name", (f"%{name_or_id}%",)
    ).fetchall()
    if len(fuzzy) == 1:
        return dict(fuzzy[0])
    if len(fuzzy) > 1:
        names = ", ".join(f"{r['full_name']} ({r['position']} {r['team']})" for r in fuzzy[:10])
        raise ValueError(f"'{name_or_id}' is ambiguous, matches: {names}. Be more specific or use the player_id.")

    raise ValueError(f"No player found matching '{name_or_id}'.")


def pick_value_estimate(conn: sqlite3.Connection, season: int, round_num: int, discount_rate: float) -> dict:
    """My model's value for a future pick: FantasyCalc's real, current
    "{PICK_VALUE_BASE_SEASON} {round}" market price, discounted forward by
    discount_rate per year of distance from that base season. Compared
    against FantasyCalc's own price for this exact pick when they have one,
    that comparison is the arbitrage; picks further out than FantasyCalc
    prices get a model value but no arbitrage figure, there is nothing to
    compare against."""
    base_value = market.pick_market_value(conn, PICK_VALUE_BASE_SEASON, round_num)
    this_pick_market_value = market.pick_market_value(conn, season, round_num)

    if base_value is None:
        return {
            "season": season, "round": round_num, "model_value": None,
            "market_value": this_pick_market_value, "arbitrage": None,
        }

    years_out = season - PICK_VALUE_BASE_SEASON
    model_value = discounted_pick_value(base_value, years_out, discount_rate)
    arbitrage = (model_value - this_pick_market_value) if this_pick_market_value is not None else None
    return {
        "season": season, "round": round_num, "model_value": model_value,
        "market_value": this_pick_market_value, "arbitrage": arbitrage,
    }


def _value_trade_side(
    conn: sqlite3.Connection, valuations: dict, players_in: list[str], picks_in: list[tuple[int, int]], discount_rate: float
) -> dict:
    """One side of a trade, players and picks valued and totaled.

    Two currencies get kept deliberately separate, they are not the same
    units and summing them was a real bug caught before this ever reported
    a number to act on: win_now_value and three_year_value are this
    league's own fantasy-points-per-game scale (from metrics.py), while
    pick model_value is FantasyCalc's trade-capital scale (thousands).
    Picks contribute 0 to win-now regardless, a rookie pick cannot help you
    win this year, that part never mixed units. market_value_total is the
    one number actually comparable across players and picks: each player's
    own FantasyCalc market value plus each pick's FantasyCalc-anchored
    model value, both already in FantasyCalc's scale.
    """
    player_rows = []
    for name_or_id in players_in:
        p = resolve_player(conn, name_or_id)
        v = valuations.get(p["player_id"])
        player_rows.append(
            {
                "player_id": p["player_id"],
                "full_name": p["full_name"],
                "position": p["position"],
                "win_now_value": v["win_now_value"] if v else 0.0,
                "three_year_value": v["three_year_value"] if v else 0.0,
                "market_value": market.latest_value(conn, p["player_id"]),
                "has_data": v is not None,
            }
        )

    pick_rows = []
    for pick_season, pick_round in picks_in:
        estimate = pick_value_estimate(conn, pick_season, pick_round, discount_rate)
        pick_rows.append({**estimate, "label": f"{pick_season} round {pick_round}"})

    win_now_total = sum(r["win_now_value"] for r in player_rows)  # players only, always unit-safe
    player_three_year_total = sum(r["three_year_value"] for r in player_rows)  # players only, my model, informative
    market_value_total = sum((r["market_value"] or 0.0) for r in player_rows) + sum(
        (r["model_value"] or 0.0) for r in pick_rows
    )  # comparable across players and picks, the one headline total
    return {
        "players": player_rows,
        "picks": pick_rows,
        "win_now_total": win_now_total,
        "player_three_year_total": player_three_year_total,
        "market_value_total": market_value_total,
        "asset_count": len(player_rows) + len(pick_rows),
    }


def evaluate_trade(
    conn: sqlite3.Connection,
    valuation_season: int,
    my_roster_id: int,
    send_players: list[str],
    send_picks: list[tuple[int, int]],
    receive_players: list[str],
    receive_picks: list[tuple[int, int]],
    discount_rate: float,
) -> dict:
    """Both sides of a proposed trade, valued on win-now and three-year
    axes, picks discounted and checked against FantasyCalc for arbitrage,
    and flagged for fit against the current contend-or-rebuild posture and
    for consolidation (many pieces for one, or the reverse)."""
    valuations = player_valuations(conn, valuation_season)

    sent = _value_trade_side(conn, valuations, send_players, send_picks, discount_rate)
    received = _value_trade_side(conn, valuations, receive_players, receive_picks, discount_rate)

    win_now_delta = received["win_now_total"] - sent["win_now_total"]
    player_three_year_delta = received["player_three_year_total"] - sent["player_three_year_total"]
    market_value_delta = received["market_value_total"] - sent["market_value_total"]

    verdict = contend_or_rebuild(conn, valuation_season, my_roster_id)
    posture = verdict["verdict"]
    if posture == "contend":
        fit = "fits a contend posture" if win_now_delta >= 0 else "cuts against a contend posture, loses win-now value"
    elif posture == "rebuild":
        fit = "fits a rebuild posture" if market_value_delta >= 0 else "cuts against a rebuild posture, loses long-term market value"
    else:
        fit = "posture is unclear right now, judge this on the raw numbers, not fit"

    consolidation = None
    if sent["asset_count"] >= 2 and received["asset_count"] == 1:
        consolidation = "consolidation: multiple pieces for one. Generally favors you, 10 bench slots against only 8 starters."
    elif received["asset_count"] >= 2 and sent["asset_count"] == 1:
        consolidation = "deconsolidation: one piece for multiple. Generally works against you unless every piece coming back is startable."

    return {
        "sent": sent,
        "received": received,
        "win_now_delta": win_now_delta,
        "player_three_year_delta": player_three_year_delta,
        "market_value_delta": market_value_delta,
        "posture": posture,
        "posture_confidence": verdict["confidence"],
        "fit": fit,
        "consolidation": consolidation,
        "discount_rate": discount_rate,
    }
