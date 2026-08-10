---
description: "Run hackathon — competitive multi-agent implementation. N Workers build the same feature in parallel and in isolation; an independent Judge uses candidate-bound authorized read-only evidence receipts; highest score wins."
argument-hint: <feature/task to implement>
---

## Progress tracking
Before each phase below, run: `python3 ~/.claude/scripts/phase.py progress "<N>/5 <step_label>"`
After the final step completes, run: `python3 ~/.claude/scripts/phase.py progress clear`

Steps: `1/5 arena_setup`, `2/5 competition`, `3/5 judging`, `4/5 verdict`, `5/5 ext_audit`

## Pattern: competitive multi-agent implementation

Unlike `/consilium` (opinions) and paired Worker+Verifier (one implementation), hackathon is **code that competes**. Multiple Worker agents implement the same Artifact Contract in isolation; an independent Judge compares them only through the same mandatory notebook and candidate-bound authorized read-only probes. No LLM judgment — only evidence-receipt exit statuses.

## Phase 1 — Arena setup (Lead)

1. **RECON** — read the relevant code (≤5 Read/Grep calls). Build a Verified Facts Brief: what exists, what interfaces must be preserved, what the feature replaces.

2. **Write Artifact Contract** — same format as `paired-verification.md`:
   - Objective, Verified Facts Brief, Inputs, Expected observable behavior, Out of scope
   - Set `Artifact path` as a template: `<base>_cN.<ext>` — each contestant writes to a distinct path
   - `Acceptance emphasis`: what the Judge will observe (Workers see this as a spec but do not run a synthetic acceptance loop)

3. **Write Judge Mandate** — a direct-observation spec and a mandatory durable notebook path under `notebooks/` or `reports/prototypes/`:
   - Each criterion = 1 point; total score = criteria passed
   - Criteria are observable behaviors (exit codes, file contents, stdout patterns, HTTP responses) proved with candidate-bound read-only probes
   - No LLM judgment allowed anywhere in the Judge Mandate

4. **Pick contestants** — 2–3 Workers. Default: equal footing (all the same model) to isolate the *approach* difference. **When invoked as a /go SHIP-4 escalation:** spawn candidates ACROSS providers instead (e.g. one Opus Agent + one Codex `codex_sandbox_worker.sh gpt-5.5`; when `ZAI_API_KEY` is present, add one GLM-5.2 read-only design/review contestant or external reviewer via `~/.claude/scripts/zai_cli.py`; when Grok CLI is authenticated, add one write-capable Grok contestant via `~/.claude/scripts/grok_sandbox_worker.sh grok-4.5`) — there the goal is provider diversity, not just prompt diversity. More contestants = more compute + better odds of the optimal solution.

## Phase 2 — Competition (all Workers in ONE message, parallel)

Before spawning, output: `Hackathon: spawning <N> Workers (Contestant 1..<N>) in parallel`

Spawn N Worker agents in a single `Agent` tool message. Each receives:
- The full Artifact Contract
- Their contestant ID: "You are Contestant N of M. Implement independently."
- Their output path: `<artifact_base>_cN.<ext>`
- The Judge Mandate as **observation spec only** (know what will be observed; do NOT run an acceptance suite yourself)
- Hard rule: do NOT read other contestants' output paths — implement from the contract only

## Phase 3 — Judging (one fresh-context Judge agent)

After all Workers return, output: `Workers complete (<N>/<N>). Spawning Judge...`

After ALL Workers return, spawn ONE Judge agent (new context, no Worker knowledge).
It receives the Artifact Contract, Judge Mandate, mandatory notebook, and all
artifact paths — never Worker reasoning or transcripts. It may plan earlier, but
candidate-verifying probe execution occurs only after the corresponding Worker
artifact exists and must be newer than its last relevant edit.

For every candidate the Judge runs only authorized read-only direct probes and
returns a PASS/FAIL/UNAVAILABLE matrix plus total score. Every receipt and
notebook cell binds the observation to artifact/tree-diff SHA-256 and, where
applicable, process/build/deployment/version identity; bounded raw output is
embedded and large raw output is a durable repo-relative artifact with SHA-256.
Old/current production is baseline evidence only unless deployed build identity
matches the candidate.

