---
type: consilium
date: 2026-07-10
project: Claude_Booster
topic: preventing the edit loop (regression loop) — backward analysis in the /go pipeline
preserve: true
---

# Consilium 2026-07-10 — Preventing the Edit Loop: Backward Analysis in `/go`

## Task context

Dmitry's framing: *"I need Lead and the models to think about NOT creating an edit loop — where
fixing one thing breaks another. They must read the PRE-HISTORY: why was this diff introduced
earlier, what were we fighting, and what happens if we now fix A and get problems B, C, D."*

Lead's hypothesis put to the panel: add a **fourth, backward-facing lens** ("archaeology lens",
Chesterton's Fence) before the Flow Designer, emitting regression invariants into the PFD.

The panel was explicitly instructed that disagreeing with the Lead's hypothesis was a valid and
welcome answer.

## Panel

| # | Perspective | Provider / model | Slot |
|---|---|---|---|
| A1 | Pipeline architect | codex-cli / gpt-5.5 | `consilium_bio` |
| A2 | Git forensics / SRE incident engineer | codex-cli / gpt-5.5 | `consilium_bio` |
| A3 | Verification engineer (exit-code purist) | codex-cli / gpt-5.5 | `consilium_bio` |
| A4 | Anti-ceremony skeptic / cost realist | codex-cli / gpt-5.5 | `consilium_bio` |
| X1 | External reviewer | zai-cli / glm-5.2 | **DEGRADED** — three attempts, `API Error 529 [1305] service temporarily overloaded`. Slot not substituted. |
| X2 | External reviewer | grok-cli / grok-composer-2.5-fast | `audit_tertiary` |
| X3 | External reviewer (with read access to the live system) | anthropic / opus-4.8 | cross-provider verification |

Provider diversity achieved: OpenAI (×5), xAI (×1), Anthropic (×1). Z.ai unavailable.

---

## [CRITICAL] Corrections to the Lead's Verified Facts Brief

X3 (Opus) was given read access to the live system and instructed to verify the brief rather
than trust it. It found **two factual errors and one overstatement in the Lead's own brief**.
The Lead independently re-verified all three. All are confirmed. They materially change the
recommendation.

### C1 — `docs/dep_manifest.json` EXISTS. Brief fact F5 was wrong.

```
$ ls -la docs/dep_manifest.json
-rw-r--r-- 1 dmitrijnazarov staff 35326 Jul 2 17:57 docs/dep_manifest.json
```

The Lead asserted its absence in the `/start` Context Receipt without checking — the exact
failure mode `rules/code-over-docs.md` exists to prevent. **Consequence for the argument:** the
"missing dependency manifest" hypothesis is not merely unproven, it is *falsified for this repo*.
The manifest is present and `integration_mismatch` still occurs. A dependency map that never
reaches the Verifier's executable test does not prevent the defect. This kills the cheapest
competing explanation and, paradoxically, strengthens the case that the missing link is
**executable regression obligations**, not more context documents.

### C2 — `defect_categories` are LLM self-labels, not measurements.

`commands/go.md:895` instructs the **Lead** to type `--category <defect>:<count>` by hand at
Phase 4, from a default map (A→`contract_ambiguity`, V→`weak_verification`, W→`missed_failure_mode`
*"or `integration_mismatch` / `capability` if that fits better"*, R→`integration_mismatch`).
`kpi_rework.py:58-64,115-132` only validates the string against an allow-list and appends it.

Lead's independent verification of label noise across all 58 rows:

```
rows with defect categories but verifier_fail_count == 0 :  5 / 58
rows where sum(category counts) != verifier_fail_count   :  6 / 58
```

Both violate the counting contract in `go.md:895`. **Every "F1 says…" argument is therefore built
on the defendant's own testimony.** This does not make the numbers useless, but it forbids using
`integration_mismatch` as the primary success metric (see Decision D5).

### C3 — `code-over-docs.md` does not forbid git history. Brief fact F4 was overstated.

