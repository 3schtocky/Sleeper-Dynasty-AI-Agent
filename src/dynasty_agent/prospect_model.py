"""Phase 4 prospect model: projected fantasy points per game over a
player's first three NFL seasons, in this league's own scoring, fitted
against real outcomes rather than hand-set weights (by explicit decision).

Training set, by explicit decision: drafted QB/RB/WR/TE from the 2018
through 2023 classes whose college stats link to their NFL ids exactly.
ESPN keeps one athlete id from college into the NFL only from about the
2018 class on (checked live: 0 of ~80 linked per class 2012-2015, 25 of 83
in 2017, 60 of 83 in 2018, 76-85 from 2019); older classes would need name
matching, rejected in favor of exact links. 2023 is the last class with
three completed NFL seasons.

Target: total fantasy points over the first three regular seasons divided
by the games scheduled in them (16 a season through 2020, 17 after), not
games played. Points per game played would let a bust with 3 games at 12
points look like a hit; per game scheduled, missed time counts as 0, which
is what a roster slot actually got.

Two variants, both fitted on the same rows:
- post_draft: draft capital (log of the overall pick), draft age, college
  production, athleticism, position.
- pre_draft: the same without draft capital, for a class not yet drafted.

A small ridge penalty keeps ~470 rows and a dozen features stable. Every
fit is scored by leaving one draft class out at a time and predicting it
from the others, and compared against a draft-capital-only baseline, so
the report says plainly whether college production adds anything.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date

import duckdb

from dynasty_agent import nflverse
from dynasty_agent.college import is_power_conference
from dynasty_agent.db import utcnow
from dynasty_agent.metrics import (
    age_on,
    athletic_score,
    breakout_age,
    compute_fantasy_points,
    dominator_rating,
    production_score,
    speed_score,
)

TRAINING_CLASSES = range(2018, 2024)
POSITIONS = ("QB", "RB", "WR", "TE")
RIDGE_LAMBDA = 1.0
# The NFL draft runs in late April; draft age is taken on April 25.
DRAFT_MONTH_DAY = (4, 25)
# Centering points so an "unknown" feature can sit at 0 alongside its flag.
BREAKOUT_AGE_CENTER = 19.5
ATHLETIC_CENTER = 50.0


def games_scheduled(season: int) -> int:
    return 16 if season <= 2020 else 17


# -- features -----------------------------------------------------------------


# A college season counts toward dominator and breakout only with a real
# sample behind it. ESPN's box scores carry an FCS team only in its games
# against FBS opponents, often 1 or 2 a year, and one game where a small
# school's WR caught 85% of the yards swamped the first pre-draft board
# (every top-15 row was a 1-4 game sample, found on the live run). Both
# thresholds are round, stated numbers, not fitted.
MIN_TEAM_GAMES = 6
MIN_PLAYER_GAMES = 4


def college_profile(conn: sqlite3.Connection, espn_id: str, before_season: int) -> dict | None:
    """Peak dominator and every season's dominator, from each college season
    before the draft with a real sample (MIN_TEAM_GAMES, MIN_PLAYER_GAMES).
    None when the player has no qualifying college season at all."""
    rows = conn.execute(
        """
        SELECT p.season, p.team_id, p.rec_yds, p.rec_td, p.team_rec_yds, p.team_rec_td,
               t.conference, t.classification, t.net_z
        FROM cfb_player_season p
        LEFT JOIN cfb_team_season t ON t.team_id = p.team_id AND t.season = p.season
        WHERE p.athlete_id = ? AND p.season < ? AND p.team_games >= ? AND p.games >= ?
        """,
        (espn_id, before_season, MIN_TEAM_GAMES, MIN_PLAYER_GAMES),
    ).fetchall()
    if not rows:
        return None
    by_season: dict[int, float | None] = {}
    best_row_by_season: dict[int, sqlite3.Row] = {}
    for r in rows:
        dom = dominator_rating(r["rec_yds"], r["rec_td"], r["team_rec_yds"], r["team_rec_td"])
        prior = by_season.get(r["season"])
        # A mid-season transfer is two team rows; keep the better share.
        if prior is None or (dom or 0.0) > prior:
            by_season[r["season"]] = dom
            best_row_by_season[r["season"]] = r
    doms = {s: d for s, d in by_season.items() if d is not None}
    peak_season = max(doms, key=doms.get) if doms else max(by_season)
    last_season = max(by_season)
    return {
        "seasons": sorted(by_season.items()),
        "peak_dominator": doms.get(peak_season, 0.0) if doms else 0.0,
        "peak_team": _team_context(best_row_by_season[peak_season]),
        "last_team": _team_context(best_row_by_season[last_season]),
    }


def _team_context(row: sqlite3.Row) -> dict:
    return {
        "power_conf": is_power_conference(row["conference"], row["season"], row["team_id"]),
        "fbs": row["classification"] == "fbs",
        "net_z": row["net_z"],
        "conference": row["conference"],
    }


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def combine_measurements(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Every combine row keyed for athletic_score: position -> list of
    measurement dicts (pfr_id included), the per-position populations."""
    by_position: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT pfr_id, position, weight_lb, forty, vertical, broad_jump, cone, shuttle FROM nfl_combine"
    ):
        m = {
            "pfr_id": r["pfr_id"],
            "speed_score": speed_score(r["weight_lb"], r["forty"]),
            "vertical": r["vertical"], "broad_jump": r["broad_jump"],
            "cone": r["cone"], "shuttle": r["shuttle"], "weight_lb": r["weight_lb"],
        }
        by_position.setdefault(r["position"], []).append(m)
    return by_position


