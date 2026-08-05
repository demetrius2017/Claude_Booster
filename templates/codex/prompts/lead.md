---
description: "Run Claude Booster lead/supervisor command in Codex"
argument-hint: '<free-form prompt> | sessions | status --session ID'
---

Use $$booster-command to run command `lead`.

Arguments: $ARGUMENTS

For native Codex subagents, retain returned task paths, work independently
while they run, and use `list_agents` for snapshots. Do not loop on waiting
messages; wait only when no independent work remains, for 30–60 seconds, then
snapshot. Native Codex waiting UI cannot be suppressed by this command.
