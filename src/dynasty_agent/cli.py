"""Command-line entrypoint: `dynasty-agent <command>`."""

from __future__ import annotations

import argparse
import json
import sys

from dynasty_agent import calibration, college, config, crosswalk, market, matchup, nfl_extra, nflverse, prospects, simulate, sleeper, valuation, weather, weekly
from dynasty_agent.db import get_db
from dynasty_agent.sleeper import SleeperClient


def _require_config() -> None:
    missing = config.missing_config()
    if missing:
        print(
            f"Missing config: {', '.join(missing)}. Run "
            f"`dynasty-agent init --username <your sleeper username>` to set it up, "
            f"or copy .env.example to .env and fill it in by hand.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def cmd_init(args: argparse.Namespace) -> None:
    user = sleeper.lookup_user(args.username)
    if user is None:
        print(f"No Sleeper user found for username '{args.username}'.", file=sys.stderr)
        raise SystemExit(1)
    user_id = user["user_id"]

    season = current_season = sleeper.current_nfl_season()
    leagues = sleeper.list_leagues_for_season(user_id, season)
    if not leagues:
        print(f"'{args.username}' has no {season} NFL leagues on Sleeper.", file=sys.stderr)
        raise SystemExit(1)

    if args.league_id:
        league = next((league for league in leagues if league["league_id"] == args.league_id), None)
        if league is None:
            print(f"'{args.username}' is not in a {season} league with id {args.league_id}.", file=sys.stderr)
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
    print(f"League: {league['name']} ({league['league_id']}, {current_season} season)")
    print("Next: `dynasty-agent sync`")


def cmd_sync(args: argparse.Namespace) -> None:
    _require_config()
    conn = get_db()
    with SleeperClient(conn) as client:
        client.sync_all()
    market.sync_market_values(conn)
    print("Synced players, league, users, rosters, traded picks, nfl state, and market values.")


def cmd_roster(args: argparse.Namespace) -> None:
    _require_config()
    conn = get_db()
    roster = conn.execute(
        "SELECT roster_id FROM rosters WHERE owner_id = ?", (config.SLEEPER_USER_ID,)
    ).fetchone()
    if roster is None:
        print("No roster found for this user. Run `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)

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
        (roster["roster_id"],),
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
    print(nflverse.ingest_season(conn, args.season, scoring_settings))


def cmd_backfill_history(args: argparse.Namespace) -> None:
    conn = get_db()
    league = conn.execute("SELECT scoring_settings_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if league is None:
        print("No league data cached yet. Run `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)
    scoring_settings = json.loads(league["scoring_settings_json"])

    for season in range(args.start_season, args.end_season + 1):
        print(nflverse.ingest_season(conn, season, scoring_settings))
        injury_rows = nfl_extra.ingest_injuries(conn, season)
        print(f"  + {injury_rows} real injury-report rows for {season}.")


def cmd_calibrate_matchup_model(args: argparse.Namespace) -> None:
    conn = get_db()
    try:
        summary = calibration.fit_and_store_calibration(conn, args.start_season, args.end_season)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Calibrated the matchup win-probability model against {summary['sample_size']} real games "
          f"({summary['start_season']}-{summary['end_season']}).")
    print(f"Fitted correction: platt_a={summary['platt_a']:.4f}, platt_b={summary['platt_b']:.4f}")
    if summary["platt_a"] <= 0:
        print("Warning: platt_a <= 0, see stderr above, treat this fit as suspect.", file=sys.stderr)
    print()
    print(f"{'Metric':<12} {'Before':>10} {'After':>10}")
    print(f"{'Brier':<12} {summary['brier_before']:>10.4f} {summary['brier_after']:>10.4f}")
    print(f"{'Log-loss':<12} {summary['log_loss_before']:>10.4f} {summary['log_loss_after']:>10.4f}")
    print(f"{'Accuracy':<12} {summary['accuracy_before']:>10.1%} {summary['accuracy_after']:>10.1%}")


def cmd_ingest_draft_data(args: argparse.Namespace) -> None:
    conn = get_db()
    print(prospects.ingest_draft_data(conn, force=args.force))


def cmd_sync_player_crosswalk(args: argparse.Namespace) -> None:
    conn = get_db()
    rows = crosswalk.sync_player_id_crosswalk(conn)
    print(f"Synced {rows} player id crosswalk rows from dynastyprocess/data.")


def cmd_ingest_college_data(args: argparse.Namespace) -> None:
    conn = get_db()
    print(college.ingest_college_data(conn, args.start_season, args.end_season, force=args.force))


def cmd_prospect_board(args: argparse.Namespace) -> None:
    conn = get_db()

    # Validated up front, before any board output: a flag this run cannot
    # honor should fail fast, not after already having printed the board.
    if args.taxi:
        _require_config()
    if args.cross_reference_picks and args.mode != "post-draft":
        print("Error: --cross-reference-picks needs --mode post-draft, FantasyCalc has no pre-draft rookie pricing.", file=sys.stderr)
        raise SystemExit(1)

    try:
        board = prospects.prospect_board(conn, args.draft_year, args.mode, position=args.position, limit=args.limit)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Prospect board, {args.draft_year} draft class, {args.mode} mode.")
    if args.mode == "pre-draft":
        print(
            "Ranked by real 247Sports composite recruiting grade, the most recent ingested recruiting "
            "class at or before this draft year. Not filtered by declared-for-the-draft status, this "
            "project has no source for that. College box-score production (dominator rating, breakout "
            "age) is not computed yet, a confirmed data-source dead end, see college.py.\n"
        )
        header = f"{'Player':<24} {'Pos':<4} {'School':<22} {'Class':<7} {'Grade':>6} {'Stars':>6}"
        print(header)
        print("-" * len(header))
        for p in board:
            grade = f"{p['recruit_grade']:.1f}" if p["recruit_grade"] is not None else "-"
            stars = str(p["recruit_stars"]) if p["recruit_stars"] is not None else "-"
            print(
                f"{p['player_name']:<24} {p['position'] or '':<4} {(p['school'] or '')[:22]:<22} "
                f"{p['season']:<7} {grade:>6} {stars:>6}"
            )
    else:
        print("Ranked by real draft capital, athletic testing shown alongside.\n")
        header = f"{'Player':<24} {'Pos':<4} {'College':<18} {'Rnd':>3} {'Pick':>4} {'Team':<5} {'Athl%':>6}"
        print(header)
        print("-" * len(header))
        for p in board:
            athl = f"{p['athleticism_score']:.0f}" if p["athleticism_score"] is not None else "-"
            print(
                f"{p['player_name']:<24} {p['position'] or '':<4} {(p['college'] or '')[:18]:<18} "
                f"{p['round'] or '':>3} {p['pick'] or '':>4} {p['team'] or '':<5} {athl:>6}"
            )

    if args.taxi:
        roster = conn.execute("SELECT roster_id FROM rosters WHERE owner_id = ?", (config.SLEEPER_USER_ID,)).fetchone()
        if roster is None:
            print("\nNo roster found for this user, skipping taxi recommendations.", file=sys.stderr)
        else:
            taxi = prospects.taxi_stash_recommendations(conn, roster["roster_id"], args.draft_year)
            print(f"\nTaxi slots: {taxi['open_taxi_slots']} open.")
            for r in taxi["taxi_eligible_rostered"]:
                print(f"  {r['full_name']:<24} {r['position'] or ''}")

    if args.cross_reference_picks:
        for round_num in (1, 2, 3):
            xref = prospects.prospect_pick_cross_reference(conn, args.draft_year, round_num, discount_rate=0.20)
            model_str = f"{xref['pick_value_estimate']['model_value']:.0f}" if xref["pick_value_estimate"]["model_value"] is not None else "-"
            print(f"\n{args.draft_year} round {round_num}: model value {model_str}, top available {xref['top_available_at_round'] or '-'}")


def _latest_ingested_season(conn) -> int | None:
    row = conn.execute("SELECT max(season) FROM weekly_stats").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def cmd_valuate(args: argparse.Namespace) -> None:
    _require_config()
    conn = get_db()
    roster = conn.execute(
        "SELECT roster_id FROM rosters WHERE owner_id = ?", (config.SLEEPER_USER_ID,)
    ).fetchone()
    if roster is None:
        print("No roster found for this user. Run `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)

    season = args.season or _latest_ingested_season(conn)
    if season is None:
        print("No nflverse data ingested yet. Run `dynasty-agent ingest-nflverse --season <year>` first.", file=sys.stderr)
        raise SystemExit(1)

    valuations = valuation.player_valuations(conn, season)
    print(f"Valuation basis: {season} season (most recently ingested nflverse data).")
    print(
        "Situation score: average of QB passing EPA/game, team pass rate over expected, and sack rate "
        "allowed (inverted), each percentile-ranked against all 32 NFL teams. Not a full offensive line "
        "grade, real OL grades are paywalled; this is the public proxy.\n"
    )

    my_players = conn.execute(
        "SELECT rp.player_id, rp.slot, p.full_name, p.position, p.age FROM roster_players rp "
        "JOIN players p ON p.player_id = rp.player_id WHERE rp.roster_id = ?",
        (roster["roster_id"],),
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
            f"{v['win_now_value']:>8.1f} {v['three_year_value']:>8.1f}"
        )

    verdict = valuation.contend_or_rebuild(conn, season, roster["roster_id"])
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


def _parse_pick(spec: str) -> tuple[int, int]:
    """Parse 'SEASON-ROUND', e.g. '2027-1', into (season, round)."""
    parts = spec.split("-")
    if len(parts) != 2:
        raise ValueError(f"pick must look like '2027-1' (season-round), got '{spec}'")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"pick must look like '2027-1' (season-round), got '{spec}'")


def cmd_trade(args: argparse.Namespace) -> None:
    _require_config()
    conn = get_db()
    roster = conn.execute(
        "SELECT roster_id FROM rosters WHERE owner_id = ?", (config.SLEEPER_USER_ID,)
    ).fetchone()
    if roster is None:
        print("No roster found for this user. Run `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)

    season = args.season or _latest_ingested_season(conn)
    if season is None:
        print("No nflverse data ingested yet. Run `dynasty-agent ingest-nflverse --season <year>` first.", file=sys.stderr)
        raise SystemExit(1)

    try:
        send_picks = [_parse_pick(p) for p in (args.send_pick or [])]
        receive_picks = [_parse_pick(p) for p in (args.receive_pick or [])]
        result = valuation.evaluate_trade(
            conn,
            season,
            roster["roster_id"],
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
            note = "" if p["has_data"] else "  (no games this season, my-model valuation is 0)"
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
                f"model {model_str:>6}  market {market_str:>6}  arbitrage {arb_str:>7}"
            )
        print(
            f"  totals: win-now (players only) {side['win_now_total']:.1f}, "
            f"3yr mine (players only) {side['player_three_year_total']:.1f}, "
            f"market value (players + picks, comparable) {side['market_value_total']:.0f}"
        )

    print(f"Trade evaluation, {season} season basis, pick discount rate {args.discount_rate:.0%} per year.")
    print(
        "Win-now and 3yr(mine) are this league's own formula, players only, picks can't help you win "
        "this year so they don't appear there. Market value is FantasyCalc's own pricing for players plus "
        "my discount-adjusted pick model, the only number below that's comparable across players and picks "
        "together.\n"
    )
    print_side("You send", result["sent"])
    print()
    print_side("You receive", result["received"])
    print()
    print(f"Net win-now (players only): {result['win_now_delta']:+.1f}")
    print(f"Net 3yr, mine (players only): {result['player_three_year_delta']:+.1f}")
    print(f"Net market value (players + picks): {result['market_value_delta']:+.0f}")
    print(f"Your posture: {result['posture'].upper()} ({result['posture_confidence']})")
    print(f"Fit: {result['fit']}")
    if result["consolidation"]:
        print(f"Note: {result['consolidation']}")


def _resolve_vegas_season(conn, explicit_vegas_season: int | None) -> int:
    """The season --week's Vegas lines belong to. The real current NFL
    season from the last sync when not given explicitly, never inferred
    from the FPPG baseline season: week 1 of a completed season already
    has real closing lines from last year's game, so any presence-based
    fallback silently prices the wrong year. See matchup.predict_matchup's
    docstring for the bug this replaced."""
    if explicit_vegas_season is not None:
        return explicit_vegas_season
    state_row = conn.execute("SELECT season FROM nfl_state ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if state_row is None:
        print(
            "No synced NFL state to determine the current season. Run `dynasty-agent sync` first, "
            "or pass --vegas-season explicitly.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return int(state_row["season"])


def cmd_predict_matchup(args: argparse.Namespace) -> None:
    conn = get_db()
    if conn.execute("SELECT 1 FROM players LIMIT 1").fetchone() is None:
        print("No player data yet. Run `dynasty-agent init` and `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)

    season = args.season or _latest_ingested_season(conn)
    if season is None:
        print("No nflverse data ingested yet. Run `dynasty-agent ingest-nflverse --season <year>` first.", file=sys.stderr)
        raise SystemExit(1)

    vegas_season = _resolve_vegas_season(conn, args.vegas_season)
    calibration_params = calibration.current_calibration(conn)

    try:
        result = matchup.predict_matchup(
            conn, season, vegas_season, args.week, args.team_a or [], args.team_b or [],
            calibration_params=calibration_params,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Matchup prediction, {season} season FPPG basis, week {args.week} of the {vegas_season} season.")
    if calibration_params:
        print("Raw heuristic, calibrated against real historical games (see calibrate-matchup-model). Both shown below.")
    else:
        print(
            "DRAFT MODEL, a heuristic, not a calibrated prediction. Run `dynasty-agent calibrate-matchup-model` "
            "to fit a real calibration against historical games; see matchup.py and PLANNING.md."
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
            if p["games"] == 0:
                data_note = "  NO DATA THIS SEASON"
            elif p["thin_sample"]:
                data_note = f"  only {p['games']} game, variance not estimable, counted as 0"
            else:
                data_note = f"  over {p['games']} games"
            vegas_note = f", vegas x{p['vegas_multiplier']:.2f}" if p["vegas_multiplier"] != 1.0 else ""
            print(
                f"  {p['full_name']:<20} {p['position'] or '':<4} {p['team'] or '':<4} "
                f"{p['adjusted_mean']:>5.1f} avg{vegas_note}{flag}{data_note}"
            )
        print(f"  Team mean: {side['mean']:.1f}, std dev: {side['variance'] ** 0.5:.1f}\n")

    print_side("Team A", result["team_a"])
    print_side("Team B", result["team_b"])

    print(f"Projected margin (A - B): {result['mean_diff']:+.1f}, combined std dev: {result['std_diff']:.1f}")
    print(f"Team A win probability (raw): {result['win_probability_a']:.1%}")
    print(f"Team B win probability (raw): {result['win_probability_b']:.1%}")
    if result["win_probability_a_calibrated"] is not None:
        print(f"Team A win probability (calibrated): {result['win_probability_a_calibrated']:.1%}")
        print(f"Team B win probability (calibrated): {result['win_probability_b_calibrated']:.1%}")


def cmd_optimize_lineup(args: argparse.Namespace) -> None:
    _require_config()
    conn = get_db()
    roster = conn.execute("SELECT roster_id FROM rosters WHERE owner_id = ?", (config.SLEEPER_USER_ID,)).fetchone()
    if roster is None:
        print("No roster found for this user. Run `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)

    stats_season = args.season or _latest_ingested_season(conn)
    if stats_season is None:
        print("No nflverse data ingested yet. Run `dynasty-agent ingest-nflverse --season <year>` first.", file=sys.stderr)
        raise SystemExit(1)
    vegas_season = _resolve_vegas_season(conn, args.vegas_season)

    with SleeperClient(conn) as client:
        client.sync_matchups(args.week)

    calibration_params = calibration.current_calibration(conn)
    try:
        result = weekly.optimize_lineup(
            conn, stats_season, vegas_season, args.week, roster["roster_id"], calibration_params=calibration_params
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    if calibration_params is None:
        print("Note: no calibration fitted yet, win probability below is the raw heuristic. Run "
              "`dynasty-agent calibrate-matchup-model` first for a calibrated number.\n")

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
        print(f"\nWin probability (raw): {result['recommended_win_probability']:.1%}")
        if result["recommended_win_probability_calibrated"] is not None:
            print(f"Win probability (calibrated): {result['recommended_win_probability_calibrated']:.1%}")
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
    _require_config()
    conn = get_db()
    roster = conn.execute("SELECT roster_id FROM rosters WHERE owner_id = ?", (config.SLEEPER_USER_ID,)).fetchone()
    if roster is None:
        print("No roster found for this user. Run `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)

    stats_season = args.season or _latest_ingested_season(conn)
    if stats_season is None:
        print("No nflverse data ingested yet. Run `dynasty-agent ingest-nflverse --season <year>` first.", file=sys.stderr)
        raise SystemExit(1)

    try:
        result = weekly.faab_recommendation(conn, stats_season, roster["roster_id"], args.player)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    print(f"FAAB recommendation for {result['player']} ({result['position']})")
    if result["is_rostered"]:
        print("Warning: this player is already on a roster in your league, not actually a free agent right now.")
    print(f"\nRemaining budget: ${result['remaining_budget']}, {result['weeks_left']} weeks left before the playoffs.")
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
    _require_config()
    conn = get_db()
    roster = conn.execute("SELECT roster_id FROM rosters WHERE owner_id = ?", (config.SLEEPER_USER_ID,)).fetchone()
    if roster is None:
        print("No roster found for this user. Run `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)

    stats_season = args.season or _latest_ingested_season(conn)
    if stats_season is None:
        print("No nflverse data ingested yet. Run `dynasty-agent ingest-nflverse --season <year>` first.", file=sys.stderr)
        raise SystemExit(1)
    vegas_season = _resolve_vegas_season(conn, args.vegas_season)

    with SleeperClient(conn) as client:
        client.sync_matchups(args.week)

    print(f"=== Week {args.week} digest, {stats_season} season FPPG basis, {vegas_season} season Vegas lines ===\n")

    calibration_params = calibration.current_calibration(conn)
    try:
        lineup = weekly.optimize_lineup(
            conn, stats_season, vegas_season, args.week, roster["roster_id"], calibration_params=calibration_params
        )
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
        print(f"\nWin probability (raw): {lineup['recommended_win_probability']:.1%}")
        if lineup["recommended_win_probability_calibrated"] is not None:
            print(f"Win probability (calibrated): {lineup['recommended_win_probability_calibrated']:.1%}")
    if lineup["differs_from_points_max"]:
        print("Chosen over the pure-points lineup for a better win probability against this week's specific opponent.")

    print("\nSit (top bench by projection):")
    for p in sorted(lineup["bench"], key=lambda p: -p["mean"])[:5]:
        print(f"  {p['full_name']:<20} {p['position']:<3} mean={p['mean']:>5.1f}")

    print("\nFAAB targets (highest win-now value actually available on waivers right now):")
    for bid in weekly.top_faab_targets(conn, stats_season, roster["roster_id"]):
        print(f"  {bid['player']:<20} {bid['position']:<3} value={bid['target_win_now_value']:>5.1f}  suggested bid ${bid['suggested_bid']}")

    if calibration_params is None:
        print("\nNo calibration fitted yet, win probability above is the raw heuristic. Run "
              "`dynasty-agent calibrate-matchup-model` for a calibrated number (see matchup.py, PLANNING.md).")


def cmd_simulate_season(args: argparse.Namespace) -> None:
    _require_config()
    conn = get_db()

    stats_season = args.season or _latest_ingested_season(conn)
    if stats_season is None:
        print("No nflverse data ingested yet. Run `dynasty-agent ingest-nflverse --season <year>` first.", file=sys.stderr)
        raise SystemExit(1)
    vegas_season = _resolve_vegas_season(conn, args.vegas_season)

    league_row = conn.execute("SELECT settings_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if league_row is None:
        print("No league data cached yet. Run `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)
    playoff_week_start = json.loads(league_row["settings_json"]).get("playoff_week_start", 15)

    state_row = conn.execute("SELECT week FROM nfl_state ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if state_row is None:
        print("No synced NFL state. Run `dynasty-agent sync` first.", file=sys.stderr)
        raise SystemExit(1)
    from_week = state_row["week"] or 1

    with SleeperClient(conn) as client:
        for week in range(from_week, playoff_week_start):
            client.sync_matchups(week)

    calibration_params = calibration.current_calibration(conn)
    try:
        result = simulate.simulate_season(
            conn, stats_season, vegas_season, from_week,
            n_simulations=args.simulations, seed=args.seed, calibration_params=calibration_params,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)

    my_roster_row = conn.execute("SELECT roster_id FROM rosters WHERE owner_id = ?", (config.SLEEPER_USER_ID,)).fetchone()
    my_roster_id = my_roster_row["roster_id"] if my_roster_row else None

    print(
        f"Season simulation, {result['n_simulations']} runs, from week {result['from_week']} through "
        f"{result['playoff_week_start'] - 1} ({result['real_matchups_simulated']} real remaining matchups), "
        f"top {result['playoff_teams']} make the playoffs."
    )
    if not result["calibration_used"]:
        print("No calibration fitted yet, odds below use the raw heuristic. Run `dynasty-agent calibrate-matchup-model` first.")
    print()

    names = {
        r["roster_id"]: (r["team_name"] or r["display_name"] or f"Roster {r['roster_id']}")
        for r in conn.execute(
            "SELECT r.roster_id, u.team_name, u.display_name FROM rosters r LEFT JOIN users u ON u.user_id = r.owner_id"
        ).fetchall()
    }

    header = f"{'Team':<22} {'Record':>8} {'AvgWins':>8} {'PlayoffOdds':>12} {'AvgRank':>8} {'LastOdds':>9}"
    print(header)
    print("-" * len(header))
    for rid, t in sorted(result["teams"].items(), key=lambda kv: -kv[1]["playoff_odds"]):
        marker = " *" if rid == my_roster_id else "  "
        name = names.get(rid, f"Roster {rid}")[:20]
        print(
            f"{marker}{name:<20} {t['current_wins']:>8} {t['avg_final_wins']:>8.1f} "
            f"{t['playoff_odds']:>11.1%} {t['avg_final_rank']:>8.1f} {t['last_place_odds']:>8.1%}"
        )
    print("\n* = your team")


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
    ingest_parser.set_defaults(func=cmd_ingest_nflverse)

    backfill_parser = sub.add_parser(
        "backfill-history",
        help="[Phase 5] Ingest nflverse weekly stats and real injury history across a range of seasons, "
        "the data layer calibrate-matchup-model needs. A real multi-hundred-MB download, several minutes.",
    )
    backfill_parser.add_argument("--start-season", type=int, required=True)
    backfill_parser.add_argument("--end-season", type=int, required=True)
    backfill_parser.set_defaults(func=cmd_backfill_history)

    calibrate_parser = sub.add_parser(
        "calibrate-matchup-model",
        help="[Phase 5] Backtest predict-matchup's win-probability heuristic against real historical games "
        "and fit a calibration correction. Requires backfill-history to have run first.",
    )
    calibrate_parser.add_argument("--start-season", type=int, default=2010)
    calibrate_parser.add_argument("--end-season", type=int, default=2025)
    calibrate_parser.set_defaults(func=cmd_calibrate_matchup_model)

    ingest_draft_parser = sub.add_parser(
        "ingest-draft-data",
        help="[Phase 4] Cache and upsert real NFL draft picks and combine testing results "
        "(nflverse draft_picks + combine, whole-history flat files, not season-scoped).",
    )
    ingest_draft_parser.add_argument(
        "--force", action="store_true", help="Re-download even if already cached, to pick up nflverse's latest update."
    )
    ingest_draft_parser.set_defaults(func=cmd_ingest_draft_data)

    sub.add_parser(
        "sync-player-crosswalk",
        help="[Phase 4] Sync the player id crosswalk (Sleeper/gsis/pfr/cfbref/espn/yahoo ids) from dynastyprocess/data.",
    ).set_defaults(func=cmd_sync_player_crosswalk)

    ingest_college_parser = sub.add_parser(
        "ingest-college-data",
        help="[Phase 4] Cache and derive college recruiting, team talent, returning production, and player "
        "production (dominator rating) for a range of college football seasons.",
    )
    ingest_college_parser.add_argument("--start-season", type=int, required=True)
    ingest_college_parser.add_argument("--end-season", type=int, required=True)
    ingest_college_parser.add_argument(
        "--force", action="store_true", help="Re-download even if already cached."
    )
    ingest_college_parser.set_defaults(func=cmd_ingest_college_data)

    prospect_board_parser = sub.add_parser(
        "prospect-board",
        help="[Phase 4] A ranked rookie prospect board: real draft capital and athletic testing "
        "(post-draft mode) or college production and recruiting pedigree (pre-draft mode).",
    )
    prospect_board_parser.add_argument("--draft-year", type=int, required=True)
    prospect_board_parser.add_argument("--mode", choices=["pre-draft", "post-draft"], required=True)
    prospect_board_parser.add_argument("--position", default=None, help="Filter to one position, e.g. WR.")
    prospect_board_parser.add_argument("--limit", type=int, default=20)
    prospect_board_parser.add_argument(
        "--taxi", action="store_true", help="Also show open taxi slots and how your taxi-eligible rookies rank."
    )
    prospect_board_parser.add_argument(
        "--cross-reference-picks", action="store_true",
        help="Also show each round's FantasyCalc-anchored pick value against the top available name at that round. "
        "post-draft mode only.",
    )
    prospect_board_parser.set_defaults(func=cmd_prospect_board)

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
        "--send-pick", action="append", metavar="SEASON-ROUND", help="A pick you would send, e.g. 2027-1. Repeatable."
    )
    trade_parser.add_argument(
        "--receive", action="append", metavar="PLAYER", help="A player you would receive. Repeatable."
    )
    trade_parser.add_argument(
        "--receive-pick", action="append", metavar="SEASON-ROUND", help="A pick you would receive, e.g. 2027-1. Repeatable."
    )
    trade_parser.add_argument(
        "--discount-rate", type=float, default=0.20,
        help="Per-year discount applied to future pick values beyond the base season (default 0.20 = 20%% per year).",
    )
    trade_parser.add_argument(
        "--season", type=int, default=None, help="Valuation basis season. Defaults to the most recently ingested season."
    )
    trade_parser.set_defaults(func=cmd_trade)

    predict_parser = sub.add_parser(
        "predict-matchup",
        help="Estimate win probability between two arbitrary rosters (not necessarily your own league). "
        "Reports both the raw heuristic and, once calibrate-matchup-model has run, a calibrated number "
        "backtested against real historical games, see matchup.py.",
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

    simulate_parser = sub.add_parser(
        "simulate-season",
        help="[Phase 6] Monte Carlo simulation of the league's real remaining schedule: playoff odds, "
        "average final wins/rank, and last-place odds per team, using the calibrated matchup model if fitted.",
    )
    simulate_parser.add_argument(
        "--season", type=int, default=None, help="FPPG/variance baseline season. Defaults to the most recently ingested season."
    )
    simulate_parser.add_argument(
        "--vegas-season", type=int, default=None,
        help="Vegas lines season for remaining weeks. Defaults to the real current NFL season from the last sync.",
    )
    simulate_parser.add_argument("--simulations", type=int, default=10000)
    simulate_parser.add_argument("--seed", type=int, default=None, help="Fixed seed for reproducible runs.")
    simulate_parser.set_defaults(func=cmd_simulate_season)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
