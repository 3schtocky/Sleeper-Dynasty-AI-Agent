"""Phase 4 pick advice: every team's rookie picks, where the next draft's
picks project to land, what each slot has really returned, and whether to
buy, hold, or sell a pick at its current market price.

Three pieces, each stated where it's shown:

1. Inventory. Sleeper's traded_picks lists only picks that changed hands;
   every other pick belongs to its original roster. Seasons: the next three
   drafts after the league's current season, rounds from draft_rounds.

2. Projected slot, next draft only (order for later drafts is unknowable).
   Reverse standings, projected: each roster's strength is its win-now
   total's percentile (best possible lineup, see valuation) blended toward
   its real record's percentile as the regular season is played, weight =
   games played / regular-season games. Weakest projects to pick first.
   Sleeper's real draft order, once the commissioner sets it, isn't used
   yet: nothing has set it for 2027.

3. Value, market against market. FantasyCalc prices a projected pick by
   tier ("2027 1st (Early)"). The comparable is FantasyCalc's own current
   value for the rookies who went at that same overall slot in the most
   recent class, ranked the way a draft-capital board ranks them. A pick
   priced well above the player that slot actually bought last year is a
   sell; well below, a buy. Both numbers are FantasyCalc's, so no unit
   conversion sits between them. Separately, each slot's real history
   (2018 onward): the average league-scored points per game its rookie
   produced over three seasons and how often that rookie became startable.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from typing import NamedTuple

from dynasty_agent import market, prospect_model, valuation
from dynasty_agent.metrics import percentile_rank, production_score

PICK_SEASONS_AHEAD = 3
# A pick's rookie counts as a hit when he averaged at least this many
# league-weighted points per game scheduled over his first 3 seasons,
# roughly a weekly flex starter in a 12-team, 8-starter league. A round,
# stated number, not fitted.
HIT_LEAGUE_PPG = 10.0
# Buy/sell bands on the market-against-market ratio. Round, stated, not fitted.
SELL_ABOVE = 1.15
BUY_BELOW = 0.87
TIER_LABELS = ("Early", "Mid", "Late")


def league_settings(conn: sqlite3.Connection) -> tuple[int, dict]:
    row = conn.execute("SELECT season, settings_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if row is None:
        raise ValueError("No league data cached yet. Run `dynasty-agent refresh` first.")
    return int(row["season"]), json.loads(row["settings_json"])


def league_shape(conn: sqlite3.Connection, settings: dict | None = None) -> tuple[int, int]:
    """(teams, rookie draft rounds) from the league's settings. A league
    missing num_teams counts its rosters; missing draft_rounds falls back to
    3, the only value this can't read elsewhere, and that one is stated."""
    if settings is None:
        _, settings = league_settings(conn)
    teams = settings.get("num_teams") or conn.execute("SELECT count(*) FROM rosters").fetchone()[0] or 12
    return teams, settings.get("draft_rounds") or 3


def next_draft_season(conn: sqlite3.Connection) -> int:
    """The next rookie draft still to happen: a league draft Sleeper lists as
    not complete (a renewed league before its draft), else the season after
    the league's own. Sleeper doesn't always list past drafts (this league's
    table is empty), so an absent draft means "after the league season"."""
    league_season, _ = league_settings(conn)
    row = conn.execute(
        "SELECT min(CAST(season AS INTEGER)) FROM drafts WHERE status != 'complete' AND CAST(season AS INTEGER) >= ?",
        (league_season,),
    ).fetchone()
    return row[0] if row and row[0] is not None else league_season + 1


class Pick(NamedTuple):
    """A rookie pick as a person names it. tier ("Early"/"Mid"/"Late") and
    slot (1.05 -> 5) are None unless given or projected."""

    season: int
    round: int
    tier: str | None = None
    slot: int | None = None


_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
_TIER_WORDS = {"early": "Early", "mid": "Mid", "middle": "Mid", "late": "Late"}
_PICK_FORMAT_HINT = "Write it like '2027 1st', '2027-1' or '2027 1.05'."


