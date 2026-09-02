"""Ad hoc win probability between two arbitrary rosters, not necessarily
your own league. This generalizes a one-off "who wins this weekend" lookup
into real, reusable code.

Read this as a heuristic, not a calibrated prediction: it has never been
checked against real outcomes, this project has no historical win/loss
data to check it against. What it does do honestly is use each player's
real weekly fantasy_points from a completed nflverse season, not a guess,
for both the mean it expects from them and the week-to-week variance
around that mean. See metrics.py's comment above matchup_win_probability
for the model itself and its independence assumption.

It reads fantasy_points straight out of weekly_stats, which nflverse.py
computed with THIS league's own scoring_settings (full PPR, no kicker or
defense scoring, this league doesn't roster those). For a QB/RB/WR/TE in
someone else's league that's still a reasonable stand-in, most leagues
score those positions similarly. For a kicker or team defense there is no
meaningful data here at all, this pipeline never scores kicking or defense
stats, that player comes back with 0 games and gets flagged, not silently
folded in as a zero.
"""

from __future__ import annotations

import sqlite3

from dynasty_agent.metrics import injury_adjusted_mean, matchup_win_probability
from dynasty_agent.valuation import resolve_player


def player_weekly_distribution(conn: sqlite3.Connection, player_id: str, season: int) -> tuple[float, float, int]:
    """A player's mean and population variance of weekly fantasy points in
    a season, from real games, plus the game count backing it. (0.0, 0.0, 0)
    for a player with no rows that season, for example a rookie who hasn't
    played yet or a kicker/defense this project's nflverse ingestion
    doesn't carry (see nflverse.py: kicking and team defense aren't in the
    per-player pipeline built here)."""
    rows = [
        r[0]
        for r in conn.execute(
            "SELECT fantasy_points FROM weekly_stats WHERE player_id = ? AND season = ? AND fantasy_points IS NOT NULL",
            (player_id, str(season)),
        ).fetchall()
    ]
    if not rows:
        return 0.0, 0.0, 0
    mean = sum(rows) / len(rows)
    variance = sum((x - mean) ** 2 for x in rows) / len(rows)
    return mean, variance, len(rows)


def _value_matchup_side(conn: sqlite3.Connection, season: int, names: list[str]) -> dict:
    players = []
    mean_total = 0.0
    variance_total = 0.0
    for name_or_id in names:
        p = resolve_player(conn, name_or_id)
        raw_mean, variance, games = player_weekly_distribution(conn, p["player_id"], season)
        adjusted_mean = injury_adjusted_mean(raw_mean, p["injury_status"])
        players.append(
            {
                "full_name": p["full_name"],
                "position": p["position"],
                "team": p["team"],
                "injury_status": p["injury_status"],
                "raw_mean": raw_mean,
                "adjusted_mean": adjusted_mean,
                "variance": variance,
                "games": games,
            }
        )
        mean_total += adjusted_mean
        variance_total += variance
    return {"players": players, "mean": mean_total, "variance": variance_total}


def predict_matchup(conn: sqlite3.Connection, season: int, team_a: list[str], team_b: list[str]) -> dict:
    """Both sides valued, meaned, varied, and turned into a win probability
    for team_a. Raises ValueError (from resolve_player) on an unmatched or
    ambiguous name, same behavior as the trade evaluator."""
    a = _value_matchup_side(conn, season, team_a)
    b = _value_matchup_side(conn, season, team_b)

    mean_diff = a["mean"] - b["mean"]
    std_diff = (a["variance"] + b["variance"]) ** 0.5
    win_prob_a = matchup_win_probability(mean_diff, std_diff)

    return {
        "season": season,
        "team_a": a,
        "team_b": b,
        "mean_diff": mean_diff,
        "std_diff": std_diff,
        "win_probability_a": win_prob_a,
        "win_probability_b": 1.0 - win_prob_a,
    }
