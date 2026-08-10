#!/usr/bin/env bash
# Acceptance test: verify systemic-thinking rule/command template updates
# Tests observable properties only — does NOT reference Worker prompt or implementation.
# Exit 0 if all checks pass, non-zero if any fail.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
QND="$ROOT/templates/rules/quality-no-defects.md"
PV="$ROOT/templates/rules/paired-verification.md"
ST="$ROOT/templates/commands/start.md"
GO="$ROOT/templates/commands/go.md"
HACKATHON="$ROOT/templates/commands/hackathon.md"
CORE="$ROOT/templates/rules/core.md"
BC="$ROOT/templates/codex/skills/booster-command/SKILL.md"
FD="$ROOT/templates/rules/flow-designer.md"
GOSKILL="$ROOT/templates/codex/skills/go/SKILL.md"
README="$ROOT/README.md"
README_RU="$ROOT/README.ru.md"

PASS=0
FAIL=0

check() {
    local num="$1"
    local desc="$2"
    local result="$3"  # "ok" or "fail"
    if [[ "$result" == "ok" ]]; then
        echo "PASS [check $num] $desc"
        PASS=$((PASS + 1))
    else
        echo "FAIL [check $num] $desc"
        FAIL=$((FAIL + 1))
    fi
}

# ── Preflight: all three files must exist ──────────────────────────────────────

for f in "$QND" "$PV" "$ST" "$GO" "$HACKATHON" "$CORE" "$BC" "$FD" "$GOSKILL" "$README" "$README_RU"; do
    if [[ ! -f "$f" ]]; then
        echo "FATAL: required file not found: $f"
        exit 2
    fi
done

# ══════════════════════════════════════════════════════════════════════════════
# quality-no-defects.md checks (1-4)
# ══════════════════════════════════════════════════════════════════════════════

# Check 1: "fix producer" / "Fix the producer" / "Не маскируй" concept
if grep -qiE "(fix the producer|fix producer|не маскируй)" "$QND"; then
    check 1 "quality-no-defects.md: contains 'fix producer' or 'Не маскируй' directive" "ok"
else
    check 1 "quality-no-defects.md: contains 'fix producer' or 'Не маскируй' directive" "fail"
    echo "  Expected one of: 'Fix the producer', 'fix producer', 'Не маскируй'"
    echo "  Got: (pattern not found in $QND)"
fi

# Check 2: references data_patches_forbidden or dep_manifest
if grep -qiE "(data_patches_forbidden|dep_manifest)" "$QND"; then
    check 2 "quality-no-defects.md: references 'data_patches_forbidden' or 'dep_manifest'" "ok"
else
    check 2 "quality-no-defects.md: references 'data_patches_forbidden' or 'dep_manifest'" "fail"
    echo "  Expected one of: 'data_patches_forbidden', 'dep_manifest'"
    echo "  Got: (pattern not found in $QND)"
fi

# Check 3: "Three Nos violation" or "Layer 2" appears in new section context
# The file already has "Layer 2" in the existing section; we check for it AND
# "Three Nos violation" as a phrase that would appear in a new enforcement section.
if grep -qiE "(three nos violation|layer 2)" "$QND"; then
    check 3 "quality-no-defects.md: contains 'Three Nos violation' or 'Layer 2' in section context" "ok"
else
    check 3 "quality-no-defects.md: contains 'Three Nos violation' or 'Layer 2' in section context" "fail"
    echo "  Expected: 'Three Nos violation' or 'Layer 2'"
    echo "  Got: (pattern not found in $QND)"
fi

# Check 4: example mentioning nav_snapshots, calculate_nav, or apply_fill
if grep -qiE "(nav_snapshot|calculate_nav|apply_fill)" "$QND"; then
    check 4 "quality-no-defects.md: contains example with nav_snapshots / calculate_nav / apply_fill" "ok"
else
    check 4 "quality-no-defects.md: contains example with nav_snapshots / calculate_nav / apply_fill" "fail"
    echo "  Expected one of: 'nav_snapshot', 'calculate_nav', 'apply_fill'"
    echo "  Got: (pattern not found in $QND)"
fi

# ══════════════════════════════════════════════════════════════════════════════
# paired-verification.md checks (5-10)
# ══════════════════════════════════════════════════════════════════════════════

