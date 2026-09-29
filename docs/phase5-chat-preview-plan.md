# Phase 5 Preview Plan: talk to the Sleeper Dynasty Agent

Handoff for a new chat, written 2026-09-29 at the end of the session "Sleeper Agent 002".
Paste the prompt at the bottom into the new chat to pick up exactly here.

---

## 1. Goal of this preview

A first working `dynasty-agent chat`: open a terminal, ask a plain-English question about your
real Sleeper league, and get an answer grounded in this project's real numbers, answered by an
open-source model running locally on the MacBook Air M4 (16 GB) through Ollama.

Built on its **own branch** (`phase5-chat-preview`), tried by the user, and only merged to
`main` after that test plus the full test suite pass.

**In the preview**
- `uv run dynasty-agent chat`, talking to `qwen3:4b-instruct` through Ollama.
- Six tools on the real league: start/sit lineup, trade evaluation, my team, waiver targets,
  pick advice, taxi/IR plan.
- Python writes the numbers block (instant, can't be misquoted); the model streams a 1-3
  sentence take underneath.
- A tokens/second line under each answer (not yet pinned bottom right, see section 7).

**Not in the preview** (rest of Phase 5): the first-run setup wizard, the full ~40-question
evaluation set and model comparison, the pinned bottom-right status bar, a local web page.

---

## 2. Where things stand

- **Repo**: github.com/3schtocky/Sleeper-Dynasty-AI-Agent, local at `~/Sleeper-Dynasty-AI-Agent`,
  on `main` at `5ec74fa`. Public backup: github.com/3schtocky/Sleeper-Dynasty-Agent (remote
  `backup`, not auto-synced: after `git push`, run `git push backup --all`).
- **Built and on main**: Phases 0-4 plus the Phase 3.5 audit. 118 tests pass
  (`uv run pytest`). `refresh` runs daily at 06:00 via launchd.
- **`PLANNING.md`** has the full Phase 5 plan (section "Phase 5: talk to the agent"), the model
  bake-off, the UX target, and the live-stats requirement. `CLAUDE.md` is the reference doc and
  working rules (writing style: no em dashes, no Oxford commas, active voice).
- **Ollama 0.34.4** installed at `/usr/local/bin/ollama`, app in `/Applications`. Models live in
  `~/Open Source Models` (`~/.ollama/models` is a symlink to it), inventoried in `MODELS.md`
  there. Quitting Ollama: use the menu bar, then confirm with `pgrep -fl "ollama serve"`; the
  app can stay running hidden.
- **Model**: `qwen3:4b-instruct` (2.5 GB, Q4_K_M). Bake-off (`benchmarks/model_bench.py`):
  16/16 routing on every run, 0 invented numbers, ~35 tok/s writing and ~270 tok/s reading when
  the machine is cool. The fanless Air throttles under sustained load (down to ~19 tok/s after
  40 minutes of benchmarks). Faster models tested (qwen3:1.7b, lfm2.5, granite4, gemma4:e2b)
  swap trade sides or burn tokens on hidden reasoning.

---

## 3. Design rules (decided, don't relitigate)

1. **The model routes and explains; Python computes every number.** It never does arithmetic
   or recalls a stat.
2. **Never ask the model for what Python knows.** No week, season, roster or team arguments:
   the model once invented "week 1" in week 4. Python fills them in.
3. **Accept any reasonable argument format.** The model wrote picks as `2027-1` in one run and
   `2027 1st` in the next. Normalize in the tool layer.
4. **Python writes the numbers block, the model adds a short take.** That's fast (~60 tokens,
   ~2 seconds) and can't misquote a number. Stream the take.
5. **Keep tool results compact** for the model. Reading costs ~270 tok/s, so hand it a tight
   summary; the full numbers stay available via `/raw`.
6. **Ambiguous names become questions, never guesses.** `valuation.resolve_player` already
   raises on "Justin Jefferson" (a WR and an LB). Turn that into "Which one: ...?"
7. **Sleeper's API is read-only.** The agent recommends; the user makes the move in the Sleeper
   app. Say so when recommending a move.
8. **FantasyCalc values are points, not dollars.** In testing, the model called a -259 market
   value "$259". Say it in the system prompt.

---

## 4. Existing code to reuse (exact signatures on main)

