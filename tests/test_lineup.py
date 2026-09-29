"""The lineup optimizer against the cases a real week throws at it: an
opponent who hasn't set a lineup, slots the roster can't fill, byes when the
lines aren't out, a rookie with no variance yet, and Sleeper being down."""

import json
import statistics

import httpx
import pytest

from dynasty_agent import weekly
from tests.test_integration import add_league, add_player, add_roster


@pytest.fixture
def week(monkeypatch):
    """No Vegas lines, KC and BUF scheduled, every other team idle."""
    state = {"implied": {}, "playing": {"KC", "BUF"}}
    monkeypatch.setattr(weekly, "team_week_implied_points", lambda season, week: state["implied"])
    monkeypatch.setattr(weekly, "team_season_avg_implied_points", lambda season, week: {})
    monkeypatch.setattr(weekly, "teams_playing", lambda season, week: state["playing"])
    return state


def my_team(conn, roster_id=1, team="KC", fppg=10.0):
    add_player(conn, f"{roster_id}qb", "QB", fppg + 8, roster_id=roster_id, team=team)
    for i in range(3):
        add_player(conn, f"{roster_id}rb{i}", "RB", fppg + 2 - i, roster_id=roster_id, team=team)
    for i in range(4):
        add_player(conn, f"{roster_id}wr{i}", "WR", fppg + 3 - i, roster_id=roster_id, team=team)
    add_player(conn, f"{roster_id}te", "TE", fppg - 2, roster_id=roster_id, team=team)


def pair(conn, opponent_starters):
    for rid, starters in ((1, []), (2, opponent_starters)):
        conn.execute(
            "INSERT INTO matchups (league_id, week, roster_id, matchup_id, starters_json, players_json, fetched_at) "
            "VALUES ('L1', 4, ?, 1, ?, '[]', 't')",
            (rid, json.dumps(starters)),
        )


def setup(conn, opponent_fppg=9.0):
    add_league(conn)
    add_roster(conn, 1)
    add_roster(conn, 2)
    my_team(conn, 1)
    my_team(conn, 2, team="BUF", fppg=opponent_fppg)


def test_opponent_with_no_lineup_set_is_projected_from_their_roster_not_zero(conn, week):
    setup(conn, opponent_fppg=14.0)  # a stronger opponent who hasn't set a lineup
    pair(conn, ["0"] * 8)
    result = weekly.optimize_lineup(conn, 2025, 2026, 4, 1)
    assert result["opponent_source"] == "their best projected lineup (they haven't set one yet)"
    assert result["opponent_mean"] > 100  # their 8 best real players, not an empty lineup scoring 0
    # The old code scored an unset opponent as 0 and called this a near-certain
    # win; against their real roster it's a likely loss.
    assert result["recommended_win_probability"] < 0.5


def test_a_partly_set_lineup_is_filled_from_their_roster(conn, week):
    setup(conn)
    pair(conn, ["2qb", "2rb0", "0", "0", "0", "0", "0", "0"])
    result = weekly.optimize_lineup(conn, 2025, 2026, 4, 1)
    assert result["opponent_source"] == "their best projected lineup (theirs has 6 empty slot(s))"


def test_a_fully_set_lineup_is_used_as_set(conn, week):
    setup(conn)
    pair(conn, ["2qb", "2rb0", "2rb1", "2wr0", "2wr1", "2wr2", "2te", "2rb2"])
    result = weekly.optimize_lineup(conn, 2025, 2026, 4, 1)
    assert result["opponent_source"] == "their set lineup"


def test_slots_the_roster_cant_fill_start_empty_instead_of_no_lineup(conn, week):
    add_league(conn)
    conn.execute("UPDATE league SET roster_positions_json = ?", (json.dumps(["QB", "TE", "TE", "K", "SUPER_FLEX", "BN"]),))
    add_roster(conn, 1)
    add_player(conn, "qb1", "QB", 20.0, roster_id=1)
    add_player(conn, "qb2", "QB", 15.0, roster_id=1)
    add_player(conn, "te", "TE", 8.0, roster_id=1)
    result = weekly.optimize_lineup(conn, 2025, 2026, 4, 1)
    assert {p["player_id"] for p in result["recommended_lineup"]} == {"qb1", "qb2", "te"}  # qb2 at SUPER_FLEX
    assert sorted(result["empty_slots"]) == ["K", "TE"]
    assert result["unsupported_slots"] == ["K"]


def test_bye_comes_from_the_schedule_not_from_missing_lines(conn, week):
    add_league(conn)
    add_roster(conn, 1)
    add_player(conn, "kc", "WR", 12.0, roster_id=1, team="KC")
    add_player(conn, "mia", "WR", 12.0, roster_id=1, team="MIA")
    week["implied"] = {"BUF": 24.0}  # KC's line is off the board; KC still plays
    result = weekly.optimize_lineup(conn, 2025, 2026, 4, 1)
    by_id = {p["player_id"]: p for p in result["recommended_lineup"] + result["bench"]}
    assert not by_id["kc"]["on_bye"] and by_id["kc"]["mean"] > 0
    assert by_id["mia"]["on_bye"] and by_id["mia"]["mean"] == 0


