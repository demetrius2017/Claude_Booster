#!/usr/bin/env bash
# Smoke test for /audit external-review routing.
#
# This intentionally keeps the scope at the Booster command layer. It verifies
# that the installed audit command can select PAL, GLM-5.1 through zai_cli.py,
# Grok through grok_cli.py, or an explicit DEGRADED path without running a full
# repository audit.

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT/templates/commands/audit.md"
INSTALLED="$HOME/.claude/commands/audit.md"
SKILL_REF="$HOME/.agents/skills/booster-command/references/commands/audit.md"
ZAI_SCRIPT="$ROOT/templates/scripts/zai_cli.py"
GROK_SCRIPT="$ROOT/templates/scripts/grok_cli.py"

# Isolation: the smoke cases below invoke zai_cli.py / grok_cli.py, which append
# provider-failure events and model metrics. Redirect both sinks into a
# disposable directory so the developer's ~/.claude state is never touched.
TMP_ISOLATION="$(mktemp -d "${TMPDIR:-/tmp}/audit_zai_smoke.XXXXXX")"
trap 'rm -rf "$TMP_ISOLATION"' EXIT
export CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG="$TMP_ISOLATION/provider_failures.jsonl"
export CLAUDE_BOOSTER_METRICS_DB="$TMP_ISOLATION/metrics.db"

TOTAL=14
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

if contains "$TEMPLATE" "Z.ai GLM-5.1"; then
    pass "C5 Z.ai GLM-5.1 third-model path is documented"
else
    fail "C5 audit command does not mention Z.ai GLM-5.1"
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

if contains "$TEMPLATE" "grok_cli.py review --model grok-4.6 --budget-turns 8"; then
    pass "C8 audit command invokes Grok-4.6 read-only lane"
else
    fail "C8 audit command does not invoke Grok-4.6"
fi

if contains "$TEMPLATE" "grok_cli.py status\` exits 0 (127 = binary missing, 69 = not authenticated)"; then
    pass "C9 Grok availability is gated on grok_cli.py status exit codes"
else
    fail "C9 audit command does not gate Grok on grok_cli.py status exit codes"
fi

if cmp -s "$TEMPLATE" "$INSTALLED"; then
    pass "C10 installed audit command matches template"
else
    fail "C10 installed audit command differs from template"
fi

if printf 'Reply GLM_OK\n' | env -u ZAI_API_KEY ZAI_API_KEY_FILE="$TMP_ISOLATION"/claude-booster-missing-zai-key python3 "$ZAI_SCRIPT" smoke >"$TMP_ISOLATION"/audit_zai_smoke.out 2>"$TMP_ISOLATION"/audit_zai_smoke.err; then
    fail "C11 zai_cli.py smoke unexpectedly succeeded without any credential source"
else
    rc=$?
    if [[ "$rc" -eq 64 ]] && grep -Fq "missing ZAI_API_KEY" "$TMP_ISOLATION"/audit_zai_smoke.err; then
        pass "C11 missing env and secret file returns deterministic degraded signal"
    else
        fail "C11 expected exit 64 for missing env and secret file, got $rc"
    fi
fi

if python3 "$GROK_SCRIPT" smoke </dev/null >"$TMP_ISOLATION"/audit_grok_smoke.out 2>"$TMP_ISOLATION"/audit_grok_smoke.err; then
    fail "C12 grok_cli.py smoke unexpectedly accepted empty stdin"
else
    rc=$?
    if [[ "$rc" -eq 65 ]] && grep -Fq "empty stdin prompt" "$TMP_ISOLATION"/audit_grok_smoke.err; then
        pass "C12 empty Grok prompt returns deterministic degraded signal"
    else
        fail "C12 expected exit 65 for empty Grok prompt, got $rc"
    fi
fi

if contains "$TEMPLATE" "Grok: grok_cli.py status exit 69 (not authenticated)"; then
    pass "C13 DEGRADED label names a concrete Grok failure class"
else
    fail "C13 DEGRADED label does not name a concrete Grok failure class"
fi

if GROK_BIN="/nonexistent/claude-booster-missing-grok" GROK_AUTH_FILE="$TMP_ISOLATION"/claude-booster-missing-grok-auth.json \
        CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG="$TMP_ISOLATION"/audit_grok_status_events.jsonl \
        GROK_CLI_DISABLE_TELEMETRY=1 \
        python3 "$GROK_SCRIPT" status >"$TMP_ISOLATION"/audit_grok_status.out 2>"$TMP_ISOLATION"/audit_grok_status.err; then
    fail "C14 grok_cli.py status unexpectedly reported available without a binary"
else
    rc=$?
    if [[ "$rc" -eq 127 ]] && grep -Fq "status=unavailable reason=binary_missing" "$TMP_ISOLATION"/audit_grok_status.out; then
        pass "C14 grok_cli.py status returns 127 when the binary is missing"
    else
        fail "C14 expected exit 127 and binary_missing status line, got $rc"
    fi
fi


echo
echo "  Result: PASS=${PASS} FAIL=${FAIL}"
if [[ "$PASS" -eq "$TOTAL" && "$FAIL" -eq 0 ]]; then
    exit 0
fi
exit 1
