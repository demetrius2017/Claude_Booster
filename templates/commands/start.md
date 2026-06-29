---
description: "Initialize a session: read README, handover, knowledge base, telemetry, then plan."
---

## Progress tracking
Before each numbered step below, run: `python3 ~/.claude/scripts/phase.py progress "<N>/4 <step_label>"`
After the final step completes, run: `python3 ~/.claude/scripts/phase.py progress clear`

Steps: `1/4 readme`, `2/4 knowledge_base`, `3/4 context_receipt`, `4/4 plan`
(Step 0 is a phase-set command, not a tracked step — tracking begins at step 1.)

0. **Set phase:** `python3 ~/.claude/scripts/phase.py set RECON` — session initialization is information gathering; this exempts diagnostic commands from the delegation gate.

1. Read `README.md` (always present). For roadmap, **try `Read roadmap.html` then `Read roadmap.md` — if the tool returns "file does not exist", move on; do NOT probe via shell glob like `ls roadmap.*` (zsh `nomatch` aborts the command before redirects apply, and one shell error in a parallel tool-call block cancels every sibling call).** For the latest `reports/handover_*.md`, read ONLY the `## Summary`, the "first step tomorrow" / "First step" section (Russian or English), and the `## Required reading` section — use `Read` with `offset`/`limit` narrow slices, not the whole file. Read every file listed under `## Required reading` before touching any code. If those sections cite a specific file needed for today's task, `Read` that too. Only read the full handover if you cannot locate the needed context from these sections. (Saves ~5,000 tokens per /start — per `reports/audit_2026-04-18_startup_token_budget.md` R2.) If `docs/` or `doc/` folder exists — read key files (architecture, API, setup, conventions).

   **[CRITICAL] Architecture map:** Try `Read ARCHITECTURE.md` (project root) and `Read docs/dep_manifest.json`. If either exists, scan the dependency table / components list for the area you'll be working in today — this is the "circuit board" showing what connects to what and what breaks if you change X. If **neither file exists** and the project has >20 source files: **auto-spawn architecture generation in background** — spawn a `general-purpose` Agent with `run_in_background: true` and `model: "opus"`. Agent prompt: "Read `~/.claude/commands/architecture.md` and execute the full /architecture protocol for the project at `<project root path>`." Note in the /start plan: "Architecture generation running in background — ARCHITECTURE.md and dep_manifest.json will appear when ready." Continue /start without waiting. For small projects (<20 files), note the gap in the plan but don't block. The user can also run `/architecture` manually at any time, or `/architecture --update` after major refactors.
2. **[CRITICAL] Review existing knowledge base — cross-project, category-biased:**
   - Run: `python ~/.claude/scripts/rolling_memory.py start-context --scope "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"` — lists indexed INCIDENT REGISTER warnings first, then consilium/audit rows. The git-toplevel resolution ensures the project category is correct even when Claude is launched from a subdirectory; the helper also walks ancestors to find the indexed project root, so a non-git project still resolves correctly. Lines marked `*` are this project's own; `-` are cross-project rows that may still be relevant. Each row has the source path — `Read` every incident source and every consilium/audit source relevant to the current task.
   - Topic-driven: same command + `--query "<keywords>"` — FTS5 search across the same corpus, current-project hits ranked first.
   - **Incident warnings are mandatory context:** if `start-context` emits `=== INCIDENT REGISTER ===` or `=== INCIDENT WARNINGS ===`, read those incident files before planning. Extract: production impact, trigger, mitigation, recurrence guard, and any "do not repeat" constraint.
   - Extract from each consilium/audit read: decisions made, rejected alternatives, identified risks, recommendations.
   - Factor these decisions into the plan — do not revisit what was already justified unless context has changed.
   - **If `start-context` returns "(no consilium/audit reports indexed...)"** or "DB not initialized": run `python ~/.claude/scripts/index_reports.py` once, then retry. Indexer is idempotent and also imports `reports/incident_*.md` plus frontmatter `type: incident`.
   - **Tag hygiene check**: `python ~/.claude/scripts/check_review_ages.py` — surfaces `[UNDER REVIEW]` tags that have passed their `resolve by` date or are missing canonical structure. Exit 0 = clean; exit 1 = at least one OVERDUE/MALFORMED line on stdout. Any finding must be resolved (retag `[SUPERSEDED by ...]` with replacement rule, restore to `[ACTIVE]`, or delete) before the session's main task — overdue tags are the T2.3 supersession mechanism rotting, per scenario #4.
   - **Rules-load canary**: `python ~/.claude/scripts/check_rules_loaded.py` — echoes the expected canary token. **Cross-check**: search the current session's loaded instructions for the same token. If the file is OK on disk but the token is not visible in Claude's system-reminder / loaded-rules, the `~/.claude/rules/` auto-load mechanism is broken (likely hook/upgrade regression, scenario #7). Surface the mismatch to the user before proceeding with the session's main task.
   - **Agent-health telemetry**: `python ~/.claude/scripts/telemetry_agent_health.py` — prints 5 anti-theater signals (evidence density, N/A ratio, overdue [UNDER REVIEW] tags, stale citations, session cadence). Informational — always exits 0. `⚠` markers surface regressions that should be acknowledged in the planning step. Reference at least one signal value in handover if any is non-✓. JSON mode available: `--json` for hooks/scripts. Reconsider MCP 2026-06-18 if agent starts parsing prose or needs cross-session queries the script can't answer.
   - **Rework KPI**: `python3 ~/.claude/scripts/kpi_rework.py report --window 30` — the SHIP-1..4 effectiveness signal. Prints first-pass-clean rate, mean verifier_fail_count, and per-defect-category totals over the last 30 days of `/go` runs (recorded automatically by `/go` Phase 4). "no data" until enough `/go` runs accumulate. Watch the trend: `contract_ambiguity` + `missed_failure_mode` should fall (challenge + Flow Designer catch them at design time) while first-pass-clean rate rises. If only `capability` defects move, the design-time gates aren't the lever — revisit. JSON: `--json`.
   - **Stuck-loop discipline**: when `start-context --stuck-check` emits
     `=== STUCK LOOP CANDIDATE ===`, before `EnterPlanMode` you MUST answer
     reframe-questions Q1–Q4 inline (one paragraph each). If answers
     justify continuing — proceed; if not — soft-delete the topic via
     `python ~/.claude/scripts/rolling_memory.py forget --id <N>` AND drop
     it from the next handover's First-step. Silently re-listing the topic
     without answering Q1–Q4 means the next /start re-fires the block.
3. **[CRITICAL] Emit a Context Receipt before planning.** This is the session's
   permit-to-work. It must be present before any code/config edit, deploy,
   migration, or coding Agent/Worker spawn. Format:

   ```
   Context Receipt:
     Architecture: <ARCHITECTURE.md read|absent>; dep_manifest: <read|absent>; touched components: <list|unknown until task topic known>
     Incidents: <none|N read: source paths + do-not-repeat constraints>
     Handover required reading: <none|paths read>
     Code cross-check: <files/functions grepped/read during /start>
   ```

   Hard stop: if `start-context` listed incident sources and they were not read,
   or if `## Required reading` listed existing files and they were not read, do
   not proceed to planning. Read them first. Reports decay, but unread incident
   reports are an explicit "blind agent" failure.
4. `EnterPlanMode` → summary report + action plan (informed by prior reports and the Context Receipt) → the user's approval → `ExitPlanMode`
