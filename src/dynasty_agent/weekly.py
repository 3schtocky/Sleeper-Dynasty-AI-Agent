"""Weekly workflow: opponent strength, Vegas context, and (soon) the
lineup optimizer, FAAB sizing, and the digest command that ties them
together. Quantitative first, by explicit request (see PLANNING.md's
Phase 3 section): every input here resolves to a real, sourced number,
never a qualitative override layered on top of the math.
"""

from __future__ import annotations

import itertools
import json
import sqlite3

import duckdb

from dynasty_agent import nflverse
from dynasty_agent.matchup import player_weekly_distribution, team_season_avg_implied_points, team_week_implied_points
from dynasty_agent.metrics import (
    injury_adjusted_mean,
    injury_adjusted_variance,
    matchup_win_probability,
    percentile_rank,
    platt_scale,
    vegas_week_multiplier,
)
from dynasty_agent.valuation import player_valuations, resolve_player, to_nflverse_team

POSITIONS = ("QB", "RB", "WR", "TE")


def opponent_strength_by_position(season: int) -> dict[str, dict[str, dict]]:
    """Average real EPA per play ALLOWED by every defense, broken down by
    the offensive position that gained it (a QB's own rush attempts count
    under QB, not RB). Real per-play efficiency, never raw fantasy points
    allowed, which is schedule-biased and noisy (CLAUDE.md's own Phase 3
    constraint). Requires a position for the rusher/receiver on each play,
    joined from the same weekly roster crosswalk nflverse.py already uses
    to resolve player identities, sourced from real games only (rush
    attempts and completed passes, not every dropback).

    Returns {defteam: {position: {"epa_allowed": float, "percentile": float,
    "plays": int}}}. Percentile is against the other 31 defenses at that
    position, lower EPA allowed is better defense, so it's inverted:
    100 = the stingiest defense at that position, 0 = the most generous.
    """
    roster_path = nflverse.ensure_cached("roster_weekly", season)
    pbp_path = nflverse.ensure_cached("pbp", season)

    con = duckdb.connect()
    try:
        rows = con.execute(
            """
            WITH crosswalk AS (
                SELECT DISTINCT season, week, gsis_id, position
                FROM read_parquet(?)
                WHERE gsis_id IS NOT NULL AND position IN ('QB', 'RB', 'WR', 'TE')
            ),
            touches AS (
                SELECT p.defteam, cw.position, p.epa
                FROM read_parquet(?) p
                JOIN crosswalk cw
                    ON cw.season = p.season AND cw.week = p.week AND cw.gsis_id = p.rusher_player_id
                WHERE p.season_type = 'REG' AND p.rush = 1 AND p.epa IS NOT NULL AND p.defteam IS NOT NULL
                UNION ALL
                SELECT p.defteam, cw.position, p.epa
                FROM read_parquet(?) p
                JOIN crosswalk cw
                    ON cw.season = p.season AND cw.week = p.week AND cw.gsis_id = p.receiver_player_id
                WHERE p.season_type = 'REG' AND p.complete_pass = 1 AND p.epa IS NOT NULL AND p.defteam IS NOT NULL
            )
            SELECT defteam, position, avg(epa) AS epa_allowed, count(*) AS plays
            FROM touches
            GROUP BY defteam, position
            """,
            [str(roster_path), str(pbp_path), str(pbp_path)],
        ).fetchall()
    finally:
        con.close()

    by_position: dict[str, list[tuple[str, float]]] = {pos: [] for pos in POSITIONS}
    raw: dict[str, dict[str, dict]] = {}
    for defteam, position, epa_allowed, plays in rows:
        raw.setdefault(defteam, {})[position] = {"epa_allowed": epa_allowed, "plays": plays}
        by_position[position].append((defteam, epa_allowed))

    result: dict[str, dict[str, dict]] = {}
    for position in POSITIONS:
        population = [epa for _, epa in by_position[position]]
        for defteam, epa_allowed in by_position[position]:
            # Inverted: less EPA allowed is better defense, so a low raw
            # value should read as a HIGH (stingy) percentile.
            pct = 100.0 - percentile_rank(epa_allowed, population)
            result.setdefault(defteam, {})[position] = {
                "epa_allowed": epa_allowed,
                "percentile": pct,
                "plays": raw[defteam][position]["plays"],
            }
    return result


def team_vegas_context(vegas_season: int, week: int, team: str) -> dict:
    """A team's real Vegas context for one specific week: its implied
    points, its own season norm, and the multiplier between them (see
    metrics.vegas_week_multiplier). Neutral (multiplier 1.0) when there's
    no line yet or no season baseline, same honest fallback matchup.py
    already uses, never a guessed direction."""
    nflverse_team = to_nflverse_team(team)
    week_implied = team_week_implied_points(vegas_season, week)
    season_avg_implied = team_season_avg_implied_points(vegas_season, week)
    implied_this_week = week_implied.get(nflverse_team)
    season_avg = season_avg_implied.get(nflverse_team)
    return {
        "implied_points": implied_this_week,
        "season_avg_implied": season_avg,
        "multiplier": vegas_week_multiplier(implied_this_week, season_avg),
        "on_bye": bool(week_implied) and nflverse_team not in week_implied,
    }


