---
name: "code-review"
description: "Run the Claude Booster code-review protocol in Codex instead of the built-in Codex review: focused reuse/simplicity/efficiency review with optional model selector such as `code-review fable`."
---

# Booster Code Review

Read the sibling skill `../booster-command/SKILL.md`, then run command
`code-review` through that runner.

Treat the rest of the user message as the optional review model, topic, scope,
and flags. Example: `code-review fable --scope templates/commands`.

When the selected review model is `fable`, invoke
`python3 ~/.claude/scripts/fable_usage.py refresh-display` after the Fable
review and include its output if non-empty. This refreshes the current UTC
month from Claude/Codex transcript stores before printing. The spend lines are
API-equivalent / credit-rate estimates, not an actual billing ledger.
