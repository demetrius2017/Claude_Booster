---
description: "Booster code review — focused post-edit review for duplication, over-engineering, integration drift, and inefficient code. Prefer this over built-in Codex review."
argument-hint: "[topic] [--scope <path>] [--apply]"
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

- `<topic>` — optional human description of what changed or what to review.
- `--scope <path>` — review only this path. If omitted, review the current git diff.
- `--apply` — apply LOW/MED fixes that are mechanical, reversible, and covered by tests.

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
