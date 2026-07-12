---
description: "Execute Семёрка / Семёрка-F when opt-in (Flow Designer → Challenge → Prototype Gate → Worker + Verifier → Test → Diff-review → Verdict) — hardcoded, non-skippable cross-provider pipeline."
argument-hint: "[fable] <Artifact Contract — structured text with Objective, Verified Facts, etc.>"
---

## Progress tracking
Before each numbered step below, run: `python3 ~/.claude/scripts/phase.py progress "<N>/7 <step_label>"`
After the final step completes, run: `python3 ~/.claude/scripts/phase.py progress clear`

Steps: `1/7 flow_designer`, `2/7 challenge`, `3/7 prototype_gate`, `4/7 worker_verifier`, `5/7 test_run`, `6/7 diff_review`, `7/7 verdict`

---

## Opt-in mode — `/go fable` / Семёрка-F

If the first non-flag token in `$ARGUMENTS` is `fable`, consume it as an
opt-in mode selector and run the normal `/go` pipeline with **Fable as Quality
Chair for this run**. Fable is not Lead, not Worker, not Verifier, not the final
judge, and not a default route. The Booster Lead still owns orchestration,
evidence, retries, git state, budget caps, and the final exit-code verdict.
This mode MUST NOT mutate `~/.claude/model_balancer.json`.

`/go fable` changes exactly two artifact-mediated gates:

1. **Phase 1B Challenge** runs as `Fable Challenge` when the budget gate allows
   it. It reviews the Flow Designer's PFD and emits typed additions plus a
   `fable_watchlist`.
2. **Phase 3B Diff-review** runs as `Fable Diff-review` when the budget gate
   allows it. It closes the `fable_watchlist` item-by-item against the final
   diff and test evidence.

Context continuity is artifact-only. Do not rely on hidden Fable chat memory.
The Phase 1B reconciliation MUST persist all Fable context into the augmented
PFD under:

```yaml
fable_control:
  enabled: true
  mode: quality_chair
  max_fable_calls: 2
  calls_used: <0|1|2>
  degraded: false
  downgrade_reason: null
  challenge_session: <session id/path or none>
  diff_review_session: <session id/path or none>
  watchlist:
    - id: FBL-001
      origin: fable-challenge
      severity: HIGH | MED | LOW
      concern: <specific risk>
      close_criteria: <observable closure condition>
      required_evidence: <test/assertion/file:line/prototype evidence>
      target_phase: prototype | worker | verifier | diff_review
      status: OPEN | CLOSED | PARTIAL
      closure_evidence: null
  rework_log:
    - from_phase: <2|6>
      to_phase: flow_designer | prototype_gate | worker | verifier | user
      reason: DESIGN_REWORK | PROTOTYPE_REWORK | IMPLEMENTATION_REWORK | VERIFIER_REWORK | CONTRACT_AMBIGUOUS
      evidence: <specific finding id/output>
```

Budget guardrails:

- Hard cap: **at most 2 Fable calls per `/go fable` run**: one Challenge call
  and one Diff-review call. Worker retries, verifier retries, debugging,
  failing-test triage, and repeated post-rework reviews MUST NOT call Fable.
- Before each Fable call, check current usage/routing context. If the weekly
  usage snapshot is `>=80%`, or Fable is unavailable, set
  `fable_control.degraded=true`, record `downgrade_reason`, and run the same
  gate on the normal audit/external review lane using the persisted
  `fable_watchlist` contract.
- If the final diff is large, first use the cheap lane to produce a
  watchlist-oriented diff summary and pass only the relevant slices plus the
  `fable_watchlist` to Fable.
- Fable usage lines are estimates only. Run
  `python3 ~/.claude/scripts/fable_usage.py refresh-display` after any Fable
  call and include the output if non-empty.

Progress labels remain seven segments; annotate the two upgraded gates instead
of adding hidden phases: `2/7 challenge(fable)` and `6/7 diff_review(fable)`.

---

## Phase 0 — AC VALIDATION (Lead, mandatory before any agent spawns)

Parse `$ARGUMENTS` as the Artifact Contract. If the first non-flag token is
`fable`, set `GO_FABLE=1`, remove that token from the Artifact Contract text,
and apply the opt-in Семёрка-F rules above. It may also be formulated in the
preceding conversation context if the user said "run /go" after building the AC
together.

Check that the Artifact Contract contains ALL of these mandatory fields:

| Field | Description |
|-------|-------------|
| `Objective:` | One sentence — what the system should do after this change |
| `Verified Facts Brief:` | Current state evidence with file:line references (NOT docs or memory alone) |
| `Architecture Context:` | `ARCHITECTURE.md` / `docs/dep_manifest.json` consulted or absent, touched components, critical flags, callers, feeds, downstream consumers, and code cross-check evidence |
| `Incident Warnings:` | Incident register result: `none` OR source paths read plus production impact, trigger, mitigation, recurrence guard, and "do not repeat" constraints |
| `Regression Loop Guard:` | File-scoped preservation analysis for every edited existing file: touched surface, consumers checked, relevant git/incident history, behaviors that must not regress, and the verification target |
| `Artifact path:` | Where Worker writes the result |
| `Expected observable behavior:` | What an external observer sees (curl response, file content, stdout, etc.) |
| `Acceptance emphasis:` | What Verifier must specifically check |

**If ANY field is missing:**

Output exactly:
```
/go BLOCKED — Artifact Contract incomplete.

Missing fields:
  - <field name>: <what is needed>
  ...

Complete the AC and re-run /go. Do not proceed without a complete AC.
```

Then STOP. Do NOT spawn any agents.

**If AC is complete:** write the .go_active marker so the go_gate hook allows Agent spawns during this pipeline run. Write a **run tag** into the marker — it scopes this run's RECON findings (Phase 1) and the post-pipeline debt clear (Phase 4):
```bash
RUNTAG="go:$(date +%s)"
printf '%s\n' "$RUNTAG" > "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.claude/.go_active"
```
Remember `RUNTAG` for the rest of the pipeline (go_gate checks marker *existence*, not content — the tag is for debt scoping). Then proceed to Phase 1.

### Incident warnings gate (mandatory after AC validation, before Flow Designer)

Load the production incident register before any Flow Designer work:

```bash
python ~/.claude/scripts/rolling_memory.py start-context --scope "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
```

Cross-check the command output against the Artifact Contract's
`Incident Warnings:` field. If the command lists incidents but the field says
`none`, the Artifact Contract is stale or incomplete: pause, read every incident
source, update `Incident Warnings:`, then continue. Do not spawn Flow Designer
from a stale incident view.

If the output contains `=== INCIDENT REGISTER ===` or `=== INCIDENT WARNINGS ===`:

1. Read every listed incident source path before Phase 1.
2. Extract production impact, trigger, mitigation, recurrence guard, and any "do not repeat" constraint.
3. Add a compact `Incident Warnings` block to the Flow Designer prompt immediately before the Artifact Contract.

Do not treat incidents as `error_lesson`, `audit`, or `consilium` rows. They are a separate high-priority memory lane for post-deploy production incidents.

### Architecture context gate (mandatory after AC validation, before Flow Designer)

Cross-check the Artifact Contract's `Architecture Context:` against current
project files:

1. Read `ARCHITECTURE.md` and `docs/dep_manifest.json` if they exist.
2. Identify touched components, any `critical: true` entries, `called_by`,
   `feeds`, downstream consumers, protected tables/paths, and stale-doc risks.
3. Verify the relevant entries against current code with `rg`/file reads before
   treating them as facts.
4. If either architecture file is absent, keep going only after the field says
   so explicitly and the Flow Designer prompt includes `Architecture Context:
   absent`.

If `Architecture Context:` is missing, says `unknown`, or does not name the
files checked, `/go` remains blocked. A Worker that only sees a code fragment is
not allowed to write production code.

### Regression loop guard (mandatory after architecture gate, before Flow Designer)

