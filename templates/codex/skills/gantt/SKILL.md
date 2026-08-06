---
name: "gantt"
description: "Render a compact, fact-bound Russian Gantt snapshot from current task state without polling or scheduling."
---

# Booster Gantt

Read the sibling skill `../booster-command/SKILL.md`, then run command `gantt`
through that runner.

This supported Codex surface is `$gantt` (or legacy `/prompts:gantt`), not a
guaranteed native top-level slash command. Render only current `update_plan` /
task state, concrete known conversation facts, and at most one `list_agents`
snapshot when available. Do not poll, infer progress, or create a scheduler.
The command defines the Russian lanes, required fields, legend, unknown-state
handling, slot summary, and Lead lifecycle checkpoints.
