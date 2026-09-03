---
description: "Schedule bounded external Codex queue wakes for an explicit thread"
argument-hint: 'INTERVAL --thread UUID | status | stop'
---

Use $$booster-command to run command `loop`.

Arguments: $ARGUMENTS

Use `$loop` or legacy `/prompts:loop`, not a claimed native `/loop`. Require an
explicit thread for start; route status and stop to `codex_loop.py` without
invoking Codex for status.
