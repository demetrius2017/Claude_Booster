---
description: Show a current Codex subagent snapshot without polling native waiting UI
argument-hint: '[detail]'
---

# Agent Status — Codex subagent observability

Use this command through `$agent-status` (or legacy `/prompts:agent-status`).
It is deliberately **not** presented as a native top-level Codex slash command.

## Snapshot protocol

1. Call `list_agents` once to obtain the current direct runtime snapshot. Its
   rows use `agent_name` and `agent_status`; do not infer a different schema.
2. Exclude the root caller row (`agent_name: "/root"`) from child counts and
   task output. For every remaining row:
   - `agent_status: "running"` → count as `active`, render
     `- <agent_name> — running`.
   - `agent_status: {"completed": "<summary>"}` → count as `done`, render
     `- <agent_name> — completed` and, when useful, append a summary truncated
     to 160 characters.
   - Any other string, object, or missing value → count as `other`, render the
     exact status without guessing whether it is active or completed.
3. Keep `agent_name` exactly as returned: it is the task path/handle used by
   `send_message` and `followup_task`.
4. Render the concise state-change line:

   ```text
   [agents: active N · done M · other K]
   ```

   Omit `other K` when it is zero. Then list the normalized child rows above.
5. `list_agents` has no reliable activity timestamps. Therefore never label an
   agent as stalled from this snapshot alone. Only say `possible stall
   (inference; no activity timestamp exposed)` when the lead has concrete
   prior evidence of no progress across separate snapshots; otherwise report
   the returned state as fact.

## Non-polling invariant

- Do not call `wait_agent` as part of this status command.
- Do not repeatedly wait after `No agents completed yet`.
- Do not claim that Booster can suppress, remove, or modify Codex's native
  `Waiting for agents` messages. This command provides an independent status
  snapshot; native UI behavior remains owned by Codex.

For a known task path, the lead retains contact with that child by using
`send_message` for a non-blocking message or `followup_task` to send work to
an idle child. Neither action should replace the snapshot with an assumption.
