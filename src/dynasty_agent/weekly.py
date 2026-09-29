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
import statistics

import duckdb

import httpx

from dynasty_agent import nflverse
from dynasty_agent.matchup import (
    is_on_bye,
    player_weekly_distribution,
    team_season_avg_implied_points,
    team_week_implied_points,
    teams_playing,
)
from dynasty_agent.metrics import (
    FLEX_SLOT_ELIGIBILITY,
    injury_adjusted_mean,
    injury_adjusted_variance,
    matchup_win_probability,
    percentile_rank,
    sample_mean_variance,
    starting_slot_counts,
    vegas_week_multiplier,
)
from dynasty_agent.valuation import player_valuations, resolve_player, slot_positions, to_nflverse_team

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
        "on_bye": is_on_bye(nflverse_team, teams_playing(vegas_season, week), week_implied),
    }


def opponent_for_week(conn: sqlite3.Connection, my_roster_id: int, week: int) -> dict | None:
    """My real Sleeper opponent for a given week: their roster_id and
    currently-set starters, the best available proxy for their actual
    lineup (they can still change it before kickoff, same as anyone can).
    Sleeper writes "0" for a starting slot left empty; those are dropped, and
    empty_slots counts them. None if matchups for that week haven't been
    synced yet (see sync_matchups_or_note) or aren't paired yet."""
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
    starters = json.loads(opp_row["starters_json"] or "[]")
    set_ids = [pid for pid in starters if pid and pid != "0"]
    return {"roster_id": opp_row["roster_id"], "starter_ids": set_ids, "empty_slots": len(starters) - len(set_ids)}


def sync_matchups_or_note(conn: sqlite3.Connection, week: int) -> str | None:
    """Pull this week's matchups from Sleeper. If Sleeper can't be reached,
    keep whatever was synced before and return a note saying how old it is,
    instead of failing the whole lineup."""
    from dynasty_agent.sleeper import SleeperClient

    try:
        with SleeperClient(conn) as client:
            client.sync_matchups(week)
        return None
    except httpx.HTTPError as e:
        row = conn.execute("SELECT max(fetched_at) FROM matchups WHERE week = ?", (week,)).fetchone()
        when = f"from {row[0][:16].replace('T', ' ')} UTC" if row and row[0] else "none stored for this week"
        return f"Sleeper couldn't be reached ({type(e).__name__}), using the last synced matchups ({when})."


def position_variance_fallback(conn: sqlite3.Connection, stats_season: int) -> dict[str, float]:
    """{position: the median weekly-points sample variance among players at
    that position with 6+ games last season}. Stands in for a player whose
    own variance can't be estimated yet (a rookie before his second game),
    instead of 0, which read as a risk-free player and tilted the
    win-probability search toward him."""
    by_player: dict[tuple[str, str], list[float]] = {}
    for r in conn.execute(
        "SELECT player_id, position, fantasy_points FROM weekly_stats WHERE season = ? AND fantasy_points IS NOT NULL",
        (str(stats_season - 1),),
    ):
        by_player.setdefault((r["player_id"], r["position"]), []).append(r["fantasy_points"])
    by_position: dict[str, list[float]] = {}
    for (_, position), values in by_player.items():
        if len(values) >= 6:
            by_position.setdefault(position, []).append(sample_mean_variance(values)[1])
    return {pos: statistics.median(vs) for pos, vs in by_position.items()}


def project_player(
    conn: sqlite3.Connection,
    stats_season: int,
    player_id: str,
    week_implied: dict[str, float],
    season_avg_implied: dict[str, float],
    playing: set[str] | None = None,
    variance_fallback: dict[str, float] | None = None,
) -> dict | None:
    """One player's real mean and variance for a specific week: the season
    baseline (matchup.player_weekly_distribution), injury-adjusted, then
    Vegas-adjusted for their team's specific week. The same layered
    pipeline matchup.py already uses for an ad hoc matchup, reused here
    rather than reimplemented. None if the player_id isn't in the players
    table at all (should not happen for a real roster, guarded anyway).

    Byes come from the schedule (playing, see matchup.teams_playing). A
    player with no estimable variance gets his position's median
    (variance_fallback) and variance_estimated says so; with no fallback
    either it stays 0, flagged the same way."""
    row = conn.execute(
        "SELECT player_id, full_name, position, team, injury_status FROM players WHERE player_id = ?",
        (player_id,),
    ).fetchone()
    if row is None:
        return None

    dist = player_weekly_distribution(conn, player_id, stats_season)
    raw_mean, raw_variance, games = dist["mean"] or 0.0, dist["variance"], dist["current_games"]
    variance_estimated = raw_variance is None
    if variance_estimated:
        raw_variance = (variance_fallback or {}).get(row["position"], 0.0)
    injury_mean = injury_adjusted_mean(raw_mean, row["injury_status"])
    injury_variance = injury_adjusted_variance(raw_variance, row["injury_status"])

    nflverse_team = to_nflverse_team(row["team"])
    on_bye = is_on_bye(nflverse_team, playing or set(), week_implied)
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
        "prior_games": dist["prior_games"],
        "value_source": dist["source"],
        "on_bye": on_bye,
        "vegas_multiplier": vegas_mult,
        "mean": mean,
        "variance": variance,
        "variance_estimated": variance_estimated,
    }


