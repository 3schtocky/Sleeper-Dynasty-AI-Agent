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

from dynasty_agent import blend, metrics, prospect_model, valuation
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
    r"\b(?:get|got|calculat\w*|comput\w*|work(?:ed)? out|come up|came up|derive\w*|figure\w*|built|build|arrive\w*)\b"
)
_ASK_TO_EXPLAIN = re.compile(
    r"\b(?:explain|walk me through|walk through|break down|breakdown|show (?:me )?(?:the |your )?(?:math|work|calculation|steps))\b"
)
_WHERE_FROM = re.compile(r"\bwhere does\b.*\b(?:come|number|value|score|figure|rating)\b")
_WHY_NUMBER = re.compile(
    r"\bwhy (?:is|are|was|were|does|do|did)\b.*\b(?:number|value|score|percent\w*|probability|lower|higher|factor|3yr)\b"
)
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
    if _HOW_CALCULATED.search(q) or _ASK_TO_EXPLAIN.search(q) or _WHERE_FROM.search(q) or _WHY_NUMBER.search(q):
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
