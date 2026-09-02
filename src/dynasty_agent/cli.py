"""Command-line entrypoint: `dynasty-agent <command>`."""

from __future__ import annotations

import argparse
import json
import sys

from dynasty_agent import config, market, matchup, nflverse, prospects, sleeper, valuation, weather, weekly
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


def cmd_ingest_draft_data(args: argparse.Namespace) -> None:
    conn = get_db()
    print(prospects.ingest_draft_data(conn, force=args.force))


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
    print(f"Team A win probability: {result['win_probability_a']:.1%}")
    print(f"Team B win probability: {result['win_probability_b']:.1%}")


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

    try:
        result = weekly.optimize_lineup(conn, stats_season, vegas_season, args.week, roster["roster_id"])
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

    try:
        lineup = weekly.optimize_lineup(conn, stats_season, vegas_season, args.week, roster["roster_id"])
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

    print("\nFAAB targets (highest win-now value actually available on waivers right now):")
    for bid in weekly.top_faab_targets(conn, stats_season, roster["roster_id"]):
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
    args.func(args)


if __name__ == "__main__":
    main()
