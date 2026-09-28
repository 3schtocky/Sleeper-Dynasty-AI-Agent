from datetime import date

import pytest

from dynasty_agent.metrics import age_on, athletic_score, breakout_age, dominator_rating, speed_score


def test_age_on_is_exact_and_never_estimated():
    assert age_on(date(2003, 10, 1), date(2026, 4, 23)) == pytest.approx(22.56, abs=0.01)
    assert age_on(None, date(2026, 4, 23)) is None


def test_dominator_rating_hand_calculation():
    # 1,243 of 3,755 team yards (33.1%) and 12 of 33 team TDs (36.4%): average 34.7%.
    assert dominator_rating(1243, 12, 3755, 33) == pytest.approx((1243 / 3755 + 12 / 33) / 2)


def test_dominator_rating_undefined_without_team_totals():
    assert dominator_rating(500, 5, 0, 20) is None
    assert dominator_rating(500, 5, 3000, 0) is None
    assert dominator_rating(None, None, 3000, 20) == 0.0  # no catches is a real 0, not missing


def test_breakout_age_uses_the_first_season_at_twenty_percent():
    seasons = [(2023, 0.12), (2024, 0.21), (2025, 0.35)]
    status, age = breakout_age(seasons, date(2005, 3, 1))
    assert status == "broke_out"
    assert age == pytest.approx(age_on(date(2005, 3, 1), date(2024, 9, 1)))


def test_breakout_age_separates_never_from_unknown():
    assert breakout_age([(2024, 0.10), (2025, None)], date(2004, 1, 1)) == ("never", None)
    assert breakout_age([(2024, 0.30)], None) == ("unknown", None)


def test_speed_score_hand_calculation():
    assert speed_score(220, 4.40) == pytest.approx(220 * 200 / 4.40**4)
    assert speed_score(None, 4.40) is None
    assert speed_score(220, None) is None


def test_athletic_score_inverts_lower_is_better_drills_and_needs_three_tests():
    population = [
        {"speed_score": 90, "vertical": 30, "cone": 7.2, "shuttle": 4.4},
        {"speed_score": 100, "vertical": 35, "cone": 7.0, "shuttle": 4.2},
        {"speed_score": 110, "vertical": 40, "cone": 6.8, "shuttle": 4.0},
    ]
    best, tests = athletic_score(population[2], population)
    worst, _ = athletic_score(population[0], population)
    assert tests == 4
    assert best > 50 > worst  # the fastest cone and shuttle rank high, not low
    assert athletic_score({"speed_score": 110, "vertical": 40}, population) == (None, 2)