Cross-check the Artifact Contract's `Regression Loop Guard:` against the files
that will be edited. This gate exists to prevent the classic edit loop: fix A,
silently break adjacent behavior B, then spend the next sessions repairing the
damage caused by the repair.

For every existing file in `Artifact path:` or otherwise expected to be edited,
the field must follow the canonical format in `rules/core.md` §Regression Loop
Guard. Keep `rules/core.md` as the source of truth for the checklist; this gate
only validates that the Artifact Contract contains enough concrete evidence to
feed `verifier_assertions`.

Greenfield exemption: if no existing file will be edited, the field must state
`Regression Loop Guard: none — new files only: <paths>`. That value passes this
gate. A bare `N/A` does not.

If `Regression Loop Guard:` is missing, says only `N/A`, omits consumers for an
existing-file edit, or contains preservation claims without either an executable
verification target or an explicit `advisory/test gap`, `/go` remains blocked.
An `advisory/test gap` satisfies field completeness, but it must be logged as
debt and cannot be used as PASS evidence. Do not spawn Flow Designer until the
guard is concrete enough to feed `verifier_assertions`.

---

## Phase 1 — FLOW DESIGNER

Run: `python3 ~/.claude/scripts/phase.py progress "1/7 flow_designer"`

Query the model balancer:
```bash
python3 ~/.claude/scripts/model_balancer.py get hard
```

Use the returned provider/model/reasoning_effort for the Flow Designer. Pass Codex effort per invocation via `CODEX_REASONING_EFFORT`; never export it globally. Fallback if balancer fails: provider `anthropic`, `model: "opus"`.

### Spawn mechanics by provider

| Provider from `get hard` | Flow Designer spawn path |
|---|---|
| `anthropic` or balancer error | Spawn ONE Flow Designer via the **Agent tool** with the returned model; fallback `model: "opus"`. **NOT `run_in_background`** — Lead waits for the result before Phase 1B. |
| `codex-cli` | Run via `CLAUDE_BOOSTER_TASK_CATEGORY=hard CODEX_REASONING_EFFORT="<reasoning_effort>" ~/.claude/scripts/codex_worker.sh <model> < <prompt-file>`. The prefixes are per invocation: category tags telemetry and effort prevents inheritance from the Lead. This is the read-only TEXT channel. |
| `zai-cli` | Run the Flow Designer via Bash: `printf '%s\n' "$PROMPT" \| ZAI_API_KEY="$ZAI_API_KEY" ~/.claude/scripts/zai_cli.py review --budget 5 --model <model>`. Use only when `ZAI_API_KEY` is present; otherwise fall back and log `zai-cli unavailable`. |

**Flow Designer agent prompt:**

```
You are a Flow Designer agent. Your role is to produce a Process Flow Document (PFD) for the following task.

The PFD is the foundational artifact that shapes what both the Worker and the Verifier will do.
It is NOT a design doc — it is a map of temporal traps, branching outcomes, and invariants.
Be specific. Be pessimistic. Name every failure mode.

---

## Artifact Contract

<INSERT FULL ARTIFACT CONTRACT FROM PHASE 0>

---

## Your methodology: Three Lenses

Apply ALL three lenses to the task described in the Artifact Contract:

**Lens 1 — Temporal Projection:** For every non-trivial state variable, ask "what will this value be at effect-time, not read-time?" Identify STATIC / DECAYING / ACCUMULATING / VOLATILE / PERIODIC variables. Quantify temporal gaps (how long between phases? what drifts during that time?).

**Lens 2 — Branching & Failure Modes:** Apply HAZOP guide words (NO, MORE, LESS, REVERSE, LATE, EARLY, OTHER, PARTIAL) to every external interaction (DB, API, filesystem, broker, network). For each operation, enumerate at minimum: success, one failure, one partial/race outcome. Identify blast radius per branch.

**Lens 3 — State Dependency Cascade:** When source state X changes, what derived values become stale? Trace cascade chains. Identify consistency boundaries (atomic vs eventually consistent). Name hidden consumers of derived state.

---

## Required output: PFD in YAML format

Produce a PFD with ALL of the following sections (per flow-designer.md §4 schema):

- `meta` — task, temporal_class, time_horizon, critical_state_vars
- `timeline` — ordered phases, each with state_at_entry + operations + per-outcome state_delta + next_phase + recovery
- `state_variables` — name, temporal_class, freshness_window, depends_on, cascade_depth, recompute_trigger
- `branching_scenarios` — per operation: all branches, guide_word, state_after, downstream_effects, blast_radius, recovery
- `failure_modes` — id, guide_word, operation, trigger, affected_state, detection, downstream_impact, mitigation, category
- `invariants` — name, expression (formal/semi-formal boolean), violation_consequence, enforcement_point
- `temporal_gaps` — between (phases), duration (MUST be quantified), drifting_state, drift_rate, stale_after, mitigation
- `cascade_chains` — trigger, chain, propagation_time, atomicity, current_gap
- `worker_directives` — imperative ("MUST..."), rationale (which failure_mode/gap this prevents), enforcement type
- `prototype_plan` — read-only executable proof plan: data sources, commands/notebook/script path, exact comparisons, invariants to prove before Worker, expected handoff artifact
- `verifier_assertions` — assertion (what to test), type (temporal/branching/invariant/freshness/cascade), how (concrete test approach), derived_from (failure_mode ID or invariant)
- `role_handoff_contract` — exact payload each downstream role receives from the previous role; include fields, artifact paths, allowed writes, forbidden state changes, and pass/fail criteria
- `fable_control` — required only when `GO_FABLE=1`; use the schema from
  `/go fable` above, initialized with `enabled: true`, `calls_used: 0`, and
  empty `watchlist`/`rework_log`
- `branch_tree.mermaid` — visual graph of operations and outcomes (all non-success terminals shown)
- `adjacent_findings` — **RECON-as-review output.** While reading the existing code to build the PFD, do NOT just study it — REVIEW it critically, the way a code reviewer would. Every defect, inaccuracy, wrong assumption, missing guard, dead code, or risky pattern you notice in the code you read becomes an entry here. This is separate from `failure_modes` (those are about the NEW artifact); `adjacent_findings` is about the EXISTING surrounding code. Each entry: `location` (file:line), `severity` (HIGH = real bug / MED = inaccuracy or latent risk / LOW = smell or style), `in_radius` (true if it sits in the artifact being built OR a direct caller/helper this task touches; false if it is tangential code you happened to read), `issue` (one line), `fix` (one line). Empty list is allowed ONLY if the code you read was genuinely clean — say so explicitly rather than omitting the section.

Quality criteria — your PFD FAILS internal review if:
- Any operation has only one outcome (happy path only)
- Any temporal_gap has vague duration ("some time", "eventually") — MUST be quantified
- Any invariant is not expressible as a boolean assertion
- Any worker_directive is advisory ("should", "consider") instead of imperative ("MUST")
- `prototype_plan` is empty for a task touching broker sync, database producers, migrations, ledger/NAV/TWR, external APIs, concurrency/cache, or incident-driven fixes
- `role_handoff_contract` does not specify what evidence is passed from Flow Designer → Challenge → Prototyper → Worker → Verifier → Diff Reviewer
- The branch_tree shows no non-success terminal states
- The failure_modes list is empty

Output ONLY the YAML PFD. No prose before or after.
```

**After Flow Designer returns:**

Extract from the PFD:
- Count of `failure_modes` entries → `<N>`
- Count of `worker_directives` entries → `<M>`
- Count of `verifier_assertions` entries → `<K>`
- Count of `prototype_plan` checks → `<P>`

Output:
```
Flow Designer complete. PFD: <N> failure modes, <M> worker directives, <K> verifier assertions, <P> prototype checks.
```