The hierarchy of truth (`code-over-docs.md`, Hierarchy section) ranks runtime > source > SDK docs
> in-repo docs > memory > comments. It **never mentions git history at all**. Absence is not
prohibition. The fix is therefore an **addition** (name a source the rule omits), not a **repeal**
(overturn a ban). Cheaper, non-breaking, and it means no existing rule has to be weakened.

### C4 — The 11 `integration_mismatch` tasks all PASSED.

```
integration_mismatch rows : 11
outcome counts            : {'pass': 11}
all 58 rows               : {'pass': 56, 'fail_exhausted': 2}
```

Every one was caught **inside** the run — by a Verifier retry or by the Phase 3B cross-provider
diff-review, whose review axis #1 is literally *"INTEGRATION — does this break a caller … or
REINVENT an existing helper"* (`go.md:807`). So `integration_mismatch=14` is substantially a
record of **the diff-reviewer doing its job**, not of loops escaping to production.

**The decisive consequence:** `kpi_rework` measures *retries within one `/go` run*. Dmitry's loop
is *cross-session and production-facing* — "fix A, and later B, C, D break." **The loop Dmitry
described is currently not measured at all.** F1 cannot be evidence for its cause.

---

## Agent positions

| Agent | Position on the 4th lens | Root cause named | Key insight |
|---|---|---|---|
| **A1** Architect | **Reject as a lens.** Phase 0D gate, Lead-owned, read-only | Incomplete context → but reframes: *"the missing mechanism is not 'read history', it is 'convert historical reasons into executable regression obligations before implementation'"* | Inside Flow Designer it becomes prose blended into the PFD — "that is exactly how 'add a review step' fails". Worker must never be the one deciding which fences are removable. |
| **A2** Git forensics | **Reject as a lens.** Phase 0C gate | `weak_verification` is the global leak; missing backward analysis is *one concrete cause* of the integration leak | *"Plain `git blame` alone is theatre."* Real signal lives in **revert commits, incident files, and the tests added alongside the change** — not in commit subjects. Demands `active/superseded/retired` statuses or "archaeology becomes a museum". |
| **A3** Verification | **Reject as a lens.** Phase 0.5 gate | **`integration_mismatch` is the symptom class; weak executable characterization is the root class** | The cure is a **characterization test** (Feathers): pin the defended behavior *before* the Worker edits. Hard rule: every invariant is `executable` \| `non_executable` \| `out_of_scope` — no fourth category. Non-executable invariants **must not** gate the verdict, or the exit-code axiom dies. |
| **A4** Skeptic | **Reject, and don't build it yet.** Conditional gate only after cheaper fixes fail | Verification, not history | Goodhart warning: *"once `integration_mismatch` is a tracked KPI, agents will simply stop labelling defects that way."* Cheapest first move: make the **existing** Phase 1B Challenge ask the backward question — zero marginal cost. Demands a `regression_contract_present` / `regression_test_behavioral` double-label to expose ceremony. |
| **X2** Grok | **Reject as a lens.** Phase 0 script-first gate | Archaeology is *necessary, not sufficient*; `weak_verification` + downstream testing are co-equal levers | Names the taxonomy trap: *"14 integration mismatches are not 14 'ignored git history' events"* — some are diff-review surface findings after a green Verifier test. Warns of **false fences** from blame/pickaxe and **Chesterton paralysis**. |
| **X3** Opus | **Reject entirely; do not build a phase.** Split it: deterministic half into existing gates, executable half into the Verifier | **Weak verification.** And: the production loop is *unmeasured* | Produced C1–C4 above. Critically: *"the 'fix A breaks B' loop usually does not delete B's guard — it changes an adjacent value B depended on, leaving B's code untouched. No diff-level deletion, so a deletion-gate is blind. Only a preservation test catches that."* |
| **X1** GLM-5.2 | — | — | **DEGRADED** (provider 529 × 3). No position recorded. |

### Unanimity

All six responding panelists — across three providers — **independently rejected the Lead's
hypothesis in its proposed form**. Not one endorsed a fourth lens inside the Flow Designer. The
convergent reasoning: the Flow Designer is a forward-time reasoning agent by construction
(`flow-designer.md:55,83,118`), so a backward pass placed inside it gets swallowed by the same
forward-planning machinery that missed the defect, and degrades into prose in the PFD.