All return plain dicts or lists; the CLI functions in `cli.py` only format and print them.

| Tool | Function | Notes |
|---|---|---|
| set_lineup | `weekly.optimize_lineup(conn, stats_season, vegas_season, week, my_roster_id)` | Call `SleeperClient(conn).sync_matchups(week)` first (see `cli.cmd_optimize_lineup`) |
| evaluate_trade | `valuation.evaluate_trade(conn, valuation_season, my_roster_id, send_players, send_picks, receive_players, receive_picks, discount_rate)` | Picks are `(season, round)` tuples; `cli._parse_pick` only accepts `2027-1` today, so build a lenient parser (rule 3) |
| my_team | `valuation.player_valuations(conn, season)` + `valuation.contend_or_rebuild(conn, season, my_roster_id)` | See `cli.cmd_valuate` for which roster rows to show |
| waiver_targets | `weekly.top_faab_targets(conn, stats_season, my_roster_id, limit=5)` | Each entry is a `faab_recommendation` dict |
| pick_advice | `picks.pick_report(conn, stats_season, my_roster_id, last_complete_season, scoring_settings, roster_filter)` | Pass `roster_filter=my_roster_id` |
| taxi_plan | `taxi.plan(conn, stats_season, my_roster_id)` | |

Helpers in `cli.py`: `_latest_ingested_season(conn)`, `_resolve_vegas_season(conn, None)`,
`_latest_complete_season(conn)`, `_require_config()`. My roster:
`SELECT roster_id FROM rosters WHERE owner_id = config.SLEEPER_USER_ID`. Current week:
`SELECT week FROM nfl_state ORDER BY fetched_at DESC LIMIT 1`.

Ollama chat API: `POST http://localhost:11434/api/chat` with `model`, `messages`, `tools`,
`stream`, `options: {"num_ctx": 8192, "temperature": 0}` for routing, and `keep_alive`. A tool
result goes back as `{"role": "tool", "content": ..., "tool_name": ...}`, after the assistant's
own `tool_calls` message. The final streamed chunk carries `eval_count` and `eval_duration`
(nanoseconds) for exact tok/s, plus `prompt_eval_count`, `prompt_eval_duration` and
`load_duration`. See `benchmarks/model_bench.py` for working request shapes, including the two
harness bugs already fixed there.

---

## 5. Build steps (each verified before the next)

Work on branch `phase5-chat-preview`. Run `uv run pytest` after every step.

**Step 1: separate logic from printing, for the six tools only.** Move the formatting out of
`cmd_optimize_lineup`, `cmd_trade`, `cmd_valuate`, `cmd_faab`/`digest`'s waiver section,
`cmd_picks` and `cmd_taxi` into formatter functions that return strings. The commands then just
call the logic and print the formatter's string.
*Verify:* capture each command's output before the change and diff it after. Must be identical
(ignore timestamps).

**Step 2: `src/dynasty_agent/llm.py`, a minimal Ollama client.** `chat(messages, tools,
stream)` returns tool calls, or yields content chunks plus the final stats. `LLM_MODEL` and
`OLLAMA_URL` come from `.env`, defaulting to `qwen3:4b-instruct` and
`http://localhost:11434`. `ensure_ready()` checks the server and model, returning a clear
message such as "Ollama isn't running: open the Ollama app" or "Run `ollama pull
qwen3:4b-instruct`".
*Verify:* unit tests with a fake HTTP transport, plus one live call.

**Step 3: `src/dynasty_agent/tools.py`, schemas and dispatch.** The six tools as flat JSON
schemas with no week/season/roster arguments, plus `run_tool(conn, name, args) -> (numbers_block,
compact_result)`. Include lenient parsing for picks (`2027 1st`, `2027-1`, `2027 round 1`,
`'27 first`) and player names. An ambiguous name returns `needs_clarification` with the
candidates.
*Verify:* tests for every pick format and for the ambiguous "Justin Jefferson" case, plus each
tool run against the real database.

**Step 4: `dynasty-agent chat`.**
- On start, run `refresh`. Warm the model in parallel (a 1-token request with `keep_alive`),
  so there's no 18-second cold start.
- Loop: read the question, route it (model + tools), run the tool, print the numbers block,
  then stream the model's take with a system prompt applying rules 4, 7 and 8.
