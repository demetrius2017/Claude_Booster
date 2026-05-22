---
description: "Tool strategy: direct tools, agents, PAL MCP, Context7, Browser MCP. Always loaded."
---

# Tool Strategy
- **Direct tools** (Glob, Grep, Read, WebSearch, WebFetch) — parallel calls for speed.
- **Agents** — tasks spanning 5+ files, 2+ independent streams, deep research. NOT for small edits (<3 files).
  - `subagent_type: Explore` — codebase search
  - `subagent_type: Plan` — architectural planning
  - `general-purpose` — implementation, testing, audit
- **[CRITICAL] Model routing for delegated agents — Lead decides by complexity, Lead itself stays on Opus 4.7 (1M):**
  Pass explicit `model` param to `Agent` tool. Default (omitted) inherits Lead's Opus 4.7 — **do not** leave it blank for mechanical work, that burns Opus budget.
  - **Trivial / mechanical** (grep, file read, path lookup, simple regex edit, boilerplate, "find X in repo") → `model: "haiku"` — Haiku 4.5, fastest, cheapest.
  - **Medium** (pure mechanical work: boilerplate generation, simple grep-replace, formatting, single-file code review, standard /code-review passes) → `model: "sonnet"` — Sonnet 4.6. Use Sonnet only when the work is clearly mechanical with no reasoning required.
  - **Coding / implementation** (Worker agents that write code: features, bug fixes, refactors, test files, config changes producing ≥20 lines) → **check balancer first:** `python3 ~/.claude/scripts/model_balancer.py get coding` — if `provider=codex-cli` → use `codex_sandbox_worker.sh <model>` via Bash (not Agent tool); if `provider=anthropic` → `model: "sonnet"` in Agent call. Fallback (no balancer / error): `model: "sonnet"`. For supervised workers via `/lead`, use `--model claude-sonnet-4-6` explicitly. **For complex bugs or debugging with unknown root cause, escalate to Opus.**
  - **Bio-agent** (consilium opinion agents, audit lens agents, hackathon workers) → `codex_worker.sh gpt-5.5` via Bash. Codex gpt-5.5 (intelligence_score=20, same tier as Opus) is the default for these roles — flat-fee, no API token burn. Do NOT use Agent tool for bio-agents; pipe task to `codex_worker.sh gpt-5.5` directly.
  - **Hard** (architecture design, cross-system reasoning, security review of auth/broker/payments, deep debugging with unknown root cause, Round-2 audits, synthesis of 3+ agent outputs) → `model: "opus"` — inherits Opus 4.7.
  - **Tie-breakers:** if the agent will write ≥20 lines of non-boilerplate code → `sonnet`. If the task has a "why" question (root cause, trade-offs, design) → `opus`. If the task is purely "what / where" (locate, list, extract) → `haiku` is enough.

## Escalation on failure
When a delegated agent/worker fails:
- Do NOT retry on the same model tier
- Escalate: Haiku → Sonnet → Opus, or codex-5.3 → codex-5.5 → Opus
- Include the failed agent's session context in retry brief (see `paired-verification.md` §Session context injection)
  - **Do not downgrade the Lead** via `ANTHROPIC_DEFAULT_OPUS_MODEL` / settings.json `model` — the Lead orchestrates, synthesises, and decides routing; that requires the strongest model. Speed gains come from routing delegates, not from weakening the orchestrator.
  - **`model_balancer` (daily decision, since 2026-05-12):** The static tier mapping above is the **fallback**. The live daily routing decision lives in `~/.claude/model_balancer.json`, refreshed by `model_balancer.py decide` (SessionStart hook, idempotent per UTC day). On `/start`, `memory_session_start.py` injects a `=== MODEL BALANCER ===` line into `additionalContext` showing today's `lead/coding/hard/audit` routing. When delegating, prefer the balancer's choice over the static defaults — query via `python3 ~/.claude/scripts/model_balancer.py get <category>` (returns `{"provider","model"}`) or read the JSON directly. Categories: `trivial, recon, medium, coding, hard, consilium_bio, audit_external, lead, high_blast_radius`. **Explicit `model:` in command frontmatter or in the `Agent` call still wins** — balancer is the default, not the override. **high_blast_radius stays on Claude Sonnet via `Agent` tool** (not Codex) so `dep_guard.py` / `financial_dml_guard.py` / `verify_gate.py` PreToolUse hooks fire — Codex subprocess is opaque to those guards by design. Applies to: auth, security, secrets, db_migrations, financial_dml, infra_config.
- **Calling Codex (when balancer returns `"provider": "codex-cli"`):** Use `Bash` with `~/.claude/scripts/codex_worker.sh <model>` — this is a delegation signal; `delegate_gate.py` resets budget (same as `Agent`). Safe categories: `trivial`, `recon`, `medium` (read-only analysis), standalone `coding` on new isolated files, `consilium_bio`, audit lens agents, hackathon workers. NOT for `high_blast_radius` (migrations, auth, broker, infra). Lead MUST review output before applying file changes. **`codex_worker.sh gpt-5.5` replaces `mcp__pal__second_opinion` and `mcp__pal__codereview` as the default external review mechanism** — flat-fee vs API tokens.
  ```bash
  # Write task to temp file, pipe to Codex
  printf '%s\n' '<describe task in detail>' | ~/.claude/scripts/codex_worker.sh gpt-5.3-codex-spark
  # For longer prompts:
  cat > /tmp/codex_task.txt << 'EOF'
  <multi-line task description>
  EOF
  ~/.claude/scripts/codex_worker.sh gpt-5.3-codex < /tmp/codex_task.txt
  ```
  Check `python3 ~/.claude/scripts/model_balancer.py get <category>` to get today's model choice. If output needs to be applied as file edits, paste into Edit tool directly — do NOT let Codex write files directly.
