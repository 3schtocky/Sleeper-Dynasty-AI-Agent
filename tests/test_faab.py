"""Waiver targets and FAAB bids: ranked by how much a player would raise my
best lineup, bids scaled by that gain, paced over the weeks that matter."""


import pytest

from dynasty_agent import weekly
from tests.test_integration import add_league, add_player, add_roster


def league(conn, week=4, settings=None, season_type="regular"):
    add_league(conn, settings or {"playoff_week_start": 15, "playoff_teams": 6, "waiver_budget": 100})
    conn.execute("UPDATE nfl_state SET week = ?, season_type = ?", (week, season_type))
    add_roster(conn, 1)
    add_player(conn, "myqb", "QB", 22.0, roster_id=1)
    for i, fppg in enumerate([14.0, 6.0, 3.0]):
        add_player(conn, f"myrb{i}", "RB", fppg, roster_id=1)
    for i, fppg in enumerate([15.0, 13.0, 12.0, 11.0]):
        add_player(conn, f"mywr{i}", "WR", fppg, roster_id=1)
    add_player(conn, "myte", "TE", 8.0, roster_id=1)


def test_a_backup_qb_ranks_below_a_starting_upgrade_however_good(conn):
    # The week 4 digest listed five free-agent QBs in a 1QB league.
    league(conn)
    add_player(conn, "fa_qb", "QB", 21.0)  # great, but behind my QB
    add_player(conn, "fa_rb", "RB", 10.0)  # would start over my 6-point RB2
    targets = weekly.top_faab_targets(conn, 2025, 1)
    assert [t["player_id"] for t in targets] == ["fa_rb", "fa_qb"]
    assert targets[0]["lineup_gain"] > 0 and targets[1]["lineup_gain"] == 0


def test_bids_scale_with_the_lineup_gain_instead_of_all_topping_out(conn):
    league(conn)
    add_player(conn, "big", "RB", 13.0)
    add_player(conn, "small", "RB", 7.0)
    add_player(conn, "none", "QB", 21.0)
    bids = {t["player_id"]: t for t in weekly.top_faab_targets(conn, 2025, 1)}
    assert bids["big"]["value_multiplier"] == pytest.approx(weekly.FAAB_MAX_VALUE_MULTIPLIER)
    assert bids["none"]["value_multiplier"] == pytest.approx(weekly.FAAB_MIN_VALUE_MULTIPLIER)
    assert bids["big"]["suggested_bid"] > bids["small"]["suggested_bid"] > bids["none"]["suggested_bid"]


def test_position_filter_and_player_ids(conn):
    league(conn)
    add_player(conn, "fa_rb", "RB", 10.0)
    add_player(conn, "fa_te", "TE", 9.0)
    targets = weekly.top_faab_targets(conn, 2025, 1, position="te")
    assert [t["player_id"] for t in targets] == ["fa_te"]


def test_an_empty_waiver_pool_is_an_empty_list(conn):
    league(conn)
    assert weekly.top_faab_targets(conn, 2025, 1) == []


def test_playoff_weeks_follow_the_leagues_bracket():
    assert list(weekly.playoff_weeks({"playoff_week_start": 15, "playoff_teams": 6})) == [15, 16, 17]
    assert list(weekly.playoff_weeks({"playoff_week_start": 15, "playoff_teams": 4, "playoff_round_type": 1})) == [15, 16, 17]
    assert list(weekly.playoff_weeks({"playoff_week_start": 14, "playoff_teams": 8, "playoff_round_type": 2})) == list(range(14, 20))


def test_regular_season_paces_to_the_playoffs(conn):
    league(conn, week=4)
    budget = weekly.faab_budget(conn, 1)
    assert (budget["phase"], budget["weeks_left"], budget["note"]) == ("regular", 11, None)


def test_playoffs_pace_over_the_playoff_weeks_left_not_one_week(conn):
    # The old formula floored weeks_left at 1 once the playoffs began:
    # a whole budget suggested for a single week, with no word why.
    league(conn, week=16)
    budget = weekly.faab_budget(conn, 1)
    assert (budget["phase"], budget["weeks_left"]) == ("playoffs", 2)
    assert "worth nothing after the final" in budget["note"]


@pytest.mark.parametrize("week, season_type", [(18, "regular"), (1, "off"), (2, "post")])
def test_after_the_season_there_is_nothing_to_bid(conn, week, season_type):
    league(conn, week=week, season_type=season_type)
    add_player(conn, "fa_rb", "RB", 10.0)
    target = weekly.top_faab_targets(conn, 2025, 1)[0]
    assert target["phase"] == "over" and target["suggested_bid"] == 0
    assert "season is over" in target["note"]


def test_a_single_player_bid_is_measured_against_the_whole_wire(conn):
    league(conn)
    add_player(conn, "best", "RB", 13.0)
    add_player(conn, "asked", "RB", 7.0)
    one = weekly.faab_recommendation(conn, 2025, 1, "asked")
    listed = {t["player_id"]: t for t in weekly.top_faab_targets(conn, 2025, 1)}
    assert one["suggested_bid"] == listed["asked"]["suggested_bid"]
    assert one["best_available_gain"] == listed["best"]["lineup_gain"]
