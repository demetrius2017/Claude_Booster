---
name: "autopilot"
description: "Enable, inspect, or disable Fable autopilot and its North Star for delegated engineering decisions."
---

# Booster Autopilot

Read the sibling skill `../booster-command/SKILL.md`, then run command
`autopilot` through that runner. Preserve the same hard boundary in Codex even
though Claude hooks are not active: UI action/visual acceptance always goes to
Dmitry, as do secrets, real/user/production data or persistent project files at
risk, irreversible actions, external messages, publication, payments/orders,
and expansion beyond existing authority. Classify security topics by blast
radius and reversibility: validated task-specific temporary fixtures and
sandbox-only reversible changes may be delegated.

Treat remaining user text as `on <North Star>`, `status`, or `off`. Fable calls
must use `~/.claude/scripts/fable_consult.sh`; never fabricate a user answer.

For `on <North Star>`, state activation and persistent work activation are one
operation. After validating project-local autopilot state, call `get_goal`
first. Retain a matching unfinished goal. If there is no unfinished goal, call
`create_goal` with an objective derived from the North Star and do not pass a
`token_budget`. If a different unfinished goal exists, report that concrete
conflict and do not replace or falsely complete it. `status` and `off` never
create a goal. After creating or retaining the matching goal, immediately begin
the first autonomous North-Star/roadmap step in the same turn; never end the
activation turn with only setup confirmation or status.

After the first concrete artifact contract and allowed paths are known, follow
the prospective command sequence: generate concrete run/wrapper-session UUIDs,
immediately call installed `slice_calibration.py session-start`, then wrap
`slice_ledger.py acquire` and `slice_git.py capture` in paired control events.
Treat durable session-start as a prerequisite: on failure do not acquire,
capture, or begin managed slice work, although directional autopilot may remain
active. Emit control-end only after success; failures use typed
`control-na --reason operation_failed` and remain non-PASS.
Record real verification, exact hash-bound terminal, and actual domain events
only at Booster-owned lifecycle points. Unsupported native Codex uses typed
`control-na` UNKNOWN; never backfill or fabricate evidence. Failure is
an explicit diagnostic, not native enforcement, and does not silently stop the
first safe work step. `status` uses cached ledger/close/telemetry status only—no
transcript discovery—and reports `Claude hooks/wrappers advisory; native Codex
observational/no enforcement`. `off` preserves all slice history and never
fabricates closure for an active slice. Directional `.claude/autopilot.json`
and the implementation slice ledger remain separate.

Codex activation requires one explicit existing transcript. The leading
`session_meta.payload.session_id` is the root session and must match
`--session-id`; `session_meta.payload.id` is a distinct thread identity and may
differ. Persist only their hashes and the metadata hash, never raw metadata.

Codex must use the same trusted lifecycle as Claude:
`fable_autopilot.py consult-decision --prompt-file` or trusted
`checkpoint plan_complete|first_slice|final_diff --prompt-file`. The runner
itself reserves, invokes `fable_consult.sh`, hashes exact output, validates an
`ON_COURSE|REFOCUS|REPLAN|ASK_USER` verdict, typed directive, output SHA-256,
and `/go fable` watchlist reconciliation. State is project-local and its stored `scope` must equal the
resolved git/workspace root; never use ambient HOME state for another project.