def opponent_for_week(conn: sqlite3.Connection, my_roster_id: int, week: int) -> dict | None:
    """My real Sleeper opponent for a given week: their roster_id and
    currently-set starters, the best available proxy for their actual
    lineup (they can still change it before kickoff, same as anyone can).
    None if matchups for that week haven't been synced yet (see
    sleeper.SleeperClient.sync_matchups) or aren't paired yet."""
    my_row = conn.execute(
        "SELECT matchup_id FROM matchups WHERE week = ? AND roster_id = ?", (week, my_roster_id)
    ).fetchone()
    if my_row is None or my_row["matchup_id"] is None:
        return None
    opp_row = conn.execute(
        "SELECT roster_id, starters_json FROM matchups WHERE week = ? AND matchup_id = ? AND roster_id != ?",
        (week, my_row["matchup_id"], my_roster_id),
    ).fetchone()
    if opp_row is None:
        return None
    return {"roster_id": opp_row["roster_id"], "starter_ids": json.loads(opp_row["starters_json"] or "[]")}


def project_player(
    conn: sqlite3.Connection,
    stats_season: int,
    player_id: str,
    week_implied: dict[str, float],
    season_avg_implied: dict[str, float],
) -> dict | None:
    """One player's real mean and variance for a specific week: the season
    baseline (matchup.player_weekly_distribution), injury-adjusted, then
    Vegas-adjusted for their team's specific week. The same layered
    pipeline matchup.py already uses for an ad hoc matchup, reused here
    rather than reimplemented. None if the player_id isn't in the players
    table at all (should not happen for a real roster, guarded anyway)."""
    row = conn.execute(
        "SELECT player_id, full_name, position, team, injury_status FROM players WHERE player_id = ?",
        (player_id,),
    ).fetchone()
    if row is None:
        return None

    raw_mean, raw_variance, games = player_weekly_distribution(conn, player_id, stats_season)
    injury_mean = injury_adjusted_mean(raw_mean, row["injury_status"])
    injury_variance = injury_adjusted_variance(raw_variance, row["injury_status"]) if raw_variance is not None else 0.0

    nflverse_team = to_nflverse_team(row["team"])
    on_bye = bool(week_implied) and row["team"] is not None and nflverse_team not in week_implied
    if on_bye:
        mean, variance, vegas_mult = 0.0, 0.0, 0.0
    else:
        vegas_mult = vegas_week_multiplier(week_implied.get(nflverse_team), season_avg_implied.get(nflverse_team))
        mean = injury_mean * vegas_mult
        variance = injury_variance

    return {
        "player_id": player_id,
        "full_name": row["full_name"],
        "position": row["position"],
        "team": row["team"],
        "injury_status": row["injury_status"],
        "games": games,
        "on_bye": on_bye,
        "vegas_multiplier": vegas_mult,
        "mean": mean,
        "variance": variance,
    }


FLEX_ELIGIBLE = ("RB", "WR", "TE")


def _starting_slot_counts(roster_positions: list[str]) -> dict[str, int]:
    """How many of each real starting slot this league uses, bench/taxi/IR
    excluded, read from the league's own roster_positions rather than
    hardcoded, so this stays correct if it ever runs against a differently
    shaped league."""
    counts: dict[str, int] = {}
    for slot in roster_positions:
        if slot in ("BN", "TAXI", "IR"):
            continue
        counts[slot] = counts.get(slot, 0) + 1
    return counts


