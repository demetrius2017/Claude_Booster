---
description: "Execute тройка (Flow Designer → Worker + Verifier) — hardcoded, non-skippable pipeline."
argument-hint: "<Artifact Contract — structured text with Objective, Verified Facts, etc.>"
---

## Progress tracking
Before each numbered step below, run: `python3 ~/.claude/scripts/phase.py progress "<N>/4 <step_label>"`
After the final step completes, run: `python3 ~/.claude/scripts/phase.py progress clear`

Steps: `1/4 flow_designer`, `2/4 worker_verifier`, `3/4 test_run`, `4/4 verdict`

---

## Phase 0 — AC VALIDATION (Lead, mandatory before any agent spawns)

Parse `$ARGUMENTS` as the Artifact Contract. It may also be formulated in the preceding conversation context if the user said "run /go" after building the AC together.

Check that the Artifact Contract contains ALL of these mandatory fields:

| Field | Description |
|-------|-------------|
| `Objective:` | One sentence — what the system should do after this change |
| `Verified Facts Brief:` | Current state evidence with file:line references (NOT docs or memory alone) |
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

**If AC is complete:** write the .go_active marker so the go_gate hook allows Agent spawns during this pipeline run:
```bash
touch "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.claude/.go_active"
```
Then proceed to Phase 1.

---

## Phase 1 — FLOW DESIGNER

Run: `python3 ~/.claude/scripts/phase.py progress "1/4 flow_designer"`

Query the model balancer:
```bash
python3 ~/.claude/scripts/model_balancer.py get hard
```

Use the returned model for the Flow Designer agent. Fallback if balancer fails: `model: "opus"`.

Spawn ONE Flow Designer agent. **NOT `run_in_background`** — Lead waits for the result before Phase 2.

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
- `verifier_assertions` — assertion (what to test), type (temporal/branching/invariant/freshness/cascade), how (concrete test approach), derived_from (failure_mode ID or invariant)
- `branch_tree.mermaid` — visual graph of operations and outcomes (all non-success terminals shown)

Quality criteria — your PFD FAILS internal review if:
- Any operation has only one outcome (happy path only)
- Any temporal_gap has vague duration ("some time", "eventually") — MUST be quantified
- Any invariant is not expressible as a boolean assertion
- Any worker_directive is advisory ("should", "consider") instead of imperative ("MUST")
- The branch_tree shows no non-success terminal states
- The failure_modes list is empty

Output ONLY the YAML PFD. No prose before or after.
```

**After Flow Designer returns:**

Extract from the PFD:
- Count of `failure_modes` entries → `<N>`
- Count of `worker_directives` entries → `<M>`
- Count of `verifier_assertions` entries → `<K>`

Output:
```
Flow Designer complete. PFD: <N> failure modes, <M> worker directives, <K> verifier assertions.
```

Save the full PFD text for Phase 2.

---

## Phase 2 — WORKER + VERIFIER (parallel)

Run: `python3 ~/.claude/scripts/phase.py progress "2/4 worker_verifier"`

Query model balancer for coding tier:
```bash
python3 ~/.claude/scripts/model_balancer.py get coding
```

Use the returned model for BOTH agents. Fallback if balancer fails: `model: "sonnet"`.

**Spawn BOTH agents in ONE message as parallel tool calls, both with `run_in_background: true`.**

Do not wait for one before spawning the other. They run simultaneously.

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

<INSERT FULL PFD FROM PHASE 1>

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
4. Outputs clear PASS/FAIL per test case:
   ```
   [PASS] assertion: <description>
   [FAIL] assertion: <description> — expected <X>, got <Y>
   ```
5. Prints a summary at the end:
   ```
   Results: <N> passed, <M> failed
   ```
6. Exits with code 0 if ALL assertions pass, non-zero if ANY fail

Save the test script to: `<artifact_path_dir>/test_<artifact_name>.sh`
(or `.py` if Python is more natural for the assertion logic)

Where `<artifact_path_dir>` = directory containing the artifact path from the AC,
and `<artifact_name>` = basename of the artifact without extension.

Do not modify the Worker's artifact. Do not implement any feature logic.
Test only. Read, run, assert, report.
```

---

### Progress output

Output as each agent completes:
```
Тройка ▰▱▱ 1/2 · Flow Designer ✓
Тройка ▰▰▱ 2/2 · Worker ✓ · Verifier ✓
```

Do NOT begin Phase 3 until BOTH Worker and Verifier have returned.

---

## Phase 3 — TEST RUN

Run: `python3 ~/.claude/scripts/phase.py progress "3/4 test_run"`

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

## Phase 4 — VERDICT

Run: `python3 ~/.claude/scripts/phase.py progress "4/4 verdict"`

### If exit=0 (ALL PASS):

```
✓ PASS — тройка complete. Artifact at <artifact_path>.
```

Run: `python3 ~/.claude/scripts/phase.py progress clear`

Remove the .go_active marker (absolute last action):
```bash
rm -f "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.claude/.go_active"
```

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
Respawn Verifier with the same prompt as Phase 2, plus:
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
Respawn Worker with the same Phase 2 prompt, plus:
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
Hard cap: 3 retries total across all categories (V + W combined; A and E do not count as retries).

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
Run: `python3 ~/.claude/scripts/phase.py progress clear`

Remove the .go_active marker:
```bash
rm -f "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.claude/.go_active"
```

On retry, always include the failed agent's session context (via `session_context.py`) in the new agent's brief so it sees what the predecessor tried and where it got stuck — not Lead's summary.

---

## [CRITICAL] Non-negotiable constraints

1. **ALL THREE agents MUST spawn.** There is no "skip Flow Designer" path inside `/go`.
   If the task is trivial enough to skip Flow Designer, do NOT use `/go` — edit directly.

2. **Flow Designer MUST complete BEFORE Worker + Verifier spawn.**
   PFD is an INPUT to both. Spawning Worker or Verifier without a PFD = protocol violation.

3. **Verifier MUST NOT see Worker's prompt or implementation approach.**
   The Verifier's prompt contains ONLY: AC fields (Objective, Artifact path, Expected observable behavior, Acceptance emphasis) + PFD sections (verifier_assertions, invariants, branching_scenarios). Nothing else.
   Independence is the mechanism that prevents self-evaluation bias.

4. **Lead MUST NOT evaluate Worker's code quality subjectively.**
   Exit code from Verifier's test = the ONLY verdict mechanism.
   "The code looks correct to me" is never a reason to mark PASS.

5. **Lead MUST run the Verifier's test (Phase 3).**
   Do NOT skip Phase 3. Do NOT infer pass/fail from reading the code.
   The test runs, or the pipeline is incomplete.
