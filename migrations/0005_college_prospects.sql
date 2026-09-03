-- Phase 4 completion: college production data, from sportsdataverse's
-- public sportsdataverse-data GitHub releases (see college.py for the full
-- verified-live schema notes). Unblocks the college-production question
-- CLAUDE.md/PLANNING.md left open.
--
-- recruit_id is cfb_recruits' own real, stable id (247Sports/CFBD's own
-- recruit index), used as the primary key here rather than a composite of
-- name/school/year, which is a more reliable natural key than anything
-- ncaa_mfb_player_stats offers (no player id at all, see below).
CREATE TABLE IF NOT EXISTS cfb_recruits (
    recruit_id TEXT PRIMARY KEY,
    season INTEGER NOT NULL,               -- recruiting class year
    player_name TEXT NOT NULL,
    position TEXT,
    school TEXT,                           -- CFBD-style name, resolved from team_id via cfb_team_info
    stars INTEGER,
    grade REAL,                            -- 247Sports composite rating, roughly 0-100
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cfb_recruits_name_season ON cfb_recruits (player_name, season);

CREATE TABLE IF NOT EXISTS cfb_team_talent (
    season INTEGER NOT NULL,
    team_id TEXT NOT NULL,
    school TEXT,
    talent_composite REAL,
    talent_rank INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (season, team_id)
);

CREATE TABLE IF NOT EXISTS cfb_returning_production (
    season INTEGER NOT NULL,
    team_id TEXT NOT NULL,
    school TEXT,
    off_returning REAL,                    -- fraction (0-1) of returning offensive production, the fantasy-relevant side
    n_returning INTEGER,                   -- rough sample-size signal, not itself a fantasy input
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (season, team_id)
);

-- Derived from ncaa_mfb_player_stats: a real per-game, long-format boxscore
-- (a rushing row and a receiving row for the same player/game are separate
-- rows), summed here to season totals. No stable per-player id exists in
-- that file, confirmed live, so player_name plus school is the real key
-- available, a stated limitation shared with normalize_school_name's own
-- coverage gaps, not hidden.
CREATE TABLE IF NOT EXISTS college_production_season (
    season INTEGER NOT NULL,
    player_name TEXT NOT NULL,
    school TEXT NOT NULL,
    position TEXT,
    rushing_yards REAL,
    rushing_tds INTEGER,
    receiving_yards REAL,
    receiving_tds INTEGER,
    team_rushing_yards REAL,
    team_rushing_tds INTEGER,
    team_receiving_yards REAL,
    team_receiving_tds INTEGER,
    dominator_rating REAL,                 -- metrics.dominator_rating, combination (rush+rec vs. rec-only) decided per position at ingest time
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (season, player_name, school)
);
CREATE INDEX IF NOT EXISTS idx_college_production_name ON college_production_season (player_name);