def feature_row(
    position: str, overall_pick: int | None, draft_age: float, college: dict,
    birthdate: date | None, athletic: float | None,
) -> dict:
    """The model's inputs for one player, every one visible by name.
    Receiving production is zeroed for QBs, whose receiving share is
    meaningless, and each partially-known input carries its own flag so
    "unknown" and "bad" never look alike to the fit."""
    is_qb = position == "QB"
    status, b_age = breakout_age(college["seasons"], birthdate)
    # Team context from the season that defines the player: his peak
    # receiving season, or his last season for a QB.
    team = college["last_team"] if is_qb else college["peak_team"]
    return {
        # Every drafted player in the 2018-2023 training rows came from a
        # rated FBS team, so an unrated (FCS) team is outside what the model
        # has seen; pre_draft_board leaves those players off rather than
        # guess. None here marks it.
        "power_conf": float(team["power_conf"]),
        "team_strength": team["net_z"],
        "pos_RB": float(position == "RB"),
        "pos_WR": float(position == "WR"),
        "pos_TE": float(position == "TE"),
        "log_pick": math.log(overall_pick) if overall_pick else None,
        "draft_age": draft_age,
        "peak_dominator": 0.0 if is_qb else college["peak_dominator"],
        "broke_out": float(not is_qb and status == "broke_out"),
        "never_broke_out": float(not is_qb and status == "never"),
        "breakout_age": (b_age - BREAKOUT_AGE_CENTER) if (not is_qb and b_age is not None) else 0.0,
        "athletic_known": float(athletic is not None),
        "athletic": (athletic - ATHLETIC_CENTER) if athletic is not None else 0.0,
    }


POST_DRAFT_FEATURES = (
    "pos_RB", "pos_WR", "pos_TE", "log_pick", "draft_age", "peak_dominator",
    "broke_out", "never_broke_out", "breakout_age", "athletic_known", "athletic",
    "power_conf", "team_strength",
)
PRE_DRAFT_FEATURES = tuple(f for f in POST_DRAFT_FEATURES if f != "log_pick")
# Checked live: only 71 of 1,911 draft-eligible 2025 college skill players
# have a real birth date on record, and age is never estimated (explicit
# decision), so pre-draft ranking needs a variant that doesn't use it.
PRE_DRAFT_NO_AGE_FEATURES = tuple(f for f in PRE_DRAFT_FEATURES if f != "draft_age")
BASELINE_FEATURES = ("pos_RB", "pos_WR", "pos_TE", "log_pick")

VARIANTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("post_draft", POST_DRAFT_FEATURES),
    ("baseline_draft_capital", BASELINE_FEATURES),
    ("pre_draft", PRE_DRAFT_FEATURES),
    ("pre_draft_no_age", PRE_DRAFT_NO_AGE_FEATURES),
)


# -- outcomes -----------------------------------------------------------------


def first_three_season_ppg(scoring_settings: dict, seasons: range) -> dict[tuple[str, int], float]:
    """{(gsis_id, draft_class): points per game scheduled over that class's
    first three regular seasons}, from nflverse's stats_player_week files,
    scored with this league's own scoring_settings."""
    totals: dict[tuple[str, int], float] = {}
    con = duckdb.connect()
    try:
        for season in seasons:
            path = nflverse.ensure_cached("stats_player_week", season)
            cur = con.execute(
                """
                SELECT player_id, passing_yards, passing_tds, passing_interceptions, passing_2pt_conversions,
                       rushing_yards, rushing_tds, rushing_2pt_conversions, receptions, receiving_yards,
                       receiving_tds, receiving_2pt_conversions,
                       coalesce(sack_fumbles_lost, 0) + coalesce(rushing_fumbles_lost, 0)
                         + coalesce(receiving_fumbles_lost, 0) AS fumbles_lost
                FROM read_parquet(?)
                WHERE season_type = 'REG' AND player_id IS NOT NULL
                """,
                [str(path)],
            )
            columns = [d[0] for d in cur.description]
            for values in cur.fetchall():
                record = dict(zip(columns, values))
                pts = compute_fantasy_points(record, scoring_settings)
                for draft_class in range(season - 2, season + 1):
                    key = (record["player_id"], draft_class)
                    totals[key] = totals.get(key, 0.0) + pts
    finally:
        con.close()
    return totals


def training_rows(conn: sqlite3.Connection, scoring_settings: dict) -> tuple[list[dict], dict]:
    """(rows, coverage). Each row: identity, features, and target. coverage
    counts every drafted skill player and why any was left out."""
    first_season, last_season = TRAINING_CLASSES.start, TRAINING_CLASSES.stop - 1 + 2
    points = first_three_season_ppg(scoring_settings, range(first_season, last_season + 1))
    combine = combine_measurements(conn)

    picks = conn.execute(
        f"""
        SELECT d.season, d.pick, d.position, d.player_name, d.gsis_id, d.pfr_player_id,
               pi.espn_id, pi.birthdate
        FROM nfl_draft_picks d
        LEFT JOIN player_ids pi ON pi.gsis_id = d.gsis_id
        WHERE d.season BETWEEN ? AND ? AND d.position IN ({",".join("?" * len(POSITIONS))})
        """,
        (TRAINING_CLASSES.start, TRAINING_CLASSES.stop - 1, *POSITIONS),
    ).fetchall()

    coverage = {"drafted": len(picks), "no_college_link": 0, "no_birthdate": 0, "used": 0}
    rows = []
    for p in picks:
        college = college_profile(conn, p["espn_id"], p["season"]) if p["espn_id"] else None
        if college is None:
            coverage["no_college_link"] += 1
            continue
        birthdate = _parse_date(p["birthdate"])
        draft_age = age_on(birthdate, date(p["season"], *DRAFT_MONTH_DAY))
        if draft_age is None:
            coverage["no_birthdate"] += 1
            continue
        population = combine.get(p["position"], [])
        mine = next((m for m in population if p["pfr_player_id"] and m["pfr_id"] == p["pfr_player_id"]), None)
        athletic = athletic_score(mine, population)[0] if mine else None
        n_games = sum(games_scheduled(s) for s in range(p["season"], p["season"] + 3))
        target = points.get((p["gsis_id"], p["season"]), 0.0) / n_games
        rows.append({
            "draft_class": p["season"], "pick": p["pick"], "position": p["position"],
            "name": p["player_name"], "gsis_id": p["gsis_id"],
            "features": feature_row(p["position"], p["pick"], draft_age, college, birthdate, athletic),
            "target": target,
        })
        coverage["used"] += 1
    return rows, coverage


