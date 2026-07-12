#!/usr/bin/env bash
# Acceptance test for /architecture command file.
# Verifier contract: tests OBSERVABLE BEHAVIOR of the artifact against the
# Artifact Contract. Does NOT inspect Worker's implementation strategy.
# Exit 0 = all assertions pass; non-zero = one or more failures.

set -uo pipefail

ARTIFACT="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/commands/architecture.md"

PASS=0
FAIL=0

_pass() { echo "[PASS] $1"; PASS=$((PASS + 1)); }
_fail() { echo "[FAIL] $1 — expected pattern: $2"; FAIL=$((FAIL + 1)); }

# assert_contains FILE PATTERN DESCRIPTION
assert_contains() {
  local file="$1" pattern="$2" desc="$3"
  if grep -qE "$pattern" "$file" 2>/dev/null; then
    _pass "$desc"
  else
    _fail "$desc" "$pattern"
  fi
}

echo "========================================"
echo "/architecture command — acceptance test"
echo "Artifact: $ARTIFACT"
echo "========================================"
echo ""

# ── Check 1: file exists and is non-empty ────────────────────────────────────
echo "--- Check 1: file existence ---"
if [[ -s "$ARTIFACT" ]]; then
  _pass "File exists and is non-empty"
else
  if [[ ! -e "$ARTIFACT" ]]; then
    echo "[FAIL] File does not exist: $ARTIFACT"
  else
    echo "[FAIL] File exists but is empty: $ARTIFACT"
  fi
  FAIL=$((FAIL + 1))
fi
echo ""

# Guard: remaining checks only make sense if file is readable
if [[ ! -r "$ARTIFACT" ]]; then
  echo "FATAL: Cannot read artifact — aborting remaining checks."
  exit 1
fi

# ── Check 2: YAML frontmatter with description: field ───────────────────────
echo "--- Check 2: YAML frontmatter ---"
assert_contains "$ARTIFACT" '^description:' \
  "Has YAML frontmatter 'description:' field"
echo ""

# ── Check 3: MAP phase / Phase 1 with 4 parallel agents ─────────────────────
echo "--- Check 3: MAP phase ---"
assert_contains "$ARTIFACT" '(MAP|Phase 1|Phase I)' \
  "Mentions MAP phase or Phase 1"
# 4 parallel agents: look for the digit 4 near words like agent/parallel/spawn
assert_contains "$ARTIFACT" '4.*(agent|parallel|spawn)|( agent|parallel|spawn).*\b4\b' \
  "References 4 parallel agents"
echo ""

# ── Check 4: 4 agent roles present ──────────────────────────────────────────
echo "--- Check 4: agent roles ---"
assert_contains "$ARTIFACT" '(DB|[Dd]atabase)' \
  "Mentions DB/database agent role"
assert_contains "$ARTIFACT" '(API|[Ee]ndpoint)' \
  "Mentions API/endpoint agent role"
assert_contains "$ARTIFACT" '([Ll]ogic|[Ff]unction)' \
  "Mentions Logic/function agent role"
assert_contains "$ARTIFACT" '([Ii]ntegration|[Ee]xternal)' \
  "Mentions Integration/external agent role"
echo ""

# ── Check 5: REDUCE phase / Phase 2 with Architect synthesizer ──────────────
echo "--- Check 5: REDUCE phase ---"
assert_contains "$ARTIFACT" '(REDUCE|Phase 2|Phase II)' \
  "Mentions REDUCE phase or Phase 2"
assert_contains "$ARTIFACT" '([Aa]rchitect|[Ss]ynthesize|[Ss]ynthesizer)' \
  "Mentions Architect synthesizer"
echo ""

# ── Check 6: ARCHITECTURE.md as output ──────────────────────────────────────
echo "--- Check 6: ARCHITECTURE.md output ---"
assert_contains "$ARTIFACT" 'ARCHITECTURE\.md' \
  "Mentions ARCHITECTURE.md as output"
echo ""

# ── Check 7: dep_manifest.json as output ────────────────────────────────────
echo "--- Check 7: dep_manifest.json output ---"
assert_contains "$ARTIFACT" 'dep_manifest\.json' \
  "Mentions dep_manifest.json as output"
echo ""

# ── Check 8: Mermaid diagram format ─────────────────────────────────────────
echo "--- Check 8: Mermaid ---"
assert_contains "$ARTIFACT" '[Mm]ermaid' \
  "Mentions Mermaid diagram format"
echo ""

# ── Check 9: model routing — haiku for explore, opus for architect ───────────
echo "--- Check 9: model routing ---"
assert_contains "$ARTIFACT" 'haiku' \
  "Mentions 'haiku' for explore agents"
assert_contains "$ARTIFACT" 'opus' \
  "Mentions 'opus' for architect agent"
echo ""

# ── Check 10: /start integration ────────────────────────────────────────────
echo "--- Check 10: /start integration ---"
assert_contains "$ARTIFACT" '(/start|auto.?invoke|missing.*architecture|architecture.*missing)' \
  "Mentions /start integration or auto-invoke when architecture missing"
echo ""

# ── Check 11: --update option ───────────────────────────────────────────────
echo "--- Check 11: --update option ---"
assert_contains "$ARTIFACT" '\-\-update' \
  "Mentions --update option for existing docs"
echo ""

# ── Check 12: dependency table ──────────────────────────────────────────────
echo "--- Check 12: dependency table ---"
assert_contains "$ARTIFACT" '([Dd]ependency table|[Dd]ependencies|[Dd]ependency)' \
  "Mentions dependency table or dependency reference"
echo ""

# ── Check 13: data flow diagrams ────────────────────────────────────────────
echo "--- Check 13: data flow ---"
assert_contains "$ARTIFACT" '([Dd]ata [Ff]low|[Dd]ataflow)' \
  "Mentions data flow diagrams"
echo ""

# ── Summary ──────────────────────────────────────────────────────────────────
echo "========================================"
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "========================================"

if [[ "$FAIL" -gt 0 ]]; then
  echo "ACCEPTANCE TEST FAILED"
  exit 1
fi

echo "ACCEPTANCE TEST PASSED"
exit 0
