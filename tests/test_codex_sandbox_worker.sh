#!/usr/bin/env bash
# Acceptance test for codex_sandbox_worker.sh feature.
# Tests observable behavior only — no Codex binary required.
# Exit 0 if all assertions pass, non-zero on any failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && git rev-parse --show-toplevel)"
TMPL_SCRIPTS="$REPO_ROOT/templates/scripts"
TMPL_RULES="$REPO_ROOT/templates/rules"
INSTALLED_SCRIPTS="$HOME/.claude/scripts"
INSTALLED_RULES="$HOME/.claude/rules"

PASS=0
FAIL=0

pass() {
    echo "  PASS: $1"
    PASS=$((PASS + 1))
}

fail() {
    echo "  FAIL: $1"
    echo "        Expected: $2"
    echo "        Got:      $3"
    FAIL=$((FAIL + 1))
}

assert_file_exists() {
    local label="$1" path="$2"
    if [[ -f "$path" ]]; then
        pass "$label — file exists: $path"
    else
        fail "$label — file exists" "$path to exist" "file not found"
    fi
}

assert_file_executable() {
    local label="$1" path="$2"
    if [[ -x "$path" ]]; then
        pass "$label — file is executable: $path"
    else
        fail "$label — file is executable" "$path to be executable" "not executable or missing"
    fi
}

assert_files_identical() {
    local label="$1" a="$2" b="$3"
    if diff -q "$a" "$b" >/dev/null 2>&1; then
        pass "$label — template and installed are byte-identical"
    else
        fail "$label — template and installed are byte-identical" \
             "no diff between $a and $b" \
             "files differ (run: diff $a $b)"
    fi
}

assert_contains() {
    local label="$1" file="$2" pattern="$3"
    if grep -qF "$pattern" "$file" 2>/dev/null; then
        pass "$label — '$pattern' found in $(basename $file)"
    else
        fail "$label — contains '$pattern'" "string present in $(basename $file)" "string NOT found"
    fi
}

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 1: File existence and permissions ==="

assert_file_exists    "tmpl codex_sandbox_worker.sh exists"   "$TMPL_SCRIPTS/codex_sandbox_worker.sh"
assert_file_executable "tmpl codex_sandbox_worker.sh is +x"  "$TMPL_SCRIPTS/codex_sandbox_worker.sh"
assert_file_exists    "inst codex_sandbox_worker.sh exists"   "$INSTALLED_SCRIPTS/codex_sandbox_worker.sh"
assert_file_executable "inst codex_sandbox_worker.sh is +x"  "$INSTALLED_SCRIPTS/codex_sandbox_worker.sh"

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 2: Template–installed sync ==="

assert_files_identical "codex_sandbox_worker.sh in sync" \
    "$TMPL_SCRIPTS/codex_sandbox_worker.sh" \
    "$INSTALLED_SCRIPTS/codex_sandbox_worker.sh"

assert_files_identical "delegate_gate.py in sync" \
    "$TMPL_SCRIPTS/delegate_gate.py" \
    "$INSTALLED_SCRIPTS/delegate_gate.py"

assert_files_identical "model_metric_capture.py in sync" \
    "$TMPL_SCRIPTS/model_metric_capture.py" \
    "$INSTALLED_SCRIPTS/model_metric_capture.py"

assert_files_identical "tool-strategy.md in sync" \
    "$TMPL_RULES/tool-strategy.md" \
    "$INSTALLED_RULES/tool-strategy.md"

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 3: Script argument validation ==="

# No-arg invocation must exit 2 and print usage to stderr.
EXIT_CODE=0
STDERR_OUTPUT=$(bash "$TMPL_SCRIPTS/codex_sandbox_worker.sh" 2>&1 >/dev/null) || EXIT_CODE=$?

if [[ "$EXIT_CODE" -eq 2 ]]; then
    pass "no-args exits with code 2 (got $EXIT_CODE)"
else
    fail "no-args exit code" "2" "$EXIT_CODE"
fi

if echo "$STDERR_OUTPUT" | grep -qi "usage:"; then
    pass "no-args prints 'usage:' to stderr"
else
    fail "no-args stderr contains 'usage:'" "'usage:' in stderr" "got: $STDERR_OUTPUT"
fi

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 4: delegate_gate.py pattern recognition ==="

run_dg_test() {
    local label="$1"
    local cmd="$2"
    local expect="$3"   # "true" or "false"

    RESULT=$(python3 - "$cmd" "$expect" <<'PYEOF'
import sys

cmd    = sys.argv[1]
expect = sys.argv[2]   # "true" or "false"

# Add templates/scripts to sys.path so _gate_common and other local imports resolve.
scripts_dir = "/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts"
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

src = open(f"{scripts_dir}/delegate_gate.py").read()
g = {"__name__": "delegate_gate_test", "__file__": f"{scripts_dir}/delegate_gate.py"}
exec(compile(src, "delegate_gate.py", "exec"), g)

fn = g["_bash_is_codex_worker"]
result = fn(cmd)
got = "true" if result else "false"
if got == expect:
    print("PASS")
else:
    print(f"FAIL: _bash_is_codex_worker({cmd!r}) = {got}, expected {expect}")
PYEOF
    )

    if [[ "$RESULT" == "PASS" ]]; then
        pass "$label"
    else
        fail "$label" "result=$expect" "$RESULT"
    fi
}