# -- fitting --------------------------------------------------------------------


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting, a small dense system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        if abs(m[col][col]) < 1e-12:
            raise ValueError("singular system: a feature has no variation in the training rows")
        for r in range(col + 1, n):
            factor = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= factor * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def fit_ridge(x: list[list[float]], y: list[float], ridge_lambda: float = RIDGE_LAMBDA) -> list[float]:
    """Ridge regression, intercept first and unpenalized. Features are
    standardized for the penalty and the coefficients mapped back, so the
    returned weights apply to the raw feature values."""
    n, k = len(x), len(x[0])
    means = [sum(row[j] for row in x) / n for j in range(k)]
    sds = [math.sqrt(sum((row[j] - means[j]) ** 2 for row in x) / n) or 1.0 for j in range(k)]
    z = [[(row[j] - means[j]) / sds[j] for j in range(k)] for row in x]
    y_mean = sum(y) / n
    xtx = [[sum(z[i][a] * z[i][b] for i in range(n)) + (ridge_lambda if a == b else 0.0) for b in range(k)] for a in range(k)]
    xty = [sum(z[i][a] * (y[i] - y_mean) for i in range(n)) for a in range(k)]
    beta_z = _solve(xtx, xty)
    beta = [beta_z[j] / sds[j] for j in range(k)]
    intercept = y_mean - sum(beta[j] * means[j] for j in range(k))
    return [intercept] + beta


def predict(weights: list[float], features: list[float]) -> float:
    return weights[0] + sum(w * v for w, v in zip(weights[1:], features))


def _matrix(rows: list[dict], names: tuple[str, ...]) -> list[list[float]]:
    return [[r["features"][n] for n in names] for r in rows]


def leave_one_class_out(rows: list[dict], names: tuple[str, ...]) -> dict:
    """Predict each draft class from a fit on the others. Returns MAE and
    R^2 over those out-of-sample predictions."""
    errors, targets, preds = [], [], []
    for held in sorted({r["draft_class"] for r in rows}):
        train = [r for r in rows if r["draft_class"] != held]
        test = [r for r in rows if r["draft_class"] == held]
        w = fit_ridge(_matrix(train, names), [r["target"] for r in train])
        for r, feats in zip(test, _matrix(test, names)):
            p = predict(w, feats)
            preds.append(p)
            targets.append(r["target"])
            errors.append(abs(p - r["target"]))
    mean_t = sum(targets) / len(targets)
    ss_tot = sum((t - mean_t) ** 2 for t in targets)
    ss_res = sum((t - p) ** 2 for t, p in zip(targets, preds))
    return {"mae": sum(errors) / len(errors), "r2": 1 - ss_res / ss_tot if ss_tot else 0.0, "n": len(targets)}


