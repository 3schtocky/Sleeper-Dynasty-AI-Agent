"""Phase 4 taxi and IR planning: which players to move off the active
roster right now, and whether next year's rookie draft forces a cut.

Every rule comes from the league's own settings, never assumed:
taxi_slots, taxi_years (years of experience a taxi player may have),
taxi_allow_vets, taxi_deadline (0 = moves allowed all season),
reserve_slots and the reserve_allow_* flags (which injury designations the
IR slot accepts; Sleeper's "IR" designation always qualifies). Active
capacity is the league's starting slots plus BN slots from roster_positions.

Found on the live run that motivated this: the user's roster was full
(18 of 18 active), all three rookies sat on the bench, the 3 taxi slots
were empty, and one rookie carried an IR designation with the IR slot
open. Each of those is a bench spot for a waiver claim, free.
"""

from __future__ import annotations

import json
import sqlite3

from dynasty_agent import picks, valuation
from dynasty_agent.metrics import best_lineup_total, starting_slot_counts

RESERVE_FLAGS = {
    "Out": "reserve_allow_out",
    "Doubtful": "reserve_allow_doubtful",
    "Sus": "reserve_allow_sus",
    "Suspended": "reserve_allow_sus",
    "NA": "reserve_allow_na",
    "DNR": "reserve_allow_dnr",
    "COV": "reserve_allow_cov",
}


def ir_eligible(injury_status: str | None, settings: dict) -> bool:
    if injury_status == "IR":
        return True
    flag = RESERVE_FLAGS.get(injury_status or "")
    return bool(flag and settings.get(flag))


def taxi_eligible(years_exp: int | None, settings: dict) -> bool:
    years = years_exp or 0
    if not settings.get("taxi_allow_vets") and years > 0:
        return False
    return years < (settings.get("taxi_years") or 1)


# Designations that keep a player out of this week's lineup. Season-long
# value ignores them on purpose (a dynasty asset on IR is still an asset),
# however "would he start right now" can't: the first live run marked an IR
# rookie as a starter and kept him off the open IR slot.
UNAVAILABLE = ("IR", "Out", "Suspended", "Sus", "PUP", "NA", "DNR", "COV")


def _best_lineup_ids(players: list[dict], slot_counts: dict[str, int]) -> set[str]:
    """Which player ids make this week's best lineup, by projected points
    per game (the blended average, no age discount), by removing each one
    and seeing whether the best total drops. Not win-now value: its age
    curve is right for dynasty worth, wrong for "does he start this week"
    (the first live run kept a 4.4 PPG rookie active over a 29-year-old
    RB averaging 11.7)."""
    pairs = [(p["position"], p["ppg"]) for p in players]
    full = best_lineup_total(pairs, slot_counts)
    starters = set()
    for i, p in enumerate(players):
        without = pairs[:i] + pairs[i + 1:]
        if best_lineup_total(without, slot_counts) < full - 1e-9:
            starters.add(p["player_id"])
    return starters


def taxi_locked(settings: dict, current_week: int | None) -> bool:
    """Whether the league's taxi deadline has passed. Sleeper's taxi_deadline
    is 0 for no deadline; a nonzero value is read here as the week taxi moves
    stop, an interpretation the CLI states rather than hides (Sleeper
    doesn't document the field, and this league's is 0)."""
    deadline = settings.get("taxi_deadline") or 0
    return bool(deadline) and current_week is not None and current_week >= deadline