All six also converged on four further points:

1. The artifact must be **schema-validated YAML with executable invariants**, never prose.
2. A **hook may check existence, schema, and wiring — never wisdom.** Hooks cannot read intent.
3. **Chesterton's Fence means "state why you're taking the fence down," never "never take it down."**
   Without `active/superseded/retired` statuses plus a superseding note, the mechanism becomes a
   ratchet that forbids all deletion and *causes* dead-code accretion.
4. Git history is **evidence of past intent**, not truth about present correctness. It may create
   a *test obligation*; it must never create an *automatic preservation obligation*.

---

## Decision

### D1 — REJECT the fourth Flow Designer lens. REJECT a new Phase 0.5.

Unanimous across 6 panelists / 3 providers. `/go` already has seven stages; the marginal stage
must earn its latency, and it cannot earn it against an unmeasured defect (C4).

### D2 — The real target is `weak_verification` (35), not `integration_mismatch` (14).

A3 and X3 independently reach the mechanically-correct root: **a shallow test passes a Worker
whose change silently breaks an adjacent behavior.** That *is* the loop. `integration_mismatch` is
already caught in-run 11/11 (C4). Backward analysis earns its place **only as a source of test
inputs** — never as prose a human must read.

The requirement to add: **the Verifier's test must assert preservation of adjacent behavior**, not
only correctness of new behavior. Mechanism: **characterization test** (Feathers / golden master),
seeded from a bounded `git blame` of the edit hunk, capturing the defended invariant *before* the
Worker edits.

