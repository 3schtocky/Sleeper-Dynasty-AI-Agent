-- Dynasty Agent initial schema.
-- Applied by dynasty_agent.db.apply_migrations(). Do not edit an already
-- applied migration; add a new numbered file instead.

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Generic raw-response cache. Every Sleeper and FantasyCalc call checks here
-- first and honors a per-call TTL before hitting the network again.
CREATE TABLE IF NOT EXISTS api_cache (
    cache_key TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,        -- Sleeper player_id
    full_name TEXT,
    first_name TEXT,
    last_name TEXT,
    position TEXT,
    team TEXT,
    age INTEGER,
    years_exp INTEGER,
    status TEXT,
    injury_status TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS league (
    league_id TEXT PRIMARY KEY,
    name TEXT,
    season TEXT,
    status TEXT,
    previous_league_id TEXT,
    num_teams INTEGER,
    scoring_settings_json TEXT NOT NULL,
    roster_positions_json TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nfl_state (
    fetched_at TEXT PRIMARY KEY,
    season TEXT,
    week INTEGER,
    season_type TEXT,
    display_week INTEGER
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    league_id TEXT NOT NULL,
    display_name TEXT,
    team_name TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rosters (
    roster_id INTEGER PRIMARY KEY,
    league_id TEXT NOT NULL,
    owner_id TEXT,
    wins INTEGER,
    losses INTEGER,
    ties INTEGER,
    fpts REAL,
    fpts_against REAL,
    waiver_position INTEGER,
    waiver_budget_used INTEGER,
    fetched_at TEXT NOT NULL
);

-- Current roster composition. Fully replaced (delete + insert) on every
-- sync, so this is a snapshot, not a history.
CREATE TABLE IF NOT EXISTS roster_players (
    roster_id INTEGER NOT NULL,
    player_id TEXT NOT NULL,
    slot TEXT NOT NULL CHECK (slot IN ('starter', 'bench', 'taxi', 'reserve')),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (roster_id, player_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    league_id TEXT NOT NULL,
    week INTEGER,
    type TEXT,
    status TEXT,
    roster_ids_json TEXT,
    adds_json TEXT,
    drops_json TEXT,
    waiver_budget_json TEXT,
    created INTEGER,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matchups (
    league_id TEXT NOT NULL,
    week INTEGER NOT NULL,
    roster_id INTEGER NOT NULL,
    matchup_id INTEGER,
    points REAL,
    starters_json TEXT,
    players_json TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (league_id, week, roster_id)
);

CREATE TABLE IF NOT EXISTS traded_picks (
    league_id TEXT NOT NULL,
    season TEXT NOT NULL,
    round INTEGER NOT NULL,
    roster_id INTEGER NOT NULL,          -- the pick's original slot
    previous_owner_id INTEGER,
    owner_id INTEGER NOT NULL,           -- who currently holds it
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (league_id, season, round, roster_id)
);

CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    league_id TEXT,
    season TEXT,
    status TEXT,
    type TEXT,
    settings_json TEXT,
    slot_to_roster_id_json TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_picks (
    draft_id TEXT NOT NULL,
    pick_no INTEGER NOT NULL,
    round INTEGER,
    roster_id INTEGER,
    player_id TEXT,
    is_keeper INTEGER,
    metadata_json TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (draft_id, pick_no)
);

-- Per player per week, derived from nflverse. Raw counting stats plus the
-- opportunity and efficiency metrics this league's settings say matter most.
-- route_participation is left NULL: true route counts aren't in any public
-- nflverse file (paywalled at PFF and Fantasy Points). yards_per_route_run
-- is an estimate built from offensive snaps standing in for routes run, not
-- a real measurement, hence is_estimated.
CREATE TABLE IF NOT EXISTS weekly_stats (
    player_id TEXT NOT NULL,           -- Sleeper player_id where resolvable, else the nflverse gsis_id
    gsis_id TEXT,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    team TEXT,
    position TEXT,

    targets INTEGER,
    receptions INTEGER,
    receiving_yards REAL,
    receiving_tds INTEGER,
    carries INTEGER,
    rushing_yards REAL,
    rushing_tds INTEGER,
    pass_attempts INTEGER,
    completions INTEGER,
    passing_yards REAL,
    passing_tds INTEGER,
    interceptions INTEGER,
    fumbles_lost INTEGER,

    snap_share REAL,
    route_participation REAL,          -- unavailable in public data, always NULL for now
    target_share REAL,
    air_yards_share REAL,
    wopr REAL,
    yards_per_route_run REAL,          -- estimate, see is_estimated
    weighted_opportunity REAL,         -- carries + 2 * targets
    red_zone_touches INTEGER,
    inside_five_touches INTEGER,
    qb_rush_attempts_per_game REAL,
    team_pass_rate_over_expected REAL,

    fantasy_points REAL,               -- computed from this league's actual scoring_settings
    is_estimated INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (player_id, season, week)
);

CREATE TABLE IF NOT EXISTS market_values (
    player_id TEXT NOT NULL,           -- Sleeper player_id (FantasyCalc's sleeperId)
    source TEXT NOT NULL DEFAULT 'fantasycalc',
    as_of_date TEXT NOT NULL,
    value REAL,
    overall_rank INTEGER,
    position_rank INTEGER,
    redraft_value REAL,
    trend_30day REAL,
    trade_frequency REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (player_id, source, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_weekly_stats_player ON weekly_stats (player_id);
CREATE INDEX IF NOT EXISTS idx_market_values_player ON market_values (player_id, as_of_date);
CREATE INDEX IF NOT EXISTS idx_roster_players_player ON roster_players (player_id);
