#!/usr/bin/env bash
# Acceptance test for go_gate.py
#
# Tests observable behavior from the Artifact Contract.
# Does NOT test implementation details — only what an external observer sees.
#
# Exit code: 0 if ALL assertions pass, 1 if ANY fail.
# Works on macOS bash 3.2+.

set -u

GATE="$( cd "$( dirname "$0" )" && pwd )/go_gate.py"

# ── Locate templates/scripts so _gate_common and model_tag_enforcer are importable ─
SCRIPT_DIR="$( cd "$( dirname "$0" )" && pwd )"

# ── Temp workspace ──────────────────────────────────────────────────────────────
TMPDIR_TEST="$(mktemp -d /tmp/test_go_gate_XXXXXX)"

# .claude subdir (phase, go_active marker live here)
CLAUDE_DIR="${TMPDIR_TEST}/.claude"
mkdir -p "${CLAUDE_DIR}"

# logs dir (go_gate_decisions.jsonl goes here via CLAUDE_HOME)
LOGS_DIR="${TMPDIR_TEST}/logs"
mkdir -p "${LOGS_DIR}"

# Export CLAUDE_HOME so _gate_common.logs_dir() writes to our temp dir
export CLAUDE_HOME="${TMPDIR_TEST}"

# Export PYTHONPATH so the gate can find _gate_common and model_tag_enforcer
export PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH:-}"

# ── Counters ────────────────────────────────────────────────────────────────────
PASS=0
FAIL=0

# ── Helpers ─────────────────────────────────────────────────────────────────────

# Run go_gate.py with given stdin JSON.
# Returns: sets GATE_EXIT and GATE_STDERR globals.
run_gate() {
    local json="$1"
    local env_prefix="${2:-}"
    GATE_STDERR=""
    if [ -n "$env_prefix" ]; then
        GATE_STDERR=$(eval "${env_prefix} python3 '${GATE}'" 2>&1 <<< "${json}"); GATE_EXIT=$?
    else
        GATE_STDERR=$(python3 "${GATE}" 2>&1 <<< "${json}"); GATE_EXIT=$?
    fi
}

assert_exit() {
    local label="$1"
    local expected="$2"
    local actual="$3"
    if [ "$actual" -eq "$expected" ]; then
        echo "[PASS] assertion: ${label}"
        PASS=$((PASS + 1))
    else
        echo "[FAIL] assertion: ${label} — expected exit ${expected}, got ${actual}"
        FAIL=$((FAIL + 1))
    fi
}

assert_stderr_contains() {
    local label="$1"
    local pattern="$2"
    local actual="$3"
    if echo "${actual}" | grep -qF "${pattern}"; then
        echo "[PASS] assertion: ${label}"
        PASS=$((PASS + 1))
    else
        echo "[FAIL] assertion: ${label} — expected stderr to contain '${pattern}', got: ${actual}"
        FAIL=$((FAIL + 1))
    fi
}

set_phase() {
    echo "$1" > "${CLAUDE_DIR}/.phase"
}

clear_phase() {
    rm -f "${CLAUDE_DIR}/.phase"
}

set_go_active() {
    touch "${CLAUDE_DIR}/.go_active"
}

clear_go_active() {
    rm -f "${CLAUDE_DIR}/.go_active"
}

# JSON helpers — produce minimal valid PreToolUse payloads
# cwd is set to TMPDIR_TEST so the gate walks up to find .claude/
agent_json() {
    local description="$1"
    local subagent_type="${2:-}"
    local agent_id="${3:-}"
    printf '{"tool_name":"Agent","tool_input":{"description":"%s","subagent_type":"%s"},"agent_id":"%s","cwd":"%s","session_id":"test-session"}' \
        "$description" "$subagent_type" "$agent_id" "$TMPDIR_TEST"
}

non_agent_json() {
    local tool="$1"
    printf '{"tool_name":"%s","tool_input":{"command":"ls"},"agent_id":"","cwd":"%s","session_id":"test-session"}' \
        "$tool" "$TMPDIR_TEST"
}

# ── Guard: artifact must exist ──────────────────────────────────────────────────
if [ ! -f "${GATE}" ]; then
    echo "[FAIL] SETUP: go_gate.py not found at ${GATE}"
    echo "Results: 0 passed, 1 failed"
    exit 1
fi

# ═══════════════════════════════════════════════════════════════════════════════
# TEST CASES
# ═══════════════════════════════════════════════════════════════════════════════

# ── Assertion 1: implement + IMPLEMENT phase + no marker → exit 2 + stderr ──────
set_phase "IMPLEMENT"
clear_go_active
run_gate "$(agent_json 'implement the fix')"
assert_exit "VA1: implement+IMPLEMENT+no marker → exit 2" 2 "$GATE_EXIT"
assert_stderr_contains "VA1: stderr contains 'Use /go'" "Use /go" "$GATE_STDERR"

