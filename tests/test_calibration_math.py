"""Calibration math ported from the worktree-phase4-college-data branch, with
its own tests, unchanged. Not wired into any prediction yet."""

from dynasty_agent.metrics import brier_score, fit_platt_scaling, log_loss, logit, platt_scale, sigmoid



def test_logit_and_sigmoid_are_inverses():
    assert round(sigmoid(logit(0.7)), 6) == 0.7


def test_logit_clamps_extreme_probabilities_instead_of_raising():
    # An exact 0.0/1.0 is real and reachable (matchup_win_probability
    # returns it on a degenerate zero-variance side), never +/-inf.
    assert logit(0.0) < -10
    assert logit(1.0) > 10


def test_sigmoid_at_zero_is_one_half():
    assert sigmoid(0.0) == 0.5


def test_sigmoid_handles_large_magnitude_without_overflow():
    assert sigmoid(-1000.0) == 0.0
    assert round(sigmoid(1000.0), 6) == 1.0


def test_platt_scale_is_identity_at_a1_b0():
    assert round(platt_scale(0.7, 1.0, 0.0), 6) == 0.7


def test_fit_platt_scaling_returns_identity_on_empty_sample():
    assert fit_platt_scaling([]) == (1.0, 0.0)


def test_fit_platt_scaling_recovers_known_overconfidence():
    # Raw probabilities of 0.9/0.1 are overconfident here by construction:
    # the real empirical win rate at each extreme is only 70%/30%, not
    # 90%/10%. A correct fit should deflate that (a < 1.0) and measurably
    # improve Brier score on the same sample.
    pairs = [(0.9, 1.0)] * 7 + [(0.9, 0.0)] * 3 + [(0.1, 0.0)] * 7 + [(0.1, 1.0)] * 3
    a, b = fit_platt_scaling(pairs, iterations=1000, learning_rate=0.1)
    assert a < 1.0
    calibrated_pairs = [(platt_scale(p, a, b), y) for p, y in pairs]
    assert brier_score(calibrated_pairs) < brier_score(pairs)


def test_brier_score_perfect_predictions_score_zero():
    assert brier_score([(1.0, 1.0), (0.0, 0.0)]) == 0.0


def test_brier_score_always_predicting_half_scores_one_quarter():
    assert brier_score([(0.5, 1.0), (0.5, 0.0)]) == 0.25


def test_brier_score_empty_sample_is_zero():
    assert brier_score([]) == 0.0


def test_log_loss_confident_correct_prediction_scores_near_zero():
    assert log_loss([(0.99, 1.0)]) < 0.02


def test_log_loss_confident_wrong_prediction_scores_high():
    assert log_loss([(0.99, 0.0)]) > 4.0


def test_log_loss_empty_sample_is_zero():
    assert log_loss([]) == 0.0
