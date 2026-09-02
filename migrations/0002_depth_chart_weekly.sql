-- Depth chart snapshots, mapped from their raw snapshot date onto the NFL
-- week they precede (see dynasty_agent.metrics.map_snapshot_to_week). Built
-- for Phase 2's situation score and roster-competition context.
CREATE TABLE IF NOT EXISTS depth_chart_weekly (
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    team TEXT NOT NULL,
    gsis_id TEXT,
    player_name TEXT,
    pos_abb TEXT,
    pos_rank INTEGER,
    snapshot_dt TEXT NOT NULL,     -- the raw depth_charts.dt this row came from, for auditing
    fetched_at TEXT NOT NULL,
    -- pos_rank deliberately excluded from the key: it is the value being
    -- tracked, and can change between the several snapshots that map to the
    -- same week. The row always reflects the latest snapshot in that window.
    PRIMARY KEY (season, week, team, pos_abb, gsis_id)
);

CREATE INDEX IF NOT EXISTS idx_depth_chart_weekly_gsis ON depth_chart_weekly (gsis_id);