Fail closed: HTTP only GET/HEAD; SQL only via a DB-enforced read-only role or
transaction and only SELECT, non-mutating WITH, or EXPLAIN (no CALL, DDL/DML,
mutating CTE, side-effecting function, or COPY PROGRAM); CLI only documented
get/list/show/status/describe/logs/diff or an explicitly known read verb;
filesystem reads only. No shell redirection or pipe into a mutator. Each row
records operation class and allowlist decision. Unknown or unprovable read-only
operations are not executed and are marked UNAVAILABLE.

## Phase 4 — Verdict

Lead reads the score matrix:

| Result | Action |
|--------|--------|
| Clear winner | Move/copy winner to canonical path, then complete the mandatory canonical re-probe gate below before any deletion or `/go` diff review |
| Tie | Run `/code-review` on tied implementations; pick cleaner/shorter one |
| All fail — W (artifact wrong) | Re-run Workers with narrowed scope; include Judge output in new brief |
| All fail — V (observation contract over-constrained) | Refine the read-only observation contract; re-run Phase 3 only |
| All fail — A (contract ambiguous) | Clarify Artifact Contract; restart from Phase 2 |

Failure classification per `paired-verification.md` §Failure classification (W/V/A/E).
Hard cap: 3 retries per phase.

### Canonical re-probe gate — mandatory after winner move/copy

Moving or copying a winner changes the artifact under review. After the winner
is placed at its canonical path, the Judge MUST re-run the required authorized
read-only probes against the canonical post-move tree/runtime. Record a new
canonical artifact/tree-diff SHA-256, applicable process/build/deployment/version
identity, and evidence whose timestamp is newer than the move/copy time. The
canonical re-probe has its own PASS/FAIL/UNAVAILABLE receipt; pre-move candidate
evidence is baseline only for this decision.

Do not delete losing artifacts until the canonical re-probe PASS. On FAIL or a
required UNAVAILABLE observation, preserve every candidate artifact, classify
the result as W/V/A/E, and return to the corresponding remediation phase. Only
after canonical re-probe PASS may the Lead delete losing artifacts and resume
`/go` at Phase 3B diff review of the canonical winner.

### Edge evidence harvest — source facts only (SHIP-4)

Winner-take-all keeps a single coherent codebase — **never merge competing code**.
Until one winner is stable at the final deploy gate, edge harvest may harvest only
source-backed invariants and direct-probe requirements from candidates. It may
not create an acceptance suite, test harvest/rewrite, fixture, mock, synthetic
data, stand, harness, or executable test runner. This is not a property-style
test-generation loop.

After winner evidence PASS and diff-review stability, the final deploy gate
derives a regression manifest from proven evidence and every required
unavailable branch. Existing coverage counts only with named test path and
SHA-256. Any unavailable CRITICAL/HIGH required observation needs durable
coverage or deployment is blocked. Create/update durable regression tests once,
freeze the manifest and every test-file hash before the first full-suite run,
then run the full existing suite. No tests may change after that run begins; a
test-contract defect blocks for a separate scoped run, while code/environment
fixes may rerun the frozen suite.

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

**Auto-invoked by `/go` (SHIP-4):** for a high-blast-radius task with genuine solution uncertainty, `/go` escalates its implementation stage to a hackathon automatically (see `go.md` Phase 2 escalation decision). The hackathon's deterministic Judge replaces the single cross-provider Verifier for that run, candidates are spawned cross-provider, and edge harvest contributes only source-backed direct-probe requirements until the winner reaches the final deploy gate.

## Quick-start template

```
/hackathon implement <feature>

Phase 1 — Artifact Contract:
  Objective: <one sentence>
  Artifact path template: templates/scripts/<name>_cN.py
  Acceptance emphasis: <what Judge will test>
  Out of scope: <what not to change>

Phase 2 — 2 contestants, model: sonnet
Phase 3 — Judge with same candidate-bound read-only evidence notebook
Phase 4 — Verdict by score
```
