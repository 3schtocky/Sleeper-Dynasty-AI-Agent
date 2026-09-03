"""Phase 5: backtests matchup.py's win-probability heuristic against real
historical NFL games and fits a calibration correction.

**A real design decision, stated plainly, not glossed over:** orienting
every backtest game as home=team_a would let Platt scaling's global `b`
silently absorb any real home-field effect (the raw heuristic has no
home/away concept at all). That correction then gets applied everywhere,
including `optimize-lineup`, where "team A vs team B" means your fantasy
roster vs your opponent's and home/away is meaningless. Fix: for each real
game, which real side plays "team A" is picked by `random.Random(seed)`,
a fixed, documented seed for reproducibility, so any real home-field
effect gets absorbed as noise across both classes instead of baked into a
directional bias. This is the calibration-specific expression of this
project's existing nonpartisan-by-construction rule (see matchup.py): the
correction has to be a property of the formula, not an artifact of how the
backtest happened to orient real games.

**A stated limitation, not hidden:** this corrects the heuristic's overall
over/under-confidence. It adds no new information the raw heuristic didn't
already have; home-field advantage specifically stays entirely unmodeled,
exactly as before. A real home-field feature is a future improvement, not
this one.

**The hindsight-roster assumption, stated in the same style matchup.py's
own docstring already uses:** a historical team's "roster" for one game is
reconstructed from who actually recorded a real weekly_stats row for that
team in that week, not a true pre-game depth chart. This project has no
historical Sleeper-style lineup for, say, the 2014 Broncos, this is the
only real roster concept available for an arbitrary past NFL team.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

import duckdb
import httpx

from dynasty_agent import matchup, nfl_extra
from dynasty_agent.config import NFLVERSE_CACHE_DIR
from dynasty_agent.db import utcnow
from dynasty_agent.metrics import (
    brier_score,
    fit_platt_scaling,
    injury_adjusted_mean,
    injury_adjusted_variance,
    log_loss,
    matchup_win_probability,
    platt_scale,
    vegas_week_multiplier,
)

# The seed is fixed and documented, not left to chance: it makes the
# random team_a/team_b assignment (see module docstring) reproducible run
# to run, so a re-run against the same season range reproduces the same
# backtest_games rows, not a new random sample every time.
DEFAULT_SEED = 20260902


def ensure_games_cached(force: bool = False) -> Path:
    """Whole-history download of matchup.GAMES_URL to data/nflverse/, once,
    same pattern prospects.ensure_cached already uses for draft_picks and
    combine. matchup.py's own functions stay network-based (fine for their
    normal one-or-two-calls-per-run usage); a ~4,000-game backtest needs
    real Vegas context for every (season, week) pair, hitting the network
    that many times would be wasteful, not just slow."""
    NFLVERSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = NFLVERSE_CACHE_DIR / "games.parquet"
    if dest.exists() and not force:
        return dest

    with httpx.stream("GET", matchup.GAMES_URL, timeout=60.0, follow_redirects=True) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
    return dest


def predicted_team_distribution(
    conn: sqlite3.Connection,
    baseline_season: int,
    season: int,
    week: int,
    team: str,
    week_implied: dict,
    season_avg_implied: dict,
) -> tuple[float, float, int]:
    """One historical team's predicted mean/variance for one real game,
    built from the SAME mean/variance/injury/Vegas machinery matchup.py's
    own _value_matchup_side uses, just driven by a hindsight roster (see
    module docstring) instead of a user-specified player list. Returns
    (mean_total, variance_total, players_found); players_found is stored
    downstream as a real coverage/confidence signal, not hidden."""
    rows = conn.execute(
        "SELECT DISTINCT player_id, gsis_id FROM weekly_stats "
        "WHERE team = ? AND season = ? AND week = ? AND gsis_id IS NOT NULL",
        (team, str(season), week),
    ).fetchall()

    mean_total, variance_total = 0.0, 0.0
    for player_id, gsis_id in rows:
        raw_mean, raw_variance, _games = matchup.player_weekly_distribution(conn, player_id, baseline_season)
        injury_status = nfl_extra.report_status_for_week(conn, season, week, gsis_id)
        injury_mean = injury_adjusted_mean(raw_mean, injury_status)
        injury_variance = injury_adjusted_variance(raw_variance, injury_status) if raw_variance is not None else 0.0
        vegas_mult = vegas_week_multiplier(week_implied.get(team), season_avg_implied.get(team))
        mean_total += injury_mean * vegas_mult
        variance_total += injury_variance
    return mean_total, variance_total, len(rows)


def build_backtest_sample(
    conn: sqlite3.Connection, start_season: int, end_season: int, seed: int = DEFAULT_SEED
) -> list[dict]:
    """For each real completed REG game in [start_season, end_season] (from
    the local games.parquet cache): baseline_season = season - 1, the real
    prior season's per-player stats as the projection input, matching
    matchup.predict_matchup's own two-season design. Games where neither
    side has a single real weekly_stats row to project from (no baseline
    data ingested for that team/week) are skipped, there's nothing real to
    backtest there. Replaces any prior backtest_games rows for this exact
    season range, then returns the same rows as dicts."""
    games_path = ensure_games_cached()

    con = duckdb.connect()
    try:
        games = con.execute(
            """
            SELECT season, week, home_team, away_team, home_score, away_score
            FROM read_parquet(?)
            WHERE game_type = 'REG' AND season >= ? AND season <= ?
              AND home_score IS NOT NULL AND away_score IS NOT NULL
            """,
            [str(games_path), start_season, end_season],
        ).fetchall()
    finally:
        con.close()

    rng = random.Random(seed)
    fetched_at = utcnow()
    implied_cache: dict[tuple[int, int], tuple[dict, dict]] = {}
    rows = []

    for season, week, home_team, away_team, home_score, away_score in games:
        baseline_season = season - 1

        cache_key = (season, week)
        if cache_key not in implied_cache:
            week_implied = matchup.team_week_implied_points(season, week, games_source=str(games_path))
            season_avg_implied = matchup.team_season_avg_implied_points(season, week, games_source=str(games_path))
            implied_cache[cache_key] = (week_implied, season_avg_implied)
        week_implied, season_avg_implied = implied_cache[cache_key]

        # The random team_a/team_b assignment this module's docstring
        # explains: which real side plays "team A" is a coin flip, not
        # always the home team.
        team_a_is_home = rng.random() < 0.5
        team_a, team_b = (home_team, away_team) if team_a_is_home else (away_team, home_team)

        a_mean, a_var, a_found = predicted_team_distribution(
            conn, baseline_season, season, week, team_a, week_implied, season_avg_implied
        )
        b_mean, b_var, b_found = predicted_team_distribution(
            conn, baseline_season, season, week, team_b, week_implied, season_avg_implied
        )
        if a_found == 0 or b_found == 0:
            continue  # nothing real to project one side from, skip rather than record a meaningless 50/50

        mean_diff = a_mean - b_mean
        std_diff = (a_var + b_var) ** 0.5
        raw_win_probability_a = matchup_win_probability(mean_diff, std_diff)

        a_won = (home_score > away_score) if team_a_is_home else (away_score > home_score)
        b_won = (home_score < away_score) if team_a_is_home else (away_score < home_score)
        actual_a_win = 1.0 if a_won else (0.0 if b_won else 0.5)

        rows.append(
            {
                "season": season,
                "week": week,
                "team_a": team_a,
                "team_b": team_b,
                "team_a_is_home": team_a_is_home,
                "raw_win_probability_a": raw_win_probability_a,
                "predicted_margin": mean_diff,
                "predicted_std": std_diff,
                "team_a_players_found": a_found,
                "team_b_players_found": b_found,
                "actual_a_win": actual_a_win,
                "fetched_at": fetched_at,
            }
        )

    conn.execute("DELETE FROM backtest_games WHERE season >= ? AND season <= ?", (start_season, end_season))
    conn.executemany(
        """
        INSERT INTO backtest_games (
            season, week, team_a, team_b, team_a_is_home, raw_win_probability_a,
            predicted_margin, predicted_std, team_a_players_found, team_b_players_found,
            actual_a_win, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r["season"], r["week"], r["team_a"], r["team_b"], int(r["team_a_is_home"]),
                r["raw_win_probability_a"], r["predicted_margin"], r["predicted_std"],
                r["team_a_players_found"], r["team_b_players_found"], r["actual_a_win"], r["fetched_at"],
            )
            for r in rows
        ],
    )
    conn.commit()
    return rows


