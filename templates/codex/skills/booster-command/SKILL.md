---
name: "booster-command"
description: "Run Claude Booster command protocols in Codex. Use when Dmitry invokes or asks to install/run Booster commands such as start, handover, consilium, audit, code-review, architecture, go, debt, phase, update, delegate, lead, verify-flow, verify-after-edit, audit-trace, or hackathon."
---

# Booster Command Runner

This skill is the Codex compatibility layer for Claude Booster command specs.

Use it when the user invokes a Booster command by name, for example:

- `start`
- `handover`
- `consilium <topic>`
- `audit <topic>`
- `code-review [topic] [--scope <path>] [--apply]`
- `architecture [--update]`
- `go <artifact contract>`
- `debt <mode>`
- `$consilium <topic>` or `/prompts:consilium <topic>`

## Source Of Truth

Load the original command spec before executing. Resolve `<command>` from the
alias skill name, prompt name, or the first argument after `$booster-command`.

Search in this order:

1. `~/.claude/commands/<command>.md`
2. `references/commands/<command>.md` relative to this installed skill
3. `<repo-root>/templates/commands/<command>.md`
4. `<repo-root>/.claude/commands/<command>.md`

If the command spec is missing, say which paths were checked and stop.

## Codex Adapters

Execute the command behavior, not the literal Claude Code tool names.

- `Read` / `Grep` / `Glob` / `Bash` mean Codex file reads, `rg`, and shell tools.
- `EnterPlanMode` / `ExitPlanMode` mean use Codex planning behavior or `update_plan`.
- `TaskCreate` / `TaskUpdate` mean use `update_plan` and concise progress updates.
- Claude `Agent` / `Explore` / `general-purpose` means Codex subagents when the
  command explicitly asks for independent agents. The command invocation itself
  is explicit permission to use those subagents. Evidence is the spawned agent
  ids or names plus their final messages. If no Codex subagent tool is available,
  state that full independent-agent parity is unavailable and run only a local
  second pass; do not label that fallback as a full Booster multi-agent result.
- When spawning Codex subagents, pass `reasoning_effort: "medium"` unless the
  user explicitly requested a higher effort for that delegate. Do not omit it:
  omitted effort inherits the Lead session and can accidentally spawn delegates
  as `gpt-5.5 high`.
- Claude model names (`haiku`, `sonnet`, `opus`) are guidance only. Use Codex's
  available subagent defaults unless a model can be pinned safely.
- PAL/GPT external review: use PAL if the MCP tools exist. If not, use the
  Z.ai third-model runner when `ZAI_API_KEY` is present:
  `printf '%s\n' '<review prompt>' | ZAI_API_KEY="$ZAI_API_KEY" ~/.claude/scripts/zai_cli.py review --budget 5`.
  Label it exactly as "GLM-5.2 via Z.ai". If Z.ai is unavailable but Grok CLI
  is authenticated, use:
  `printf '%s\n' '<review prompt>' | ~/.claude/scripts/grok_cli.py review --budget-turns 3`.
  Label it exactly as "Grok via xAI". If PAL, Z.ai, and Grok are unavailable,
  spawn a separate Codex review subagent when subagents are available and label
  it clearly as "Codex second opinion", not as PAL/GPT or Z.ai. If none are
  available, mark the external-review step as unavailable with the missing tool
  evidence.
- Claude session JSONL paths under `~/.claude/projects/...` become the newest
  relevant Codex session JSONL under `~/.codex/sessions/...` when preparing a
  Codex handover. If a Claude session is relevant, mention both.
- Claude hooks are not assumed to be active in Codex. Preserve the evidence
  discipline in assistant output even when no hook enforces it.

### Cross-provider stages (SHIP-1..4 in `go` and `hackathon`)

The `go` pipeline (Phase 1B Challenge, Phase 1C Prototype Gate, Phase 2 Verifier,
Phase 3B Diff-review) and the `hackathon` edge-test harvest require each
verifying/reviewing role to run on a **different provider than the Worker** when
that provider channel exists. The spec is written from the Claude-CLI viewpoint,
where the native model is Claude and "the other provider" is Codex
(`codex_sandbox_worker.sh` / `codex_worker.sh`). On Codex CLI the roles
**mirror** — translate, do not execute literally:

