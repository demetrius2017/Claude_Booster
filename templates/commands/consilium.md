---
description: "Run consilium (multi-agent debate). RECON first, spawn 3-5 bio-specific agents + GPT/PAL and GLM-5.2 external perspectives when available, synthesize, save report."
argument-hint: <topic for consilium/audit>
---

## Progress tracking
Before each numbered step below, run: `python3 ~/.claude/scripts/phase.py progress "<N>/6 <step_label>"`
After the final step completes, run: `python3 ~/.claude/scripts/phase.py progress clear`

Steps: `1/6 recon`, `2/6 spawn_agents`, `3/6 analysis`, `4/6 gpt_review`, `5/6 synthesis`, `6/6 save_report`

1. **[CRITICAL] RECON before opinions — verify current state against code, not memory:**
   - Spawn Explore agents to read actual code/configs relevant to the topic (Grep for key functions, Read configs, check deploy state)
   - Cross-reference findings with reports/memory — flag discrepancies ("report says X, code shows Y")
   - Build a **Verified Facts Brief**: what exists now, what works, what doesn't — with file paths and evidence
   - Present brief to Dmitry before proceeding. If facts contradict the premise — reframe the question
   - **Never brief consilium agents from reports alone. Reports decay. Code is truth.**
   - Before spawn, test the brief: name its falsifier and check whether all
     perspectives would inherit the same unverified premise. Resolve or label it.
2. Spawn 3-5 agents with different Bios (architect, security, product, devops, data engineer — task-specific). **Each agent receives the Verified Facts Brief, not raw report excerpts.**
   Before spawning, output: `Consilium: spawning <N> agents (<bio1> · <bio2> · …) + external reviewers`
3. Each independently: analysis, KPIs, decision
4. **[MANDATORY] External experts, provider-diverse:**
   Primary: use PAL MCP for independent GPT opinion when runtime-available:
   - `mcp__pal__ask` — request GPT analysis/opinion on a specific question
   - `mcp__pal__thinkdeep` — deep GPT reasoning on architectural decisions
   - `mcp__pal__consensus` — Claude vs GPT debate for controversial decisions
   - `mcp__pal__second_opinion` — GPT second opinion on a finished Claude solution
   - `mcp__pal__codereview` — code review via GPT
   A PAL tool being registered or callable is not proof of runtime availability.
   PAL is successful only when the call returns a usable opinion. Treat `429
   insufficient_quota`, `401`, `403`, any `5xx`, timeout, connection-closed, or
   tool exception as `PAL runtime unavailable`; record the concrete sanitized
   reason and continue the fallback chain. Do not abort consilium and do not
   present an error payload as a reviewer opinion.

   Third-model reviewer: when `ZAI_API_KEY` is present, run GLM-5.2 via:
   `printf '%s\n' '<consilium prompt>' | ZAI_API_KEY="$ZAI_API_KEY" ~/.claude/scripts/zai_cli.py review --budget 5`
   On PAL runtime failure, attempt reviewers in this exact order until a usable
   external opinion returns: **Z.ai → Grok → Codex native second opinion**.
   Grok command:
   `printf '%s\n' '<consilium prompt>' | ~/.claude/scripts/grok_cli.py review --model grok-4.5 --budget-turns 3`
   A missing credential/binary, non-zero exit, timeout, empty/error-only output,
   or tool exception means that reviewer is runtime unavailable and advances the
   chain. Label successful routes exactly `GLM-5.2 via Z.ai`, `Grok via xAI`, or
   `Codex second opinion`. A Codex-native fallback is same-provider and MUST be
   marked `degraded_external_independence`; it is a second pass, not independent
   external verification. If no fallback returns a usable opinion, mark the
   external slot unavailable with sanitized evidence and continue consilium.
   A successful PAL opinion remains the primary PAL/GPT result; fallback does
   not replace or relabel it.
   After all agents and external reviewers return, output: `All <N+M> perspectives collected. Synthesizing...`
5. Before synthesis, Lead checks source independence, states the strongest
   counterargument to the majority, and independently rechecks one decision-critical
   fact. Then synthesize + table "agent / position / key insight / KPI"
   (including the successful external route and any additional successful
   provider-diverse reviewers).
6. **[CRITICAL] Save results to file:**
   - Consilium → `reports/consilium_YYYY-MM-DD_<topic>.md`
   - Format: title, task context, agent positions (table), decision made, rejected alternatives with reasons, risks, implementation recommendations.
   - Git add + commit. These reports are the project's knowledge base, read during `start`.