run_dg_test "codex_sandbox_worker.sh gpt-5.3-codex → True"            "codex_sandbox_worker.sh gpt-5.3-codex"              "true"
run_dg_test "codex_sandbox_worker.sh gpt-5.5 → True"                  "codex_sandbox_worker.sh gpt-5.5"                    "true"
run_dg_test "pipe | codex_sandbox_worker.sh gpt-5.3-codex → True"     "printf '%s' 'task' | codex_sandbox_worker.sh gpt-5.3-codex" "true"
run_dg_test "grep codex_sandbox_worker.sh logs/ → False"               "grep codex_sandbox_worker.sh logs/"                 "false"
run_dg_test "vim codex_sandbox_worker.sh → False"                      "vim codex_sandbox_worker.sh"                        "false"
run_dg_test "cat codex_sandbox_worker.sh → False"                      "cat codex_sandbox_worker.sh"                        "false"

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 5: model_metric_capture.py pattern recognition ==="

run_mmc_test() {
    local label="$1"
    local cmd="$2"
    local expect="$3"   # model string or "None"

    RESULT=$(python3 - "$cmd" "$expect" <<'PYEOF'
import sys

cmd    = sys.argv[1]
expect = sys.argv[2]

# Add templates/scripts to sys.path so local imports resolve.
scripts_dir = "/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts"
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

src = open(f"{scripts_dir}/model_metric_capture.py").read()
g = {"__name__": "mmc_test", "__file__": f"{scripts_dir}/model_metric_capture.py"}
exec(compile(src, "model_metric_capture.py", "exec"), g)

fn = g["_match_codex_command"]
result = fn(cmd)
got = str(result) if result is not None else "None"
if got == expect:
    print("PASS")
else:
    print(f"FAIL: _match_codex_command({cmd!r}) = {got!r}, expected {expect!r}")
PYEOF
    )

    if [[ "$RESULT" == "PASS" ]]; then
        pass "$label"
    else
        fail "$label" "result=$expect" "$RESULT"
    fi
}

run_mmc_test "codex_sandbox_worker.sh gpt-5.5 → gpt-5.5"                 "codex_sandbox_worker.sh gpt-5.5"                 "gpt-5.5"
run_mmc_test "codex_sandbox_worker.sh gpt-5.3-codex → gpt-5.3-codex"     "codex_sandbox_worker.sh gpt-5.3-codex"           "gpt-5.3-codex"
run_mmc_test "grep codex_sandbox_worker.sh logs/ → None"                  "grep codex_sandbox_worker.sh logs/"              "None"
run_mmc_test "vim codex_sandbox_worker.sh → None"                         "vim codex_sandbox_worker.sh"                     "None"

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 6: tool-strategy.md content ==="

TOOL_STRAT="$TMPL_RULES/tool-strategy.md"

if grep -q "codex_sandbox_worker\.sh" "$TOOL_STRAT" 2>/dev/null; then
    pass "tool-strategy.md contains 'codex_sandbox_worker.sh'"
else
    fail "tool-strategy.md contains 'codex_sandbox_worker.sh'" "string present" "string NOT found in $TOOL_STRAT"
fi

if grep -qi "when to use which\|When to use which" "$TOOL_STRAT" 2>/dev/null; then
    pass "tool-strategy.md contains 'When to use which' (case-insensitive)"
else
    fail "tool-strategy.md contains 'When to use which'" "phrase present" "phrase NOT found in $TOOL_STRAT"
fi

if grep -q "codex_worker\.sh" "$TOOL_STRAT" 2>/dev/null; then
    pass "tool-strategy.md contains 'codex_worker.sh' (non-sandbox variant)"
else
    fail "tool-strategy.md contains 'codex_worker.sh'" "string present" "string NOT found in $TOOL_STRAT"
fi

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 7: Script content sanity ==="

SANDBOX="$TMPL_SCRIPTS/codex_sandbox_worker.sh"

for keyword in "worktree" "git diff" "trap" "workspace-write" "--ephemeral" "model_reasoning_effort" "CODEX_REASONING_EFFORT"; do
    if grep -qF -- "$keyword" "$SANDBOX" 2>/dev/null; then
        pass "codex_sandbox_worker.sh contains '$keyword'"
    else
        fail "codex_sandbox_worker.sh contains '$keyword'" "keyword present" "keyword NOT found in $SANDBOX"
    fi
done

# ─────────────────────────────────────────────────────────────
TOTAL=$((PASS + FAIL))
echo ""
echo "================================================"
echo "  $PASS/$TOTAL assertions passed"
echo "================================================"

if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi

exit 0