Honest limit (X3): you cannot pin byte-identical behavior, because changing behavior may be the
point of the edit. The assertion must target **the invariant the incident established** ("position
never double-counts"), not full output equality. No incident and no expressible invariant → the
finding becomes an **advisory watchlist entry**, never a gate. This preserves the exit-code axiom.

### D3 — Split the mechanism. Add no new phase, no new agent, no new document class.

- **Deterministic half** → extend the two **existing** Phase 0 gates (`go.md:127` incident gate,
  `go.md:149` architecture gate). They already read state, cross-check the Artifact Contract, and
  block a stale contract. Add a bounded `git blame` of the touched hunk there.
- **Executable half** → a new PFD section `defended_behaviors:`, which inherits the PFD's existing
  split-delivery machinery (`paired-verification.md:388-396`): the rationale reaches the Worker via
  the `worker_directives` channel, the assertion reaches the Verifier via the `verifier_assertions`
  channel, and the Verifier still never sees the Worker's code or prompt. **The knowledge boundary
  is the one that already exists** — no new boundary to design or get wrong.

Schema (synthesized from A1's RAD, A2's RCD, A3's, A4's, X2's RDD, X3's `defended_behaviors`):

```yaml
defended_behaviors:
  - id: DB1
    hunk: "src/reconcile.py:88-94"
    introduced_by: "a1f3c9d  fix(sync): guard partial J2T fills (2026-07-06)"
    defends_against: "incident_2026-07-06_j2t_phantom_loop.md — late fill after local timeout double-counted position"
    evidence: "git log -S 'CANCELLING' -- src/reconcile.py"
    status: active            # active | superseded | retired
    superseded_by: null       # required non-null when status != active
    retirement_evidence: null # required when status == retired
    checkability: executable  # executable | non_executable | out_of_scope
    worker_directive:         # -> Worker channel only
      "MUST preserve the CANCELLING->FILLED transition; do not collapse to CANCELLED."
    regression_assertion:     # -> Verifier channel only
      assert: "a fill arriving after local timeout does NOT create a second position row"
      how: "inject late-fill for a CANCELLING order; assert position rowcount unchanged"
      derived_from: "DB1"
      exit_code_test: true
```

Only `checkability: executable` entries may block. `non_executable` entries are advisory.

### D4 — Amend `code-over-docs.md` by ADDITION, not re-ranking (per C3).

Four panelists proposed re-ranking the hierarchy to insert git history at level 2b/4. X3 showed
this is unnecessary: the rule never mentions history, so nothing needs overturning. Adopt the
minimal, non-breaking form:

> Commit messages, `git log -S` / `git blame` output on the touched lines, revert history, and
> linked `incident_*.md` files are **authoritative for the question "why does this exist"** — and
> remain level-4 (in-repo docs) for the question "what is true now." A 2026 commit message
> describing current behavior can be as stale as a README.
>
> History proves a fence existed and why. It does **not** prove the reason still holds. Therefore:
> a defended behavior may be deleted, but only with `status: superseded|retired` **plus a
> superseding note in the commit**. Chesterton's rule is "state why you're taking the fence down,"
> never "you may not take it down."

A2's forensic addendum, adopted verbatim into the runbook: **`git log --oneline` on subject lines
is theatre.** The high-signal probes are:

```bash
git log --follow -p -S 'symbol' -- path      # semantic presence of a guard
git log --follow --grep='revert\|incident\|regression\|fix' -i -- path
git log --follow --name-only -- path | rg 'test|spec|incident|regression'
git blame -w -C -C -L 120,170 -- path        # ignore whitespace, detect moves/copies
```

### D5 — Measure the loop that Dmitry actually described. It is currently unmeasured (C4).

`integration_mismatch` is **disqualified as the primary success metric**: it is self-labeled
(C2), noisy (5/58 and 6/58 contract violations), measures intra-run rework rather than production
loops (C4), and is Goodhart-exposed (A4: agents will simply stop typing the label).

**New primary signal — `reopen_rate` (deterministic, no LLM label):** the fraction of `/go` runs
whose touched hunks overlap a hunk touched by a **passed** `/go` run in the trailing 30 days, where
the new run's Artifact Contract names a regression. Derivable from `git` plus the existing task
log. This is the production-loop proxy, and it is exactly what should fall if backward analysis works.

Secondary guardrails — these must **not** rise: `mean_verifier_fail_count`, `worker_spawns`.

Counter-signals that prove we bought ceremony instead of quality:

| Counter-signal | What it means |
|---|---|
| `first_pass_clean_rate` **falls** | The new regression assertions over-constrain; prevented loops became V-failures. **X3 calls this the most likely failure mode.** |
| `weak_verification` flat or rising | The Verifier cites invariant IDs but still writes shallow tests |
| `defended_behaviors` entries mostly `non_executable` | Prose wearing a schema |
| Verifier tests assert the *presence of YAML fields* instead of behavior | A3's and A1's shared nightmare |
| Defect mass shifts to `contract_ambiguity` or `missed_failure_mode` | The taxonomy was gamed, not the defect fixed |
| Per-`/go` latency up materially, `reopen_rate` flat | Pure tax |

Goodhart defense (A4), adopted — a second label on every `kpi_rework` row:

```json
{ "defect": "integration_mismatch",
  "discovery_stage": "phase3_test",
  "regression_contract_present": true,
  "regression_test_behavioral": false }
```

This makes it *visible* when the artifact exists but fails to improve the test.

Horizon: **30-day rolling, and not fewer than 25 runs.** Cadence is ~58 runs in ~4 weeks.

### D6 — The deterministic hook: narrow, and honestly labeled as narrow.

Adopt a `chesterton_gate.py` PreToolUse hook, keyed off the one fact a hook can actually establish:
**the diff deletes a line that git-blames to an incident-linked commit the Artifact Contract never
acknowledged.**

```python
# chesterton_gate.py — PreToolUse. Deterministic; no model in the loop.
# Mirrors go_gate.py: fail-open on error, exit 2 to block.
for ln in deleted_logic_lines(git_diff_of_target(tool_input)):
    commit = git_blame_line(ln.file, ln.no, rev="HEAD")
    inc = incident_for_commit(commit)     # rolling_memory rows, priority>=95, by commit hash
    if inc and inc.id not in AC_incident_warnings():
        stderr(f"chesterton: deleting line defended by {inc.id} ({commit}); "
               f"name it in Incident Warnings or add a superseding note")
        return 2
return 0
```

**X3's limit is recorded as a first-class caveat, not a footnote:** this gate fires only on
*deletions of incident-linked lines*. The "fix A breaks B" loop usually does **not** delete B's
guard — it changes an adjacent value that B depended on, leaving B's code untouched. No diff-level
deletion, so **the gate is blind to the common case.** It is a narrow safety net for "someone ripped
out a guard," and it is *not* the cure. The cure is D2 (preservation tests).

Every panelist agreed on the hook's boundary: **it checks existence, schema, and wiring — never
whether the model understood the history.** Hooks should not pretend to read minds. As A2 put it:
*"This will not guarantee wisdom. It will prevent silent omission."*

---

## Rejected alternatives

| Alternative | Proposed by | Why rejected |
|---|---|---|
| Fourth lens inside Flow Designer | **Lead's original hypothesis** | Unanimous 6/6 rejection across 3 providers. FD is forward-time by construction; a backward pass inside it degrades into PFD prose and taxes every `/go`, including trivial ones. |
| Separate Phase 0.5 / 0C / 0D archaeology gate | A1, A2, A3, X2 (majority of the panel!) | Overruled by the Lead on X3's + A4's evidence: the target defect is caught in-run 11/11 (C4), the metric justifying it is a self-label (C2), and Phase 0 already has two gates with the exact required shape. A new phase must not be bought against an unmeasured defect. **Revisit if D5's `reopen_rate` shows the loop is real and D2 fails to move it.** |
| Dedicated archaeology agent on every run | A1, A2 (conditional) | Cost (a provider spawn per run) unjustified before measurement. Deterministic `git` probes cost milliseconds; the LLM summarization is the expense — bound *that*. |
| Generate `docs/dep_manifest.json` as the cheap fix | A4 (as the cheapest alternative), Lead | **Falsified by C1** — the manifest already exists (35 KB, 2026-07-02) and the defect persists. Necessary, not sufficient. |
| Re-rank the `code-over-docs.md` hierarchy of truth | A1, A2, A3, X2 (four panelists proposed variants) | **Unnecessary per C3.** The rule omits git history; it does not forbid it. Addition beats repeal: cheaper, non-breaking, no existing rule weakened. |
| `integration_mismatch` as the primary success metric | A1, A2, A3, X2 (four panelists) | **Disqualified per C2 + C4:** self-labeled, demonstrably noisy (5/58, 6/58), measures intra-run rework rather than the production loop, and Goodhart-exposed. Replaced by deterministic `reopen_rate`. |
| Do nothing yet; only make Phase 1B Challenge ask the backward question | A4 | Adopted **in part** — it is free and ships immediately. But it is not sufficient alone: Challenge output is prose, and prose does not become an exit code. It rides along with D2. |

---

## Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | Regression assertions over-constrain → `first_pass_clean_rate` falls, prevented loops become V-failures | **HIGH** (X3: "the most likely failure mode") | Assertions target the *incident's invariant*, never full-output equality. Non-executable → advisory watchlist, never a gate. Watch `first_pass_clean_rate` as a stop-loss. |
| R2 | Cargo-cult preservation: Chesterton's Fence becomes a ratchet forbidding all deletion → dead-code accretion (the *inverse* of the problem we're solving) | **HIGH** | `status: active\|superseded\|retired` is mandatory. Deletion always permitted with a superseding note. A2: *"Without active/superseded/retired, archaeology becomes a museum."* |
| R3 | Goodhart: agents stop typing `integration_mismatch` and the metric "improves" | **HIGH** | Primary metric replaced by deterministic `reopen_rate` (D5). Double-label (`regression_contract_present`, `regression_test_behavioral`) exposes artifact-without-effect. |
| R4 | False fences: `git blame` / pickaxe attribute a line to a reformatting or merge commit; the "defended behavior" is invented | MED | `blame -w -C -C` (ignore whitespace, follow moves). A2: subject-line `git log` is theatre. Require a **linked incident** for a finding to reach `checkability: executable`. |
| R5 | The hook is blind to the common loop shape (adjacent-value change, no deletion) | MED — **accepted, documented** | Recorded as a first-class caveat in D6. The hook is a narrow net for "guard ripped out"; the cure is D2. Do not let its presence create false confidence. |
| R6 | `git log -S` is O(history); pathological on large monorepos | LOW | Bound to the touched hunk and the AC's file scope. A2: never run archaeology over the repo. If it takes >60s, that is a signal the AC scope is too broad. |
| R7 | The panel's own numbers are LLM self-labels (C2) — including the ones used to *reject* the fourth lens | MED — **accepted** | Symmetric honesty: the same noise that weakens "build the lens" also weakens "don't build it." Resolved by D5: build the deterministic metric *first*, then decide. |