# Check 5: Artifact Contract section contains "Affected downstream:" field
if grep -q "Affected downstream:" "$PV"; then
    check 5 "paired-verification.md: Artifact Contract has 'Affected downstream:' field" "ok"
else
    check 5 "paired-verification.md: Artifact Contract has 'Affected downstream:' field" "fail"
    echo "  Expected: 'Affected downstream:' in Artifact Contract block"
    echo "  Got: (not found in $PV)"
fi

# Check 6: Artifact Contract section contains "Architecture map consulted:" field
if grep -q "Architecture map consulted:" "$PV"; then
    check 6 "paired-verification.md: Artifact Contract has 'Architecture map consulted:' field" "ok"
else
    check 6 "paired-verification.md: Artifact Contract has 'Architecture map consulted:' field" "fail"
    echo "  Expected: 'Architecture map consulted:' in Artifact Contract block"
    echo "  Got: (not found in $PV)"
fi

# Check 7: contains a Post-VERIFY architecture update section
if grep -qiE "(post-verify|post verify)" "$PV"; then
    check 7 "paired-verification.md: contains 'Post-VERIFY' section" "ok"
else
    check 7 "paired-verification.md: contains 'Post-VERIFY' section" "fail"
    echo "  Expected: 'Post-VERIFY' or 'post-VERIFY' section heading/reference"
    echo "  Got: (pattern not found in $PV)"
fi

# Check 8: Post-VERIFY section mentions "background" AND "ARCHITECTURE.md"
# Both must appear in the document (they naturally cluster in the new section)
pv_has_background=$(grep -ic "background" "$PV" || true)
pv_has_arch=$(grep -c "ARCHITECTURE.md" "$PV" || true)
if [[ "$pv_has_background" -ge 1 && "$pv_has_arch" -ge 1 ]]; then
    check 8 "paired-verification.md: post-verify section references 'background' and 'ARCHITECTURE.md'" "ok"
else
    check 8 "paired-verification.md: post-verify section references 'background' and 'ARCHITECTURE.md'" "fail"
    echo "  Expected: both 'background' (found: $pv_has_background) and 'ARCHITECTURE.md' (found: $pv_has_arch)"
fi

# Check 9: contains "dep_manifest.json" somewhere
if grep -q "dep_manifest.json" "$PV"; then
    check 9 "paired-verification.md: contains 'dep_manifest.json'" "ok"
else
    check 9 "paired-verification.md: contains 'dep_manifest.json'" "fail"
    echo "  Expected: 'dep_manifest.json' reference"
    echo "  Got: (not found in $PV)"
fi

# Check 10: contains a RECON section that mentions architecture reading
if grep -qiE "recon" "$PV" && grep -qiE "(architecture|ARCHITECTURE)" "$PV"; then
    check 10 "paired-verification.md: RECON section mentions architecture reading" "ok"
else
    check 10 "paired-verification.md: RECON section mentions architecture reading" "fail"
    echo "  Expected: 'RECON' section (case-insensitive) AND 'architecture' reference"
    recon_count=$(grep -ic "recon" "$PV" || true)
    arch_count=$(grep -ic "architecture" "$PV" || true)
    echo "  'recon' occurrences: $recon_count, 'architecture' occurrences: $arch_count"
fi

# ══════════════════════════════════════════════════════════════════════════════
# start.md checks (11-16)
# ══════════════════════════════════════════════════════════════════════════════

# Check 11: contains "ARCHITECTURE.md"
if grep -q "ARCHITECTURE.md" "$ST"; then
    check 11 "start.md: contains 'ARCHITECTURE.md'" "ok"
else
    check 11 "start.md: contains 'ARCHITECTURE.md'" "fail"
    echo "  Expected: 'ARCHITECTURE.md' reference in start command"
    echo "  Got: (not found in $ST)"
fi

# Check 12: contains "dep_manifest.json"
if grep -q "dep_manifest.json" "$ST"; then
    check 12 "start.md: contains 'dep_manifest.json'" "ok"
else
    check 12 "start.md: contains 'dep_manifest.json'" "fail"
    echo "  Expected: 'dep_manifest.json' reference in start command"
    echo "  Got: (not found in $ST)"
fi

# Check 13: contains "circuit board" or "dependency" or "architecture map"
if grep -qiE "(circuit board|dependency|architecture map)" "$ST"; then
    check 13 "start.md: contains 'circuit board', 'dependency', or 'architecture map'" "ok"
