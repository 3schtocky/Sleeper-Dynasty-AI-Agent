"""DRAFT MODEL. Win probability for a specific NFL week between two
arbitrary rosters, not necessarily your own league. This generalizes a
one-off "who wins this weekend" lookup into real, reusable code, however it
is a first draft, not a finished prediction engine. The intended next step
is a proper prediction mode (calibrated against real outcomes, this project
has none yet) with a trade bot built on top of it; this module is what that
gets built from, not the finished thing. See PLANNING.md's "Out-of-band"
section for the current status.

Read this as a heuristic, not a calibrated prediction: it has never been
checked against real outcomes, this project has no historical win/loss
data to check it against. What it does do honestly is build from real
numbers at every step:

- Each player's mean and sample variance of weekly fantasy_points from a
  completed nflverse season (metrics.sample_mean_variance, Bessel's
  correction, not the population-variance shortcut an earlier version used).
- A real per-team Vegas line for the specific week being predicted,
  spread_line and total_line straight from nflverse's free, unauthenticated
  schedules file, not a paid odds API and not a hardcoded team bias.

See metrics.py's comments above matchup_win_probability, injury_adjusted_variance,
and vegas_week_multiplier for the model itself and its stated assumptions
(players score independently of teammates; a missing Vegas baseline gets a
neutral 1.0 multiplier, never a guessed direction).

Nonpartisan by construction, not just by claim: there is no team- or
player-identity lookup table anywhere in this module or in metrics.py's
vegas_week_multiplier/injury_adjusted_mean/injury_adjusted_variance. Team A
and Team B run through the exact same functions with the exact same
formulas; a team's number only moves because of its own real data (its
players' actual weekly fantasy_points, its own current Vegas line, its own
players' actual injury designations), never because of which team it is.
This was a deliberate rejection of a pattern found in a third-party repo
reviewed for this project, which hardcoded a specific favored/disfavored
team list (see PLANNING.md's "Out-of-band" section) into its scoring;
nothing like that exists here.

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

import duckdb

from dynasty_agent.metrics import (
    injury_adjusted_mean,
    injury_adjusted_variance,
    matchup_win_probability,
    sample_mean_variance,
    vegas_week_multiplier,
)
from dynasty_agent.valuation import resolve_player, to_nflverse_team

GAMES_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.parquet"


def player_weekly_distribution(conn: sqlite3.Connection, player_id: str, season: int) -> tuple[float, float | None, int]:
    """A player's mean and unbiased sample variance (see
    metrics.sample_mean_variance) of weekly fantasy points in a season, from
    real games, plus the game count backing it. variance is None for fewer
    than 2 games, a sample variance is undefined from 0 or 1 points, that
    includes a rookie who hasn't played yet or a kicker/defense this
    project's nflverse ingestion doesn't carry (see nflverse.py: kicking and
    team defense aren't in the per-player pipeline built here)."""
    rows = [
        r[0]
        for r in conn.execute(
            "SELECT fantasy_points FROM weekly_stats WHERE player_id = ? AND season = ? AND fantasy_points IS NOT NULL",
            (player_id, str(season)),
        ).fetchall()
    ]
    return sample_mean_variance(rows)


def team_week_implied_points(season: int, week: int) -> dict[str, float]:
    """Vegas-implied points for every team with a scheduled game in a given
    week, derived from real spread_line/total_line in nflverse's free
    schedules file (implied = total/2 +/- spread/2). A team on a bye that
    week is simply absent from the result, not present with a fabricated
    number. Empty dict if this week's lines aren't published yet."""
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    try:
        rows = con.execute(
            """
            SELECT away_team, home_team, spread_line, total_line
            FROM read_parquet(?)
            WHERE season = ? AND week = ? AND spread_line IS NOT NULL AND total_line IS NOT NULL
            """,
            [GAMES_URL, season, week],
        ).fetchall()
    finally:
        con.close()
    implied: dict[str, float] = {}
    for away, home, spread, total in rows:
        implied[away] = total / 2 - spread / 2
        implied[home] = total / 2 + spread / 2
    return implied


def team_season_avg_implied_points(season: int, before_week: int) -> dict[str, float]:
    """Each team's average Vegas-implied points across their own completed
    games so far this season, weeks strictly before before_week only, so
    this never uses a future week's line to describe a team's "normal."
    Empty for week 1: there is nothing prior to average yet, and
    vegas_week_multiplier treats that as a neutral 1.0, not a guess."""
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    try:
        rows = con.execute(
            """
            SELECT away_team, home_team, spread_line, total_line
            FROM read_parquet(?)
            WHERE season = ? AND week < ? AND spread_line IS NOT NULL AND total_line IS NOT NULL
            """,
            [GAMES_URL, season, before_week],
        ).fetchall()
    finally:
        con.close()
    totals: dict[str, list[float]] = {}
    for away, home, spread, total in rows:
        totals.setdefault(away, []).append(total / 2 - spread / 2)
        totals.setdefault(home, []).append(total / 2 + spread / 2)
    return {team: sum(vals) / len(vals) for team, vals in totals.items()}


def _value_matchup_side(
    conn: sqlite3.Connection,
    season: int,
    names: list[str],
    week_implied: dict[str, float],
    season_avg_implied: dict[str, float],
    week_has_data: bool,
) -> dict:
    players = []
    mean_total = 0.0
    variance_total = 0.0
    for name_or_id in names:
        p = resolve_player(conn, name_or_id)
        raw_mean, raw_variance, games = player_weekly_distribution(conn, p["player_id"], season)
        injury_mean = injury_adjusted_mean(raw_mean, p["injury_status"])
        # A None raw_variance (0 or 1 games) means there is no sample-based
        # estimate at all, not that the true variance is zero. Contributing
        # 0.0 here is the same "insufficient data, treat as absent from the
        # variance side of the model" choice already made for a 0-game
        # player; thin_sample flags it below so it's visible, not hidden.
        injury_variance = injury_adjusted_variance(raw_variance, p["injury_status"]) if raw_variance is not None else 0.0

        # Sleeper and nflverse disagree on a couple of team codes (the Rams:
        # Sleeper "LAR", nflverse "LA"), confirmed by diffing the two sets
        # directly, see valuation.TEAM_ALIASES. week_implied/season_avg_implied
        # are keyed by nflverse's codes, so the lookup normalizes; the
        # player's own displayed team stays whatever Sleeper calls it.
        team = p["team"]
        nflverse_team = to_nflverse_team(team)
        on_bye = week_has_data and nflverse_team is not None and nflverse_team not in week_implied
        if on_bye:
            vegas_mult = 0.0
            final_mean = 0.0
            final_variance = 0.0
        else:
            vegas_mult = vegas_week_multiplier(week_implied.get(nflverse_team), season_avg_implied.get(nflverse_team))
            final_mean = injury_mean * vegas_mult
            final_variance = injury_variance  # Vegas context shifts the mean, not the week-to-week spread around it.

        players.append(
            {
                "full_name": p["full_name"],
                "position": p["position"],
                "team": team,
                "injury_status": p["injury_status"],
                "raw_mean": raw_mean,
                "injury_adjusted_mean": injury_mean,
                "vegas_multiplier": vegas_mult,
                "adjusted_mean": final_mean,
                "variance": final_variance,
                "on_bye": on_bye,
                "thin_sample": games <= 1,
                "games": games,
            }
        )
        mean_total += final_mean
        variance_total += final_variance
    return {"players": players, "mean": mean_total, "variance": variance_total}


def predict_matchup(
    conn: sqlite3.Connection, season: int, vegas_season: int, week: int, team_a: list[str], team_b: list[str]
) -> dict:
    """Both sides valued for a specific week, meaned, varied, Vegas-adjusted,
    and turned into a win probability for team_a. Raises ValueError (from
    resolve_player) on an unmatched or ambiguous name, same behavior as the
    trade evaluator.

    Two season numbers, deliberately not one, and neither one guessed from
    the other: `season` is the FPPG/variance baseline (normally the most
    recently completed season, real games), `vegas_season` is which
    season's real week `week` to price. An earlier version tried to infer
    vegas_season from season (falling back to season + 1 when season/week
    had no lines) and that was wrong in exactly the case that matters most:
    predicting week 1 of a new season, where season/week 1 already has real
    CLOSING lines from last year's completed game, so the fallback never
    fired and it silently priced the wrong year's matchups. The caller
    (see cli.py) resolves vegas_season from the actual current NFL season,
    not a guess."""
    week_implied = team_week_implied_points(vegas_season, week)
    season_avg_implied = team_season_avg_implied_points(vegas_season, week)
    week_has_data = bool(week_implied)

    a = _value_matchup_side(conn, season, team_a, week_implied, season_avg_implied, week_has_data)
    b = _value_matchup_side(conn, season, team_b, week_implied, season_avg_implied, week_has_data)

    mean_diff = a["mean"] - b["mean"]
    std_diff = (a["variance"] + b["variance"]) ** 0.5
    win_prob_a = matchup_win_probability(mean_diff, std_diff)

    return {
        "season": season,
        "week": week,
        "vegas_season": vegas_season,
        "week_has_vegas_data": week_has_data,
        "team_a": a,
        "team_b": b,
        "mean_diff": mean_diff,
        "std_diff": std_diff,
        "win_probability_a": win_prob_a,
        "win_probability_b": 1.0 - win_prob_a,
    }
