"""The chat's tools on a small fake league: schemas that never ask for what
Python knows, lenient arguments, and questions instead of guesses."""

import json

import pytest

from dynasty_agent import config, tools
from tests.test_integration import add_player, trade_league


def test_no_tool_asks_the_model_for_week_season_or_roster():
    for tool in tools.TOOLS:
        props = set(tool["function"]["parameters"]["properties"])
        assert not props & {"week", "season", "roster", "roster_id", "team", "league"}, tool["function"]["name"]
    assert tools.TOOL_NAMES == ["set_lineup", "evaluate_trade", "my_team", "waiver_targets", "pick_advice", "taxi_plan"]


@pytest.fixture
def league(conn, monkeypatch):
    trade_league(conn, monkeypatch)  # rosters 1 and 2, players mine/theirs/fa, pick prices
    monkeypatch.setattr(config, "SLEEPER_USER_ID", "u1")
    conn.execute("UPDATE rosters SET owner_id = 'u1' WHERE roster_id = 1")
    conn.execute("UPDATE players SET full_name = 'Jonah Coleman' WHERE player_id = 'mine'")
    conn.execute("UPDATE players SET full_name = 'Puka Nacua' WHERE player_id = 'theirs'")
    return conn


@pytest.mark.parametrize("pick", ["2027 1st", "2027-1", "2027 round 1", "'27 first", "2027-1st"])
def test_trade_reads_every_way_the_model_writes_a_pick(league, pick):
    result = tools.run_tool(league, "evaluate_trade", {"send_players": ["Jonah Coleman"], "receive_picks": [pick]})
    assert result.clarification is None
    assert result.compact["user_receives"][0].startswith("2027 round 1")


def test_trade_accepts_a_string_where_a_list_was_asked_for_and_splits_names(league):
    add_player(league, "third", "WR", 8.0, roster_id=2)
    league.execute("UPDATE players SET full_name = 'Jaxon Smith-Njigba' WHERE player_id = 'third'")
    r = tools.run_tool(league, "evaluate_trade", {"send_players": "Jonah Coleman", "receive_players": "Puka Nacua and Jaxon Smith-Njigba"})
    assert [x.split(" (")[0] for x in r.compact["user_receives"]] == ["Puka Nacua", "Jaxon Smith-Njigba"]


def test_a_pick_listed_among_players_is_treated_as_a_pick(league):
    r = tools.run_tool(league, "evaluate_trade", {"send_players": ["Jonah Coleman", "2028 1st"], "receive_players": ["Puka Nacua"]})
    assert r.clarification is None
    assert r.compact["user_sends"][1].startswith("2028 round 1")


def test_an_ambiguous_name_is_a_question(league):
    add_player(league, "kw1", "RB", 9.0)
    add_player(league, "kw2", "WR", 3.0, team=None)
    league.execute("UPDATE players SET full_name = 'Kenneth Walker' WHERE player_id IN ('kw1', 'kw2')")
    r = tools.run_tool(league, "evaluate_trade", {"receive_players": ["Kenneth Walker III"], "send_players": ["Jonah Coleman"]})
    assert r.numbers == "" and r.clarification.startswith("Which Kenneth Walker III do you mean: Kenneth Walker (RB KC)")
    assert r.clarification.endswith("or Kenneth Walker (WR FA)?")


def test_an_unknown_name_or_impossible_pick_is_a_question(league):
    assert "Check the spelling" in tools.run_tool(league, "evaluate_trade", {"send_players": ["Zzyzx Qwerty"]}).clarification
    assert "no round 4" in tools.run_tool(league, "evaluate_trade", {"send_picks": ["2027 4th"]}).clarification
    assert "What would you send" in tools.run_tool(league, "evaluate_trade", {}).clarification


def test_trade_numbers_block_is_the_cli_text_and_compact_rounds_like_it(league):
    r = tools.run_tool(league, "evaluate_trade", {"send_players": ["Jonah Coleman"], "receive_players": ["Puka Nacua"]})
    assert r.numbers.startswith("Trade evaluation, pick discount rate 20% per year.")
    assert r.compact["net_market_value"] in r.numbers  # "-500" appears exactly as shown
    assert "not dollars" in r.compact["units"]


def test_waiver_targets_and_one_players_bid(league):
    listed = tools.run_tool(league, "waiver_targets", {})
    assert listed.numbers.startswith("FAAB targets")
    one = tools.run_tool(league, "waiver_targets", {"player": "Player fa"})
    assert one.numbers.startswith("FAAB recommendation for Player fa")
    assert one.compact["suggested_bid"].startswith("$")


def test_unknown_tool_is_a_message_not_a_crash(league):
    assert "don't have a tool" in tools.run_tool(league, "delete_team", {}).clarification


def test_compact_results_are_json_ready(league):
    r = tools.run_tool(league, "my_team", {})
    json.dumps(r.compact)
    assert r.compact["verdict"] in ("contend", "rebuild", "unclear")


def test_a_hint_in_parentheses_settles_which_player(league):
    add_player(league, "kw1", "RB", 9.0)
    add_player(league, "kw2", "WR", 3.0, team=None)
    league.execute("UPDATE players SET full_name = 'Kenneth Walker' WHERE player_id IN ('kw1', 'kw2')")
    for name in ("Kenneth Walker (RB)", "Kenneth Walker (RB KC)", "Kenneth Walker III (KC)"):
        r = tools.run_tool(league, "evaluate_trade", {"receive_players": [name], "send_players": ["Jonah Coleman"]})
        assert r.clarification is None and r.compact["user_receives"][0].startswith("Kenneth Walker"), name
    r = tools.run_tool(league, "evaluate_trade", {"receive_players": ["Kenneth Walker (QB)"], "send_players": ["Jonah Coleman"]})
    assert r.clarification.startswith("Which Kenneth Walker")  # a hint that fits neither still asks


def test_a_trade_read_backwards_is_turned_around_by_the_rosters(league):
    # "Would you trade my 2027 1st for Puka Nacua?" read as sending Puka.
    r = tools.run_tool(league, "evaluate_trade", {"send_players": ["Puka Nacua"], "receive_picks": ["2027 1st"]})
    assert r.compact["user_sends"][0].startswith("2027 1.")  # the user's own pick, so its projected slot is known
    assert r.compact["user_receives"][0].startswith("Puka Nacua")
    assert r.compact["warnings"][0] == "Puka Nacua is on another team's roster, so it's evaluated as a player you receive."
    assert "WARNING: Puka Nacua is on another team's roster" in r.numbers


def test_both_assets_put_on_one_side_are_split_by_the_rosters(league):
    # "A guy offered me his 2028 1st for Jonah Coleman" read as receiving both.
    r = tools.run_tool(league, "evaluate_trade", {"receive_players": ["Jonah Coleman"], "receive_picks": ["2028 1st"]})
    assert [x.split(" (")[0] for x in r.compact["user_sends"]] == ["Jonah Coleman"]
    assert [x.split(" (")[0] for x in r.compact["user_receives"]] == ["2028 round 1"]


def test_a_free_agent_is_never_moved_only_warned_about(league):
    r = tools.run_tool(league, "evaluate_trade", {"send_players": ["Jonah Coleman"], "receive_players": ["Player fa"]})
    assert r.compact["user_receives"][0].startswith("Player fa")
    assert not any("so it's evaluated" in w for w in r.compact["warnings"])


def test_none_in_an_optional_argument_means_blank(league):
    assert tools.run_tool(league, "waiver_targets", {"player": "none", "position": "null"}).numbers.startswith("FAAB targets")
