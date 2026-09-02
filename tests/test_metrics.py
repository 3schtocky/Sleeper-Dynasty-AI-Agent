from datetime import date

from dynasty_agent.metrics import (
    age_multiplier,
    compute_fantasy_points,
    discounted_pick_value,
    map_snapshot_to_week,
    percentile_rank,
    production_score,
    situation_multiplier,
    three_year_age_factor,
    three_year_value,
    weighted_opportunity,
    win_now_value,
    yards_per_route_run_estimate,
)

LEAGUE_SCORING = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "pass_int": -1.0,
    "pass_2pt": 2.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rush_2pt": 2.0,
    "rec": 1.0,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "rec_2pt": 2.0,
    "fum_lost": -2.0,
}


def test_compute_fantasy_points_matches_hand_calculation_for_a_qb_line():
    stat_line = {
        "passing_yards": 250,
        "passing_tds": 2,
        "passing_interceptions": 1,
        "rushing_yards": 30,
    }
    # 250*0.04 + 2*4 + 1*-1 + 30*0.1 = 10 + 8 - 1 + 3 = 20
    assert compute_fantasy_points(stat_line, LEAGUE_SCORING) == 20.0


def test_compute_fantasy_points_matches_hand_calculation_for_a_full_ppr_receiver_line():
    stat_line = {"receptions": 7, "receiving_yards": 95, "receiving_tds": 1}
    # 7*1 + 95*0.1 + 1*6 = 7 + 9.5 + 6 = 22.5
    assert compute_fantasy_points(stat_line, LEAGUE_SCORING) == 22.5


def test_compute_fantasy_points_handles_missing_stats_and_missing_weights():
    # This league carries no K or DST weights in scoring_settings. An empty
    # dict must yield 0, not a KeyError.
    assert compute_fantasy_points({"passing_yards": 300}, {}) == 0.0


def test_compute_fantasy_points_sums_fumble_subfields_when_no_total_is_given():
    stat_line = {"sack_fumbles_lost": 1, "rushing_fumbles_lost": 1, "receiving_fumbles_lost": 0}
    assert compute_fantasy_points(stat_line, LEAGUE_SCORING) == -4.0


def test_compute_fantasy_points_prefers_an_explicit_fumbles_lost_total():
    stat_line = {
        "sack_fumbles_lost": 5,  # would be wrong if this got summed too
        "rushing_fumbles_lost": 5,
        "receiving_fumbles_lost": 5,
        "fumbles_lost": 1,
    }
    assert compute_fantasy_points(stat_line, LEAGUE_SCORING) == -2.0


def test_weighted_opportunity_is_carries_plus_double_targets():
    assert weighted_opportunity(carries=10, targets=5) == 20
    assert weighted_opportunity(carries=None, targets=3) == 6
    assert weighted_opportunity(carries=None, targets=None) == 0


def test_yards_per_route_run_estimate_divides_receiving_yards_by_offensive_snaps():
    assert yards_per_route_run_estimate(receiving_yards=60, offensive_snaps=30) == 2.0


def test_yards_per_route_run_estimate_is_none_without_a_snap_count():
    assert yards_per_route_run_estimate(receiving_yards=60, offensive_snaps=0) is None
    assert yards_per_route_run_estimate(receiving_yards=60, offensive_snaps=None) is None


# -- age curve ----------------------------------------------------------------


def test_age_multiplier_is_flat_through_the_peak_window():
    assert age_multiplier("WR", 22) == 1.0
    assert age_multiplier("WR", 28) == 1.0  # peak_end itself, still full value


def test_age_multiplier_decays_past_peak_and_never_hits_zero():
    at_peak = age_multiplier("RB", 25)
    two_past = age_multiplier("RB", 27)
    five_past = age_multiplier("RB", 30)
    assert at_peak == 1.0
    assert 0 < five_past < two_past < at_peak


