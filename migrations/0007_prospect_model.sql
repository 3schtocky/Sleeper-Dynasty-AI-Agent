-- Fitted prospect model weights, one row per variant (post_draft,
-- pre_draft, baseline_draft_capital), replaced on every refit. Stored with
-- their training metadata and cross-validated error so every projection
-- traces back to a specific, dated fit in the database (CLAUDE.md working
-- rule), never to constants typed into the code.
CREATE TABLE IF NOT EXISTS prospect_model (
    variant TEXT PRIMARY KEY,
    features_json TEXT NOT NULL,       -- ordered feature names
    weights_json TEXT NOT NULL,        -- intercept first, then one weight per feature
    n_rows INTEGER NOT NULL,
    cv_mae REAL,                       -- leave-one-draft-class-out mean absolute error, PPG
    cv_r2 REAL,
    training_classes TEXT NOT NULL,
    ridge_lambda REAL NOT NULL,
    fitted_at TEXT NOT NULL
);