# Positions this project projects: nflverse's weekly stats carry offense
# only, so a kicker, team defense or IDP player has no projection here.
PROJECTED_POSITIONS = ("QB", "RB", "WR", "TE")
REGULAR_AND_PLAYOFF_WEEKS = range(1, 19)


def _slot_order(slot_counts: dict[str, int]) -> list[str]:
    """Single-position slots first, then flex slots narrowest first, so each
    flex slot draws from whoever the narrower slots left."""
    return sorted(slot_counts, key=lambda slot: (slot in FLEX_SLOT_ELIGIBILITY, len(slot_positions(slot))))


def _lineups(projections: dict[str, dict], slot_counts: dict[str, int]):
    """Every distinct lineup as a list of (slot, player_id). Each slot takes
    as many eligible players as it can, up to its count: a roster with one
    TE for a league with two TE slots starts one and leaves a hole, instead
    of producing no lineup at all."""
    order = _slot_order(slot_counts)

    def fill(i: int, used: frozenset):
        if i == len(order):
            yield []
            return
        slot = order[i]
        eligible = slot_positions(slot)
        pool = [pid for pid, p in projections.items() if p["position"] in eligible and pid not in used]
        for group in itertools.combinations(pool, min(slot_counts[slot], len(pool))):
            for rest in fill(i + 1, used | set(group)):
                yield [(slot, pid) for pid in group] + rest

    yield from fill(0, frozenset())


def _unfilled(assignment: list[tuple[str, str]], slot_counts: dict[str, int]) -> list[str]:
    filled: dict[str, int] = {}
    for slot, _ in assignment:
        filled[slot] = filled.get(slot, 0) + 1
    return [slot for slot, count in slot_counts.items() for _ in range(count - filled.get(slot, 0))]


def best_points_lineup(projections: dict[str, dict], slot_counts: dict[str, int]) -> list[tuple[str, str]]:
    """The highest projected-points lineup (ties: lower variance)."""
    return max(
        _lineups(projections, slot_counts),
        key=lambda a: (sum(projections[pid]["mean"] for _, pid in a), -sum(projections[pid]["variance"] for _, pid in a)),
        default=[],
    )