else
    check 13 "start.md: contains 'circuit board', 'dependency', or 'architecture map'" "fail"
    echo "  Expected one of: 'circuit board', 'dependency', 'architecture map'"
    echo "  Got: (pattern not found in $ST)"
fi

# Check 14: start emits a Context Receipt before planning
if grep -q "Context Receipt" "$ST" && grep -qi "permit-to-work" "$ST"; then
    check 14 "start.md: requires Context Receipt permit before planning" "ok"
else
    check 14 "start.md: requires Context Receipt permit before planning" "fail"
    echo "  Expected: 'Context Receipt' and 'permit-to-work' in $ST"
fi

# Check 15: start hard-stops on unread incident sources
if grep -q "incident sources" "$ST" && grep -qi "Hard stop" "$ST"; then
    check 15 "start.md: hard-stops when incident sources are listed but unread" "ok"
else
    check 15 "start.md: hard-stops when incident sources are listed but unread" "fail"
    echo "  Expected: 'incident sources' and 'Hard stop' in $ST"
fi

# Check 16: start receipt includes handover required reading
if grep -q "Handover required reading" "$ST"; then
    check 16 "start.md: Context Receipt records handover required reading" "ok"
else
    check 16 "start.md: Context Receipt records handover required reading" "fail"
    echo "  Expected: 'Handover required reading' in $ST"
fi

# ══════════════════════════════════════════════════════════════════════════════
# go.md checks (17-19)
# ══════════════════════════════════════════════════════════════════════════════

if grep -q "Architecture Context:" "$GO"; then
    check 17 "go.md: Artifact Contract requires Architecture Context" "ok"
else
    check 17 "go.md: Artifact Contract requires Architecture Context" "fail"
    echo "  Expected: 'Architecture Context:' in $GO"
fi

if grep -q "Incident Warnings:" "$GO"; then
    check 18 "go.md: Artifact Contract requires Incident Warnings" "ok"
else
    check 18 "go.md: Artifact Contract requires Incident Warnings" "fail"
    echo "  Expected: 'Incident Warnings:' in $GO"
fi

if grep -qi "Worker that only sees a code fragment" "$GO"; then
    check 19 "go.md: blocks fragment-only Worker execution" "ok"
else
    check 19 "go.md: blocks fragment-only Worker execution" "fail"
    echo "  Expected: fragment-only Worker block in $GO"
fi

# ══════════════════════════════════════════════════════════════════════════════
# global/core + Codex bridge checks (20-23)
# ══════════════════════════════════════════════════════════════════════════════

if grep -q "Pre-Work Context Gate" "$CORE" && grep -q "Context Receipt" "$CORE"; then
    check 20 "core.md: global Pre-Work Context Gate exists" "ok"
else
    check 20 "core.md: global Pre-Work Context Gate exists" "fail"
    echo "  Expected: 'Pre-Work Context Gate' and 'Context Receipt' in $CORE"
fi

if grep -q "coding Agent spawn" "$CORE" && grep -q "Incident memory" "$CORE"; then
    check 21 "core.md: blocks coding Agent spawn without incident-aware receipt" "ok"
else
    check 21 "core.md: blocks coding Agent spawn without incident-aware receipt" "fail"
    echo "  Expected: 'coding Agent spawn' and 'Incident memory' in $CORE"
fi

if grep -q "Pre-Work Context Gate" "$BC" && grep -q "memory_start_context" "$BC"; then
    check 22 "booster-command skill: Codex runner requires memory start context" "ok"
else
    check 22 "booster-command skill: Codex runner requires memory start context" "fail"
    echo "  Expected: 'Pre-Work Context Gate' and 'memory_start_context' in $BC"
fi

if grep -q "Architecture Context:" "$BC" && grep -q "Incident Warnings:" "$BC"; then
    check 23 "booster-command skill: Codex /go requires architecture and incident fields" "ok"
else
    check 23 "booster-command skill: Codex /go requires architecture and incident fields" "fail"
    echo "  Expected: 'Architecture Context:' and 'Incident Warnings:' in $BC"
fi

# ══════════════════════════════════════════════════════════════════════════════
# Prototype Gate / role handoff checks (24-31)
# ══════════════════════════════════════════════════════════════════════════════

if grep -q "Phase 1C — PROTOTYPE GATE" "$GO" && grep -q "Prototype Handoff" "$GO"; then
    check 24 "go.md: inserts Prototype Gate before Worker" "ok"