def fit_and_store(conn: sqlite3.Connection, scoring_settings: dict) -> dict:
    """Fit both variants plus the baseline, cross-validate all three, and
    store the fitted weights with their metadata in prospect_model.
    Returns the full report."""
    rows, coverage = training_rows(conn, scoring_settings)
    report = {"coverage": coverage, "classes": [TRAINING_CLASSES.start, TRAINING_CLASSES.stop - 1], "variants": {}}
    for variant, names in VARIANTS:
        rows_v = [r for r in rows if all(r["features"][n] is not None for n in names)]
        weights = fit_ridge(_matrix(rows_v, names), [r["target"] for r in rows_v])
        cv = leave_one_class_out(rows_v, names)
        report["variants"][variant] = {"features": list(names), "weights": weights, "cv": cv, "n": len(rows_v)}
        conn.execute(
            """
            INSERT INTO prospect_model (variant, features_json, weights_json, n_rows, cv_mae, cv_r2,
                                        training_classes, ridge_lambda, fitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (variant) DO UPDATE SET
                features_json = excluded.features_json, weights_json = excluded.weights_json,
                n_rows = excluded.n_rows, cv_mae = excluded.cv_mae, cv_r2 = excluded.cv_r2,
                training_classes = excluded.training_classes, ridge_lambda = excluded.ridge_lambda,
                fitted_at = excluded.fitted_at
            """,
            (variant, json.dumps(list(names)), json.dumps(weights), len(rows_v), cv["mae"], cv["r2"],
             f"{TRAINING_CLASSES.start}-{TRAINING_CLASSES.stop - 1}", RIDGE_LAMBDA, utcnow()),
        )
    conn.commit()
    return report


def load_model(conn: sqlite3.Connection, variant: str) -> dict | None:
    row = conn.execute("SELECT * FROM prospect_model WHERE variant = ?", (variant,)).fetchone()
    if row is None:
        return None
    return {**dict(row), "features": json.loads(row["features_json"]), "weights": json.loads(row["weights_json"])}


# -- the prospect board -------------------------------------------------------------
#
# Post-draft ranks on the draft-capital baseline, not the full post-draft
# fit: checked on 2018-2023, adding college production, age, and
# athleticism to draft capital did not improve out-of-sample error overall
# (MAE 2.65 both) or within any position reliably (it got slightly worse for
# RB, WR, and QB). College inputs are still shown on each row, as context.
#
# Pre-draft ranks on college production alone, a weak signal, labeled weak
# on every run: its job is a first read on a class before the NFL draft
# order exists, not a confident ranking.

# An undrafted rookie is priced as if taken one pick after the last real
# pick. The model never saw an undrafted player in training, so this is an
# extrapolation, and every such row says so.
UNDRAFTED_PICK = 260


def _rank_by_league_value(rows: list[dict]) -> None:
    """Sort by projected PPG after this league's position weighting
    (metrics.production_score: QB x0.70 for 1QB, WR x1.05), the same
    adjustment valuate and trade apply, so a QB isn't ranked like a
    superflex asset. Both numbers stay on the row."""
    for r in rows:
        r["league_ppg"] = production_score(r["projected_ppg"], r["position"])
    rows.sort(key=lambda r: -r["league_ppg"])


def _require_model(conn: sqlite3.Connection, variant: str) -> dict:
    model = load_model(conn, variant)
    if model is None:
        raise ValueError("No fitted prospect model yet. Run `dynasty-agent fit-prospect-model` first.")
    return model


def _project(model: dict, features: dict) -> float:
    return predict(model["weights"], [features[n] for n in model["features"]])


def _athletic_for(combine: dict[str, list[dict]], position: str, pfr_id: str | None) -> float | None:
    population = combine.get(position, [])
    mine = next((m for m in population if pfr_id and m["pfr_id"] == pfr_id), None)
    return athletic_score(mine, population)[0] if mine else None