---

## Implementation recommendations (ordered; each independently shippable)

1. **Ship the measurement before the cure.** Implement `reopen_rate` in `kpi_rework.py` —
   deterministic, git-derived, no LLM label. Until the production loop is measured, every further
   step is justified by self-reported telemetry (C2). *This is the highest-value, lowest-risk step,
   and it is a prerequisite for evaluating everything below.*
2. **Free, immediate:** extend the Phase 1B Challenge prompt (`go.md:276`) to ask the backward
   question — *"which behavior in the touched hunk was introduced to fix something, and would this
   plan silently remove it?"* Zero marginal cost: the Challenge stage already runs, cross-provider,
   on every `/go`.
3. **Add the `defended_behaviors:` section to the PFD schema** (`flow-designer.md` §4) with the
   status / checkability fields from D3. Wire it into the existing split-delivery channels in
   `paired-verification.md:388-396`. No new document class, no new boundary.
4. **Extend the two existing Phase 0 gates** (`go.md:127`, `go.md:149`) with a bounded
   `git blame -w -C -C` of the touched hunk plus the A2 probe set. Deterministic; no agent spawn.
5. **Amend `code-over-docs.md` by addition** (D4). One paragraph. Non-breaking.
6. **Add the double-label to `kpi_rework.py`** (`regression_contract_present`,
   `regression_test_behavioral`) so ceremony is visible in the telemetry from day one.
