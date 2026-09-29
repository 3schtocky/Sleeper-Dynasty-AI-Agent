"""How the numbers are built, written by Python so the chat never has to guess.

A 4B model asked to explain methodology from memory would invent it, and the
grounding check only catches invented numbers, not invented reasoning. So every
explanation here is computed from the same constants and functions that
produce the number the user was shown, and each one is checked against that
number before it is returned: an explanation that doesn't reproduce the
number raises instead of being displayed. The model only retells the steps
in conversation.

Nothing here reads the network. Player value explanations read the database
through valuation.player_valuations, the one source of the numbers shown.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field

from dynasty_agent import blend, market, metrics, prospect_model, valuation
from dynasty_agent.metrics import discounted_pick_value
from dynasty_agent.errors import AgentError

TOLERANCE = 1e-6


class ExplanationMismatch(AgentError):
    """The steps did not reproduce the number that was shown. Never displayed
    as an explanation: a wrong walkthrough is worse than none."""


@dataclass
class Step:
    label: str
    formula: str
    value: str
    why: str = ""


@dataclass
class Explanation:
    title: str
    steps: list[Step]
    limits: list[str] = field(default_factory=list)
    subject: dict | None = None  # {"player_id", "full_name"} when about one player, so "his 3yr?" finds him

    def text(self) -> str:
        """The block Python prints, the same way it prints a numbers block."""
        out = [self.title, ""]
        for i, s in enumerate(self.steps, 1):
            out.append(f"{i}. {s.label}: {s.formula} = {s.value}")
            if s.why:
                out.append(f"     {s.why}")
        if self.limits:
            out.append("")
            out.append("What to keep in mind:")
            out += [f"  - {line}" for line in self.limits]
        out.append("")
        out.append("(Figures are rounded for display; the calculation uses the unrounded values.)")
        return "\n".join(out)

    def summary(self) -> dict:
        """What the model is given to retell: the steps in order, with their reasons."""
        return {
            "title": self.title,
            "steps": [f"{s.label}: {s.formula} = {s.value}" + (f" ({s.why})" if s.why else "") for s in self.steps],
            "limits": self.limits,
        }


def ordinal(n: float) -> str:
    """82 -> '82nd', 11 -> '11th', 1 -> '1st'."""
    n = round(n)
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _pts(x: float) -> str:
    return f"{x:.1f}"


def _mult(x: float) -> str:
    return f"{x:.2f}"


def _check(name: str, got: float, shown: float) -> None:
    if abs(got - shown) > TOLERANCE * max(1.0, abs(shown)):
        raise ExplanationMismatch(
            f"I couldn't reproduce {name} ({got:.4f} against the {shown:.4f} shown), so I won't walk you through it. "
            f"Run `dynasty-agent valuate` for the raw numbers."
        )


# -- a player's value --------------------------------------------------------------


def explain_player_value(conn: sqlite3.Connection, season: int, player_id: str) -> Explanation:
    """The chain behind one player's win-now and three-year value:
    blended points per game x position weight x age curve x team situation."""
    v = valuation.player_valuations(conn, season).get(player_id)
    if v is None:
        row = conn.execute("SELECT full_name FROM players WHERE player_id = ?", (player_id,)).fetchone()
        name = row["full_name"] if row else player_id
        raise AgentError(
            f"{name} has no valuation this season: no games in {season} or {season - 1} and no rookie projection."
        )
    name, pos, age = v["full_name"], v["position"], v["age"]
    steps: list[Step] = []
    limits: list[str] = []

    # 1. blended points per game
    if v["value_source"] == "prospect_model":
        rookie = prospect_model.rookie_projections(conn, season).get(player_id) or {}
        projected = rookie.get("projected_ppg")
        current = blend.weekly_values(conn, player_id, season)
        k = v["prior_weight"]
        pick = v.get("draft_pick")
        origin = "undrafted" if v.get("undrafted") else f"drafted number {pick}" if pick else "draft capital unknown"
        blended = blend.blended_mean(projected, k, current)
        steps.append(Step(
            "Points per game (rookie)",
            f"({k:g} games x projection {_pts(projected)} + {len(current)} real games totalling {_pts(sum(current))}) "
            f"/ ({k:g} + {len(current)})",
            _pts(blended),
            f"a rookie has no last season, so the model's projection from draft capital ({origin}) counts as {k:g} "
            f"games and each real game adds on top",
        ))
        limits.append("A rookie's projection comes from the prospect model, fitted on the 2018 to 2023 draft classes.")
    else:
        prior = blend.weekly_values(conn, player_id, season - 1)
        current = blend.weekly_values(conn, player_id, season)
        k = v["prior_weight"]
        prior_mean = sum(prior) / len(prior) if prior else None
        blended = blend.blended_mean(prior_mean, k, current)
        if prior_mean is None:
            steps.append(Step(
                "Points per game", f"average of {len(current)} real {season} games", _pts(blended),
                f"no {season - 1} games, so only this season counts",
            ))
        else:
            steps.append(Step(
                "Points per game",
                f"({k:g} games x last season's {_pts(prior_mean)} + {len(current)} {season} games totalling "
                f"{_pts(sum(current))}) / ({k:g} + {len(current)})",
                _pts(blended),
                f"last season's average counts as {k:g} games (chosen by backtest) and each real {season} game adds "
                f"on top, so the new season takes over as it accumulates",
            ))
    _check("the points per game", blended, v["fantasy_points_per_game"])

    # 2. position weight
    weight = metrics.POSITION_PRODUCTION_MULTIPLIER.get(pos or "", 1.0)
    production = blended * weight
    _check("the production score", production, v["production_score"])
    why_pos = {
        "QB": "one starting quarterback per team makes the position nearly replaceable, so QB points are discounted",
        "WR": "three starting receivers plus a flex in full PPR makes wide receiver this league's highest-signal position",
    }.get(pos or "", "this position's points already reflect the league's scoring, so no adjustment")
    steps.append(Step("Production score", f"{_pts(blended)} x position weight {_mult(weight)} ({pos})", _pts(production), why_pos))

    # 3. age
    age_mult = metrics.age_multiplier(pos, age)
    if age is None:
        steps.append(Step("Age factor", "age unknown", _mult(age_mult), "with no age on file the factor is a neutral 1.00"))
    else:
        peak_end, decay = metrics.AGE_CURVES.get(pos or "", metrics._DEFAULT_AGE_CURVE)
        if age <= peak_end:
            steps.append(Step("Age factor", f"age {age:g} is at or below the {pos} peak of {peak_end}", _mult(age_mult),
                              "no decline is applied through a position's peak"))
        else:
            steps.append(Step(
                "Age factor", f"e^(-{decay} x ({age:g} - {peak_end}))", _mult(age_mult),
                f"value decays smoothly after a {pos}'s peak age of {peak_end}",
            ))
    limits.append("The age curves are round numbers anchored to typical position aging, not fitted to this league's history.")

    # 4. team situation
    situation = v["situation_score"]
    sit_mult = metrics.situation_multiplier(situation)
    steps.append(Step(
        "Situation factor", f"0.85 + 0.30 x ({situation:.0f} / 100), team {v['team'] or 'unknown'}", _mult(sit_mult),
        "the score averages his team's quarterback quality, pass rate over expected and pass protection, each ranked "
        "against the 32 teams; a middle team is 1.00 and the range is capped at 0.85 to 1.15 so context never "
        "outweighs production",
    ))
    limits.append("Pass protection is a public stand-in (sack rate); real offensive line grades are paywalled.")

    # 5. win-now
    win_now = production * age_mult * sit_mult
    _check("the win-now value", win_now, v["win_now_value"])
    steps.append(Step(
        "Win-now value", f"{_pts(production)} x {_mult(age_mult)} x {_mult(sit_mult)}", _pts(win_now),
        "how much he helps you win this season, on this league's points-per-game scale",
    ))

    # 6. three-year
    factor = metrics.three_year_age_factor(pos, age)
    three_year = production * factor * sit_mult
    _check("the three-year value", three_year, v["three_year_value"])
    if age is None:
        detail = "age unknown"
    else:
        detail = "average age factor over this year and the next two: " + ", ".join(
            _mult(metrics.age_multiplier(pos, age + i)) for i in range(3)
        )
    steps.append(Step(
        "Three-year value", f"{_pts(production)} x {_mult(factor)} x {_mult(sit_mult)}", _pts(three_year),
        f"same production and situation, with the age factor averaged over three seasons ({detail})",
    ))

    return Explanation(f"How {name}'s value is built ({pos}, {season} season)", steps, limits,
                       subject={"player_id": player_id, "full_name": name})



# -- a lineup and its win probability ------------------------------------------------


def _signed(x: float) -> str:
    return f"{x:+.1f}"


def explain_lineup(r: dict) -> Explanation:
    """How this week's lineup and win probability were reached, from the
    result optimize_lineup returned: projected points and spread for each
    side, the gap, and the normal-curve step. The last step is recomputed and
    must equal the probability shown."""
    lineup = r["recommended_lineup"]
    mean_me = sum(p["mean"] for p in lineup)
    var_me = sum(p["variance"] for p in lineup)
    steps: list[Step] = []
    limits = [
        "Win probability is a draft heuristic: it treats every player's weekly score as independent, which teammates "
        "are not, and it has not been checked against real results.",
        "The injury factors (Questionable 0.85, Doubtful 0.25, Out 0) are round numbers, not fitted.",
    ]

    steps.append(Step(
        f"Your projected points, week {r['week']}",
        " + ".join(f"{p['full_name']} {_pts(p['mean'])}" for p in lineup), _pts(mean_me),
        "each starter's mean is his blended points per game, times an injury factor and a Vegas factor",
    ))
    notes = []
    for p in lineup:
        bits = []
        status = p.get("injury_status")
        if status in metrics.INJURY_MEAN_MULTIPLIER:
            bits.append(f"{status} x{_mult(metrics.INJURY_MEAN_MULTIPLIER[status])}")
        if p.get("on_bye"):
            bits.append("on a bye, so 0")
        elif abs(p["vegas_multiplier"] - 1.0) > 1e-9:
            bits.append(f"Vegas x{_mult(p['vegas_multiplier'])}")
        if bits:
            notes.append(f"{p['full_name']}: " + ", ".join(bits))
    steps.append(Step(
        "Adjustments this week", "; ".join(notes) if notes else "none",
        f"{len(notes)} player{'s' if len(notes) != 1 else ''}",
        "the Vegas factor is his team's implied points this week divided by its own season norm, so a big expected "
        "game lifts him and a low one lowers him" if notes else "no injury tags and no unusual Vegas lines",
    ))
    estimated = [p["full_name"] for p in lineup if p.get("variance_estimated")]
    steps.append(Step(
        "Your spread (variance)", f"the sum of the {len(lineup)} starters' weekly variances", _pts(var_me),
        "variance measures how much a player's score swings from week to week; it adds across players"
        + (f"; {', '.join(estimated)} borrow their position's median because they have too few games" if estimated else ""),
    ))

    if r["opponent_mean"] is None:
        steps.append(Step(
            "Opponent", r["opponent_note"] or "no opponent", "none yet",
            "with nobody to measure against there is no win probability, so the lineup with the most projected points is chosen",
        ))
        return Explanation(f"How week {r['week']}'s lineup was chosen", steps, limits)

    mean_opp, var_opp = r["opponent_mean"], r["opponent_variance"]
    steps.append(Step(
        f"Opponent (roster {r['opponent_roster_id']})", f"{r['opponent_source']}: projected points and variance",
        f"{_pts(mean_opp)} and {_pts(var_opp)}", "built the same way as your side",
    ))
    diff, std = mean_me - mean_opp, (var_me + var_opp) ** 0.5
    steps.append(Step(
        "The gap", f"your {_pts(mean_me)} - their {_pts(mean_opp)}", _signed(diff),
        "the combined spread is the square root of both variances added: "
        f"sqrt({_pts(var_me)} + {_pts(var_opp)}) = {_pts(std)}",
    ))
    prob = metrics.matchup_win_probability(diff, std)
    shown = r["recommended_win_probability"]
    if shown is None or abs(prob - shown) > TOLERANCE:
        raise ExplanationMismatch("I couldn't reproduce the win probability shown, so I won't walk you through it.")
    steps.append(Step(
        "Win probability", f"chance a normal curve centered on {_signed(diff)} with spread {_pts(std)} lands above 0 "
        f"(z = {diff / std:.2f})" if std > 0 else "no spread, so the sign of the gap decides", f"{prob:.1%}",
        "the same normal-curve idea behind converting a spread to a moneyline, simplified to two team totals",
    ))
    if r.get("differs_from_points_max") and r.get("swaps_from_points_max"):
        swaps = "; ".join(f"{s['starts']} over {s['over']}" for s in r["swaps_from_points_max"])
        steps.append(Step(
            "Why this lineup and not the highest-points one", swaps, f"{r['points_max_total']:.1f} pts for the alternative",
            "every valid lineup is scored for win probability, and this one wins; a slightly lower mean can be worth "
            "it against this specific opponent",
        ))
    return Explanation(f"How week {r['week']}'s win probability is built", steps, limits)


# -- a trade ---------------------------------------------------------------------------


def _side_lines(side: dict) -> str:
    parts = [f"{p['full_name']} {p['market_value']:.0f}" for p in side["players"] if p["market_value"] is not None]
    parts += [f"{k['label']} {k['model_value']:.0f}" for k in side["picks"] if k["model_value"] is not None]
    return " + ".join(parts) or "nothing"


def explain_trade(conn: sqlite3.Connection, r: dict) -> Explanation:
    """How a trade's totals, pick prices and fit were reached, from the
    result evaluate_trade returned. Each total is recomputed and must equal
    the one shown."""
    rate = r["discount_rate"]
    steps: list[Step] = []
    limits = [
        "Market value is FantasyCalc's price in trade-value points, not dollars. Win-now is points per game. The two "
        "are never added together.",
        f"The {rate:.0%} yearly pick discount is a tunable default, not fitted to this league.",
    ]

    for side in (r["sent"], r["received"]):
        for k in side["picks"]:
            if k["model_value"] is None:
                continue
            years = k["season"] - k["base_season"]
            if years == 0:
                steps.append(Step(f"Pick price: {k['label']}", f"FantasyCalc's {k['price_label']} price", f"{k['model_value']:.0f}",
                                  "the nearest draft is priced directly, by tier where FantasyCalc prices tiers"))
                continue
            base = market.pick_market_value(conn, k["base_season"], k["round"])
            back = discounted_pick_value(base, years, rate)
            if abs(back - k["model_value"]) > TOLERANCE * max(1.0, back):
                raise ExplanationMismatch(f"I couldn't reproduce the price of the {k['label']}, so I won't walk you through it.")
            gap = f"; FantasyCalc prices it at {k['market_value']:.0f}, a gap of {k['arbitrage']:+.0f}" if k["arbitrage"] is not None else ""
            steps.append(Step(
                f"Pick price: {k['label']}",
                f"{base:.0f} (FantasyCalc's {k['base_season']} price) x (1 - {rate:.0%})^{years}", f"{k['model_value']:.0f}",
                f"a pick {years} year{'s' if years != 1 else ''} past the nearest priced draft is worth less because it is "
                f"further away and less certain{gap}",
            ))

    for label, side in (("Market value you send", r["sent"]), ("Market value you receive", r["received"])):
        steps.append(Step(label, _side_lines(side), f"{side['market_value_total']:.0f}",
                          "FantasyCalc's price for each player plus this project's price for each pick"))
        total = sum(p["market_value"] or 0.0 for p in side["players"]) + sum(k["model_value"] or 0.0 for k in side["picks"])
        if abs(total - side["market_value_total"]) > TOLERANCE * max(1.0, total):
            raise ExplanationMismatch(f"I couldn't reproduce the {label.lower()}, so I won't walk you through it.")
    net = r["received"]["market_value_total"] - r["sent"]["market_value_total"]
    if abs(net - r["market_value_delta"]) > TOLERANCE * max(1.0, abs(net)):
        raise ExplanationMismatch("I couldn't reproduce the net market value, so I won't walk you through it.")
    steps.append(Step("Net market value", f"receive {r['received']['market_value_total']:.0f} - send {r['sent']['market_value_total']:.0f}",
                      f"{net:+.0f}", "positive means you get more trade value than you give"))

    sent_wn, got_wn = r["sent"]["win_now_total"], r["received"]["win_now_total"]
    steps.append(Step("Net win-now (players only)", f"receive {_pts(got_wn)} - send {_pts(sent_wn)}", _signed(got_wn - sent_wn),
                      "picks add nothing here: a rookie pick cannot help you win this season"))
    steps.append(Step("Net 3yr, mine (players only)",
                      f"receive {_pts(r['received']['player_three_year_total'])} - send {_pts(r['sent']['player_three_year_total'])}",
                      _signed(r["player_three_year_delta"]), "the same players-only formula with age averaged over three seasons"))

    posture, wn, mv = r["posture"], got_wn - sent_wn, net
    if posture == "contend":
        fit = "fits a contend posture" if wn >= 0 else "cuts against a contend posture, loses win-now value"
        rule = f"as a contender the test is net win-now at or above 0 ({_signed(wn)})"
    elif posture == "rebuild":
        fit = "fits a rebuild posture" if mv >= 0 else "cuts against a rebuild posture, loses long-term market value"
        rule = f"as a rebuilder the test is net market value at or above 0 ({mv:+.0f})"
    else:
        fit = "posture is unclear right now, judge this on the raw numbers, not fit"
        rule = "with an unclear posture there is no fit test, so judge the raw numbers"
    if fit != r["fit"]:
        raise ExplanationMismatch("I couldn't reproduce the fit call, so I won't walk you through it.")
    steps.append(Step("Fit with your posture", f"{r['posture_label']}: {rule}", fit, r["posture_confidence"]))
    if r.get("consolidation"):
        steps.append(Step("Roster shape", r["consolidation"], "noted", "how many pieces change hands matters on a deep roster"))
    if r["sent"]["unpriced"] or r["received"]["unpriced"]:
        limits.append("Anything with no market price counts as 0 in the market totals: "
                      + ", ".join(r["sent"]["unpriced"] + r["received"]["unpriced"]) + ".")
    return Explanation("How this trade was evaluated", steps, limits)


# -- a waiver bid ------------------------------------------------------------------------


def explain_faab(bid: dict) -> Explanation:
    """How a suggested FAAB bid was sized, from one recommendation dict."""
    from dynasty_agent import weekly

    lo, hi = weekly.FAAB_MIN_VALUE_MULTIPLIER, weekly.FAAB_MAX_VALUE_MULTIPLIER
    steps: list[Step] = []
    limits = [f"The {lo} and {hi} multipliers are round numbers, not fitted to past waiver results."]
    remaining, weeks = bid["remaining_budget"], bid["weeks_left"]
    base = remaining / weeks if weeks else 0.0
    if abs(base - bid["base_per_week_budget"]) > TOLERANCE:
        raise ExplanationMismatch("I couldn't reproduce the weekly pace, so I won't walk you through it.")
    window = "before the playoffs" if bid["phase"] == "regular" else "of playoffs"
    steps.append(Step("Weekly pace", f"${remaining} left / {weeks} weeks {window}", f"${base:.2f} a week",
                      "the remaining budget spread evenly over the weeks it has to last"))
    gain, best = bid["lineup_gain"], bid["best_available_gain"]
    if gain > 0:
        steps.append(Step(f"Lineup gain for {bid['player']}", "how much adding him raises your best possible lineup's win-now total",
                          f"+{gain:.1f}", f"the biggest upgrade anyone available offers is +{best:.1f}"))
    else:
        steps.append(Step(f"Lineup gain for {bid['player']}", "he would not start for you", "none",
                          "a player who wouldn't start adds nothing to your best lineup, however good he is"))
    share = gain / best if best > 0 else 0.0
    mult = lo + (hi - lo) * share
    if abs(mult - bid["value_multiplier"]) > TOLERANCE:
        raise ExplanationMismatch("I couldn't reproduce the bid multiplier, so I won't walk you through it.")
    steps.append(Step("Bid multiplier", f"{lo} + ({hi} - {lo}) x ({_pts(gain)} / {_pts(best)})" if best > 0 else f"no upgrade on the wire, so the floor {lo}",
                      _mult(mult), "the best upgrade available gets the top multiplier, everyone else scales by their share of it"))
    suggested = max(0, min(round(base * mult), remaining))
    if suggested != bid["suggested_bid"]:
        raise ExplanationMismatch("I couldn't reproduce the suggested bid, so I won't walk you through it.")
    steps.append(Step("Suggested bid", f"${base:.2f} x {_mult(mult)} = ${base * mult:.2f}, rounded to whole dollars and capped at what is left",
                      f"${suggested}", "FAAB is the one place this project talks in dollars"))
    if bid.get("budget_is_default"):
        limits.append("Your league's settings carry no waiver budget, so Sleeper's default of $100 is assumed.")
    if bid.get("note"):
        limits.append(bid["note"])
    return Explanation(f"How the ${suggested} bid for {bid['player']} was sized", steps, limits,
                       subject={"player_id": bid["player_id"], "full_name": bid["player"]})


# -- contend, rebuild or unclear -----------------------------------------------------------


def explain_verdict(conn: sqlite3.Connection, season: int, my_roster_id: int) -> Explanation:
    """How the contend, rebuild or unclear call was reached: your best
    lineup's totals ranked against the other teams, then the stated bands."""
    v = valuation.contend_or_rebuild(conn, season, my_roster_id)
    steps: list[Step] = []
    limits = [
        f"The bands (contend at the {valuation.CONTEND_MIN_WIN_NOW_PCT}th percentile or better now, rebuild below the "
        f"{valuation.REBUILD_MAX_WIN_NOW_PCT}th) are round numbers, not fitted: this league has no history to fit them to.",
        f"Percentiles rank you against the other {v['compared_against']} teams only; counting your own team would cap the best roster at the 96th.",
    ]
    for label, key, pct_key, mine in (
        ("Win-now", "league_win_now_totals", "win_now_percentile", v["my_win_now_total"]),
        ("Three-year", "league_three_year_totals", "three_year_percentile", v["my_three_year_total"]),
    ):
        others = [t for rid, t in v[key].items() if rid != my_roster_id]
        pct = metrics.percentile_rank(mine, others)
        if abs(pct - v[pct_key]) > TOLERANCE:
            raise ExplanationMismatch("I couldn't reproduce the percentile, so I won't walk you through it.")
        steps.append(Step(
            f"{label} standing", f"your best lineup {_pts(mine)} against the other teams' {_pts(min(others))} to {_pts(max(others))}",
            f"{ordinal(pct)} percentile", f"each team is scored on the best lineup it could start; you beat {sum(t < mine for t in others)} of {len(others)}",
        ))
    wn, ty = v["win_now_percentile"], v["three_year_percentile"]
    steps.append(Step(
        "The call", f"contend needs win-now >= {valuation.CONTEND_MIN_WIN_NOW_PCT} and three-year >= {valuation.CONTEND_MIN_THREE_YEAR_PCT}; "
        f"rebuild needs win-now < {valuation.REBUILD_MAX_WIN_NOW_PCT} and three-year >= {valuation.REBUILD_MIN_THREE_YEAR_PCT}",
        v["verdict"].upper(), f"with win-now {wn:.0f} and three-year {ty:.0f}: {v['reason']}",
    ))
    steps.append(Step("Confidence", f"{v['games_played']} games played (real results count after {valuation.SIGNAL_GAMES})",
                      v["confidence"].split(".")[0], v["confidence"]))
    return Explanation("How the contend, rebuild or unclear call was made", steps, limits)


