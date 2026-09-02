"""Project paths and league constants.

This is a single-league tool by design. The league identifiers below are
hardcoded, not pulled from env vars or a config file, because there is
exactly one league this runs against. If that ever changes, promote these
to real configuration then, not before.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "dynasty.db"
NFLVERSE_CACHE_DIR = DATA_DIR / "nflverse"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

SLEEPER_USERNAME = "bigstott"
SLEEPER_USER_ID = "1399550449018802176"
LEAGUE_ID = "1397608187129016320"
DRAFT_ID = "1397608188068507648"

# FantasyCalc dynasty value parameters for this league: 12 teams, 1QB, full PPR.
FANTASYCALC_PARAMS = {"isDynasty": "true", "numQbs": "1", "numTeams": "12", "ppr": "1"}
