"""Integration tests: real migrations on an in-memory SQLite database, a
tiny fake league written into it, and every network or parquet read
stubbed out. These exercise the SQL joins and the glue between modules,
which the pure-function tests in test_metrics.py can't reach."""

import json
import shutil
import sqlite3

import httpx
import pytest

from dynasty_agent import market, nflverse, prospects, sleeper, valuation, weather, weekly
from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations

LINEUP = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX"] + ["BN"] * 10 + ["TAXI"] * 3 + ["IR"]


@pytest.fixture
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c, MIGRATIONS_DIR)
    # Situation scores read nflverse parquet files; every team neutral here.
    monkeypatch.setattr(valuation, "team_situation_scores", lambda season: {})
    return c


def add_league(conn, settings=None):
    conn.execute(
        "INSERT INTO league (league_id, season, scoring_settings_json, roster_positions_json, settings_json, fetched_at) "
        "VALUES ('L1', '2026', '{}', ?, ?, 't')",
        (json.dumps(LINEUP), json.dumps(settings or {"playoff_week_start": 15})),
    )
    conn.execute("INSERT INTO nfl_state (fetched_at, season, week) VALUES ('t', '2026', 4)")


def add_player(conn, pid, position, fppg, roster_id=None, slot="bench", team="KC", age=25, games=4):
    conn.execute(
        "INSERT INTO players (player_id, full_name, position, team, age, fetched_at) VALUES (?, ?, ?, ?, ?, 't')",
        (pid, f"Player {pid}", position, team, age),
    )
    for week in range(1, games + 1):
        conn.execute(
            "INSERT INTO weekly_stats (player_id, season, week, position, fantasy_points, fetched_at) "
            "VALUES (?, '2025', ?, ?, ?, 't')",
            (pid, week, position, fppg),
        )
    if roster_id is not None:
        conn.execute(
            "INSERT INTO roster_players (roster_id, player_id, slot, fetched_at) VALUES (?, ?, ?, 't')",
            (roster_id, pid, slot),
        )


def add_roster(conn, roster_id, waiver_budget_used=0):
    conn.execute(
        "INSERT INTO rosters (roster_id, league_id, wins, losses, ties, waiver_budget_used, fetched_at) "
        "VALUES (?, 'L1', 0, 0, 0, ?, 't')",
        (roster_id, waiver_budget_used),
    )


def full_roster(conn, roster_id, base_fppg, slot="bench"):
    for i, pos in enumerate(["QB", "RB", "RB", "RB", "WR", "WR", "WR", "WR", "TE"]):
        add_player(conn, f"{roster_id}-{i}", pos, base_fppg + i, roster_id=roster_id, slot=slot)


# -- valuation --------------------------------------------------------------------


def test_player_valuations_joins_sleeper_players_to_weekly_stats(conn):
    add_player(conn, "p1", "WR", 15.0, games=3)
    add_player(conn, "p2", "RB", 10.0, games=0)  # no stats: absent, not a silent 0
    v = valuation.player_valuations(conn, 2025)
    assert set(v) == {"p1"}
    assert v["p1"]["games"] == 3
    assert v["p1"]["fantasy_points_per_game"] == 15.0
    assert v["p1"]["win_now_value"] == pytest.approx(15.0 * 1.05)  # WR bump, neutral age and situation


def test_contend_or_rebuild_scores_best_lineup_not_the_lineup_a_manager_last_set(conn):
    add_league(conn)
    add_roster(conn, 1)
    add_roster(conn, 2)
    # Roster 1 is stronger but its manager never set a lineup: every player
    # sits on the bench. The old starters-only version scored it 0.
    full_roster(conn, 1, base_fppg=20.0, slot="bench")
    full_roster(conn, 2, base_fppg=5.0, slot="starter")
    verdict = valuation.contend_or_rebuild(conn, 2025, my_roster_id=1)
    assert verdict["my_win_now_total"] > verdict["league_win_now_totals"][2] > 0


# -- pick valuation -------------------------------------------------------------


def fake_pick_values(prices):
    return [{"player": {"position": "PICK", "name": name}, "value": value} for name, value in prices.items()]


