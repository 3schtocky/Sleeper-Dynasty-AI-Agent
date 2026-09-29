"""Text for the six results the CLI prints and the chat shows: lineup, trade,
my team, waiver targets, picks, taxi. Each takes the plain dict its logic
function returns and gives back a string; nothing here reads the database
or the network, so the CLI and the chat print from one path."""

from __future__ import annotations

from dynasty_agent import blend, picks, prospect_model

ROOKIE_FOOTNOTE = (
    f"\n* Rookie: FPPG starts from the prospect model's projection from real draft capital (fit on 2018-2023 "
    f"classes), worth {blend.ROOKIE_PRIOR_GAMES:g} games, with his real games this season blended in on top. The "
    f"projection is points per game scheduled over a first 3 seasons, so it runs a little conservative next to a "
    f"veteran's per-game-played average. Run `prospect-board` for the inputs."
)


def basis_line(season: int) -> str:
    return (
        f"Valuation basis: the {season} season to date, blended with {season - 1}: last season's average counts as "
        f"{blend.VETERAN_PRIOR_GAMES:g} games and each real {season} game adds on top (chosen by backtest, see "
        f"`calibrate-blend`), so the new season takes over as it accumulates."
    )


def rookie_note(v: dict) -> str:
    if v.get("value_source") != "prospect_model":
        return ""
    games = f" + {v['games']} games" if v.get("games") else ""
    if v.get("undrafted"):
        return f"  * rookie, undrafted (priced as the last pick, outside the model's training data){games}"
    return f"  * rookie, projected from pick {v['draft_pick']}{games}"


def pick_price_note(pick: dict) -> str:
    """Which FantasyCalc price a traded pick got, when it matters: a next-draft
    pick's price swings with its tier (2027 1st: Early 4608, Late 2313)."""
    if pick["tier"] and pick["price_label"] and "(" in pick["price_label"]:
        return f"  priced as {pick['price_label']}"
    if pick["tier"] is None and pick["price_label"] and pick["season"] == pick["base_season"]:
        return "  tier unknown, FantasyCalc's average; name a tier or slot (e.g. '2027 early 1st', '2027 1.03')"
    return ""


def why_zero(p: dict) -> str:
    """Why a player projects to nothing this week, when that's the case."""
    if p["on_bye"]:
        return "  BYE WEEK"
    if p["injury_status"]:
        return f"  ({p['injury_status']})"
    return ""


def lineup_warnings(result: dict) -> list[str]:
    out = []
    if result["unsupported_slots"]:
        out.append(
            f"this tool has no projections for {', '.join(result['unsupported_slots'])} (nflverse weekly stats cover "
            f"QB/RB/WR/TE only); players there count as 0."
        )
    if result["empty_slots"]:
        out.append(f"no eligible player on your roster for {', '.join(result['empty_slots'])}; that slot starts empty.")
    return out


def gain_line(bid: dict) -> str:
    if bid["lineup_gain"] <= 0:
        return "Lineup gain: none, this player wouldn't start for you, so it's a token bid for depth only."
    return (
        f"Lineup gain: +{bid['lineup_gain']:.1f} to your best lineup's win-now total "
        f"(the biggest upgrade on waivers is +{bid['best_available_gain']:.1f})."
    )