def plan(conn: sqlite3.Connection, stats_season: int, my_roster_id: int) -> dict:
    league = conn.execute("SELECT season, settings_json, roster_positions_json FROM league ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if league is None:
        raise ValueError("No league data cached yet. Run `dynasty-agent refresh` first.")
    settings = json.loads(league["settings_json"])
    positions = json.loads(league["roster_positions_json"])
    slot_counts = starting_slot_counts(positions)
    active_capacity = sum(slot_counts.values()) + positions.count("BN")
    taxi_slots = settings.get("taxi_slots") or 0
    reserve_slots = settings.get("reserve_slots") or 0

    valuations = valuation.player_valuations(conn, stats_season)
    roster = []
    for r in conn.execute(
        """
        SELECT rp.player_id, rp.slot, p.full_name, p.position, p.years_exp, p.injury_status
        FROM roster_players rp JOIN players p ON p.player_id = rp.player_id
        WHERE rp.roster_id = ?
        """,
        (my_roster_id,),
    ):
        v = valuations.get(r["player_id"], {})
        roster.append({
            **dict(r),
            "ppg": v.get("fantasy_points_per_game") or 0.0,
            "win_now_value": v.get("win_now_value", 0.0),
            "three_year_value": v.get("three_year_value", 0.0),
            "value_source": v.get("value_source"),
        })

    active = [p for p in roster if p["slot"] in ("starter", "bench")]
    available = [p for p in active if p["injury_status"] not in UNAVAILABLE]
    starters = _best_lineup_ids(available, slot_counts)
    taxi_now = [p for p in roster if p["slot"] == "taxi"]
    ir_now = [p for p in roster if p["slot"] == "reserve"]

    state = conn.execute("SELECT week FROM nfl_state ORDER BY fetched_at DESC LIMIT 1").fetchone()
    current_week = state["week"] if state else None
    locked = taxi_locked(settings, current_week)

    moves = []
    # IR first: any IR-eligible active player outside the best lineup, most
    # valuable first (he's the one worth keeping rather than cutting).
    ir_open = reserve_slots - len(ir_now)
    for p in sorted(active, key=lambda p: -p["three_year_value"]):
        if ir_open <= 0:
            break
        if p["player_id"] not in starters and ir_eligible(p["injury_status"], settings):
            slot_word = "the IR slot is" if reserve_slots == 1 else "an IR slot is"
            moves.append({"player": p, "to": "IR", "why": f"designated {p['injury_status']}, {slot_word} open"})
            ir_open -= 1
    moved = {m["player"]["player_id"] for m in moves}

    # Taxi: eligible active players outside the best lineup, the most
    # long-term value first (the stash worth protecting).
    taxi_open = 0 if locked else taxi_slots - len(taxi_now)
    for p in sorted(active, key=lambda p: -p["three_year_value"]):
        if taxi_open <= 0:
            break
        if p["player_id"] in moved or p["player_id"] in starters or not taxi_eligible(p["years_exp"], settings):
            continue
        moves.append({"player": p, "to": "taxi", "why": "not in your best lineup, and taxi players can't be started anyway"})
        taxi_open -= 1

    keep_active = [p for p in active if p["player_id"] in starters and taxi_eligible(p["years_exp"], settings)]

    # Next season's crunch: every rostered player carries over, current
    # taxi players graduate back to the active roster (taxi_years), and the
    # next draft's rookies can fill the taxi slots first.
    next_draft = picks.next_draft_season(conn)
    next_picks = [
        p for p in picks.inventory(conn)
        if p["season"] == next_draft and p["owner_roster_id"] == my_roster_id
    ]
    # Who leaves taxi for the active roster next season: today's taxi
    # players past taxi_years, plus the rookies moved there now.
    graduating = [p for p in taxi_now if (p["years_exp"] or 0) + 1 >= (settings.get("taxi_years") or 1)]
    graduating += [m["player"] for m in moves if m["to"] == "taxi"]
    # IR isn't counted as next-season room: it's temporary, and a player
    # stashed there today is most likely healthy by the draft.
    total_next = len(roster) + len(next_picks)
    capacity_next = active_capacity + min(len(next_picks), taxi_slots)
    overflow = total_next - capacity_next
    # A cut candidate is judged on the season, not this week: an injured
    # starter is out of this week's lineup but is no one to cut.
    season_starters = _best_lineup_ids(active + ir_now, slot_counts)
    cut_candidates = sorted(
        (p for p in roster if p["player_id"] not in season_starters and (p["years_exp"] or 0) > 0),
        key=lambda p: p["three_year_value"],
    )[: max(overflow, 0)]

    return {
        "settings": {
            "taxi_slots": taxi_slots, "taxi_years": settings.get("taxi_years"), "taxi_allow_vets": bool(settings.get("taxi_allow_vets")),
            "taxi_deadline": settings.get("taxi_deadline"), "reserve_slots": reserve_slots,
        },
        "current_week": current_week, "taxi_locked": locked,
        "active_count": len(active), "active_capacity": active_capacity,
        "taxi_now": taxi_now, "ir_now": ir_now, "moves": moves, "keep_active": keep_active,
        "bench_spots_freed": len(moves),
        "next_draft": next_draft, "next_picks": len(next_picks), "graduating": graduating,
        "roster_next": total_next, "capacity_next": capacity_next, "overflow": overflow, "cut_candidates": cut_candidates,
    }