else
    check 24 "go.md: inserts Prototype Gate before Worker" "fail"
    echo "  Expected: 'Phase 1C — PROTOTYPE GATE' and 'Prototype Handoff' in $GO"
fi

if grep -q "prototype_plan" "$GO" && grep -q "role_handoff_contract" "$GO"; then
    check 25 "go.md: PFD schema requires prototype plan and role handoff contract" "ok"
else
    check 25 "go.md: PFD schema requires prototype plan and role handoff contract" "fail"
    echo "  Expected: 'prototype_plan' and 'role_handoff_contract' in $GO"
fi

if grep -q "NO INSERT/UPDATE/DELETE" "$GO" && grep -q "mandatory notebook" "$GO" && grep -q "investigation journal" "$GO" && grep -q "paired probe script merely" "$GO"; then
    check 26 "go.md: Prototyper is read-only; notebook is mandatory journal, not a paired probe-script requirement" "ok"
else
    check 26 "go.md: Prototyper is read-only; notebook is mandatory journal, not a paired probe-script requirement" "fail"
    echo "  Expected: read-only DML ban, mandatory journal semantics, and no paired probe-script requirement in $GO"
fi

if grep -q "broker sync" "$GO" && grep -q "Prototype PASS before Worker" "$GO"; then
    check 27 "go.md: broker/data/DB class requires Prototype PASS before Worker" "ok"
else
    check 27 "go.md: broker/data/DB class requires Prototype PASS before Worker" "fail"
    echo "  Expected: broker/data class and 'Prototype PASS before Worker' in $GO"
fi

if grep -q "Role handoff standard" "$GO" && grep -q "Prototyper | Worker" "$GO" && grep -q "Prototyper | Verifier" "$GO"; then
    check 28 "go.md: defines role handoff payloads between Prototyper, Worker, and Verifier" "ok"
else
    check 28 "go.md: defines role handoff payloads between Prototyper, Worker, and Verifier" "fail"
    echo "  Expected role handoff table rows for Prototyper -> Worker/Verifier in $GO"
fi

if grep -q "Prototype Gate:" "$PV" && grep -q "Prototype Handoff:" "$PV"; then
    check 29 "paired-verification.md: Artifact Contract carries Prototype Gate and Handoff fields" "ok"
else
    check 29 "paired-verification.md: Artifact Contract carries Prototype Gate and Handoff fields" "fail"
    echo "  Expected: 'Prototype Gate:' and 'Prototype Handoff:' in $PV"
fi

# ══════════════════════════════════════════════════════════════════════════════
# Evidence-first / final-deploy test-gate checks (32-38)
# ══════════════════════════════════════════════════════════════════════════════

if grep -q "Evidence Receipt" "$PV" && grep -q "source identity" "$PV" && grep -q "timestamp" "$PV"; then
    check 32 "paired-verification.md: requires source identity and timestamps in an Evidence Receipt" "ok"
else
    check 32 "paired-verification.md: requires source identity and timestamps in an Evidence Receipt" "fail"
fi

if grep -q "Final deploy gate — frozen durable regression tests only after evidence" "$PV" && grep -q "full existing suite" "$PV"; then
    check 33 "paired-verification.md: delays durable regression tests until final deploy gate" "ok"
else
    check 33 "paired-verification.md: delays durable regression tests until final deploy gate" "fail"
fi

if grep -q "Do not create or rewrite an acceptance suite, test files, fixtures, mocks, synthetic datasets" "$GO" && grep -q "## Phase 3 — DIRECT-PROBE RUN" "$GO"; then
    check 34 "go.md: forbids synthetic validation artifacts during iterations and requires direct probes" "ok"
else
    check 34 "go.md: forbids synthetic validation artifacts during iterations and requires direct probes" "fail"
fi

if grep -q "Now, and only now, create or update durable regression tests" "$GO" && grep -q "Direct probes: exit=0. Full existing suite: exit=0." "$GO"; then
    check 35 "go.md: final deploy gate requires direct evidence before durable tests and full suite" "ok"
else
    check 35 "go.md: final deploy gate requires direct evidence before durable tests and full suite" "fail"
fi

if grep -q "direct-probe requirements" "$FD" && ! grep -q "mock time, inject failure" "$FD"; then
    check 36 "flow-designer.md: emits direct-probe requirements rather than mock-test instructions" "ok"
