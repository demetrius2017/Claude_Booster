---
description: Show or set the current workflow phase (RECON/PLAN/IMPLEMENT/AUDIT/VERIFY/MERGE)
argument-hint: [get | set <PHASE> | list]
---

# Phase — Lead-Orchestrator workflow state

Run the phase CLI with the user's arguments. No argument = show current phase.

```bash
if [ -z "$ARGUMENTS" ]; then
  python3 ~/.claude/scripts/phase.py get
else
  python3 ~/.claude/scripts/phase.py $ARGUMENTS
fi
```

## Phases

| Phase | Rule |
|---|---|
| `RECON` | read-only exploration (Read/Grep/Glob/WebSearch); no Edit/Write |
| `PLAN` | design + TaskCreate + consilium if uncertainty >30%; no code edits |
| `IMPLEMENT` | Edit/Write allowed; invoke `test_dispatcher.py plan/run` for advisory managed tests |
| `AUDIT` | code review + PAL second opinion; no new code |
| `VERIFY` | real curl / pytest / Chrome DevTools — collect evidence |
| `MERGE` | git push after user acceptance; post-merge curl/console check |

## Usage

- `/phase` — show current
- `/phase set PLAN` — advance to PLAN
- `/phase list` — show all phases with rules

## Enforcement

- `phase_gate.py` PreToolUse hook blocks Edit/Write outside IMPLEMENT (unless file is under docs/reports/tests/.claude/*.md)
- `phase_prompt_inject.py` UserPromptSubmit hook injects `[phase: X]` before every user message
- Transitions logged to `<project>/.claude/phase_transitions.log`
- `set` also creates `<project>/.claude/phase_lease.json`: expiring, project/run-bound structured state. The legacy `.phase` marker is compatibility-only and never authorizes development mode.
- `/phase progress "…"` appends structured progress for the existing `/go` contract.
- Final verification still uses the dispatcher’s frozen **release** manifest: every required registry job/platform must pass. A selective development plan is never release evidence.
- Escape: `CLAUDE_BOOSTER_SKIP_PHASE_GATE=1`