def test_age_multiplier_rb_decays_faster_than_qb_the_same_distance_past_peak():
    # RB peak_end=25, QB peak_end=32; compare each 3 years past its own peak.
    rb_three_past = age_multiplier("RB", 28)
    qb_three_past = age_multiplier("QB", 35)
    assert rb_three_past < qb_three_past


def test_age_multiplier_handles_unknown_age_and_unknown_position():
    assert age_multiplier("WR", None) == 1.0
    assert age_multiplier(None, 40) > 0  # falls back to a default curve, does not raise


def test_three_year_age_factor_is_lower_than_the_current_year_multiplier_past_peak():
    current_year = age_multiplier("RB", 26)
    three_year = three_year_age_factor("RB", 26)
    assert three_year < current_year  # averaging in two more years of decline pulls it down


def test_three_year_age_factor_matches_flat_peak_when_still_climbing():
    assert three_year_age_factor("WR", 24) == 1.0


# -- situation score ------------------------------------------------------------


def test_percentile_rank_orders_correctly():
    population = [10, 20, 30, 40, 50]
    assert percentile_rank(50, population) == 90.0  # better than 4 of 5
    assert percentile_rank(10, population) == 10.0  # better than 0 of 5
    assert percentile_rank(30, population) == 50.0  # tied with itself, better than 2 of 5


def test_percentile_rank_defaults_to_neutral_on_missing_data():
    assert percentile_rank(None, [1, 2, 3]) == 50.0
    assert percentile_rank(5, []) == 50.0
    assert percentile_rank(5, [None, None]) == 50.0


def test_situation_multiplier_is_a_noop_at_league_average():
    assert situation_multiplier(50.0) == 1.0


def test_situation_multiplier_is_bounded():
    assert situation_multiplier(0.0) == 0.85
    assert situation_multiplier(100.0) == 1.15
    assert situation_multiplier(1000.0) == 1.15  # clamped, not extrapolated


# -- production score and value --------------------------------------------------


def test_production_score_discounts_qb_and_bumps_wr():
    assert production_score(20.0, "QB") == 14.0
    assert production_score(20.0, "WR") == 21.0
    assert production_score(20.0, "RB") == 20.0
    assert production_score(20.0, "TE") == 20.0


def test_win_now_value_is_production_times_age_and_situation():
    # 20 production, WR at peak age (mult 1.0), league-average situation (mult 1.0)
    assert win_now_value(20.0, "WR", 25, 50.0) == 20.0


def test_three_year_value_is_lower_than_win_now_for_an_aging_player():
    production = 20.0
    assert three_year_value(production, "RB", 27, 50.0) < win_now_value(production, "RB", 27, 50.0)


# -- depth chart snapshot to week mapping ----------------------------------------


def test_map_snapshot_to_week_finds_the_next_upcoming_week():
    week_starts = [(1, date(2025, 9, 4)), (2, date(2025, 9, 11)), (3, date(2025, 9, 18))]
    assert map_snapshot_to_week(date(2025, 8, 15), week_starts) == 1
    assert map_snapshot_to_week(date(2025, 9, 5), week_starts) == 2
    assert map_snapshot_to_week(date(2025, 9, 4), week_starts) == 1  # exactly on a game day counts


def test_map_snapshot_to_week_is_none_past_the_last_known_week():
    week_starts = [(1, date(2025, 9, 4)), (2, date(2025, 9, 11))]
    assert map_snapshot_to_week(date(2025, 9, 20), week_starts) is None


# -- pick discounting -----------------------------------------------------------


def test_discounted_pick_value_no_discount_at_the_base_year():
    assert discounted_pick_value(1000.0, years_from_base=0, discount_rate=0.20) == 1000.0


def test_discounted_pick_value_compounds_per_year():
    # 1000 * 0.8 * 0.8 = 640
    assert round(discounted_pick_value(1000.0, years_from_base=2, discount_rate=0.20), 4) == 640.0


def test_discounted_pick_value_never_gives_a_bonus_for_negative_years():
    assert discounted_pick_value(1000.0, years_from_base=-3, discount_rate=0.20) == 1000.0