def test_pick_base_season_is_read_live_not_hardcoded(conn, monkeypatch):
    # After the 2027 rookie draft FantasyCalc stops pricing 2027 picks. The
    # base season has to move to 2028 on its own.
    prices = {"2028 1st": 2000, "2028 1st (Early)": 3500, "2029 1st": 1700}
    monkeypatch.setattr(market, "fetch_values", lambda conn: fake_pick_values(prices))
    assert market.priced_pick_seasons(conn, 1) == [2028, 2029]  # "(Early)" tier not mistaken for a season

    est = valuation.pick_value_estimate(conn, 2029, 1, discount_rate=0.20)
    assert est["base_season"] == 2028
    assert est["model_value"] == pytest.approx(1600.0)
    assert est["arbitrage"] == pytest.approx(-100.0)


def test_pick_for_an_already_held_draft_gets_no_model_value(conn, monkeypatch):
    monkeypatch.setattr(market, "fetch_values", lambda conn: fake_pick_values({"2028 1st": 2000}))
    assert valuation.pick_value_estimate(conn, 2027, 1, discount_rate=0.20)["model_value"] is None


def test_trade_side_names_unpriced_assets_instead_of_a_silent_zero(conn, monkeypatch):
    monkeypatch.setattr(market, "fetch_values", lambda conn: fake_pick_values({"2028 1st": 2000}))
    add_player(conn, "p1", "WR", 12.0)
    side = valuation._value_trade_side(conn, {}, [valuation.resolve_player(conn, "p1")], [(2027, 1)], discount_rate=0.2)
    assert side["unpriced"] == ["Player p1", "2027 round 1"]


# -- FAAB -------------------------------------------------------------------------


def test_faab_uses_the_leagues_real_budget(conn):
    add_league(conn, {"playoff_week_start": 15, "waiver_budget": 200})
    add_roster(conn, 1, waiver_budget_used=50)
    add_player(conn, "fa", "WR", 10.0)
    result = weekly.faab_recommendation(conn, 2025, 1, "fa")
    assert result["total_budget"] == 200
    assert result["remaining_budget"] == 150
    assert result["budget_is_default"] is False


def test_faab_budget_default_is_reported_as_a_default(conn):
    add_league(conn)
    add_roster(conn, 1)
    add_player(conn, "fa", "WR", 10.0)
    result = weekly.faab_recommendation(conn, 2025, 1, "fa")
    assert result["total_budget"] == weekly.DEFAULT_FAAB_BUDGET
    assert result["budget_is_default"] is True


def test_retired_and_teamless_players_are_not_faab_targets(conn):
    add_league(conn)
    add_roster(conn, 1)
    add_player(conn, "retired", "WR", 30.0, team=None)  # last season's best, no NFL team now
    add_player(conn, "fa", "WR", 8.0)
    add_player(conn, "mine", "WR", 20.0, roster_id=1)
    targets = weekly.top_faab_targets(conn, 2025, 1)
    assert [t["player"] for t in targets] == ["Player fa"]


# -- lineup optimizer -------------------------------------------------------------


def test_optimize_lineup_fills_every_slot_with_the_best_eligible_players(conn, monkeypatch):
    monkeypatch.setattr(weekly, "team_week_implied_points", lambda season, week: {})
    monkeypatch.setattr(weekly, "team_season_avg_implied_points", lambda season, week: {})
    add_league(conn)
    add_roster(conn, 1)
    add_player(conn, "qb", "QB", 20.0, roster_id=1)
    for i, fppg in enumerate([15.0, 12.0, 4.0]):
        add_player(conn, f"rb{i}", "RB", fppg, roster_id=1)
    for i, fppg in enumerate([14.0, 13.0, 11.0, 9.0]):
        add_player(conn, f"wr{i}", "WR", fppg, roster_id=1)
    add_player(conn, "te", "TE", 7.0, roster_id=1)
    add_player(conn, "taxi", "WR", 50.0, roster_id=1, slot="taxi")  # taxi can't start on Sleeper

    result = weekly.optimize_lineup(conn, 2025, 2026, 4, 1)
    chosen = {p["player_id"] for p in result["recommended_lineup"]}
    assert chosen == {"qb", "rb0", "rb1", "wr0", "wr1", "wr2", "te", "wr3"}  # wr3 (9.0) beats rb2 (4.0) at FLEX
    assert result["opponent_note"] == "no matchup set for this week yet"