- The native orchestrator here is gpt-5.5, so "the other provider" is Claude;
  reach it via a Claude subagent/CLI channel if one is available.
- The invariant that matters is **Worker and Verifier/Challenger/Reviewer run on
  DIFFERENT providers** — not which provider is native. Read the spec's
  `WP=codex-cli → Verifier=Opus` / `WP=anthropic → Verifier=codex` tables as
  "Worker on the native model → the other role on the other provider," and vice
  versa.
- The Prototype Gate is read-only executable proof, not coding. For broker/data
  sync, DB producers, migrations/backfills, ledger/NAV/TWR, financial data,
  external APIs, concurrency/cache, incident-driven fixes, or critical
  components, Codex must run or request a Prototyper pass before spawning Worker.
  A notebook is allowed only when accompanied by a repeatable probe script or
  command output; notebook-only proof is not sufficient handoff evidence.
- If `ZAI_API_KEY` is present, GLM-5.2 via `~/.claude/scripts/zai_cli.py` is a
  third-model read-only channel for Challenge, external audit, edge-harvest, and
  diff-review. It does not replace the exit-code Judge/Verifier unless a future
  audited command explicitly makes it write-capable.
- If Grok CLI is authenticated, Grok via `~/.claude/scripts/grok_cli.py` is a
  fourth-model read-only review channel, and `~/.claude/scripts/grok_sandbox_worker.sh`
  is a write-capable code-worker channel that must run in an isolated worktree
  and return a diff.
- If the other-provider, Z.ai, or Grok channel is unavailable on this CLI, apply the
  spec's documented DEGRADATION: run a same-provider second pass and label the
  result `cross-provider: DEGRADED (<reason>)`. NEVER claim cross-provider when
  it degraded. It is a quality optimization, not a safety gate — do not wedge
  the pipeline over it.
- `phase.py`, `model_balancer.py`, and `kpi_rework.py` (the `go` Phase 4 KPI
  `record`/`report`) are plain `python3 ~/.claude/scripts/<name>.py ...` and run
  identically on both CLIs — invoke them verbatim, including the auto-`record`
  at the end of every `go` run.

## Execution Rules

- **Pre-Work Context Gate:** before any command step that can lead to a code/config
  edit, deploy, migration, or coding subagent/worker spawn, build and state a
  `Context Receipt`:
  - Read `ARCHITECTURE.md` and `docs/dep_manifest.json` if they exist; extract
    touched components, critical flags, callers, feeds, and downstream consumers.
  - Run rolling memory start context for the project via MCP
    `memory_start_context(scope=<repo root>)` or
    `python ~/.claude/scripts/rolling_memory.py start-context --scope "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"`.
    If it lists `=== INCIDENT REGISTER ===` or `=== INCIDENT WARNINGS ===`, read
    every incident source before planning and carry forward the production impact,
    trigger, mitigation, recurrence guard, and "do not repeat" constraints.
  - Read the latest handover `## Summary`, `## Required reading`, and `## First
    step` sections when the command is starting a work session or continuing a
    prior thread; read every existing Required reading file before editing.
  - Cross-check architecture and memory claims against current code with `rg` or
    file reads before using them as facts.
  - Inject the relevant receipt lines into any subagent/worker prompt; subagents
    do not inherit the Lead's conversation memory automatically.
- Always perform RECON against current code/config before reports or opinions.
- For `consilium`, `audit`, and `architecture`, build a Verified Facts Brief
  before spawning subagents.
- For `go`, the Artifact Contract is incomplete unless it contains both
  `Architecture Context:` and `Incident Warnings:` fields populated from the
  Context Receipt. For broker/data/DB/financial/migration/external-system/
  incident/critical-component work, the `/go` run must also produce a
  `Prototype Handoff` before Worker: source-of-truth inputs, current-system
  comparison, first divergence, counts/samples, invariants proven, Worker facts,
  and Verifier regression assertions.
- Save generated reports to the same repo paths the original command specifies,
  usually `reports/`.
- Do not invent top-level Codex slash commands. If bare `/consilium` is
  intercepted by the UI, use `/prompts:consilium` or `$consilium`.
- Keep outputs concise but include concrete evidence for verification steps.
