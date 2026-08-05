---
name: "lead"
description: "Run the Claude Booster lead/supervisor protocol in Codex, adapting Claude supervisor instructions to current Codex capabilities."
---

# Booster Lead

Read the sibling skill `../booster-command/SKILL.md`, then run command `lead`
through that runner.

## Codex child-agent lifecycle

When this protocol uses native Codex subagents, preserve every task path
returned by `spawn_agent`. Those paths are the durable handles for
`list_agents`, `send_message`, and `followup_task`; do not treat a child as
lost merely because its initial response is delayed.

While children run, do independent recon, synthesis, verification planning, or
other non-overlapping work. Obtain status with `list_agents` snapshots, not a
wait loop. Use `send_message` for a known-path non-blocking message and
`followup_task` to give a known idle child more work.

Call `wait_agent` only after no independent work remains, for a bounded
30–60 seconds, then take a fresh `list_agents` snapshot. Never repeatedly
wait after `No agents completed yet`, and never claim that native Codex
`Waiting for agents` output can be suppressed or modified.

On every state change, report one concise line such as
`[agents: active 2 · done 1]`; use `$agent-status` for a complete snapshot.
