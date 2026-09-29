from pathlib import Path

import pytest

from dynasty_agent import schedule


def test_plist_uses_absolute_paths_launchd_can_run():
    plist = schedule.build_plist("/Users/me/.local/bin/uv", Path("/Users/me/proj"), 6, 30, Path("/Users/me/proj/data/logs/refresh.log"))
    assert plist["ProgramArguments"] == ["/Users/me/.local/bin/uv", "run", "--project", "/Users/me/proj", "dynasty-agent", "refresh"]
    assert plist["StartCalendarInterval"] == {"Hour": 6, "Minute": 30}
    assert plist["EnvironmentVariables"]["PATH"].startswith("/Users/me/.local/bin:")
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == "/Users/me/proj/data/logs/refresh.log"


def test_parse_time():
    assert schedule.parse_time("06:00") == (6, 0)
    assert schedule.parse_time("23:59") == (23, 59)
    for bad in ("6am", "24:00", "06:60", "6", ""):
        with pytest.raises(ValueError):
            schedule.parse_time(bad)