def optimize_lineup(conn: sqlite3.Connection, stats_season: int, vegas_season: int, week: int, my_roster_id: int) -> dict:
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

    The opponent is their set lineup. If it has empty slots (not set yet,
    or set around a bye), their best projected lineup from their roster
    stands in, and opponent_source says so. With no opponent at all there
    is no win probability (None): the lineup is simply the most points,
    never a win probability against an opponent scoring 0."""
    if week not in REGULAR_AND_PLAYOFF_WEEKS:
        raise ValueError(f"Week {week} isn't an NFL week; weeks run 1 through 18.")
    league_row = conn.execute("SELECT roster_positions_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if league_row is None:
        raise ValueError("No league data cached yet. Run `dynasty-agent refresh` first.")
    slot_counts = starting_slot_counts(json.loads(league_row["roster_positions_json"]))
    unsupported_slots = [s for s in slot_counts if not slot_positions(s) & set(PROJECTED_POSITIONS)]

    opponent = opponent_for_week(conn, my_roster_id, week)
    week_implied = team_week_implied_points(vegas_season, week)
    season_avg_implied = team_season_avg_implied_points(vegas_season, week)
    playing = teams_playing(vegas_season, week)
    fallback = position_variance_fallback(conn, stats_season)

    def project(pid: str) -> dict | None:
        return project_player(conn, stats_season, pid, week_implied, season_avg_implied, playing, fallback)

    def roster_projections(roster_id: int) -> dict[str, dict]:
        rows = conn.execute(
            "SELECT player_id FROM roster_players WHERE roster_id = ? AND slot IN ('starter', 'bench')", (roster_id,)
        ).fetchall()
        projected = {r["player_id"]: project(r["player_id"]) for r in rows}
        return {pid: p for pid, p in projected.items() if p is not None}

    my_projections = roster_projections(my_roster_id)

    opponent_mean = opponent_variance = None
    opponent_source = None
    if opponent is None:
        opponent_note = "no matchup set for this week yet"
    else:
        opponent_note = None
        if opponent["empty_slots"] == 0 and opponent["starter_ids"]:
            opp_players = [p for p in (project(pid) for pid in opponent["starter_ids"]) if p]
            opponent_source = "their set lineup"
        else:
            opp_all = roster_projections(opponent["roster_id"])
            opp_players = [opp_all[pid] for _, pid in best_points_lineup(opp_all, slot_counts)]
            opponent_source = (
                "their best projected lineup (they haven't set one yet)" if not opponent["starter_ids"]
                else f"their best projected lineup (theirs has {opponent['empty_slots']} empty slot(s))"
            )
        opponent_mean = sum(p["mean"] for p in opp_players)
        opponent_variance = sum(p["variance"] for p in opp_players)

    # Ranked by (slots filled, win probability, projected points). Probability
    # saturates: with an edge big enough that the normal CDF rounds to exactly
    # 1.0, every lineup ties, and an earlier version kept whichever tied
    # lineup it reached first, starting a 4-point RB over a 9-point WR in the
    # flex. With no opponent, points alone decide.
    best_key, best, best_prob = None, [], None
    points_key, points_best = None, []
    for assignment in _lineups(my_projections, slot_counts):
        mean_total = sum(my_projections[pid]["mean"] for _, pid in assignment)
        variance_total = sum(my_projections[pid]["variance"] for _, pid in assignment)
        prob = None
        if opponent_mean is not None:
            prob = matchup_win_probability(mean_total - opponent_mean, (variance_total + opponent_variance) ** 0.5)
        key = (len(assignment), prob if prob is not None else 0.0, mean_total)
        if best_key is None or key > best_key:
            best_key, best, best_prob = key, assignment, prob
        pkey = (len(assignment), mean_total, -variance_total)
        if points_key is None or pkey > points_key:
            points_key, points_best = pkey, assignment

    best_ids = [pid for _, pid in best]
    points_ids = [pid for _, pid in points_best]
    slot_of = {pid: slot for slot, pid in best}
    swaps = [
        {"starts": my_projections[i]["full_name"], "slot": slot_of[i], "over": my_projections[o]["full_name"]}
        for i, o in zip(
            sorted(set(best_ids) - set(points_ids), key=lambda pid: -my_projections[pid]["mean"]),
            sorted(set(points_ids) - set(best_ids), key=lambda pid: -my_projections[pid]["mean"]),
        )
    ]

    return {
        "week": week,
        "unsupported_slots": unsupported_slots,
        "empty_slots": _unfilled(best, slot_counts),
        "opponent_roster_id": opponent["roster_id"] if opponent else None,
        "opponent_note": opponent_note,
        "opponent_source": opponent_source,
        "opponent_mean": opponent_mean,
        "opponent_variance": opponent_variance,
        "recommended_lineup": [my_projections[pid] for pid in best_ids],
        "recommended_slots": [slot for slot, _ in best],
        "recommended_win_probability": best_prob,
        "points_max_lineup": [my_projections[pid] for pid in points_ids],
        "points_max_total": points_key[1] if points_key else None,
        "differs_from_points_max": set(best_ids) != set(points_ids),
        "swaps_from_points_max": swaps,
        "bench": [p for pid, p in my_projections.items() if pid not in set(best_ids)],
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

# Sleeper's own default FAAB budget, used only when the league's settings
# don't carry waiver_budget at all, and reported as a default when used.
DEFAULT_FAAB_BUDGET = 100


def _available_player_ids(conn: sqlite3.Connection, valuations: dict, rostered_ids: set[str]) -> list[str]:
    """Players with a valuation who could actually be claimed right now: on
    no roster in this league and on an NFL team today. valuations come from
    a completed season's stats, so without the team check every player who
    retired or went unsigned since reads as a free agent, inflating the
    percentile pool and able to top the FAAB target list."""
    on_nfl_team = {r[0] for r in conn.execute("SELECT player_id FROM players WHERE team IS NOT NULL").fetchall()}
    return [pid for pid in valuations if pid not in rostered_ids and pid in on_nfl_team]


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
    league_row = conn.execute("SELECT settings_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if league_row is None:
        raise ValueError("No league data cached yet. Run `dynasty-agent sync` first.")
    league_settings = json.loads(league_row["settings_json"])
    # The league's real FAAB budget, not a hardcoded $100 (an earlier
    # version's assumption, right for this league only by coincidence).
    total_budget = league_settings.get("waiver_budget")
    budget_is_default = total_budget is None
    if budget_is_default:
        total_budget = DEFAULT_FAAB_BUDGET
    remaining_budget = total_budget - (roster_row["waiver_budget_used"] or 0)

    if weeks_left is None:
        state_row = conn.execute("SELECT week FROM nfl_state ORDER BY fetched_at DESC LIMIT 1").fetchone()
        if state_row is None:
            raise ValueError("No NFL state cached yet. Run `dynasty-agent sync` first.")
        playoff_week_start = league_settings.get("playoff_week_start", 15)
        current_week = state_row["week"] or 1
        weeks_left = max(playoff_week_start - current_week, 1)

    if valuations is None:
        valuations = player_valuations(conn, stats_season)
    rostered_ids = {r[0] for r in conn.execute("SELECT DISTINCT player_id FROM roster_players").fetchall()}
    available_ids = _available_player_ids(conn, valuations, rostered_ids)
    available_values = [valuations[pid]["win_now_value"] for pid in available_ids]

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
        "total_budget": total_budget,
        "budget_is_default": budget_is_default,
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
            (pid, valuations[pid])
            for pid in _available_player_ids(conn, valuations, rostered_ids)
            if valuations[pid]["win_now_value"] > 0
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