**RECON-as-review harvest — log each `adjacent_findings` entry as a scoped debt:**
For every entry in the PFD's `adjacent_findings`, run:
```bash
/debt add "<issue> — fix: <fix> (<location>)" --priority <HIGH|MED|LOW> --origin "$RUNTAG" --in-radius <true|false>
```
These are findings about EXISTING code, scoped to this run via `$RUNTAG`. They are NOT worked now — they are cleared at the very end (Phase 4 post-pipeline) by `/debt auto --scope "$RUNTAG"`, which auto-fixes only the in-radius HIGH/MED and surfaces the rest to the user. Output: `RECON review: +<X> adjacent findings logged (<a> in-radius, <b> adjacent).`

Save the full PFD text for Phase 1B.

---

## Phase 1B — PFD ADVERSARIAL CHALLENGE (cross-provider, Opus)

Run: `python3 ~/.claude/scripts/phase.py progress "2/7 challenge"` (or
`"2/7 challenge(fable)"` when `GO_FABLE=1`)

The Flow Designer drafted the PFD on the `hard` tier. This phase has a **different-provider** reviewer attack that PFD **before any code is written** — the cheapest place to catch rework. (Consilium 2026-06-13: contract ambiguity + missed failure modes are ~65% of returns-to-code; model capability is ~5%. Design-time is where the strong model earns its keep — see `reports/consilium_2026-06-13_dual_model_rework_reduction.md`, SHIP-1.)

**Provider rule — the challenge MUST run on a different provider than the Flow Designer (this is the whole point — a model cannot find its own blind spots):**

- Check what `python3 ~/.claude/scripts/model_balancer.py get hard` returned for Phase 1.
- **If `GO_FABLE=1` and the budget gate allows it**: run exactly ONE read-only
  Fable Challenge (`claude --print --model fable` or the strongest available
  Fable channel) with edit/write/deploy tools disabled. This consumes the
  Challenge Fable call budget and must update `fable_control.calls_used`. If
  Fable is unavailable or weekly usage is `>=80%`, set
  `fable_control.degraded=true`, record `downgrade_reason`, and use the normal
  cross-provider Challenge mapping below with the same output contract.
- **If Flow Designer's provider was `codex-cli` or `zai-cli`**: spawn ONE Challenge **Agent** with `model: "opus"` explicitly. **NOT `run_in_background`** — Lead waits.
- **If Flow Designer's provider WAS `anthropic`**: prefer GLM-5.2 when available; otherwise use `CLAUDE_BOOSTER_TASK_CATEGORY=hard CODEX_REASONING_EFFORT=medium ~/.claude/scripts/codex_worker.sh gpt-5.6-sol < <prompt-file>`.

Either way the prompt is identical:

**Challenge prompt:**

```
You are a PFD Challenge agent. A Flow Designer (a DIFFERENT model) produced the Process Flow Document below. Attack it adversarially and find what it missed — before any code is written. You do NOT write code. You do NOT rewrite the PFD. You return a structured critique with concrete additions.

---

## Artifact Contract
<INSERT FULL ARTIFACT CONTRACT FROM PHASE 0>

---

## Process Flow Document to challenge
<INSERT FULL PFD FROM PHASE 1>

---

## Attack it on five axes — be specific (name the field / operation, not "improve error handling"):

1. MISSING FAILURE MODES — apply each HAZOP guide word (NO/MORE/LESS/REVERSE/LATE/EARLY/OTHER/PARTIAL) to every external interaction in the PFD. Which guide word produced NO failure_mode entry? Add it.
2. CONTRACT AMBIGUITY — is any AC field underspecified so that "correct" is undefined (a silent input, an unspecified error shape, an undefined return on an edge)? If yes → A-class signal; name the exact ambiguity.
3. INTEGRATION MISMATCH — does the task touch existing code? Which existing helper/function/invariant could this duplicate or break? (This is the #1 under-caught rework class — design review usually misses it.)
4. WEAK INVARIANTS — is any invariant not expressible as a boolean assertion? Any temporal_gap duration vague? Any worker_directive advisory ("should") instead of imperative ("MUST")?
5. VERIFIER BLIND SPOTS — is there a failure_mode with NO corresponding verifier_assertion? Every CRITICAL/HIGH failure mode must be testable.
6. PROTOTYPE BLIND SPOTS — for data/external-system tasks, does `prototype_plan` prove the first lossy transform or only restate the implementation idea? It must compare source-of-truth input against current code/DB behavior before Worker writes code.

## Required output — structured, no prose preamble:

VERDICT: SOUND | GAPS_FOUND | CONTRACT_AMBIGUOUS

ADDITIONS (only if GAPS_FOUND):
- new_failure_modes: [<id, guide_word, trigger, mitigation, category> ...]
- new_worker_directives: [<imperative "MUST..." + rationale> ...]
- new_verifier_assertions: [<assertion + how-to-test + derived_from> ...]
- new_prototype_checks: [<read-only check + command/notebook/script shape + expected comparison> ...]
- invariant_fixes: [<which invariant, how to make it boolean> ...]

CONTRACT_AMBIGUITY (only if CONTRACT_AMBIGUOUS):
- <exact field + what is undefined + what the AC must specify>

Output only the verdict block. Be ruthless but concrete — a vague critique is worse than none.
```

**Additional output contract when `GO_FABLE=1`:**

The Fable Challenge (or degraded replacement reviewer) MUST use this verdict
shape instead of the base `VERDICT` line:

```text
FABLE_CHALLENGE_VERDICT:
  status: SOUND | ADDITIVE_GAPS | DESIGN_REWORK | CONTRACT_AMBIGUOUS
  target_phase: proceed | flow_designer | user
  pfd_revision_required: true | false
  additions:
    new_failure_modes: [...]
    new_worker_directives: [...]
    new_verifier_assertions: [...]
    new_prototype_checks: [...]
    invariant_fixes: [...]
  fable_watchlist:
    - id: FBL-001
      origin: fable-challenge
      severity: HIGH | MED | LOW
      concern: <specific risk>
      close_criteria: <observable closure condition>
      required_evidence: <test/assertion/file:line/prototype evidence>
      target_phase: prototype | worker | verifier | diff_review
  contract_ambiguity:
    - <exact AC field + what is undefined + what must be specified>
```

`SOUND` means no watchlist item is required. `ADDITIVE_GAPS` means the PFD is
structurally sound but must absorb the additions and watchlist. `DESIGN_REWORK`
means the Flow Designer's model is wrong enough that a bullet-point patch would
hide the defect; return to Phase 1 with a Rework Packet. `CONTRACT_AMBIGUOUS`
means pause for user clarification.

**After Challenge returns — Lead reconciles (additive, deterministic):**

- **VERDICT: SOUND** → PFD unchanged. Output `Challenge: SOUND — PFD held.` Proceed to Phase 1C with the original PFD.
- **VERDICT: GAPS_FOUND** → APPEND the agent's `new_failure_modes`, `new_worker_directives`, `new_verifier_assertions`, `new_prototype_checks`, and `invariant_fixes` into the PFD's corresponding sections. **Additive only** — the challenge may ADD requirements, never delete the Flow Designer's. Output `Challenge: GAPS_FOUND — +<a> failure modes, +<b> directives, +<c> assertions, +<d> prototype checks folded into PFD.` Proceed to Phase 1C with the **augmented PFD**.
- **FABLE_CHALLENGE_VERDICT: SOUND** → PFD unchanged except `fable_control.calls_used`/session metadata. Proceed to Phase 1C.
- **FABLE_CHALLENGE_VERDICT: ADDITIVE_GAPS** → APPEND the additions and write every `fable_watchlist` item into `fable_control.watchlist` with `status: OPEN`. Tag every addition with `origin: fable-challenge`. Proceed to Phase 1C with the augmented PFD.
- **FABLE_CHALLENGE_VERDICT: DESIGN_REWORK** → return to Phase 1, not Phase 1C. Build a Rework Packet containing the original AC, prior PFD, the Fable critique, mandatory PFD changes, and current `fable_control.rework_log`; append a log row `{from_phase: 2, to_phase: flow_designer, reason: DESIGN_REWORK}`. Re-run Flow Designer once with this packet. Hard cap: one Fable-triggered design rework before code; a second design-rework request becomes `CONTRACT_AMBIGUOUS` and pauses for the user.
- **VERDICT: CONTRACT_AMBIGUOUS** or **FABLE_CHALLENGE_VERDICT: CONTRACT_AMBIGUOUS** → A-class signal caught at design time (far cheaper than a post-implementation A/W-failure). STOP and surface to the user:
  ```
  /go PAUSED — PFD challenge found the Artifact Contract ambiguous:
    <the ambiguity from the challenge>
  Clarify the AC, then re-run /go. (Catching this now is exactly why the challenge exists.)
  ```
  Then run `python3 ~/.claude/scripts/phase.py progress clear` and remove the .go_active marker:
  ```bash
  rm -f "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.claude/.go_active"
  ```

