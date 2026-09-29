"""Reuse of expensive results within one process must never serve stale or
foreign data: a write on the same connection, a write from another
connection (the daily refresh) and a different database all recompute."""

import sqlite3

import pytest

from dynasty_agent import valuation
from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations


@pytest.fixture
def counted(monkeypatch):
    calls = []
    monkeypatch.setattr(valuation, "player_valuations_uncached", lambda conn, season: calls.append(season) or {"n": len(calls)})
    valuation._valuations_memo.clear()
    return calls


def open_db(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    apply_migrations(c, MIGRATIONS_DIR)
    return c


def test_reused_until_this_connection_writes(tmp_path, counted):
    conn = open_db(tmp_path / "a.db")
    first = valuation.player_valuations(conn, 2026)
    assert valuation.player_valuations(conn, 2026) is first and counted == [2026]
    conn.execute("INSERT INTO players (player_id, full_name, fetched_at) VALUES ('p', 'P', 't')")
    conn.commit()
    assert valuation.player_valuations(conn, 2026) is not first and counted == [2026, 2026]


def test_a_write_from_another_connection_is_seen(tmp_path, counted):
    conn = open_db(tmp_path / "a.db")
    first = valuation.player_valuations(conn, 2026)
    other = sqlite3.connect(tmp_path / "a.db")  # the launchd refresh, say
    other.execute("INSERT INTO players (player_id, full_name, fetched_at) VALUES ('p', 'P', 't')")
    other.commit()
    assert valuation.player_valuations(conn, 2026) is not first


def test_a_different_database_never_gets_anothers_result(tmp_path, counted):
    a, b = open_db(tmp_path / "a.db"), open_db(tmp_path / "b.db")
    assert valuation.player_valuations(a, 2026) is not valuation.player_valuations(b, 2026)
    assert counted == [2026, 2026]


def test_seasons_are_kept_apart(tmp_path, counted):
    conn = open_db(tmp_path / "a.db")
    assert valuation.player_valuations(conn, 2026) is not valuation.player_valuations(conn, 2025)


def test_draft_class_scoring_recomputes_for_new_scoring_or_a_new_file(tmp_path, monkeypatch):
    from dynasty_agent import prospect_model

    files = {s: tmp_path / f"{s}.parquet" for s in (2018, 2019)}
    for f in files.values():
        f.write_text("x")
    calls = []
    monkeypatch.setattr(prospect_model.nflverse, "ensure_cached", lambda kind, season: files[season])
    monkeypatch.setattr(prospect_model, "_first_three_season_ppg", lambda scoring, seasons: calls.append(1) or {})
    prospect_model._ppg_memo.clear()
    ppr, half = {"rec": 1.0}, {"rec": 0.5}
    prospect_model.first_three_season_ppg(ppr, range(2018, 2020))
    prospect_model.first_three_season_ppg(ppr, range(2018, 2020))
    assert len(calls) == 1
    prospect_model.first_three_season_ppg(half, range(2018, 2020))
    assert len(calls) == 2  # new scoring
    import os
    os.utime(files[2019], ns=(1, 1))
    prospect_model.first_three_season_ppg(half, range(2018, 2020))
    assert len(calls) == 3  # a re-downloaded file