def post_draft_board(conn: sqlite3.Connection, draft_class: int) -> dict:
    """Every QB/RB/WR/TE actually drafted in draft_class, ranked by the
    draft-capital model's projected PPG, with college, age, athleticism,
    landing spot, and FantasyCalc's current value shown as inputs."""
    from dynasty_agent import market  # local: market imports nothing from here, kept lazy for startup cost

    model = _require_model(conn, "baseline_draft_capital")
    combine = combine_measurements(conn)
    picks = conn.execute(
        f"""
        SELECT d.season, d.pick, d.round, d.team, d.position, d.player_name, d.pfr_player_id,
               pi.espn_id, pi.sleeper_id, pi.birthdate
        FROM nfl_draft_picks d
        LEFT JOIN player_ids pi ON pi.gsis_id = d.gsis_id
        WHERE d.season = ? AND d.position IN ({",".join("?" * len(POSITIONS))})
        """,
        (draft_class, *POSITIONS),
    ).fetchall()
    rows = []
    for p in picks:
        birthdate = _parse_date(p["birthdate"])
        college = college_profile(conn, p["espn_id"], p["season"]) if p["espn_id"] else None
        status, b_age = breakout_age(college["seasons"], birthdate) if college else ("no college data", None)
        features = {
            "pos_RB": float(p["position"] == "RB"), "pos_WR": float(p["position"] == "WR"),
            "pos_TE": float(p["position"] == "TE"), "log_pick": math.log(p["pick"]),
        }
        rows.append({
            "name": p["player_name"], "position": p["position"], "nfl_team": p["team"],
            "round": p["round"], "pick": p["pick"],
            "draft_age": age_on(birthdate, date(draft_class, *DRAFT_MONTH_DAY)),
            "peak_dominator": college["peak_dominator"] if college else None,
            "breakout": status, "breakout_age": b_age,
            "athletic": _athletic_for(combine, p["position"], p["pfr_player_id"]),
            "sleeper_id": p["sleeper_id"],
            "market_value": market.latest_value(conn, p["sleeper_id"]) if p["sleeper_id"] else None,
            "projected_ppg": _project(model, features),
        })
    _rank_by_league_value(rows)
    return {"mode": "post_draft", "draft_class": draft_class, "model": model, "rows": rows}


def pre_draft_board(conn: sqlite3.Connection, draft_class: int, min_college_seasons: int = 3) -> dict:
    """College QB/RB/WR/TE who played in the season before draft_class, have
    at least min_college_seasons seasons on record (a stand-in for draft
    eligibility; actual declarations are unknown until January), and are
    not already drafted. Ranked by the pre-draft model: with age where a
    real birth date exists, without it otherwise, each row saying which."""
    with_age = _require_model(conn, "pre_draft")
    no_age = _require_model(conn, "pre_draft_no_age")
    last_season = draft_class - 1
    candidates = conn.execute(
        """
        SELECT s.athlete_id, max(s.athlete_name) AS name, max(s.position) AS position,
               (SELECT count(DISTINCT season) FROM cfb_player_season s2 WHERE s2.athlete_id = s.athlete_id) AS seasons,
               max(s.games) AS games_this_season, a.date_of_birth
        FROM cfb_player_season s
        LEFT JOIN cfb_athletes a ON a.athlete_id = s.athlete_id
        WHERE s.season = ? AND s.position IN ('QB', 'RB', 'WR', 'TE')
          AND s.athlete_id NOT IN (SELECT espn_id FROM player_ids WHERE draft_year IS NOT NULL AND espn_id IS NOT NULL)
        GROUP BY s.athlete_id
        HAVING seasons >= ?
        """,
        (last_season, min_college_seasons),
    ).fetchall()
    rows = []
    skipped_thin = skipped_unrated = 0
    for c in candidates:
        college = college_profile(conn, c["athlete_id"], draft_class)
        if college is None:
            skipped_thin += 1  # no season with a real sample (MIN_TEAM_GAMES / MIN_PLAYER_GAMES)
            continue
        birthdate = _parse_date(c["date_of_birth"])
        draft_age = age_on(birthdate, date(draft_class, *DRAFT_MONTH_DAY))
        features = feature_row(c["position"], None, draft_age or 0.0, college, birthdate, None)
        if features["team_strength"] is None:
            skipped_unrated += 1  # FCS or unrated team: outside the model's training range
            continue
        model = with_age if draft_age is not None else no_age
        rows.append({
            "name": c["name"], "position": c["position"], "college_seasons": c["seasons"],
            "games_last_season": c["games_this_season"], "draft_age": draft_age,
            "peak_dominator": college["peak_dominator"],
            "breakout": breakout_age(college["seasons"], birthdate)[0],
            "model_used": "pre_draft" if draft_age is not None else "pre_draft_no_age",
            "conference": (college["last_team"] if c["position"] == "QB" else college["peak_team"])["conference"],
            "team_strength": features["team_strength"],
            "projected_ppg": _project(model, features),
        })
    _rank_by_league_value(rows)
    return {"mode": "pre_draft", "draft_class": draft_class, "models": {"pre_draft": with_age, "pre_draft_no_age": no_age},
            "last_college_season": last_season, "skipped_thin_sample": skipped_thin,
            "skipped_unrated_team": skipped_unrated, "rows": rows}


