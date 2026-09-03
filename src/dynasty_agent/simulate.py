"""Phase 6: Monte Carlo season-outcome simulation, built on Phase 5's
calibrated matchup model.

Verified live before writing this, the same discipline the rest of this
project follows: queried the real Sleeper matchups endpoint
(`/league/{id}/matchups/{week}`) directly against this league for weeks 1,
2, 5, 10, 14, 15, and 17 while still at week 1 of the season. Confirmed:
every regular-season week already has real, complete matchup_id pairings
for all 12 rosters, Sleeper pre-generates the full schedule at season
start, it does not wait for each week to arrive. That is exactly what
this module needs.

One real nuance the same check surfaced: weeks 15-17 (the real playoff
weeks for this league) also returned pairings, but those almost certainly
do not reflect the real eventual playoff bracket, Sleeper seeds playoffs
from final regular-season standings, which do not exist yet this early.
This turns out not to matter here: "playoff odds" is a regular-season
outcome (who finishes in a qualifying seed), not a simulation of the
bracket games themselves, so this module only ever reads weeks from_week
through playoff_week_start - 1 and never touches playoff-week pairings.

Stated simplifying assumption, consistent with weekly.optimize_lineup's
own: each team's projection for a future week assumes their CURRENT real
starters hold for the rest of the season. This measures roster strength
given today's roster, not perfect future in-season lineup management.
"""

from __future__ import annotations

import json
import random
import sqlite3

from dynasty_agent.matchup import team_season_avg_implied_points, team_week_implied_points
from dynasty_agent.metrics import matchup_win_probability, platt_scale
from dynasty_agent.weekly import project_player


def _team_projection_for_week(
    conn: sqlite3.Connection,
    stats_season: int,
    roster_id: int,
    week_implied: dict,
    season_avg_implied: dict,
) -> tuple[float, float]:
    """Sum of weekly.project_player (reused, not reimplemented) over that
    roster's real current starters."""
    player_ids = [
        r[0]
        for r in conn.execute(
            "SELECT player_id FROM roster_players WHERE roster_id = ? AND slot = 'starter'", (roster_id,)
        ).fetchall()
    ]
    mean_total, variance_total = 0.0, 0.0
    for player_id in player_ids:
        p = project_player(conn, stats_season, player_id, week_implied, season_avg_implied)
        if p is not None:
            mean_total += p["mean"]
            variance_total += p["variance"]
    return mean_total, variance_total


