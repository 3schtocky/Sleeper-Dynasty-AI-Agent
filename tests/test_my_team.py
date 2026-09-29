"""The contend-or-rebuild verdict and the my_team view the chat answers
"am I a contender?" from."""

import pytest

from dynasty_agent import valuation
from dynasty_agent.errors import AgentError
from tests.test_integration import add_league, add_player, add_roster


def league_of(conn, monkeypatch, teams: dict[int, tuple[float, float]]):
    """One QB per roster whose (win-now, three-year) values are given
    directly; a league whose only starting slot is QB."""
    add_league(conn)
    conn.execute("UPDATE league SET roster_positions_json = '[\"QB\", \"BN\"]'")
    values = {}
    for rid, (win_now, three_year) in teams.items():
        add_roster(conn, rid)
        conn.execute("INSERT INTO players (player_id, full_name, position, fetched_at) VALUES (?, ?, 'QB', 't')",
                     (f"qb{rid}", f"QB {rid}"))
        conn.execute("INSERT INTO roster_players (roster_id, player_id, slot, fetched_at) VALUES (?, ?, 'starter', 't')",
                     (rid, f"qb{rid}"))
        values[f"qb{rid}"] = {"position": "QB", "win_now_value": win_now, "three_year_value": three_year}
    monkeypatch.setattr(valuation, "player_valuations", lambda conn, season: values)


def test_percentile_is_against_the_other_teams_only(conn, monkeypatch):
    league_of(conn, monkeypatch, {1: (30, 30), 2: (20, 20), 3: (10, 10)})
    best = valuation.contend_or_rebuild(conn, 2025, 1)
    assert best["win_now_percentile"] == 100.0  # beats both others; counting itself capped it at 83
    assert best["compared_against"] == 2
    assert valuation.contend_or_rebuild(conn, 2025, 3)["win_now_percentile"] == 0.0


@pytest.mark.parametrize("mine, expected", [
    ((30, 30), "contend"),  # top on both
    ((5, 30), "rebuild"),   # bottom now, top later
    ((15, 15), "unclear"),  # middle on both
])
def test_verdict_is_one_of_three_words_with_a_reason(conn, monkeypatch, mine, expected):
    league_of(conn, monkeypatch, {1: mine, 2: (20, 20), 3: (10, 10), 4: (25, 12), 5: (12, 25)})
    v = valuation.contend_or_rebuild(conn, 2025, 1)
    assert v["verdict"] == expected
    assert v["reason"] and v["label"].startswith(expected.upper())


def test_unclear_label_keeps_the_sentence_the_cli_always_showed(conn, monkeypatch):
    league_of(conn, monkeypatch, {1: (15, 15), 2: (20, 20), 3: (10, 10)})
    assert valuation.contend_or_rebuild(conn, 2025, 1)["label"] == (
        "UNCLEAR: NOT A CLEAN CONTENDER OR A CLEAN REBUILD ON ROSTER CONSTRUCTION ALONE"
    )


def test_confidence_grows_with_games_played(conn, monkeypatch):
    league_of(conn, monkeypatch, {1: (15, 15), 2: (20, 20)})
    assert valuation.contend_or_rebuild(conn, 2025, 1)["confidence"].startswith("low.")
    conn.execute("UPDATE rosters SET wins = 2, losses = 1 WHERE roster_id = 1")
    assert valuation.contend_or_rebuild(conn, 2025, 1)["confidence"].startswith("low to moderate")
    conn.execute(f"UPDATE rosters SET wins = {valuation.SIGNAL_GAMES} WHERE roster_id = 1")
    assert valuation.contend_or_rebuild(conn, 2025, 1)["confidence"].startswith("moderate to high")


def test_a_missing_roster_is_an_agent_error_not_a_silent_zero(conn, monkeypatch):
    league_of(conn, monkeypatch, {1: (15, 15)})
    with pytest.raises(AgentError, match="No roster 9"):
        valuation.contend_or_rebuild(conn, 2025, 9)


def test_my_team_orders_by_slot_then_value_and_keeps_players_with_no_data(conn):
    add_league(conn)
    add_roster(conn, 1)
    add_player(conn, "bench_good", "WR", 15.0, roster_id=1, slot="bench")
    add_player(conn, "bench_ok", "WR", 8.0, roster_id=1, slot="bench")
    add_player(conn, "starter", "RB", 5.0, roster_id=1, slot="starter")
    add_player(conn, "no_games", "TE", 0.0, roster_id=1, slot="bench", games=0)
    add_player(conn, "taxi", "WR", 20.0, roster_id=1, slot="taxi")
    team = valuation.my_team(conn, 2025, 1)
    assert [p["player_id"] for p in team["players"]] == ["starter", "bench_good", "bench_ok", "no_games", "taxi"]
    assert team["players"][3]["valuation"] is None
    assert team["verdict"]["verdict"] in ("contend", "rebuild", "unclear")
