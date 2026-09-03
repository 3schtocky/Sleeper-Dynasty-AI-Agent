from dynasty_agent.college import normalize_school_name


def test_normalize_school_name_expands_st_suffix():
    assert normalize_school_name("Ohio St.") == "Ohio State"
    assert normalize_school_name("Arizona St.") == "Arizona State"


def test_normalize_school_name_expands_col_suffix():
    assert normalize_school_name("Boston Col.") == "Boston College"


def test_normalize_school_name_applies_confirmed_one_off_aliases():
    # Confirmed live against the real cfb_team_info school list, not guessed.
    assert normalize_school_name("Hawaii") == "Hawai'i"
    assert normalize_school_name("Mississippi") == "Ole Miss"
    assert normalize_school_name("Connecticut") == "UConn"
    assert normalize_school_name("Miami (FL)") == "Miami"
    assert normalize_school_name("Central Florida") == "UCF"
    assert normalize_school_name("North Carolina St.") == "NC State"
    assert normalize_school_name("Sam Houston St.") == "Sam Houston"


def test_normalize_school_name_passes_through_unresolved_names():
    # A small or non-US school not in CFBD's coverage: passed through
    # unchanged rather than guessed, so it simply won't join to a real row.
    assert normalize_school_name("Wisconsin–Whitewater") == "Wisconsin–Whitewater"


def test_normalize_school_name_handles_none():
    assert normalize_school_name(None) is None
