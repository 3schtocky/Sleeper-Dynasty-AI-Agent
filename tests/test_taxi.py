import json
import sqlite3

import pytest

from dynasty_agent import picks, taxi, valuation
from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations

SETTINGS = {"taxi_slots": 2, "taxi_years": 1, "taxi_allow_vets": 0, "taxi_deadline": 0, "reserve_slots": 1}


def test_ir_eligibility_follows_the_leagues_reserve_flags():
    assert taxi.ir_eligible("IR", {})
    assert not taxi.ir_eligible("Out", {})
    assert taxi.ir_eligible("Out", {"reserve_allow_out": 1})
    assert not taxi.ir_eligible(None, {"reserve_allow_out": 1})


def test_taxi_eligibility_follows_years_and_the_vets_flag():
    assert taxi.taxi_eligible(0, SETTINGS)
    assert not taxi.taxi_eligible(1, SETTINGS)
    assert taxi.taxi_eligible(1, {"taxi_years": 2, "taxi_allow_vets": 1})


@pytest.fixture
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c, MIGRATIONS_DIR)
    c.execute(
        "INSERT INTO league (league_id, season, scoring_settings_json, roster_positions_json, settings_json, fetched_at) "
        "VALUES ('L1', '2026', '{}', ?, ?, 't')",
        (json.dumps(["QB", "RB", "WR", "BN", "BN", "BN"]), json.dumps(SETTINGS)),
    )
    players = [
        # id, position, years_exp, injury, ppg, win_now, three_year, slot
        ("qb", "QB", 5, None, 20.0, 14.0, 13.0, "starter"),
        ("vet_rb", "RB", 7, None, 11.0, 3.0, 2.0, "starter"),  # old: low win-now, but he starts this week
        ("wr", "WR", 3, None, 15.0, 15.0, 15.0, "starter"),
        ("rookie_ir", "RB", 0, "IR", 6.0, 6.0, 6.0, "bench"),  # would outrank vet_rb on win-now, but is on IR
        ("rookie_a", "RB", 0, None, 4.0, 4.5, 4.5, "bench"),  # outranks vet_rb on win-now, not on points
        ("rookie_b", "WR", 0, None, 2.0, 2.0, 2.0, "bench"),
    ]
    vals = {}
    for pid, pos, yrs, inj, ppg, wn, ty, slot in players:
        c.execute(
            "INSERT INTO players (player_id, full_name, position, years_exp, injury_status, fetched_at) VALUES (?, ?, ?, ?, ?, 't')",
            (pid, pid, pos, yrs, inj),
        )
        c.execute("INSERT INTO roster_players (roster_id, player_id, slot, fetched_at) VALUES (1, ?, ?, 't')", (pid, slot))
        vals[pid] = {"fantasy_points_per_game": ppg, "win_now_value": wn, "three_year_value": ty, "position": pos}
    monkeypatch.setattr(valuation, "player_valuations", lambda conn, season: vals)
    monkeypatch.setattr(picks, "inventory", lambda conn: [
        {"season": 2027, "round": r, "original_roster_id": 1, "owner_roster_id": 1} for r in (1, 2, 3)
    ])
    return c


def test_plan_sends_ir_to_ir_and_non_starting_rookies_to_taxi(conn):
    result = taxi.plan(conn, 2026, 1)
    moves = [(m["player"]["player_id"], m["to"]) for m in result["moves"]]
    # IR rookie goes to IR even though his win-now would make the best lineup:
    # he can't play this week. rookie_a goes to taxi even though his win-now
    # beats the aging vet: this week's lineup is chosen by points, not age curve.
    assert moves == [("rookie_ir", "IR"), ("rookie_a", "taxi"), ("rookie_b", "taxi")]
    assert result["bench_spots_freed"] == 3
    assert result["keep_active"] == []


def test_plan_flags_a_crunch_and_names_cut_candidates(conn):
    result = taxi.plan(conn, 2026, 1)
    # 6 rostered + 3 picks = 9 players; 6 active spots + 2 taxi for rookies = 8.
    assert (result["roster_next"], result["capacity_next"], result["overflow"]) == (9, 8, 1)
    assert [p["player_id"] for p in result["cut_candidates"]] == []  # every non-rookie here starts