else
    check 36 "flow-designer.md: emits direct-probe requirements rather than mock-test instructions" "fail"
fi

if grep -q "synthetic test stand" "$BC" && grep -q "final deploy gate" "$BC" && grep -q "evidence receipt from direct read-only probes" "$GOSKILL"; then
    check 37 "Codex bridge and go skill preserve evidence-first final-deploy semantics" "ok"
else
    check 37 "Codex bridge and go skill preserve evidence-first final-deploy semantics" "fail"
fi

if ! grep -qiE "write an executable acceptance test|rewrite the test script|same verifier test|same unchanged test" "$PV" "$GO" "$FD" "$BC" "$GOSKILL"; then
    check 38 "canonical protocol has no pre-final acceptance-test/rewrite mandate" "ok"
else
    check 38 "canonical protocol has no pre-final acceptance-test/rewrite mandate" "fail"
fi

# Evidence-first verification must not regress to synthetic scenario injection or
# test-exit-code verdicts in the PFD and README explanations.
if ! grep -qiE "PFD informs testing|which branches to inject in tests|simulate: fire timeout, then inject fill|inject ramping forecast|Workers and Verifiers writing code, tests|Lead runs the test|Executable test shapes|Mock clock / controllable time|Branch injection|standard Verifier testing|generated branch-injection tests|downstream consumer tests|Verifier tests must cover at least one downstream consumer|green test's exit code|EXIT CODE = interim verdict|a green test" "$FD" "$README"; then
    check 39 "PFD and README contain no synthetic verification-loop residuals" "ok"
else
    check 39 "PFD and README contain no synthetic verification-loop residuals" "fail"
fi

if grep -q "Role handoff standard" "$PV" && grep -q "Prototype FAIL means no Worker spawn" "$PV"; then
    check 30 "paired-verification.md: standardizes no-loss handoff and blocks Worker on failed prototype" "ok"
else
    check 30 "paired-verification.md: standardizes no-loss handoff and blocks Worker on failed prototype" "fail"
    echo "  Expected: role handoff standard and failed-prototype Worker block in $PV"
fi

if grep -q "Prototype Gate" "$BC" && grep -q "Prototype Handoff" "$BC"; then
    check 31 "booster-command skill: Codex bridge carries Prototype Gate requirement" "ok"
else
    check 31 "booster-command skill: Codex bridge carries Prototype Gate requirement" "fail"
    echo "  Expected: 'Prototype Gate' and 'Prototype Handoff' in $BC"
fi

# Mandatory investigation-notebook contract: no optional notebook wording may
# survive in canonical operational text.
if grep -q "non-trivial behavioral, data, runtime, external-system" "$GO" \
    && grep -q "ISO timestamp" "$GO" \
    && grep -q "raw-output reference" "$GO" \
    && grep -q "pure docs/format/static-config task with no executable data/runtime hypothesis" "$GO" \
    && grep -q "mandatory investigation notebook" "$BC" \
    && grep -q "mandatory investigation notebook" "$GOSKILL"; then
    check 40 "canonical protocol: mandatory notebook records complete read-only investigation evidence" "ok"
else
    check 40 "canonical protocol: mandatory notebook records complete read-only investigation evidence" "fail"
fi

if ! grep -qiE "if a notebook is useful|notebook is useful|notebook path or none|notebook: <path or none>" "$PV" "$GO" "$BC" "$GOSKILL"; then
    check 41 "canonical protocol: notebook is never optional and never reported as path-or-none" "ok"
else
    check 41 "canonical protocol: notebook is never optional and never reported as path-or-none" "fail"
fi

# Audit-fix hardening: all operational variants must preserve the same
# candidate-bound, fail-closed evidence contract.
if grep -q "artifact/tree-diff SHA-256" "$GO" "$PV" "$HACKATHON" \
    && grep -q "process/build/deployment/version identity" "$GO" "$PV" "$HACKATHON" \
    && grep -q "newer than the last relevant edit" "$GO" "$PV" "$HACKATHON"; then
    check 42 "candidate binding: receipts bind exact artifact and runtime identity after the final edit" "ok"
else
    check 42 "candidate binding: receipts bind exact artifact and runtime identity after the final edit" "fail"
fi

