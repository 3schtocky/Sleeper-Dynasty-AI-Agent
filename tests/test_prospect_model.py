import pytest

from dynasty_agent import prospect_model as pm


def test_solve_matches_a_hand_solved_system():
    # 2x + y = 5, x + 3y = 10  ->  x = 1, y = 3
    assert pm._solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0]) == pytest.approx([1.0, 3.0])


def test_solve_rejects_a_feature_with_no_variation():
    with pytest.raises(ValueError):
        pm._solve([[1.0, 1.0], [1.0, 1.0]], [1.0, 2.0])


def test_fit_ridge_recovers_known_weights_with_a_tiny_penalty():
    x = [[float(i), float(i % 3)] for i in range(30)]
    y = [4.0 + 2.0 * a - 1.5 * b for a, b in x]
    weights = pm.fit_ridge(x, y, ridge_lambda=1e-9)
    assert weights == pytest.approx([4.0, 2.0, -1.5], abs=1e-5)
    assert pm.predict(weights, [10.0, 2.0]) == pytest.approx(4.0 + 20.0 - 3.0, abs=1e-4)


def test_fit_ridge_penalty_shrinks_weights_toward_zero():
    x = [[float(i)] for i in range(10)]
    y = [3.0 * v for v in range(10)]
    assert abs(pm.fit_ridge(x, y, ridge_lambda=50.0)[1]) < abs(pm.fit_ridge(x, y, ridge_lambda=1e-9)[1])


def test_leave_one_class_out_nearly_recovers_a_noiseless_relationship():
    # Not exact: the default ridge penalty deliberately shrinks the slope a
    # little (16 training rows per fold, lambda 1), so a small error remains.
    rows = [
        {"draft_class": c, "features": {"f": float(i)}, "target": 1.0 + 0.5 * i}
        for c in (2018, 2019, 2020) for i in range(8)
    ]
    cv = pm.leave_one_class_out(rows, ("f",))
    assert cv["n"] == 24
    assert cv["mae"] < 0.15
    assert cv["r2"] > 0.98


def test_feature_row_zeroes_receiving_for_qbs_and_flags_unknowns():
    strong = {"power_conf": True, "fbs": True, "net_z": 1.5, "conference": "SEC"}
    weak = {"power_conf": False, "fbs": True, "net_z": -0.8, "conference": "Sun Belt"}
    college = {"seasons": [(2023, 0.30)], "peak_dominator": 0.30, "peak_team": strong, "last_team": weak}
    qb = pm.feature_row("QB", 1, 22.0, college, None, None)
    assert (qb["peak_dominator"], qb["broke_out"], qb["breakout_age"]) == (0.0, 0.0, 0.0)
    assert (qb["athletic_known"], qb["athletic"]) == (0.0, 0.0)
    assert (qb["power_conf"], qb["team_strength"]) == (0.0, -0.8)  # a QB's context is his last season

    from datetime import date
    wr = pm.feature_row("WR", 20, 21.5, college, date(2003, 9, 1), 70.0)
    assert wr["broke_out"] == 1.0 and wr["never_broke_out"] == 0.0
    assert wr["breakout_age"] == pytest.approx(20.0 - pm.BREAKOUT_AGE_CENTER, abs=0.01)
    assert (wr["athletic_known"], wr["athletic"]) == (1.0, 20.0)
    assert wr["log_pick"] == pytest.approx(2.9957, abs=1e-3)
    assert (wr["power_conf"], wr["team_strength"]) == (1.0, 1.5)  # a receiver's context is his peak season

    unrated = pm.feature_row("WR", None, 21.0, {**college, "peak_team": {**weak, "net_z": None}}, None, None)
    assert unrated["team_strength"] is None  # an FCS/unrated team stays None, never guessed


def test_is_power_conference_tracks_realignment():
    from dynasty_agent.college import is_power_conference
    assert is_power_conference("Pac-12", 2023, "26")
    assert not is_power_conference("Pac-12", 2024, "204")  # two-team Pac-12 from 2024
    assert is_power_conference("FBS Independents", 2025, "87")  # Notre Dame
    assert not is_power_conference("Sun Belt", 2025, "2026")


def test_games_scheduled_changed_in_2021():
    assert pm.games_scheduled(2020) == 16
    assert pm.games_scheduled(2021) == 17
