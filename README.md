# Dynasty Agent

A CLI tool that manages a dynasty fantasy football team on [Sleeper](https://sleeper.com). It pulls your real roster and league settings from the Sleeper API, layers in play-by-play and weekly stats from [nflverse](https://github.com/nflverse), and prices players and picks with dynasty market data from [FantasyCalc](https://fantasycalc.com), to answer three questions: what's my roster worth, am I contending or rebuilding, and is this trade good.

It is built for **one specific league shape**: 12 teams, 1QB, full PPR, no TE premium, a 3-round rookie-only startup/rookie draft. It will run against a differently-shaped Sleeper dynasty league, since your real league settings and scoring get pulled live, however some of the tuning (the age curve, the QB-discount and WR-bump multipliers, the situation score weights) was calibrated for that shape specifically. Read `CLAUDE.md` for the full reasoning behind those numbers before trusting them blindly in, say, a superflex or half-PPR league.

## What it does today

- **`sync`** — pulls your league, all rosters, users, traded picks, current NFL week, and FantasyCalc dynasty values.
- **`roster`** — your current roster with age, team, market value, and 30-day value trend.
- **`ingest-nflverse`** — derives per-player-per-week target share, air yards share, WOPR, weighted opportunity, red zone touches, team pass rate over expected, and fantasy points computed from *your* league's actual scoring settings, not a generic PPR assumption.
- **`valuate`** — win-now value and three-year value per player (kept separate, never blended into one number), plus a contend-or-rebuild verdict with its confidence stated, not implied.
- **`trade`** — evaluates a proposed trade: both sides on win-now and three-year value, future picks discounted by a tunable rate and checked against FantasyCalc for arbitrage, and consolidation opportunities flagged.
- **`predict-matchup`** *(draft)* — win probability between any two rosters, not necessarily your own league or even a dynasty league. A first-draft heuristic, not a fitted model, on the way to a proper prediction mode with a trade bot built on top of it. See "How the numbers work" below for exactly what that means.

What it doesn't do yet: weekly lineup optimization for your own team, Vegas/weather-aware start-sit advice, waiver FAAB sizing, and rookie draft prep. See `PLANNING.md` for where those stand.

## Requirements

- Python 3.12, managed by [`uv`](https://docs.astral.sh/uv/). If you don't have `uv`:
  ```
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- A Sleeper account that's a member of at least one dynasty league. You don't need an API key, Sleeper's read API is public.

## Install

```
git clone https://github.com/3schtocky/Sleeper-Dynasty-AI-Agent.git
cd Sleeper-Dynasty-AI-Agent
uv sync
```

`uv sync` creates a `.venv`, installs Python 3.12 if you don't already have it, and installs every dependency pinned in `uv.lock`.

## Point it at your own league

Nothing about your Sleeper account or league is hardcoded. Run:

```
uv run dynasty-agent init --username <your-sleeper-username>
```

This looks up your Sleeper user ID, finds your current-season dynasty leagues, and writes a `.env` file (already in `.gitignore`, it never gets committed) with your username, user ID, league ID, and draft ID. If you're in more than one league this season, it lists them and asks you to re-run with `--league-id <id>` to pick one.

Prefer to do it by hand, or your league is for a season Sleeper doesn't return by default? Copy `.env.example` to `.env` and fill in the four values yourself. Your league ID and draft ID are both in your league's Sleeper URL and in the response from `https://api.sleeper.app/v1/user/<your-username>`.

If your league isn't 12-team/1QB/full-PPR, also set the `FANTASYCALC_NUM_QBS` / `FANTASYCALC_NUM_TEAMS` / `FANTASYCALC_PPR` variables shown (commented out) in `.env.example`, so the FantasyCalc market values you pull actually match your format.

## Run it

```
uv run dynasty-agent sync                          # your league, rosters, and market values
uv run dynasty-agent roster                         # sanity check: is this your team?
uv run dynasty-agent ingest-nflverse --season 2025  # the most recently completed NFL season
uv run dynasty-agent valuate                        # win-now/three-year value + the verdict
```

Evaluate a trade, players by name (fuzzy-matched) or Sleeper `player_id`, picks as `<season>-<round>`:

```
uv run dynasty-agent trade \
  --send "Rashee Rice" \
  --receive-pick 2027-1 --receive-pick 2027-3 \
  --discount-rate 0.20
```

`trade` reports three numbers per side, deliberately never summed together: **win-now** and **three-year** (this league's own formula, players only, a pick can't help you win this year so it doesn't appear there), and **market value** (FantasyCalc's real pricing for players plus a discount-adjusted model value for picks, the one number that's actually comparable across a player and a pick in the same trade).

Estimate a matchup for a specific week, any two rosters, kicker and defense included if you want, players by name or `player_id`:

```
uv run dynasty-agent predict-matchup --week 1 \
  --team-a "Jalen Hurts" --team-a "Derrick Henry" --team-a "Puka Nacua" \
  --team-b "Jayden Daniels" --team-b "James Cook" --team-b "Drake London"
```

`--week` is required, it picks up real Vegas lines for that week from nflverse's own free schedules data (no paid odds API, no API key). The FPPG baseline season and the Vegas season are two different numbers on purpose, `--season` defaults to the most recently completed season (there's usually no current-season data yet) while `--vegas-season` defaults to whatever season is actually live right now, per your last `sync`. Override either if you're checking a specific past week.

Every command reruns fresh against whatever's cached in `data/` (git-ignored, local SQLite plus downloaded nflverse parquet files). Nothing here needs a server or an account beyond your own Sleeper login.

## How the numbers work, briefly

- **Fantasy points** come from your league's real `scoring_settings`, pulled live from the Sleeper API, not a hardcoded PPR formula.
- **Age curve**: flat at full value through a position's peak, then an exponential decay past it, tuned per position (see `metrics.AGE_CURVES`).
- **Situation score**: a team's QB passing EPA, pass rate over expected, and sack rate allowed (inverted, a public proxy for offensive line quality), each percentile-ranked against all 32 NFL teams and averaged.
- **Pick values**: anchored to FantasyCalc's real current price for a `<round>` pick, discounted forward per year by `--discount-rate` (default 20%/year).
- **Matchup win probability** (`predict-matchup`) takes each side's players' real per-season mean and *sample* variance of weekly fantasy points (Bessel's correction, dividing by n-1, an earlier draft divided by n and understated it), scales the mean by a real Vegas-implied team total for the specific week versus that team's own season norm (nflverse's free schedules data, `spread_line`/`total_line`, no paid odds API), widens or collapses variance by current injury status (Questionable/Doubtful is closer to bimodal than a healthy player's normal week-to-week swing, so it widens; Out/IR collapses toward near-certain zero), and reads the win probability off the normal distribution of the resulting margin, the same idea Vegas spread-to-moneyline conversion uses. Read this one as a **draft heuristic, not a fitted model**: nothing here is calibrated against real game outcomes, and it assumes players score independently of their teammates, which isn't quite true (a QB and his own WR1 correlate). It's grounded in real per-player, per-week data at every step, it's just not a validated prediction, and it's a first draft toward a real prediction mode with a trade bot on top, not that thing itself.

Every one of these is a plain, readable function in `src/dynasty_agent/metrics.py`, unit tested in `tests/test_metrics.py`, not a black box.

## Project layout

```
src/dynasty_agent/   the CLI and every module (sleeper, nflverse, market, metrics, valuation)
migrations/          the SQLite schema, one file per migration, applied automatically
tests/                unit tests for the metric calculations
CLAUDE.md             environment, league settings, strategic reasoning, and working rules
PLANNING.md           phase-by-phase build status and open questions
```

## License

MIT, see `LICENSE`.