7. **Only then** consider `chesterton_gate.py` (D6) — and ship it with its blindness documented in
   the same commit.
8. **Re-evaluate the rejected Phase 0.5** after 25+ runs of `reopen_rate` data. Four of six
   panelists wanted it. They may yet be right; they were arguing from the numbers available, and
   those numbers were the defendant's own testimony.

---

## Meta — what this consilium demonstrated about the consilium mechanism itself

The single highest-value output of this session was **not** a design decision. It was X3
discovering that the Lead's own Verified Facts Brief contained two errors and an overstatement
(C1, C3) — and that the telemetry justifying the entire question is LLM self-labeled (C2, C4).

The mechanism that caught it: **one panelist was given read access to the live system and
explicitly instructed to verify the brief rather than trust it.** The other five reasoned
faithfully — and therefore wrongly — from `docs/dep_manifest.json` being absent and from
`code-over-docs.md` forbidding history. Neither is true.

Four of six panelists then recommended building a new phase justified by a metric that does not
measure the problem. They were not wrong to; they had no way to know.

**Institutional lesson (promote to `institutional.md`):** in a consilium, at least one panelist
must be a *brief-verifier* with read access and an explicit mandate to attack the brief's facts —
not merely the brief's conclusions. `institutional.md` already says *"Consilium agents must be
briefed from verified code state, not reports/memory alone."* This session shows the Lead's own
RECON is itself a report that decays, and needs the same adversarial treatment. The Lead's brief
is not exempt from `code-over-docs`.

Second lesson: the Lead asserted `dep_manifest: absent` in the `/start` Context Receipt without
running `ls`. The Context Receipt is a **permit-to-work**, and an unverified line in it is worse
than a missing one, because downstream agents treat it as established fact. Every line of a Context
Receipt must be backed by a command that was actually run.