# -- weather ----------------------------------------------------------------------


def test_wind_forecast_looks_up_the_rams_by_nflverse_code(monkeypatch):
    seen = []
    monkeypatch.setattr(weather, "_game_for_team", lambda season, week, team: seen.append(team))
    weather.game_wind_forecast(2026, 4, "LAR")
    assert seen == ["LA"]


# -- downloads ----------------------------------------------------------------------


class _BrokenStream:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self, chunk_size):
        yield b"partial"
        raise httpx.ReadError("connection dropped")


def test_interrupted_download_never_leaves_a_file_that_looks_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(nflverse.httpx, "stream", lambda *a, **kw: _BrokenStream())
    dest = tmp_path / "pbp_2025.parquet"
    with pytest.raises(httpx.ReadError):
        nflverse.download("https://example.invalid/file.parquet", dest)
    assert not dest.exists()


# -- combine ----------------------------------------------------------------------


def test_combine_row_id_prefers_pfr_then_cfb_then_name():
    assert prospects.combine_row_id(2026, "SmitJo00", "john-smith-1", "John Smith", "Ohio St.") == "2026:pfr:SmitJo00"
    assert prospects.combine_row_id(2026, None, "john-smith-1", "John Smith", "Ohio St.") == "2026:cfb:john-smith-1"
    assert prospects.combine_row_id(2026, None, None, " John Smith ", "Ohio St.") == "2026:name:john smith|ohio st."


def test_combine_migration_keeps_existing_rows(tmp_path):
    early = tmp_path / "migrations"
    early.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.stem < "0004":
            shutil.copy(path, early / path.name)
    c = sqlite3.connect(":memory:")
    apply_migrations(c, early)
    c.execute(
        "INSERT INTO nfl_combine (season, pfr_id, player_name, position, forty, fetched_at) "
        "VALUES (2025, 'DoeJa00', 'Jane Doe', 'WR', 4.41, 't')"
    )
    apply_migrations(c, MIGRATIONS_DIR)
    assert c.execute("SELECT combine_id, forty FROM nfl_combine").fetchall() == [("2025:pfr:DoeJa00", 4.41)]
    # pfr_id is nullable now: a pre-draft row with no PFR page fits.
    c.execute(
        "INSERT INTO nfl_combine (combine_id, season, cfb_id, player_name, fetched_at) "
        "VALUES ('2027:cfb:x', 2027, 'x', 'Pre Draft', 't')"
    )


# -- Sleeper league renewal ---------------------------------------------------------


def test_find_successor_league_matches_on_previous_league_id(monkeypatch):
    calls = []

    def fake_list(user_id, season):
        calls.append(season)
        return [
            {"league_id": "other", "previous_league_id": "someone-else", "season": season},
            {"league_id": "renewed", "previous_league_id": "L1", "season": season},
        ]

    monkeypatch.setattr(sleeper, "list_leagues_for_season", fake_list)
    assert sleeper.find_successor_league("u1", "L1", "2026")["league_id"] == "renewed"
    assert calls == ["2027"]


def test_find_successor_league_is_none_before_renewal(monkeypatch):
    monkeypatch.setattr(sleeper, "list_leagues_for_season", lambda user_id, season: [])
    assert sleeper.find_successor_league("u1", "L1", "2026") is None


# -- rookie values ------------------------------------------------------------------


def add_baseline_model(conn, weights):
    conn.execute(
        "INSERT INTO prospect_model (variant, features_json, weights_json, n_rows, training_classes, ridge_lambda, fitted_at) "
        "VALUES ('baseline_draft_capital', ?, ?, 400, '2018-2023', 1.0, 't')",
        (json.dumps(["pos_RB", "pos_WR", "pos_TE", "log_pick"]), json.dumps(weights)),
    )


