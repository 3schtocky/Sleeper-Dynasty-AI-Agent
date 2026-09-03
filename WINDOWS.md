# Windows install guide

Dynasty Agent runs the same on Windows as it does on macOS and Linux, same Python, same `uv`, same commands. Nothing in the code shells out to anything OS-specific (checked directly, not assumed: no `subprocess` calls, no hardcoded `/`-style paths, no POSIX-only system calls anywhere in `src/`). The only real differences are how you install `uv` in the first place and how your terminal wants line breaks written. This guide covers both, in copy-pasteable PowerShell.

If you hit something this guide doesn't cover, the "How the numbers work" and command reference in the main [`README.md`](README.md) apply exactly as written, those aren't OS-specific either.

## Requirements

- Windows 10 or 11.
- **PowerShell**, not the old `cmd.exe`. Every command below is PowerShell syntax. Windows 11 opens PowerShell by default when you right-click in a folder and choose "Open in Terminal"; on Windows 10, search the Start menu for "PowerShell" (Windows Terminal, if you have it installed, is the nicer experience but not required).
- A Sleeper account that's a member of at least one dynasty league. No API key needed, Sleeper's read API is public.

## One-line install

Want the whole thing (uv, LM Studio's local model server, and `dynasty-agent` itself, installed globally) in one shot, including the conversational agent? Run:

```powershell
irm https://raw.githubusercontent.com/3schtocky/Sleeper-Dynasty-AI-Agent/main/install.ps1 | iex
```

That's `install.ps1` in the repo root, read it before running it if you'd rather know exactly what it does first, it's a short, plain script, same spirit as the `uv` installer above. It checks for `uv` and LM Studio's headless CLI (`lms`, no GUI app required, LM Studio's own official headless installer, not this project's), installs whichever it doesn't find, pulls and loads the local model, installs `dynasty-agent` as a real global command via `uv tool install`, and walks you through `dynasty-agent init`. Prefer the manual, step-by-step walkthrough below if you'd rather see and run each piece yourself, both get you to the same place.

## 1. Install Git, if you don't have it

Check first:

```powershell
git --version
```

If that errors, install it:

```powershell
winget install --id Git.Git -e
```

Close and reopen PowerShell afterward so it picks up the new PATH entry.

## 2. Install uv

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

That's `uv`'s own official installer (verified live before writing this: `astral.sh/uv/install.ps1` redirects to a real, signed installer script). It installs `uv` and adds it to your PATH. Close and reopen PowerShell afterward, then confirm it worked:

```powershell
uv --version
```

Prefer a package manager instead of piping a script into `iex`? That's a fair instinct:

```powershell
winget install --id astral-sh.uv -e
```

Either way works identically for everything after this step. You do **not** need to separately install Python; `uv` downloads and manages Python 3.12 for you the first time it's needed.

## 3. Clone and set up the project

```powershell
git clone https://github.com/3schtocky/Sleeper-Dynasty-AI-Agent.git
cd Sleeper-Dynasty-AI-Agent
uv sync
```

`uv sync` creates a `.venv` folder, installs Python 3.12 if it isn't already on your machine, and installs every dependency pinned in `uv.lock`. This is also where Windows most commonly differs from macOS/Linux in a Python project, activating a virtual environment (that `Activate.ps1` script Windows users so often hit an execution-policy wall on). You can skip all of that here: every command below runs through `uv run`, which finds and uses the right environment automatically. No activation step, ever.

## 4. Point it at your own league

```powershell
uv run dynasty-agent init --username <your-sleeper-username>
```

This looks up your Sleeper user ID, finds your current-season dynasty leagues, and writes a `.env` file in the project folder (already in `.gitignore`, it never gets committed) with your username, user ID, league ID, and draft ID. If you're in more than one league this season, it lists them and asks you to re-run with `--league-id <id>` to pick one.

Prefer to do it by hand? Copy `.env.example` to `.env`:

```powershell
Copy-Item .env.example .env
```

then open `.env` in Notepad (or any editor) and fill in the four values. Your league ID and draft ID are both in your league's Sleeper URL and in the response from `https://api.sleeper.app/v1/user/<your-username>`.

## 5. Run it

```powershell
uv run dynasty-agent sync                          # your league, rosters, and market values
uv run dynasty-agent roster                         # sanity check: is this your team?
uv run dynasty-agent ingest-nflverse --season 2025  # the most recently completed NFL season
uv run dynasty-agent valuate                        # win-now/three-year value + the verdict
```

Evaluate a trade. PowerShell line continuation is a backtick (`` ` ``) at the very end of the line, not the backslash (`\`) you'd use in Bash, and it has to be the *last* character on the line, no trailing space after it or PowerShell won't recognize it:

```powershell
uv run dynasty-agent trade `
  --send "Rashee Rice" `
  --receive-pick 2027-1 --receive-pick 2027-3 `
  --discount-rate 0.20
```

Or skip the continuation entirely and put it on one line, which sidesteps the trailing-space gotcha altogether:

```powershell
uv run dynasty-agent trade --send "Rashee Rice" --receive-pick 2027-1 --receive-pick 2027-3 --discount-rate 0.20
```

Predict a matchup, same backtick rule:

```powershell
uv run dynasty-agent predict-matchup --week 1 `
  --team-a "Jalen Hurts" --team-a "Derrick Henry" --team-a "Puka Nacua" `
  --team-b "Jayden Daniels" --team-b "James Cook" --team-b "Drake London"
```

Everything reruns fresh against whatever's cached in the `data\` folder (git-ignored, local SQLite plus downloaded nflverse files). Nothing here needs a server or an account beyond your own Sleeper login. What each command actually does, and how the underlying math works, is documented once in the main [`README.md`](README.md), not repeated here, it applies exactly as written regardless of OS.

## Troubleshooting

**"running scripts is disabled on this system"** when running the `uv` installer, or anything else PowerShell-script-based. The installer command above already includes `-ExecutionPolicy ByPass` for itself, so this shouldn't hit you there, however if you see it elsewhere, PowerShell's default execution policy blocks unsigned scripts. Check your current policy and loosen it for your own account only (safer than a machine-wide change):

```powershell
Get-ExecutionPolicy
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**Windows Defender (or another antivirus) flags or quarantines `uv.exe` or a downloaded Python interpreter.** This is a known false-positive pattern with newly-published binaries and freshly downloaded executables in general, not specific to this project. If it happens, add an exclusion for `uv`'s install directory (typically `%USERPROFILE%\.local\bin` or wherever the installer reports) and re-run the install.

**A path-length error deep inside `.venv`, something mentioning `MAX_PATH` or a path over 260 characters.** Rare on a current Windows 10/11 install (long paths are enabled by default in recent versions) but possible if yours is older or was upgraded from a much older install, or if you cloned into a deeply nested folder. Two ways out: clone somewhere shallower (like `C:\dev\` instead of a folder six levels deep in your Documents), or enable long path support once, as an administrator:

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

then restart.

**`ingest-nflverse` or `predict-matchup` fails to reach `github.com` or hangs on the first DuckDB query.** DuckDB installs a small extension (`httpfs`) the first time it needs to read a remote file, and on a locked-down corporate network or VPN that blocks outbound connections to unfamiliar hosts, that download (or the actual nflverse data fetch) can get blocked. Check whether a proxy or firewall is intercepting `github.com` and `objects.githubusercontent.com`; this isn't a Windows-specific issue, it'd behave the same way on any OS behind the same network policy.

**Everything works but output looks like `Ã¢â‚¬â€` instead of normal punctuation.** That's a console codepage issue, an old `cmd.exe` window not set to UTF-8. Switch to PowerShell (this guide assumes PowerShell throughout, not `cmd.exe`) or, if you must use `cmd.exe`, run `chcp 65001` first to switch its codepage to UTF-8.

## What's actually different from macOS/Linux, concretely

For the curious, not just "trust me": the whole Python application was audited for OS-specific assumptions while writing this guide, grepped for `subprocess`, hardcoded POSIX paths, `os.path.join` (this project uses `pathlib.Path` throughout instead, which produces the correct separator for whatever OS it runs on automatically), `chmod`/symlink calls, and shell scripts. None exist. Two small things *were* found and fixed as part of adding this guide: two places wrote or read a text file without an explicit `encoding="utf-8"`, which works by accident on most systems but isn't guaranteed on Windows, where the default text encoding often isn't UTF-8. Both now pin the encoding explicitly. Beyond that, the only real differences are the ones this guide walks through: how you install `uv`, and how your shell wants multi-line commands written.
