---
name: "agent-status"
description: "Show a current Codex subagent snapshot with active and completed counts, without polling or claiming native UI controls."
---

# Booster Agent Status

Read the sibling skill `../booster-command/SKILL.md`, then run command
`agent-status` through that runner.

This is a supported Codex skill surface: invoke it as `$agent-status` (or the
legacy `/prompts:agent-status`). It is not a guaranteed native top-level slash
command.

Interpret native `list_agents` rows by their actual `agent_name` and
`agent_status` fields. Exclude the root caller (`agent_name: "/root"`) from
child counts; a child status of `"running"` is active and
`{"completed": "..."}` is done. The command spec defines unknown-status and
summary rendering without polling.
