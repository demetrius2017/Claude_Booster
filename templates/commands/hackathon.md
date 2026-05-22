---
description: "Run hackathon — competitive multi-agent implementation. N Workers build the same feature in parallel and in isolation; an independent Judge tests all solutions with the same acceptance suite; highest score wins."
argument-hint: <feature/task to implement>
---

## Progress tracking
Before each phase below, run: `python3 ~/.claude/scripts/phase.py progress "<N>/5 <step_label>"`
After the final step completes, run: `python3 ~/.claude/scripts/phase.py progress clear`

Steps: `1/5 arena_setup`, `2/5 competition`, `3/5 judging`, `4/5 verdict`, `5/5 ext_audit`

## Pattern: competitive multi-agent implementation

Unlike `/consilium` (opinions) and paired Worker+Verifier (one implementation), hackathon is **code that competes**. Multiple Worker agents implement the same Artifact Contract in isolation; an independent Judge runs the same acceptance tests against every implementation; highest score wins. No LLM judgment — only exit codes.

## Phase 1 — Arena setup (Lead)

1. **RECON** — read the relevant code (≤5 Read/Grep calls). Build a Verified Facts Brief: what exists, what interfaces must be preserved, what the feature replaces.

2. **Write Artifact Contract** — same format as `paired-verification.md`:
   - Objective, Verified Facts Brief, Inputs, Expected observable behavior, Out of scope
   - Set `Artifact path` as a template: `<base>_cN.<ext>` — each contestant writes to a distinct path
   - `Acceptance emphasis`: what the Judge will test (Workers see this as a spec — they know what will be tested but do not run the tests themselves)

3. **Write Judge Mandate** — an executable acceptance test spec:
   - Each criterion = 1 point; total score = criteria passed
   - Criteria are observable-behavior assertions (exit codes, file contents, stdout patterns, curl responses)
   - No LLM judgment allowed anywhere in the Judge Mandate

4. **Pick contestants** — 2–3 Workers, all at `model: "sonnet"` (equal footing). More contestants = more compute + better odds of finding optimal solution.

## Phase 2 — Competition (all Workers in ONE message, parallel)

Before spawning, output: `Hackathon: spawning <N> Workers (Contestant 1..<N>) in parallel`

Spawn N Worker agents in a single `Agent` tool message. Each receives:
- The full Artifact Contract
- Their contestant ID: "You are Contestant N of M. Implement independently."
- Their output path: `<artifact_base>_cN.<ext>`
- The Judge Mandate as **spec only** (know what will be tested; do NOT run tests yourself)
- Hard rule: do NOT read other contestants' output paths — implement from the contract only

## Phase 3 — Judging (one fresh-context Judge agent)

After all Workers return, output: `Workers complete (<N>/<N>). Spawning Judge...`

After ALL Workers return, spawn ONE Judge agent (new context, no Worker knowledge):

- Receives: Artifact Contract + executable Judge Mandate + list of ALL artifact paths
- Runs the identical test suite against each artifact path independently
- Produces: PASS/FAIL matrix (contestant × criterion) + total score per contestant
- **Must NOT inspect contestant code before running tests** — tests observable behavior, not internals
- Reports verbatim test output per contestant

## Phase 4 — Verdict

Lead reads the score matrix:

| Result | Action |
|--------|--------|
| Clear winner | Move winner artifact to canonical path; delete others |
| Tie | Run `/code-review` on tied implementations; pick cleaner/shorter one |
| All fail — W (artifact wrong) | Re-run Workers with narrowed scope; include Judge output in new brief |
| All fail — V (tests over-constrained) | Rewrite Judge Mandate; re-run Phase 3 only |
| All fail — A (contract ambiguous) | Clarify Artifact Contract; restart from Phase 2 |

Failure classification per `paired-verification.md` §Failure classification (W/V/A/E).
Hard cap: 3 retries per phase.

## Phase 5 — External audit (recommended for critical features)

After winner is selected:
- `mcp__pal__codereview` — GPT second opinion on winning implementation
- Address any HIGH findings before committing
- Save judge report to `reports/hackathon_YYYY-MM-DD_<topic>.md` and git commit

## When to use

| Use hackathon | Use instead |
|---------------|-------------|
| Multiple valid approaches; want the best, not just a working one | Simple deterministic task → Worker+Verifier |
| Critical feature worth parallel effort | Opinion/analysis question → `/consilium` |
| Optimisation problem (speed, size, correctness tradeoffs) | Pure research/exploration → Explore agents |
| Want empirical evidence of which approach wins, not Lead's prior | One obvious implementation → paired Worker+Verifier |

## Quick-start template

```
/hackathon implement <feature>

Phase 1 — Artifact Contract:
  Objective: <one sentence>
  Artifact path template: templates/scripts/<name>_cN.py
  Acceptance emphasis: <what Judge will test>
  Out of scope: <what not to change>

Phase 2 — 2 contestants, model: sonnet
Phase 3 — Judge with same test suite
Phase 4 — Verdict by score
```
