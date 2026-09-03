-- Phase 5: real per-week historical injury status (nflverse's `injuries`
-- release, 2009+). The `players` table only ever holds TODAY's status,
-- useless for backtesting a real past week; this is that history.
CREATE TABLE IF NOT EXISTS nfl_injuries (
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    gsis_id TEXT NOT NULL,
    team TEXT,
    position TEXT,
    report_status TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (season, week, gsis_id)
);

CREATE INDEX IF NOT EXISTS idx_nfl_injuries_gsis ON nfl_injuries (gsis_id);