**Why additive reconciliation preserves the exit-code axiom:** the challenge never produces code and never overrides a test verdict — it only enriches the PFD with more failure modes, stricter assertions, and read-only prototype checks. More verifier_assertions = a stricter acceptance test, and more prototype checks = better pre-code facts; neither can make a defective Worker output more likely to wrongly PASS. The "PASS = test exit code only" axiom is untouched.

---

## Phase 1C — PROTOTYPE GATE (read-only executable proof)

Run: `python3 ~/.claude/scripts/phase.py progress "3/7 prototype_gate"`

The Prototype Gate exists for the failure mode where RECON produces a plausible
story, but the live data path disproves it. The Prototyper turns the PFD's
hypothesis into a read-only executable probe before any Worker can edit the
producer. For broker sync, NAV/TWR, ledger, migrations, external APIs,
concurrency/cache, incident-driven fixes, or any component listed as
`critical: true`, this gate is **mandatory**.

### Prototype applicability decision

Before spawning Worker, Lead classifies the task:

| Task class | Prototype Gate |
|---|---|
| Broker/data sync, DB producer, migration/backfill, ledger/NAV/TWR, financial data, external API integration, concurrency/cache, incident-driven fix, or `critical: true` component | **MUST PASS** |
| Pure local/static transform with no external state, no producer-owned table, no incident context, and no critical component | May be `N/A`, but Lead must print `Prototype Gate: N/A (<specific reason>)` before Phase 2 |

If the gate is mandatory and the PFD lacks `prototype_plan`, pause and return to
Phase 1B Challenge with `CONTRACT_AMBIGUOUS`: the design is not ready for code.

### Prototyper role

Spawn one Prototyper on the same channel selected for hard/read-only analysis
(prefer the provider different from the Flow Designer when available). The
Prototyper may create files only under `notebooks/`, `scripts/probes/`,
`reports/prototypes/`, or a temp directory named in the Artifact Contract. It
must not edit production code, migrations, configs, app modules, or producer
tables.

**Prototyper prompt:**

```
You are a Prototyper agent. Your job is to prove or falsify the PFD's data/process
hypothesis BEFORE any Worker edits production code.

You are read-only with respect to production state:
- NO INSERT/UPDATE/DELETE/MERGE/TRUNCATE/ALTER/DROP.
- NO deploy.
- NO changes to production code paths.
- NO git clean/reset --hard.
- If a notebook is useful, create it under notebooks/ AND create a paired runnable
  .py probe under scripts/probes/ so the proof is reviewable and repeatable.

---

## Artifact Contract
<INSERT FULL ARTIFACT CONTRACT FROM PHASE 0>

---

## Process Flow Document
<INSERT PHASE 1B-AUGMENTED PFD>

---

## Prototype plan from PFD
<INSERT prototype_plan SECTION FROM PFD>

---

## Required output

Produce a Prototype Handoff in markdown with these exact sections:

Prototype verdict: PASS | FAIL | N/A
Artifacts:
- Notebook: <path or none>
- Probe script: <path or none>
- Output report: <path or inline summary>
Source-of-truth inputs:
- <broker/API/file/table + read-only command used>
Current-system comparison:
- <current code/DB path + read-only command used>
First divergence:
- <exact step/key/date/filter/account/transform where counts or values diverge>
Counts and samples:
- <source count, current count, lost/extra count, 3-10 representative rows/keys>
Invariants proven:
- <boolean invariant + evidence>
Worker handoff:
- Facts Worker MUST treat as true
- Hypotheses Worker MUST NOT assume
- Exact files/functions Worker should inspect/change
- Regression assertions Verifier must include

If verdict is FAIL, name the missing access/data/error and stop. Do not guess.
```

### Prototype pass/fail rule

- **PASS** means the probe identified source-of-truth inputs, current-system
  behavior, first divergence, counts/samples, and at least one boolean invariant
  that the Worker can preserve. Proceed to Phase 2 and inject the Prototype
  Handoff into the Worker prompt.
- **FAIL** means the design is not ready for code. Stop before Worker. Output:
  ```
  /go PAUSED — Prototype Gate failed before code:
    <missing data/access/divergence/evidence>
  No Worker spawned. Fix the probe/input access or revise the Artifact Contract.
  ```
  Then clear progress and remove `.go_active`.
- **N/A** is allowed only for the low-risk local/static class above. It must be
  logged in the Phase 4 verdict.

### Role handoff standard

Every role hands over a concrete artifact, not a prose impression:

| From | To | Required payload | Forbidden payload |
|---|---|---|---|
| Lead | Flow Designer | Artifact Contract + Context Receipt + Verified Facts Brief | Unchecked memory/report claims |
| Flow Designer | Challenge | Full PFD including `prototype_plan` and `role_handoff_contract` | Implementation code |
| Challenge | Prototyper | Augmented PFD + additive prototype checks | Deleted/overridden PFD requirements |
| Prototyper | Worker | Prototype Handoff: verdict, artifacts, source/current counts, first divergence, invariants, worker facts | Guesswork, write queries, prod mutations |
| Prototyper | Verifier | Regression assertions derived from proven facts and invariants | Worker's implementation approach |
| Worker | Verifier/Test | Artifact path only; Verifier still uses AC/PFD/prototype assertions, not Worker prompt | Worker's prompt/reasoning |
| Worker/Test | Diff Reviewer | Git diff + AC + PFD + Prototype Handoff + test output | Permission to edit code |
| Fable Challenge | Fable Diff-review | `fable_control.watchlist` + rework_log persisted in the augmented PFD | Hidden Fable chat memory |

This handoff standard is the anti-loop mechanism: a downstream role may build on
evidence, but may not inherit an upstream role's unproven opinion.

Before spawning Worker, run a multiplication check: classify handoff claims as
verified/inferred/assumed, preserve units and sources, name the falsifier, and
ensure Worker and Verifier are not inheriting one unverified shared premise.

---

## Phase 2 — WORKER + VERIFIER (parallel, cross-provider)

Run: `python3 ~/.claude/scripts/phase.py progress "4/7 worker_verifier"`

### Escalation decision (SHIP-4) — single Worker vs /hackathon tournament

Before spawning the Worker, decide whether this task warrants COMPETING implementations. Default is a single Worker (the standard path below). Escalate to a `/hackathon` tournament ONLY when **BOTH** conditions hold:

- **A. High stakes** — the task is in a high-blast-radius class: auth / secrets, DB schema migration, financial DML, concurrency / caching, multi-service contract, or infra config. A subtle wrong choice here is expensive, so a second independent attempt pays for itself.
- **B. Genuine solution uncertainty** — the PFD or the Phase 1B challenge surfaced ≥2 materially different viable approaches, OR ≥1 CRITICAL failure mode whose mitigation is non-obvious. If there is one obvious correct implementation, a tournament just burns 2–3× cost for identical results.

**If NOT both → single Worker** (continue to the standard path below). Most tasks land here — escalation is the exception, gated to protect cost (consilium 2026-06-13, SHIP-4: do NOT escalate by default).

