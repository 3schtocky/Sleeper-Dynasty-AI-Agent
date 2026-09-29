"""End-to-end checks for the paths the chat will call that had no test of
their own: a trade's numbers, the pick report's advice, Sleeper matchup
syncing, and every chat command on an empty database."""

import json
import sqlite3

import httpx
import pytest

from dynasty_agent import cli, config, market, picks, sleeper, valuation
from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations
from dynasty_agent.metrics import discounted_pick_value
from tests.test_integration import add_league, add_player, add_roster, fake_pick_values


def set_market(conn, pid, value):
    conn.execute(
        "INSERT INTO market_values (player_id, source, as_of_date, value, fetched_at) VALUES (?, 'fantasycalc', '2026-09-29', ?, 't')",
        (pid, value),
    )


# -- a trade, number by number ----------------------------------------------------


def test_trade_numbers_add_up_and_never_mix_units(conn, monkeypatch):
    monkeypatch.setattr(market, "fetch_values", lambda conn: fake_pick_values({"2027 1st": 2800, "2028 1st": 2000, "2028 2nd": 1200}))
    add_league(conn)
    conn.execute("UPDATE league SET settings_json = ?", (json.dumps({"playoff_week_start": 15, "draft_rounds": 3}),))
    add_roster(conn, 1)
    add_roster(conn, 2)
    add_player(conn, "mine", "WR", 12.0, roster_id=1)
    add_player(conn, "theirs", "RB", 10.0, roster_id=2)
    set_market(conn, "mine", 3000)
    set_market(conn, "theirs", 2500)

    r = valuation.evaluate_trade(conn, 2025, 1, ["mine"], [(2028, 2)], ["theirs"], [(2027, 1)], 0.2)
    sent, received = r["sent"], r["received"]
    v = valuation.player_valuations(conn, 2025)
    # Win-now and three-year count players only: picks can't score this year.
    assert sent["win_now_total"] == pytest.approx(v["mine"]["win_now_value"])
    assert received["win_now_total"] == pytest.approx(v["theirs"]["win_now_value"])
    assert r["win_now_delta"] == pytest.approx(received["win_now_total"] - sent["win_now_total"])
    # Market totals: FantasyCalc player values plus pick model values, one scale.
    # FantasyCalc prices no 2027 2nd here, so 2028 is the 2nd round's base season
    # and its model value is its own price; the 2027 1st is the 1st round's base.
    assert sent["picks"][0]["base_season"] == 2028 and sent["picks"][0]["model_value"] == pytest.approx(1200)
    assert sent["market_value_total"] == pytest.approx(3000 + 1200)
    assert received["market_value_total"] == pytest.approx(2500 + 2800)
    assert r["market_value_delta"] == pytest.approx(5300 - 4200)
    assert r["warnings"] == [] and r["consolidation"] is None  # two for two
    # A pick past the base season discounts forward from the base's price.
    later = valuation.pick_value_estimate(conn, 2028, 1, 0.2)
    assert later["model_value"] == pytest.approx(discounted_pick_value(2800, 1, 0.2))
    assert later["arbitrage"] == pytest.approx(later["model_value"] - 2000)


# -- the pick report's advice --------------------------------------------------------