def simulate_season(
    conn: sqlite3.Connection,
    stats_season: int,
    vegas_season: int,
    from_week: int,
    n_simulations: int = 10000,
    seed: int | None = None,
    calibration_params: tuple[float, float] | None = None,
) -> dict:
    """Monte Carlo over the league's real remaining regular-season
    schedule. Each real matchup gets ONE calibrated win probability
    (computed once, reusing the same mean/variance/Vegas machinery
    matchup.py already uses), then resampled as a single Bernoulli draw
    per simulation, not by resampling full player distributions again: the
    win probability already collapses that. Raises ValueError if there is
    nothing real to simulate (matchups not synced yet for any week in
    range), never returns a guessed result."""
    league_row = conn.execute("SELECT settings_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if league_row is None:
        raise ValueError("No league data cached yet. Run `dynasty-agent sync` first.")
    settings = json.loads(league_row["settings_json"])
    playoff_week_start = settings.get("playoff_week_start", 15)
    playoff_teams = settings.get("playoff_teams", 6)

    roster_rows = conn.execute("SELECT roster_id, wins, fpts FROM rosters").fetchall()
    if not roster_rows:
        raise ValueError("No rosters synced yet. Run `dynasty-agent sync` first.")
    current_wins = {r["roster_id"]: (r["wins"] or 0) for r in roster_rows}
    current_fpts = {r["roster_id"]: (r["fpts"] or 0.0) for r in roster_rows}
    all_roster_ids = list(current_wins.keys())

    # One calibrated win probability per real remaining matchup, computed
    # once, not per simulation trial.
    week_matchups: list[tuple[int, int, float]] = []  # (roster_a, roster_b, win_probability_a)
    for week in range(from_week, playoff_week_start):
        rows = conn.execute(
            "SELECT roster_id, matchup_id FROM matchups WHERE week = ? AND matchup_id IS NOT NULL", (week,)
        ).fetchall()
        if not rows:
            continue  # not synced for this week, skip rather than guess

        by_matchup: dict[int, list[int]] = {}
        for r in rows:
            by_matchup.setdefault(r["matchup_id"], []).append(r["roster_id"])

        week_implied = team_week_implied_points(vegas_season, week)
        season_avg_implied = team_season_avg_implied_points(vegas_season, week)

        for roster_ids in by_matchup.values():
            if len(roster_ids) != 2:
                continue  # an unpaired or malformed entry, skip rather than guess a result
            roster_a, roster_b = roster_ids
            mean_a, var_a = _team_projection_for_week(conn, stats_season, roster_a, week_implied, season_avg_implied)
            mean_b, var_b = _team_projection_for_week(conn, stats_season, roster_b, week_implied, season_avg_implied)
            raw_prob_a = matchup_win_probability(mean_a - mean_b, (var_a + var_b) ** 0.5)
            win_prob_a = platt_scale(raw_prob_a, *calibration_params) if calibration_params else raw_prob_a
            week_matchups.append((roster_a, roster_b, win_prob_a))

    if not week_matchups:
        raise ValueError(
            f"No real remaining-schedule matchups found from week {from_week} through "
            f"{playoff_week_start - 1}. Run `dynasty-agent sync` and sync matchups for those weeks first."
        )

    rng = random.Random(seed)
    wins_sum = {rid: 0 for rid in all_roster_ids}
    rank_sum = {rid: 0 for rid in all_roster_ids}
    playoff_count = {rid: 0 for rid in all_roster_ids}
    last_place_count = {rid: 0 for rid in all_roster_ids}
    n_teams = len(all_roster_ids)

    for _ in range(n_simulations):
        sim_wins = dict(current_wins)
        for roster_a, roster_b, win_prob_a in week_matchups:
            if rng.random() < win_prob_a:
                sim_wins[roster_a] = sim_wins.get(roster_a, 0) + 1
            else:
                sim_wins[roster_b] = sim_wins.get(roster_b, 0) + 1

        # Real current fpts as the tiebreak: a stated approximation of
        # Sleeper's actual tiebreak rule, not identical to it, and static
        # (not itself simulated) on purpose, simulating full point totals
        # is out of scope here.
        standings = sorted(all_roster_ids, key=lambda rid: (-sim_wins.get(rid, 0), -current_fpts.get(rid, 0.0)))
        for rank, rid in enumerate(standings, start=1):
            wins_sum[rid] += sim_wins.get(rid, 0)
            rank_sum[rid] += rank
            if rank <= playoff_teams:
                playoff_count[rid] += 1
            if rank == n_teams:
                last_place_count[rid] += 1

    teams = {
        rid: {
            "current_wins": current_wins[rid],
            "avg_final_wins": wins_sum[rid] / n_simulations,
            "playoff_odds": playoff_count[rid] / n_simulations,
            "avg_final_rank": rank_sum[rid] / n_simulations,
            "last_place_odds": last_place_count[rid] / n_simulations,
        }
        for rid in all_roster_ids
    }

    return {
        "stats_season": stats_season,
        "vegas_season": vegas_season,
        "from_week": from_week,
        "playoff_week_start": playoff_week_start,
        "playoff_teams": playoff_teams,
        "n_simulations": n_simulations,
        "calibration_used": calibration_params is not None,
        "real_matchups_simulated": len(week_matchups),
        "teams": teams,
    }
