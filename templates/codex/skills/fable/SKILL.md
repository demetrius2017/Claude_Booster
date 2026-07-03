---
name: "fable"
description: "Ask Fable 5 for a one-off read-only advisory opinion. Not consilium; no report; no routing change."
---

# Booster Fable Consult

Read the sibling skill `../booster-command/SKILL.md`, then run command `fable`
through that runner.

Treat the rest of the user message as the Fable question. This is a single
read-only advisory consult, not the multi-agent consilium protocol.

After the Fable call completes, invoke
`python3 ~/.claude/scripts/fable_usage.py refresh-display` and include its two
spend estimate lines if it prints anything. This refreshes the current UTC
month from Claude/Codex transcript stores before printing. The lines are
API-equivalent / credit-rate estimates, not an actual billing ledger.
