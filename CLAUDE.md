# Dynasty Agent

A dynasty fantasy football agent for a single Sleeper league. It evaluates trades in-season, sets weekly lineups, calls contend-or-rebuild, and preps rookie-only drafts. It ships as a CLI tool.

## Environment

- MacBook Air M4, 16GB RAM, macOS.
- Python 3.12, managed with `uv`. Run everything through `uv run`, add dependencies with `uv add`.
- SQLite for persistent storage. DuckDB for analytical queries over play-by-play data.
- No GPU work and no local models. Every model call hits an API.
- Memory stays modest. nflverse play-by-play files are large. Read them with DuckDB or pyarrow, never load a full file into a pandas DataFrame.

## League

- Sleeper username: `bigstott`.
- user_id: `1399550449018802176`.
- league_id: `1397608187129016320`.
- draft_id: `1397608188068507648`.
- "Dynasty", 12 teams, dynasty type, inaugural season (`previous_league_id` is null).

**Roster:** QB, RB, RB, WR, WR, WR, TE, FLEX. Eight starters, ten bench slots, 3 taxi slots (rookies only, one year), one IR slot. No kicker and no defense slot. The K and DST entries in `scoring_settings` are unused defaults and get ignored entirely.

**Scoring:** Full PPR, 1.0 per reception. 4 points per passing touchdown, 0.04 per passing yard. 0.1 per rushing and receiving yard, 6 per rushing and receiving touchdown. -1 per interception, -2 per fumble lost. No TE premium.

**Rules:** 6 playoff teams, playoffs start week 15. Trade deadline week 9. Pick trading enabled. Trade review runs 2 days and needs 6 veto votes. FAAB waivers, $100 budget, clears Wednesdays. Rookie draft is 3 rounds.

## What the settings imply

1. This is 1QB with 4-point passing touchdowns. Twelve starting quarterbacks across twelve teams makes the position nearly replaceable. Never value a quarterback like a superflex asset. Rushing quarterbacks still separate, however the agent should acquire them cheaply, not trade real assets for them.
2. Wide receiver wins this league. Three starting receivers plus a flex in full PPR makes target share and yards per route run the highest-signal metrics in the model.
3. Full PPR inflates pass-catching running backs. Weight receiving work heavily over pure early-down volume.
4. No TE premium. Tight end is a one-slot position with a low replacement level. Do not overpay.
5. Rosters run deep. Ten bench plus 3 taxi across 12 teams leaves the waiver wire thin. Roster construction and consolidation matter more than streaming.
6. The rookie draft runs three rounds. Only 36 rookies come off the board. Hit rate on picks 1 through 12 matters enormously, and late picks are near-worthless. That shapes how future picks get valued in trades.
7. The trade deadline lands in week 9. That is early. The contend-or-rebuild call has to be made by roughly week 6 or 7, on a small sample, in an inaugural season with no prior-year league data.
8. Playoffs run weeks 15 through 17. Weight those weeks when evaluating schedule.

## Phase plan

Work in phases. Do not skip ahead. Stop at the end of each phase, show what got built, and wait for approval before continuing.

- **Phase 0, verify and orient.** Confirm league state against the Sleeper API. Report roster, record, and pick inventory in plain language before writing feature code.
- **Phase 1, data layer only.** Sleeper client (`src/sleeper.py`), nflverse ingestion (`src/nflverse.py`), market values from FantasyCalc (`src/market.py`), and a normalized SQLite schema written as a migration file. No rankings or projections yet. Acceptance test: print the current roster with age, position, team, market value, and 30-day value trend.
- **Phase 2, analysis layer.** Player valuation (production score, dynasty age curve, situation score, separate win-now and three-year values), contend-or-rebuild verdict with stated confidence, and trade evaluation with pick-value discounting and consolidation flags.
- **Phase 3, weekly workflow.** Vegas implied totals, opponent strength by EPA allowed, injury and practice-report checks, weather, a win-probability lineup optimizer, FAAB sizing, and a weekly digest command.
- **Phase 4, rookie draft prep.** A prospect board (draft capital, landing spot, age, college dominator rating, breakout age, athletic testing), pre-draft and post-draft weighting modes, taxi-slot modeling, and pick-value cross-referencing.

## Working rules

- At the start of every session, including a resumed one, run `dynasty-agent sync` before anything else. Nothing here gets reasoned about from a stale cache.
- Ask before assuming. If a data source or intent is unclear, ask rather than guess.
- Every recommendation shows its inputs. The goal is an auditable chain of reasoning, not a bare verdict.
- State what is unknown. Snap share and route participation are partly paywalled at PFF and Fantasy Points. An estimate built from public data gets labeled as an estimate.
- Never write a projection that cannot trace back to a source in the database.
- Treat Reddit and beat writer commentary as sentiment and market signal, not as fact. Prefer r/DynastyFF over r/fantasyfootball.
- Prefer boring, readable code. No premature abstraction. No framework unless asked for one.
- Write tests for the metric calculations. A wrong WOPR or weighted-opportunity number breaks everything downstream, and it needs to fail loudly.

## Writing style

No em dashes. No Oxford commas. Active voice. Declarative sentences. Specifics over adjectives. Use "however" as the pivot word.