def format_lineup(result: dict, stats_season: int, vegas_season: int, sync_note: str | None = None) -> str:
    week = result["week"]
    out: list[str] = []
    if sync_note:
        out.append(f"Note: {sync_note}\n")
    for line in lineup_warnings(result):
        out.append(f"Warning: {line}")
    if result["unsupported_slots"] or result["empty_slots"]:
        out.append("")

    out.append(f"Lineup optimizer, {stats_season} season FPPG basis, week {week} of the {vegas_season} season.")
    out.append("Picks by win probability against your real Sleeper opponent, not raw projected points.\n")

    if result["opponent_note"]:
        out.append(f"Opponent: {result['opponent_note']}\n")
    else:
        source = "" if result["opponent_source"] == "their set lineup" else f", {result['opponent_source']}"
        out.append(
            f"Opponent (roster {result['opponent_roster_id']}{source}): projected "
            f"{result['opponent_mean']:.1f} ± {result['opponent_variance'] ** 0.5:.1f}\n"
        )

    out.append("Recommended lineup:")
    for p in result["recommended_lineup"]:
        flag = f"  ({p['injury_status']})" if p["injury_status"] else ""
        vegas_note = f", vegas x{p['vegas_multiplier']:.2f}" if p["vegas_multiplier"] != 1.0 else ""
        bye_note = "  BYE WEEK" if p["on_bye"] else ""
        est = "*" if p["variance_estimated"] else ""
        out.append(
            f"  {p['full_name']:<20} {p['position']:<3} mean={p['mean']:>5.1f} var={p['variance']:>5.1f}{est}"
            f"{vegas_note}{flag}{bye_note}"
        )

    if result["recommended_win_probability"] is not None:
        out.append(f"\nWin probability: {result['recommended_win_probability']:.1%}")
    else:
        out.append("\nWin probability: n/a, no opponent to measure against yet; this is the most projected points.")

    if result["differs_from_points_max"]:
        swaps = "; ".join(f"{s['starts']} ({s['slot']}) over {s['over']}" for s in result["swaps_from_points_max"])
        out.append(
            f"\nNote: this differs from the highest-raw-points lineup ({result['points_max_total']:.1f} pts): {swaps}. "
            f"That trades a little mean for a better win probability given this specific matchup, not just stacking points."
        )

    out.append("\nBench:")
    for p in sorted(result["bench"], key=lambda p: -p["mean"]):
        out.append(f"  {p['full_name']:<20} {p['position']:<3} mean={p['mean']:>5.1f}{why_zero(p)}")
    if any(p["variance_estimated"] for p in result["recommended_lineup"]):
        out.append(
            "\n* variance estimated: too few games to measure the player's own, so the position's median weekly "
            "variance stands in."
        )
    return "\n".join(out)


def _trade_side(label: str, side: dict) -> list[str]:
    out = [f"{label}:"]
    if not side["players"] and not side["picks"]:
        out.append("  (nothing)")
    for p in side["players"]:
        note = rookie_note(p) if p["has_data"] else "  (no games this season, my-model valuation is 0)"
        market_str = f"{p['market_value']:.0f}" if p["market_value"] is not None else "-"
        out.append(
            f"  {p['full_name']:<22} {p['position'] or '':<4} "
            f"win-now {p['win_now_value']:>6.1f}  3yr(mine) {p['three_year_value']:>6.1f}  "
            f"market {market_str:>6}{note}"
        )
    for pk in side["picks"]:
        model_str = f"{pk['model_value']:.0f}" if pk["model_value"] is not None else "-"
        market_str = f"{pk['market_value']:.0f}" if pk["market_value"] is not None else "-"
        arb_str = f"{pk['arbitrage']:+.0f}" if pk["arbitrage"] is not None else "-"
        out.append(
            f"  {pk['label']:<22} {'PICK':<4} "
            f"model {model_str:>6}  market {market_str:>6}  arbitrage {arb_str:>7}{pick_price_note(pk)}"
        )
    out.append(
        f"  totals: win-now (players only) {side['win_now_total']:.1f}, "
        f"3yr mine (players only) {side['player_three_year_total']:.1f}, "
        f"market value (players + picks, comparable) {side['market_value_total']:.0f}"
    )
    if side["unpriced"]:
        out.append(f"  WARNING: no market price for {', '.join(side['unpriced'])}, counted as 0 in the market total above.")
    return out


def format_trade(result: dict, season: int) -> str:
    out = [f"Trade evaluation, pick discount rate {result['discount_rate']:.0%} per year.", basis_line(season)]
    out.append(
        "Win-now and 3yr(mine) are this league's own formula, players only, picks can't help you win "
        "this year so they don't appear there. Market value is FantasyCalc's own pricing for players plus "
        "my discount-adjusted pick model, the only number below that's comparable across players and picks "
        "together.\n"
    )
    for warning in result["warnings"]:
        out.append(f"WARNING: {warning}")
    if result["warnings"]:
        out.append("")
    out += _trade_side("You send", result["sent"])
    out.append("")
    out += _trade_side("You receive", result["received"])
    sides = (result["sent"], result["received"])
    if any(p.get("value_source") == "prospect_model" for side in sides for p in side["players"]):
        out.append(ROOKIE_FOOTNOTE)
    out.append("")
    out.append(f"Net win-now (players only): {result['win_now_delta']:+.1f}")
    out.append(f"Net 3yr, mine (players only): {result['player_three_year_delta']:+.1f}")
    out.append(f"Net market value (players + picks): {result['market_value_delta']:+.0f}")
    out.append(f"Your posture: {result['posture_label']} ({result['posture_confidence']})")
    out.append(f"Fit: {result['fit']}")
    if result["consolidation"]:
        out.append(f"Note: {result['consolidation']}")
    return "\n".join(out)