# ── Assertion 2: implement + IMPLEMENT + .go_active present → exit 0 ────────────
set_phase "IMPLEMENT"
set_go_active
run_gate "$(agent_json 'implement the fix')"
assert_exit "VA2: implement+IMPLEMENT+marker present → exit 0" 0 "$GATE_EXIT"
clear_go_active

# ── Assertion 3: Explore type + implement phrase + IMPLEMENT + no marker → exit 0
set_phase "IMPLEMENT"
clear_go_active
run_gate "$(agent_json 'explore and add context' 'Explore')"
assert_exit "VA3: Explore subagent_type + coding desc + IMPLEMENT → exit 0" 0 "$GATE_EXIT"

# ── Assertion 4: Plan type + IMPLEMENT + no marker → exit 0 ─────────────────────
set_phase "IMPLEMENT"
clear_go_active
run_gate "$(agent_json 'plan the update strategy' 'Plan')"
assert_exit "VA4: Plan subagent_type + IMPLEMENT + no marker → exit 0" 0 "$GATE_EXIT"

# ── Assertion 5: coding keywords + RECON phase + no marker → exit 0 ─────────────
set_phase "RECON"
clear_go_active
run_gate "$(agent_json 'implement the feature')"
assert_exit "VA5: coding keywords + RECON phase → exit 0" 0 "$GATE_EXIT"

# ── Assertion 6: coding keywords + no .phase file + no marker → exit 0 ──────────
clear_phase
clear_go_active
run_gate "$(agent_json 'implement the feature')"
assert_exit "VA6: coding keywords + missing .phase file → exit 0" 0 "$GATE_EXIT"

# ── Assertion 7: sub-agent (agent_id set) + coding + IMPLEMENT + no marker → 0 ──
set_phase "IMPLEMENT"
clear_go_active
run_gate "$(agent_json 'implement the feature' '' 'abc123')"
assert_exit "VA7: sub-agent (agent_id=abc123) + coding + IMPLEMENT → exit 0" 0 "$GATE_EXIT"

# ── Assertion 8: bypass env var + all blocking conditions → exit 0 ───────────────
set_phase "IMPLEMENT"
clear_go_active
GATE_STDERR=""
GATE_STDERR=$(CLAUDE_BOOSTER_SKIP_GO_GATE=1 python3 "${GATE}" 2>&1 <<< "$(agent_json 'implement the feature')"); GATE_EXIT=$?
assert_exit "VA8: CLAUDE_BOOSTER_SKIP_GO_GATE=1 + blocking conditions → exit 0" 0 "$GATE_EXIT"

# ── Assertion 9: malformed stdin → exit 0 (fail-open) ───────────────────────────
set_phase "IMPLEMENT"
clear_go_active
GATE_STDERR=$(python3 "${GATE}" 2>&1 <<< "NOT_VALID_JSON{{{}}}"); GATE_EXIT=$?
assert_exit "VA9: malformed JSON stdin → exit 0 (fail-open)" 0 "$GATE_EXIT"

# ── Assertion 10: non-Agent tool (Bash) → exit 0 ────────────────────────────────
set_phase "IMPLEMENT"
clear_go_active
run_gate "$(non_agent_json 'Bash')"
assert_exit "VA10: Bash tool (not Agent) → exit 0" 0 "$GATE_EXIT"

# ── Assertion 11: general-purpose + coding desc + IMPLEMENT + no marker → exit 2 ─
set_phase "IMPLEMENT"
clear_go_active
run_gate "$(agent_json 'write code for feature X')"
assert_exit "VA11: general-purpose + 'write code' + IMPLEMENT + no marker → exit 2" 2 "$GATE_EXIT"

# ── Assertion 12: decisions logged to go_gate_decisions.jsonl ────────────────────
# Run a blocking case and a passing case; both should produce log entries.
set_phase "IMPLEMENT"
clear_go_active
run_gate "$(agent_json 'implement the fix')"          # should block → exit 2
set_go_active
run_gate "$(agent_json 'implement the fix')"          # marker present → exit 0
clear_go_active

LOG_FILE="${LOGS_DIR}/go_gate_decisions.jsonl"
if [ -f "${LOG_FILE}" ] && [ "$(wc -l < "${LOG_FILE}")" -ge 2 ]; then
    echo "[PASS] assertion: VA12: decisions logged (at least 2 lines in go_gate_decisions.jsonl)"
    PASS=$((PASS + 1))
else
    LINES=0
    if [ -f "${LOG_FILE}" ]; then
        LINES=$(wc -l < "${LOG_FILE}")
    fi
    echo "[FAIL] assertion: VA12: decisions logged — expected ≥2 lines in ${LOG_FILE}, got ${LINES}"
    FAIL=$((FAIL + 1))
fi

# ═══════════════════════════════════════════════════════════════════════════════
# INVARIANT CHECKS
# ═══════════════════════════════════════════════════════════════════════════════

# ── Invariant: fail_open_on_exception — empty stdin → exit 0 ────────────────────
set_phase "IMPLEMENT"
clear_go_active
GATE_STDERR=$(python3 "${GATE}" 2>&1 <<< ""); GATE_EXIT=$?
assert_exit "INV: fail_open — empty stdin → exit 0" 0 "$GATE_EXIT"

