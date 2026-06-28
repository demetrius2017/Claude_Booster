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

4. **Pick contestants** — 2–3 Workers. Default: equal footing (all the same model) to isolate the *approach* difference. **When invoked as a /go SHIP-4 escalation:** spawn candidates ACROSS providers instead (e.g. one Opus Agent + one Codex `codex_sandbox_worker.sh gpt-5.5`; when `ZAI_API_KEY` is present, add one GLM-5.2 read-only design/review contestant or external reviewer via `~/.claude/scripts/zai_cli.py`; when Grok CLI is authenticated, add one write-capable Grok contestant via `~/.claude/scripts/grok_sandbox_worker.sh grok-build`) — there the goal is provider diversity, not just prompt diversity. More contestants = more compute + better odds of the optimal solution.

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

### Edge-test harvest — the one safe "merge" (SHIP-4)

Winner-take-all keeps a single coherent codebase — **never merge competing code** (that needs LLM judgment and manufactures integration bugs). But the LOSING candidates often cover edge cases the Judge Mandate missed, and that *test insight* is safe to import.

After a clear winner is selected, spawn ONE edge-harvest agent on a **different provider than the winner's author**:
- It reads ALL candidate implementations (winner + losers) + the Judge Mandate.
- It identifies edge cases / branches a LOSING candidate handled that the Judge Mandate does NOT already assert.
- It emits those as NEW executable assertions (same Test Legitimacy Standard: observable behavior, deterministic, non-zero exit on failure — NO LLM judgment).

Append the new assertions to the Judge Mandate and re-run it against the WINNER only:
- Winner passes all (including new) → done. The harvest confirmed coverage; the unioned suite is now the canonical acceptance test.
- Winner FAILS a new assertion → a real gap a loser caught. Respawn the winner's Worker to fix it (counts toward the retry cap), re-run.
- A new assertion is spurious (asserts a non-contract detail) → discard it (V-class). Do NOT weaken the winner to satisfy a test the contract never required.

Only TEST coverage is unioned across candidates; CODE stays single-author. This captures the tournament's diversity benefit without a code merge.

## Phase 5 — External audit (recommended for critical features)

After winner is selected:
- `mcp__pal__codereview` — GPT second opinion on winning implementation when PAL is available
- GLM-5.2 third-model review when `ZAI_API_KEY` is present:
  `printf '%s\n' '<winner review prompt>' | ZAI_API_KEY="$ZAI_API_KEY" ~/.claude/scripts/zai_cli.py review --budget 5`
- Grok fourth-model review when Grok CLI is authenticated:
  `printf '%s\n' '<winner review prompt>' | ~/.claude/scripts/grok_cli.py review --budget-turns 3`
- If PAL is unavailable, GLM-5.2 is mandatory fallback. If GLM is unavailable but Grok is authenticated, Grok is the mandatory fallback. If all external channels are unavailable, record `external-review: DEGRADED (PAL unavailable; ZAI_API_KEY absent; Grok unauthenticated)`.
- Address any HIGH findings before committing
- Save judge report to `reports/hackathon_YYYY-MM-DD_<topic>.md` and git commit

## When to use

| Use hackathon | Use instead |
|---------------|-------------|
| Multiple valid approaches; want the best, not just a working one | Simple deterministic task → Worker+Verifier |
| Critical feature worth parallel effort | Opinion/analysis question → `/consilium` |
| Optimisation problem (speed, size, correctness tradeoffs) | Pure research/exploration → Explore agents |
| Want empirical evidence of which approach wins, not Lead's prior | One obvious implementation → paired Worker+Verifier |

**Auto-invoked by `/go` (SHIP-4):** for a high-blast-radius task with genuine solution uncertainty, `/go` escalates its implementation stage to a hackathon automatically (see `go.md` Phase 2 escalation decision). The hackathon's deterministic Judge replaces the single cross-provider Verifier for that run, candidates are spawned cross-provider, and the edge-test harvest folds losers' coverage into the winner.

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
