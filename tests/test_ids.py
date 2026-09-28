import sqlite3

from dynasty_agent import ids
from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations


def test_row_key_prefers_gsis_then_espn_then_sleeper():
    assert ids.row_key("00-0041562", "4837248", "13269") == "00-0041562"
    assert ids.row_key(None, "4837248", "13269") == "espn:4837248"
    assert ids.row_key(None, None, "13269") == "sleeper:13269"
    assert ids.row_key(None, None, None) is None


def test_sleeper_id_for_each_id_type():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, MIGRATIONS_DIR)
    conn.execute(
        "INSERT INTO player_ids (row_key, gsis_id, espn_id, sleeper_id, pfr_id, fetched_at) "
        "VALUES ('00-0041562', '00-0041562', '4837248', '13269', 'MendFe00', 't')"
    )
    assert ids.sleeper_id_for(conn, espn_id="4837248") == "13269"
    assert ids.sleeper_id_for(conn, gsis_id="00-0041562") == "13269"
    assert ids.sleeper_id_for(conn, pfr_id="MendFe00") == "13269"
    assert ids.sleeper_id_for(conn, espn_id="nobody") is None
    assert ids.sleeper_id_for(conn) is None