**If both → escalate to `/hackathon`** for the implementation stage:
- Pass the PFD-augmented Artifact Contract and Prototype Handoff as the hackathon Artifact Contract.
- Seed the Judge Mandate from the PFD `verifier_assertions` + `invariants` and Prototype Handoff regression assertions — the deterministic acceptance the Семёрка already derived.
- Spawn the 2–3 candidates ACROSS providers (e.g. one Opus Agent + one Codex `codex_sandbox_worker.sh gpt-5.5`). When `ZAI_API_KEY` is present, include GLM-5.2 via `~/.claude/scripts/zai_cli.py review` for design critique, edge harvest, or external diff review. When Grok CLI is authenticated, include Grok via `~/.claude/scripts/grok_sandbox_worker.sh grok-build` as a write-capable contestant or via `~/.claude/scripts/grok_cli.py review` as a fourth-model reviewer. Z.ai is a third-model review lane by default; Grok may be a code worker only through the sandbox worker; neither should be the deterministic Judge unless the Judge remains an executable test runner with exit-code scoring.
- The hackathon's deterministic Judge (exit-code score, winner-take-all) REPLACES the single cross-provider Verifier for this run — same no-LLM-judgment axiom, stronger evidence. It includes the SHIP-4 **edge-test harvest** (losers' test coverage unioned into the winner's suite; see `hackathon.md` Phase 4).
- When the hackathon returns a winner, **resume the Семёрка at Phase 3B** (diff-review the winner) → Phase 4 verdict. Skip the standard single-Worker path below.
- Log it in the verdict: `implementation: /hackathon (N candidates, winner cN, score X/Y)`.

---

### Standard path — single Worker + cross-provider Verifier

Query the model balancer for the coding tier:
```bash
python3 ~/.claude/scripts/model_balancer.py get coding
```
It returns `{"provider": "<WP>", "model": "<WM>", "reasoning_effort": "<WE>"}` for GPT-5.6 routes. Call these the Worker provider, model, and effort. Fallback if the balancer fails: `WP=anthropic, WM=sonnet`.

### [CRITICAL] SHIP-2 — the Verifier runs on a DIFFERENT provider than the Worker

A model verifying its own output shares its own blind spots — same-provider verification is the correlated-failure mode that the Fable→mono-provider regression introduced (consilium 2026-06-13, SHIP-2). The **Verifier provider `VP` is forced to the OTHER provider:**

