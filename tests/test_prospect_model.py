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
    college = {"seasons": [(2023, 0.30)], "peak_dominator": 0.30}
    qb = pm.feature_row("QB", 1, 22.0, college, None, None)
    assert (qb["peak_dominator"], qb["broke_out"], qb["breakout_age"]) == (0.0, 0.0, 0.0)
    assert (qb["athletic_known"], qb["athletic"]) == (0.0, 0.0)

    from datetime import date
    wr = pm.feature_row("WR", 20, 21.5, college, date(2003, 9, 1), 70.0)
    assert wr["broke_out"] == 1.0 and wr["never_broke_out"] == 0.0
    assert wr["breakout_age"] == pytest.approx(20.0 - pm.BREAKOUT_AGE_CENTER, abs=0.01)
    assert (wr["athletic_known"], wr["athletic"]) == (1.0, 20.0)
    assert wr["log_pick"] == pytest.approx(2.9957, abs=1e-3)


def test_games_scheduled_changed_in_2021():
    assert pm.games_scheduled(2020) == 16
    assert pm.games_scheduled(2021) == 17
