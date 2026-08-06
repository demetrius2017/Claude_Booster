---
description: "Render a compact fact-bound Russian Booster Gantt snapshot"
argument-hint: '[detail]'
---

Use $$booster-command to run command `gantt`.

Arguments: $ARGUMENTS

Use only current `update_plan`/task state, concrete known conversation facts,
and one `list_agents` snapshot if available. Do not poll, invent progress, or
create a scheduler; label absent facts explicitly as `неизвестно` or `нет данных`.
