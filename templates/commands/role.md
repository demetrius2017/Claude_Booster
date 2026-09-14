---
description: Choose developer or lead execution for this session; default developer.
argument-hint: developer | lead | status
disable-model-invocation: true
---

# Session execution role

This command selects how the main agent carries out ordinary work. It does not
change model identity, permissions, hooks, model routing, or other client settings.
Use this contract directly; do not load the general Booster command runner.

## Selection and state

- `/role developer`: main agent implements; delegates research and testing.
- `/role lead`: main agent coordinates reusable domain agents.
- `/role` or `/role status`: show the current mode and known team, without changing it.
- Claude Code loads this file from `~/.claude/commands/role.md` as `/role`.
- Codex supports `$role developer`, `$role lead`, `$role status`, and the
  legacy `/prompts:role` alias. In Codex, bare `/role` works only if the client
  delivers it as user text; do not claim it is a registered native slash command.
- Any other argument: show the accepted values and retain the current mode.

Start each new conversation in `developer`. A selection lasts for the current
conversation, including its resumed/compacted continuation. Keep a compact state
in conversation/handoff context: mode, role-to-agent ID mapping, active assignment,
and unresolved integration obligations. Do not persist a global selected-mode flag
or reuse agent IDs from a different conversation. If a continuation loses the
selection, disclose that it is unknown and use the default until corrected.

Selecting a mode does not start agents or execute a project task by itself. Confirm
the mode in one sentence. A status request reports existing facts only.

## Developer (default)

The main agent develops, debugs, integrates, and carries the user task through to
its observable result. Delegate useful bounded research to a `research` agent and
independent testing/review to a `tester` agent; the main agent remains free to read
code and run immediate build/test/debug commands while implementing. A trivial
task need not manufacture research or testing assignments.

Research returns findings, source locations, and uncertainties. Tester checks
the requested behavior and integration, returning evidence and actionable defects.
These assignments do not transfer implementation ownership: the main agent fixes
defects. Reuse the same research/tester agents for follow-ups in this conversation
when available. If delegation is unavailable, disclose it and do those steps
directly; do not block the user's result for a missing helper.

## Lead

Delegate implementation to these long-lived role agents, created only when a
concrete assignment needs them:

| Role | Ownership |
|---|---|
| `devops` | Builds, deployment configuration, infrastructure, runtime operations |
| `frontend` | User interface and client behavior |
| `backend` | Services, APIs, data paths, server behavior |
| `tester` | Independent behavior/integration verification and regression checks |

Give each agent a coherent result to deliver, relevant source references, ownership
boundaries, and acceptance conditions. Keep coupled changes together where possible;
for cross-domain changes, agree on the shared interface before concurrent edits.
Do not delegate each tool call or restart an implementer after every fix.

Maintain one agent ID per role. Send subsequent tasks and corrections to that same
agent through the host's follow-up/resume mechanism. A completed turn or idle agent
is reusable: do not close it to make a replacement. In Codex use `followup_task`
for an idle agent and `send_message` for a running agent when those tools exist;
other hosts may expose equivalent resume/send-input tools. Use actual tool APIs,
not fabricated session IDs or background processes.

In Claude Code, retain returned Agent IDs and resume those agents through the
available Agent resume or SendMessage interface. If using native team members,
send the next assignment to the same named member without a shutdown request.
Do not replace reusable agents with one-shot `claude -p` subprocesses. Choose the
mechanism actually exposed by the current client; a name alone is not a session.

Retain agents for the lifetime of this conversation. This is not a promise that
the host preserves processes or context forever. If an agent is genuinely lost,
or its usable context is exhausted, disclose the replacement and transfer current
facts, decisions, failed approaches, and outstanding obligations to its successor.
Do not replace merely because it returned a defect or finished an assignment.

Honor the host's concurrency limit. Queue work and reuse idle agents when possible.
If retained agents exhaust the host's agent limit and a missing role cannot be
created, disclose that limit and perform the necessary work in the main session;
do not silently terminate another role to simulate a permanent four-agent team.

The main agent owns prioritization, shared interfaces, integration, and the final
user-visible outcome. It can inspect code, resolve integration changes, and run
checks directly. Do not declare the product complete merely because every agent
reports its assignment complete; verify the full requested scenario.

## Switching and scope

On a mode switch, retain the team mapping. Let current assignments finish, or
explicitly interrupt and obtain a handoff before reassigning their files. Never
start a new writer over work still owned by a running agent. In developer mode,
idle implementation agents stay available but receive no new implementation work.
Switching back to lead reuses them. A bare status request never spawns or polls.

This selection replaces blanket Booster requirements to always delegate ordinary
implementation, including legacy Lead/delegation cues injected by memory or phase
hooks. This only changes who implements: permission checks, data protections,
required verification, and phase transitions still apply. Do not disable or bypass
a hook to make a role work; report an actual incompatible block if one occurs.
It does not silently weaken a separately requested `/go`, `/audit`,
or other explicit command contract, and selecting lead is distinct from the
existing `/lead` supervisor command. After that command's bounded task, resume the
selected mode. Access memory when relevant; do not automatically reload old global
behavioral rules to choose how to work.
