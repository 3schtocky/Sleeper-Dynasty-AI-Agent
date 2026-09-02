"""Pure metric calculations, kept separate from data plumbing so they are
easy to unit test in isolation. If weighted opportunity or the point formula
is wrong here, every derived table and every later valuation is wrong too,
and nothing downstream will look obviously broken.
"""

from __future__ import annotations

import math
from datetime import date


def compute_fantasy_points(stat_line: dict, scoring_settings: dict) -> float:
    """Fantasy points for one player-week, using this league's own
    scoring_settings dict (as pulled from the Sleeper API), never a generic
    PPR assumption. Missing stats and missing scoring weights both default
    to 0, so a partial scoring_settings, this league has no K or DST
    weights and does not need them, never raises.

    stat_line accepts either a single "fumbles_lost" total or the three
    nflverse sub-fields (sack_fumbles_lost, rushing_fumbles_lost,
    receiving_fumbles_lost); it sums the sub-fields only when no total is
    given directly.
    """

    def w(key: str) -> float:
        return scoring_settings.get(key) or 0.0

    def s(key: str) -> float:
        return stat_line.get(key) or 0.0

    if stat_line.get("fumbles_lost") is not None:
        fumbles_lost = s("fumbles_lost")
    else:
        fumbles_lost = s("sack_fumbles_lost") + s("rushing_fumbles_lost") + s("receiving_fumbles_lost")

    return (
        s("passing_yards") * w("pass_yd")
        + s("passing_tds") * w("pass_td")
        + s("passing_interceptions") * w("pass_int")
        + s("passing_2pt_conversions") * w("pass_2pt")
        + s("rushing_yards") * w("rush_yd")
        + s("rushing_tds") * w("rush_td")
        + s("rushing_2pt_conversions") * w("rush_2pt")
        + s("receptions") * w("rec")
        + s("receiving_yards") * w("rec_yd")
        + s("receiving_tds") * w("rec_td")
        + s("receiving_2pt_conversions") * w("rec_2pt")
        + fumbles_lost * w("fum_lost")
    )


def weighted_opportunity(carries: float | None, targets: float | None) -> float:
    """Carries plus twice targets. The single best public proxy for a
    player's raw opportunity, full PPR weighs a target roughly like two
    carries."""
    return (carries or 0) + 2 * (targets or 0)


def yards_per_route_run_estimate(receiving_yards: float | None, offensive_snaps: float | None) -> float | None:
    """Estimate only. True routes run is not in any public nflverse file,
    it is paywalled at PFF and Fantasy Points, so offensive snaps stands in
    for the denominator here. Returns None when there is no snap count to
    divide by, rather than a division by zero or a silent 0.0."""
    if not offensive_snaps:
        return None
    return (receiving_yards or 0) / offensive_snaps


# -- Phase 2: dynasty age curve ----------------------------------------------

# (peak_end_age, decay_rate) per position: value holds flat at 1.0 through
# peak_end_age, then decays as exp(-decay_rate * years_past_peak). Anchored
# to CLAUDE.md's strategic implications: RBs decline sharply at 26-27 (short
# peak, steep decay_rate), WRs peak 24-28 and fade near 30 (longer peak,
# gentle decay), TEs peak 26-30 (same gentle decay as WR, no distinguishing
# language in the spec), QBs hold value longest (latest peak_end, gentlest
# decay). A position missing from this table (there should not be one in a
# league with no K/DST slots) gets a neutral middle-of-the-road curve rather
# than raising.
AGE_CURVES: dict[str, tuple[int, float]] = {
    "RB": (25, 0.32),
    "WR": (28, 0.15),
    "TE": (30, 0.15),
    "QB": (32, 0.08),
}
_DEFAULT_AGE_CURVE = (27, 0.18)


def age_multiplier(position: str | None, age: float | None) -> float:
    """1.0 through a position's peak, then a smooth exponential decay past
    it. Unknown age returns 1.0 (neutral) rather than guessing."""
    if age is None:
        return 1.0
    peak_end, decay_rate = AGE_CURVES.get(position or "", _DEFAULT_AGE_CURVE)
    if age <= peak_end:
        return 1.0
    return math.exp(-decay_rate * (age - peak_end))


