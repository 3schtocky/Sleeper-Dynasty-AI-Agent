"""Player-name lookup, the first thing every chat question with a name in it
hits. The collisions below are real ones from Sleeper's players table."""

import json
import sqlite3

import pytest

from dynasty_agent import valuation
from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations
from dynasty_agent.valuation import AmbiguousPlayer, PlayerNotFound, normalize_name, resolve_player

LINEUP = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX"] + ["BN"] * 10


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c, MIGRATIONS_DIR)
    c.execute(
        "INSERT INTO league (league_id, season, scoring_settings_json, roster_positions_json, settings_json, fetched_at) "
        "VALUES ('L1', '2026', '{}', ?, '{}', 't')",
        (json.dumps(LINEUP),),
    )
    for pid, name, pos, team in [
        ("6794", "Justin Jefferson", "WR", "MIN"),
        ("13524", "Justin Jefferson", "LB", "CLE"),
        ("6853", "Van Jefferson", "WR", "WAS"),
        ("4983", "DJ Moore", "WR", "BUF"),
        ("4961", "DJ Moore", "CB", None),
        ("4984", "Josh Allen", "QB", "BUF"),
        ("2212", "Josh Allen", "G", None),
        ("8151", "Kenneth Walker", "RB", "KC"),
        ("4634", "Kenneth Walker", "WR", None),
        ("7564", "Ja'Marr Chase", "WR", "CIN"),
        ("7547", "Amon-Ra St. Brown", "WR", "DET"),
        ("11628", "Marvin Harrison", "WR", "ARI"),
        ("4068", "Mike Williams", "WR", None),
        ("748", "Mike Williams", "WR", None),
        ("9999", "Percy 100% Test_Name", "WR", "KC"),
    ]:
        c.execute(
            "INSERT INTO players (player_id, full_name, position, team, fetched_at) VALUES (?, ?, ?, ?, 't')",
            (pid, name, pos, team),
        )
    return c


def test_normalize_name_drops_punctuation_and_suffixes():
    assert normalize_name("Ja'Marr Chase") == normalize_name("Jamarr Chase") == "jamarr chase"
    assert normalize_name("D.J. Moore") == "dj moore"
    assert normalize_name("Marvin Harrison Jr.") == "marvin harrison"
    assert normalize_name("Kenneth Walker III") == "kenneth walker"
    assert normalize_name("Amon-Ra St. Brown") == "amon ra st brown"


def test_player_id_resolves_directly(conn):
    assert resolve_player(conn, "13524")["position"] == "LB"


def test_a_name_prefers_the_player_who_can_play_in_this_league(conn):
    # No IDP slots here: the Browns LB can't matter, the Vikings WR can.
    assert resolve_player(conn, "Justin Jefferson")["player_id"] == "6794"
    assert resolve_player(conn, "d.j. moore")["player_id"] == "4983"  # not the retired CB
    assert resolve_player(conn, "Josh Allen")["player_id"] == "4984"  # not the guard


def test_a_teamless_player_at_a_startable_position_still_makes_it_a_question(conn):
    # The teamless WR could be a free agent who signed since the last sync;
    # choosing the KC RB silently would size a FAAB bid on the wrong player.
    with pytest.raises(AmbiguousPlayer) as err:
        resolve_player(conn, "Kenneth Walker III")
    assert [c["player_id"] for c in err.value.candidates] == ["8151", "4634"]  # the one with a team first


def test_an_idp_league_asks_which_justin_jefferson(conn):
    conn.execute("UPDATE league SET roster_positions_json = ?", (json.dumps(LINEUP + ["LB"]),))
    with pytest.raises(AmbiguousPlayer) as err:
        resolve_player(conn, "Justin Jefferson")
    assert {c["player_id"] for c in err.value.candidates} == {"6794", "13524"}
    assert "Justin Jefferson (WR MIN)" in str(err.value)  # distinct labels need no id


def test_punctuation_and_suffixes_in_the_question_still_match(conn):
    assert resolve_player(conn, "Jamarr Chase")["player_id"] == "7564"
    assert resolve_player(conn, "Marvin Harrison Jr.")["player_id"] == "11628"
    assert resolve_player(conn, "Amon Ra St Brown")["player_id"] == "7547"


def test_a_last_name_alone_asks_which(conn):
    with pytest.raises(AmbiguousPlayer) as err:
        resolve_player(conn, "Jefferson")
    # The LB is left out: only players who can matter to this league are offered.
    assert {c["player_id"] for c in err.value.candidates} == {"6794", "6853"}


def test_a_unique_partial_name_resolves(conn):
    assert resolve_player(conn, "St. Brown")["player_id"] == "7547"


def test_two_equally_relevant_players_are_a_question_not_a_guess(conn):
    # Both teamless WRs, neither matters more than the other.
    with pytest.raises(AmbiguousPlayer) as err:
        resolve_player(conn, "Mike Williams")
    assert {c["player_id"] for c in err.value.candidates} == {"4068", "748"}
    assert "id 4068" in str(err.value) and "id 748" in str(err.value)


def test_a_rostered_player_is_listed_first_not_chosen(conn):
    conn.execute("INSERT INTO roster_players (roster_id, player_id, slot, fetched_at) VALUES (1, '748', 'bench', 't')")
    with pytest.raises(AmbiguousPlayer) as err:
        resolve_player(conn, "Mike Williams")
    assert err.value.candidates[0]["player_id"] == "748"


def test_no_match_and_empty_input_are_clear_errors(conn):
    with pytest.raises(PlayerNotFound, match="Zzyzx"):
        resolve_player(conn, "Zzyzx")
    for blank in ("", "   ", "..."):
        with pytest.raises(PlayerNotFound):
            resolve_player(conn, blank)


def test_sql_wildcards_are_plain_characters(conn):
    # The old LIKE lookup treated % and _ as wildcards, so '%' matched everyone.
    with pytest.raises(PlayerNotFound):
        resolve_player(conn, "%")
    with pytest.raises(PlayerNotFound):
        resolve_player(conn, "_")


def test_both_errors_are_still_value_errors_for_existing_callers(conn):
    assert issubclass(AmbiguousPlayer, ValueError) and issubclass(PlayerNotFound, ValueError)


def test_trade_names_resolve_before_the_valuation_pass(conn, monkeypatch):
    def fail(*args):
        raise AssertionError("player_valuations ran before a bad name was caught")

    monkeypatch.setattr(valuation, "player_valuations", fail)
    with pytest.raises(PlayerNotFound):
        valuation.evaluate_trade(conn, 2026, 1, ["Zzyzx"], [], [], [], 0.2)
