"""How much last season (or a rookie's projection) should count this
season, chosen by backtest rather than by feel.

The backtest: for every player, at each point w weeks into a season, blend
the prior with the first w weeks (metrics.blended_mean) and score that
against his real average over the rest of the season. The prior weight K
(in games) with the lowest mean absolute error across weeks 1-8 wins.
`dynasty-agent calibrate-blend` reruns it; the chosen constants below
record the result they came from.
"""

from __future__ import annotations

import math
import sqlite3

from dynasty_agent.metrics import blended_mean, blended_variance

# Chosen by backtest, run 2026-09-28 (`dynasty-agent calibrate-blend`):
# - veterans, 2024 as the prior for 2025, 1,231 players: lowest error at
#   K = 4 (MAE 0.883 PPG against 1.082 for this season alone and 0.947 for
#   K = 30); the curve is flat from 3 to 6.
# - rookies, the 2025 class (outside the model's 2018-2023 training range),
#   71 players: lowest error at K = 3 (MAE 3.032 against 3.454 alone).
# One season pair each, so treat these as a well-supported starting point,
# rerun each season as more pairs accumulate.
VETERAN_PRIOR_GAMES = 4.0
ROOKIE_PRIOR_GAMES = 3.0

CANDIDATE_K = (0, 1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 30)
WEEKS_INTO_SEASON = range(1, 9)
MIN_PRIOR_GAMES = 4
MIN_REST_GAMES = 4


def _weekly_points(conn: sqlite3.Connection, season: int) -> dict[str, list[tuple[int, float]]]:
    out: dict[str, list[tuple[int, float]]] = {}
    for r in conn.execute(
        "SELECT player_id, week, fantasy_points FROM weekly_stats WHERE season = ? AND fantasy_points IS NOT NULL ORDER BY week",
        (str(season),),
    ):
        out.setdefault(r[0], []).append((r[1], r[2]))
    return out


def _score(cases: list[tuple[float, list[tuple[int, float]]]]) -> dict[float, float]:
    """cases: (prior_mean, [(week, points)] for the current season).
    Returns {K: MAE} over every case and week cutoff with enough games on
    both sides."""
    errors: dict[float, list[float]] = {k: [] for k in CANDIDATE_K}
    for prior_mean, games in cases:
        for w in WEEKS_INTO_SEASON:
            before = [p for week, p in games if week <= w]
            after = [p for week, p in games if week > w]
            if len(after) < MIN_REST_GAMES:
                continue
            actual = sum(after) / len(after)
            for k in CANDIDATE_K:
                pred = blended_mean(prior_mean, k, before)
                if pred is not None:
                    errors[k].append(abs(pred - actual))
    return {k: sum(e) / len(e) for k, e in errors.items() if e}


def backtest_veterans(conn: sqlite3.Connection, prior_season: int, current_season: int) -> tuple[dict, int]:
    prior, current = _weekly_points(conn, prior_season), _weekly_points(conn, current_season)
    cases = [
        (sum(p for _, p in prior[pid]) / len(prior[pid]), games)
        for pid, games in current.items()
        if len(prior.get(pid, [])) >= MIN_PRIOR_GAMES
    ]
    return _score(cases), len(cases)


def backtest_rookies(conn: sqlite3.Connection, rookie_class: int) -> tuple[dict, int]:
    """Rookies of rookie_class, the draft-capital projection as the prior,
    their rookie season as the current season. A class outside the model's
    training range is a genuine out-of-sample test."""
    from dynasty_agent.prospect_model import load_model, predict

    model = load_model(conn, "baseline_draft_capital")
    if model is None:
        raise ValueError("No fitted prospect model yet. Run `dynasty-agent fit-prospect-model` first.")
    current = _weekly_points(conn, rookie_class)
    cases = []
    for r in conn.execute(
        """
        SELECT pi.sleeper_id, d.gsis_id, d.pick, d.position FROM nfl_draft_picks d
        JOIN player_ids pi ON pi.gsis_id = d.gsis_id
        WHERE d.season = ? AND d.position IN ('QB', 'RB', 'WR', 'TE')
        """,
        (rookie_class,),
    ):
        games = current.get(r["sleeper_id"]) or current.get(r["gsis_id"])
        if not games:
            continue
        feats = {"pos_RB": float(r["position"] == "RB"), "pos_WR": float(r["position"] == "WR"),
                 "pos_TE": float(r["position"] == "TE"), "log_pick": math.log(r["pick"])}
        cases.append((predict(model["weights"], [feats[n] for n in model["features"]]), games))
    return _score(cases), len(cases)


def weekly_values(conn: sqlite3.Connection, player_id: str, season: int) -> list[float]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT fantasy_points FROM weekly_stats WHERE player_id = ? AND season = ? AND fantasy_points IS NOT NULL",
            (player_id, str(season)),
        )
    ]


def player_distribution(conn: sqlite3.Connection, player_id: str, season: int, rookie: dict | None = None) -> dict:
    """One player's blended per-game mean and variance for `season`, with
    what went into them. The prior is last season's average (weight
    VETERAN_PRIOR_GAMES, or his actual game count if lower) or, for a
    rookie, the prospect model's projection (weight ROOKIE_PRIOR_GAMES).
    rookie, when given, is that player's prospect_model.rookie_projections
    entry. variance is None when there's no real sample to estimate one."""
    current = weekly_values(conn, player_id, season)
    if rookie is not None:
        mean = blended_mean(rookie["projected_ppg"], ROOKIE_PRIOR_GAMES, current)
        variance = blended_variance([], 0.0, current)
        return {"mean": mean, "variance": variance, "current_games": len(current), "prior_games": 0,
                "prior_weight": ROOKIE_PRIOR_GAMES, "source": "prospect_model"}
    prior = weekly_values(conn, player_id, season - 1)
    k = min(VETERAN_PRIOR_GAMES, float(len(prior)))
    prior_mean = sum(prior) / len(prior) if prior else None
    return {
        "mean": blended_mean(prior_mean, k, current),
        "variance": blended_variance(prior, k, current),
        "current_games": len(current),
        "prior_games": len(prior),
        "prior_weight": k,
        "source": "nfl_stats",
    }


def blended_situations(current: dict[str, dict], prior: dict[str, dict]) -> dict[str, dict]:
    """Team situation scores blended the same way as player averages: last
    season's score counts as VETERAN_PRIOR_GAMES games against this season's
    games played so far. A team missing from one side uses the other."""
    out: dict[str, dict] = {}
    for team in set(current) | set(prior):
        cur, pri = current.get(team), prior.get(team)
        if cur is None or pri is None:
            out[team] = dict(cur or pri)
            continue
        n = cur.get("games", 0)
        w = n / (n + VETERAN_PRIOR_GAMES)
        out[team] = {**cur, "situation_score": w * cur["situation_score"] + (1 - w) * pri["situation_score"]}
    return out
