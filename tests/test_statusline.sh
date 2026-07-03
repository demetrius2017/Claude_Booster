#!/usr/bin/env bash
# Acceptance test for the statusline feature.
# Verifies observable behavior of statusline.sh, settings.json.template, and install.py.
# Exit 0 if all assertions pass, non-zero on any failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && git rev-parse --show-toplevel)"
TMPL_SCRIPTS="$REPO_ROOT/templates/scripts"
STATUSLINE="$TMPL_SCRIPTS/statusline.sh"
INSTALLED_SCRIPTS="$HOME/.claude/scripts"
INSTALLED_STATUSLINE="$INSTALLED_SCRIPTS/statusline.sh"

PASS=0
FAIL=0

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

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
        pass "$label"
    else
        fail "$label" "$path to exist" "file not found"
    fi
}

assert_file_executable() {
    local label="$1" path="$2"
    if [[ -x "$path" ]]; then
        pass "$label"
    else
        fail "$label" "$path to be executable" "not executable or missing"
    fi
}

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        pass "$label"
    else
        fail "$label" "$expected" "$actual"
    fi
}

assert_exit_zero() {
    local label="$1" code="$2"
    if [[ "$code" -eq 0 ]]; then
        pass "$label"
    else
        fail "$label" "exit code 0" "exit code $code"
    fi
}

assert_contains() {
    local label="$1" file="$2" pattern="$3"
    if grep -qF "$pattern" "$file" 2>/dev/null; then
        pass "$label — '$pattern' found in $(basename "$file")"
    else
        fail "$label — contains '$pattern'" "string present in $(basename "$file")" "string NOT found"
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

# ─────────────────────────────────────────────────────────────
# Temp dir management
# ─────────────────────────────────────────────────────────────

TMPDIR_LIST=()

make_tmpdir() {
    local d
    d="$(mktemp -d)"
    TMPDIR_LIST+=("$d")
    echo "$d"
}

cleanup() {
    local d
    for d in "${TMPDIR_LIST[@]+"${TMPDIR_LIST[@]}"}"; do
        [[ -d "$d" ]] && rm -rf "$d" || true
    done
}
trap cleanup EXIT

STATUSLINE_TEST_HOME="$(make_tmpdir)/home"
mkdir -p "$STATUSLINE_TEST_HOME"

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 1: File existence and permissions ==="
# ─────────────────────────────────────────────────────────────

assert_file_exists    "templates/scripts/statusline.sh exists"   "$STATUSLINE"
assert_file_executable "templates/scripts/statusline.sh is +x"  "$STATUSLINE"

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 2: Phase reading ==="
# ─────────────────────────────────────────────────────────────

# Test 2a: .claude/.phase contains "IMPLEMENT" → output is "[IMPLEMENT]"
TMPDIR_A="$(make_tmpdir)"
mkdir -p "$TMPDIR_A/.claude"
printf 'IMPLEMENT' > "$TMPDIR_A/.claude/.phase"

PHASE_OUTPUT=""
PHASE_EXIT=0
PHASE_OUTPUT=$(cd "$TMPDIR_A" && HOME="$STATUSLINE_TEST_HOME" bash "$STATUSLINE") || PHASE_EXIT=$?

assert_exit_zero "phase read — exit code is 0 when .phase=IMPLEMENT" "$PHASE_EXIT"
assert_eq        "phase read — output is [IMPLEMENT]"                 "[IMPLEMENT]" "$PHASE_OUTPUT"

# Test 2b: no .claude/.phase at all → output defaults to "[RECON]"
TMPDIR_B="$(make_tmpdir)"

DEFAULT_OUTPUT=""
DEFAULT_EXIT=0
DEFAULT_OUTPUT=$(cd "$TMPDIR_B" && HOME="$STATUSLINE_TEST_HOME" bash "$STATUSLINE") || DEFAULT_EXIT=$?

assert_exit_zero "default phase — exit code is 0 when .phase missing" "$DEFAULT_EXIT"
assert_eq        "default phase — output is [RECON] when .phase missing" "[RECON]" "$DEFAULT_OUTPUT"

# Test 2c: .claude/.phase is empty → output defaults to "[RECON]"
TMPDIR_C="$(make_tmpdir)"
mkdir -p "$TMPDIR_C/.claude"
printf '' > "$TMPDIR_C/.claude/.phase"

EMPTY_OUTPUT=""
EMPTY_EXIT=0
EMPTY_OUTPUT=$(cd "$TMPDIR_C" && HOME="$STATUSLINE_TEST_HOME" bash "$STATUSLINE") || EMPTY_EXIT=$?

assert_exit_zero "empty phase — exit code is 0 when .phase is empty" "$EMPTY_EXIT"
assert_eq        "empty phase — output is [RECON] when .phase is empty" "[RECON]" "$EMPTY_OUTPUT"

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 3: Output format — exactly one [PHASE_NAME] line ==="
# ─────────────────────────────────────────────────────────────

# Reuse TMPDIR_A for a known-phase check; verify format
LINE_COUNT=$(cd "$TMPDIR_A" && HOME="$STATUSLINE_TEST_HOME" bash "$STATUSLINE" | wc -l | tr -d ' ')
assert_eq "phase output — exactly one line" "1" "$LINE_COUNT"

# The single output line must match the pattern [A-Z_]+
FORMAT_CHECK=$(cd "$TMPDIR_A" && HOME="$STATUSLINE_TEST_HOME" bash "$STATUSLINE")
if echo "$FORMAT_CHECK" | grep -qE '^\[[A-Z_]+\]$'; then
    pass "phase output — matches [PHASE_NAME] format"
else
    fail "phase output — matches [PHASE_NAME] format" "matches ^\[[A-Z_]+\]$" "$FORMAT_CHECK"
fi

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 4: settings.json.template contains statusLine key ==="
# ─────────────────────────────────────────────────────────────

SETTINGS_TMPL="$REPO_ROOT/templates/settings.json.template"
assert_file_exists "templates/settings.json.template exists" "$SETTINGS_TMPL"
assert_contains    "settings.json.template has statusLine key" "$SETTINGS_TMPL" '"statusLine"'

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 5: install.py seeded-keys tuple contains statusLine ==="
# ─────────────────────────────────────────────────────────────

INSTALL_PY="$REPO_ROOT/install.py"
assert_file_exists "install.py exists" "$INSTALL_PY"
assert_contains    "install.py seeded-keys has statusLine" "$INSTALL_PY" '"statusLine"'

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Section 6: Installed copy matches template ==="
# ─────────────────────────────────────────────────────────────

assert_file_exists "~/.claude/scripts/statusline.sh installed" "$INSTALLED_STATUSLINE"
assert_files_identical "statusline.sh template vs installed" "$STATUSLINE" "$INSTALLED_STATUSLINE"

# ─────────────────────────────────────────────────────────────
echo ""
echo "=== Results ==="
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
echo ""

if [[ "$FAIL" -gt 0 ]]; then
    echo "RESULT: FAIL ($FAIL assertion(s) failed)"
    exit 1
else
    echo "RESULT: PASS"
    exit 0
fi
