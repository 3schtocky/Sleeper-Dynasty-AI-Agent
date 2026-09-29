# Starting prompt: next session

Open Terminal, `cd ~/Sleeper-Dynasty-AI-Agent`, run `claude`, and paste everything in the box below.

---

```
I'm continuing my Sleeper Dynasty Agent project in ~/Sleeper-Dynasty-AI-Agent
(GitHub: 3schtocky/Sleeper-Dynasty-AI-Agent, public backup 3schtocky/Sleeper-Dynasty-Agent).

Before anything else, read these, in this order:
1. docs/phase5-chat-preview-plan.md  (the handoff from my last session, "Sleeper Agent 002")
2. CLAUDE.md                          (reference doc, working rules, writing style)
3. The "Phase 5: talk to the agent" section of PLANNING.md

Then, before writing any code:
- Run `dynasty-agent refresh` (the session-start rule) and `uv run pytest` (118 should pass),
  and confirm Ollama is running with qwen3:4b-instruct available (`ollama list`).
- Ask me what my clean first-time install test from GitHub turned up.
- Ask me whether to review the unmerged `worktree-phase4-college-data` branch first
  (it has a calibrated matchup model, a Monte Carlo sim, and an LM Studio installer).

Then build the Phase 5 chat preview on a new branch, `phase5-chat-preview`:
- Follow the plan's 7 steps in order, and verify each one before starting the next.
- Keep the design rules in section 3 of the plan: the model routes and explains, Python
  computes every number, the model is never asked for what Python already knows.
- Ask me before adding `prompt_toolkit` (the pinned bottom-right tokens/second bar).
- Don't merge to main: push the branch, open a PR, and let me try the chat first.

Be methodical, and stop to show me each step's result.
```

---

**If the new chat seems to be missing context**, point it at the handoff file again, or run
`claude --resume` and choose "Sleeper Agent 002" to reopen the original session.
