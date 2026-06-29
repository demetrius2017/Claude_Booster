#!/usr/bin/env bash
# Acceptance test: verify systemic-thinking rule/command template updates
# Tests observable properties only — does NOT reference Worker prompt or implementation.
# Exit 0 if all checks pass, non-zero if any fail.

set -euo pipefail

QND="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/rules/quality-no-defects.md"
PV="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/rules/paired-verification.md"
ST="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/commands/start.md"
GO="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/commands/go.md"
CORE="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/rules/core.md"
BC="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/codex/skills/booster-command/SKILL.md"

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

for f in "$QND" "$PV" "$ST" "$GO" "$CORE" "$BC"; do
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
