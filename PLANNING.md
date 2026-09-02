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

### Open questions to confirm first
- [ ] Odds API. CLAUDE.md's own Phase 3 spec says ask before wiring one up. No provider chosen yet.
- [ ] Injury and practice-report source. Spec calls for web search against official reports and beat writers, checked Wednesday, Friday, and Sunday morning. Needs confirmation on whether this runs on demand or on a schedule.
- [ ] Weather source, not yet chosen, needed for the 15 mph wind flag.

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
