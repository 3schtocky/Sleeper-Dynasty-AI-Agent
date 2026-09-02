"""Real wind-flag check for a specific NFL week's game, not a guess. Two
free, live, unauthenticated sources, same "no auth needed" pattern as
Sleeper, FantasyCalc, and nflverse:

- nflverse's own schedules file (already used for Vegas lines in
  matchup.py) for the actual game's venue and roof status, read per game,
  not assumed per team. Two things confirmed by checking directly, not
  assumed: retractable-roof stadiums show up "closed" some games and
  "outdoors" others in the same season, so a static per-stadium roof
  default would be wrong on any given week; and international games (one
  most weeks in-season) are played at a real, unrelated venue with no
  connection to either team's home stadium, "home team" LA in a real 2026
  Week 1 game maps to a "stadium" of Melbourne Cricket Ground, not SoFi.
  A team-keyed stadium table cannot handle that; this reads the game's
  own stadium every time instead.
- api.weather.gov (the National Weather Service's public forecast API,
  pulled programmatically here by direct request), for the actual
  forecast wind speed at that venue's coordinates around kickoff.

Two real, honest limits, not smoothed over:
- No nflverse release publishes stadium coordinates (checked: no release
  tag matches "stadium"/"venue"/"weather"). STADIUM_COORDINATES below is
  this project's own static lookup, keyed by nflverse's own stable
  stadium_id (a sponsor-name change, e.g. Highmark Stadium was New Era
  Field, doesn't change the id), covering the 30 current domestic venues
  only. An international game's stadium_id won't be in it, and that comes
  back as "not covered," not a guessed coordinate.
- NWS forecasts only extend about a week out. A game more than about 7
  days away simply has no forecast yet; that comes back as "not
  forecasted yet," not an error and not a silent 0 mph.
"""

from __future__ import annotations

import re

import duckdb
import httpx

GAMES_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.parquet"
WIND_FLAG_MPH = 15.0

# This project's own static reference, not from any nflverse file (none
# exists). Approximate stadium coordinates, public knowledge of these
# venues, sufficient precision for a city-scale weather forecast lookup.
# Keyed by nflverse's stadium_id, shared ids reflect a shared building
# (SoFi Stadium: LAX01 for both the Rams and Chargers; MetLife: NYC01 for
# both the Giants and Jets).
STADIUM_COORDINATES: dict[str, tuple[float, float]] = {
    "DAL00": (32.7473, -97.0945),   # AT&T Stadium, Arlington TX (Cowboys)
    "PIT00": (40.4468, -80.0158),   # Acrisure Stadium, Pittsburgh PA
    "VEG00": (36.0909, -115.1833),  # Allegiant Stadium, Las Vegas NV
    "CAR00": (35.2258, -80.8528),   # Bank of America Stadium, Charlotte NC
    "NOR00": (29.9511, -90.0812),   # Caesars Superdome, New Orleans LA
    "DEN00": (39.7439, -105.0201),  # Empower Field, Denver CO
    "JAX00": (30.3239, -81.6373),   # EverBank Stadium, Jacksonville FL
    "WAS00": (38.9078, -76.8645),   # Northwest Stadium, Landover MD
    "CLE00": (41.5061, -81.6995),   # Huntington Bank Field, Cleveland OH
    "DET00": (42.3400, -83.0456),   # Ford Field, Detroit MI
    "KAN00": (39.0489, -94.4839),   # Arrowhead Stadium, Kansas City MO
    "BOS00": (42.0909, -71.2643),   # Gillette Stadium, Foxborough MA (Patriots)
    "MIA00": (25.9580, -80.2389),   # Hard Rock Stadium, Miami Gardens FL
    "BUF00": (42.7738, -78.7870),   # Highmark Stadium, Orchard Park NY
    "GNB00": (44.5013, -88.0622),   # Lambeau Field, Green Bay WI
    "SFO01": (37.4032, -121.9698),  # Levi's Stadium, Santa Clara CA
    "PHI00": (39.9008, -75.1675),   # Lincoln Financial Field, Philadelphia PA
    "IND00": (39.7601, -86.1639),   # Lucas Oil Stadium, Indianapolis IN
    "SEA00": (47.5952, -122.3316),  # Lumen Field, Seattle WA
    "BAL00": (39.2780, -76.6227),   # M&T Bank Stadium, Baltimore MD
    "ATL97": (33.7554, -84.4008),   # Mercedes-Benz Stadium, Atlanta GA
    "NYC01": (40.8135, -74.0745),   # MetLife Stadium, East Rutherford NJ (Giants + Jets)
    "HOU00": (29.6847, -95.4107),   # NRG Stadium, Houston TX
    "NAS00": (36.1665, -86.7713),   # Nissan Stadium, Nashville TN
    "CIN00": (39.0954, -84.5160),   # Paycor Stadium, Cincinnati OH
    "TAM00": (27.9759, -82.5033),   # Raymond James Stadium, Tampa FL
    "LAX01": (33.9535, -118.3392),  # SoFi Stadium, Inglewood CA (Rams + Chargers)
    "CHI98": (41.8623, -87.6167),   # Soldier Field, Chicago IL
    "PHO00": (33.5276, -112.2626),  # State Farm Stadium, Glendale AZ
    "MIN01": (44.9738, -93.2581),   # U.S. Bank Stadium, Minneapolis MN
}