# -- buy, hold or sell a pick ------------------------------------------------------------------


def explain_pick(row: dict) -> Explanation:
    """How one slotted pick's buy, hold or sell call was reached, from a
    pick_report row."""
    from dynasty_agent import picks

    price, comparable = row["fantasycalc_price"], row["comparable_value"]
    call, ratio = picks.advice(price, comparable)
    if call != row["advice"]:
        raise ExplanationMismatch("I couldn't reproduce the pick call, so I won't walk you through it.")
    steps = [
        Step(f"Projected slot for the {row['season']} round {row['round']}", "the original owner's record and roster strength, weakest first",
             f"{row['projected_slot']} ({row['tier']} tier)", "a weaker projected team picks earlier"),
        Step("FantasyCalc's price", f"the {row['season']} {row['tier']} {row['round']} price", f"{price:.0f}",
             "in trade-value points, not dollars"),
        Step("What that slot bought last year", "median FantasyCalc value of last class's rookies at this slot and either side: "
             + ", ".join(row["comparable_players"]), f"{comparable:.0f}", "the market-against-market comparison"),
    ]
    if ratio is not None:
        steps.append(Step("Ratio", f"{price:.0f} / {comparable:.0f}", f"{ratio:.2f}",
                          f"SELL above {picks.SELL_ABOVE}, BUY below {picks.BUY_BELOW}, otherwise HOLD"))
    steps.append(Step("The call", f"ratio {ratio:.2f} against {picks.BUY_BELOW} and {picks.SELL_ABOVE}", call,
                      "a pick priced well above what its slot bought is worth selling; well below, worth buying"))
    return Explanation(f"How the call on the {row['season']} {row['projected_slot']} was made", steps,
                       [f"The {picks.BUY_BELOW} and {picks.SELL_ABOVE} bands are round numbers, not fitted.",
                        "Slotting is a projection from the current standings and roster strength, so it moves as the season does."])