if grep -q "HTTP is GET/HEAD only" "$GO" \
    && grep -q "DB-enforced read-only role" "$GO" "$PV" "$HACKATHON" \
    && grep -q "COPY PROGRAM" "$GO" "$PV" "$HACKATHON" \
    && grep -q "operation class" "$GO" "$PV" "$HACKATHON" \
    && grep -q "allowlist decision" "$GO" "$PV" "$HACKATHON"; then
    check 43 "read-only policy: HTTP/SQL/CLI/filesystem allowlist fails closed" "ok"
else
    check 43 "read-only policy: HTTP/SQL/CLI/filesystem allowlist fails closed" "fail"
fi

if grep -q "never a temp directory" "$GO" \
    && grep -q "never a tempdir" "$PV" "$BC" "$GOSKILL" \
    && grep -q "durable repo-relative artifact" "$GO" "$PV" "$HACKATHON" \
    && grep -q "SHA-256" "$GO" "$PV" "$HACKATHON"; then
    check 44 "durable notebook: no ephemeral path and raw-output SHA retained" "ok"
else
    check 44 "durable notebook: no ephemeral path and raw-output SHA retained" "fail"
fi

if grep -q "final regression manifest" "$GO" "$PV" "$HACKATHON" \
    && grep -q "test-file SHA-256" "$GO" "$PV" "$HACKATHON" \
    && grep -q "tests cannot" "$GO" "$PV" "$HACKATHON" \
    && grep -q "separate scoped run" "$GO" "$PV" "$HACKATHON"; then
    check 45 "final-test immutability: manifest and hashes freeze before suite" "ok"
else
    check 45 "final-test immutability: manifest and hashes freeze before suite" "fail"
fi

if grep -q "prototype_plan:" "$FD" && grep -q "role_handoff_contract:" "$FD" \
    && ! grep -qiE "property-style (test|тест)" "$PV" \
    && ! grep -qi "reopens test contract" "$PV"; then
    check 46 "PFD schema carries prototype and handoff sections; paired verification has no property-test residue" "ok"
else
    check 46 "PFD schema carries prototype and handoff sections; paired verification has no property-test residue" "fail"
fi

if grep -q "candidate-bound" "$README_RU" \
    && grep -q "Evidence Receipt" "$README_RU" \
    && ! grep -qiE "independent test|green test" "$README_RU" \
    && ! grep -qi "created/updated paths or none needed" "$GO" \
    && ! grep -qiE "edge-test harvest|durable test harvest" "$BC" "$GO"; then
    check 48 "residue cleanup: Russian evidence wording and frozen-test contract" "ok"
else
    check 48 "residue cleanup: Russian evidence wording and frozen-test contract" "fail"
fi

if ! grep -qiE "same acceptance suite|same test suite|Judge tests all" "$README" "$README_RU" \
    && grep -q "candidate-bound" "$README" "$README_RU" \
    && grep -q "not create an acceptance suite" "$HACKATHON" \
    && grep -q "test harvest/rewrite" "$HACKATHON" \
    && grep -q "final deploy gate" "$HACKATHON"; then
    check 47 "README and hackathon avoid pre-winner synthetic-test bypass language" "ok"
else
    check 47 "README and hackathon avoid pre-winner synthetic-test bypass language" "fail"
fi

# Final protocol corrections: each assertion is scoped to the file that owns
# the contract, so a phrase in another template cannot produce a false pass.
if sed -n '/\*\*Prototyper prompt:\*\*/,/## Artifact Contract/p' "$GO" | grep -q "DevTools" \
    && sed -n '/\*\*Prototyper prompt:\*\*/,/## Artifact Contract/p' "$GO" | grep -q "may only inspect console, network, performance, DOM, or storage state" \
    && sed -n '/\*\*Prototyper prompt:\*\*/,/## Artifact Contract/p' "$GO" | grep -q "click, type, navigation, script injection, storage mutation" \
    && sed -n '/\*\*Prototyper prompt:\*\*/,/## Artifact Contract/p' "$GO" | grep -q "worker mutation, or any page-state mutation" \
    && sed -n '/\*\*Prototyper prompt:\*\*/,/## Artifact Contract/p' "$GO" | grep -q "Unknown or unprovable read-only"; then
    check 49 "go.md: Prototyper embeds fail-closed DevTools read-only allowlist" "ok"
else
    check 49 "go.md: Prototyper embeds fail-closed DevTools read-only allowlist" "fail"
fi