def test_pick_report_advises_against_what_the_slot_bought_last_year(conn, monkeypatch):
    add_league(conn)
    conn.execute("UPDATE league SET settings_json = ?", (json.dumps({"playoff_week_start": 15, "draft_rounds": 1, "num_teams": 3}),))
    for rid in (1, 2, 3):
        add_roster(conn, rid)
    monkeypatch.setattr(valuation, "contend_or_rebuild", lambda conn, s, rid: {"league_win_now_totals": {1: 50.0, 2: 150.0, 3: 100.0}})
    monkeypatch.setattr(picks, "slot_history", lambda *a: {1: {"mean_league_ppg": 12.0, "hit_rate": 0.6, "n": 5}})
    monkeypatch.setattr(market, "fetch_values", lambda conn: fake_pick_values(
        {"2027 1st (Early)": 6000, "2027 1st (Mid)": 3000, "2027 1st (Late)": 1000, "2027 1st": 3000, "2028 1st": 2500, "2029 1st": 2000}
    ))
    board = [{"name": n, "market_value": v} for n, v in (("A", 4000), ("B", 3000), ("C", 2000))]
    monkeypatch.setattr(picks.prospect_model, "post_draft_board", lambda conn, cls: {"rows": board})

    report = picks.pick_report(conn, 2026, 1, 2025, {}, None)
    by_owner = {r["owner_roster_id"]: r for r in report["rows"] if r["season"] == 2027}
    # Roster 1 is weakest: 1.01, Early, 6000 against slots 1-2's median 3500 -> SELL.
    assert (by_owner[1]["projected_slot"], by_owner[1]["tier"], by_owner[1]["fantasycalc_price"]) == ("1.01", "Early", 6000)
    assert by_owner[1]["comparable_value"] == 3500 and by_owner[1]["advice"] == "SELL"
    # Roster 2 is strongest: 1.03, Late, 1000 against slots 2-3's median 2500 -> BUY.
    assert (by_owner[2]["projected_slot"], by_owner[2]["advice"]) == ("1.03", "BUY")
    assert by_owner[1]["history"] == {"mean_league_ppg": 12.0, "hit_rate": 0.6, "n": 5}
    later = [r for r in report["rows"] if r["season"] > 2027]
    assert later and all(r["advice"] == "too far out to slot" for r in later)
    assert report["next_draft"] == 2027 and report["recent_class"] == 2026


# -- Sleeper matchups, offline --------------------------------------------------------


def test_sync_matchups_stores_pairs_replaces_old_rows_and_caches(conn):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=[
            {"roster_id": 1, "matchup_id": 1, "points": 0, "starters": ["a", "0"], "players": ["a", "b"]},
            {"roster_id": 2, "matchup_id": 1, "points": 0, "starters": ["c", "d"], "players": ["c", "d"]},
        ])

    client = sleeper.SleeperClient(conn, league_id="L1")
    client._http = httpx.Client(base_url=sleeper.BASE_URL, transport=httpx.MockTransport(handler))
    conn.execute(
        "INSERT INTO matchups (league_id, week, roster_id, matchup_id, starters_json, players_json, fetched_at) "
        "VALUES ('L1', 4, 9, 5, '[]', '[]', 'old')"
    )
    client.sync_matchups(4)
    rows = conn.execute("SELECT roster_id, matchup_id, starters_json FROM matchups WHERE week = 4 ORDER BY roster_id").fetchall()
    assert [(r["roster_id"], r["matchup_id"]) for r in rows] == [(1, 1), (2, 1)]  # the stale roster 9 row is gone
    assert json.loads(rows[0]["starters_json"]) == ["a", "0"]
    client.sync_matchups(4)
    assert calls == ["/v1/league/L1/matchups/4"]  # the second sync inside 5 minutes came from the cache


# -- every chat command on an empty database -----------------------------------------


@pytest.mark.parametrize("argv", [
    ["optimize-lineup", "--week", "4"],
    ["trade", "--send", "Someone"],
    ["valuate"],
    ["digest", "--week", "4"],
    ["faab", "--player", "Someone"],
    ["picks"],
    ["taxi"],
])
def test_each_command_on_an_empty_database_says_to_refresh(monkeypatch, capsys, argv):
    empty = sqlite3.connect(":memory:")
    empty.row_factory = sqlite3.Row
    apply_migrations(empty, MIGRATIONS_DIR)
    monkeypatch.setattr(cli, "get_db", lambda: empty)
    for name, value in [("SLEEPER_USERNAME", "me"), ("SLEEPER_USER_ID", "u1"), ("LEAGUE_ID", "L1")]:
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr("sys.argv", ["dynasty-agent", *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("Error:") and "refresh" in err and "Traceback" not in err
