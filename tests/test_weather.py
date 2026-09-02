from dynasty_agent.weather import WIND_FLAG_MPH, parse_wind_mph


def test_parse_wind_mph_single_value():
    assert parse_wind_mph("6 mph") == 6.0


def test_parse_wind_mph_range_takes_the_higher_end():
    # A flag should err toward catching real risk, not averaging it away.
    assert parse_wind_mph("6 to 12 mph") == 12.0


def test_parse_wind_mph_none_when_unparseable():
    assert parse_wind_mph(None) is None
    assert parse_wind_mph("") is None
    assert parse_wind_mph("Calm") is None


def test_wind_flag_threshold_is_fifteen_mph():
    assert WIND_FLAG_MPH == 15.0
