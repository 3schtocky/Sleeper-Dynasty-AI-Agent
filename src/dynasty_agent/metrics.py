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