# nflverse's own roof value for that specific game, when populated, is the
# source of truth ("closed"/"dome" = indoors, wind irrelevant; "outdoors"/
# "open" = check the forecast). A blank roof field (it happens, mostly for
# not-yet-finalized future games at retractable-roof venues) falls back to
# this per-stadium default, itself only a best guess of how that building
# is usually configured, not a promise. True fixed domes are unambiguous
# either way; retractable venues default closed here since that's their
# more common state, however the per-game field overrides this whenever
# it's actually populated.
DEFAULT_INDOORS: dict[str, bool] = {
    "VEG00": True, "NOR00": True, "DET00": True, "MIN01": True,  # fixed domes
    "LAX01": True,  # SoFi's roofed, however it's an open-sided canopy design, not a sealed dome; treated as indoors per nflverse's own "dome" classification for that venue, flagged here as a real simplification, not a claim it's fully wind-proof.
    "DAL00": True, "IND00": True, "HOU00": True, "PHO00": True, "ATL97": True,  # retractable, usually closed
}


def _game_for_team(season: int, week: int, team: str) -> dict | None:
    """The one real game a team plays in a given week, home or away, with
    its actual stadium_id, roof, and kickoff date/time. None if the team
    has no game that week (a bye) or the schedule doesn't cover it."""
    con = duckdb.connect()
    try:
        row = con.execute(
            """
            SELECT home_team, away_team, stadium, stadium_id, roof, gameday, gametime
            FROM read_parquet(?)
            WHERE season = ? AND week = ? AND (home_team = ? OR away_team = ?)
            LIMIT 1
            """,
            [GAMES_URL, season, week, team, team],
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    home, away, stadium, stadium_id, roof, gameday, gametime = row
    return {
        "home_team": home,
        "away_team": away,
        "stadium": stadium,
        "stadium_id": stadium_id,
        "roof": roof or None,
        "gameday": gameday,
        "gametime": gametime,
    }


def parse_wind_mph(wind_speed_text: str | None) -> float | None:
    """NWS returns windSpeed as free text: "10 mph" or a range "10 to 20
    mph". Takes the higher end of a range, a flag should err toward
    catching real risk, not averaging it away. None if nothing parses."""
    if not wind_speed_text:
        return None
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", wind_speed_text)]
    return max(numbers) if numbers else None


def _fetch_forecast_periods(lat: float, lon: float) -> list[dict]:
    with httpx.Client(timeout=15.0, headers={"User-Agent": "dynasty-agent (github.com/3schtocky/Sleeper-Dynasty-AI-Agent)"}) as client:
        points = client.get(f"https://api.weather.gov/points/{lat},{lon}")
        points.raise_for_status()
        forecast_url = points.json()["properties"]["forecast"]
        forecast = client.get(forecast_url)
        forecast.raise_for_status()
        return forecast.json()["properties"]["periods"]


def game_wind_forecast(season: int, week: int, team: str) -> dict:
    """Everything known about wind risk for a team's specific game this
    week, all of it real and sourced, never a placeholder. Shape:
    {"status": "bye"} | {"status": "no_schedule_data"} |
    {"status": "indoors", "stadium": ..., "roof": ...} |
    {"status": "not_covered", "stadium": ..., "reason": ...} |
    {"status": "not_forecasted_yet", "stadium": ...} |
    {"status": "ok", "stadium": ..., "wind_mph": float, "flag": bool}."""
    game = _game_for_team(season, week, team)
    if game is None:
        return {"status": "bye"}

    roof = (game["roof"] or "").lower()
    stadium_id = game["stadium_id"]
    indoors = roof in ("closed", "dome") or (not roof and DEFAULT_INDOORS.get(stadium_id, False))
    if indoors:
        return {"status": "indoors", "stadium": game["stadium"], "roof": game["roof"] or "closed (default)"}

    coords = STADIUM_COORDINATES.get(stadium_id)
    if coords is None:
        return {
            "status": "not_covered",
            "stadium": game["stadium"],
            "reason": "no coordinates for this venue, likely an international game at a site outside this project's static table",
        }

    try:
        periods = _fetch_forecast_periods(*coords)
    except httpx.HTTPError as e:
        return {"status": "fetch_error", "stadium": game["stadium"], "reason": str(e)}

    game_date = game["gameday"]
    game_hour = int(str(game["gametime"]).split(":")[0]) if game["gametime"] else 13
    is_night = game_hour >= 18

    matching = [p for p in periods if p["startTime"].startswith(game_date)]
    if not matching:
        return {"status": "not_forecasted_yet", "stadium": game["stadium"]}
    period = next((p for p in matching if p.get("isDaytime") is (not is_night)), matching[0])

    wind_mph = parse_wind_mph(period.get("windSpeed"))
    if wind_mph is None:
        return {"status": "not_forecasted_yet", "stadium": game["stadium"]}

    return {
        "status": "ok",
        "stadium": game["stadium"],
        "gameday": game_date,
        "wind_mph": wind_mph,
        "wind_direction": period.get("windDirection"),
        "short_forecast": period.get("shortForecast"),
        "flag": wind_mph >= WIND_FLAG_MPH,
    }