# -- terms ---------------------------------------------------------------------------

GLOSSARY: dict[str, tuple[tuple[str, ...], str]] = {
    "win-now value": (
        ("win-now", "win now", "winnow"),
        "Win-now value is how much a player helps you win this season: his blended points per game, weighted for "
        "his position, reduced for age and adjusted for his team's offense. It is on this league's points-per-game "
        "scale. Ask 'how did you get' a player's win-now value for the full chain.",
    ),
    "three-year value": (
        ("3yr", "three year", "three-year", "3 year", "3yr mine"),
        "Three-year value is the same production and team situation as win-now, with the age factor averaged over "
        "this season and the next two, so it rewards players who hold their value and penalizes fast decliners like "
        "aging running backs. 'Mine' means it comes from this project's formula, not from FantasyCalc.",
    ),
    "market value": (
        ("market value", "fantasycalc", "trade value", "market"),
        "Market value is FantasyCalc's price for a player or pick, in trade-value points, not dollars. It is the only "
        "number that compares players and picks on one scale, which is why trades total it.",
    ),
    "situation score": (
        ("situation score", "situation"),
        "A 0 to 100 score for a player's team: the average of its quarterback quality (passing EPA per game), pass "
        "rate over expected and pass protection (sack rate), each ranked against the 32 teams and blended with last "
        "season by games played. It becomes a factor between 0.85 and 1.15, with 1.00 for a middle team.",
    ),
    "fppg": (
        ("fppg", "points per game", "ppg", "fantasy points per game"),
        "FPPG is fantasy points per game under this league's own scoring (full PPR, 4 points per passing touchdown). "
        "It blends last season's average, which counts as a few games, with this season's real games, so the new "
        "season takes over as it accumulates.",
    ),
    "variance": (
        ("variance", "swing", "boom bust", "spread"),
        "Variance measures how much a player's weekly score swings around his average. The lineup optimizer adds it "
        "up across a team to judge how uncertain a matchup is. A player with few games borrows his position's median "
        "variance, marked with an asterisk.",
    ),
    "win probability": (
        ("win probability", "win prob", "chance to win"),
        "The chance your projected lineup outscores your opponent's this week. It treats each team's total as a "
        "normal curve built from every player's average and variance and asks how likely the difference is above "
        "zero. It is a draft heuristic that assumes players' scores are independent, and it has not been checked "
        "against real results.",
    ),
    "arbitrage": (
        ("arbitrage",),
        "Arbitrage is the gap between this project's own price for a draft pick and FantasyCalc's market price. A "
        "positive gap means the market pays more than the model thinks the pick is worth.",
    ),
    "discount rate": (
        ("discount rate", "discount", "pick discount"),
        "Future picks are worth less than near ones because they are further away and less certain. Each extra year "
        "cuts a pick's value by the discount rate, 20% by default, so a pick two years out counts at 64% of its base.",
    ),
    "posture": (
        ("posture", "contend", "rebuild", "verdict"),
        "Posture is the contend, rebuild or unclear call. It compares your best possible lineup's win-now and "
        "three-year totals against the other teams in the league, by percentile, and says how confident it is: "
        "confidence stays low early in a season because few games have been played.",
    ),
    "age factor": (
        ("age", "age curve", "aging", "decay", "peak"),
        "Age matters because players decline after a position's peak. Value holds flat through the peak age, then "
        "decays smoothly: "
        + ", ".join(f"{pos} peaks at {peak} and loses {1 - math.exp(-decay):.0%} a year after" for pos, (peak, decay) in metrics.AGE_CURVES.items())
        + ". Running backs fall fastest and quarterbacks hold value longest. These curves are round numbers anchored "
        "to typical aging, not fitted to this league.",
    ),
    "taxi": (
        ("taxi", "taxi squad", "ir", "injured reserve"),
        "Taxi holds up to three rookies for one year without using a bench spot, and IR holds one injured player. "
        "Moving players there frees bench spots on a thin waiver wire.",
    ),
    "faab": (
        ("faab", "waiver budget", "bid"),
        "FAAB is the $100 waiver budget. It is the one place this project talks in dollars. A bid scales with how much "
        "the player would raise your best lineup compared with the biggest upgrade on the waiver wire.",
    ),
}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def lookup_glossary(topic: str) -> tuple[str, str] | None:
    """(term, definition) when the topic names a known term, else None.
    Longer aliases win, so '3yr mine' finds the three-year entry."""
    wanted = f" {_normalize(topic)} "
    best: tuple[int, str, str] | None = None
    for term, (aliases, definition) in GLOSSARY.items():
        for alias in (term, *aliases):
            a = _normalize(alias)
            if a and f" {a} " in wanted and (best is None or len(a) > best[0]):
                best = (len(a), term, definition)
    return (best[1], best[2]) if best else None