_SLOT_LABELS = {"starter": "START", "bench": "BENCH", "taxi": "TAXI", "reserve": "IR"}


def format_my_team(team: dict) -> str:
    season = team["season"]
    out = [basis_line(season)]
    out.append(
        "Situation score: average of QB passing EPA/game, team pass rate over expected, and sack rate "
        "allowed (inverted), each percentile-ranked against all 32 NFL teams. Not a full offensive line "
        "grade, real OL grades are paywalled; this is the public proxy.\n"
    )
    header = f"{'Slot':<7} {'Player':<22} {'Pos':<4} {'Age':<4} {'FPPG':>6} {'Sit%':>6} {'WinNow':>8} {'3yr':>8}"
    out += [header, "-" * len(header)]
    for row in team["players"]:
        v = row["valuation"]
        label = _SLOT_LABELS.get(row["slot"], row["slot"])
        name = (row["full_name"] or "?")[:22]
        pos = row["position"] or ""
        age = str(row["age"]) if row["age"] is not None else "-"
        if v is None:
            out.append(f"{label:<7} {name:<22} {pos:<4} {age:<4} {'-':>6} {'-':>6} {'-':>8} {'-':>8}  (no {season} games)")
            continue
        out.append(
            f"{label:<7} {name:<22} {pos:<4} {age:<4} "
            f"{v['fantasy_points_per_game']:>6.1f} {v['situation_score']:>6.1f} "
            f"{v['win_now_value']:>8.1f} {v['three_year_value']:>8.1f}{rookie_note(v)}"
        )
    if team["has_rookie_projection"]:
        out.append(ROOKIE_FOOTNOTE)

    verdict = team["verdict"]
    out.append("")
    out.append(f"Verdict: {verdict['label']}")
    out.append(f"Confidence: {verdict['confidence']}")
    out.append(
        f"Inputs: win-now total {verdict['my_win_now_total']:.1f} "
        f"({verdict['win_now_percentile']:.0f}th percentile against the other {verdict['compared_against']} teams), "
        f"three-year total {verdict['my_three_year_total']:.1f} "
        f"({verdict['three_year_percentile']:.0f}th percentile against the other {verdict['compared_against']} teams), "
        f"{verdict['games_played']} games played this season."
    )
    return "\n".join(out)


def format_faab(result: dict) -> str:
    out = [f"FAAB recommendation for {result['player']} ({result['position']})"]
    if result["is_rostered"]:
        out.append("Warning: this player is already on a roster in your league, not actually a free agent right now.")
    if result["note"]:
        out.append(result["note"])
    budget_note = " (league settings carry no waiver_budget, assumed Sleeper's default)" if result["budget_is_default"] else ""
    window = "before the playoffs" if result["phase"] == "regular" else "of playoffs"
    out.append(
        f"\nRemaining budget: ${result['remaining_budget']} of ${result['total_budget']}{budget_note}, "
        f"{result['weeks_left']} weeks left {window}."
    )
    out.append(
        f"Win-now value: {result['target_win_now_value']:.1f} points-per-game scale "
        f"({result['percentile_among_available']:.0f}th percentile among players actually available on waivers)."
    )
    out.append(gain_line(result))
    out.append(
        f"Base pace, remaining budget split evenly across the weeks left: ${result['base_per_week_budget']:.2f}/week, "
        f"scaled ×{result['value_multiplier']:.2f} for this target's lineup gain."
    )
    out.append(f"\nSuggested bid: ${result['suggested_bid']} (FAAB dollars)")
    return "\n".join(out)


