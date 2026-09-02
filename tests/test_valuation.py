from dynasty_agent.valuation import to_nflverse_team


def test_to_nflverse_team_translates_the_rams():
    # The one confirmed real mismatch (diffed directly against live data,
    # not assumed): Sleeper says "LAR", nflverse says "LA". Missing this
    # once already caused every Rams player to read as on a bye every
    # single week in predict-matchup, since the lookup never matched.
    assert to_nflverse_team("LAR") == "LA"


def test_to_nflverse_team_passes_through_unmapped_teams():
    assert to_nflverse_team("SEA") == "SEA"
    assert to_nflverse_team("KC") == "KC"


def test_to_nflverse_team_handles_none():
    assert to_nflverse_team(None) is None