def optimize_lineup(
    conn: sqlite3.Connection,
    stats_season: int,
    vegas_season: int,
    week: int,
    my_roster_id: int,
    calibration_params: tuple[float, float] | None = None,
) -> dict:
    """The starting lineup, out of everyone eligible on my roster, that
    maximizes win probability against my actual Sleeper opponent this
    week, not raw projected points. Every valid lineup respecting this
    league's real roster_positions gets evaluated (brute force; the search
    space here is small enough, low thousands of combinations at most,
    that exact search costs nothing worth trading away for an
    approximation) and the one with the highest win_probability wins, with
    the highest-raw-points lineup reported alongside for comparison, since
    they can differ. That gap is exactly what the original spec means by
    "the flex slot is the main lever": a heavy favorite should prefer the
    safer, lower-variance option even at a slightly lower mean, a heavy
    underdog the reverse.

    calibration_params (Phase 5), if given, is the (platt_a, platt_b)
    fitted by `dynasty-agent calibrate-matchup-model`. The brute-force
    search itself keeps comparing candidates by the raw win probability
    (cheap, called thousands of times per run); the correction is applied
    exactly once, to the winning lineup's final reported probability,
    since Platt scaling with a > 0 is strictly monotonic in the raw
    probability and therefore never changes which lineup has the highest
    one. calibration.fit_and_store_calibration flags (does not silently
    accept) a fitted a <= 0 specifically because that assumption would
    break here."""
    league_row = conn.execute("SELECT roster_positions_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if league_row is None:
        raise ValueError("No league data cached yet. Run `dynasty-agent sync` first.")
    roster_positions = json.loads(league_row["roster_positions_json"])
    slot_counts = _starting_slot_counts(roster_positions)
    unsupported_slots = [s for s in slot_counts if s not in ("QB", "RB", "WR", "TE", "FLEX")]

    opponent = opponent_for_week(conn, my_roster_id, week)
    week_implied = team_week_implied_points(vegas_season, week)
    season_avg_implied = team_season_avg_implied_points(vegas_season, week)

    my_player_rows = conn.execute(
        "SELECT player_id FROM roster_players WHERE roster_id = ? AND slot IN ('starter', 'bench')",
        (my_roster_id,),
    ).fetchall()
    my_projections = {
        r["player_id"]: project_player(conn, stats_season, r["player_id"], week_implied, season_avg_implied)
        for r in my_player_rows
    }
    my_projections = {pid: p for pid, p in my_projections.items() if p is not None}

    if opponent is None:
        opponent_mean, opponent_variance, opponent_note = 0.0, 0.0, "no matchup set for this week yet"
    else:
        opp_projections = [
            project_player(conn, stats_season, pid, week_implied, season_avg_implied)
            for pid in opponent["starter_ids"]
        ]
        opponent_mean = sum(p["mean"] for p in opp_projections if p)
        opponent_variance = sum(p["variance"] for p in opp_projections if p)
        opponent_note = None

    pools: dict[str, list[str]] = {
        slot: [pid for pid, p in my_projections.items() if p["position"] == slot]
        for slot in slot_counts
        if slot != "FLEX"
    }
    non_flex_slots = list(pools.keys())
    flex_count = slot_counts.get("FLEX", 0)

    best_prob, best_lineup = -1.0, None
    best_points_total, best_points_lineup = -1.0, None

    slot_combos = [itertools.combinations(pools[slot], slot_counts[slot]) for slot in non_flex_slots]
    for combo in itertools.product(*slot_combos):
        chosen: list[str] = [pid for group in combo for pid in group]
        if len(set(chosen)) != len(chosen):
            continue
        chosen_set = set(chosen)
        if flex_count:
            flex_pool = [pid for pid, p in my_projections.items() if p["position"] in FLEX_ELIGIBLE and pid not in chosen_set]
            flex_options = itertools.combinations(flex_pool, flex_count)
        else:
            flex_options = [()]
        for flex_group in flex_options:
            lineup = chosen + list(flex_group)
            mean_total = sum(my_projections[pid]["mean"] for pid in lineup)
            variance_total = sum(my_projections[pid]["variance"] for pid in lineup)
            prob = matchup_win_probability(mean_total - opponent_mean, (variance_total + opponent_variance) ** 0.5)
            if prob > best_prob:
                best_prob, best_lineup = prob, lineup
            if mean_total > best_points_total:
                best_points_total, best_points_lineup = mean_total, lineup

    recommended_win_probability = best_prob if best_lineup else None
    recommended_win_probability_calibrated = (
        platt_scale(recommended_win_probability, *calibration_params)
        if calibration_params and recommended_win_probability is not None
        else None
    )

    return {
        "week": week,
        "unsupported_slots": unsupported_slots,
        "opponent_roster_id": opponent["roster_id"] if opponent else None,
        "opponent_note": opponent_note,
        "opponent_mean": opponent_mean,
        "opponent_variance": opponent_variance,
        "recommended_lineup": [my_projections[pid] for pid in (best_lineup or [])],
        "recommended_win_probability": recommended_win_probability,
        "recommended_win_probability_calibrated": recommended_win_probability_calibrated,
        "points_max_lineup": [my_projections[pid] for pid in (best_points_lineup or [])],
        "points_max_total": best_points_total if best_points_lineup else None,
        "differs_from_points_max": bool(best_lineup) and set(best_lineup) != set(best_points_lineup or []),
        "bench": [p for pid, p in my_projections.items() if pid not in set(best_lineup or [])],
    }


# Round, labeled, not fitted from outcome data, same honesty standard as
# metrics.INJURY_MEAN_MULTIPLIER: a target valued at the very bottom of
# what's actually available still gets a token bid (spending nothing
# provides no information, and $0 bids are functionally a pass anyway),
# an elite, rare available player can justify spending most of one week's
# even pace-split share in one shot rather than losing it to someone
# who bid more.
FAAB_MIN_VALUE_MULTIPLIER = 0.2
FAAB_MAX_VALUE_MULTIPLIER = 3.0


def faab_recommendation(
    conn: sqlite3.Connection,
    stats_season: int,
    my_roster_id: int,
    player_name_or_id: str,
    weeks_left: int | None = None,
    valuations: dict | None = None,
) -> dict:
    """A suggested FAAB bid for one waiver target, sized against real
    remaining budget (rosters.waiver_budget_used, from the last sync) and
    real weeks left before the playoffs (this league's own
    playoff_week_start minus the real current week, from nfl_state),
    scaled by how the target's real win-now value (valuation.py, age and
    situation-adjusted, not just raw stats) compares to every other player
    actually available on waivers right now, not a guess at their name
    value. Raises ValueError (from resolve_player) on an unmatched or
    ambiguous name, same as the trade evaluator and predict-matchup.

    valuations, if given, is player_valuations(conn, stats_season) already
    computed by the caller: a real, non-trivial computation (it runs
    team_situation_scores under the hood), so top_faab_targets computes it
    once and passes it through here N times rather than recomputing the
    same thing per candidate."""
    player = resolve_player(conn, player_name_or_id)

    roster_row = conn.execute("SELECT waiver_budget_used FROM rosters WHERE roster_id = ?", (my_roster_id,)).fetchone()
    if roster_row is None:
        raise ValueError(f"No roster found for roster_id {my_roster_id}.")
    remaining_budget = 100 - (roster_row["waiver_budget_used"] or 0)

    if weeks_left is None:
        league_row = conn.execute("SELECT settings_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
        state_row = conn.execute("SELECT week FROM nfl_state ORDER BY fetched_at DESC LIMIT 1").fetchone()
        if league_row is None or state_row is None:
            raise ValueError("No league/state data cached yet. Run `dynasty-agent sync` first.")
        playoff_week_start = json.loads(league_row["settings_json"]).get("playoff_week_start", 15)
        current_week = state_row["week"] or 1
        weeks_left = max(playoff_week_start - current_week, 1)

    if valuations is None:
        valuations = player_valuations(conn, stats_season)
    rostered_ids = {r[0] for r in conn.execute("SELECT DISTINCT player_id FROM roster_players").fetchall()}
    available_values = [v["win_now_value"] for pid, v in valuations.items() if pid not in rostered_ids]

    is_rostered = player["player_id"] in rostered_ids
    target_valuation = valuations.get(player["player_id"])
    target_value = target_valuation["win_now_value"] if target_valuation else 0.0
    percentile = percentile_rank(target_value, available_values)

    base_per_week_budget = remaining_budget / weeks_left
    value_multiplier = FAAB_MIN_VALUE_MULTIPLIER + (FAAB_MAX_VALUE_MULTIPLIER - FAAB_MIN_VALUE_MULTIPLIER) * (percentile / 100.0)
    suggested_bid = max(0, min(round(base_per_week_budget * value_multiplier), remaining_budget))

    return {
        "player": player["full_name"],
        "position": player["position"],
        "is_rostered": is_rostered,
        "remaining_budget": remaining_budget,
        "weeks_left": weeks_left,
        "target_win_now_value": target_value,
        "percentile_among_available": percentile,
        "base_per_week_budget": base_per_week_budget,
        "value_multiplier": value_multiplier,
        "suggested_bid": suggested_bid,
    }


def top_faab_targets(conn: sqlite3.Connection, stats_season: int, my_roster_id: int, limit: int = 5) -> list[dict]:
    """The real, actually-available (not rostered by anyone) players with
    the highest win-now value right now, each already run through
    faab_recommendation for a sized bid. What `digest` surfaces so a FAAB
    suggestion doesn't require already knowing which name to ask about."""
    valuations = player_valuations(conn, stats_season)
    rostered_ids = {r[0] for r in conn.execute("SELECT DISTINCT player_id FROM roster_players").fetchall()}
    available = sorted(
        (
            (pid, v)
            for pid, v in valuations.items()
            if pid not in rostered_ids and v["win_now_value"] > 0
        ),
        key=lambda item: -item[1]["win_now_value"],
    )
    # By player_id, not name: resolve_player's name lookup can be ambiguous
    # (two real players sharing "DJ Moore" already caused a bug elsewhere
    # in this project), and the id is already known here, no need to guess.
    # valuations passed through so each call doesn't recompute the same
    # (non-trivial: it runs team_situation_scores) thing from scratch.
    return [
        faab_recommendation(conn, stats_season, my_roster_id, pid, valuations=valuations)
        for pid, _ in available[:limit]
    ]
