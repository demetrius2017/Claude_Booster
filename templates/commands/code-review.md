---
description: "Booster code review — focused post-edit review for duplication, over-engineering, integration drift, and inefficient code. Prefer this over built-in Codex review."
argument-hint: "[model] [topic] [--model <model>] [--scope <path>] [--apply]"
---

## Purpose

Run the Booster code-review standard. This is **not** the broad `/audit`
tribunal. It is the fast post-edit review pass used before external audit:
find avoidable complexity, duplicated helpers, invented abstractions, weak
integration with existing code, and inefficient implementation choices. It may
apply low-risk fixes when `--apply` is present or when the parent pipeline
explicitly requires auto-fix.

## Progress tracking

Before each phase below, run: `python3 ~/.claude/scripts/phase.py progress "<N>/4 <step_label>"`
After the final step completes, run: `python3 ~/.claude/scripts/phase.py progress clear`

Steps: `1/4 recon`, `2/4 review`, `3/4 apply`, `4/4 verify`

## Arguments

Parse `$ARGUMENTS`:

- `[model]` — optional first positional model selector. Recognized aliases:
  `fable`, `codex`, `codex-5.5`, `gpt-5.5`, `sonnet`, `opus`, `haiku`,
  `grok`, `zai`, `glm`. Example: `code-review fable --scope templates/commands`.
- `--model <model>` — explicit model selector; wins over positional model.
- `<topic>` — optional human description of what changed or what to review.
- `--scope <path>` — review only this path. If omitted, review the current git diff.
- `--apply` — apply LOW/MED fixes that are mechanical, reversible, and covered by tests.

Model parsing rule:

1. First consume flags (`--model`, `--scope`, `--apply`) and their values.
2. If `--model <model>` is present, set `review_model=<model>`.
3. Otherwise, if the first remaining positional token is one of the recognized
   model aliases above, set `review_model=<that token>` and remove it from the
   topic.
4. The rest of the positional tokens are `<topic>`.

If no model is supplied, use the default review route from the normal
code-review protocol.

### Review model routing

The selected `review_model` controls only the reviewer/opinion phase. It does
not change the Lead model, `model_balancer.json`, or the model used for later
commands.

| Selector | Reviewer channel |
|---|---|
| `fable` | One explicit Fable 5 review pass. This is Dmitry's explicit permission for this `/code-review` only. Fable must be read-only: it may inspect context and produce findings, but must not edit files. |
| `codex`, `codex-5.5`, `gpt-5.5` | Codex `gpt-5.5` read-only reviewer, e.g. `codex_worker.sh gpt-5.5` when available. |
| `sonnet` | Claude Sonnet reviewer via Agent/Claude CLI when available. |
| `opus` | Claude Opus reviewer via Agent/Claude CLI when available. |
| `haiku` | Claude Haiku reviewer; use only for mechanical/simple diffs. |
| `grok` | Grok read-only review channel when authenticated. |
| `zai`, `glm` | GLM/Z.ai read-only review channel when healthy and `ZAI_API_KEY` is present. |

If the selected reviewer channel is unavailable, say so plainly and stop the
review; do not silently fall back to another model. If the selected reviewer is
`fable`, do not reinterpret the request as `/fable` or `/consilium`: this is
still the `/code-review` protocol, just with Fable as the review model.
After any `review_model=fable` review pass, run
`python3 ~/.claude/scripts/fable_usage.py refresh-display` and append its output
if it prints anything. This refreshes the current UTC month from Claude/Codex
transcript stores before printing. The spend lines are API-equivalent /
credit-rate estimates from the shared Fable usage ledger/cache, not an actual
billing ledger.

Default scope:

```bash
git diff --name-only --diff-filter=ACMRTUXB
```

If there is no diff and no `--scope`, stop with:

```text
/code-review: nothing to review — no git diff and no --scope supplied.
```

## Phase 1 — RECON

Run: `python3 ~/.claude/scripts/phase.py progress "1/4 recon"`

Build a concise Verified Review Brief before any review opinion:

1. `git diff --stat` and `git diff --name-only`.
2. For each changed file or `--scope` path, identify language/framework and
   nearby tests.
3. Read `ARCHITECTURE.md` and `docs/dep_manifest.json` if present; note touched
   components, `critical: true`, `feeds`, and `called_by`.
