import json
import sqlite3

import pytest

from dynasty_agent import market, picks, valuation
from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c, MIGRATIONS_DIR)
    c.execute(
        "INSERT INTO league (league_id, season, scoring_settings_json, roster_positions_json, settings_json, fetched_at) "
        "VALUES ('L1', '2026', '{}', '[]', ?, 't')",
        (json.dumps({"draft_rounds": 2, "num_teams": 3, "playoff_week_start": 15}),),
    )
    for rid, wins, losses, fpts in ((1, 2, 0, 300.0), (2, 0, 2, 150.0), (3, 1, 1, 200.0)):
        c.execute(
            "INSERT INTO rosters (roster_id, league_id, wins, losses, ties, fpts, fetched_at) VALUES (?, 'L1', ?, ?, 0, ?, 't')",
            (rid, wins, losses, fpts),
        )
    return c


def test_inventory_defaults_to_the_original_roster_and_applies_trades(conn):
    conn.execute(
        "INSERT INTO traded_picks (league_id, season, round, roster_id, previous_owner_id, owner_id, fetched_at) "
        "VALUES ('L1', '2027', 1, 2, 2, 1, 't')"
    )
    inv = picks.inventory(conn)
    assert len(inv) == 3 * 2 * 3  # 3 seasons, 2 rounds, 3 rosters
    assert {p["season"] for p in inv} == {2027, 2028, 2029}
    traded = next(p for p in inv if (p["season"], p["round"], p["original_roster_id"]) == (2027, 1, 2))
    assert traded["owner_roster_id"] == 1
    untouched = next(p for p in inv if (p["season"], p["round"], p["original_roster_id"]) == (2028, 1, 2))
    assert untouched["owner_roster_id"] == 2


def test_projected_order_leans_on_roster_strength_early_and_record_as_games_accrue(conn, monkeypatch):
    # Roster 2 is the strongest roster but 0-2; roster 1 is weakest but 2-0.
    monkeypatch.setattr(valuation, "contend_or_rebuild", lambda conn, s, rid: {"league_win_now_totals": {1: 50.0, 2: 150.0, 3: 100.0}})
    early = picks.projected_order(conn, 2026, 1)
    assert early[1]["slot"] == 1  # 2 of 14 games: roster strength dominates, weakest roster picks first
    assert early[1]["record_weight"] == pytest.approx(2 / 14)

    conn.execute("UPDATE rosters SET wins = 12, losses = 0 WHERE roster_id = 1")
    conn.execute("UPDATE rosters SET wins = 0, losses = 12 WHERE roster_id = 2")
    conn.execute("UPDATE rosters SET wins = 6, losses = 6 WHERE roster_id = 3")
    late = picks.projected_order(conn, 2026, 1)
    assert late[2]["slot"] == 1  # 12 of 14 games: the 0-12 team picks first despite its roster
    assert late[1]["slot"] == 3


def test_tier_for_slot_splits_a_round_in_thirds():
    assert [picks.tier_for_slot(s, 12) for s in (1, 4, 5, 8, 9, 12)] == ["Early", "Early", "Mid", "Mid", "Late", "Late"]


def test_advice_thresholds():
    assert picks.advice(3000, 2000) == ("SELL", 1.5)
    assert picks.advice(1700, 2000)[0] == "BUY"  # 0.85x
    assert picks.advice(1800, 2000)[0] == "HOLD"  # 0.90x, inside the band
    assert picks.advice(2000, 2000) == ("HOLD", 1.0)
    assert picks.advice(None, 2000) == ("no market comparison", None)
    assert picks.advice(2000, None) == ("no market comparison", None)


def test_fantasycalc_pick_price_prefers_the_tier_and_falls_back(conn, monkeypatch):
    values = [
        {"player": {"position": "PICK", "name": "2027 1st"}, "value": 2839},
        {"player": {"position": "PICK", "name": "2027 1st (Early)"}, "value": 4616},
        {"player": {"position": "PICK", "name": "2028 1st"}, "value": 2060},
    ]
    monkeypatch.setattr(market, "fetch_values", lambda conn: values)
    assert picks.fantasycalc_pick_price(conn, 2027, 1, "Early") == 4616
    assert picks.fantasycalc_pick_price(conn, 2028, 1, "Early") == 2060  # no tiers priced that far out
    assert picks.fantasycalc_pick_price(conn, 2029, 1, None) is None
