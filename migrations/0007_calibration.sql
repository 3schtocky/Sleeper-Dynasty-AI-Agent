-- Phase 5: backtest results and the fitted calibration correction for
-- matchup.py's win-probability heuristic. See calibration.py for the full
-- methodology, including the randomized team_a/team_b assignment that
-- keeps a real home-field effect from being baked into the correction.
CREATE TABLE IF NOT EXISTS backtest_games (
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    team_a TEXT NOT NULL,              -- randomly assigned real side, not always home
    team_b TEXT NOT NULL,
    team_a_is_home INTEGER NOT NULL,   -- 1/0, so the random assignment stays auditable
    raw_win_probability_a REAL,
    predicted_margin REAL,
    predicted_std REAL,
    team_a_players_found INTEGER,      -- coverage/confidence signal, not hidden
    team_b_players_found INTEGER,
    actual_a_win REAL,                 -- 1.0 / 0.0 / 0.5 for a real tie
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (season, week, team_a, team_b)
);

CREATE TABLE IF NOT EXISTS calibration_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fitted_at TEXT NOT NULL,
    start_season INTEGER NOT NULL,
    end_season INTEGER NOT NULL,
    sample_size INTEGER NOT NULL,
    platt_a REAL NOT NULL,
    platt_b REAL NOT NULL,
    brier_before REAL NOT NULL,
    brier_after REAL NOT NULL,
    log_loss_before REAL NOT NULL,
    log_loss_after REAL NOT NULL,
    accuracy_before REAL NOT NULL,
    accuracy_after REAL NOT NULL
);