def add_rookie(conn, pid, position, pick, years_exp=0, games=0):
    add_player(conn, pid, position, 10.0, games=games)
    conn.execute("UPDATE players SET years_exp = ? WHERE player_id = ?", (years_exp, pid))
    gsis = f"00-{pid}"
    conn.execute("INSERT INTO player_ids (row_key, gsis_id, sleeper_id, fetched_at) VALUES (?, ?, ?, 't')", (gsis, gsis, pid))
    if pick is not None:
        conn.execute(
            "INSERT INTO nfl_draft_picks (season, round, pick, gsis_id, position, fetched_at) VALUES (2026, 1, ?, ?, ?, 't')",
            (pick, gsis, position),
        )


def test_rookies_are_valued_from_the_prospect_model_not_zero(conn):
    import math
    add_baseline_model(conn, [10.0, 1.0, 0.5, -0.5, -2.0])  # intercept, pos_RB, pos_WR, pos_TE, log_pick
    add_rookie(conn, "rb1", "RB", pick=10)
    add_rookie(conn, "udfa", "WR", pick=None)
    v = valuation.player_valuations(conn, 2025)

    assert v["rb1"]["value_source"] == "prospect_model"
    assert v["rb1"]["fantasy_points_per_game"] == pytest.approx(10.0 + 1.0 - 2.0 * math.log(10))
    assert v["rb1"]["draft_pick"] == 10 and v["rb1"]["games"] == 0
    assert v["rb1"]["win_now_value"] > 0

    assert v["udfa"]["undrafted"] is True
    assert v["udfa"]["fantasy_points_per_game"] == pytest.approx(10.0 + 0.5 - 2.0 * math.log(260))


def test_a_rookies_projection_blends_into_his_real_games_with_no_cliff(conn):
    import math
    from dynasty_agent import blend
    add_baseline_model(conn, [10.0, 1.0, 0.5, -0.5, -2.0])
    add_rookie(conn, "rb2", "RB", pick=40, games=5)  # 5 real games at 10.0 PPG in 2025
    add_rookie(conn, "vet", "WR", pick=41, years_exp=4, games=0)  # not a rookie, no stats: absent
    v = valuation.player_valuations(conn, 2025)
    projection = 10.0 + 1.0 - 2.0 * math.log(40)
    k = blend.ROOKIE_PRIOR_GAMES
    assert v["rb2"]["value_source"] == "prospect_model"
    assert v["rb2"]["fantasy_points_per_game"] == pytest.approx((k * projection + 5 * 10.0) / (k + 5))
    assert "vet" not in v


def test_last_season_blends_into_this_season_by_games_played(conn):
    from dynasty_agent import blend
    add_player(conn, "wr", "WR", 20.0, games=10)  # 2025: 10 games at 20 PPG
    conn.executemany(
        "INSERT INTO weekly_stats (player_id, season, week, position, fantasy_points, fetched_at) VALUES ('wr', '2026', ?, 'WR', 8.0, 't')",
        [(1,), (2,)],
    )  # 2026: 2 games at 8 PPG
    v = valuation.player_valuations(conn, 2026)["wr"]
    k = blend.VETERAN_PRIOR_GAMES
    assert v["fantasy_points_per_game"] == pytest.approx((k * 20.0 + 16.0) / (k + 2))
    assert (v["games"], v["prior_games"], v["value_source"]) == (2, 10, "nfl_stats")


def test_no_fitted_model_means_no_rookie_values_not_a_crash(conn):
    add_rookie(conn, "rb3", "RB", pick=10)
    assert "rb3" not in valuation.player_valuations(conn, 2025)


def test_team_situations_blend_by_games_played():
    from dynasty_agent import blend
    current = {"KC": {"situation_score": 90.0, "games": 4}, "NE": {"situation_score": 40.0, "games": 3}}
    prior = {"KC": {"situation_score": 50.0, "games": 17}, "LV": {"situation_score": 30.0, "games": 17}}
    out = blend.blended_situations(current, prior)
    w = 4 / (4 + blend.VETERAN_PRIOR_GAMES)
    assert out["KC"]["situation_score"] == pytest.approx(w * 90.0 + (1 - w) * 50.0)
    assert out["NE"]["situation_score"] == 40.0  # no prior: this season alone
    assert out["LV"]["situation_score"] == 30.0  # no games yet this season: last season