4. Search for existing helpers before claiming duplication:
   - function names from the diff
   - obvious domain keywords
   - import/module names introduced by the patch
5. Collect commands already run this session if visible; otherwise mark
   verification state as unknown.

Brief shape:

```text
Verified Review Brief:
  Topic: <topic or inferred from diff>
  Review model: <review_model or default>
  Scope: <paths>
  Changed files: <N>
  Architecture map: <read|absent>; critical components: <list|none>
  Existing helpers searched: <patterns>
  Verification before review: <commands/evidence|unknown>
```

## Phase 2 — REVIEW

Run: `python3 ~/.claude/scripts/phase.py progress "2/4 review"`

For fewer than 5 changed source files, one reviewer may run the three lenses in
one pass. For 5+ files, split into three independent reviewers. In Codex, use
subagents if available; otherwise run a local second pass and label it as local,
not as full multi-agent parity.

If `review_model` is set, every reviewer/lens must use that selected model
channel. For `review_model=fable`, prefer one Fable 5 reviewer that covers all
three lenses in a single pass even for larger diffs, unless Dmitry explicitly
asks to spend additional Fable calls. The Lead may split the brief into chunks
only when the selected channel's context limit requires it, and must report the
number of Fable calls used.

### Lenses

| Lens | Question | Finding prefix |
|---|---|---|
| `reuse` | Did the patch duplicate existing helpers, ignore local patterns, or invent a parallel abstraction? | R |
| `simplicity` | Is the change broader, more abstract, more stateful, or more indirect than the problem requires? | S |
| `efficiency` | Does it add avoidable latency, memory use, N+1 work, repeated parsing, or expensive operations in hot paths? | E |

Every reviewer receives the same Verified Review Brief and must return exactly:

```text
LENS: <reuse|simplicity|efficiency>
VERDICT: PASS | CONCERN | FAIL

FINDINGS:
FINDING-<R|S|E><N>:
  severity: HIGH | MED | LOW
  file: <path>:<line>
  evidence: <specific code fact; quote only the minimum needed>
  issue: <what is wrong>
  fix: <imperative fix directive>
  apply_safe: true | false

RECOMMENDATIONS:
- <ordered, concrete next actions>
```

Severity:

- HIGH: likely behavioral regression, data loss, security issue, or critical
  integration break. Do not auto-apply; route to `/go` or `/audit` if needed.
- MED: real maintainability/performance/integration issue, safe to fix if small.
- LOW: style or local simplification.

## Phase 3 — APPLY

Run: `python3 ~/.claude/scripts/phase.py progress "3/4 apply"`

If `--apply` is absent: do not edit. Print findings and skip to Phase 4 with
`apply: skipped`.

If `--apply` is present:

1. Apply only findings where `apply_safe: true` and severity is LOW or MED.
2. Do not apply HIGH findings automatically.
3. Do not broaden scope beyond reviewed files.
4. Preserve user changes and unrelated dirty files.
5. If applying a fix touches a data path function, add/keep input guards,
   invariants, and output validation per Three Nos.

If a finding requires design uncertainty, DB mutation, migration, auth/security
change, external side effect, or cross-service contract change, stop applying
that finding and recommend `/go` with a complete Artifact Contract.

## Phase 4 — VERIFY

Run: `python3 ~/.claude/scripts/phase.py progress "4/4 verify"`

Verification depends on what happened:

- No edits made: verify with `git diff --check` and any existing test evidence
  already available; report runtime verification as N/A.
- Edits made: run the narrowest relevant tests, linters, or syntax checks. At
  minimum run `git diff --check`; for shell tests use `bash -n`; for Python
  changed files use `python -m py_compile` or project tests when available.

Output:

```text
Code review verdict: PASS | CONCERN | FAIL
Review model: <review_model or default>
Scope: <paths>
Findings: <count by severity>
Applied: <count and files, or skipped>
Verification:
- <command> exit=<N>
Recommended next action: <specific next action>
```

Run: `python3 ~/.claude/scripts/phase.py progress clear`

## Relationship to `/audit`

Use `/code-review` first for local quality cleanup. Use `/audit` after that when
you need the full multi-lens tribunal with external review and a persisted
`reports/audit_*.md` knowledge artifact.
