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

Publish `$gantt` after worker launch, reassignment or `followup_task`, and a
completion, failure, or blocker event—not per tool call. Use `send_message`
for direct worker messages without waiting for a reply.