def _accuracy(pairs: list[tuple[float, float]]) -> float:
    """Fraction of real games where the predicted favorite (probability
    >= 0.5) matches the actual winner. A real tie (actual_outcome 0.5)
    counts as a "win" here for simplicity, a diagnostic number alongside
    Brier score and log-loss, not itself load-bearing anywhere."""
    if not pairs:
        return 0.0
    return sum(1 for p, y in pairs if (p >= 0.5) == (y >= 0.5)) / len(pairs)


def fit_and_store_calibration(conn: sqlite3.Connection, start_season: int, end_season: int) -> dict:
    """Orchestrates build_backtest_sample, fits a global Platt-scaling
    correction, scores the heuristic before and after on the same real
    sample, and stores one new calibration_params row. Raises ValueError
    if the season range produced no backtestable games at all (the real
    prerequisite, backfill-history, hasn't been run). Prints, does not
    silently accept, a fitted a <= 0: weekly.optimize_lineup's
    argmax-preservation argument depends on Platt scaling staying
    monotonic increasing."""
    rows = build_backtest_sample(conn, start_season, end_season)
    pairs = [(r["raw_win_probability_a"], r["actual_a_win"]) for r in rows]

    if not pairs:
        raise ValueError(
            f"No backtestable real games found for {start_season}-{end_season}. Run "
            f"`dynasty-agent backfill-history --start-season {start_season - 1} --end-season {end_season}` first."
        )

    platt_a, platt_b = fit_platt_scaling(pairs)
    if platt_a <= 0:
        print(
            f"Warning: fitted calibration has a={platt_a:.4f} <= 0. That means the raw heuristic's ranking "
            f"is not preserved by this correction, optimize-lineup's argmax shortcut assumes a > 0. Storing "
            f"it anyway since it is the real fit, not silently discarding it, but treat the result as suspect.",
            file=sys.stderr,
        )

    calibrated_pairs = [(platt_scale(p, platt_a, platt_b), y) for p, y in pairs]

    summary = {
        "start_season": start_season,
        "end_season": end_season,
        "sample_size": len(pairs),
        "platt_a": platt_a,
        "platt_b": platt_b,
        "brier_before": brier_score(pairs),
        "brier_after": brier_score(calibrated_pairs),
        "log_loss_before": log_loss(pairs),
        "log_loss_after": log_loss(calibrated_pairs),
        "accuracy_before": _accuracy(pairs),
        "accuracy_after": _accuracy(calibrated_pairs),
    }

    conn.execute(
        """
        INSERT INTO calibration_params (
            fitted_at, start_season, end_season, sample_size, platt_a, platt_b,
            brier_before, brier_after, log_loss_before, log_loss_after, accuracy_before, accuracy_after
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utcnow(), start_season, end_season, summary["sample_size"], platt_a, platt_b,
            summary["brier_before"], summary["brier_after"], summary["log_loss_before"],
            summary["log_loss_after"], summary["accuracy_before"], summary["accuracy_after"],
        ),
    )
    conn.commit()
    return summary


def current_calibration(conn: sqlite3.Connection) -> tuple[float, float] | None:
    """The latest fitted (platt_a, platt_b), or None if calibrate-matchup-
    model has never been run. Callers degrade to the raw, uncalibrated
    heuristic when this is None, never guess a correction, the same
    convention metrics.vegas_week_multiplier's neutral-1.0 fallback
    already uses for a missing input."""
    row = conn.execute("SELECT platt_a, platt_b FROM calibration_params ORDER BY fitted_at DESC LIMIT 1").fetchone()
    return (row["platt_a"], row["platt_b"]) if row else None
