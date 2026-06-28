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
2. Spawn 3-5 agents with different Bios (architect, security, product, devops, data engineer — task-specific). **Each agent receives the Verified Facts Brief, not raw report excerpts.**
   Before spawning, output: `Consilium: spawning <N> agents (<bio1> · <bio2> · …) + external reviewers`
3. Each independently: analysis, KPIs, decision
4. **[MANDATORY] External experts, provider-diverse:**
   Primary: use PAL MCP for independent GPT opinion when available:
   - `mcp__pal__ask` — request GPT analysis/opinion on a specific question
   - `mcp__pal__thinkdeep` — deep GPT reasoning on architectural decisions
   - `mcp__pal__consensus` — Claude vs GPT debate for controversial decisions
   - `mcp__pal__second_opinion` — GPT second opinion on a finished Claude solution
   - `mcp__pal__codereview` — code review via GPT
   Third-model reviewer: when `ZAI_API_KEY` is present, also run GLM-5.2 via:
   `printf '%s\n' '<consilium prompt>' | ZAI_API_KEY="$ZAI_API_KEY" ~/.claude/scripts/zai_cli.py review --budget 5`
   If PAL is unavailable, GLM-5.2 is the mandatory fallback. If both PAL and GLM are unavailable, label the external slot `DEGRADED (PAL unavailable; ZAI_API_KEY absent)`.
   After all agents and external reviewers return, output: `All <N+M> perspectives collected. Synthesizing...`
5. Lead: synthesis + table "agent / position / key insight / KPI" (including GPT/PAL and GLM-5.2 rows when available)
6. **[CRITICAL] Save results to file:**
   - Consilium → `reports/consilium_YYYY-MM-DD_<topic>.md`
   - Format: title, task context, agent positions (table), decision made, rejected alternatives with reasons, risks, implementation recommendations.
   - Git add + commit. These reports are the project's knowledge base, read during `start`.
