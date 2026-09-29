"""Shared fixtures. conn is a real, migrated in-memory database with team
situation scores neutral (they read nflverse parquet files); a test module
that defines its own conn fixture overrides this one."""

import sqlite3

import pytest

from dynasty_agent import valuation
from dynasty_agent.config import MIGRATIONS_DIR
from dynasty_agent.db import apply_migrations


@pytest.fixture
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c, MIGRATIONS_DIR)
    monkeypatch.setattr(valuation, "team_situation_scores", lambda season: {})
    return c