def three_year_age_factor(position: str | None, age: float | None) -> float:
    """Average age multiplier over this year and the next two, so a
    three-year value reflects the decline trajectory, not a single-year
    snapshot."""
    if age is None:
        return 1.0
    multipliers = [age_multiplier(position, age + years_ahead) for years_ahead in range(3)]
    return sum(multipliers) / len(multipliers)


# -- Phase 2: situation score -------------------------------------------------

def percentile_rank(value: float | None, population: list[float | None]) -> float:
    """Where value ranks in population, 0 to 100. Ties split the difference
    (a value tied with everyone else lands at 50). An empty population, or a
    None value, returns 50.0, a neutral midpoint, rather than raising or
    silently returning 0."""
    clean_population = [p for p in population if p is not None]
    if value is None or not clean_population:
        return 50.0
    below = sum(1 for p in clean_population if p < value)
    equal = sum(1 for p in clean_population if p == value)
    return 100.0 * (below + 0.5 * equal) / len(clean_population)


def situation_multiplier(situation_score_0_100: float) -> float:
    """Maps a 0-100 situation score to a bounded 0.85-1.15 multiplier, so a
    team's context nudges value without ever dominating production. A
    league-average situation (score 50) is a no-op multiplier of 1.0."""
    score = max(0.0, min(100.0, situation_score_0_100))
    return 0.85 + 0.30 * (score / 100.0)


# -- Phase 2: production score and value ------------------------------------

# QB production is discounted for the 1QB format, twelve starting
# quarterbacks makes the position nearly replaceable. WR gets a modest bump,
# three starting receivers plus a flex in full PPR makes the position the
# league's highest-signal one. RB and TE stay neutral, their point totals
# already reflect PPR's RB bump and TE's lack of a scoring premium.
POSITION_PRODUCTION_MULTIPLIER: dict[str, float] = {"QB": 0.70, "RB": 1.00, "WR": 1.05, "TE": 1.00}


def production_score(fantasy_points_per_game: float | None, position: str | None) -> float:
    weight = POSITION_PRODUCTION_MULTIPLIER.get(position or "", 1.0)
    return (fantasy_points_per_game or 0.0) * weight


def win_now_value(production: float, position: str | None, age: float | None, situation_score_0_100: float) -> float:
    return production * age_multiplier(position, age) * situation_multiplier(situation_score_0_100)


def three_year_value(production: float, position: str | None, age: float | None, situation_score_0_100: float) -> float:
    return production * three_year_age_factor(position, age) * situation_multiplier(situation_score_0_100)


# -- Phase 2: depth chart snapshot to week mapping ---------------------------

def discounted_pick_value(base_value: float, years_from_base: int, discount_rate: float) -> float:
    """A future pick's value discounted by a tunable per-year rate from a
    base (nearest market-priced) draft. Floors years_from_base at 0, a pick
    at or before the base season gets no discount, never a bonus."""
    years = max(0, years_from_base)
    return base_value * (1 - discount_rate) ** years


def map_snapshot_to_week(snapshot_date: date, week_starts: list[tuple[int, date]]) -> int | None:
    """Given a depth chart snapshot date and a list of (week, week_start_date)
    pairs, return the earliest week whose games start on or after the
    snapshot, the upcoming week this snapshot is informing. None if the
    snapshot postdates every known week, for example a post-season roster
    cut snapshot with no more games left to precede."""
    candidate_weeks = [week for week, start in week_starts if start >= snapshot_date]
    return min(candidate_weeks) if candidate_weeks else None


# -- ad hoc matchup win probability -------------------------------------------
#
# This is a heuristic, not a fitted statistical model. Nothing here was
# calibrated against real game outcomes, there is no historical win/loss
# dataset in this project to fit against, and it makes a real, stated
# simplifying assumption: that players' weekly scores are independent, when
# in practice teammates correlate somewhat (a QB and his own WR1, for
# example). What IS real: every mean and variance that feeds it comes from
# a player's actual weekly fantasy_points in a completed nflverse season,
# not a guess, and the win-probability step is the same normal-CDF-of-a-
# point-differential idea Vegas spread-to-moneyline conversion and most NFL
# win-probability models use, just simplified to two independent team
# totals instead of a play-by-play model.