def format_waiver_targets(targets: list[dict]) -> str:
    out = ["FAAB targets (the waiver players who would raise your best lineup the most):"]
    if targets and targets[0]["note"]:
        out.append(f"  {targets[0]['note']}")
    for bid in targets:
        gain = f"+{bid['lineup_gain']:.1f} to lineup" if bid["lineup_gain"] > 0 else "depth only"
        out.append(
            f"  {bid['player']:<20} {bid['position']:<3} win-now {bid['target_win_now_value']:>5.1f}  {gain:<16} "
            f"suggested bid ${bid['suggested_bid']}"
        )
    if not targets:
        out.append("  none: nobody available has a win-now value this season.")
    return "\n".join(out)


def format_digest(lineup: dict, targets: list[dict], winds: dict[str, str], stats_season: int,
                  vegas_season: int, sync_note: str | None = None) -> str:
    """winds is {player_id: " WIND ..." note} for starters with a flagged
    forecast; the caller looks them up, this only formats."""
    week = lineup["week"]
    out = [f"=== Week {week} digest, {stats_season} season FPPG basis, {vegas_season} season Vegas lines ===\n"]
    if sync_note:
        out.append(f"Note: {sync_note}\n")
    for line in lineup_warnings(lineup):
        out.append(f"Warning: {line}")
    out.append("Start:")
    for p in lineup["recommended_lineup"]:
        flag = f"  ({p['injury_status']})" if p["injury_status"] else ""
        bye_note = "  BYE WEEK" if p["on_bye"] else ""
        out.append(f"  {p['full_name']:<20} {p['position']:<3} mean={p['mean']:>5.1f}{flag}{winds.get(p['player_id'], '')}{bye_note}")

    if lineup["recommended_win_probability"] is not None:
        out.append(f"\nWin probability: {lineup['recommended_win_probability']:.1%}")
    else:
        out.append(f"\nWin probability: n/a, {lineup['opponent_note']}.")
    if lineup["differs_from_points_max"]:
        out.append("Chosen over the pure-points lineup for a better win probability against this week's specific opponent.")

    out.append("\nSit (top bench by projection):")
    for p in sorted(lineup["bench"], key=lambda p: -p["mean"])[:5]:
        out.append(f"  {p['full_name']:<20} {p['position']:<3} mean={p['mean']:>5.1f}{why_zero(p)}")

    out.append("\n" + format_waiver_targets(targets))
    out.append("\nThis is DRAFT-heuristic math throughout (see matchup.py, PLANNING.md), not a calibrated prediction.")
    return "\n".join(out)


def format_picks(report: dict, names: dict[int, str], me: int, verdict: dict, show_all: bool) -> str:
    weight = report["record_weight"]
    out = [f"Rookie picks, {'every team' if show_all else 'yours'}. Next draft: {report['next_draft']}."]
    out.append(
        f"{report['next_draft']} slots are PROJECTED reverse standings: roster strength (best-lineup win-now) blended "
        f"toward real record, record weighted {weight:.0%} so far (games played / regular-season games). "
        f"Later drafts can't be slotted yet."
    )
    out.append(
        f"Advice compares FantasyCalc's price for the pick with FantasyCalc's current value for the "
        f"{report['recent_class']} rookies taken at that same slot: pick more than {picks.SELL_ABOVE:.2f}x the "
        f"player it bought last year = SELL, under {picks.BUY_BELOW:.2f}x = BUY. History: that slot's rookies since "
        f"{prospect_model.FIRST_TRAINING_CLASS}, league-weighted PPG over 3 seasons and how often they hit "
        f"{picks.HIT_LEAGUE_PPG:g}+ (a weekly flex starter).\n"
    )
    header = f"{'Pick':<14} {'Holder':<16} {'From':<16} {'FCalc':>6} {'Comp':>6} {'Ratio':>5}  {'Advice':<8} {'Hist PPG':>8} {'Hit%':>5}"
    out += [header, "-" * len(header)]

    def fmt(v, spec):
        return format(v, spec) if v is not None else "-"

    for r in report["rows"]:
        slot = r["projected_slot"] or "  -  "
        label = f"{r['season']} {slot}" + (f" {r['tier'][0]}" if r["tier"] else "")
        if not r["projected_slot"]:
            label = f"{r['season']} rd {r['round']}"
        hist = r["history"] or {}
        holder = names.get(r["owner_roster_id"], "?")[:16]
        origin = "own" if r["original_roster_id"] == r["owner_roster_id"] else names.get(r["original_roster_id"], "?")[:16]
        mark = "*" if r["owner_roster_id"] == me else " "
        out.append(
            f"{label:<14}{mark}{holder:<16} {origin:<16} {fmt(r['fantasycalc_price'], '6.0f'):>6} "
            f"{fmt(r.get('comparable_value'), '6.0f'):>6} {fmt(r['ratio'], '5.2f'):>5}  {r['advice'][:8]:<8} "
            f"{fmt(hist.get('mean_league_ppg'), '8.1f'):>8} {fmt(hist.get('hit_rate') * 100 if hist else None, '4.0f'):>4}%"
        )
    slotted = [r for r in report["rows"] if r.get("comparable_players")]
    if slotted and not show_all:
        out.append("\nComparables (the recent rookies each projected slot actually bought):")
        for r in slotted:
            out.append(f"  {r['season']} {r['projected_slot']}: {', '.join(r['comparable_players'])}")
    out.append(
        f"\nYour posture: {verdict['label']} ({verdict['confidence']}). "
        f"A contender sells picks for win-now help; a rebuilder holds or buys them."
    )
    return "\n".join(out)