- Print the stats line under each answer:
  `qwen3:4b-instruct · 34.8 tok/s · first words 1.2s · tool: evaluate_trade`
- Commands: `/raw` (full numbers of the last answer), `/stats off|on`, `/help`, `/quit`.
- No tool picked: answer briefly, and say what the agent can help with.

*Verify:* a manual session on the real league (section 6).

**Step 5: grounding check.** Every number in the model's take must appear in that turn's
numbers block or tool result. Otherwise drop the take and show the numbers block alone, with
a one-line note. Carry the draft caveat for matchup and win-probability numbers through.
*Verify:* unit test with a take that invents a number.

**Step 6: routing regression tests.** Turn the 16 bake-off questions in
`benchmarks/model_bench.py` into `tests/test_chat_routing.py`: offline, using recorded model
responses, asserting that routing reaches the right tool with normalized arguments.

**Step 7: document and hand over.** Add a README section ("Talk to the agent (preview)":
install Ollama, `ollama pull qwen3:4b-instruct`, `uv run dynasty-agent chat`) and a
PLANNING.md entry recording what was verified live. Change CLAUDE.md's "No GPU work and no
local models" rule deliberately, with the reason. Push the branch, open a PR, and do **not**
merge until the user has tried it.

---

## 6. Acceptance test (live, on the real league)

Each answer's numbers must match the matching CLI command's output exactly:

1. "Who should I start this week?" should match `optimize-lineup --week <current>`.
2. "Should I trade Jonah Coleman and my 2028 2nd for a 2027 1st?" should match `trade --send "Jonah
   Coleman" --send-pick 2028-2 --receive-pick 2027-1`.
3. "Am I a contender?" should match `valuate`'s verdict.
4. "Who should I pick up?" should match `digest`'s FAAB targets.
5. "Should I sell my first?" should match `picks`.
6. "Anyone I should put on taxi?" should match `taxi`.
7. "Trade Justin Jefferson for Puka Nacua?" should ask which Justin Jefferson.
8. "What's the capital of France?" should get a short reply with no tool and no invented stats.

Also: tok/s is shown under each answer, and after a cold start the first answer shows "loading
model..." instead of a false slow rate.

---

## 7. Open decisions and pending items

- **Pinned bottom-right status bar** (the user's request, in PLANNING.md): it needs
  `prompt_toolkit`'s bottom toolbar. That's the project's first UI dependency, so **ask the user
  before adding it**. The preview uses a plain line under each answer.
- **Unmerged branch `worktree-phase4-college-data`** (Sept 2-3, on both GitHub repos): its own
  Phase 4, a matchup model **calibrated against 3,403 real games**, a **Monte Carlo season
  simulation**, and a one-line installer built on **LM Studio**. It forked from the same point as
  `main` (`5019ce3`) and overlaps heavily. Review it with the user before Phase 5 goes much
  further; the calibration and simulation may be worth porting. Don't discard it.
- **The user's clean first-time install test** (cloning from GitHub fresh and following the
  README): ask what broke. Those findings drive the setup wizard. Suspected gap, unverified:
  `refresh` on an empty database may try to refit the prospect model before college history
  exists.
- **Rejected models** in `~/Open Source Models` (~20 GB): the user hasn't decided whether to
  remove them. Keep `qwen3:4b-instruct` and maybe `gemma4:e2b`; update `MODELS.md` if any are
  removed.
- **Roster moves the tool recommended** (the user makes these in Sleeper): Jonah Coleman (on IR)
  to the IR slot, Emmett Johnson and Demond Claiborne to taxi. Emmett Johnson is a sell-high
  candidate (FantasyCalc ranks him 11th in the 2026 class, draft capital 36th).

---

## 8. Prompt for the new chat

> I'm continuing the Sleeper Dynasty Agent project in `~/Sleeper-Dynasty-AI-Agent`. Read
> `docs/phase5-chat-preview-plan.md` first, then `CLAUDE.md` and the Phase 5 section of
> `PLANNING.md`. Build the Phase 5 chat preview on a new branch, following the plan's steps in
> order and verifying each before the next. Ask me before adding `prompt_toolkit`. Before
> starting, ask me what my clean-install test found.