def test_with_no_schedule_a_missing_line_still_means_bye(conn, week):
    add_league(conn)
    add_roster(conn, 1)
    add_player(conn, "mia", "WR", 12.0, roster_id=1, team="MIA")
    week["playing"], week["implied"] = set(), {"BUF": 24.0}
    result = weekly.optimize_lineup(conn, 2025, 2026, 4, 1)
    assert result["recommended_lineup"][0]["on_bye"]


def test_a_player_with_no_variance_gets_the_positions_median_not_zero(conn, week):
    add_league(conn)
    add_roster(conn, 1)
    for i, points in enumerate([[5, 15, 10, 20, 8, 12], [9, 11, 10, 10, 12, 8]]):
        pid = f"vet{i}"
        conn.execute("INSERT INTO players (player_id, full_name, position, team, fetched_at) VALUES (?, ?, 'WR', 'KC', 't')", (pid, pid))
        for wk, pts in enumerate(points, 1):
            conn.execute(
                "INSERT INTO weekly_stats (player_id, season, week, position, fantasy_points, fetched_at) VALUES (?, '2024', ?, 'WR', ?, 't')",
                (pid, wk, pts),
            )
    add_player(conn, "new", "WR", 10.0, roster_id=1, games=1)  # one game: no sample variance
    fallback = weekly.position_variance_fallback(conn, 2025)
    variances = [statistics.variance([5, 15, 10, 20, 8, 12]), statistics.variance([9, 11, 10, 10, 12, 8])]
    assert fallback["WR"] == pytest.approx(statistics.median(variances))  # the two veterans' median
    p = weekly.optimize_lineup(conn, 2025, 2026, 4, 1)["recommended_lineup"][0]
    assert p["variance_estimated"] and p["variance"] == pytest.approx(fallback["WR"])


@pytest.mark.parametrize("bad_week", [0, 19, -1])
def test_week_outside_the_season_is_an_error(conn, week, bad_week):
    add_league(conn)
    with pytest.raises(ValueError, match="weeks run 1 through 18"):
        weekly.optimize_lineup(conn, 2025, 2026, bad_week, 1)


def add_weeks(conn, pid, position, points, roster_id=None):
    conn.execute("INSERT INTO players (player_id, full_name, position, team, fetched_at) VALUES (?, ?, ?, 'KC', 't')",
                 (pid, pid, position))
    for wk, pts in enumerate(points, 1):
        conn.execute(
            "INSERT INTO weekly_stats (player_id, season, week, position, fantasy_points, fetched_at) VALUES (?, '2025', ?, ?, ?, 't')",
            (pid, wk, position, pts),
        )
    if roster_id:
        conn.execute("INSERT INTO roster_players (roster_id, player_id, slot, fetched_at) VALUES (?, ?, 'bench', 't')", (roster_id, pid))


def test_an_underdog_starts_the_boom_or_bust_flex_and_the_swap_says_so(conn, week):
    add_league(conn)
    conn.execute("UPDATE league SET roster_positions_json = ?", (json.dumps(["WR", "FLEX", "BN", "BN"]),))
    add_roster(conn, 1)
    add_roster(conn, 2)
    add_weeks(conn, "wr", "WR", [10] * 6, roster_id=1)
    add_weeks(conn, "steady", "RB", [10] * 6, roster_id=1)  # mean 10, no variance
    add_weeks(conn, "boom", "RB", [0, 19, 0, 19, 0, 19], roster_id=1)  # mean 9.5, huge variance
    add_weeks(conn, "opp1", "WR", [15] * 6, roster_id=2)
    add_weeks(conn, "opp2", "RB", [15] * 6, roster_id=2)
    pair(conn, ["opp1", "opp2"])
    result = weekly.optimize_lineup(conn, 2025, 2026, 4, 1)
    # Down 10 points with a steady lineup is a sure loss; only variance can win it.
    assert {p["player_id"] for p in result["recommended_lineup"]} == {"wr", "boom"}
    assert {p["player_id"] for p in result["points_max_lineup"]} == {"wr", "steady"}
    assert result["swaps_from_points_max"] == [{"starts": "boom", "slot": "FLEX", "over": "steady"}]


def test_sleeper_down_keeps_the_stored_matchups_and_says_how_old(conn, monkeypatch):
    add_league(conn)
    pair(conn, [])

    class Down:
        def __init__(self, conn):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def sync_matchups(self, week):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr("dynasty_agent.sleeper.SleeperClient", Down)
    note = weekly.sync_matchups_or_note(conn, 4)
    assert "couldn't be reached (ConnectError)" in note and "last synced matchups" in note
    assert conn.execute("SELECT count(*) FROM matchups WHERE week = 4").fetchone()[0] == 2  # kept