# -- recognizing an explain question, in Python -------------------------------------
#
# The 4B model routes among the six tools 39 of 39 on the bake-off questions;
# offering it a seventh tool for "explain" dropped that to 36 (no wording of the
# tool's description fixed it) and it still missed explain questions. So the
# model never sees an explain tool. Python recognizes the question, and the
# model only retells the steps afterwards. A phrasing this misses falls through
# to the six-tool router and gets an ordinary answer, never a wrong one.

_HOW_CALCULATED = re.compile(
    r"\bhow (?:did|do|does|is|are|was|were) (?:you|we|they|it|that|this|these|those|his|her|their|the|my)\b.*"
    r"\b(?:get|got|calculat\w*|comput\w*|work(?:ed)? out|come up|came up|derive\w*|figure\w*|built|build|arrive\w*|"
    r"decid\w*|determin\w*|reach\w*|size\w*|pric\w*|rank\w*)\b"
)
_ASK_TO_EXPLAIN = re.compile(
    r"\b(?:explain|walk me through|walk through|break down|breakdown|show (?:me )?(?:the |your )?(?:math|work|calculation|steps))\b"
)
_WHERE_FROM = re.compile(r"\bwhere does\b.*\b(?:come|number|value|score|figure|rating)\b")
_WHY_NUMBER = re.compile(
    r"\bwhy (?:is|are|was|were|does|do|did)\b.*\b(?:number|value|score|percent\w*|probability|lower|higher|factor|3yr|"
    r"bid|verdict|total|price|gap|sell|buy|hold|call|rebuild|contend)\b"
)
_WHY_YOU = re.compile(r"\bwhy (?:did|do|are|would) you (?:bench|sit|start|bid|say|call|rate|price|put|rank)\b")
_WHY_MATTER = re.compile(r"\bwhy (?:does|do|is|are)\b.*\bmatter")
_DEFINE = re.compile(r"^(?:what (?:is|are|does|do|s)|whats|define|meaning of)\b")
_BARE_FOLLOW_UP = {
    "why", "why is that", "why is that number", "why is it", "how so", "explain", "explain that", "explain this",
    "what does that mean", "what do you mean", "what does this mean", "what does it mean", "how did you get that",
    "how did you get this", "how did you calculate that",
}
_DEFINITION_FILLER = {"a", "an", "the", "mean", "means", "meaning", "of", "by", "exactly", "really", "in", "this",
                      "league", "fantasy", "does", "do", "is", "are", "s", "what", "whats", "define"}