def parse_pick(spec: str, rounds: int | None = None, first_season: int | None = None,
               last_season: int | None = None, num_teams: int = 12) -> Pick:
    """Read a pick the way people write it: '2027-1', '2027 1st', '2027 round
    1', "'27 first", '2027-1st', '2027 early 1st', '2027 1.05'. A slot sets
    the tier. Raises ValueError, saying what's wrong, on anything it can't
    read or a round or season outside the given bounds. "next year's 1st"
    means the first_season draft when bounds are given."""
    text = (spec or "").lower().replace("’", "'")
    text = re.sub(r"[-_/,()]", " ", text)

    tier = None
    for word, label in _TIER_WORDS.items():
        if re.search(rf"\b{word}\b", text):
            tier = label
            text = re.sub(rf"\b{word}\b", " ", text)

    season = None
    # "next year's first": the next draft, known only with the league's bounds.
    relative = re.search(r"\b(?:next (?:year|season|draft)|upcoming|this coming)\b", text)
    if relative and first_season is not None:
        season = first_season
        text = text[: relative.start()] + " " + text[relative.end():]
    for pattern, offset in ((r"\b(20\d\d)\b", 0), (r"'(\d\d)\b", 2000), (r"(?<![.\d])\b(\d\d)\b(?![.\d])", 2000)):
        if season is not None:
            break
        m = re.search(pattern, text)
        if m:
            season = int(m.group(1)) + offset
            text = text[: m.start()] + " " + text[m.end():]
            break

    rnd = slot = None
    m = re.search(r"\b(\d)\.(\d{1,2})\b", text)
    if m:
        rnd, slot = int(m.group(1)), int(m.group(2))
    else:
        for pattern in (r"\b(\d)(?:st|nd|rd|th)\b", r"\b(?:round|rd|r)\s*(\d)\b", r"\b(\d)\b"):
            m = re.search(pattern, text)
            if m:
                rnd = int(m.group(1))
                break
        if rnd is None:
            rnd = next((n for word, n in _ORDINALS.items() if re.search(rf"\b{word}\b", text)), None)

    if season is None or rnd is None:
        raise ValueError(f"Couldn't read '{spec}' as a draft pick. {_PICK_FORMAT_HINT}")
    if rounds is not None and not 1 <= rnd <= rounds:
        raise ValueError(f"'{spec}': this league's rookie draft has {rounds} rounds, there's no round {rnd}.")
    if first_season is not None and season < first_season:
        raise ValueError(f"'{spec}': the {season} rookie draft already happened, the next one is {first_season}.")
    if last_season is not None and season > last_season:
        raise ValueError(f"'{spec}': picks can be valued through the {last_season} draft, not {season}.")
    if slot is not None:
        if not 1 <= slot <= num_teams:
            raise ValueError(f"'{spec}': a {num_teams}-team league has picks {rnd}.01 through {rnd}.{num_teams:02d}.")
        tier = tier_for_slot(slot, num_teams)
    return Pick(season, rnd, tier, slot)


def parse_league_pick(conn: sqlite3.Connection, spec: str) -> Pick:
    """parse_pick checked against this league: its draft rounds, its team
    count, and the drafts whose picks exist (the next PICK_SEASONS_AHEAD)."""
    teams, rounds = league_shape(conn)
    first = next_draft_season(conn)
    return parse_pick(
        spec, rounds=rounds, first_season=first, last_season=first + PICK_SEASONS_AHEAD - 1, num_teams=teams,
    )


def inventory(conn: sqlite3.Connection) -> list[dict]:
    """Every pick in the next PICK_SEASONS_AHEAD drafts: season, round, the
    roster it originally belonged to, and the roster that holds it now."""
    _, rounds = league_shape(conn)
    first = next_draft_season(conn)
    roster_ids = [r[0] for r in conn.execute("SELECT roster_id FROM rosters ORDER BY roster_id")]
    traded = {
        (int(r["season"]), r["round"], r["roster_id"]): r["owner_id"]
        for r in conn.execute("SELECT season, round, roster_id, owner_id FROM traded_picks")
    }
    picks = []
    for season in range(first, first + PICK_SEASONS_AHEAD):
        for rnd in range(1, rounds + 1):
            for original in roster_ids:
                picks.append({
                    "season": season, "round": rnd, "original_roster_id": original,
                    "owner_roster_id": traded.get((season, rnd, original), original),
                })
    return picks


