"""Command-line entrypoint: `dynasty-agent <command>`."""

from __future__ import annotations

import argparse
import json
import sys

from dynasty_agent import market, nflverse, valuation
from dynasty_agent.config import SLEEPER_USER_ID
from dynasty_agent.db import get_db
from dynasty_agent.sleeper import SleeperClient


def cmd_sync(args: argparse.Namespace) -> None:
    conn = get_db()
    with SleeperClient(conn) as client:
        client.sync_all()
    market.sync_market_values(conn)
    print("Synced players, league, users, rosters, traded picks, nfl state, and market values.")


def cmd_roster(args: argparse.Namespace) -> None:
    conn = get_db()
    roster = conn.execute(
        "SELECT roster_id FROM rosters WHERE owner_id = ?", (SLEEPER_USER_ID,)
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


def _latest_ingested_season(conn) -> int | None:
    row = conn.execute("SELECT max(season) FROM weekly_stats").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def cmd_valuate(args: argparse.Namespace) -> None:
    conn = get_db()
    roster = conn.execute(
        "SELECT roster_id FROM rosters WHERE owner_id = ?", (SLEEPER_USER_ID,)
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
    conn = get_db()
    roster = conn.execute(
        "SELECT roster_id FROM rosters WHERE owner_id = ?", (SLEEPER_USER_ID,)
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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dynasty-agent", description="Dynasty fantasy football agent for a Sleeper league."
    )
    sub = parser.add_subparsers(dest="command", required=True)

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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