def detect(question: str, has_recent: bool = False) -> bool:
    """True when the question asks how a number is built or what a term means.
    Deliberately narrow: an ordinary question for advice must never match,
    because it would get an explanation instead of an answer. has_recent says
    an earlier answer exists, which a bare "why?" needs to refer to."""
    q = _normalize(question)
    if not q:
        return False
    if q in _BARE_FOLLOW_UP:
        return has_recent
    if _HOW_CALCULATED.search(q) or _ASK_TO_EXPLAIN.search(q) or _WHERE_FROM.search(q) or _WHY_NUMBER.search(q) or _WHY_YOU.search(q):
        return True
    hit = lookup_glossary(question)
    if hit is None:
        return False
    if _WHY_MATTER.search(q):
        return True
    if _DEFINE.search(q):
        # "what is arbitrage?" is a definition; "what's a good FAAB bid for Justin Fields?" is not.
        rest = [w for w in q.split() if w not in _DEFINITION_FILLER]
        for alias in _aliases_for(hit[0]):
            rest = [w for w in rest if w not in alias.split()]
        return len(rest) <= 1
    return False


def _aliases_for(term: str) -> list[str]:
    aliases, _ = GLOSSARY[term]
    return [_normalize(a) for a in (term, *aliases)]


def find_player(conn: sqlite3.Connection, season: int, text: str) -> str | None:
    """The full name of the valued player the text names, longest match first,
    or None. Possessives are dropped ("Benson's" is "Benson"). Used so a
    question needn't be routed by the model to find who it is about."""
    wanted = f" {valuation.normalize_name(re.sub(r"['’]s\b", '', text or ''))} "
    best: str | None = None
    for v in valuation.player_valuations(conn, season).values():
        name = valuation.normalize_name(v["full_name"] or "")
        if name and f" {name} " in wanted and (best is None or len(name) > len(valuation.normalize_name(best))):
            best = v["full_name"]
    return best


def wants_math(topic: str) -> bool:
    """True when the topic asks for the calculation, not the definition."""
    return bool(re.search(
        r"\b(how|why|where|explain|walk|break|calculat\w*|comput\w*|work out|math|derive\w*|come up|came up|build|built|get|got)\b",
        _normalize(topic),
    ))
