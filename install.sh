#!/usr/bin/env bash
# Dynasty Agent installer (macOS/Linux): uv, LM Studio (headless, no GUI app
# needed), the local model, and the dynasty-agent CLI itself, in one script.
#
# Every command below was run for real against a fresh install before this
# script was written, not assumed: see PLANNING.md's Phase 7 section for the
# exact verified sequence and its real output.
set -eu

REPO_URL="git+https://github.com/3schtocky/Sleeper-Dynasty-AI-Agent"
# Confirmed live: this is the real, working LM Studio Hub identifier for
# Qwen3-4B. The newer "-instruct-2507" refresh does NOT resolve as a Hub
# artifact (confirmed live, a clean "does not exist" error), don't switch to
# it without re-confirming first.
MODEL="qwen/qwen3-4b"

echo "== Dynasty Agent installer =="
echo ""

# 1. uv, the Python package/tool manager this project runs on.
if command -v uv >/dev/null 2>&1; then
    echo "uv already installed ($(uv --version))."
else
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# 2. LM Studio's headless CLI (llmster daemon + lms), no GUI app required.
# This supersedes LM Studio's older, GUI-bundled `lms` (which needed the app
# launched at least once before it worked); the headless installer below is
# LM Studio's own official path for exactly this kind of scripted setup.
if command -v lms >/dev/null 2>&1; then
    echo "LM Studio (lms) already installed."
else
    echo "Installing LM Studio (headless, no GUI app)..."
    curl -fsSL https://lmstudio.ai/install.sh | bash
    export PATH="$HOME/.lmstudio/bin:$PATH"
fi

# 3. Start the daemon, pull and load the model, and bring up the local,
# OpenAI-compatible server dynasty-agent's chat/ask commands talk to.
echo ""
echo "Starting LM Studio's local server..."
lms daemon up
lms get "$MODEL" -y
lms load "$MODEL" -y

if lms server status --json 2>/dev/null | grep -q '"running":true'; then
    echo "Server already running."
else
    lms server start --port 1234
fi
lms server status

# 4. Install dynasty-agent itself as a real, global command.
echo ""
echo "Installing dynasty-agent..."
uv tool install "$REPO_URL"

# 5. Point it at your own Sleeper league.
echo ""
read -r -p "Your Sleeper username: " SLEEPER_USERNAME
dynasty-agent init --username "$SLEEPER_USERNAME"

echo ""
echo "Done. Next steps:"
echo "  dynasty-agent sync"
echo "  dynasty-agent chat"
