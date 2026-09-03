# Dynasty Agent installer (Windows PowerShell): uv, LM Studio (headless, no
# GUI app needed), the local model, and the dynasty-agent CLI itself, in one
# script.
#
# The macOS/Linux command sequence this mirrors was run for real against a
# fresh install before this script was written; PowerShell syntax here is
# translated with the same care WINDOWS.md's own manual walkthrough already
# uses, not literally re-run on a Windows box (this project's own tooling
# has no way to do that), stated plainly, not hidden.

$ErrorActionPreference = "Stop"

$RepoUrl = "git+https://github.com/3schtocky/Sleeper-Dynasty-AI-Agent"
# Confirmed live (macOS): this is the real, working LM Studio Hub identifier
# for Qwen3-4B. The newer "-instruct-2507" refresh does NOT resolve as a Hub
# artifact, don't switch to it without re-confirming first.
$Model = "qwen/qwen3-4b"

Write-Host "== Dynasty Agent installer =="
Write-Host ""

# 1. uv, the Python package/tool manager this project runs on.
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "uv already installed."
} else {
    Write-Host "Installing uv..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
}

# 2. LM Studio's headless CLI (llmster daemon + lms), no GUI app required.
if (Get-Command lms -ErrorAction SilentlyContinue) {
    Write-Host "LM Studio (lms) already installed."
} else {
    Write-Host "Installing LM Studio (headless, no GUI app)..."
    irm https://lmstudio.ai/install.ps1 | iex
}

# 3. Start the daemon, pull and load the model, and bring up the local,
# OpenAI-compatible server dynasty-agent's chat/ask commands talk to.
Write-Host ""
Write-Host "Starting LM Studio's local server..."
lms daemon up
lms get $Model -y
lms load $Model -y

$status = lms server status --json 2>$null | ConvertFrom-Json
if ($status -and $status.running) {
    Write-Host "Server already running."
} else {
    lms server start --port 1234
}
lms server status

# 4. Install dynasty-agent itself as a real, global command.
Write-Host ""
Write-Host "Installing dynasty-agent..."
uv tool install $RepoUrl

# 5. Point it at your own Sleeper league.
Write-Host ""
$SleeperUsername = Read-Host "Your Sleeper username"
dynasty-agent init --username $SleeperUsername

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  dynasty-agent sync"
Write-Host "  dynasty-agent chat"
