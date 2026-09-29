# Phase planning

Working checklist and build log for Phases 2 through 4. Phase 0 and Phase 1 are done and verified; Phases 2 and 3 below are also done. CLAUDE.md has the quick status summary and command list, this file has the full detail behind every number: methodology decisions and why they were made, real bugs found and fixed, and what was actually verified live versus just written. Each phase's open questions get confirmed before code gets written, the same discipline Phase 1 used for the nflverse release names and the FantasyCalc response shape.

## Phase 2: analysis layer

### Open questions, confirmed
- [x] Age curve shape: smooth decay, not piecewise linear. Flat at 1.0 through each position's peak, then `exp(-decay_rate * years_past_peak)`. Peak/decay per position in `metrics.AGE_CURVES`, reasoning in the comment above it (RB: peak 25, decay 0.32, sharp; WR/TE: peak 28/30, decay 0.15, gradual fade; QB: peak 32, decay 0.08, holds longest).
- [x] Situation score inputs: combined, not dropped. Average of three percentile ranks against all 32 NFL teams: QB passing EPA/game, team pass rate over expected, and sack rate allowed (inverted, an OL pass-pro proxy since real OL grades are paywalled). Implemented in `valuation.team_situation_scores`.
- [x] Depth chart join: each snapshot mapped to the next NFL week it precedes, via `metrics.map_snapshot_to_week`, built from real game dates in play-by-play. Verified against Dallas's actual 2025 WR depth chart (CeeDee Lamb ranked WR1, correctly).
- [x] Pick-value discount rate: CLI flag with a default. Not built yet, belongs to the trade evaluator below.

### Build order
1. [x] Depth chart join: `nflverse.derive_depth_chart_weekly`, migration `0002_depth_chart_weekly.sql`. 122,691 rows for 2025, spot-checked.
2. [x] Production score: `metrics.production_score`, position-weighted (QB 0.70x discount, WR 1.05x bump, RB/TE neutral).
3. [x] Dynasty age adjustment: `metrics.age_multiplier` / `three_year_age_factor`.
4. [x] Situation score: `valuation.team_situation_scores`, scoped to QB EPA, team pass rate, and the sack-rate OL proxy, all that is actually available.
5. [x] Win-now value and three-year value: `metrics.win_now_value` / `three_year_value`, `valuation.player_valuations`, reported as separate columns, never blended.
6. [x] Contend-or-rebuild verdict: `valuation.contend_or_rebuild`. Roster-construction based (win-now and three-year percentile vs. the other 11 teams), confidence stated and explicitly low pre-week-1, 0 games played.
7. [x] Trade evaluator: `valuation.evaluate_trade`, `dynasty-agent trade`. Both sides valued on win-now and three-year axes; picks discounted via `metrics.discounted_pick_value`, anchored to FantasyCalc's real "2027 {round}" price (`PICK_VALUE_BASE_SEASON`) and compared back against FantasyCalc's own price for the exact pick traded, that comparison is the arbitrage; `--discount-rate` CLI flag, default 20%/year; consolidation and deconsolidation flagged by asset count; fit against the current contend-or-rebuild posture stated, not just implied.
8. [x] A test for each formula before any CLI command wrapped it: age curve, situation score math, production score, win-now/three-year value, pick discounting, and the depth chart date-to-week mapping are all in `tests/test_metrics.py`. 27 tests passing.

### Trade evaluator, verified live
- Player-for-player: sensible values, no crash.
- Pick-only: 2027 1st (the base season) priced with exactly 0 arbitrage against FantasyCalc, as it should; a 2029 1st came in $30 under FantasyCalc's own price at the default 20% discount rate, a small, plausible gap.
- Two-for-one: correctly flagged as a consolidation opportunity, however the raw win-now/three-year numbers still came back negative in this example, the tool reports both rather than letting the heuristic override the math.
- Errors handled cleanly, not a crash: unknown player name, malformed pick spec ("2027" instead of "2027-1"), and an ambiguous name ("Josh" matched 10+ players) all exit 1 with a clear message.

### Acceptance test
`dynasty-agent valuate` prints the roster with win-now value, three-year value, and the contend-or-rebuild verdict, inputs shown. Ran clean against live 2025 nflverse data.

### Known limitation, flagged not hidden
Team pass rate over expected penalizes run-heavy offenses (Baltimore under Lamar Jackson scored a 21st-percentile situation, dragging his win-now value down) even though CLAUDE.md's own strategic notes treat a rushing QB's offense differently, that volume is a feature for him, not a situation flaw. The formula does not currently know the difference. Worth a second look before this feeds a real trade decision.

## Phase 3: weekly workflow

**Constraint, stated by request, not just by prior habit: quantitative first.** Every input here should resolve to a real, sourced number wherever one exists, not a qualitative override layered on top of the math. Concretely: Vegas implied team totals and spreads come from real market data, not a gut adjustment. Opponent strength is EPA allowed per play (already derivable from Phase 1's play-by-play ingestion), never raw fantasy points allowed, which is schedule-biased and noisy. The lineup optimizer picks by computed win probability against that week's specific opponent, not raw projected points. Injury and weather feed in as structured multipliers on the underlying math (the same pattern `metrics.injury_adjusted_mean`/`injury_adjusted_variance` already use in the matchup-prediction draft), not as narrative color. Sentiment-only sources (beat writer chatter, Reddit) stay exactly what CLAUDE.md's working rules already call them, signal, not fact, and never substitute for a real underlying stat.

### Open questions, confirmed
- [x] Odds API. Resolved for free: nflverse's own schedules file (`spread_line`/`total_line`/moneylines, no key, no paid provider) is already wired up and tested in `matchup.py` (`team_week_implied_points`, `team_season_avg_implied_points`). Reused directly, not a second provider.
- [x] Injury and practice-report source: Sleeper's structured `injury_status` only, by request. No web-search layer built. Already the exact signal `matchup.py` uses, now reused a third time in `weekly.project_player`.
- [x] Weather source: `api.weather.gov` (pulled programmatically, per direct request), confirmed live. Real technical detail resolved during the build, not just flagged: no static team-to-stadium table works, `games.parquet`'s own `stadium`/`roof`/`stadium_id` columns are read per game (international games, e.g. a real 2026 Week 1 game's home "LA" mapping to Melbourne Cricket Ground, break any team-keyed assumption). `weather.STADIUM_COORDINATES` covers the 30 current domestic venues, keyed by `stadium_id`, not team name (survives sponsor renames). An international game's venue isn't in it and comes back explicitly "not covered." NWS forecasts only reach about a week out; a game further off comes back "not forecasted yet," never a guessed number.

