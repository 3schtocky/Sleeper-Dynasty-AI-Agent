"""A daily `dynasty-agent refresh`, scheduled with macOS's own launchd, so
the data is current whenever the tool (or, later, the Phase 5 chat) is
opened, without remembering to run anything.

macOS only, and the one place this project makes subprocess calls
(`launchctl`), deliberately gated to this module: CLAUDE.md's Windows
audit records "no subprocess calls" as part of why the tool runs
identically on Windows. On Windows, WINDOWS.md shows the Task Scheduler
equivalent instead.

launchd runs a job missed while the Mac slept as soon as it wakes, and it
starts jobs with an almost empty PATH, so the plist carries absolute paths
for uv and the project, found at install time.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from dynasty_agent.config import DATA_DIR, PROJECT_ROOT

LABEL = "com.dynastyagent.refresh"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_PATH = DATA_DIR / "logs" / "refresh.log"


def build_plist(uv_path: str, project_root: Path, hour: int, minute: int, log_path: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [uv_path, "run", "--project", str(project_root), "dynasty-agent", "refresh"],
        "WorkingDirectory": str(project_root),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": {"PATH": f"{Path(uv_path).parent}:/usr/bin:/bin:/usr/sbin:/sbin"},
        "RunAtLoad": False,
    }


def parse_time(text: str) -> tuple[int, int]:
    """'06:00' -> (6, 0). Raises ValueError on anything else."""
    parts = text.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"time must look like 06:00, got '{text}'")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time must be a real 24-hour time, got '{text}'")
    return hour, minute


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError(
            "Scheduling is built on macOS's launchd. On Windows, see WINDOWS.md for the Task Scheduler "
            "equivalent (one command, schtasks)."
        )


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(hour: int, minute: int) -> str:
    _require_macos()
    uv_path = shutil.which("uv")
    if uv_path is None:
        raise RuntimeError("Couldn't find `uv` on PATH, the scheduled job needs its full path.")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", domain, str(PLIST_PATH))  # replace any earlier install; fails harmlessly if none
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(build_plist(uv_path, PROJECT_ROOT, hour, minute, LOG_PATH), f)
    result = _launchctl("bootstrap", domain, str(PLIST_PATH))
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}")
    return f"Scheduled `dynasty-agent refresh` daily at {hour:02d}:{minute:02d}. Log: {LOG_PATH}"


def remove() -> str:
    _require_macos()
    if not PLIST_PATH.exists():
        return "No daily refresh is scheduled."
    _launchctl("bootout", f"gui/{os.getuid()}", str(PLIST_PATH))
    PLIST_PATH.unlink()
    return "Daily refresh removed."


def status() -> str:
    _require_macos()
    if not PLIST_PATH.exists():
        return "No daily refresh is scheduled. `dynasty-agent schedule --install` sets one up (default 06:00)."
    with open(PLIST_PATH, "rb") as f:
        plist = plistlib.load(f)
    when = plist["StartCalendarInterval"]
    loaded = _launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0
    lines = [
        f"Daily refresh at {when['Hour']:02d}:{when['Minute']:02d}, "
        f"{'loaded in launchd' if loaded else 'NOT loaded, rerun --install'}.",
        f"Log: {LOG_PATH}",
    ]
    if LOG_PATH.exists():
        tail = LOG_PATH.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-4:]
        lines += ["Last run:"] + [f"  {t}" for t in tail]
    else:
        lines.append("It hasn't run yet.")
    return "\n".join(lines)
