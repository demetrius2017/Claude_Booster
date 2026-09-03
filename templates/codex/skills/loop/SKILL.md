---
name: "loop"
description: "Schedule bounded external Codex queue wakes for one explicit existing thread."
---

# Booster Loop

Read the sibling `../booster-command/SKILL.md`, then follow command `loop`.
The supported Codex surfaces are `$loop` and legacy `/prompts:loop`, not a
guaranteed native `/loop`. Route `$loop 30m --thread <uuid>` to:

```sh
python3 ~/.claude/scripts/codex_loop.py start 30m --thread <uuid>
```

Route `$loop status` and `$loop stop` to the corresponding `status` and `stop`
subcommands. Keep the supplied thread explicit; do not infer or substitute one.
The scheduler, not the model, sleeps and invokes `codex queue` after a full
interval. Consult the command spec for lifecycle and safety constraints.
