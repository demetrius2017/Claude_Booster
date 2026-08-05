---
description: "Show a non-polling Codex subagent status snapshot"
argument-hint: '[detail]'
---

Use $$booster-command to run command `agent-status`.

Arguments: $ARGUMENTS

Use the returned `agent_name`/`agent_status` shape: exclude `/root`, count
`"running"` as active and `{"completed": "..."}` as done, and preserve
unknown states as `other`. Do not poll or promise control of native waiting UI.
