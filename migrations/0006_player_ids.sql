-- Phase 4 player id crosswalk: one row per player, every id this project
-- joins on. Built from DynastyProcess's db_playerids.csv (keyless, GitHub)
-- with nflverse's players.parquet filling any id or birth date it lacks.
-- Checked live before writing this: for drafted QB/RB/WR/TE since 2010,
-- 97-100% per class carry an espn_id and a birth date through these two
-- sources, and sleeper_id is complete from the 2014 class on.
--
-- espn_id is the bridge to college production: ESPN keeps one athlete id
-- from college into the NFL (verified: Fernando Mendoza, 4837248, in both
-- the 2025 college box scores and DynastyProcess).
--
-- Replaced whole on every ingest, so no row outlives its source.
CREATE TABLE IF NOT EXISTS player_ids (
    row_key TEXT PRIMARY KEY,          -- gsis_id when present, else "espn:<id>", else "sleeper:<id>"
    gsis_id TEXT,
    espn_id TEXT,
    sleeper_id TEXT,
    pfr_id TEXT,
    cfbref_id TEXT,
    name TEXT,
    position TEXT,
    birthdate TEXT,                    -- ISO date
    birthdate_source TEXT,             -- 'dynastyprocess' or 'nflverse_players'
    draft_year INTEGER,
    draft_round INTEGER,
    draft_pick INTEGER,                -- overall pick number
    college TEXT,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_player_ids_espn ON player_ids (espn_id);
CREATE INDEX IF NOT EXISTS idx_player_ids_sleeper ON player_ids (sleeper_id);
CREATE INDEX IF NOT EXISTS idx_player_ids_gsis ON player_ids (gsis_id);
CREATE INDEX IF NOT EXISTS idx_player_ids_pfr ON player_ids (pfr_id);
