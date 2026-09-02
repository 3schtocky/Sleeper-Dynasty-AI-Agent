# Phase planning

Working checklist for initiating Phases 2 through 4. Phase 0 and Phase 1 are done and verified, see CLAUDE.md for the summary. Each phase below gets its open questions confirmed before code gets written, the same discipline Phase 1 used for the nflverse release names and the FantasyCalc response shape.

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

### Open questions to confirm first
- [x] Odds API. Resolved for free: nflverse's own schedules file (`spread_line`/`total_line`/moneylines`, no key, no paid provider) is already wired up and tested in `matchup.py` (`team_week_implied_points`, `team_season_avg_implied_points`). Reuse that, don't source a second provider.
- [ ] Injury and practice-report source. Spec calls for web search against official reports and beat writers, checked Wednesday, Friday, and Sunday morning. Needs confirmation on whether this runs on demand or on a schedule. Given the quantitative-first constraint above, the structured Sleeper `injury_status` field (already live, already the exact signal the matchup-prediction draft uses) is the primary quantitative input; web-searched practice-report color, if built at all, is a secondary, explicitly-labeled overlay, not a replacement for it.
- [ ] Weather source, not yet chosen, needed for the 15 mph wind flag. `api.weather.gov` (NWS, US government, free, no key) confirmed live and reachable, matches this project's no-auth-provider pattern (Sleeper, FantasyCalc, nflverse). Needs a stadium lat/lon lookup table (genuinely non-derivable, not in any existing data source) and explicit dome/retractable-roof handling, wind doesn't apply indoors.

### Build order
1. [ ] Vegas implied team totals and spreads, once a provider is confirmed.
2. [ ] Opponent strength by position from EPA allowed per play, derivable from Phase 1's play-by-play ingestion, not raw fantasy points allowed.
3. [ ] Injury and practice participation checks.
4. [ ] Weather, flagging wind above 15 mph.
5. [ ] Lineup optimizer maximizing win probability against that week's specific opponent, not raw projected points.
6. [ ] FAAB bid sizing against remaining budget and weeks left.
7. [ ] Weekly digest command tying all of the above into one readable brief.

### Acceptance test
`dynasty-agent digest` for a real week, producing a lineup recommendation and FAAB suggestions with inputs shown.

## Phase 4: rookie draft prep

**Same constraint as Phase 3: quantitative first.** The prospect board ranks on quantifiable inputs, draft capital, college production metrics (dominator rating), breakout age, athletic testing, with stated weights per position matching this league's actual scoring, not subjective scouting takes. Where a number can be sourced and computed, it gets computed; sentiment-only inputs stay explicitly labeled as such and never substitute for a real underlying stat, the same standard Phase 2 already set with win-now/three-year value and the trade evaluator's arbitrage math.

### Open questions to confirm first
- [ ] Prospect data source. Draft capital, landing spot, college dominator rating, breakout age, and athletic testing are not in nflverse's player-week files. Needs a source before the prospect board can be built.
- [ ] Pre-draft vs. post-draft mode switch. Confirm whether this is a CLI flag or an automatic switch keyed off the NFL draft date.

### Build order
1. [ ] Prospect board ingestion from whatever source gets confirmed.
2. [ ] Two weighting modes, pre-NFL-draft and post-NFL-draft.
3. [ ] Position weighting to match league scoring, receivers up, quarterbacks down.
4. [ ] Taxi-slot modeling against the 3 available slots.
5. [ ] Cross-reference against rookie pick market values for buy, hold, or sell-the-pick guidance.

### Acceptance test
A ranked prospect board for the next rookie draft, with a recommendation on any picks currently held.

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
