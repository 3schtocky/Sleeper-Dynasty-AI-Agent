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
    assert market.pick_price(conn, 2027, 1, "Early") == (4616, "2027 1st (Early)")
    assert market.pick_price(conn, 2028, 1, "Early") == (2060, "2028 1st")  # no tiers priced that far out
    assert market.pick_price(conn, 2029, 1, None) == (None, None)
    assert market.pick_market_value(conn, 2027, 1) == 2839


# -- reading a pick the way people write it ------------------------------------------


@pytest.mark.parametrize("spec", [
    "2027-1", "2027 1st", "2027 round 1", "'27 first", "’27 1st", "2027-1st", "2027 1", "27 1st",
    "2027 first round", "1st round 2027", "2027 rd 1", "2027 R1", "2027 1st round pick", "2027, 1st",
])
def test_parse_pick_reads_every_common_way_to_write_a_2027_first(spec):
    assert picks.parse_pick(spec) == picks.Pick(2027, 1)


def test_parse_pick_reads_tiers_and_slots():
    assert picks.parse_pick("2027 early 1st") == picks.Pick(2027, 1, "Early")
    assert picks.parse_pick("2027 1st (Late)") == picks.Pick(2027, 1, "Late")
    assert picks.parse_pick("2027 mid second") == picks.Pick(2027, 2, "Mid")
    assert picks.parse_pick("2027 1.05") == picks.Pick(2027, 1, "Mid", 5)  # 12 teams: 1-4 early, 5-8 mid
    assert picks.parse_pick("2027 2.11") == picks.Pick(2027, 2, "Late", 11)
    assert picks.parse_pick("2028 3rd") == picks.Pick(2028, 3)


@pytest.mark.parametrize("spec", ["", "2027", "first", "a 1st", "Justin Jefferson"])
def test_parse_pick_rejects_what_isnt_a_pick(spec):
    with pytest.raises(ValueError, match="Couldn't read"):
        picks.parse_pick(spec)


def test_next_years_pick_means_the_next_draft_when_the_league_is_known():
    for spec in ("next year's 1st", "next year first", "next draft 2nd", "my upcoming 1st"):
        assert picks.parse_pick(spec, first_season=2027).season == 2027
    with pytest.raises(ValueError, match="Couldn't read"):
        picks.parse_pick("next year's 1st")  # without the league, "next year" is unknown


def test_parse_pick_checks_the_leagues_bounds():
    kwargs = {"rounds": 3, "first_season": 2027, "last_season": 2029, "num_teams": 12}
    with pytest.raises(ValueError, match="3 rounds, there's no round 4"):
        picks.parse_pick("2027 4th", **kwargs)
    with pytest.raises(ValueError, match="2026 rookie draft already happened"):
        picks.parse_pick("2026 1st", **kwargs)
    with pytest.raises(ValueError, match="through the 2029 draft"):
        picks.parse_pick("2030 1st", **kwargs)
    with pytest.raises(ValueError, match="1.01 through 1.12"):
        picks.parse_pick("2027 1.13", **kwargs)


def test_parse_league_pick_reads_the_leagues_own_settings(conn):
    # The fixture league: 2026 season, 2 rounds, 3 teams.
    assert picks.parse_league_pick(conn, "2027 2nd") == picks.Pick(2027, 2)
    assert picks.parse_league_pick(conn, "2027 1.03") == picks.Pick(2027, 1, "Late", 3)
    with pytest.raises(ValueError, match="2 rounds"):
        picks.parse_league_pick(conn, "2027 3rd")


def test_next_draft_is_an_unfinished_draft_sleeper_lists_else_after_the_league_season(conn):
    assert picks.next_draft_season(conn) == 2027
    conn.execute("INSERT INTO drafts (draft_id, league_id, season, status, fetched_at) VALUES ('d', 'L1', '2026', 'pre_draft', 't')")
    assert picks.next_draft_season(conn) == 2026  # a renewed league before its rookie draft
    conn.execute("UPDATE drafts SET status = 'complete'")
    assert picks.next_draft_season(conn) == 2027


# -- one price for a pick, whichever tool asks -----------------------------------------


def test_my_own_next_draft_pick_is_priced_at_its_projected_tier(conn, monkeypatch):
    monkeypatch.setattr(valuation, "contend_or_rebuild", lambda conn, s, rid: {"league_win_now_totals": {1: 50.0, 2: 150.0, 3: 100.0}})
    # Roster 1 is weakest: its own 2027 1st projects to slot 1 of 3, Early.
    assert picks.projected_tier(conn, 2026, 1, picks.Pick(2027, 1)) == picks.Pick(2027, 1, "Early", 1)
    # A later draft can't be slotted, and a tier the user gave is kept.
    assert picks.projected_tier(conn, 2026, 1, picks.Pick(2028, 1)) == picks.Pick(2028, 1)
    assert picks.projected_tier(conn, 2026, 1, picks.Pick(2027, 1, "Late")) == picks.Pick(2027, 1, "Late")


def test_holding_two_picks_in_a_round_leaves_the_tier_unknown(conn, monkeypatch):
    monkeypatch.setattr(valuation, "contend_or_rebuild", lambda conn, s, rid: {"league_win_now_totals": {1: 50.0, 2: 150.0, 3: 100.0}})
    conn.execute(
        "INSERT INTO traded_picks (league_id, season, round, roster_id, previous_owner_id, owner_id, fetched_at) "
        "VALUES ('L1', '2027', 1, 2, 2, 1, 't')"
    )
    assert picks.projected_tier(conn, 2026, 1, picks.Pick(2027, 1)) == picks.Pick(2027, 1)  # which one? unknown


def test_trade_and_pick_report_price_a_tiered_pick_the_same(conn, monkeypatch):
    values = [
        {"player": {"position": "PICK", "name": "2027 1st"}, "value": 2834},
        {"player": {"position": "PICK", "name": "2027 1st (Early)"}, "value": 4608},
        {"player": {"position": "PICK", "name": "2028 1st"}, "value": 2048},
    ]
    monkeypatch.setattr(market, "fetch_values", lambda conn: values)
    est = valuation.pick_value_estimate(conn, 2027, 1, 0.2, "Early")
    assert est["market_value"] == est["model_value"] == market.pick_market_value(conn, 2027, 1, "Early") == 4608
    assert est["price_label"] == "2027 1st (Early)"
    untiered = valuation.pick_value_estimate(conn, 2027, 1, 0.2)
    assert (untiered["market_value"], untiered["price_label"]) == (2834, "2027 1st")
    later = valuation.pick_value_estimate(conn, 2028, 1, 0.2, "Early")  # no tiers that far out
    assert later["price_label"] == "2028 1st"


def test_the_cli_trade_default_discount_is_the_one_in_picks(monkeypatch):
    from dynasty_agent import cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_trade", lambda args: seen.update(rate=args.discount_rate))
    monkeypatch.setattr("sys.argv", ["dynasty-agent", "trade"])
    cli.main()
    assert seen["rate"] == picks.DISCOUNT_RATE == picks.valuation_discount_rate()