- **Calling Codex Sandbox (when task requires file changes):** Use `codex_sandbox_worker.sh` when Codex needs to produce code edits. The sandbox runs Codex in an isolated git worktree, captures all changes as a unified diff on stdout. Lead applies each changed file via Edit tool — PreToolUse guards (`dep_guard.py`, `financial_dml_guard.py`, `verify_gate.py`) fire normally.
  ```bash
  # Pipe task to sandbox worker, get diff on stdout
  printf '%s\n' '<describe the coding task>' | ~/.claude/scripts/codex_sandbox_worker.sh gpt-5.3-codex
  # For longer prompts:
  cat > /tmp/codex_task.txt << 'EOF'
  <multi-line coding task with file paths and context>
  EOF
  ~/.claude/scripts/codex_sandbox_worker.sh gpt-5.3-codex < /tmp/codex_task.txt
  ```
  The diff output is a standard unified diff. For each file in the diff, apply changes via Edit tool. For new files, use Write tool. Guards fire on each Edit/Write.
  **When to use which:**
  - `codex_worker.sh` — read-only analysis, recon, trivial questions, consilium bio-agents, audit lens agents, hackathon workers, external second opinions. Output is text, not file changes.
  - `codex_sandbox_worker.sh` — coding tasks that produce file changes. Output is a diff. Lead applies via Edit/Write.
  - Neither (use `Agent` tool) — `high_blast_radius` tasks (auth, security, migrations, broker, infra) so PreToolUse hooks fire directly.
- **[CRITICAL] /lead model override for supervised workers:** When spawning workers via `/lead`, always pass `--model` explicitly. Default: `--model claude-sonnet-4-6` for coding tasks. Use `--model claude-opus-4-6` only when the task requires deep reasoning AND fast output. Never omit `--model` — without it, the worker inherits whatever `CLAUDE_BOOSTER_MODEL` env var is set to (or no model override at all).
- **Skills:** `/code-review` for code review (AUDIT phase). `/frontend-design` for UI tasks with Design Gate.
- **PAL MCP (GPT-5.5)** — optional; Codex CLI preferred for cost. For second opinions, prefer `codex_worker.sh gpt-5.5` (flat-fee) over PAL MCP (API tokens). Use PAL when Codex is unavailable or when the task specifically requires PAL's `thinkdeep`/`consensus` modes. `ask` for questions, `thinkdeep` for architecture, `consensus` for debates, `second_opinion`/`codereview` for validation.
  - **[CRITICAL] PAL file handling:** PAL server **reads files from disk itself** via `relevant_files`. NEVER paste file contents into `step`/`findings`/`problem_context` — GPT won't see them (truncation). Correct pattern:
    - `relevant_files`: array of **absolute paths** (PAL reads them itself, max 1MB/file, token budgeting)
    - `step`: description of current analysis step (what we're doing, what we're checking)
    - `findings`: conclusions and insights (text, NOT code)
    - `problem_context`: task context, business logic, constraints
    - `files_checked`: all examined files (including ruled-out ones) — for tracking
    - `relevant_context`: function/method names involved in the problem
- **Context7** — when working with external libraries: `resolve-library-id` + `query-docs` for up-to-date docs BEFORE writing code. Do not rely on memory — APIs change.
- **Browser MCP (3 levels):**
  - `chrome-devtools` — **primary power tool**. You have FULL browser control via `evaluate_script` — use it autonomously, NEVER ask the user to do things you can do yourself:
    - **Cache/storage:** `caches.delete()`, `localStorage.clear()`, `sessionStorage.clear()`, `indexedDB.deleteDatabase()`, `navigator.serviceWorker.getRegistrations().then(r=>r.forEach(sw=>sw.unregister()))` — clear any cache without asking user
    - **Console errors:** `list_console_messages` — read JS errors, warnings, failed assertions directly. Use `get_console_message(id)` for stack traces
    - **Network diagnostics:** `list_network_requests` — see all requests, status codes, timings. `get_network_request(id)` for full request/response bodies and headers
    - **Performance metrics:** `evaluate_script("JSON.stringify(performance.getEntriesByType('navigation')[0])")` — TTFB, DOM load, full load timing. `performance.getEntriesByType('resource')` — per-resource waterfall. `performance.memory` — heap usage
    - **DOM/state inspection:** `evaluate_script("document.querySelector(...)")` — check DOM state, computed styles, React devtools, app state
    - **Runtime debugging:** `evaluate_script` can run ANY JS in page context — intercept fetch, monkey-patch functions, add breakpoints, measure timing, check cookies, read/write globals
    - **Lighthouse:** `lighthouse_audit` — a11y, SEO, performance scores in one call
    - **Performance traces:** `performance_start_trace` / `performance_stop_trace` / `performance_analyze_insight` — Core Web Vitals, render blocking, LCP breakdown
    - **Device emulation:** `emulate(device, networkCondition, cpuThrottling)` — test mobile/slow network without asking user to switch devices
    - **Memory:** `take_memory_snapshot` — heap snapshots for leak detection
  - `claude-in-chrome` — visual: screenshots, GIF, find (NL), auth sessions
  - `playwright` — E2E tests, cross-browser, visual regression