def format_taxi(result: dict) -> str:
    st = result["settings"]
    deadline = (
        f"taxi deadline {st['taxi_deadline']} (read as week {st['taxi_deadline']}; Sleeper doesn't document the field)"
        if st["taxi_deadline"] else "no taxi deadline (moves allowed all season)"
    )
    eligible = "rookies or veterans" if st["taxi_allow_vets"] else "rookies only"
    ir_word = "IR slot" if st["reserve_slots"] == 1 else "IR slots"
    out = [
        f"Taxi and IR plan. Your league: {st['taxi_slots']} taxi slots, {eligible}, {st['taxi_years']} year max, "
        f"{deadline}; {st['reserve_slots']} {ir_word}. Taxi players can't be started.\n"
    ]
    if result["taxi_locked"]:
        out.append(f"Taxi moves are locked: it's week {result['current_week']}, past the deadline. IR moves still work.\n")
    out.append(
        f"Active roster: {result['active_count']} of {result['active_capacity']}. "
        f"Taxi: {len(result['taxi_now'])} of {st['taxi_slots']} used. IR: {len(result['ir_now'])} of {st['reserve_slots']} used."
    )
    if result["moves"]:
        out.append(f"\nRecommended moves, each frees a bench spot ({result['bench_spots_freed']} total):")
        for m in result["moves"]:
            p = m["player"]
            out.append(
                f"  {p['full_name']:<22} {p['position']:<3} -> {m['to']:<4}  {p['ppg']:.1f} PPG, "
                f"3yr value {p['three_year_value']:.1f}: {m['why']}"
            )
        out.append("  Make these in the Sleeper app, this tool can't change your roster (Sleeper's API is read-only).")
    else:
        out.append("\nNo moves: every open taxi and IR slot is either filled or has no eligible player to put there.")
    for p in result["keep_active"]:
        out.append(f"  Keep {p['full_name']} active: taxi-eligible, but in your best lineup right now.")

    out.append(f"\nNext season ({result['next_draft']} rookie draft):")
    graduating = ", ".join(p["full_name"] for p in result["graduating"])
    out.append(
        f"  You hold {result['next_picks']} picks in that draft. Taxi players, after the moves above, graduate back to "
        f"the active roster{f' ({graduating})' if graduating else ''}; the new rookies can take the taxi slots. "
        f"Projected: {result['roster_next']} players for {result['capacity_next']} spots."
    )
    if result["overflow"] > 0:
        if result["cut_candidates"]:
            out.append(
                f"  Roster crunch: {result['overflow']} cut(s) needed. Lowest three-year value among veterans who "
                f"don't start over a season:"
            )
            for p in result["cut_candidates"]:
                out.append(f"    {p['full_name']:<22} {p['position']:<3} 3yr value {p['three_year_value']:.1f}")
        else:
            out.append(
                f"  Roster crunch: {result['overflow']} cut(s) needed, and every non-rookie on your roster starts, "
                f"so the cut would come from your lineup or a rookie."
            )
        out.append("  Or trade picks away before the draft; see `dynasty-agent picks`.")
    else:
        out.append("  No crunch: everyone fits, before any waiver adds between now and then.")
    return "\n".join(out)