if grep -q "Baseline/source snapshot binding:" "$GO" \
    && grep -q "query SHA-256; result SHA-256; raw-output SHA-256" "$GO" \
    && ! sed -n '/\*\*Prototyper prompt:\*\*/,/### Prototype pass\/fail rule/p' "$GO" | grep -q "Candidate binding:"; then
    check 50 "go.md: Prototype Handoff binds baseline snapshot rather than future candidate" "ok"
else
    check 50 "go.md: Prototype Handoff binds baseline snapshot rather than future candidate" "fail"
fi

if sed -n '/## Prototype Gate — executable truth before code/,/## Verifier mandate/p' "$PV" | grep -q "Baseline/source snapshot binding:" \
    && sed -n '/\*\*Стадия 2/,/\*\*Стадия 3/p' "$PV" | grep -q "baseline/source snapshot binding (exact query SHA-256, result SHA-256, and raw-output SHA-256)" \
    && ! sed -n '/\*\*Стадия 2/,/\*\*Стадия 3/p' "$PV" | grep -q "candidate binding" \
    && sed -n '/## Prototype Gate — executable truth before code/,/## Verifier mandate/p' "$PV" | grep -q "source/environment identity" \
    && sed -n '/## Prototype Gate — executable truth before code/,/## Verifier mandate/p' "$PV" | grep -q "exact query SHA-256" \
    && sed -n '/## Prototype Gate — executable truth before code/,/## Verifier mandate/p' "$PV" | grep -q "result SHA-256" \
    && sed -n '/## Prototype Gate — executable truth before code/,/## Verifier mandate/p' "$PV" | grep -q "raw-output SHA-256" \
    && sed -n '/## Prototype Gate — executable truth before code/,/## Verifier mandate/p' "$PV" | grep -q "ISO timestamp or observation" \
    && sed -n '/## Prototype Gate — executable truth before code/,/## Verifier mandate/p' "$PV" | grep -q "window, baseline/source snapshot binding" \
    && sed -n '/## Prototype Gate — executable truth before code/,/## Verifier mandate/p' "$PV" | grep -q "MUST NOT fabricate or name a future candidate identity" \
    && ! sed -n '/## Prototype Gate — executable truth before code/,/## Verifier mandate/p' "$PV" | grep -q "Candidate binding: artifact/tree-diff SHA-256" \
    && ! sed -n '/## Prototype Gate — executable truth before code/,/## Verifier mandate/p' "$PV" | grep -q "process/build/deployment/version identity as applicable"; then
    check 53 "paired-verification.md: pre-Worker Prototype evidence binds baseline, never a future candidate" "ok"
else
    check 53 "paired-verification.md: pre-Worker Prototype evidence binds baseline, never a future candidate" "fail"
fi

if grep -q "baseline_source_snapshot:" "$FD" \
    && grep -q "prototype_to_verifier: \"<baseline-derived" "$FD" \
    && ! sed -n '/prototype_plan:/,/role_handoff_contract:/p' "$FD" | grep -q "candidate_binding:"; then
    check 51 "flow-designer.md: prototype plan and handoff carry baseline snapshot only" "ok"
else
    check 51 "flow-designer.md: prototype plan and handoff carry baseline snapshot only" "fail"
fi

if grep -q "Canonical re-probe gate — mandatory after winner move/copy" "$HACKATHON" \
    && grep -q "evidence whose timestamp is newer than the move/copy time" "$HACKATHON" \
    && grep -q "Do not delete losing artifacts until the canonical re-probe PASS" "$HACKATHON" \
    && sed -n '/### Canonical re-probe gate/,/### Edge evidence harvest/p' "$HACKATHON" | grep -q "canonical re-probe PASS may the Lead delete losing artifacts and resume" \
    && sed -n '/### Canonical re-probe gate/,/### Edge evidence harvest/p' "$HACKATHON" | grep -qF '`/go` at Phase 3B diff review'; then
    check 52 "hackathon.md: canonical winner is re-probed before deletion or /go review" "ok"
else
    check 52 "hackathon.md: canonical winner is re-probed before deletion or /go review" "fail"
fi

# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════

echo ""
echo "Results: $PASS passed, $FAIL failed (out of $((PASS + FAIL)) checks)"

if [[ "$FAIL" -gt 0 ]]; then
    echo "OVERALL: FAIL"
    exit 1
else
    echo "OVERALL: PASS"
    exit 0
fi