| Worker provider `WP` | Verifier provider `VP` | Verifier model |
|----------------------|------------------------|----------------|
| `codex-cli` (today's pinned state) | `anthropic` | `opus` |
| `anthropic` | `codex-cli` | `gpt-5.6-terra` at medium effort |
| `zai-cli` | `anthropic` or `codex-cli` | `opus` preferred, else Terra at medium effort |
| `grok-cli` | `anthropic` or `codex-cli` | `opus` preferred, else Terra at medium effort |

This guarantees Worker and Verifier never share a provider. The Verifier still sees ONLY the AC fields + PFD `verifier_assertions`/`invariants`/`branching_scenarios` (never the Worker's prompt or code) — cross-provider does not relax the knowledge boundary, it hardens it. Z.ai/GLM-5.2 is currently a read-only third-model lane for Challenge, edge-harvest, and Diff-review unless a future audited commit adds a write-capable Z.ai worker. Grok may write code only through `grok_sandbox_worker.sh`, which isolates writes in a git worktree and returns a diff for Lead review.

(The real invariant is provider inequality: Sol, Terra, and Luna are all OpenAI/Codex and never independently verify one another. On Codex CLI the other provider is Claude. The bridge handles the mirror and degrade-and-log fallback.)

### Spawn mechanics by provider

The Agent tool spawns Claude models only; Codex spawns via the sandbox worker, which runs in an isolated git worktree and emits a unified diff on stdout (Lead applies each changed file via Edit/Write, so `dep_guard.py` / `financial_dml_guard.py` / `verify_gate.py` fire on every write).

| Provider | Spawn channel | Background? |
|----------|---------------|-------------|
| `anthropic` | Agent tool, `model: <tier>`, `run_in_background: true` | yes |
| `codex-cli` | Bash `~/.claude/scripts/codex_sandbox_worker.sh <model> < <prompt-file>` → diff on stdout; Lead applies via Edit/Write | no (foreground) |
| `zai-cli` | Bash `~/.claude/scripts/zai_cli.py review` → read-only analysis; no diff output | no (foreground) |
| `grok-cli` | Bash `~/.claude/scripts/grok_sandbox_worker.sh <model> < <prompt-file>` → diff on stdout; Lead applies via Edit/Write, or `grok_cli.py review` for read-only analysis | no (foreground) |

**Preserve parallelism — spawn the anthropic side as a background Agent FIRST, then run the codex side foreground** (the Agent runs concurrently in the background while Codex executes in its worktree). Because `VP` is forced to differ from `WP`, exactly one side is anthropic and one is codex-cli — never two foreground Bash calls, never two Agents.

- **Today (`WP=codex-cli`):** (1) spawn the **Verifier** as a background Opus Agent (`model: "opus"`, `run_in_background: true`); (2) run the **Worker** via `CLAUDE_BOOSTER_TASK_CATEGORY=coding ~/.claude/scripts/codex_sandbox_worker.sh "$WM" < worker_prompt.txt` (the `coding` prefix tags this Codex call's telemetry as the `coding` tier — debt #1), capture the diff, apply each changed file via Edit/Write; (3) collect the Verifier's test path when it returns.
- **If `WP=anthropic`:** spawn the Worker as a background Agent, then run the Verifier via `CODEX_REASONING_EFFORT=medium codex_sandbox_worker.sh gpt-5.6-terra < verifier_prompt.txt`.
- **If `WP=grok-cli`:** (1) spawn the **Verifier** as a background Opus Agent when available, else Codex; (2) run the **Worker** via `CLAUDE_BOOSTER_TASK_CATEGORY=coding ~/.claude/scripts/grok_sandbox_worker.sh "$WM" < worker_prompt.txt`, capture the diff, apply each changed file via Edit/Write; (3) collect the Verifier result.

The Worker and Verifier **prompts are identical regardless of provider** — only the spawn channel differs. Use the prompt blocks below verbatim.

**Degradation (cross-provider is a quality optimization, NOT a safety gate):** if the required other-provider channel is unavailable (e.g. the `codex` binary is missing, Codex auth fails, or `ZAI_API_KEY` is absent), fall back to another available provider or a same-provider Verifier on the Agent tool and **log the degradation** in the Phase 4 verdict line (`cross-provider: DEGRADED — Verifier on same provider as Worker (<reason>)`). Do NOT wedge the pipeline over it — a same-provider test is weaker than cross-provider but still far better than no test.

---

### Worker agent prompt:

```
You are a Worker agent. Implement the following task.

This is a delegated implementation task. Do the work — write the code, make the changes,
produce the artifact at the specified path. Do not explain plans. Do not ask for confirmation.

---

## Artifact Contract

<INSERT FULL ARTIFACT CONTRACT FROM PHASE 0>

---

## Process Flow Document (PFD)

<INSERT FULL PFD — the Phase 1B-augmented version if the challenge returned GAPS_FOUND, else the Phase 1 original>

When `GO_FABLE=1`, this PFD includes `fable_control.watchlist`. Treat every
watchlist item assigned to `worker` as a MUST-level directive and preserve the
evidence needed to close it later.

---

## Prototype Handoff

<INSERT Prototype Handoff from Phase 1C, or `Prototype Gate: N/A (<reason>)`>

The Prototype Handoff is executable evidence. Treat its proven facts as input
guards. Do not re-assume a theory that the probe falsified.

---

## [CRITICAL] Worker directives from PFD

These are imperative requirements, not suggestions. Implement EVERY directive:

<INSERT worker_directives SECTION FROM PFD>

These directives exist because the Flow Designer identified specific failure modes that
flat/happy-path implementation would miss. Skipping any directive = leaving a known defect.

---

## Deliverable

Implement the task. Write the result to the artifact path specified in the Artifact Contract.
If the artifact path is a code file, write the complete implementation.
If it is a directory, produce all required files within it.

Do not write a test — that is the Verifier's job.
```

---

### Verifier agent prompt:

```
You are a Verifier agent. Write an executable acceptance test for the following task.

You are independent. You have NOT seen the Worker's implementation.
You do NOT know how the Worker chose to implement anything.
Your job is to test observable behavior — what an external observer sees —
against the contract and the failure modes identified by the Flow Designer.

---

## Artifact Contract (what you are testing against)

Objective: <INSERT Objective FROM AC>
Artifact path: <INSERT Artifact path FROM AC>
Expected observable behavior: <INSERT Expected observable behavior FROM AC>
Acceptance emphasis: <INSERT Acceptance emphasis FROM AC>

---

## From the PFD: what to test

### Verifier assertions
<INSERT verifier_assertions SECTION FROM PFD ONLY>

### Fable watchlist assertions (only when GO_FABLE=1)
<INSERT fable_control.watchlist items whose target_phase is verifier or whose required_evidence is a test/assertion>

### Prototype regression assertions
<INSERT regression assertions from Prototype Handoff, if any>

### Invariants (must hold after execution)
<INSERT invariants SECTION FROM PFD ONLY>

### Branching scenarios (cover at least one non-happy-path branch)
<INSERT branching_scenarios SECTION FROM PFD ONLY>

---

## Test script requirements

Write a bash or python test script that:

1. Tests each verifier_assertion from the PFD — one assertion = one labeled test case
2. Checks each invariant holds after execution — assert the boolean expression or its observable proxy
3. Covers at least one non-happy-path branch from branching_scenarios — inject the failure condition and assert correct handling
4. Includes every Prototype Handoff regression assertion as a labeled test case when Phase 1C returned PASS
5. Outputs clear PASS/FAIL per test case:
   ```
   [PASS] assertion: <description>
   [FAIL] assertion: <description> — expected <X>, got <Y>
   ```
6. Prints a summary at the end:
   ```
   Results: <N> passed, <M> failed
   ```
7. Exits with code 0 if ALL assertions pass, non-zero if ANY fail

Save the test script to: `<artifact_path_dir>/test_<artifact_name>.sh`
(or `.py` if Python is more natural for the assertion logic)

Where `<artifact_path_dir>` = directory containing the artifact path from the AC,
and `<artifact_name>` = basename of the artifact without extension.

Do not modify the Worker's artifact. Do not implement any feature logic.
Test only. Read, run, assert, report.
```

---

### Progress output — the Семёрка bar (7 stages, 7 segments)

The pipeline has SEVEN stages, so the status bar has seven segments. Emit the matching line as each stage completes (fill one segment per phase):
```
Семёрка ▰▱▱▱▱▱▱ 1/7 · Flow Designer ✓
Семёрка ▰▰▱▱▱▱▱ 2/7 · Challenge ✓
Семёрка ▰▰▰▱▱▱▱ 3/7 · Prototype Gate ✓
Семёрка ▰▰▰▰▱▱▱ 4/7 · Worker ✓ · Verifier ✓
Семёрка ▰▰▰▰▰▱▱ 5/7 · Test ✓
Семёрка ▰▰▰▰▰▰▱ 6/7 · Diff review ✓
Семёрка ▰▰▰▰▰▰▰ 7/7 · Verdict ✓
```
If a stage is skipped or degraded, annotate that segment instead of dropping it — e.g. `6/7 · Diff review SKIPPED (trivial diff)`, `3/7 · Prototype Gate N/A (local static transform)`, or `4/7 · cross-provider DEGRADED`. The bar always shows all seven segments so the reader sees the whole pipeline.

Do NOT begin Phase 3 until BOTH Worker and Verifier have returned.

---

## Phase 3 — TEST RUN

Run: `python3 ~/.claude/scripts/phase.py progress "5/7 test_run"`

After both agents complete:

1. Read the Verifier's test script from the path it wrote (the `test_<artifact_name>.sh` or `.py` file).
2. Run it:
   ```bash
   bash <test_script_path>
   ```
   (Use `python3 <test_script_path>` if the file is `.py`)
3. Capture the full stdout + stderr output and the exit code.
4. Output:
   ```
   Test result: exit=<N>
   <stdout/stderr output>
   ```

**Do NOT skip this phase.** "The code looks correct" is not a substitute for an executable test.

---

## Phase 3B — POST-IMPLEMENTATION DIFF REVIEW (cross-provider, only on PASS)

**Run only if Phase 3 returned exit=0.** If the test failed, skip straight to Phase 4 fail-classification — there is nothing to review yet.

Run: `python3 ~/.claude/scripts/phase.py progress "6/7 diff_review"` (or
`"6/7 diff_review(fable)"` when `GO_FABLE=1`)

The Verifier tested *observable behavior* but never saw the code. This phase gives the **diff itself** a second look by a different-provider reviewer — to catch defects that emerge at implementation time and that a behavioral test does not exercise: integration breakage, reinvented helpers, security holes, dead/over-broad churn. Per consilium 2026-06-13 (SHIP-3): design-time review cannot see these — they live in the written code.

**Skip criteria (log the skip in the verdict):** the diff is trivial — docs/comments only, or < ~15 changed lines with no logic / control-flow / IO. Otherwise the review runs.

**Provider rule:** the reviewer MUST run on a different provider than the Worker (it reads the Worker's code, so it must not be the author's own model). Prefer GLM-5.2 as a third-model reviewer when `ZAI_API_KEY` is present and the Worker is not `zai-cli`; otherwise use the same mapping as the Verifier:
- **If `GO_FABLE=1` and the budget gate allows it**: run exactly ONE read-only
  Fable Diff-review. The prompt MUST include only the AC, Prototype Handoff,
  test output, final diff or watchlist-oriented diff slices, and
  `fable_control.watchlist`. This consumes the second and final Fable call.
  If Fable is unavailable, weekly usage is `>=80%`, or a Fable call was already
  consumed by a retry/recheck, set `fable_control.degraded=true`, record
  `downgrade_reason`, and run the normal reviewer below against the same
  watchlist contract.
- `WP=codex-cli` → reviewer = Opus **Agent** (`model: "opus"`), read-only.
- `WP=anthropic` → reviewer = GLM-5.2 when available, else `CLAUDE_BOOSTER_TASK_CATEGORY=hard CODEX_REASONING_EFFORT=medium ~/.claude/scripts/codex_worker.sh gpt-5.6-sol < review_prompt.txt`.
- `WP=zai-cli` → reviewer = Opus Agent preferred, else Codex.
- `WP=grok-cli` → reviewer = GLM-5.2 via `~/.claude/scripts/zai_cli.py review` when available, else Opus Agent/Codex.

Collect the diff first: `git -C "$(git rev-parse --show-toplevel)" diff -- <changed paths>` (or read the files the Worker wrote).

**Reviewer prompt:**

```
You are a Post-Implementation Diff Reviewer. The code below already PASSED its acceptance test. Give the DIFF a different-provider second look for defects a behavioral test cannot catch. You do NOT write or rewrite code — you return structured findings only.

## Artifact Contract
<INSERT FULL ARTIFACT CONTRACT FROM PHASE 0>

## Prototype Handoff
<INSERT Prototype Handoff from Phase 1C, or Prototype Gate N/A reason>

## Fable Control (only when GO_FABLE=1)
<INSERT fable_control SECTION FROM AUGMENTED PFD, especially fable_watchlist and rework_log>

## Diff under review
<INSERT git diff OF THE WORKER'S CHANGES>

## Review on four axes — be concrete, cite file:line:
1. INTEGRATION — does this break a caller, change a depended-on signature/contract, or REINVENT an existing helper/function? (The #1 emergent defect; design review can't see it.)
2. MINIMALITY — unnecessary churn, dead code, a broad refactor where a small patch would do, unrelated formatting. Smaller diff = lower regression risk.
3. SECURITY — injection, secrets in code, unsafe path/SQL/shell construction, missing validation at a boundary, auth/permission gaps.
4. UNTESTED BEHAVIOR — a code path the acceptance test clearly does not exercise that could fail in production (error branch, edge input, resource cleanup, partial failure).

## Output — structured, no preamble:
VERDICT: CLEAN | FINDINGS
FINDINGS (each):
- severity: HIGH | MED | LOW
- axis: integration | minimality | security | untested
- location: <file:line>
- issue: <what is wrong, concretely>
- fix_directive: <imperative "MUST ..." the Worker can act on>

Only HIGH findings block. A vague finding is noise — omit it.
```

**Additional output contract when `GO_FABLE=1`:**

```text
FABLE_DIFF_REVIEW_VERDICT:
  status: PASS | REWORK_REQUIRED | CONTRACT_AMBIGUOUS
  watchlist_closure:
    - id: FBL-001
      status: CLOSED | OPEN | PARTIAL
      evidence: <file:line, test name, prototype fact, or missing evidence>
      target_phase_if_open: worker | verifier | prototype | flow_designer | user
  findings:
    - severity: HIGH | MED | LOW
      class: implementation | verifier_gap | prototype_gap | design_gap | contract_gap
      target_phase: worker | verifier | prototype_gate | flow_designer | user
      fix_directive: <imperative directive>
```

Fable Diff-review must close the watchlist item-by-item. It does not get a
free-form veto and it does not mark PASS. It either proves each item closed
with evidence, or routes a typed rework request to the correct phase.

**Lead reconciliation:**
- **VERDICT CLEAN, or only MED/LOW findings** → review passes. Log MED/LOW in the Phase 4 verdict line (advisory — surface them, do not silently drop, do not auto-fix). Proceed to Phase 4 PASS.
- **Any HIGH finding** → **R-failure**: respawn the Worker on the same provider `WP` with the HIGH `fix_directive`s appended to its prompt, plus the failed-attempt session context (`session_context.py --agent "<Worker desc>" --no-thinking`). Then **re-run Phase 3 (the SAME Verifier test — it MUST stay green)** and **re-run this Phase 3B review**. R counts toward the 3-retry cap (shared with V/W). The reviewer never edits code — only the Worker does, and the unchanged test still gates.
- **FABLE_DIFF_REVIEW_VERDICT: PASS** → mark every closed item in
  `fable_control.watchlist` with `status: CLOSED` and `closure_evidence`. Proceed
  to Phase 4 PASS if the normal review also has no blocking HIGH finding.
- **FABLE_DIFF_REVIEW_VERDICT: REWORK_REQUIRED** → route by `target_phase` /
  `target_phase_if_open`:
  - `implementation` / `worker` → R-failure; respawn Worker, re-run Phase 3,
    then re-run diff-review with the **normal reviewer** using the persisted
    watchlist. Do not spend a third Fable call.
  - `verifier_gap` / `verifier` → V-failure; respawn Verifier to add/repair the
    missing assertion, then re-run Phase 3. Do not respawn Worker unless the new
    test fails against the implementation.
  - `prototype_gap` / `prototype_gate` → return to Phase 1C Prototype Gate with
    the missing read-only proof added to the prototype plan.
  - `design_gap` / `flow_designer` → return to Phase 1 with a Rework Packet and
    append `{from_phase: 6, to_phase: flow_designer, reason: DESIGN_REWORK}` to
    `fable_control.rework_log`. Hard cap: one post-implementation design rework;
    a second becomes `CONTRACT_AMBIGUOUS`.
  - `contract_gap` / `user` → pause for user clarification.
- **FABLE_DIFF_REVIEW_VERDICT: CONTRACT_AMBIGUOUS** → pause for user clarification.

**Why this preserves the axiom:** the reviewer produces findings, never code, and never overrides the test verdict. A HIGH finding routes to a Worker fix that must still pass the unchanged Verifier test — so PASS stays "exit code of the test", never "the reviewer approved it".

---

## Phase 4 — VERDICT

Run: `python3 ~/.claude/scripts/phase.py progress "7/7 verdict"`

### If exit=0 (ALL PASS) AND Phase 3B review cleared (CLEAN, or only MED/LOW):

Before emitting the verdict, confirm evidence is newer than the last relevant
edit, obtain an independent check for high-risk claims, state the attempted
falsifier, and record residual risk plus downstream/integration status. These are
mandatory provenance checks; they never override the exit-code-only PASS rule.

```
✓ PASS — Семёрка ▰▰▰▰▰▰▰ 7/7 complete. Artifact at <artifact_path>.
```
Append any of these that apply (honest status, not silent drop):
- `prototype gate: <PASS | N/A (<reason>)>`
- `diff review: <CLEAN | N MED/LOW advisory findings — list them as follow-ups | SKIPPED (trivial diff)>`
- `fable control: <off | PASS (N/N watchlist items closed, calls_used X/2) | DEGRADED (<reason>) | REWORK routed to <phase>>`
- `cross-provider: <OK | DEGRADED (<reason>)>` (if the Verifier or reviewer fell back to same-provider in Phase 2/3B)

**Record the KPI outcome (SHIP instrumentation — proves the pipeline reduces rework):**
```bash
python3 ~/.claude/scripts/kpi_rework.py record \
  --task "<short Objective>" --outcome pass \
  --worker-spawns <1 + number of W/R Worker re-spawns> \
  --verifier-fails <number of test-fail cycles that occurred: V + W + R retries> \
  [--category <defect>:<count> for each classified retry]
```
First-pass-clean run → `--worker-spawns 1 --verifier-fails 0` (no `--category`). For each retry that happened, tag the defect category (W/V/A/R → category): A-failure → `contract_ambiguity`; V-failure → `weak_verification`; W-failure → `missed_failure_mode` (or `integration_mismatch` / `capability` if that fits better); R-failure (Phase 3B HIGH) → `integration_mismatch` (or the finding's axis). A CONTRACT_AMBIGUOUS caught at Phase 1B and resolved pre-code is NOT a retry — it is prevented rework, so it does not count toward `verifier-fails`.

Run: `python3 ~/.claude/scripts/phase.py progress clear`

Capture the run tag, then remove the .go_active marker (the pipeline is now complete):
```bash
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
RUNTAG="$(cat "$ROOT/.claude/.go_active" 2>/dev/null)"   # the tag written at Phase 0
rm -f "$ROOT/.claude/.go_active"
```

### Post-pipeline — surface THIS run's findings (auto-fix is OPT-IN)

RECON-as-review already harvested this run's `adjacent_findings` into scoped debts (origin `$RUNTAG`) back in Phase 1 — that collection always happens and nothing is lost. What this step decides is whether to **auto-fix** them now.

**Default — SURFACE only, do NOT auto-fix.** Auto-fixing was made opt-in deliberately: an always-on auto-fix balloons a small `/go` and the disable-flag would be forgotten. So by default, just point at the findings:
```bash
# count this run's scoped open debts (origin == RUNTAG); print a one-line pointer:
echo "Семёрка: logged <N> findings for this run [origin $RUNTAG] (<M> in-radius HIGH/MED). To auto-fix the in-radius ones now: /debt auto --scope \"$RUNTAG\"   ·   to review all: /debt list"
```
The findings stay in `.session_debts.json` (visible via `/debt list`), scoped to `$RUNTAG`. The user fixes them whenever they want — `/debt auto --scope "$RUNTAG"` (auto-fix in-radius HIGH/MED, surface adjacent+LOW) or `/debt work <N>` individually.

**Opt-in — auto-fix in-radius now.** Run the scoped clear automatically ONLY if BOTH:
- env `CLAUDE_BOOSTER_POST_GO_AUTOFIX=1` is set (the user opted into post-`/go` auto-fixing), AND
- the marker `<project>/.claude/.debt_auto_active` does NOT exist (recursion guard — this `/go` was NOT spawned by `/debt auto`; otherwise auto-fixing again would recurse).

```bash
# auto-fix gate:
if [ "${CLAUDE_BOOSTER_POST_GO_AUTOFIX:-0}" = "1" ] && [ ! -f "$ROOT/.claude/.debt_auto_active" ]; then
  echo "RUN /debt auto --scope $RUNTAG (opt-in autofix)"
else
  echo "SURFACE only (default — autofix opt-in via CLAUDE_BOOSTER_POST_GO_AUTOFIX=1)"
fi
```
If the gate says RUN → invoke `/debt auto --scope "$RUNTAG"`. Otherwise print the one-line pointer above and finish.

Done.

---

### If exit≠0 (FAIL):

Classify the failure using this decision tree. Read the test output carefully before classifying.

| Question | If YES → |
|----------|----------|
| Does the test assert something NOT stated in the AC or PFD (Verifier overstepped)? | **V-failure** |
| Does the test assert something IN the AC/PFD, but Worker didn't implement it? | **W-failure** |
| Is the AC ambiguous — "correct" is undefined or contradictory? | **A-failure** |
| Is it environment: wrong path, missing dependency, permission error, runtime not available? | **E-failure** |

**V-failure (Verifier overstepped):**
Respawn Verifier on the **same provider channel `VP`** as Phase 2 (cross-provider invariant holds across retries — the Verifier stays on the opposite provider from the Worker), with the same prompt, plus:
```
CORRECTION: Your previous test was rejected (V-failure) because it asserted:
  <specific assertion that overstepped>
This is NOT in the Artifact Contract or PFD. Remove it. Test ONLY what is in
the AC's "Expected observable behavior" and "Acceptance emphasis", and the PFD's
"verifier_assertions" and "invariants". Rewrite the test script.
```

**W-failure (Worker missed a requirement):**
Inject the failed Worker's session context:
```bash
python3 ~/.claude/scripts/session_context.py --agent "<Worker agent description>" --no-thinking
```
Respawn Worker on the **same provider channel `WP`** as Phase 2 (Verifier stays on `VP`, the opposite provider), with the same prompt, plus:
```
CORRECTION: Your implementation failed the Verifier's test (W-failure).

Failed test output:
<paste test stdout/stderr>

Failed assertions:
<list the [FAIL] lines from test output>

Session context from your previous attempt:
<INSERT session_context output>

Fix the implementation. The Verifier's test is the ground truth — do not argue with it.
Implement what it checks.
```

**A-failure (AC ambiguous):**
Stop retry. Output:
```
/go BLOCKED — A-failure: Artifact Contract is ambiguous.

The Verifier's test failed because the AC does not clearly define correct behavior for:
  <specific aspect>

Fix the AC to specify:
  <what needs to be clarified>

Then re-run /go from Phase 1 with the corrected AC.
```
Run: `python3 ~/.claude/scripts/phase.py progress clear`

Remove the .go_active marker:
```bash
rm -f "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.claude/.go_active"
```

**E-failure (environment issue):**
Fix the environment issue (install missing dep, correct path, fix permissions).
Then re-run Phase 3 only — do NOT respawn Worker or Verifier.

**Retry cap:**
Hard cap: 3 retries total across all categories (V + W + R combined; A and E do not count as retries). R-failures (Phase 3B review HIGH findings) share this budget — a task cannot loop forever between Worker fix and diff review.

After 3 retries, STOP:
```
/go FAILED — 3 retries exhausted.

Attempt history:
  1. <classification> — <what was tried>
  2. <classification> — <what was tried>
  3. <classification> — <what was tried>

Aggregated failure:
<final test output>

Recommended next action: <specific concrete next step — not a question>
```

**Record the KPI outcome (failed run still counts — it is the rework signal):**
```bash
python3 ~/.claude/scripts/kpi_rework.py record \
  --task "<short Objective>" --outcome fail_exhausted \
  --worker-spawns <total Worker spawns> --verifier-fails <total test-fail cycles> \
  [--category <defect>:<count> for each classified retry]
```

Run: `python3 ~/.claude/scripts/phase.py progress clear`

Remove the .go_active marker:
```bash
rm -f "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.claude/.go_active"
```

On retry, always include the failed agent's session context (via `session_context.py`) in the new agent's brief so it sees what the predecessor tried and where it got stuck — not Lead's summary.

---

## [CRITICAL] Non-negotiable constraints

1. **ALL FIVE role stages MUST run or explicitly gate: Flow Designer → Challenge → Prototype Gate → Worker + Verifier.** There is no silent "skip" path inside `/go`.
   If the task is trivial enough to skip Flow Designer, do NOT use `/go` — edit directly.
   Prototype Gate may be `N/A` only for pure local/static tasks with no external state, no producer-owned table, no incident context, and no critical component. Broker/data/DB/financial/migration/external-system tasks require Prototype PASS before Worker.
   (Under SHIP-4 escalation, the Worker+Verifier stage is REPLACED by a `/hackathon` tournament — competing candidates + a deterministic Judge — but Flow Designer, Challenge, and Prototype Gate still precede it, and a test still gates the result. The roles never collapse; only the implementation stage's shape changes.)

2. **Flow Designer → Challenge → Prototype Gate → Worker + Verifier is a strict order.**
   PFD is an INPUT to the Challenge; the (possibly augmented) PFD is an INPUT to the Prototype Gate; the Prototype Handoff is an INPUT to both Worker and Verifier. Spawning Worker or Verifier before the Challenge reconciles and Prototype Gate passes/N/A logs = protocol violation.

   **The Challenge MUST run on a different provider than the Flow Designer.** A model cannot find its own blind spots — same-provider "review" is theater. If `get hard` returned a non-anthropic provider, the Challenge is an Opus Agent; if it returned anthropic, the Challenge runs via `codex_worker.sh gpt-5.5`. The Challenge is additive (may add failure modes / directives / assertions, never delete them) and produces NO code — so the exit-code-only PASS axiom is preserved.

3. **Verifier MUST NOT see Worker's prompt or implementation approach.**
   The Verifier's prompt contains ONLY: AC fields (Objective, Artifact path, Expected observable behavior, Acceptance emphasis) + PFD sections (verifier_assertions, invariants, branching_scenarios). Nothing else.
   Independence is the mechanism that prevents self-evaluation bias.

   **The Verifier MUST run on a different provider than the Worker (SHIP-2).** Same-provider verification shares the Worker's blind spots — that is the correlated-failure mode the mono-provider regression introduced. `WP=codex-cli → Verifier=Opus`; `WP=anthropic → Verifier=codex gpt-5.5`. Exactly one of {Worker, Verifier} is anthropic and one is codex-cli. Cross-provider HARDENS the knowledge boundary (it never relaxes it). If the other-provider channel is unavailable, degrade to same-provider and LOG it — this is a quality optimization, not a safety gate, so it must not wedge the pipeline.

4. **Lead MUST NOT evaluate Worker's code quality subjectively.**
   Exit code from Verifier's test = the ONLY verdict mechanism.
   "The code looks correct to me" is never a reason to mark PASS.

5. **Lead MUST run the Verifier's test (Phase 3).**
   Do NOT skip Phase 3. Do NOT infer pass/fail from reading the code.
   The test runs, or the pipeline is incomplete.

6. **`/go fable` MUST keep Fable as Quality Chair, not Lead.**
   Fable may challenge the PFD and close the final `fable_watchlist`; it may
   route typed rework requests to Flow Designer, Prototype, Worker, Verifier, or
   the user. Fable MUST NOT write code, write tests, spawn workers, change
   `model_balancer.json`, mark final PASS, or participate in debugging/polling
   loops. Context between Fable calls MUST be carried by `fable_control` in the
   PFD, not by hidden session memory. Hard cap: two Fable calls per run. If the
   cap, availability, or `>=80%` weekly-usage budget gate blocks Fable, the same
   watchlist contract runs on the normal review lane and the verdict records
   `fable control: DEGRADED`.

7. **Phase 3B diff review (SHIP-3) runs on PASS, on a different provider than the Worker, and produces findings — never code.**
   It is conditional (skipped for a trivial diff, logged) and gated (only after the test is green). A HIGH finding routes to a Worker fix that must re-pass the SAME unchanged test (R-failure, counts toward the retry cap). The reviewer never edits code and never overrides the test verdict — PASS stays "test exit code", never "reviewer approved".
