from dynasty_agent.prospects import to_nflverse_team_from_draft_code


def test_to_nflverse_team_from_draft_code_translates_pfr_style_codes():
    # Confirmed live: these 8 of 32 teams differ between nflverse's
    # draft_picks file (PFR-style) and this project's standard team code.
    assert to_nflverse_team_from_draft_code("GNB") == "GB"
    assert to_nflverse_team_from_draft_code("KAN") == "KC"
    assert to_nflverse_team_from_draft_code("LAR") == "LA"
    assert to_nflverse_team_from_draft_code("LVR") == "LV"
    assert to_nflverse_team_from_draft_code("NOR") == "NO"
    assert to_nflverse_team_from_draft_code("NWE") == "NE"
    assert to_nflverse_team_from_draft_code("SFO") == "SF"
    assert to_nflverse_team_from_draft_code("TAM") == "TB"


def test_to_nflverse_team_from_draft_code_passes_through_unmapped_teams():
    assert to_nflverse_team_from_draft_code("SEA") == "SEA"
    assert to_nflverse_team_from_draft_code("DAL") == "DAL"


def test_to_nflverse_team_from_draft_code_handles_none():
    assert to_nflverse_team_from_draft_code(None) is None
