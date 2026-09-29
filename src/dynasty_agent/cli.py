"""Command-line entrypoint: `dynasty-agent <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from dynasty_agent import blend, college, config, context, market, matchup, nflverse, picks, prospect_model, prospects, refresh, schedule, sleeper, taxi, valuation, weather, weekly
from dynasty_agent.db import get_db
from dynasty_agent.errors import AgentError
from dynasty_agent.sleeper import SleeperClient


def cmd_init(args: argparse.Namespace) -> None:
    user = sleeper.lookup_user(args.username)
    if user is None:
        print(f"No Sleeper user found for username '{args.username}'.", file=sys.stderr)
        raise SystemExit(1)
    user_id = user["user_id"]

    season = sleeper.current_nfl_season()
    leagues = sleeper.list_leagues_for_season(user_id, season)

    if args.league_id:
        # A renewed league belongs to next season, and Sleeper creates it
        # months before its own current season rolls over, so an explicit
        # --league-id (what `sync`'s renewal notice suggests) searches both.
        next_season = str(int(season) + 1)
        candidates = leagues + sleeper.list_leagues_for_season(user_id, next_season)
        league = next((league for league in candidates if league["league_id"] == args.league_id), None)
        if league is None:
            print(
                f"'{args.username}' is not in a {season} or {next_season} league with id {args.league_id}.",
                file=sys.stderr,
            )
            raise SystemExit(1)
    elif not leagues:
        print(f"'{args.username}' has no {season} NFL leagues on Sleeper.", file=sys.stderr)
        raise SystemExit(1)
    elif len(leagues) == 1:
        league = leagues[0]
    else:
        print(f"'{args.username}' is in {len(leagues)} {season} leagues:\n")
        for league in leagues:
            print(f"  {league['league_id']}  {league['name']}")
        print(f"\nRe-run with --league-id <id> to pick one.")
        raise SystemExit(1)

    # encoding explicit: Path.write_text() otherwise falls back to the OS
    # locale encoding, not always UTF-8 on Windows.
    config.ENV_PATH.write_text(
        f"SLEEPER_USERNAME={args.username}\n"
        f"SLEEPER_USER_ID={user_id}\n"
        f"LEAGUE_ID={league['league_id']}\n"
        f"DRAFT_ID={league.get('draft_id') or ''}\n",
        encoding="utf-8",
    )
    print(f"Wrote {config.ENV_PATH}")
    print(f"League: {league['name']} ({league['league_id']}, {league.get('season') or season} season)")
    print("Next: `dynasty-agent sync`")


def cmd_sync(args: argparse.Namespace) -> None:
    context.require_config()
    conn = get_db()
    with SleeperClient(conn) as client:
        client.sync_all()
    market.sync_market_values(conn)
    print("Synced players, league, users, rosters, traded picks, drafts, nfl state, and market values.")

    league_row = conn.execute("SELECT season FROM league WHERE league_id = ?", (config.LEAGUE_ID,)).fetchone()
    if league_row is not None and league_row["season"]:
        successor = sleeper.find_successor_league(config.SLEEPER_USER_ID, config.LEAGUE_ID, league_row["season"])
        if successor is not None:
            print(
                f"\nNOTICE: this league has been renewed for {successor['season']} under a new league_id, "
                f"{successor['league_id']}. Everything above synced the {league_row['season']} league. "
                f"Run `dynasty-agent init --username {config.SLEEPER_USERNAME} --league-id {successor['league_id']}` "
                f"to switch to it.",
                file=sys.stderr,
            )


def cmd_roster(args: argparse.Namespace) -> None:
    context.require_config()
    conn = get_db()
    me = context.my_roster_id(conn)

    rows = conn.execute(
        """
        SELECT rp.player_id, p.full_name, p.position, p.team, p.age, rp.slot,
               mv.value AS market_value
        FROM roster_players rp
        JOIN players p ON p.player_id = rp.player_id
        LEFT JOIN market_values mv ON mv.player_id = rp.player_id AND mv.source = 'fantasycalc'
            AND mv.as_of_date = (
                SELECT max(as_of_date) FROM market_values mv2
                WHERE mv2.player_id = rp.player_id AND mv2.source = 'fantasycalc'
            )
        WHERE rp.roster_id = ?
        ORDER BY CASE rp.slot WHEN 'starter' THEN 0 WHEN 'bench' THEN 1 WHEN 'taxi' THEN 2 ELSE 3 END,
                 mv.value DESC
        """,
        (me,),
    ).fetchall()

    slot_labels = {"starter": "START", "bench": "BENCH", "taxi": "TAXI", "reserve": "IR"}
    header = f"{'Slot':<7} {'Player':<22} {'Pos':<4} {'Team':<5} {'Age':<4} {'Value':>6} {'30d':>7}"
    print(header)
    print("-" * len(header))
    for r in rows:
        trend = market.value_trend(conn, r["player_id"], 30)
        value = r["market_value"]
        print(
            f"{slot_labels.get(r['slot'], r['slot']):<7} "
            f"{(r['full_name'] or '?'):<22} {(r['position'] or ''):<4} {(r['team'] or ''):<5} "
            f"{(str(r['age']) if r['age'] is not None else '-'):<4} "
            f"{(f'{value:.0f}' if value is not None else '-'):>6} "
            f"{(f'{trend:+.0f}' if trend is not None else '-'):>7}"
        )


def cmd_ingest_nflverse(args: argparse.Namespace) -> None:
    conn = get_db()
    league = conn.execute(
        "SELECT scoring_settings_json FROM league ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()
    if league is None:
        print("No league data cached yet. Run `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)
    scoring_settings = json.loads(league["scoring_settings_json"])
    # The current NFL season is still gaining weeks, so its cached files are
    # always re-downloaded; a completed season's files never change.
    state_row = conn.execute("SELECT season FROM nfl_state ORDER BY fetched_at DESC LIMIT 1").fetchone()
    is_current_season = state_row is not None and state_row["season"] is not None and int(state_row["season"]) == args.season
    force = args.force or is_current_season
    if is_current_season and not args.force:
        print(f"{args.season} is the current NFL season, re-downloading its files to pick up new weeks.")
    print(nflverse.ingest_season(conn, args.season, scoring_settings, force=force))


def cmd_ingest_draft_data(args: argparse.Namespace) -> None:
    conn = get_db()
    print(prospects.ingest_draft_data(conn, force=args.force))


def cmd_ingest_college(args: argparse.Namespace) -> None:
    conn = get_db()
    last = args.through or args.season
    if last < args.season:
        print("--through must be the same season as --season or later.", file=sys.stderr)
        raise SystemExit(1)
    for season in range(args.season, last + 1):
        print(college.ingest_season(conn, season, force=args.force))


def cmd_fit_prospect_model(args: argparse.Namespace) -> None:
    conn = get_db()
    league = conn.execute("SELECT scoring_settings_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if league is None:
        print("No league data cached yet. Run `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)
    report = prospect_model.fit_and_store(conn, json.loads(league["scoring_settings_json"]), context.latest_complete_season(conn))
    cov = report["coverage"]
    first, last = report["classes"]
    print(f"Prospect model fit on the {first}-{last} draft classes, drafted QB/RB/WR/TE, your league's scoring.")
    print(
        f"Coverage: {cov['drafted']} drafted, {cov['used']} used, {cov['no_college_link']} with no exact college "
        f"link, {cov['no_birthdate']} with no birth date.\n"
    )
    print("Target: fantasy points per game scheduled over each player's first 3 NFL seasons.")
    print("Scored by leaving one draft class out at a time and predicting it from the rest:\n")
    labels = {
        "post_draft": "Post-draft (draft capital + college + age + athleticism)",
        "pre_draft": "Pre-draft (no draft capital)",
        "pre_draft_no_age": "Pre-draft, no age (most prospects have no birth date)",
        "baseline_draft_capital": "Baseline (draft capital + position only)",
    }
    for variant, v in report["variants"].items():
        print(f"  {labels[variant]:<58} MAE {v['cv']['mae']:.2f} PPG, R^2 {v['cv']['r2']:.3f}, n={v['n']}")
    print("\nWeights (applied to raw feature values, intercept first):")
    for variant, v in report["variants"].items():
        print(f"  {variant}: intercept {v['weights'][0]:+.3f}")
        for name, w in zip(v["features"], v["weights"][1:]):
            print(f"      {name:<16} {w:+.3f}")


_BOARD_LEGEND = (
    "\nDom: peak college dominator rating (share of team receiving yards and TDs). RecBreakout: age at the first "
    "20%+ dominator season, receiving only, so it undersells a between-the-tackles RB; 'unknown' means no real "
    "birth date, never estimated. Ath: this project's position-relative athletic score, needs 3+ combine tests. "
    "FCalc: FantasyCalc's current dynasty value. Proj: projected PPG over the first 3 NFL seasons. League: Proj "
    "after this league's position weighting (QB x0.70, WR x1.05), the ranking column."
)


def _breakout_label(row: dict) -> str:
    if row["position"] == "QB":
        return "-"
    if row["breakout"] == "broke_out":
        return f"at {row['breakout_age']:.1f}" if row.get("breakout_age") is not None else "yes"
    return row["breakout"]


def cmd_prospect_board(args: argparse.Namespace) -> None:
    conn = get_db()
    try:
        if args.mode == "post-draft":
            board = prospect_model.post_draft_board(conn, args.draft_class)
        else:
            board = prospect_model.pre_draft_board(conn, args.draft_class)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)
    rows = board["rows"][: args.limit]
    if not rows:
        print(
            f"No {args.draft_class} prospects found. Post-draft mode needs that NFL draft to have happened "
            f"(`ingest-draft-data --force`); pre-draft mode needs the prior college season (`ingest-college`).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    def fmt(value, spec, missing="-"):
        return format(value, spec) if value is not None else missing

    if board["mode"] == "post_draft":
        m = board["model"]
        print(f"{args.draft_class} rookie board, POST-DRAFT: ranked by draft capital and position.")
        print(
            f"Model: fitted on the {m['training_classes']} classes, projects PPG per game scheduled over the first "
            f"3 NFL seasons, your league's scoring. Held-out error {m['cv_mae']:.2f} PPG, R^2 {m['cv_r2']:.2f}."
        )
        print(
            "College production, age, and athleticism are shown for context only: tested on the same classes, "
            "they did not improve the ranking once draft capital was known.\n"
        )
        header = (
            f"{'#':>3} {'Player':<24} {'Pos':<3} {'Team':<4} {'Pick':>5} {'Age':>5} {'Dom':>5} {'RecBreakout':<14} "
            f"{'Ath':>4} {'FCalc':>6} {'Proj':>5} {'League':>6}"
        )
        print(header)
        print("-" * len(header))
        for i, r in enumerate(rows, 1):
            print(
                f"{i:>3} {r['name'][:24]:<24} {r['position']:<3} {r['nfl_team'] or '':<4} "
                f"{r['round']}.{r['pick']:<3} {fmt(r['draft_age'], '5.1f'):>5} "
                f"{('-' if r['position'] == 'QB' else fmt(r['peak_dominator'], '5.2f')):>5} "
                f"{_breakout_label(r):<14} {fmt(r['athletic'], '4.0f'):>4} {fmt(r['market_value'], '6.0f'):>6} "
                f"{r['projected_ppg']:>5.1f} {r['league_ppg']:>6.1f}"
            )
        print(_BOARD_LEGEND)
    else:
        with_age, no_age = board["models"]["pre_draft"], board["models"]["pre_draft_no_age"]
        print(f"{args.draft_class} class, PRE-DRAFT: WEAK SIGNAL, a first read before the NFL draft order exists.")
        print(
            f"Ranked on college production through the {board['last_college_season']} season. Held-out accuracy is low: "
            f"R^2 {with_age['cv_r2']:.2f} with a real birth date, {no_age['cv_r2']:.2f} without one (most prospects). "
            f"Draft capital explains far more; switch to --mode post-draft once the NFL draft happens. "
            f"Running backs are under-ranked here: dominator rating measures receiving only."
        )
        print(
            f"Eligibility is estimated as 3+ college seasons on record, real declarations are unknown until January. "
            f"A season counts only with {prospect_model.MIN_TEAM_GAMES}+ team games and "
            f"{prospect_model.MIN_PLAYER_GAMES}+ player games in ESPN's data, so an in-progress season joins once it "
            f"gets there; {board['skipped_thin_sample']} players had no qualifying season and "
            f"{board['skipped_unrated_team']} played for an unrated (mostly FCS) team the model never saw in training, "
            f"both left off rather than guessed. Team strength: the peak season's opponent-adjusted rating.\n"
        )
        header = (
            f"{'#':>3} {'Player':<24} {'Pos':<3} {'Seasons':>7} {'Games':>5} {'Age':>5} {'Dom':>5} "
            f"{'RecBreakout':<14} {'Conf':<12} {'TeamZ':>5} {'Proj':>5} {'League':>6}"
        )
        print(header)
        print("-" * len(header))
        for i, r in enumerate(rows, 1):
            print(
                f"{i:>3} {r['name'][:24]:<24} {r['position']:<3} {r['college_seasons']:>7} {r['games_last_season']:>5} "
                f"{fmt(r['draft_age'], '5.1f', 'n/a'):>5} "
                f"{('-' if r['position'] == 'QB' else format(r['peak_dominator'], '5.2f')):>5} "
                f"{_breakout_label(r):<14} {(r['conference'] or '')[:12]:<12} {r['team_strength']:>5.2f} "
                f"{r['projected_ppg']:>5.1f} {r['league_ppg']:>6.1f}"
            )
        print(_BOARD_LEGEND)


def cmd_refresh(args: argparse.Namespace) -> None:
    context.require_config()
    conn = get_db()
    started = datetime.now()
    print(f"=== dynasty-agent refresh, {started:%Y-%m-%d %H:%M} ===")
    results = refresh.run(conn)
    for r in results:
        print(f"  [{'ok' if r['ok'] else 'FAILED'}] {r['step']}: {r['detail']}")
        if not r["ok"] and args.verbose:
            print(r["traceback"])
    print("\nHow current everything is:")
    for label, value in refresh.freshness(conn):
        print(f"  {label:<22} {value}")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)} of {len(results)} steps ok, {(datetime.now() - started).seconds}s.")
    if failed:
        raise SystemExit(1)


def cmd_picks(args: argparse.Namespace) -> None:
    context.require_config()
    conn = get_db()
    me = context.my_roster_id(conn)
    stats_season = context.stats_season(conn)
    try:
        report = picks.pick_report(
            conn, stats_season, me, context.latest_complete_season(conn),
            context.scoring_settings(conn), None if args.all else me,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    names = {
        r["roster_id"]: (r["team_name"] or r["display_name"] or f"roster {r['roster_id']}")
        for r in conn.execute(
            "SELECT ro.roster_id, u.display_name, u.team_name FROM rosters ro LEFT JOIN users u ON u.user_id = ro.owner_id"
        )
    }
    weight = report["record_weight"]
    print(f"Rookie picks, {'every team' if args.all else 'yours'}. Next draft: {report['next_draft']}.")
    print(
        f"{report['next_draft']} slots are PROJECTED reverse standings: roster strength (best-lineup win-now) blended "
        f"toward real record, record weighted {weight:.0%} so far (games played / regular-season games). "
        f"Later drafts can't be slotted yet."
    )
    print(
        f"Advice compares FantasyCalc's price for the pick with FantasyCalc's current value for the "
        f"{report['recent_class']} rookies taken at that same slot: pick more than {picks.SELL_ABOVE:.2f}x the "
        f"player it bought last year = SELL, under {picks.BUY_BELOW:.2f}x = BUY. History: that slot's rookies since "
        f"{prospect_model.FIRST_TRAINING_CLASS}, league-weighted PPG over 3 seasons and how often they hit "
        f"{picks.HIT_LEAGUE_PPG:g}+ (a weekly flex starter).\n"
    )
    header = f"{'Pick':<14} {'Holder':<16} {'From':<16} {'FCalc':>6} {'Comp':>6} {'Ratio':>5}  {'Advice':<8} {'Hist PPG':>8} {'Hit%':>5}"
    print(header)
    print("-" * len(header))
    for r in report["rows"]:
        slot = r["projected_slot"] or "  -  "
        label = f"{r['season']} {slot}" + (f" {r['tier'][0]}" if r["tier"] else "")
        if not r["projected_slot"]:
            label = f"{r['season']} rd {r['round']}"
        hist = r["history"] or {}
        fmt = lambda v, spec: format(v, spec) if v is not None else "-"
        holder = names.get(r["owner_roster_id"], "?")[:16]
        origin = "own" if r["original_roster_id"] == r["owner_roster_id"] else names.get(r["original_roster_id"], "?")[:16]
        mark = "*" if r["owner_roster_id"] == me else " "
        print(
            f"{label:<14}{mark}{holder:<16} {origin:<16} {fmt(r['fantasycalc_price'], '6.0f'):>6} "
            f"{fmt(r.get('comparable_value'), '6.0f'):>6} {fmt(r['ratio'], '5.2f'):>5}  {r['advice'][:8]:<8} "
            f"{fmt(hist.get('mean_league_ppg'), '8.1f'):>8} {fmt(hist.get('hit_rate') * 100 if hist else None, '4.0f'):>4}%"
        )
    slotted = [r for r in report["rows"] if r.get("comparable_players")]
    if slotted and not args.all:
        print("\nComparables (the recent rookies each projected slot actually bought):")
        for r in slotted:
            print(f"  {r['season']} {r['projected_slot']}: {', '.join(r['comparable_players'])}")
    verdict = valuation.contend_or_rebuild(conn, stats_season, me)
    print(
        f"\nYour posture: {verdict['verdict'].upper()} ({verdict['confidence']}). "
        f"A contender sells picks for win-now help; a rebuilder holds or buys them."
    )


def cmd_taxi(args: argparse.Namespace) -> None:
    context.require_config()
    conn = get_db()
    me = context.my_roster_id(conn)
    try:
        result = taxi.plan(conn, context.stats_season(conn), me)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)
    st = result["settings"]
    deadline = f"taxi deadline week {st['taxi_deadline']}" if st["taxi_deadline"] else "no taxi deadline (moves allowed all season)"
    eligible = "rookies or veterans" if st["taxi_allow_vets"] else "rookies only"
    print(
        f"Taxi and IR plan. Your league: {st['taxi_slots']} taxi slots, {eligible}, {st['taxi_years']} year max, "
        f"{deadline}; {st['reserve_slots']} IR slot. Taxi players can't be started.\n"
    )
    print(
        f"Active roster: {result['active_count']} of {result['active_capacity']}. "
        f"Taxi: {len(result['taxi_now'])} of {st['taxi_slots']} used. IR: {len(result['ir_now'])} of {st['reserve_slots']} used."
    )
    if result["moves"]:
        print(f"\nRecommended moves, each frees a bench spot ({result['bench_spots_freed']} total):")
        for m in result["moves"]:
            p = m["player"]
            print(
                f"  {p['full_name']:<22} {p['position']:<3} -> {m['to']:<4}  {p['ppg']:.1f} PPG, "
                f"3yr value {p['three_year_value']:.1f}: {m['why']}"
            )
        print("  Make these in the Sleeper app, this tool can't change your roster (Sleeper's API is read-only).")
    else:
        print("\nNo moves: every open taxi and IR slot is either filled or has no eligible player to put there.")
    for p in result["keep_active"]:
        print(f"  Keep {p['full_name']} active: taxi-eligible, but he's in your best lineup right now.")

    print(f"\nNext season ({result['next_draft']} rookie draft):")
    print(
        f"  You hold {result['next_picks']} picks in that draft. Today's taxi players graduate back to the active roster; "
        f"the new rookies can take the taxi slots. Projected: {result['roster_next']} players for {result['capacity_next']} spots."
    )
    if result["overflow"] > 0:
        if result["cut_candidates"]:
            print(f"  Roster crunch: {result['overflow']} cut(s) needed. Lowest three-year value among non-rookie non-starters:")
            for p in result["cut_candidates"]:
                print(f"    {p['full_name']:<22} {p['position']:<3} 3yr value {p['three_year_value']:.1f}")
        else:
            print(
                f"  Roster crunch: {result['overflow']} cut(s) needed, and every non-rookie on your roster starts, "
                f"so the cut would come from your lineup or a rookie."
            )
        print("  Or trade picks away before the draft; see `dynasty-agent picks`.")
    else:
        print("  No crunch: everyone fits, before any waiver adds between now and then.")


def cmd_schedule(args: argparse.Namespace) -> None:
    try:
        if args.install:
            print(schedule.install(*schedule.parse_time(args.at)))
        elif args.remove:
            print(schedule.remove())
        else:
            print(schedule.status())
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)


def cmd_calibrate_blend(args: argparse.Namespace) -> None:
    conn = get_db()
    season = args.season or context.latest_complete_season(conn)
    try:
        vets, n_vets = blend.backtest_veterans(conn, season - 1, season)
        rookies, n_rookies = blend.backtest_rookies(conn, season)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)
    if not vets:
        print(f"No data to backtest: ingest both {season - 1} and {season} first.", file=sys.stderr)
        raise SystemExit(1)
    print(f"Backtest: blend a prior with the first 1-8 weeks of {season}, score against the rest of that season.")
    print("How many games the prior should count as (K), mean absolute error in PPG, lower is better:\n")
    for label, res, n, current in (
        (f"Veterans ({season - 1} as the prior)", vets, n_vets, blend.VETERAN_PRIOR_GAMES),
        (f"Rookies ({season} class, draft-capital projection as the prior)", rookies, n_rookies, blend.ROOKIE_PRIOR_GAMES),
    ):
        if not res:
            print(f"  {label}: no cases.")
            continue
        best = min(res, key=res.get)
        print(f"  {label}, {n} players: best K = {best:g} (MAE {res[best]:.3f}); in use K = {current:g}")
        print("     " + "  ".join(f"K{k:g}={v:.3f}" for k, v in res.items()))
    print("\nThe constants in use live in blend.py with the run they came from; change them there if a new season disagrees.")


def cmd_valuate(args: argparse.Namespace) -> None:
    context.require_config()
    conn = get_db()
    me = context.my_roster_id(conn)

    season = context.stats_season(conn, args.season)

    valuations = valuation.player_valuations(conn, season)
    print(_basis_line(season))
    print(
        "Situation score: average of QB passing EPA/game, team pass rate over expected, and sack rate "
        "allowed (inverted), each percentile-ranked against all 32 NFL teams. Not a full offensive line "
        "grade, real OL grades are paywalled; this is the public proxy.\n"
    )

    my_players = conn.execute(
        "SELECT rp.player_id, rp.slot, p.full_name, p.position, p.age FROM roster_players rp "
        "JOIN players p ON p.player_id = rp.player_id WHERE rp.roster_id = ?",
        (me,),
    ).fetchall()

    slot_order = {"starter": 0, "bench": 1, "taxi": 2, "reserve": 3}

    def sort_key(row):
        v = valuations.get(row["player_id"])
        win_now = v["win_now_value"] if v else -1.0
        return (slot_order.get(row["slot"], 9), -win_now)

    my_players = sorted(my_players, key=sort_key)

    slot_labels = {"starter": "START", "bench": "BENCH", "taxi": "TAXI", "reserve": "IR"}
    header = f"{'Slot':<7} {'Player':<22} {'Pos':<4} {'Age':<4} {'FPPG':>6} {'Sit%':>6} {'WinNow':>8} {'3yr':>8}"
    print(header)
    print("-" * len(header))
    for row in my_players:
        v = valuations.get(row["player_id"])
        label = slot_labels.get(row["slot"], row["slot"])
        name = (row["full_name"] or "?")[:22]
        pos = row["position"] or ""
        age = str(row["age"]) if row["age"] is not None else "-"
        if v is None:
            print(f"{label:<7} {name:<22} {pos:<4} {age:<4} {'-':>6} {'-':>6} {'-':>8} {'-':>8}  (no {season} games)")
            continue
        print(
            f"{label:<7} {name:<22} {pos:<4} {age:<4} "
            f"{v['fantasy_points_per_game']:>6.1f} {v['situation_score']:>6.1f} "
            f"{v['win_now_value']:>8.1f} {v['three_year_value']:>8.1f}{_rookie_note(v)}"
        )

    if any(valuations.get(r["player_id"], {}).get("value_source") == "prospect_model" for r in my_players):
        print(_ROOKIE_FOOTNOTE)

    verdict = valuation.contend_or_rebuild(conn, season, me)
    print()
    print(f"Verdict: {verdict['verdict'].upper()}")
    print(f"Confidence: {verdict['confidence']}")
    print(
        f"Inputs: win-now total {verdict['my_win_now_total']:.1f} "
        f"({verdict['win_now_percentile']:.0f}th percentile of {len(verdict['league_win_now_totals'])} teams), "
        f"three-year total {verdict['my_three_year_total']:.1f} "
        f"({verdict['three_year_percentile']:.0f}th percentile of {len(verdict['league_three_year_totals'])} teams), "
        f"{verdict['games_played']} games played this season."
    )


_ROOKIE_FOOTNOTE = (
    f"\n* Rookie: FPPG starts from the prospect model's projection from real draft capital (fit on 2018-2023 "
    f"classes), worth {blend.ROOKIE_PRIOR_GAMES:g} games, with his real games this season blended in on top. The "
    f"projection is points per game scheduled over a first 3 seasons, so it runs a little conservative next to a "
    f"veteran's per-game-played average. Run `prospect-board` for the inputs."
)


def _basis_line(season: int) -> str:
    return (
        f"Valuation basis: the {season} season to date, blended with {season - 1}: last season's average counts as "
        f"{blend.VETERAN_PRIOR_GAMES:g} games and each real {season} game adds on top (chosen by backtest, see "
        f"`calibrate-blend`), so the new season takes over as it accumulates."
    )


def _rookie_note(v: dict) -> str:
    if v.get("value_source") != "prospect_model":
        return ""
    games = f" + {v['games']} games" if v.get("games") else ""
    if v.get("undrafted"):
        return f"  * rookie, undrafted (priced as the last pick, outside the model's training data){games}"
    return f"  * rookie, projected from pick {v['draft_pick']}{games}"


def _pick_price_note(pick: dict) -> str:
    """Which FantasyCalc price a traded pick got, when it matters: a next-draft
    pick's price swings with its tier (2027 1st: Early 4608, Late 2313)."""
    if pick["tier"] and pick["price_label"] and "(" in pick["price_label"]:
        return f"  priced as {pick['price_label']}"
    if pick["tier"] is None and pick["price_label"] and pick["season"] == pick["base_season"]:
        return "  tier unknown, FantasyCalc's average; name a tier or slot (e.g. '2027 early 1st', '2027 1.03')"
    return ""


