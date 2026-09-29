"""The six things the chat can do, as tools a local model picks from, and the
dispatch that runs them on the real league.

Design rules (PLANNING.md, Phase 5; docs/phase5-chat-preview-plan.md):
- The model never supplies what Python knows: no week, season or roster
  arguments anywhere. context.py fills them in.
- Any reasonable argument shape is accepted: a name where a list was asked
  for, "2027 1st" or "2027-1" for a pick, a pick listed among players.
- An ambiguous or unknown name comes back as a question, never a guess.
- run_tool returns the formatter's text (the numbers block the user sees,
  identical to the CLI's) and a compact summary for the model to explain,
  every number in it rounded exactly as the numbers block shows it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from dynasty_agent import context, formatters, picks, taxi, valuation, weekly
from dynasty_agent.valuation import AmbiguousPlayer, PlayerNotFound

_LIST = {"type": "array", "items": {"type": "string"}}


def _tool(name: str, description: str, properties: dict | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": []},
    }}


TOOLS = [
    _tool("set_lineup", "The user's best starting lineup for this week against their real opponent, with win probability."),
    _tool(
        "evaluate_trade",
        "Evaluate a trade. send = what the USER gives away; receive = what the USER gets. "
        "\"I'm offered X for Y\" means the user receives X and sends Y.",
        {
            "send_players": {**_LIST, "description": "Players the user gives away."},
            "send_picks": {**_LIST, "description": "Draft picks the user gives away, e.g. '2027 1st'."},
            "receive_players": {**_LIST, "description": "Players the user gets."},
            "receive_picks": {**_LIST, "description": "Draft picks the user gets, e.g. '2028 2nd'."},
        },
    ),
    _tool("my_team", "The user's roster with each player's value, and whether to contend or rebuild."),
    _tool(
        "waiver_targets",
        "Free agents worth picking up and suggested FAAB bids. Give player for one specific player's bid.",
        {
            "player": {"type": "string", "description": "Optional: one free agent to size a bid for."},
            "position": {"type": "string", "description": "Only if the user names a position: QB, RB, WR or TE."},
        },
    ),
    _tool("pick_advice", "Buy, hold or sell advice for the user's rookie draft picks."),
    _tool("taxi_plan", "Which of the user's players to move to taxi or IR to free roster spots."),
]
TOOL_NAMES = [t["function"]["name"] for t in TOOLS]

# What a win probability or projection here is, carried into every answer
# that shows one: the matchup model is a heuristic never checked against
# real outcomes (see matchup.py).
DRAFT_CAVEAT = "Win probabilities are a draft heuristic, not a calibrated prediction."


@dataclass
class ToolResult:
    name: str
    numbers: str = ""
    compact: dict = field(default_factory=dict)
    clarification: str | None = None


def _as_list(value) -> list[str]:
    """A list of names from whatever the model sent: a list, one string, or
    one string holding several ("Puka Nacua and Jaxon Smith-Njigba")."""
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        for part in re.split(r",|\band\b|&|\+", str(item)):
            if part.strip():
                out.append(part.strip())
    return out


_LOOKS_LIKE_PICK = re.compile(r"\b20\d\d\b|'\d\d\b|\b(1st|2nd|3rd|first|second|third)\b.*\b(pick|round)\b|\bround\b", re.I)


def _split_players_and_picks(players: list[str], pick_specs: list[str]) -> tuple[list[str], list[str]]:
    """A pick the model listed among players moves to the picks."""
    real_players, specs = [], list(pick_specs)
    for p in players:
        (specs if _LOOKS_LIKE_PICK.search(p) else real_players).append(p)
    return real_players, specs


_HINT = re.compile(r"^(.*?)\s*\(([^)]*)\)\s*$")


def _pin(conn: sqlite3.Connection, name: str) -> str:
    """A name with a hint in parentheses, the way the model answers "Which
    Kenneth Walker?" ("Kenneth Walker (RB)", "Kenneth Walker (RB KC)"),
    resolves to that player's id when the hint picks exactly one of the
    candidates. Anything else passes through for resolve_player to handle."""
    m = _HINT.match(name)
    if not m:
        return name
    base, hint = m.group(1).strip(), {t.upper() for t in re.split(r"[\s,/]+", m.group(2)) if t}
    try:
        return valuation.resolve_player(conn, base)["player_id"]
    except AmbiguousPlayer as e:
        fits = [c for c in e.candidates if hint & {c["position"], c["team"] or "FA"}]
        if len(fits) == 1:
            return fits[0]["player_id"]
        raise
    except PlayerNotFound:
        return base


def _clarify(e: ValueError) -> str:
    if isinstance(e, AmbiguousPlayer):
        options = [f"{c['full_name']} ({c['position']} {c['team'] or 'FA'})" for c in e.candidates]
        return f"Which {e.query} do you mean: " + ", ".join(options[:-1]) + (" or " if len(options) > 1 else "") + options[-1] + "?"
    if isinstance(e, PlayerNotFound):
        return f"{e} Check the spelling, or give the full name?"
    return str(e)


def run_tool(conn: sqlite3.Connection, name: str, args: dict | None) -> ToolResult:
    """Run one tool on the real league. Raises AgentError for setup
    problems (no sync, no stats); returns a clarification for a question
    the user has to answer (which player, which pick)."""
    args = args or {}
    if name not in TOOL_NAMES:
        return ToolResult(name, clarification=f"I don't have a tool called {name}.")
    me = context.my_roster_id(conn)
    season = context.stats_season(conn)
    try:
        return _RUNNERS[name](conn, me, season, args)
    except (AmbiguousPlayer, PlayerNotFound) as e:
        return ToolResult(name, clarification=_clarify(e))
    except ValueError as e:
        if name == "evaluate_trade" and str(e).startswith(("'", "Couldn't read")):
            return ToolResult(name, clarification=str(e))  # a pick it couldn't read or that can't exist
        raise


# -- the six runners ---------------------------------------------------------------


def _set_lineup(conn, me, season, args) -> ToolResult:
    week = context.current_week(conn)
    vegas = context.vegas_season(conn)
    note = weekly.sync_matchups_or_note(conn, week)
    r = weekly.optimize_lineup(conn, season, vegas, week, me)
    prob = r["recommended_win_probability"]
    # The closest call: the best benched player, and the starter at his
    # position he'd replace (the lowest projected one).
    bench_note = None
    top_bench = max(r["bench"], key=lambda p: p["mean"], default=None)
    if top_bench is not None:
        same = [p for p in r["recommended_lineup"] if p["position"] == top_bench["position"]]
        if same:
            over = min(same, key=lambda p: p["mean"])
            bench_note = (f"{over['full_name']} ({over['mean']:.1f}) starts over {top_bench['full_name']} "
                          f"({top_bench['mean']:.1f})")
    compact = {
        "headline": (f"start the recommended lineup, win probability {prob:.1%} against a projected "
                     f"{r['opponent_mean']:.1f}" if prob is not None else "start the recommended lineup, the most projected points")
                    + (f"; {bench_note}" if bench_note else ""),
        "week": week,
        "starters": [f"{p['full_name']} {p['position']} {p['mean']:.1f}" + formatters.why_zero(p) for p in r["recommended_lineup"]],
        "win_probability": f"{prob:.1%}" if prob is not None else "n/a",
        "opponent_projected": f"{r['opponent_mean']:.1f}" if r["opponent_mean"] is not None else r["opponent_note"],
        "best_bench": [f"{p['full_name']} {p['position']} {p['mean']:.1f}" + formatters.why_zero(p)
                       for p in sorted(r["bench"], key=lambda p: -p["mean"])[:3]],
        "swaps_from_most_points": [f"{s['starts']} over {s['over']}" for s in r["swaps_from_points_max"]],
        "warnings": formatters.lineup_warnings(r) + ([note] if note else []),
        "caveat": DRAFT_CAVEAT,
    }
    return ToolResult("set_lineup", formatters.format_lineup(r, season, vegas, note), compact)


def _evaluate_trade(conn, me, season, args) -> ToolResult:
    send_players, send_specs = _split_players_and_picks(_as_list(args.get("send_players")), _as_list(args.get("send_picks")))
    receive_players, receive_specs = _split_players_and_picks(
        _as_list(args.get("receive_players")), _as_list(args.get("receive_picks"))
    )
    if not (send_players or send_specs or receive_players or receive_specs):
        return ToolResult("evaluate_trade", clarification="What would you send and what would you get back?")
    send_players = [_pin(conn, n) for n in send_players]
    receive_players = [_pin(conn, n) for n in receive_players]
    send_picks = [picks.parse_league_pick(conn, s) for s in send_specs]
    receive_picks = [picks.parse_league_pick(conn, s) for s in receive_specs]
    r = valuation.evaluate_trade(conn, season, me, send_players, send_picks, receive_players, receive_picks,
                                 picks.valuation_discount_rate())

    def side(s: dict) -> list[str]:
        return [f"{p['full_name']} (market {p['market_value']:.0f}, win-now {p['win_now_value']:.1f})"
                if p["market_value"] is not None else f"{p['full_name']} (no market price, win-now {p['win_now_value']:.1f})"
                for p in s["players"]] + [
            f"{pk['label']} (value {pk['model_value']:.0f})" if pk["model_value"] is not None else f"{pk['label']} (no price)"
            for pk in s["picks"]]

    compact = {
        "user_sends": side(r["sent"]),
        "user_receives": side(r["received"]),
        "net_market_value": f"{r['market_value_delta']:+.0f}",
        "net_win_now": f"{r['win_now_delta']:+.1f}",
        "posture": r["posture"],
        "fit": r["fit"],
        "consolidation": r["consolidation"],
        "warnings": r["warnings"],
        "units": "market values are FantasyCalc trade-value points, not dollars; win-now is points per game",
    }
    return ToolResult("evaluate_trade", formatters.format_trade(r, season), compact)


def _my_team(conn, me, season, args) -> ToolResult:
    team = valuation.my_team(conn, season, me)
    v = team["verdict"]
    valued = [p for p in team["players"] if p["valuation"]]
    top = sorted(valued, key=lambda p: -p["valuation"]["win_now_value"])[:5]
    compact = {
        "verdict": v["verdict"],
        "reason": v["reason"],
        "confidence": v["confidence"],
        "win_now_percentile": f"{v['win_now_percentile']:.0f}th",
        "three_year_percentile": f"{v['three_year_percentile']:.0f}th",
        "compared_against_teams": v["compared_against"],
        "games_played": v["games_played"],
        "most_valuable_now": [f"{p['full_name']} {p['position']} {p['valuation']['win_now_value']:.1f}" for p in top],
    }
    return ToolResult("my_team", formatters.format_my_team(team), compact)


def _waiver_targets(conn, me, season, args) -> ToolResult:
    player = (args.get("player") or "").strip()
    if player:
        player = _pin(conn, player)
        r = weekly.faab_recommendation(conn, season, me, player)
        compact = {
            "player": f"{r['player']} {r['position']}",
            "suggested_bid": f"${r['suggested_bid']}",
            "lineup_gain": f"+{r['lineup_gain']:.1f}" if r["lineup_gain"] > 0 else "none, would not start",
            "remaining_budget": f"${r['remaining_budget']}",
            "already_rostered": r["is_rostered"],
            "note": r["note"],
        }
        return ToolResult("waiver_targets", formatters.format_faab(r), compact)
    position = (args.get("position") or "").strip().upper() or None
    if position not in (None, "QB", "RB", "WR", "TE"):
        position = None
    targets = weekly.top_faab_targets(conn, season, me, position=position)
    top = targets[0] if targets else None
    compact = {
        "headline": (f"best pickup: {top['player']} {top['position']}, bid ${top['suggested_bid']}" if top and top["lineup_gain"] > 0
                     else "nobody on waivers would start for you; any pickup is depth only"),
        "targets": [
            f"{t['player']} {t['position']}: " + (f"+{t['lineup_gain']:.1f} to lineup" if t["lineup_gain"] > 0 else "depth only")
            + f", bid ${t['suggested_bid']}"
            for t in targets
        ],
        "remaining_budget": f"${targets[0]['remaining_budget']}" if targets else None,
        "note": targets[0]["note"] if targets else "nobody available has a win-now value this season",
    }
    return ToolResult("waiver_targets", formatters.format_waiver_targets(targets), compact)


def _pick_advice(conn, me, season, args) -> ToolResult:
    report = picks.pick_report(conn, season, me, context.latest_complete_season(conn), context.scoring_settings(conn), me)
    verdict = valuation.contend_or_rebuild(conn, season, me)
    compact = {
        "next_draft": report["next_draft"],
        "picks": [
            (f"{r['season']} {r['projected_slot']}: {r['advice']}, FantasyCalc {r['fantasycalc_price']:.0f} vs "
             f"{r['comparable_value']:.0f} for last year's rookies at that slot")
            if r["projected_slot"] and r["ratio"] is not None
            else f"{r['season']} round {r['round']}: {r['advice']}"
            for r in report["rows"]
        ],
        "posture": verdict["verdict"],
    }
    return ToolResult("pick_advice", formatters.format_picks(report, context.team_names(conn), me, verdict, False), compact)


def _taxi_plan(conn, me, season, args) -> ToolResult:
    r = taxi.plan(conn, season, me)
    moves = [f"move {m['player']['full_name']} to {m['to']} ({m['why']})" for m in r["moves"]]
    compact = {
        "recommendation": moves or ["no moves: every open taxi and IR slot is filled or has no eligible player"],
        "taxi_moves_locked_by_deadline": r["taxi_locked"],
        "next_season": (
            f"roster crunch, cut candidates: {', '.join(p['full_name'] for p in r['cut_candidates']) or 'none named'}"
            if r["overflow"] > 0 else "everyone fits next season, no cuts needed"
        ),
    }
    return ToolResult("taxi_plan", formatters.format_taxi(r), compact)


_RUNNERS = {
    "set_lineup": _set_lineup,
    "evaluate_trade": _evaluate_trade,
    "my_team": _my_team,
    "waiver_targets": _waiver_targets,
    "pick_advice": _pick_advice,
    "taxi_plan": _taxi_plan,
}
