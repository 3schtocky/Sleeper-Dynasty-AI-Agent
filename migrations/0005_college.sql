-- Phase 4 college production, from sportsdataverse's keyless ESPN college
-- football releases (espn_cfb_player_box, espn_cfb_game_rosters), chosen
-- over the College Football Data API, which needs an email to register.
-- Verified live before writing this: the same columns exist for every
-- season 2008 through the in-progress 2026 season.
--
-- One row per player per season per team (a mid-season transfer is two
-- rows). team_* columns are that team's totals over the same box score
-- rows, the dominator rating's denominator, computed from the same source
-- so a game missing from ESPN's file is missing from both sides of the
-- ratio, not just one.
CREATE TABLE IF NOT EXISTS cfb_player_season (
    athlete_id TEXT NOT NULL,          -- ESPN athlete id, the same id ESPN keeps once a player reaches the NFL
    season INTEGER NOT NULL,
    team_id TEXT NOT NULL,             -- ESPN team id
    athlete_name TEXT,
    position TEXT,                     -- QB/RB/WR/TE from ESPN's position id, NULL for any other position
    class_year TEXT,                   -- ESPN's roster label (FR/SO/JR/SR), latest week listed. Unreliable: seen wrong live (Jeremiah Smith, a 2025 sophomore, listed JR). Display only, never a model input.
    games INTEGER,                     -- games with any box score line for this player
    receptions INTEGER,
    rec_yds REAL,
    rec_td INTEGER,
    rush_att INTEGER,
    rush_yds REAL,
    rush_td INTEGER,
    pass_cmp INTEGER,
    pass_att INTEGER,
    pass_yds REAL,
    pass_td INTEGER,
    team_games INTEGER,
    team_rec_yds REAL,
    team_rec_td INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (athlete_id, season, team_id)
);

CREATE INDEX IF NOT EXISTS idx_cfb_player_season_season ON cfb_player_season (season, position);

-- Birth dates, kept apart from the season table because they come from
-- several sources (ESPN rosters here, later DynastyProcess and nflverse
-- players) and are sparse: checked live, 639 of 26,316 athletes on 2025
-- ESPN rosters carry one. dob_source says which source each came from.
CREATE TABLE IF NOT EXISTS cfb_athletes (
    athlete_id TEXT PRIMARY KEY,
    full_name TEXT,
    date_of_birth TEXT,                -- ISO date, NULL when no source has one
    dob_source TEXT,
    height_in REAL,
    weight_lb REAL,
    fetched_at TEXT NOT NULL
);