# Rough, round, and labeled as such: how much of a player's normal expected
# production to still count given their current injury designation. Not
# fitted from outcome data, there's no such dataset here, these are
# deliberately conservative, common-sense fractions, easy to override.
INJURY_MEAN_MULTIPLIER: dict[str, float] = {
    "Out": 0.0,
    "IR": 0.0,
    "Suspended": 0.0,
    "Doubtful": 0.25,
    "Questionable": 0.85,
}


def injury_adjusted_mean(raw_mean: float, injury_status: str | None) -> float:
    return raw_mean * INJURY_MEAN_MULTIPLIER.get(injury_status or "", 1.0)


# An injury designation doesn't just lower expected production, it changes
# the SHAPE of the outcome. Out/IR/Suspended collapses toward near-certain
# zero: both mean and spread shrink. Questionable/Doubtful is closer to
# bimodal, plays close to a full workload or gets scratched/limited, than a
# healthy player's normal week-to-week swing, so variance widens instead of
# narrowing. Scaling only the mean (as an earlier version of this module
# did) and leaving variance untouched understates exactly the uncertainty
# an injury tag is supposed to signal. Same caveat as INJURY_MEAN_MULTIPLIER:
# round, labeled numbers, not fitted from outcome data.
INJURY_VARIANCE_MULTIPLIER: dict[str, float] = {
    "Out": 0.1,
    "IR": 0.1,
    "Suspended": 0.1,
    "Doubtful": 3.0,
    "Questionable": 2.0,
}


def injury_adjusted_variance(raw_variance: float, injury_status: str | None) -> float:
    return raw_variance * INJURY_VARIANCE_MULTIPLIER.get(injury_status or "", 1.0)


def sample_mean_variance(values: list[float]) -> tuple[float, float | None, int]:
    """Mean and unbiased SAMPLE variance (Bessel's correction: divide by
    n - 1, not n) of a list of real observations, plus the count.

    An earlier version of this divided by n, the population-variance
    formula, which is wrong here: a season's games are a sample used to
    estimate a player's true week-to-week variance, not the entire
    population of possible outcomes, and dividing by n systematically
    understates that variance. The understatement is worst exactly where it
    matters most, players with few games, which is also where this feeds a
    win-probability model that then reads as more confident than the data
    supports.

    A sample variance is undefined from fewer than two points: variance is
    None for n < 2 rather than silently returning 0.0, which would read as
    real certainty instead of missing data. An empty list returns
    (0.0, None, 0).
    """
    n = len(values)
    if n == 0:
        return 0.0, None, 0
    mean = sum(values) / n
    if n == 1:
        return mean, None, 1
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return mean, variance, n


def normal_cdf(z: float) -> float:
    """Standard normal cumulative distribution function, via math.erf so
    this has no dependency on scipy or numpy."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def vegas_week_multiplier(team_implied_this_week: float | None, team_season_avg_implied: float | None) -> float:
    """How much better or worse Vegas expects a team to score in one
    specific week versus that same team's own season norm, derived from
    real spread_line/total_line data (see matchup.team_week_implied_points),
    never a hardcoded per-team bias. Returns 1.0, a no-op, when either
    number is missing: no line published yet for this week, or no prior
    weeks this season to establish a baseline (week 1). Never guesses a
    direction when the inputs don't support one."""
    if not team_implied_this_week or not team_season_avg_implied:
        return 1.0
    return team_implied_this_week / team_season_avg_implied


def matchup_win_probability(mean_diff: float, std_diff: float) -> float:
    """P(team A's score exceeds team B's), given the mean and standard
    deviation of the (team A - team B) differential, modeled as normal.
    std_diff of 0 is a degenerate case (every player scored the exact same
    number every week, or an empty roster): returns 1.0/0.0/0.5 by the sign
    of mean_diff instead of dividing by zero."""
    if std_diff <= 0:
        if mean_diff > 0:
            return 1.0
        if mean_diff < 0:
            return 0.0
        return 0.5
    return normal_cdf(mean_diff / std_diff)