# -- rookie values inside the existing valuation ---------------------------------------

# A player's value starts from the prospect model's projection instead of
# last season's stats when he has at most ROOKIE_MAX_YEARS_EXP years of
# experience and fewer than ROOKIE_MAX_PRIOR_GAMES games last season, too
# few to be a real prior. This season's games then blend in on top of the
# projection (blend.ROOKIE_PRIOR_GAMES), so there is no cliff where a rookie
# jumps from a projection to a 4-game average. Before any of this, every
# rookie was valued at exactly 0.
ROOKIE_MAX_PRIOR_GAMES = 4
ROOKIE_MAX_YEARS_EXP = 1


def rookie_projections(conn: sqlite3.Connection, season: int, player_ids: list[str] | None = None) -> dict[str, dict]:
    """{sleeper_id: projection} for every QB/RB/WR/TE on an NFL team who
    qualifies as a rookie for `season` (see ROOKIE_MAX_* above), optionally
    limited to player_ids, from the draft-capital model (the one that held
    up out of sample). Empty when no model is fitted yet.

    projected_ppg is points per game SCHEDULED over the first 3 NFL
    seasons, the model's own target, so it runs a little conservative next
    to a veteran's per-game-played average, and it's a 3-year average
    applied to a single year. Both are stated wherever it's shown."""
    model = load_model(conn, "baseline_draft_capital")
    if model is None:
        return {}
    only = ""
    params: list = [ROOKIE_MAX_YEARS_EXP, str(season - 1), ROOKIE_MAX_PRIOR_GAMES]
    if player_ids is not None:
        if not player_ids:
            return {}
        only = f" AND p.player_id IN ({','.join('?' * len(player_ids))})"
        params += list(player_ids)
    rows = conn.execute(
        f"""
        SELECT p.player_id, p.position, d.pick
        FROM players p
        LEFT JOIN player_ids pi ON pi.sleeper_id = p.player_id
        LEFT JOIN nfl_draft_picks d ON d.gsis_id = pi.gsis_id AND pi.gsis_id IS NOT NULL
        WHERE p.position IN ('QB', 'RB', 'WR', 'TE') AND p.team IS NOT NULL
          AND coalesce(p.years_exp, 0) <= ?
          AND (SELECT count(*) FROM weekly_stats w WHERE w.player_id = p.player_id AND w.season = ?) < ?{only}
        """,
        params,
    ).fetchall()
    result: dict[str, dict] = {}
    for r in rows:
        if r["player_id"] in result:
            continue  # a player can match two crosswalk rows; the first real pick wins
        pick = r["pick"] or UNDRAFTED_PICK
        features = {
            "pos_RB": float(r["position"] == "RB"), "pos_WR": float(r["position"] == "WR"),
            "pos_TE": float(r["position"] == "TE"), "log_pick": math.log(pick),
        }
        result[r["player_id"]] = {
            "projected_ppg": _project(model, features),
            "draft_pick": r["pick"],
            "undrafted": r["pick"] is None,
        }
    return result