### Build order
1. [x] Vegas implied team totals and spreads: `weekly.team_vegas_context`, thin wrapper reusing `matchup.py`'s existing functions.
2. [x] Opponent strength by position from real EPA allowed per play: `weekly.opponent_strength_by_position`, play-by-play joined to the weekly roster crosswalk for position (a QB's own scrambles count under QB, not RB), percentile-ranked against the other 31 defenses per position. All 32 teams covered on a live run.
3. [x] Injury checks: Sleeper's `injury_status`, already flowing through `weekly.project_player` the same way `matchup.py` uses it.
4. [x] Weather: `weather.game_wind_forecast`, real per-game venue and roof status, real NWS forecast wind, 15 mph flag. Dome/closed-roof games short-circuit with no network call.
5. [x] Lineup optimizer: `weekly.optimize_lineup`, `dynasty-agent optimize-lineup`. Brute-force search (a few thousand valid lineups at most for this league's roster size, proven fast enough live, ~2s) over every combination respecting the league's own real `roster_positions` (never hardcoded), picking by `matchup_win_probability` against the real Sleeper opponent for that week (via a newly-added `sleeper.sync_matchups`, closing a gap where Phase 1 defined the `matchups` table but never populated it), not raw points. Reports the highest-raw-points lineup alongside for comparison.
6. [x] FAAB bid sizing: `weekly.faab_recommendation`, `dynasty-agent faab --player <name>`. Sized against real remaining budget (`rosters.waiver_budget_used`) and real weeks left before the playoffs (`playoff_week_start` minus the real current week), scaled by the target's real win-now value percentile among players actually unrostered right now, not a guess at name value. `FAAB_MIN/MAX_VALUE_MULTIPLIER` are round labeled constants, not fitted, same honesty standard as the injury multipliers.
7. [x] Weekly digest: `dynasty-agent digest --week <N>`. Ties 1-6 together: recommended lineup with win probability, wind flags on real per-player teams, top bench options, and real sized FAAB suggestions for the highest-value actually-available free agents (not just a pointer to run `faab` separately, the acceptance test asks for suggestions, so it gives them).

### Verified live, not just written
- `optimize_lineup` against the real Week 1 2026 roster and real Sleeper matchup: solved in ~2s, correctly benched Lamar Jackson for Matthew Stafford, a real, surprising-sounding but honest result, Stafford's actual 2025 FPPG (21.1) beat Lamar's (17.1) in the only season this project has data for. No dynasty reputation or situation score in this call, on purpose, a weekly start/sit decision isn't a dynasty-value decision.
- `faab_recommendation` tested against a rostered player (correctly flagged `is_rostered`, not silently priced as available) and a real, genuinely unrostered free agent (Marquise Brown, 98.8th percentile among what's actually available, $21 suggested out of $100 with 14 weeks left).
- `digest` end to end: network-bound (live NWS forecast calls per recommended starter), correctness kept over shaving that down. Not a fixed cost, re-timed on three separate runs and got three different numbers: 47s before a real inefficiency was caught and fixed (`top_faab_targets` was recomputing `player_valuations`, non-trivial, runs `team_situation_scores` under the hood, once per candidate instead of once total), ~21s right after that fix, then ~7-8s consistently on two more runs later the same session with no further code changes, almost certainly network/OS-level caching from repeated invocations, not anything in the code. Report a single "digest takes ~Ns" figure as unverified until re-measured; the fix itself (47s to ~21s, a same-session, back-to-back comparison) is the reliable data point.
- 55 tests passing (6 new: `parse_wind_mph`'s range/single-value/unparseable cases, `_starting_slot_counts` against this league's real shape and a different one, proving it isn't hardcoded).

### Acceptance test
`dynasty-agent digest` for a real week, producing a lineup recommendation and FAAB suggestions with inputs shown. Ran clean against real Week 1 2026 data.

## Phase 3.5: full audit, before Phase 4

A full read of every module plus live checks against the real data files, done before building Phase 4 on top of this code. Each fix below has a test in `tests/test_integration.py` or `tests/test_metrics.py` (76 tests passing, up from 58; an earlier line in this file said 55, that count was already stale).

### Real bugs found and fixed
- **Rams wind flags never fired.** `digest` passed Sleeper's `LAR` to `weather.game_wind_forecast`, whose schedule lookup uses nflverse's `LA`, so every Rams game read as a bye. The third time this exact team-code bug shape turned up. Fixed inside `game_wind_forecast` with `valuation.to_nflverse_team`. Verified live: the Rams' real Week 4 2026 game at Lincoln Financial Field now returns a real NWS forecast (5 mph).
- **Lineup optimizer picked an arbitrary lineup whenever win probability tied.** With no opponent set, or an edge big enough that the normal CDF rounds to exactly 1.0, every lineup scores the same probability and the first one reached won, starting a 4-point RB over a 9-point WR in the flex. Found by the new integration test, not by inspection. Ties now break on projected points.
- **nflverse cache never refreshed an in-progress season.** `ensure_cached` returned any file already on disk, so ingesting 2026 mid-season froze its stats at the first download. `ingest-nflverse` now re-downloads automatically when `--season` is the current NFL season, and `--force` does it for any season.
- **Interrupted downloads were cached as valid forever.** Downloads wrote straight to the final path. `nflverse.download` now writes a `.part` file and renames it only once complete; `prospects.py` reuses it.
- **Pick valuation would have broken after the 2027 rookie draft.** `PICK_VALUE_BASE_SEASON = 2027` was hardcoded. Once FantasyCalc stops listing "2027 1st", every pick's model value would have gone to None and counted as 0 in the trade market total. The base season is now the earliest season FantasyCalc actually prices (`market.priced_pick_seasons`, read live, verified against the real response: 2027 through 2029, plus Early/Mid/Late tiers for 2027 that are correctly not mistaken for seasons). Anything still unpriced is named in `trade` output instead of a silent 0.
- **Combine ingest dropped every row with no `pfr_id`**: 1,531 of 8,968 real rows (54 of 319 in 2026). All undrafted today, however a pre-draft combine row has no PFR NFL page yet either, so the 2027 class's testing, the one real input that exists before its draft, would have been dropped. Migration `0004_combine_surrogate_key.sql` keys the table on `prospects.combine_row_id` (pfr, else cfb, else name and school), and ingestion replaces the table each run so a row gaining a `pfr_id` once drafted leaves no stale duplicate. Verified live: 8,965 rows land (up from 7,434), the 3 known duplicate `pfr_id` pairs collapsing as before, 1,531 of them with no `pfr_id`.
- **Contend-or-rebuild measured lineup-setting, not rosters.** It summed whatever starters each manager last set in Sleeper: empty or stale all offseason, wrong for any manager who hadn't set one. Each team is now scored on the best lineup it could start (`metrics.best_lineup_total`, starters plus bench, the league's own `roster_positions`).
- **FAAB budget was hardcoded to $100.** Now read from the league's `waiver_budget`; Sleeper's default is used and reported as a default only when the setting is absent.
- **Retired and unsigned players counted as FAAB targets.** Valuations come from last season's stats, so anyone who retired read as an available free agent, inflating the percentile pool and able to top `digest`'s target list. The pool is now limited to players on an NFL team today.
- **Situation score mixed postseason plays into two of its three inputs** (pass rate over expected, sack rate), while QB EPA was regular season only. All three are regular season now.
- **`drafts`/`draft_picks` were never populated** and `DRAFT_ID` was never read. `sync` now pulls every league draft and its picks (`SleeperClient.sync_drafts`), needed for Phase 4's draft order.
- **League renewal wasn't handled.** Sleeper gives a renewed dynasty league a new `league_id` each season; a `.env` from 2026 would keep syncing the 2026 league through the 2027 rookie draft. `sync` now checks for a renewed league (`sleeper.find_successor_league`, matched on `previous_league_id`) and prints the `init` command to switch; it never rewrites `.env` on its own. `init --league-id` now also searches next season's leagues, since Sleeper creates the renewal months before its own season rolls over.

### Hardening, no live bug found
- The `roster_weekly` crosswalk is grouped to one row per player-week. Checked live, 2025 has no duplicate; a `SELECT DISTINCT` would have written a player's week twice under two keys if a future file ever listed him with and without a `sleeper_id`.
- The schedules file is cached locally (6-hour max age, lines move all week) instead of read over HTTP on every call, several times per player in `digest`.
- `weekly_stats.is_estimated` is 1 on every row, on purpose: it marks `yards_per_route_run` as always the snaps-based estimate. Documented at the write site.

### Still open, flagged not fixed
- Every player with no NFL stats, the whole rostered 2026 rookie class included, is valued at 0 win-now and 0 three-year. That skews `valuate`'s verdict and `trade`'s three-year numbers. Fixing it needs the prospect model, so it is Phase 4 step 6 below, not a patch here.
- FAAB sizing doesn't discriminate at the top: on the live run below, all five `digest` targets got the same $25 bid (every one sits near the 100th percentile of what's available, so each maxes the value multiplier), and two of them were QBs for a roster already carrying two. Roster need isn't an input yet. Not changed here, it's a Phase 3 design question, not a bug.

### Verified live against the real league, after the fixes
Run 2026-09-28, NFL week 3, 2025 season as the valuation basis.
- `sync`: clean. The new draft sync pulled the league's one draft so far (2026 startup, complete, 252 picks); `waiver_budget` read as the real 100.
- `ingest-nflverse --season 2025`: 18,522 player-week rows and 122,691 depth chart rows.
- `valuate`: clean on the new best-lineup scoring. Verdict unclear (win-now 38th percentile, three-year 46th, 2 games played). All three rostered 2026 rookies show "no 2025 games", the Phase 4 gap above, confirmed live.
- `trade --send "Jonah Coleman" --send-pick 2028-2 --receive-pick 2027-1`: clean. 2027 1st priced against the live base season with exactly 0 arbitrage, as it should. The rookie shows 0 on this project's own model beside a real 1,922 FantasyCalc price, the same gap again.
- `digest --week 4`: clean, about 1.3 seconds (7 to 47 seconds before the schedules file was cached locally). Recommended lineup win probability 87.2%.

## Phase 4: rookie draft prep

**Same constraint as Phase 3: quantitative first.** The prospect board ranks on quantifiable inputs, draft capital, college production metrics (dominator rating), breakout age, athletic testing, with stated weights per position matching this league's actual scoring, not subjective scouting takes. Where a number can be sourced and computed, it gets computed; sentiment-only inputs stay explicitly labeled as such and never substitute for a real underlying stat, the same standard Phase 2 already set with win-now/three-year value and the trade evaluator's arbitrage math.

### Open questions
- [x] Draft capital, landing spot, athletic testing source: nflverse's `draft_picks` and `combine` releases, free, no key. Built and live-verified below.
- [x] Pre-draft vs. post-draft mode switch: an explicit CLI flag, not an automatic date switch. An automatic switch risks the same failure shape as the earlier Vegas-season bug in `matchup.py` (guessing a value when the real one isn't resolvable yet); a flag stays explicit.
- [ ] College production source (dominator rating, breakout age): **reopened.** The College Football Data API was the first choice, confirmed live that it needs a registered key (unauthenticated calls return 401), then rejected by explicit request, registering requires an email. No keyless, free, structured source has been confirmed since. Nothing gets built against a guessed or scraped stand-in until a real source is picked; the pre-draft board has no confirmed quantitative input until this is resolved.

**Real, live-verified finding that changes what "pre-draft mode" can show, surfaced before any ranking code got written:** `draft_picks` and `combine` are post-draft-only by definition, checked live, not assumed. `draft_picks` already carries the real, already-drafted 2026 class (spot-checked: Carnell Tate, pick 4, Ohio St.) but nothing for 2027, because that draft has not happened. Same for `combine` (season range tops out at 2026; the 2027 combine runs next February). This league's inaugural startup already covered veterans, so its next real rookie draft is the 2027 class, meaning real draft capital, landing spot, and athletic testing for the players who actually matter to that draft do not exist yet, full stop, not a data-source problem to solve, a real calendar constraint.

Combined with the CFBD rejection above, this leaves pre-draft mode with no confirmed real, quantitative input at all right now, worth stating plainly rather than glossing over: draft capital/testing don't exist yet for the 2027 class regardless of source, and college production has no source. A second layer, adding early mock-draft consensus as an explicitly labeled sentiment input (never blended into a quantitative score, same bucket as Reddit/beat-writer chatter), is still planned for later, noted in `README.md`.

**Two more real, live-verified findings, the same bug shape as the Sleeper/nflverse `LAR`/`LA` team-code mismatch already found once in `valuation.py`:**
- `draft_picks.team` ships PFR-style codes (`GNB`, `KAN`, `LAR`, `LVR`, `NOR`, `NWE`, `SFO`, `TAM`), not this project's standard nflverse codes (`GB`, `KC`, `LA`, `LV`, `NO`, `NE`, `SF`, `TB`). Confirmed against every distinct code in the live file; the other 24 of 32 already match. Fixed by `prospects.to_nflverse_team_from_draft_code`, a crosswalk in the same style as `valuation.TEAM_ALIASES`, applied at ingest time so `nfl_draft_picks.team` is directly comparable to `weekly_stats.team` with no second lookup at read time. Unit tested in `tests/test_prospects.py`.
- `combine.draft_team` ships as a full franchise name (`"San Francisco 49ers"`), a third format, and its `season`/`draft_year` columns disagree on 8 of 8968 real rows (undrafted combine invitees and a few data gaps). Kept `draft_team` as informational only, not normalized or treated as a second source of truth; `nfl_draft_picks.team`, crosswalked from the same PFR pick, is the one authoritative landing-spot code. `draft_year`, not `season`, is the real draft year.

Also, an existing table in this project is already named `draft_picks` (this league's own Sleeper rookie-draft results). The new nflverse tables are named `nfl_draft_picks` / `nfl_combine`, confirmed the collision before naming anything, not assumed.

### College production: resolved, and tested for whether it's worth having
- [x] Source: sportsdataverse's keyless ESPN college releases (`espn_cfb_player_box`, `espn_cfb_game_rosters`, plus `cfb_team_info` and `cfb_ratings` for team context), chosen by request over CFBD. `dynasty-agent ingest-college --season 2008 --through 2026`. Real gaps found live, stated not smoothed over: ESPN lists every 2008-2013 player at position id 0 (unknown), 2020 is thin (COVID schedules, 146 teams, median 9 games), ESPN's class-year labels are wrong in places (Jeremiah Smith, a 2025 sophomore, listed JR; display only, never a model input), and FCS teams appear only in their games against FBS opponents.
- [x] Player id crosswalk: DynastyProcess `db_playerids.csv` plus nflverse `players.parquet` into `player_ids`, built by `ingest-draft-data`. Birth dates for drafted players 97-100% per class since 2010. ESPN keeps one athlete id from college into the NFL only from about the 2018 class on (0 of ~80 linked per class 2012-2015, 25 of 83 in 2017, 76-85 from 2019), so the training set is 2018-2023 by decision: exact links over name matching.
- [x] Fitted, not hand-set, by decision: `dynasty-agent fit-prospect-model`, ridge regression, target = league-scored points per game scheduled over the first 3 NFL seasons, scored by leaving one draft class out at a time. 429 of 475 drafted QB/RB/WR/TE used.

**The finding that shaped the board: once draft capital is known, college production adds nothing.** Held-out, draft capital plus position alone scored MAE 2.68 PPG / R^2 0.443; adding dominator rating, breakout age, draft age, athleticism, conference, and team strength scored 2.70 / 0.440. Split by position it was no better: slightly worse for RB, WR, and QB, a small TE gain (0.36 to 0.41) on only 85 players, and +0.01 R^2 for picks after round 1. This league's rookie draft runs after the NFL draft, so the post-draft board ranks on draft capital and shows college inputs as context only. Consistent with published research: NFL teams already price college production into where they draft.

Before the NFL draft, college production is the only signal, kept by request as a labeled-weak first read on a class for a better experience before draft order exists. Two real problems found on live runs and fixed: 1-game FCS samples with 85% dominator ratings swamped the first board (a season now needs 6+ team games and 4+ player games to count), then the board ranked San Jose State receivers above Jeremiah Smith (17th) because dominator rating can't see competition level; adding a power-conference flag and opponent-adjusted team strength moved held-out R^2 from 0.126 to 0.169 (with a real birth date) and 0.070 to 0.125 (without one, 1,840 of 1,911 eligible prospects), and put Smith first. Unrated (mostly FCS) teams sit outside the training data and are left off rather than guessed. Running backs stay under-ranked pre-draft, dominator measures receiving only; stated on the board.

### Build order
1. [x] `draft_picks` and `combine` ingestion: `src/dynasty_agent/prospects.py`, migration `0003_prospects.sql`, `dynasty-agent ingest-draft-data`. Verified live: 12,927 real draft picks and 7,434 combine rows landed (7,437 raw rows processed; 3 real duplicate `(season, pfr_id)` keys exist in nflverse's own combine file, a data quality quirk on their end, not an ingestion bug, documented in `ingest_combine`'s docstring). Team normalization spot-checked (a real 2024 49ers pick reads back as `SF`, not `SFO`).
2. [x] College production ingestion, dominator rating, breakout age (exact birth date only, by decision; "never broke out" kept separate from "unknown"), speed score, and a position-relative athletic score. See above.
3. [x] Prospect board: `dynasty-agent prospect-board --mode post-draft|pre-draft --class <year>`. Post-draft verified live on the real 2026 class: Love, Tate, Tyson at the top, in line with FantasyCalc.
4. [x] Position weighting to match league scoring: the board ranks on `metrics.production_score` (QB x0.70, WR x1.05), the same weighting `valuate` uses; Mendoza drops from 1st on raw PPG to 4th.
5. [x] Taxi and IR planning: `dynasty-agent taxi`. Every rule from the league's own settings (taxi_slots, taxi_years, taxi_allow_vets, taxi_deadline, reserve_slots, the reserve_allow_* flags). Moves: IR-eligible players outside this week's best lineup to open IR slots, then taxi-eligible ones to open taxi slots, most long-term value first; next season's crunch counts carryover players, graduating taxi players, and next-draft picks against active plus taxi room (IR not counted, it's temporary), naming the lowest three-year-value non-rookie non-starters as cut candidates. Two real bugs caught on the first live run and fixed: an IR rookie was counted as a starter (the lineup check now skips IR/Out/Suspended players), and the lineup check used win-now value, whose age curve kept a 4.4 PPG rookie "active" over a 29-year-old RB averaging 11.7 (this week's lineup now picks by projected points). Live: the user's roster was 18 of 18 active with 3 empty taxi slots and an open IR slot; the plan moves Jonah Coleman to IR and Emmett Johnson and Demond Claiborne to taxi, freeing 3 bench spots, and next season fits (21 players, 21 spots).
6. [x] Pick advice: `dynasty-agent picks` (`--all` for every team). Inventory from Sleeper's `traded_picks` (only traded picks are listed; the rest belong to their original roster), next three drafts. The next draft's slots are projected reverse standings: best-lineup win-now strength blended toward real record by games played (14% two games in). Advice is market against market: FantasyCalc's price for the projected tier ("2027 1st (Mid)") against FantasyCalc's current value for the 2026 rookies taken at that same slot (ranked by the draft-capital board, slots either side pooled); over 1.15x sell, under 0.87x buy (round, stated bands). Each slot's history since 2018 (league-weighted PPG over 3 seasons, rate of reaching 10+, a weekly flex starter) shown alongside. Live, the user's picks (projected 6th in each round): 2027 1.06 priced 3,040 against a 2,268 median for Makai Lemon, KC Concepcion, and Kenyon Sadiq, SELL at 1.34x; 2.06 HOLD at 1.06x; 3.06 priced 1,012 against 173, SELL at 5.85x, with that slot's rookies averaging 3.5 PPG and 0% reaching flex-starter level since 2018. League-wide, only 1.01 holds (0.97x against Love-tier rookies). This matches a known dynasty-market pattern, unknown picks price above the players they become, and CLAUDE.md's own note that late picks are near-worthless in a 3-round draft.
7. [ ] Later, not yet scoped: the labeled mock-draft-consensus layer described above.
8. [x] Acceptance test, see below.

### Acceptance test: passed
A ranked prospect board for the next rookie draft, with a recommendation on any picks currently held.
- Board: `prospect-board --mode post-draft --class 2026` against FantasyCalc's current values for the same 61 rookies: Spearman rank correlation 0.814; 10 of the board's top 12 are in the market's top 13 (Love, Tate, Tyson at the top of both). The biggest disagreement is on the user's own roster: Emmett Johnson, board rank 36 (pick 161), market rank 11, the market pricing him well above what his draft slot has historically returned, a sell-high signal. For 2027, `--mode pre-draft` gives the labeled-weak first read until that draft happens, then `--mode post-draft`.
- Picks: `dynasty-agent picks` gives a projected slot, that slot's real history, FantasyCalc's price, and buy/hold/sell for every pick held (live: 2027 1.06 SELL, 2.06 HOLD, 3.06 SELL).
- Taxi: `dynasty-agent taxi` (live: 3 bench spots freed).

**Phase 4 status: done**, pending the user's approval per the phase rule. Later, not scoped: the labeled mock-draft consensus sentiment layer.

## Phase 5: talk to the agent (preview built on branch `phase5-chat-preview`, 2026-09-29)

Goal: ask "should I trade Coleman and a 2028 2nd for a 2027 1st?" or "who do I start this week?" in plain English, answered by an open-source model running locally on the MacBook Air M4 (16GB), instead of typing `uv run` commands.

### Decisions confirmed with the user
- Runtime: Ollama, chosen as the easiest to understand and build with (`ollama pull qwen3:4b`; `ollama run` to try the bare model by hand; a plain local HTTP API the existing `httpx` reaches, no new Python dependency).
- Interface: terminal chat first (`dynasty-agent chat`); a local web page can come later on the same core.

### Core design: the model routes and explains, Python computes every number
A ~4B model is fine at understanding a question and phrasing an answer, unreliable at arithmetic and recall. It never computes or remembers a stat: it picks a tool, the existing deterministic function runs, and it explains the returned numbers. That keeps this file's rules true in chat: every recommendation shows its inputs, no projection that can't trace to the database.

Tools, thin wrappers over functions that already return dicts, flat arguments, a small set so a 4B model routes reliably: valuate / my roster, evaluate_trade, set_lineup (optimize_lineup), faab_bid and waiver_targets, predict_matchup, game_conditions (weather and schedule), prospect_board, picks, taxi. Player names stay resolved by `valuation.resolve_player`; an ambiguous name ("Justin Jefferson" matches a WR and an LB) comes back as a clarifying question, never a guess.

### Step 0, runtime verified before any project code (2026-09-28)
Ollama 0.34.4 installed and serving on localhost:11434, 336 GB free, the MacBook Air M4 runs a 4B model 100% on GPU at 3.9 GB.

Model candidates, tested raw against Ollama's API with the same tool definitions and questions:
- `qwen3:4b` (the reasoning model, 4.0B, Q4_K_M, reports tools support): routed both test questions correctly, however took a median ~50s per turn warm. Diagnosed, not guessed: 33 tokens/s generation, a normal speed, spent on ~400 tokens of reasoning before every tool call, even with Ollama's `think: false` (the reasoning moved into the visible answer instead of the thinking channel). Qwen's `/no_think` switch cut it only to 264 tokens. Also invented a fact it couldn't know (asked for week 1 in week 4).
- `qwen3:4b-instruct` (same size, non-reasoning): median 1.6s per turn warm (0.6-1.9s; 18.5s once to load into memory), ~35 tokens generated, routed both questions correctly, invented nothing. The working choice; the bake-off in step 6 still decides it on a full question set.

**Full bake-off, the same day** (`benchmarks/model_bench.py`: 16 real questions scored on tool and arguments, 3 answer tests scored on quoting the tool's real numbers without inventing any, writing speed), prompted by a goal of ~60 tokens/s. Generation is memory-bandwidth bound, so the levers were a smaller dense model, a mixture-of-experts model with ~1B active parameters, or lower-bit quantization (ruled out, quality cost):

| Model | Tok/s fresh | Routing | Answers | Outcome |
|---|---|---|---|---|
| `qwen3:4b-instruct` | ~35 | 16/16 every run | 2/3 | chosen: never misrouted, never invented a number |
| `gemma4:e2b` | ~50 | 15/16 | 1/3 | reversed "I'm offered X for Y" trades every run |
| `qwen3.5:4b` | ~22 | 15/16 | 3/3 | best answers, 7.4s to pick a tool |
| `qwen3.5:2b` (8-bit) | ~42 | 14/16 | 0/3 | listed draft picks as players |
| `qwen3.5:2b-q4_K_M` | ~44 | - | - | Ollama's template errors on a tool call with no arguments ("XML syntax error"), a real compatibility bug for tools like set_lineup |
| `qwen3:1.7b` | ~68 | 13/16 | 2/3 | swapped trade sides, turned a 2nd into a 1st, looked up Tom Brady for a pick question |
| `granite4:7b-a1b-h` (MoE) | ~50 | 10/16 | 1/3 | flipped trades, invented a "2024 1st" |
| `lfm2.5:8b-a1b` (MoE) | ~68 | 8/16 | 2/3 | reasons in `<think>` tags even with thinking off, ran out of budget before calling a tool |

Conclusion, stated plainly: on a fanless MacBook Air M4, no tested model reaches 60 tokens/s while handling trades correctly; the two that reach it swap trade sides or spend the speed reasoning. Sustained load also throttles the fanless Air (re-timed after ~40 minutes of benchmarks, every model ran 20-45% slower; the chosen model fell from 35 to 19). A model that confidently evaluates the reverse of the user's trade is the worst failure this tool can have, so speed is designed for instead: Python writes the numbers block itself (instant, can't be misquoted), the model adds 1-3 sentences (~60 tokens, ~2s), output streams, the model is loaded while `refresh` runs so no cold start, and the model is a setting so a faster Mac can run a larger one.

Two harness bugs of this project's own, found and fixed before trusting any score: "answer using only the numbers" made models reply with bare numbers, and a placeholder tool call in the grounding test crashed Qwen3.5's template; both reworded to match a real conversation.

Models live in `~/Open Source Models` (the user's choice, outside the repo; `~/.ollama/models` links to it), inventoried in `MODELS.md` there.

Design rules this surfaced, before any code: never ask the model for anything Python already knows (current week, the user's roster, the season), Python fills those in; and accept any reasonable argument format from the model (it wrote picks as "2027-1" in one run and "2027 1st" in the next), the tool layer normalizes.

### User experience target
`git clone`, then `uv run dynasty-agent` with no arguments opens a first-run setup: checks Ollama and downloads the model (saying the size first), asks the Sleeper username and league, loads the data with progress (replacing today's five first-run commands), asks what matters to the user (contending, rebuilding, trades, waivers, rookies), offers the daily refresh, then opens the chat with a short brief tuned to those priorities. Before building it, the user will run a first-time install from GitHub by hand to find where today's onboarding breaks.

### Requirement: live model stats, bottom right (requested 2026-09-29)
While talking to the agent, the user sees the model working, the way LM Studio and Ollama's own app show it: a status bar pinned to the bottom-right of the terminal.

```
                                     qwen3:4b-instruct · 34.8 tok/s · first words 1.2s · tool: evaluate_trade
```

- **Live while it writes**: tokens per second updates as the answer streams (tokens received / seconds since the first one), then settles on Ollama's exact figure when the answer finishes (`eval_count / eval_duration`, reported with every reply, the same source `ollama run --verbose` uses).
- **Also shown**: the model name, time to first words, which tool ran (or "no tool"), and after a reply the reading speed (`prompt_eval_count / prompt_eval_duration`). A one-time model load shows as "loading model..." instead of a misleading slow rate.
- **On by default**; `/stats off` hides it for the session, `DYNASTY_AGENT_STATS=off` in `.env` hides it by default.
- **How**: a terminal can't pin text to a corner with plain printing, it needs a small input layer that owns the bottom line. `prompt_toolkit` (the library behind IPython's prompt) does exactly this with its bottom toolbar, and also gives the chat input history and arrow-key editing. It would be this project's first UI dependency; CLAUDE.md says no framework unless asked, so confirm with the user when step 4 starts. Fallback with no new dependency: a right-aligned stats line printed under each answer, not pinned.
- **Test**: the stats formatter is a pure function (numbers in, status string out), unit tested; the live figure checked against `ollama run --verbose` on the same prompt.

### Step A: audit of everything the chat touches (2026-09-29)
Before building the chat, the user asked for an audit of the code and models it would talk to. Two read-only scans mapped the six tool paths on main and the unmerged `worktree-phase4-college-data` branch. The user chose to fix every finding. Each fix has tests; commits A1 through A12.

Real problems found and fixed:
- **A chat session could be ended by a missing sync.** The CLI helpers raised `SystemExit`. They moved to `context.py` and raise `AgentError`; the CLI still exits 1, the chat shows the message and carries on. `DRAFT_ID` was required yet read by nothing, and `init` writes it empty for a league with no rookie draft yet, which locked such a league out of every command.
- **Names.** `resolve_player` only reported ambiguity inside an error string, treated `%` and `_` as wildcards, and found nothing for "Marvin Harrison Jr." (Sleeper drops suffixes). It now normalizes punctuation and suffixes, returns candidates as data, drops only players at a position the league can't start (the Browns LB named Justin Jefferson, in a league with no IDP) and asks about anything else. A code review caught a first version that also preferred players with an NFL team, which could silently pick the wrong player over a free agent who signed since the last sync.
- **Picks.** The parser took "2027-9" and rejected "2027 1st". `picks.parse_pick` reads the common spellings, "next year's 1st", tiers and slots, checked against the league's rounds, teams and tradable drafts. The trade tool priced every 2027 1st at FantasyCalc's untiered 2834 while `picks` priced the same pick at its tier (1.05 Mid, 3034); both now use `market.pick_price`, and the user's own next-draft pick is priced at its projected tier.
- **Trades** say what can't be right: a sent player not on the user's roster, a received player who is, a free agent, a player on both sides, a pick not held. The consolidation note reads the league's bench and starter counts, and results label their units.
- **Lineups.** An opponent with no lineup set scored 0, giving a ~100% win probability; they're now projected from their best roster lineup. Byes came from which teams had a Vegas line, so a line taken off the board zeroed a playing team and with no lines yet (week 5 today) nobody was on bye; byes come from the schedule. A rookie before game two counted as zero variance, which read as risk-free; the position's median stands in. Any slot type works, and slots the roster can't fill start empty instead of producing no lineup.
- **Waivers.** The week 4 digest listed four QBs and a TE, every one bid at $27: raw value ranked a backup QB in a 1QB league at the top, and percentile scaling saturates at the top of a thin wire. Targets rank by lineup gain (how much each raises the user's best lineup) and bids scale by it; pacing knows the playoffs and the end of the season.
- **Verdict** percentiles counted the user's own team (the best roster capped at the 96th percentile); now against the other 11. **Taxi** ignored its deadline and could name an injured starter as a cut. The **pick report** rebuilt the draft board per pick (`picks --all` 1.48 s to 0.52 s). Valuations are reused while the database is unchanged (six tools back to back: 855 ms to 477 ms cold, 130 ms warm).
- From the old branch, only the Platt calibration math and its tests are ported (A11), used by nothing yet.

Intended changes to CLI output, everything else byte-identical before and after:
- `trade`: a received next-draft pick with no tier says so; the user's own next-draft pick shows its projected slot and tier price.
- `trade`: WARNING lines for impossible trades.
- `optimize-lineup`, `digest`: bench lines say BYE WEEK or the injury status when a player projects to 0; byes from the schedule.
- `valuate`: 38th percentile of 12 teams became 36th against the other 11; the verdict is unchanged.
- `digest`, `faab`: targets by lineup gain (week 4: Trey Benson +3.0 $27, Jake Tonges +1.8 $17, Deshaun Watson depth only $2).
- `taxi`: the next-season line names who graduates from taxi.

### Preview build (steps 1-7, 2026-09-29)
1. [x] `formatters.py`: one text function per result, used by the CLI and the chat. Output byte-identical on the seven snapshot commands and three extra variants, each diffed against the commit before.
2. [x] `llm.py`: an Ollama client over `httpx`, no new dependency. Live: warm 2.1 s, routing 1.8 s, 35.8 tok/s writing, first words 0.10 s.
3. [x] `tools.py`: six tools (`set_lineup`, `evaluate_trade`, `my_team`, `waiver_targets` with an optional player or position, `pick_advice`, `taxi_plan`), none asking for week, season or roster. Lenient arguments; an ambiguous or unknown name or an impossible pick comes back as a question.
4. [x] `dynasty-agent chat`: refresh while the model loads, route, the numbers block, a streamed take, a stats line (model, exact tok/s, first-words time or "loading model...", tool). The pinned bottom-right bar waits on the user's go-ahead for `prompt_toolkit`; the preview prints the line under each answer.
5. [x] Grounding: the take is released a sentence at a time and a sentence quoting a number not in that turn's numbers block, summary or question is never shown.
6. [x] Model audit: `chat_eval.py`, 39 questions on the real tools, trades scored on direction; `dynasty-agent chat-eval` runs it live and `--record` feeds the offline replay test.
7. [x] Docs, this entry, and CLAUDE.md's local-model rule.

Found live and fixed while building, each one a real behavior the model showed:
- Earlier answers in the router's context made it imitate them ("(showed the my_team result)") instead of calling tools. Each question is now routed on its own, the way the bake-off scored 16/16; only a clarifying exchange carries forward, so "the RB" can answer "Which Kenneth Walker?".
- Takes quoted JSON field names and rounded 16.5 to 16 (the grounding check dropped them). The prompt now asks for numbers copied character for character and a headline-first answer, and each summary carries a plain headline.
- The model added a WR filter nobody asked for, and filled an optional argument with "none".
- **Trade reversals, the worst failure.** The first live `chat-eval` scored 36/39, with two reversals: "a guy offered me his 2028 1st for Rome Odunze" and "my 2027 round 1 pick for Trey Benson". Fixed in Python rather than by trusting the model: `tools.fix_sides` moves a player the rosters prove is on the wrong side (a "received" player the user owns, a "sent" player another team owns), then the picks if one side is left empty, and says so in the output. Free agents and ambiguous names are never moved. A first version swapped whole trades, which would have sent the user's 2028 1st too when the model put both assets on one side.

Verified live on the real league (week 4, 2026):
- Acceptance: "Who should I start this week?", "Should I trade Jonah Coleman and my 2028 2nd for a 2027 1st?" (net -221, the CLI's number), "Am I a contender?", "Who should I pick up?", "Should I sell my first?", "Anyone I should put on taxi?" each answer from the matching command's own numbers. "Trade Kenneth Walker for Puka Nacua?" asks which Kenneth Walker (acceptance item 7 changed: "Justin Jefferson" now resolves to the Vikings WR, correctly, since the other is an LB the league can't start). "What's the capital of France?" gets a short reply, no tool, no numbers.
- `chat-eval`: 39/39 after the fixes, 1-2 s per route. Writing speed 27-33 tok/s in session, first words 0.5-1.8 s.

After the preview (recorded, not built):
- The pinned bottom-right stats bar (`prompt_toolkit`, first UI dependency: needs the user's go-ahead).
- Port `simulate.py` from the old branch as a seventh tool ("what are my playoff odds?").
- Re-derive calibration on this branch's blended projections before any win probability drops its draft label.
- A one-line installer rewritten for Ollama (the old branch's LM Studio script has a `curl | bash` stdin bug, no `pipefail` and a PATH gap).
- The first-run setup wizard, shaped by the user's clean-install test.

### Conversation and explaining the math (branch `phase5-chat-explain`, 2026-09-29)
The user tried the preview: it works but lacks conversation, and it can't say how it gets a number. A 4B model asked to explain methodology from memory would invent it, and the grounding check catches invented numbers, not invented reasoning. So Python writes the explanations and the model only retells them.

Decisions confirmed with the user: step-by-step explanations with their own numbers; keep `qwen3:4b-instruct` and change its behavior (no fine-tuning); remember the last 3 answers. Built in phases with a stop after each: A player values and a glossary, B trades and lineup win probability, C FAAB bids, the verdict and pick advice.

**Phase A (done).**
- `explain.py`: `explain_player_value` builds the real chain (blended points per game, then position weight, age factor, situation factor, win-now, three-year) from the live constants in `metrics.py` and `blend.py`, never copies of them. Every chain is checked against `player_valuations` before it is returned; a mismatch raises `ExplanationMismatch` and nothing is shown. A glossary of 13 terms (win-now, 3yr, market value, situation score, FPPG, variance, win probability, arbitrage, discount rate, posture, age, taxi, FAAB) is written in Python, the age entry reading the live `AGE_CURVES`.
- Output: Python prints the steps block, the model retells it in up to six sentences, and a fixed hint follows. A definition is Python's own text with no model take. The take may quote numbers from the last 3 answers, so "why?" can refer to them; an invented number is still dropped.
- **A seventh tool did not work, and the numbers show it.** Offering `explain` to the model dropped its routing of the original 39 questions from 39/39 to 36/39 (an A/B run: 39/39 with the tool removed, 36/39 with it), and it still missed explain questions ("What is arbitrage?", a bare "Why?"). Three rewrites of the tool's description and the router prompt each left the same three original questions wrong. So the model never sees `explain`: `explain.detect` recognizes the question in Python (narrow on purpose: "How do I get Puka Nacua?", "What's a good FAAB bid for Justin Fields?" and all 39 bake-off questions are tested never to match) and `explain.find_player` reads the player's name out of the question. A phrasing it misses falls through to the six-tool router and gets an ordinary answer.
- Memory: the router still sees each question alone (earlier answers made the model imitate them, and a line naming earlier tools let names leak into a new question in the A/B run). Python keeps the last 3 results for explanations, and the answer to "Whose number?" goes back to `explain`.
- Found live: a 160-token cap cut a retelling mid-sentence (explain turns now get 420); "Why is his 3yr lower?" returned the definition instead of the player's chain (`wants_math` now counts why, where and explain); the model once wrote "x -1.03" (the prompt now forbids a minus the steps don't show).
- Verified live on the real league: Trey Benson and Jonah Coleman chains, "why is his 3yr lower", "why does age matter for running backs", "what is arbitrage", and a trade followed by "why?".

**Phases B and C (done, same day).** Every explanation is built from the result the user was shown (or the live database) and its last step is recomputed and must equal the number shown, or `ExplanationMismatch` is raised and nothing is displayed.
- Lineup and win probability (`explain_lineup`): each starter's projected points with the injury and Vegas factors that moved them, the summed variance, the opponent's side, the gap, the combined spread, and the normal-curve step, with the independence and draft-heuristic limits stated. A bye or a borrowed variance is disclosed.
- Trade (`explain_trade`): each pick's price (the nearest draft priced directly, a later one as base x (1 - 20%)^years, with the gap to FantasyCalc's own price), each side's market total, the net, the players-only win-now and three-year nets, the fit rule that applied to the user's posture, and the roster-shape note. Two scales are never added, and unpriced assets are named as counting 0.
- FAAB bid (`explain_faab`): weekly pace, lineup gain against the best upgrade on the wire, the multiplier, and the bid from the unrounded product (a live test showed the model rounding it wrong when only the rounded bid was given).
- Contend, rebuild or unclear (`explain_verdict`): the two percentiles against the other teams, the stated bands, and the confidence.
- Pick buy, hold or sell (`explain_pick`): projected slot, FantasyCalc's tiered price, what that slot bought last year, the ratio and the bands. Which pick is a question when several are slotted.
- "Why?" after an answer explains that answer (trade, lineup, bid, verdict, pick), and after a player explanation it explains the same player. Taxi and IR advice has no numbers to explain and is not covered.
- Found live: the verdict step printed "82th"; "Why is that a sell?" was not recognized; both fixed and tested. The `valuate` command still prints "82th percentile" (unchanged, outside this work).
- Verified live on the real league: a trade then "why?", a lineup then "how is that win probability calculated?", a bid, the verdict and a pick sale, each retold in plain language with every number in the steps.

### Stated limits
- Sleeper's API is read-only: the agent recommends a lineup, bid, trade, or taxi move; the user makes it in the Sleeper app.
- A small model will sometimes misroute; the grounding check and eval set measure and contain that, not claim it never happens.

## Staying current in season

Found by asking "will this stay up to date?": it didn't. Nothing ran on its own, the 2026 season's stats sat un-ingested three weeks in, and ingesting them would have switched every value from 17 games of 2025 to 3 noisy games of 2026 in one step.

- [x] **Blend, don't switch.** Every per-game average (valuations, matchups, lineups) starts from last season's and this season's real games add on top; team situation scores blend the same way by games played. A rookie starts from his draft-capital projection instead. How much the prior counts was chosen by backtest (`calibrate-blend`, `blend.py`): blend the prior with the first 1-8 weeks, score against the rest of the season. Veterans, 2024 into 2025, 1,231 players: best at 4 games (MAE 0.883 PPG against 1.082 for this season alone; flat from 3 to 6). Rookies, the 2025 class, outside the model's training range, 71 players: best at 3 games (3.032 against 3.454). One season pair each, rerun as more accumulate. Verified live on 2026 through week 3: CeeDee Lamb 19.0 PPG and Rashee Rice 16.1, blending 3 real games into last season; the user's rookies moving off their projections (Emmett Johnson 4.4 after 3 games).
- [x] **`refresh`**: one command that knows the calendar (see `refresh.py`), each step independent so one failure doesn't stop the rest, ending in a freshness report. The prospect model's training window now moves forward on its own (2018 through the newest class with 3 completed seasons) and refits when it does. 3-4 seconds on a live run.
- [x] **`schedule`**: a daily `refresh` through launchd, verified by triggering the installed job through launchd itself (its stripped-down environment, not a terminal): 5 of 5 steps ok. Windows gets the `schtasks` equivalent in `WINDOWS.md`, written from Microsoft's documented syntax, not yet run on Windows.
- [x] **Real bug found on the way**: `ingest-nflverse` crashed for every season before 2025. nflverse's depth chart format changed in 2025 (dated snapshots, `pos_abb`); older seasons are week-numbered (`club_code`, `depth_team`). Both handled now, 2024 verified.
- [x] **Found live, reported to the user**: Jonah Coleman is on IR in Sleeper as of week 4.

## Out-of-band: ad hoc matchup prediction — DRAFT

Not part of the phase plan, added on request: `dynasty-agent predict-matchup` (`src/dynasty_agent/matchup.py`) estimates win probability between any two arbitrary rosters, not necessarily this league or even a dynasty league. Grew out of manually answering a one-off "who wins this weekend" question by hand.

**Status: draft, explicitly.** This is the first pass, not the finished thing. Marked as such in the module docstring, the CLI help text, the CLI's own output every time it runs, and the README, on purpose, so "draft" stays visible wherever someone actually runs into it, not just here.

**Planned next**, once there's a real dataset to build against: a proper prediction mode, calibrated against actual game outcomes rather than the current uncalibrated normal-CDF heuristic, and a trade bot built on top of that prediction mode, presumably using it to source or evaluate trade targets automatically rather than requiring a manually specified `dynasty-agent trade` call. Neither is scoped yet, no open questions or build order below, that comes when work on it actually starts.

Explicitly a heuristic, not a fitted model:
- Each player's mean and *sample* variance (Bessel's correction: divide by n-1, not n) come from their real weekly `fantasy_points` in a completed nflverse season, undefined (`None`, not a silently-wrong 0.0) for fewer than 2 games.
- The mean gets discounted by current injury status (`metrics.INJURY_MEAN_MULTIPLIER`); variance gets widened for Questionable/Doubtful (closer to bimodal, full workload or scratched, than a healthy player's normal swing) and collapsed for Out/IR/Suspended (`metrics.INJURY_VARIANCE_MULTIPLIER`). Round, labeled numbers, not fitted from outcomes.
- The mean also gets scaled by a real, specific week's Vegas-implied team total versus that team's own season norm (`metrics.vegas_week_multiplier`), sourced from nflverse's free, unauthenticated schedules file (`spread_line`/`total_line`), not a paid odds API and not a hardcoded per-team bias. Neutral (1.0) when there's no line yet or no season-baseline yet (week 1).
- Win probability is the normal CDF of the projected margin over its combined standard deviation, the same idea as Vegas spread-to-moneyline conversion, simplified to two independent team totals.
- The independence assumption (players' scores don't correlate with their teammates') is real and stated, not hidden. Nothing here has been checked against actual game outcomes, this project has no historical win/loss dataset to check it against.

**Real bugs found and fixed while building this, not just theoretical caveats:**
- Population variance (divide by n) instead of sample variance (divide by n-1) understated variance, worst for thin-sample players, exactly where overconfidence matters most. Fixed via `metrics.sample_mean_variance`.
- Injury status only touched the mean, not the shape of the outcome. Fixed via `metrics.injury_adjusted_variance`.
- An earlier version of the Vegas integration tried to infer which season's lines to use from whether the FPPG baseline season/week had any data, falling back to `season + 1` only when empty. That's wrong in exactly the case that matters most: week 1 of a completed season already has real *closing* lines from last year's game, so the fallback never fired, and it silently priced last year's matchups as if they were this week's. Fixed by resolving the Vegas season from the actual current NFL season (`nfl_state`, from the last `sync`) instead of guessing from data presence, `vegas_season` is now a fully separate parameter from `season`.
- Sleeper and nflverse disagree on the Rams' team code (`LAR` vs `LA`), a mismatch already found and fixed in `valuation.py` for the situation score; missing it here meant every Rams player read as on a bye every single week. Fixed by reusing `valuation.to_nflverse_team` instead of duplicating the fix.

Verified against a real matchup (7-a-side, PHI/BAL/LAR-heavy roster vs. a WAS/BUF/NYJ-heavy one), tracking the math as it improved: 67.8% on the original mean-only, population-variance version; 65.2% after the sample-variance and injury-variance fixes (same direction on both, less falsely confident, not a swing toward either side); confirmed the LAR/vegas_season fix directly (a prior run had wrongly shown both Rams players as on a week 1 bye, and had wrongly priced 2025's already-played week 1 instead of 2026's real upcoming week 1). Verified the Vegas multiplier engages correctly on a mid-season week with a real baseline (`--vegas-season 2025 --week 10`, multipliers 0.88-1.06, plausible). 49 tests passing.

Uses this league's own `weekly_stats.fantasy_points` (real for QB/RB/WR/TE, not scored at all for K/DST, this project's nflverse ingestion never covered kicking or defense). A kicker or defense in a matchup comes back flagged "NO DATA," not silently folded in as a zero. A bye-week player comes back flagged "BYE WEEK," counted as a hard 0 for both mean and variance, not silently averaged in as if they were playing.

**Nonpartisan, by request and by construction.** No team- or player-identity lookup table exists anywhere in `matchup.py` or the metrics it calls. Every number a team gets comes from that team's own real data (its players' actual weekly production, its own current Vegas line, its own players' actual injury designations), the same functions run for both sides of every matchup. This was a deliberate rejection of a pattern found in the third-party repo reviewed above, which hardcoded a specific list of "new system" (penalized) and "stable, high-powered" (boosted) teams straight into its scoring, favoritism baked into the math itself. Nothing like that exists here, and it should stay that way in anything built on top of this.
