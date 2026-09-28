-- Team context for college production, added after the first pre-draft
-- board ranked San Jose State receivers above Jeremiah Smith: dominator
-- rating alone can't tell a 40% share at a 115th-ranked team from one at a
-- top-3 team. Both sources join on the same ESPN team_id cfb_player_season
-- uses (verified: 194 = Ohio State, 23 = San Jose State), no name matching.
-- conference/classification from sportsdataverse cfb_team_info_{season};
-- net_z (a z-scored opponent-adjusted net efficiency) from cfb_ratings_{season},
-- FBS teams only, NULL for an FCS team.
CREATE TABLE IF NOT EXISTS cfb_team_season (
    team_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    school TEXT,
    conference TEXT,
    classification TEXT,               -- 'fbs', 'fcs', ...
    net_z REAL,
    net_rank INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (team_id, season)
);