# ── Invariant: non_agent_tools_always_allowed ───────────────────────────────────
set_phase "IMPLEMENT"
clear_go_active
for tool_name in Edit Write Bash TaskCreate; do
    run_gate "$(non_agent_json "${tool_name}")"
    assert_exit "INV: non-Agent tool '${tool_name}' always allowed" 0 "$GATE_EXIT"
done

# ── Invariant: explore_plan_never_blocked ───────────────────────────────────────
set_phase "IMPLEMENT"
clear_go_active
for st in Explore Plan; do
    run_gate "$(agent_json 'implement refactor fix write code' "${st}")"
    assert_exit "INV: subagent_type=${st} never blocked (even with all coding keywords)" 0 "$GATE_EXIT"
done

# ── Invariant: non_implement_phase_never_blocked ────────────────────────────────
clear_go_active
for phase in RECON PLAN AUDIT VERIFY MERGE; do
    set_phase "${phase}"
    run_gate "$(agent_json 'implement the fix')"
    assert_exit "INV: phase=${phase} (non-IMPLEMENT) → exit 0" 0 "$GATE_EXIT"
done

# ── Invariant: marker_present_always_allows ──────────────────────────────────────
set_phase "IMPLEMENT"
set_go_active
run_gate "$(agent_json 'implement fix apply edit modify write code')"
assert_exit "INV: marker present → exit 0 regardless of description" 0 "$GATE_EXIT"
clear_go_active

# ── Invariant: block_requires_all_four_conditions ───────────────────────────────
# Condition A missing: non-coding description (but IMPLEMENT, no marker)
set_phase "IMPLEMENT"
clear_go_active
run_gate "$(agent_json 'explore and look around')"
assert_exit "INV: block requires ALL conditions — non-coding desc → exit 0" 0 "$GATE_EXIT"

# Condition B missing: coding desc but not IMPLEMENT phase
set_phase "AUDIT"
clear_go_active
run_gate "$(agent_json 'implement the fix')"
assert_exit "INV: block requires ALL conditions — wrong phase → exit 0" 0 "$GATE_EXIT"

# Condition C missing: marker present even though everything else would block
set_phase "IMPLEMENT"
set_go_active
run_gate "$(agent_json 'implement the fix')"
assert_exit "INV: block requires ALL conditions — marker present → exit 0" 0 "$GATE_EXIT"
clear_go_active

# ═══════════════════════════════════════════════════════════════════════════════
# BRANCHING SCENARIOS (non-happy-path)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Branch: malformed_json — garbage stdin → exit 0 ─────────────────────────────
set_phase "IMPLEMENT"
clear_go_active
GATE_STDERR=$(python3 "${GATE}" 2>&1 <<< 'garbage{{{{'); GATE_EXIT=$?
assert_exit "BRANCH: malformed_json → exit 0 (fail-open)" 0 "$GATE_EXIT"

# ── Branch: marker_absent + all conditions met → exit 2 ─────────────────────────
set_phase "IMPLEMENT"
clear_go_active
run_gate "$(agent_json 'implement the feature')"
assert_exit "BRANCH: no marker + all conditions met → exit 2" 2 "$GATE_EXIT"

# ── Branch: false_positive_keyword — Explore agent with 'add' in description ────
# Explore type should win even when description has a coding keyword
set_phase "IMPLEMENT"
clear_go_active
run_gate "$(agent_json 'add context to exploration' 'Explore')"
assert_exit "BRANCH: false_positive_keyword — Explore type beats 'add' in desc" 0 "$GATE_EXIT"

# ── Branch: subagent_type check with agent_id ────────────────────────────────────
# agent_id set → auto-skip regardless of anything
set_phase "IMPLEMENT"
clear_go_active
run_gate "$(agent_json 'implement the fix' '' 'sub-42')"
assert_exit "BRANCH: agent_id='sub-42' → auto-skip → exit 0" 0 "$GATE_EXIT"

# ── Branch: PLAN phase with coding keywords → exit 0 ────────────────────────────
set_phase "PLAN"
clear_go_active
run_gate "$(agent_json 'implement the plan')"
assert_exit "BRANCH: PLAN phase + implement keyword → exit 0 (non-IMPLEMENT phase)" 0 "$GATE_EXIT"

# ── Branch: non-JSON (binary-ish) input → exit 0 ────────────────────────────────
set_phase "IMPLEMENT"
clear_go_active
GATE_STDERR=$(printf '\x00\x01\x02\x03' | python3 "${GATE}" 2>&1); GATE_EXIT=$?
assert_exit "BRANCH: binary/non-JSON stdin → exit 0 (fail-open)" 0 "$GATE_EXIT"

# ═══════════════════════════════════════════════════════════════════════════════
# CLEANUP
# ═══════════════════════════════════════════════════════════════════════════════
rm -rf "${TMPDIR_TEST}"

# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
