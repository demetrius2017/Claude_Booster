#!/usr/bin/env bash
# Smoke test for /audit external-review routing.
#
# This intentionally keeps the scope at the Booster command layer. It verifies
# that the installed audit command can select PAL, GLM-5.2 through zai_cli.py,
# Grok through grok_cli.py, or an explicit DEGRADED path without running a full
# repository audit.

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT/templates/commands/audit.md"
INSTALLED="$HOME/.claude/commands/audit.md"
SKILL_REF="$HOME/.agents/skills/booster-command/references/commands/audit.md"
ZAI_SCRIPT="$ROOT/templates/scripts/zai_cli.py"
GROK_SCRIPT="$ROOT/templates/scripts/grok_cli.py"

TOTAL=12
PASS=0
FAIL=0

pass() {
    PASS=$((PASS + 1))
    printf '  PASS  %s\n' "$1"
}

fail() {
    FAIL=$((FAIL + 1))
    printf '  FAIL  %s\n' "$1"
}

contains() {
    local file="$1"
    local pattern="$2"
    grep -Fq "$pattern" "$file"
}

echo "  /audit Z.ai smoke — command-layer routing (${TOTAL} cases)"
echo

if [[ -f "$TEMPLATE" ]]; then
    pass "C1 template audit command exists"
else
    fail "C1 missing template audit command: $TEMPLATE"
fi

if [[ -f "$INSTALLED" ]]; then
    pass "C2 installed audit command exists"
else
    fail "C2 missing installed audit command: $INSTALLED"
fi

if [[ -f "$SKILL_REF" ]]; then
    pass "C3 skill reference audit command exists"
else
    fail "C3 missing skill reference audit command: $SKILL_REF"
fi

if contains "$TEMPLATE" "PAL/GPT"; then
    pass "C4 PAL/GPT primary external expert is documented"
else
    fail "C4 audit command does not mention PAL/GPT primary path"
fi

if contains "$TEMPLATE" "Z.ai GLM-5.2"; then
    pass "C5 Z.ai GLM-5.2 third-model path is documented"
else
    fail "C5 audit command does not mention Z.ai GLM-5.2"
fi

if contains "$TEMPLATE" "ZAI_API_KEY"; then
    pass "C6 ZAI_API_KEY availability gate is documented"
else
    fail "C6 audit command does not mention ZAI_API_KEY"
fi

if contains "$TEMPLATE" "zai_cli.py review --budget 5"; then
    pass "C7 audit command invokes zai_cli.py review read-only lane"
else
    fail "C7 audit command does not invoke zai_cli.py review --budget 5"
fi

if contains "$TEMPLATE" "grok_cli.py review --budget-turns 3"; then
    pass "C8 audit command invokes grok_cli.py review read-only lane"
else
    fail "C8 audit command does not invoke grok_cli.py review --budget-turns 3"
fi

if contains "$TEMPLATE" "Grok unauthenticated"; then
    pass "C9 DEGRADED external-review path includes Grok"
else
    fail "C9 audit command does not include Grok in DEGRADED path"
fi

if cmp -s "$TEMPLATE" "$INSTALLED"; then
    pass "C10 installed audit command matches template"
else
    fail "C10 installed audit command differs from template"
fi

if printf 'Reply GLM_OK\n' | env -u ZAI_API_KEY ZAI_API_KEY_FILE=/tmp/claude-booster-missing-zai-key python3 "$ZAI_SCRIPT" smoke >/tmp/audit_zai_smoke.out 2>/tmp/audit_zai_smoke.err; then
    fail "C11 zai_cli.py smoke unexpectedly succeeded without any credential source"
else
    rc=$?
    if [[ "$rc" -eq 64 ]] && grep -Fq "missing ZAI_API_KEY" /tmp/audit_zai_smoke.err; then
        pass "C11 missing env and secret file returns deterministic degraded signal"
    else
        fail "C11 expected exit 64 for missing env and secret file, got $rc"
    fi
fi

if python3 "$GROK_SCRIPT" smoke </dev/null >/tmp/audit_grok_smoke.out 2>/tmp/audit_grok_smoke.err; then
    fail "C12 grok_cli.py smoke unexpectedly accepted empty stdin"
else
    rc=$?
    if [[ "$rc" -eq 65 ]] && grep -Fq "empty stdin prompt" /tmp/audit_grok_smoke.err; then
        pass "C12 empty Grok prompt returns deterministic degraded signal"
    else
        fail "C12 expected exit 65 for empty Grok prompt, got $rc"
    fi
fi

rm -f /tmp/audit_zai_smoke.out /tmp/audit_zai_smoke.err
rm -f /tmp/audit_grok_smoke.out /tmp/audit_grok_smoke.err

echo
echo "  Result: PASS=${PASS} FAIL=${FAIL}"
if [[ "$PASS" -eq "$TOTAL" && "$FAIL" -eq 0 ]]; then
    exit 0
fi
exit 1
