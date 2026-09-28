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

import json
import sqlite3

import duckdb

from dynasty_agent import market, nflverse
from dynasty_agent.prospect_model import rookie_projections
from dynasty_agent.metrics import (
    best_lineup_total,
    discounted_pick_value,
    percentile_rank,
    production_score,
    starting_slot_counts,
    three_year_value,
    win_now_value,
)

# The discount model below anchors on the nearest rookie draft FantasyCalc
# still prices (market.priced_pick_seasons), read live on every call. An
# earlier version hardcoded 2027 here, correct only until the 2027 rookie
# draft: FantasyCalc then stops listing "2027 1st", and every pick's model
# value would have silently gone to None.

# nflverse's own team codes occasionally differ from Sleeper's. Confirmed by
# diffing the two team-code sets directly rather than assuming: only the
# Rams differ among currently active teams (Sleeper "LAR", nflverse "LA").
TEAM_ALIASES: dict[str, str] = {"LAR": "LA"}


def to_nflverse_team(team: str | None) -> str | None:
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

    Regular season only, all three inputs. The play-by-play inputs used to
    include playoff games, so the 14 playoff teams' rates carried extra,
    unusually hard games the other 18 teams' didn't.
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
            WHERE season_type = 'REG' AND pass_oe IS NOT NULL AND posteam IS NOT NULL
            GROUP BY posteam
            """,
            [str(pbp_path)],
        ).fetchall()
        sack_rows = con.execute(
            """
            SELECT posteam AS team,
                   sum(sack) * 1.0 / nullif(sum(pass_attempt) + sum(sack), 0) AS sack_rate
            FROM read_parquet(?)
            WHERE season_type = 'REG' AND posteam IS NOT NULL
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
    """One valuation per player with a players-table entry and either real
    weekly_stats rows this season or a prospect-model projection (a rookie,
    see prospect_model.rookie_projections): production score, win-now
    value, and three-year value, plus every input that fed them.
    value_source says which: "nfl_stats" or "prospect_model"."""

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

    def value_row(row, fppg: float, games: int, source: dict) -> dict:
        team_situation = situations.get(to_nflverse_team(row["team"]), {}).get("situation_score", 50.0)
        prod = production_score(fppg, row["position"])
        return {
            "full_name": row["full_name"],
            "position": row["position"],
            "team": row["team"],
            "age": row["age"],
            "fantasy_points_per_game": fppg,
            "games": games,
            "situation_score": team_situation,
            "production_score": prod,
            "win_now_value": win_now_value(prod, row["position"], row["age"], team_situation),
            "three_year_value": three_year_value(prod, row["position"], row["age"], team_situation),
            **source,
        }

    # Rookies and near-rookies with fewer than 4 games: the prospect model's
    # draft-capital projection instead of a thin (or empty) sample. Before
    # this, every rookie was valued at exactly 0, see PLANNING.md Phase 3.5.
    rookies = rookie_projections(conn, season)

    result: dict[str, dict] = {}
    for row in rows:
        if row["player_id"] in rookies:
            continue
        result[row["player_id"]] = value_row(row, row["fppg"], row["games"], {"value_source": "nfl_stats"})

    if rookies:
        placeholders = ",".join("?" * len(rookies))
        rookie_rows = conn.execute(
            f"SELECT player_id, full_name, position, team, age FROM players WHERE player_id IN ({placeholders})",
            list(rookies),
        ).fetchall()
        for row in rookie_rows:
            r = rookies[row["player_id"]]
            games = conn.execute(
                "SELECT count(*) FROM weekly_stats WHERE player_id = ? AND season = ?", (row["player_id"], str(season))
            ).fetchone()[0]
            result[row["player_id"]] = value_row(
                row, r["projected_ppg"], games,
                {"value_source": "prospect_model", "draft_pick": r["draft_pick"], "undrafted": r["undrafted"]},
            )
    return result


def contend_or_rebuild(conn: sqlite3.Connection, season: int, my_roster_id: int) -> dict:
    """A verdict built from roster construction and compared against the
    other eleven teams, not from record or points, those are only a real
    signal once games have been played. Confidence is stated explicitly and
    stays low until real in-season results accumulate.

    Each team is scored on the best lineup it could start (starters and
    bench, per the league's own roster_positions), win-now and three-year
    each maximized separately. An earlier version summed whatever lineup
    each manager last set in Sleeper, empty or stale all offseason and
    wrong for any manager who hadn't set one, so the verdict measured
    lineup-setting diligence as much as roster strength."""

    valuations = player_valuations(conn, season)

    league_row = conn.execute("SELECT roster_positions_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if league_row is None:
        raise ValueError("No league data cached yet. Run `dynasty-agent sync` first.")
    slot_counts = starting_slot_counts(json.loads(league_row["roster_positions_json"]))

    eligible = conn.execute(
        "SELECT roster_id, player_id FROM roster_players WHERE slot IN ('starter', 'bench')"
    ).fetchall()
    team_win_now: dict[int, list[tuple[str | None, float]]] = {}
    team_three_year: dict[int, list[tuple[str | None, float]]] = {}
    for r in eligible:
        team_win_now.setdefault(r["roster_id"], [])
        team_three_year.setdefault(r["roster_id"], [])
        v = valuations.get(r["player_id"])
        if v is None:
            continue
        team_win_now[r["roster_id"]].append((v["position"], v["win_now_value"]))
        team_three_year[r["roster_id"]].append((v["position"], v["three_year_value"]))

    win_now_totals = {rid: best_lineup_total(vals, slot_counts) for rid, vals in team_win_now.items()}
    three_year_totals = {rid: best_lineup_total(vals, slot_counts) for rid, vals in team_three_year.items()}

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
    market price for the nearest season it prices in this round (the base
    season), discounted forward by discount_rate per year of distance from
    that base season. Compared against FantasyCalc's own price for this
    exact pick when they have one, that comparison is the arbitrage; picks
    further out than FantasyCalc prices get a model value but no arbitrage
    figure, there is nothing to compare against. A pick for a season before
    the base season (a draft that already happened) gets no model value:
    that pick no longer exists to trade."""
    priced = market.priced_pick_seasons(conn, round_num)
    base_season = priced[0] if priced else None
    this_pick_market_value = market.pick_market_value(conn, season, round_num)

    if base_season is None or season < base_season:
        return {
            "season": season, "round": round_num, "base_season": base_season, "model_value": None,
            "market_value": this_pick_market_value, "arbitrage": None,
        }

    base_value = market.pick_market_value(conn, base_season, round_num)
    model_value = discounted_pick_value(base_value, season - base_season, discount_rate)
    arbitrage = (model_value - this_pick_market_value) if this_pick_market_value is not None else None
    return {
        "season": season, "round": round_num, "base_season": base_season, "model_value": model_value,
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
                "value_source": v.get("value_source") if v else None,
                "draft_pick": v.get("draft_pick") if v else None,
                "undrafted": v.get("undrafted") if v else None,
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
    # Anything with no price counts 0 in market_value_total above; named
    # here so the caller can say so instead of presenting a silent 0.
    unpriced = [r["full_name"] for r in player_rows if r["market_value"] is None] + [
        r["label"] for r in pick_rows if r["model_value"] is None
    ]
    return {
        "players": player_rows,
        "picks": pick_rows,
        "win_now_total": win_now_total,
        "player_three_year_total": player_three_year_total,
        "market_value_total": market_value_total,
        "unpriced": unpriced,
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