def projected_order(conn: sqlite3.Connection, stats_season: int, my_roster_id: int) -> dict[int, dict]:
    """{roster_id: {"slot", "strength", "record_weight"}} for the next draft,
    slot 1 = projected weakest. See the module docstring."""
    _, settings = league_settings(conn)
    regular_season_games = max((settings.get("playoff_week_start") or 15) - 1, 1)
    verdict = valuation.contend_or_rebuild(conn, stats_season, my_roster_id)
    win_now = verdict["league_win_now_totals"]
    records = {
        r["roster_id"]: ((r["wins"] or 0) + 0.5 * (r["ties"] or 0), (r["wins"] or 0) + (r["losses"] or 0) + (r["ties"] or 0), r["fpts"] or 0.0)
        for r in conn.execute("SELECT roster_id, wins, losses, ties, fpts FROM rosters")
    }
    played = max((g for _, g, _ in records.values()), default=0)
    record_weight = min(played / regular_season_games, 1.0)
    win_pcts = {rid: (w / g if g else 0.5) for rid, (w, g, _) in records.items()}
    strength = {}
    for rid in records:
        roster_pct = percentile_rank(win_now.get(rid, 0.0), list(win_now.values()))
        record_pct = percentile_rank(win_pcts[rid], list(win_pcts.values()))
        strength[rid] = record_weight * record_pct + (1 - record_weight) * roster_pct
    # Weakest first; points scored breaks exact ties the way standings do.
    ordered = sorted(records, key=lambda rid: (strength[rid], records[rid][2]))
    return {rid: {"slot": i + 1, "strength": strength[rid], "record_weight": record_weight} for i, rid in enumerate(ordered)}


