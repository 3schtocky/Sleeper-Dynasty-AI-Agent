-- nfl_combine was keyed (season, pfr_id), and ingestion filtered out every
-- row with no pfr_id to satisfy that key. Checked live: 54 of 319 real 2026
-- combine rows have no pfr_id (166 of 464 in 2021). Today every one of them
-- went undrafted, however a pre-draft combine row (next February's class)
-- has no PFR NFL page yet either, so pre-draft athletic testing, the one
-- input that exists before the draft, would have been dropped whole.
--
-- combine_id is a surrogate key, built by prospects.combine_row_id:
-- season plus the first identifier present of pfr_id, cfb_id, or
-- lowercased player_name|college. Ingestion replaces the whole table each
-- run (the source is one whole-history file), so a row whose best id
-- changes, a pre-draft row gaining a pfr_id once drafted, never leaves a
-- stale duplicate behind.
CREATE TABLE nfl_combine_new (
    combine_id TEXT PRIMARY KEY,
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
    fetched_at TEXT NOT NULL
);

-- Every existing row has a pfr_id (the old WHERE clause guaranteed it).
INSERT INTO nfl_combine_new
SELECT season || ':pfr:' || pfr_id, season, draft_year, draft_team, draft_round, draft_ovr,
       pfr_id, cfb_id, player_name, position, college, height_in, weight_lb,
       forty, bench, vertical, broad_jump, cone, shuttle, fetched_at
FROM nfl_combine;

DROP TABLE nfl_combine;
ALTER TABLE nfl_combine_new RENAME TO nfl_combine;

CREATE INDEX IF NOT EXISTS idx_nfl_combine_cfb ON nfl_combine (cfb_id);
CREATE INDEX IF NOT EXISTS idx_nfl_combine_pfr ON nfl_combine (pfr_id);