def cmd_trade(args: argparse.Namespace) -> None:
    context.require_config()
    conn = get_db()
    me = context.my_roster_id(conn)

    season = context.stats_season(conn, args.season)

    try:
        send_picks = [picks.parse_league_pick(conn, p) for p in (args.send_pick or [])]
        receive_picks = [picks.parse_league_pick(conn, p) for p in (args.receive_pick or [])]
        result = valuation.evaluate_trade(
            conn,
            season,
            me,
            send_players=args.send or [],
            send_picks=send_picks,
            receive_players=args.receive or [],
            receive_picks=receive_picks,
            discount_rate=args.discount_rate,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    def print_side(label: str, side: dict) -> None:
        print(f"{label}:")
        if not side["players"] and not side["picks"]:
            print("  (nothing)")
        for p in side["players"]:
            note = _rookie_note(p) if p["has_data"] else "  (no games this season, my-model valuation is 0)"
            market_str = f"{p['market_value']:.0f}" if p["market_value"] is not None else "-"
            print(
                f"  {p['full_name']:<22} {p['position'] or '':<4} "
                f"win-now {p['win_now_value']:>6.1f}  3yr(mine) {p['three_year_value']:>6.1f}  "
                f"market {market_str:>6}{note}"
            )
        for pk in side["picks"]:
            model_str = f"{pk['model_value']:.0f}" if pk["model_value"] is not None else "-"
            market_str = f"{pk['market_value']:.0f}" if pk["market_value"] is not None else "-"
            arb_str = f"{pk['arbitrage']:+.0f}" if pk["arbitrage"] is not None else "-"
            print(
                f"  {pk['label']:<22} {'PICK':<4} "
                f"model {model_str:>6}  market {market_str:>6}  arbitrage {arb_str:>7}{_pick_price_note(pk)}"
            )
        print(
            f"  totals: win-now (players only) {side['win_now_total']:.1f}, "
            f"3yr mine (players only) {side['player_three_year_total']:.1f}, "
            f"market value (players + picks, comparable) {side['market_value_total']:.0f}"
        )
        if side["unpriced"]:
            print(f"  WARNING: no market price for {', '.join(side['unpriced'])}, counted as 0 in the market total above.")

    print(f"Trade evaluation, pick discount rate {args.discount_rate:.0%} per year.")
    print(_basis_line(season))
    print(
        "Win-now and 3yr(mine) are this league's own formula, players only, picks can't help you win "
        "this year so they don't appear there. Market value is FantasyCalc's own pricing for players plus "
        "my discount-adjusted pick model, the only number below that's comparable across players and picks "
        "together.\n"
    )
    print_side("You send", result["sent"])
    print()
    print_side("You receive", result["received"])
    if any(p.get("value_source") == "prospect_model" for side in (result["sent"], result["received"]) for p in side["players"]):
        print(_ROOKIE_FOOTNOTE)
    print()
    print(f"Net win-now (players only): {result['win_now_delta']:+.1f}")
    print(f"Net 3yr, mine (players only): {result['player_three_year_delta']:+.1f}")
    print(f"Net market value (players + picks): {result['market_value_delta']:+.0f}")
    print(f"Your posture: {result['posture'].upper()} ({result['posture_confidence']})")
    print(f"Fit: {result['fit']}")
    if result["consolidation"]:
        print(f"Note: {result['consolidation']}")


def cmd_predict_matchup(args: argparse.Namespace) -> None:
    conn = get_db()
    if conn.execute("SELECT 1 FROM players LIMIT 1").fetchone() is None:
        print("No player data yet. Run `dynasty-agent init` and `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)

    season = context.stats_season(conn, args.season)

    vegas_season = context.vegas_season(conn, args.vegas_season)

    try:
        result = matchup.predict_matchup(conn, season, vegas_season, args.week, args.team_a or [], args.team_b or [])
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    print(
        f"Matchup prediction, {season} season FPPG basis, week {args.week} of the {vegas_season} season. "
        f"DRAFT MODEL, a heuristic, not a fitted or calibrated prediction, see matchup.py and PLANNING.md."
    )
    if not result["week_has_vegas_data"]:
        print(f"No Vegas lines published yet for {vegas_season} week {args.week}. Running on season averages only, no week adjustment.")
    print()

    def print_side(label: str, side: dict) -> None:
        print(f"{label}:")
        for p in side["players"]:
            if p["on_bye"]:
                print(f"  {p['full_name']:<20} {p['position'] or '':<4} {p['team'] or '':<4}   BYE WEEK, counted as 0")
                continue
            flag = f"  ({p['injury_status']})" if p["injury_status"] else ""
            if p["value_source"] == "prospect_model":
                data_note = f"  rookie: projection + {p['games']} games this season"
            elif p["games"] == 0 and p["prior_games"] == 0:
                data_note = "  NO DATA this season or last"
            else:
                data_note = f"  {p['games']} games this season + last season's {p['prior_games']}, blended"
            if p["thin_sample"] and p["value_source"] != "prospect_model" and (p["games"] or p["prior_games"]):
                data_note += ", variance not estimable, counted as 0"
            vegas_note = f", vegas x{p['vegas_multiplier']:.2f}" if p["vegas_multiplier"] != 1.0 else ""
            print(
                f"  {p['full_name']:<20} {p['position'] or '':<4} {p['team'] or '':<4} "
                f"{p['adjusted_mean']:>5.1f} avg{vegas_note}{flag}{data_note}"
            )
        print(f"  Team mean: {side['mean']:.1f}, std dev: {side['variance'] ** 0.5:.1f}\n")

    print_side("Team A", result["team_a"])
    print_side("Team B", result["team_b"])

    print(f"Projected margin (A - B): {result['mean_diff']:+.1f}, combined std dev: {result['std_diff']:.1f}")
    print(f"Team A win probability: {result['win_probability_a']:.1%}")
    print(f"Team B win probability: {result['win_probability_b']:.1%}")


def cmd_optimize_lineup(args: argparse.Namespace) -> None:
    context.require_config()
    conn = get_db()
    me = context.my_roster_id(conn)

    stats_season = context.stats_season(conn, args.season)
    vegas_season = context.vegas_season(conn, args.vegas_season)

    with SleeperClient(conn) as client:
        client.sync_matchups(args.week)

    try:
        result = weekly.optimize_lineup(conn, stats_season, vegas_season, args.week, me)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    if result["unsupported_slots"]:
        print(
            f"Warning: this league's roster has starting slot types this optimizer doesn't handle yet: "
            f"{result['unsupported_slots']}. Those slots were left unfilled.\n"
        )

    print(f"Lineup optimizer, {stats_season} season FPPG basis, week {args.week} of the {vegas_season} season.")
    print("Picks by win probability against your real Sleeper opponent, not raw projected points.\n")

    if result["opponent_note"]:
        print(f"Opponent: {result['opponent_note']}\n")
    else:
        print(
            f"Opponent (roster {result['opponent_roster_id']}): projected "
            f"{result['opponent_mean']:.1f} ± {result['opponent_variance'] ** 0.5:.1f}\n"
        )

    print("Recommended lineup:")
    for p in result["recommended_lineup"]:
        flag = f"  ({p['injury_status']})" if p["injury_status"] else ""
        vegas_note = f", vegas x{p['vegas_multiplier']:.2f}" if p["vegas_multiplier"] != 1.0 else ""
        bye_note = "  BYE WEEK" if p["on_bye"] else ""
        print(f"  {p['full_name']:<20} {p['position']:<3} mean={p['mean']:>5.1f} var={p['variance']:>5.1f}{vegas_note}{flag}{bye_note}")

    if result["recommended_win_probability"] is not None:
        print(f"\nWin probability: {result['recommended_win_probability']:.1%}")
    else:
        print("\nWin probability: n/a, could not assemble a full valid lineup, check unsupported_slots above")

    if result["differs_from_points_max"]:
        print(
            f"\nNote: this differs from the highest-raw-points lineup ({result['points_max_total']:.1f} pts). "
            f"The flex slot is doing real work here, trading a little mean for a better win probability "
            f"given this specific matchup, not just stacking points."
        )

    print("\nBench:")
    for p in sorted(result["bench"], key=lambda p: -p["mean"]):
        print(f"  {p['full_name']:<20} {p['position']:<3} mean={p['mean']:>5.1f}")


def cmd_faab(args: argparse.Namespace) -> None:
    context.require_config()
    conn = get_db()
    me = context.my_roster_id(conn)

    stats_season = context.stats_season(conn, args.season)

    try:
        result = weekly.faab_recommendation(conn, stats_season, me, args.player)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    print(f"FAAB recommendation for {result['player']} ({result['position']})")
    if result["is_rostered"]:
        print("Warning: this player is already on a roster in your league, not actually a free agent right now.")
    budget_note = " (league settings carry no waiver_budget, assumed Sleeper's default)" if result["budget_is_default"] else ""
    print(
        f"\nRemaining budget: ${result['remaining_budget']} of ${result['total_budget']}{budget_note}, "
        f"{result['weeks_left']} weeks left before the playoffs."
    )
    print(
        f"Win-now value: {result['target_win_now_value']:.1f} "
        f"({result['percentile_among_available']:.0f}th percentile among players actually available on waivers, "
        f"not everyone in the league)."
    )
    print(
        f"Base pace, remaining budget split evenly across the weeks left: ${result['base_per_week_budget']:.2f}/week, "
        f"scaled ×{result['value_multiplier']:.2f} for this target's value."
    )
    print(f"\nSuggested bid: ${result['suggested_bid']}")


def cmd_digest(args: argparse.Namespace) -> None:
    context.require_config()
    conn = get_db()
    me = context.my_roster_id(conn)

    stats_season = context.stats_season(conn, args.season)
    vegas_season = context.vegas_season(conn, args.vegas_season)

    with SleeperClient(conn) as client:
        client.sync_matchups(args.week)

    print(f"=== Week {args.week} digest, {stats_season} season FPPG basis, {vegas_season} season Vegas lines ===\n")

    try:
        lineup = weekly.optimize_lineup(conn, stats_season, vegas_season, args.week, me)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    print("Start:")
    for p in lineup["recommended_lineup"]:
        flag = f"  ({p['injury_status']})" if p["injury_status"] else ""
        wind_note = ""
        if p["team"] and not p["on_bye"]:
            w = weather.game_wind_forecast(vegas_season, args.week, p["team"])
            if w["status"] == "ok" and w["flag"]:
                wind_note = f"  WIND {w['wind_mph']:.0f} mph at {w['stadium']}"
        bye_note = "  BYE WEEK" if p["on_bye"] else ""
        print(f"  {p['full_name']:<20} {p['position']:<3} mean={p['mean']:>5.1f}{flag}{wind_note}{bye_note}")

    if lineup["recommended_win_probability"] is not None:
        print(f"\nWin probability: {lineup['recommended_win_probability']:.1%}")
    if lineup["differs_from_points_max"]:
        print("Chosen over the pure-points lineup for a better win probability against this week's specific opponent.")

    print("\nSit (top bench by projection):")
    for p in sorted(lineup["bench"], key=lambda p: -p["mean"])[:5]:
        print(f"  {p['full_name']:<20} {p['position']:<3} mean={p['mean']:>5.1f}")

    try:
        targets = weekly.top_faab_targets(conn, stats_season, me)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)
    print("\nFAAB targets (highest win-now value actually available on waivers right now):")
    for bid in targets:
        print(f"  {bid['player']:<20} {bid['position']:<3} value={bid['target_win_now_value']:>5.1f}  suggested bid ${bid['suggested_bid']}")

    print("\nThis is DRAFT-heuristic math throughout (see matchup.py, PLANNING.md), not a calibrated prediction.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dynasty-agent", description="Dynasty fantasy football agent for a Sleeper league."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser(
        "init", help="Set up .env for your own Sleeper league: username, user_id, league_id, draft_id."
    )
    init_parser.add_argument("--username", required=True, help="Your Sleeper username.")
    init_parser.add_argument(
        "--league-id", default=None, help="Pick a specific league if --username is in more than one this season."
    )
    init_parser.set_defaults(func=cmd_init)

    sub.add_parser(
        "sync",
        help="Refresh players, league, rosters, users, traded picks, nfl state, and market values.",
    ).set_defaults(func=cmd_sync)

    sub.add_parser(
        "roster",
        help="Print the current roster with age, position, team, market value, and 30-day trend.",
    ).set_defaults(func=cmd_roster)

    ingest_parser = sub.add_parser(
        "ingest-nflverse", help="Cache nflverse files and derive weekly player metrics for a season."
    )
    ingest_parser.add_argument("--season", type=int, required=True)
    ingest_parser.add_argument(
        "--force", action="store_true",
        help="Re-download this season's files even if cached. Automatic for the current NFL season.",
    )
    ingest_parser.set_defaults(func=cmd_ingest_nflverse)

    ingest_draft_parser = sub.add_parser(
        "ingest-draft-data",
        help="[Phase 4] Cache and upsert real NFL draft picks and combine testing results "
        "(nflverse draft_picks + combine, whole-history flat files, not season-scoped).",
    )
    ingest_draft_parser.add_argument(
        "--force", action="store_true", help="Re-download even if already cached, to pick up nflverse's latest update."
    )
    ingest_draft_parser.set_defaults(func=cmd_ingest_draft_data)

    college_parser = sub.add_parser(
        "ingest-college",
        help="[Phase 4] Cache and ingest college production (ESPN box scores via sportsdataverse) for a season or a range.",
    )
    college_parser.add_argument("--season", type=int, required=True, help="First (or only) college season to ingest.")
    college_parser.add_argument("--through", type=int, default=None, help="Last season, to ingest a range, e.g. 2008 through 2025.")
    college_parser.add_argument(
        "--force", action="store_true", help="Re-download even if cached, needed for the in-progress college season."
    )
    college_parser.set_defaults(func=cmd_ingest_college)

    sub.add_parser(
        "fit-prospect-model",
        help="[Phase 4] Fit the rookie prospect model against real 2018-2023 draft class outcomes and report its accuracy.",
    ).set_defaults(func=cmd_fit_prospect_model)

    board_parser = sub.add_parser(
        "prospect-board",
        help="[Phase 4] Ranked rookie board. post-draft: after the NFL draft, ranked by draft capital. "
        "pre-draft: before it, ranked on college production, a WEAK signal, labeled as such.",
    )
    board_parser.add_argument("--mode", choices=["post-draft", "pre-draft"], required=True)
    board_parser.add_argument("--class", dest="draft_class", type=int, required=True, help="NFL draft year, e.g. 2027.")
    board_parser.add_argument("--limit", type=int, default=40, help="How many players to show (default 40).")
    board_parser.set_defaults(func=cmd_prospect_board)

    refresh_parser = sub.add_parser(
        "refresh",
        help="Bring everything current in one run: Sleeper, FantasyCalc, this season's NFL stats and matchups, and "
        "(by time of year) college stats, draft data, and the prospect model. Reports how old each source is.",
    )
    refresh_parser.add_argument("--verbose", action="store_true", help="Print full tracebacks for failed steps.")
    refresh_parser.set_defaults(func=cmd_refresh)

    picks_parser = sub.add_parser(
        "picks",
        help="[Phase 4] Your rookie picks: projected slot, what that slot has really returned, FantasyCalc's price, "
        "and buy/hold/sell against the rookies that slot bought last year.",
    )
    picks_parser.add_argument("--all", action="store_true", help="Every team's picks, not just yours.")
    picks_parser.set_defaults(func=cmd_picks)

    sub.add_parser(
        "taxi",
        help="[Phase 4] Which players to move to taxi or IR now to free bench spots, and whether next year's rookie "
        "draft forces a cut.",
    ).set_defaults(func=cmd_taxi)

    schedule_parser = sub.add_parser(
        "schedule",
        help="Run `refresh` automatically every day (macOS launchd). No flags: show the current schedule and last run.",
    )
    schedule_group = schedule_parser.add_mutually_exclusive_group()
    schedule_group.add_argument("--install", action="store_true", help="Schedule a daily refresh (see --at).")
    schedule_group.add_argument("--remove", action="store_true", help="Remove the daily refresh.")
    schedule_parser.add_argument("--at", default="06:00", help="24-hour local time for --install (default 06:00).")
    schedule_parser.set_defaults(func=cmd_schedule)

    calibrate_parser = sub.add_parser(
        "calibrate-blend",
        help="Backtest how much last season (and a rookie's projection) should count against this season's games.",
    )
    calibrate_parser.add_argument(
        "--season", type=int, default=None, help="Season to score against. Defaults to the latest complete one."
    )
    calibrate_parser.set_defaults(func=cmd_calibrate_blend)

    valuate_parser = sub.add_parser(
        "valuate",
        help="Print the roster with win-now value, three-year value, and the contend-or-rebuild verdict.",
    )
    valuate_parser.add_argument(
        "--season", type=int, default=None, help="Defaults to the most recently ingested season."
    )
    valuate_parser.set_defaults(func=cmd_valuate)

    trade_parser = sub.add_parser(
        "trade",
        help="Evaluate a proposed trade: both sides on win-now and three-year value, "
        "pick discounting, FantasyCalc arbitrage, and consolidation flags.",
    )
    trade_parser.add_argument("--send", action="append", metavar="PLAYER", help="A player you would send. Repeatable.")
    trade_parser.add_argument(
        "--send-pick", action="append", metavar="PICK",
        help="A pick you would send: 2027-1, '2027 1st', '2027 early 1st' or '2027 1.05'. Repeatable."
    )
    trade_parser.add_argument(
        "--receive", action="append", metavar="PLAYER", help="A player you would receive. Repeatable."
    )
    trade_parser.add_argument(
        "--receive-pick", action="append", metavar="PICK",
        help="A pick you would receive, written like --send-pick. Name a tier or slot for a tiered price. Repeatable."
    )
    trade_parser.add_argument(
        "--discount-rate", type=float, default=picks.valuation_discount_rate(),
        help=f"Per-year discount applied to future pick values beyond the base season "
        f"(default {picks.valuation_discount_rate():.2f}, set in picks.py).",
    )
    trade_parser.add_argument(
        "--season", type=int, default=None, help="Valuation basis season. Defaults to the most recently ingested season."
    )
    trade_parser.set_defaults(func=cmd_trade)

    predict_parser = sub.add_parser(
        "predict-matchup",
        help="[DRAFT] Estimate win probability between two arbitrary rosters (not necessarily your own "
        "league). A heuristic built from real season data, not a fitted or calibrated model, see matchup.py.",
    )
    predict_parser.add_argument("--team-a", action="append", metavar="PLAYER", help="A player on team A. Repeatable.")
    predict_parser.add_argument("--team-b", action="append", metavar="PLAYER", help="A player on team B. Repeatable.")
    predict_parser.add_argument(
        "--week", type=int, required=True, help="The NFL week to predict, used to look up real Vegas lines for that week."
    )
    predict_parser.add_argument(
        "--season", type=int, default=None,
        help="FPPG/variance baseline season. Defaults to the most recently ingested season.",
    )
    predict_parser.add_argument(
        "--vegas-season", type=int, default=None,
        help="Season --week's Vegas lines belong to. Defaults to the real current NFL season from the last sync.",
    )
    predict_parser.set_defaults(func=cmd_predict_matchup)

    optimize_parser = sub.add_parser(
        "optimize-lineup",
        help="The starting lineup that maximizes win probability against your real Sleeper opponent this week, "
        "not raw projected points.",
    )
    optimize_parser.add_argument("--week", type=int, required=True)
    optimize_parser.add_argument(
        "--season", type=int, default=None, help="FPPG/variance baseline season. Defaults to the most recently ingested season."
    )
    optimize_parser.add_argument(
        "--vegas-season", type=int, default=None,
        help="Season --week's Vegas lines belong to. Defaults to the real current NFL season from the last sync.",
    )
    optimize_parser.set_defaults(func=cmd_optimize_lineup)

    faab_parser = sub.add_parser(
        "faab", help="A sized FAAB bid for one waiver target, against your real remaining budget and weeks left."
    )
    faab_parser.add_argument("--player", required=True, metavar="PLAYER", help="Player name or Sleeper player_id.")
    faab_parser.add_argument(
        "--season", type=int, default=None, help="Valuation basis season. Defaults to the most recently ingested season."
    )
    faab_parser.set_defaults(func=cmd_faab)

    digest_parser = sub.add_parser(
        "digest", help="The weekly brief: recommended lineup, win probability, wind flags, and top bench options."
    )
    digest_parser.add_argument("--week", type=int, required=True)
    digest_parser.add_argument(
        "--season", type=int, default=None, help="FPPG/variance baseline season. Defaults to the most recently ingested season."
    )
    digest_parser.add_argument(
        "--vegas-season", type=int, default=None,
        help="Season --week's Vegas lines belong to. Defaults to the real current NFL season from the last sync.",
    )
    digest_parser.set_defaults(func=cmd_digest)

    args = parser.parse_args()
    try:
        args.func(args)
    except AgentError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
