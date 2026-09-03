-- Player ID crosswalk from dynastyprocess/data's db_playerids.csv, fetched
-- live at sync time, never vendored (the file is GPL-3.0, this repo is
-- MIT; committing a copy would pull that copyleft license into an MIT
-- codebase, see README's data-sources section).
--
-- mfl_id is the primary key. Confirmed live: the real file has no single
-- dynastyprocess-internal id column, and mfl_id (MyFantasyLeague's own id)
-- is the one column populated on effectively every real row, unlike
-- sleeper_id/gsis_id, which are only populated once Sleeper or the NFL
-- itself has assigned one.
--
-- Needed because roster_weekly's own sleeper_id/gsis_id crosswalk (already
-- used in nflverse.py) only has a row once a player has actually appeared
-- in a tracked NFL week. A drafted-but-not-yet-active rookie, or a taxi
-- prospect, has no roster_weekly row yet but is already in this file.
CREATE TABLE IF NOT EXISTS player_id_crosswalk (
    mfl_id TEXT PRIMARY KEY,
    sleeper_id TEXT,
    gsis_id TEXT,
    pfr_id TEXT,
    cfbref_id TEXT,
    espn_id TEXT,
    yahoo_id TEXT,
    name TEXT,
    merge_name TEXT,
    position TEXT,
    college TEXT,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_crosswalk_sleeper ON player_id_crosswalk (sleeper_id);
CREATE INDEX IF NOT EXISTS idx_crosswalk_gsis ON player_id_crosswalk (gsis_id);
