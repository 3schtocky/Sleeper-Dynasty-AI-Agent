"""college.ingest_season against tiny parquet files shaped like the real
sportsdataverse releases: one row per player per game per stat category,
stats stored as text."""

import sqlite3

import duckdb
import pytest

from dynasty_agent import college
from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations

BOX_COLUMNS = (
    "category, athlete_id, athlete_name, team_id, game_id, receptions, receivingYards, receivingTouchdowns, "
    "rushingAttempts, rushingYards, rushingTouchdowns, \"completions/passingAttempts\", passingYards, passingTouchdowns"
)


def box_row(category, athlete_id, name, team_id, game_id, **stats):
    keys = ["receptions", "receivingYards", "receivingTouchdowns", "rushingAttempts", "rushingYards",
            "rushingTouchdowns", "completions/passingAttempts", "passingYards", "passingTouchdowns"]
    return (category, athlete_id, name, team_id, game_id) + tuple(stats.get(k.replace("/", "_")) for k in keys)


@pytest.fixture
def files(tmp_path, monkeypatch):
    box = [
        # WR on team 10: two games of receiving, plus a rushing line and a
        # kick return line that must not leak into his receiving totals.
        box_row("receiving", 1, "Wide Out", 10, 100, receptions="5", receivingYards="80", receivingTouchdowns="1"),
        box_row("receiving", 1, "Wide Out", 10, 101, receptions="7", receivingYards="120", receivingTouchdowns="0"),
        box_row("rushing", 1, "Wide Out", 10, 101, rushingAttempts="1", rushingYards="12", rushingTouchdowns="0"),
        box_row("kickReturns", 1, "Wide Out", 10, 101),
        # Teammate TE: the rest of team 10's receiving.
        box_row("receiving", 2, "Tight End", 10, 100, receptions="3", receivingYards="100", receivingTouchdowns="1"),
        # QB with a completions/attempts string to split.
        box_row("passing", 3, "Quarter Back", 10, 100, completions_passingAttempts="20/30", passingYards="250", passingTouchdowns="2"),
        # A defender with only defensive rows is not an offensive player-season.
        box_row("defensive", 4, "Line Backer", 10, 100),
    ]
    box_path = tmp_path / "box.parquet"
    roster_path = tmp_path / "rosters.parquet"
    con = duckdb.connect()
    con.execute(
        f"CREATE TABLE box ({', '.join(c.strip() + ' VARCHAR' for c in BOX_COLUMNS.split(', '))})"
    )
    con.executemany(f"INSERT INTO box VALUES ({', '.join('?' * 14)})", [tuple(None if v is None else str(v) for v in r) for r in box])
    con.execute(f"COPY box TO '{box_path}' (FORMAT parquet)")
    pos = "http://sports.core.api.espn.com/v2/sports/football/leagues/college-football/positions/{}?lang=en"
    con.execute(
        "CREATE TABLE rosters (athlete_id BIGINT, full_name VARCHAR, position_href VARCHAR, date_of_birth VARCHAR, "
        "experience_abbreviation VARCHAR, team_id BIGINT, week INTEGER, height DOUBLE, weight DOUBLE)"
    )
    con.executemany(
        "INSERT INTO rosters VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "Wide Out", pos.format(1), "2005-03-01T07:00Z", "SO", 10, 1, 73.0, 200.0),
            (2, "Tight End", pos.format(7), None, "SR", 10, 1, 77.0, 250.0),
            (3, "Quarter Back", pos.format(8), None, "JR", 10, 1, 75.0, 220.0),
            (4, "Line Backer", pos.format(45), None, "JR", 10, 1, 74.0, 235.0),
        ],
    )
    con.execute(f"COPY rosters TO '{roster_path}' (FORMAT parquet)")
    con.close()
    paths = {"player_box": box_path, "game_rosters": roster_path}
    monkeypatch.setattr(college, "ensure_cached", lambda kind, season, force=False: paths[kind])
    return paths


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c, MIGRATIONS_DIR)
    return c


def test_ingest_sums_each_stat_from_its_own_category(conn, files):
    college.ingest_season(conn, 2025)
    rows = {r["athlete_name"]: dict(r) for r in conn.execute("SELECT * FROM cfb_player_season")}
    assert set(rows) == {"Wide Out", "Tight End", "Quarter Back"}  # defense-only player excluded

    wr = rows["Wide Out"]
    assert (wr["position"], wr["games"], wr["receptions"], wr["rec_yds"], wr["rec_td"]) == ("WR", 2, 12, 200.0, 1)
    assert (wr["rush_att"], wr["rush_yds"]) == (1, 12.0)
    # Team totals: every receiving row on team 10, the dominator denominator.
    assert (wr["team_rec_yds"], wr["team_rec_td"], wr["team_games"]) == (300.0, 2, 2)

    qb = rows["Quarter Back"]
    assert (qb["position"], qb["pass_cmp"], qb["pass_att"], qb["pass_yds"], qb["pass_td"]) == ("QB", 20, 30, 250.0, 2)
    assert rows["Tight End"]["position"] == "TE"


def test_reingest_replaces_the_season_and_keeps_a_known_birth_date(conn, files):
    college.ingest_season(conn, 2025)
    college.ingest_season(conn, 2025)
    assert conn.execute("SELECT count(*) FROM cfb_player_season").fetchone()[0] == 3
    dob = conn.execute("SELECT date_of_birth, dob_source FROM cfb_athletes WHERE athlete_id = '1'").fetchone()
    assert tuple(dob) == ("2005-03-01", "espn_roster")

    # A later roster that doesn't list the birth date must not erase it.
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * REPLACE (NULL::VARCHAR AS date_of_birth) FROM read_parquet('{files['game_rosters']}')) TO '{files['game_rosters']}.nodob' (FORMAT parquet)")
    con.close()
    files["game_rosters"] = f"{files['game_rosters']}.nodob"
    college.ingest_season(conn, 2025)
    dob = conn.execute("SELECT date_of_birth, dob_source FROM cfb_athletes WHERE athlete_id = '1'").fetchone()
    assert tuple(dob) == ("2005-03-01", "espn_roster")
