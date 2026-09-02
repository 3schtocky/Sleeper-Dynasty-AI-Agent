"""Project paths and league configuration.

The Sleeper identifiers are not hardcoded: they come from environment
variables, loaded from a project-root .env file if one exists, so anyone
can point this at their own dynasty league. Run
`dynasty-agent init --username <your sleeper username>` to generate that
.env automatically, or copy .env.example to .env and fill it in by hand.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "dynasty.db"
NFLVERSE_CACHE_DIR = DATA_DIR / "nflverse"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(ENV_PATH)  # a no-op if the file doesn't exist yet

SLEEPER_USERNAME = os.environ.get("SLEEPER_USERNAME")
SLEEPER_USER_ID = os.environ.get("SLEEPER_USER_ID")
LEAGUE_ID = os.environ.get("LEAGUE_ID")
DRAFT_ID = os.environ.get("DRAFT_ID")

# FantasyCalc dynasty value parameters. Defaults match the league this was
# built against (12 teams, 1QB, full PPR); override in .env if yours
# differs. See README for the full list of overridable variables.
FANTASYCALC_PARAMS = {
    "isDynasty": "true",
    "numQbs": os.environ.get("FANTASYCALC_NUM_QBS", "1"),
    "numTeams": os.environ.get("FANTASYCALC_NUM_TEAMS", "12"),
    "ppr": os.environ.get("FANTASYCALC_PPR", "1"),
}

# Phase 4 (rookie draft prep) only: a free key from collegefootballdata.com,
# used to compute real college production stats for the prospect board.
# None until the user registers one and adds it to .env; not in
# missing_config() below since nothing else in this project needs it.
CFBD_API_KEY = os.environ.get("CFBD_API_KEY")


def missing_config() -> list[str]:
    """Which required .env variables are unset, empty list if none. Used to
    give a clear "run init first" message instead of a confusing API error
    for a None league_id."""
    return [
        name
        for name, value in [
            ("SLEEPER_USERNAME", SLEEPER_USERNAME),
            ("SLEEPER_USER_ID", SLEEPER_USER_ID),
            ("LEAGUE_ID", LEAGUE_ID),
            ("DRAFT_ID", DRAFT_ID),
        ]
        if not value
    ]