def tier_for_slot(slot: int, num_teams: int) -> str:
    size = num_teams / len(TIER_LABELS)
    return TIER_LABELS[min(int((slot - 1) // size), len(TIER_LABELS) - 1)]


def slot_history(conn: sqlite3.Connection, scoring_settings: dict, last_complete_season: int, num_teams: int, rounds: int) -> dict[int, dict]:
    """{overall rookie-draft slot: {"mean_league_ppg", "hit_rate", "n"}},
    from every class since the model's first training class with three
    completed NFL seasons. Within each class, rookies are ordered the way
    the draft-capital board orders them (its projection, league-weighted),
    the order a rookie draft roughly follows, so slot k is the k-th rookie
    off that board. Neighboring slots are pooled (k-1..k+1) to steady a
    sample of one rookie per class per slot."""
    model = prospect_model.load_model(conn, "baseline_draft_capital")
    if model is None:
        raise ValueError("No fitted prospect model yet. Run `dynasty-agent fit-prospect-model` first.")
    classes = prospect_model.training_classes(last_complete_season)
    points = prospect_model.first_three_season_ppg(scoring_settings, range(classes.start, classes.stop + 2))
    by_slot: dict[int, list[float]] = {}
    total_slots = num_teams * rounds
    for draft_class in classes:
        rookies = []
        for r in conn.execute(
            "SELECT pick, position, gsis_id FROM nfl_draft_picks WHERE season = ? AND position IN ('QB', 'RB', 'WR', 'TE')",
            (draft_class,),
        ):
            feats = {"pos_RB": float(r["position"] == "RB"), "pos_WR": float(r["position"] == "WR"),
                     "pos_TE": float(r["position"] == "TE"), "log_pick": math.log(r["pick"])}
            board = production_score(prospect_model.predict(model["weights"], [feats[n] for n in model["features"]]), r["position"])
            n_games = sum(prospect_model.games_scheduled(s) for s in range(draft_class, draft_class + 3))
            realized = production_score(points.get((r["gsis_id"], draft_class), 0.0) / n_games, r["position"])
            rookies.append((board, realized))
        rookies.sort(key=lambda t: -t[0])
        for slot, (_, realized) in enumerate(rookies[:total_slots], start=1):
            by_slot.setdefault(slot, []).append(realized)
    history = {}
    for slot in range(1, total_slots + 1):
        pooled = [v for s in (slot - 1, slot, slot + 1) for v in by_slot.get(s, [])]
        if pooled:
            history[slot] = {
                "mean_league_ppg": sum(pooled) / len(pooled),
                "hit_rate": sum(v >= HIT_LEAGUE_PPG for v in pooled) / len(pooled),
                "n": len(pooled),
            }
    return history


def comparable_rookie_value(conn: sqlite3.Connection, recent_class: int, overall_slot: int,
                            board: list[dict] | None = None) -> tuple[float | None, list[str]]:
    """FantasyCalc's current value for the rookies at overall_slot (pooled
    with the slots either side) in recent_class, ordered by the
    draft-capital board. Returns (median value, their names). board is
    post_draft_board(conn, recent_class)["rows"] when the caller has it
    already: building it runs the prospect model over the whole class."""
    if board is None:
        board = prospect_model.post_draft_board(conn, recent_class)["rows"]
    window = [r for i, r in enumerate(board, start=1) if abs(i - overall_slot) <= 1 and r["market_value"] is not None]
    if not window:
        return None, []
    return statistics.median(r["market_value"] for r in window), [r["name"] for r in window]


def advice(pick_price: float | None, comparable: float | None) -> tuple[str, float | None]:
    if pick_price is None or not comparable:
        return "no market comparison", None
    ratio = pick_price / comparable
    if ratio > SELL_ABOVE:
        return "SELL", ratio
    if ratio < BUY_BELOW:
        return "BUY", ratio
    return "HOLD", ratio


def projected_tier(conn: sqlite3.Connection, stats_season: int, my_roster_id: int, pick: Pick) -> Pick:
    """The tier of a pick the user holds, when that's knowable: a pick in the
    next draft, no tier given, and exactly one pick of that season and round
    held (so which team's pick it is isn't in doubt). The tier comes from the
    original team's projected slot, as in pick_report. Otherwise the pick
    comes back unchanged and gets FantasyCalc's untiered price."""
    if pick.tier is not None or pick.season != next_draft_season(conn):
        return pick
    held = [p for p in inventory(conn)
            if p["owner_roster_id"] == my_roster_id and (p["season"], p["round"]) == (pick.season, pick.round)]
    if len(held) != 1:
        return pick
    teams, _ = league_shape(conn)
    slot = projected_order(conn, stats_season, my_roster_id)[held[0]["original_roster_id"]]["slot"]
    return pick._replace(tier=tier_for_slot(slot, teams), slot=slot)


def pick_report(conn: sqlite3.Connection, stats_season: int, my_roster_id: int, last_complete_season: int,
                scoring_settings: dict, roster_filter: int | None) -> dict:
    """Every pick (or only those roster_filter holds) with its projection,
    history, market price, comparable, and advice."""
    _, settings = league_settings(conn)
    num_teams, rounds = league_shape(conn, settings)
    next_draft = next_draft_season(conn)
    order = projected_order(conn, stats_season, my_roster_id)
    history = slot_history(conn, scoring_settings, last_complete_season, num_teams, rounds)
    recent_class = next_draft - 1  # the last class drafted, already valued by the market
    board = prospect_model.post_draft_board(conn, recent_class)["rows"]  # once, not per pick
    rows = []
    for p in inventory(conn):
        if roster_filter is not None and p["owner_roster_id"] != roster_filter:
            continue
        row = {**p}
        if p["season"] == next_draft:
            slot = order[p["original_roster_id"]]["slot"]
            tier = tier_for_slot(slot, num_teams)
            overall = (p["round"] - 1) * num_teams + slot
            price = market.pick_market_value(conn, p["season"], p["round"], tier)
            comparable, names = comparable_rookie_value(conn, recent_class, overall, board)
            call, ratio = advice(price, comparable)
            row.update({
                "projected_slot": f"{p['round']}.{slot:02d}", "tier": tier, "overall": overall,
                "history": history.get(overall), "fantasycalc_price": price,
                "comparable_value": comparable, "comparable_players": names, "advice": call, "ratio": ratio,
            })
        else:
            est = valuation.pick_value_estimate(conn, p["season"], p["round"], valuation_discount_rate())
            row.update({
                "projected_slot": None, "tier": None, "overall": None,
                "history": _round_history(history, p["round"], num_teams),
                "fantasycalc_price": est["market_value"], "model_value": est["model_value"],
                "advice": "too far out to slot", "ratio": None,
            })
        rows.append(row)
    rows.sort(key=lambda r: (r["season"], r["round"], r["overall"] or 0, r["original_roster_id"]))
    return {"next_draft": next_draft, "recent_class": recent_class, "order": order, "rows": rows,
            "record_weight": next(iter(order.values()))["record_weight"] if order else 0.0}


def _round_history(history: dict[int, dict], rnd: int, num_teams: int) -> dict | None:
    """The average of every slot's history in one round, for picks too far
    out to slot."""
    slots = [history[s] for s in range((rnd - 1) * num_teams + 1, rnd * num_teams + 1) if s in history]
    if not slots:
        return None
    return {
        "mean_league_ppg": sum(s["mean_league_ppg"] for s in slots) / len(slots),
        "hit_rate": sum(s["hit_rate"] for s in slots) / len(slots),
        "n": sum(s["n"] for s in slots),
    }


DISCOUNT_RATE = 0.20


def valuation_discount_rate() -> float:
    """The default future-pick discount per year, the one place it's set:
    the trade evaluator, the CLI's --discount-rate default and the pick
    report all read it here."""
    return DISCOUNT_RATE
