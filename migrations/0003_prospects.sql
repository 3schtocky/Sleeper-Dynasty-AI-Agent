-- Phase 4 (rookie draft prep) data layer: real NFL draft capital and
-- athletic testing from nflverse's draft_picks and combine releases.
--
-- Named nfl_draft_picks / nfl_combine, not draft_picks: this league already
-- has its own draft_picks table (this league's Sleeper rookie-draft picks,
-- keyed by draft_id/pick_no/roster_id). A same-named table would either
-- collide or silently shadow it. Confirmed the existing table's real
-- purpose before naming these, not assumed.
--
-- Both nflverse releases are single flat files covering every season back
-- to nflverse's start (draft_picks from 1980, combine from 2000), not
-- per-season files like stats_player_week. team is stored pre-normalized to
-- this project's standard nflverse team code (see
-- prospects.to_nflverse_team_from_draft_code): nflverse's draft_picks file
-- itself ships PFR-style codes (GNB, KAN, LAR, LVR, NOR, NWE, SFO, TAM),
-- confirmed by querying the live file directly, not the GB/KC/LA/LV/NO/NE/
-- SF/TB codes weekly_stats and everything else in this project already use.
CREATE TABLE IF NOT EXISTS nfl_draft_picks (
    season INTEGER NOT NULL,
    round INTEGER,
    pick INTEGER,
    team TEXT,                 -- normalized to this project's nflverse team code at ingest time
    gsis_id TEXT,
    pfr_player_id TEXT,
    cfb_player_id TEXT,
    player_name TEXT,
    position TEXT,
    college TEXT,
    age INTEGER,                -- age at the time of that draft, per PFR
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (season, round, pick)
);

CREATE INDEX IF NOT EXISTS idx_nfl_draft_picks_gsis ON nfl_draft_picks (gsis_id);
CREATE INDEX IF NOT EXISTS idx_nfl_draft_picks_cfb ON nfl_draft_picks (cfb_player_id);

-- combine.draft_team ships as a full franchise name ("San Francisco
-- 49ers"), not a code, confirmed live; left un-normalized here as
-- informational only. nfl_draft_picks.team (crosswalked from the same PFR
-- pick, joined via pfr_id) is the authoritative landing-spot source, so
-- there is exactly one normalized team code per player, not two
-- disagreeing ones. draft_year, not season, is the pick's real draft year;
-- confirmed the two differ on 8 of 8968 real rows (undrafted combine
-- invitees and a few data gaps), so season is not treated as a stand-in.
CREATE TABLE IF NOT EXISTS nfl_combine (
    season INTEGER NOT NULL,
    draft_year INTEGER,
    draft_team TEXT,
    draft_round INTEGER,
    draft_ovr INTEGER,
    pfr_id TEXT,
    cfb_id TEXT,
    player_name TEXT,
    position TEXT,
    college TEXT,
    height_in TEXT,
    weight_lb REAL,
    forty REAL,
    bench REAL,
    vertical REAL,
    broad_jump REAL,
    cone REAL,
    shuttle REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (season, pfr_id)
);

CREATE INDEX IF NOT EXISTS idx_nfl_combine_cfb ON nfl_combine (cfb_id);
