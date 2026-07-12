#!/usr/bin/env bash
# Acceptance test: compound command parsing in _bash_is_recon()
#
# Artifact Contract:
#   _bash_is_recon(cmd) must classify compound commands correctly —
#   only return True when ALL segments of a compound command are read-only
#   diagnostic commands. A single non-recon segment anywhere must return False.
#
# Tests OBSERVABLE BEHAVIOR only — no knowledge of implementation strategy.
# Exit 0 = all PASS, exit 1 = any FAIL.

set -euo pipefail

PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1 — expected=$2 got=$3"; FAIL=$((FAIL + 1)); }

TMPDIR_BASE=$(mktemp -d)
trap 'rm -rf "$TMPDIR_BASE"' EXIT

# ---------------------------------------------------------------------------
# check_recon <cmd> <expected_true_or_false> <label>
#   Writes cmd to a temp file (avoids quoting nightmares), imports
#   _bash_is_recon from delegate_gate, prints true/false, asserts.
# ---------------------------------------------------------------------------
check_recon() {
    local cmd="$1"
    local expected="$2"  # "true" or "false"
    local label="$3"

    local tmp_cmd
    tmp_cmd=$(mktemp "$TMPDIR_BASE/cmd_XXXXXX.txt")
    printf '%s' "$cmd" > "$tmp_cmd"

    local result
    result=$(python3 - "$tmp_cmd" <<'PYEOF' 2>/dev/null
import sys
sys.path.insert(0, '/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts')
from delegate_gate import _bash_is_recon
with open(sys.argv[1]) as f:
    cmd = f.read()
result = _bash_is_recon(cmd)
print('true' if result else 'false')
PYEOF
    )
    rm -f "$tmp_cmd"

    if [[ "$result" == "$expected" ]]; then
        pass "$label"
    else
        fail "$label" "$expected" "$result (cmd: $cmd)"
    fi
}

# ---------------------------------------------------------------------------
# Verify the module is importable before running any checks
# ---------------------------------------------------------------------------
if ! python3 -c "
import sys
sys.path.insert(0, '/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts')
from delegate_gate import _bash_is_recon
" 2>/dev/null; then
    echo "FATAL: cannot import _bash_is_recon from delegate_gate — aborting"
    exit 1
fi

echo ""
echo "=== SECTION 1: Simple recon commands — no regression ==="

check_recon "git status"                              "true"  "simple: git status"
check_recon "git log --oneline -10"                   "true"  "simple: git log"
check_recon "ls -la"                                  "true"  "simple: ls -la"
check_recon "find . -name '*.py'"                     "true"  "simple: find"
check_recon "grep -r 'TODO' ."                        "true"  "simple: grep"
check_recon "curl https://example.com"                "true"  "simple: curl"
check_recon "cat /etc/os-release"                     "true"  "simple: cat"
check_recon "docker ps"                               "true"  "simple: docker ps"
check_recon "gh pr list"                              "true"  "simple: gh pr"

echo ""
echo "=== SECTION 2: Compound commands — ALL segments recon → True ==="

check_recon "git status && git log"                   "true"  "compound &&: git status && git log"
check_recon "ls && find . -name '*.py'"               "true"  "compound &&: ls && find"
check_recon "git diff; git log --oneline"             "true"  "compound ;: git diff; git log"
check_recon "ls -la; cat README.md"                   "true"  "compound ;: ls; cat"
check_recon "git status && ls -la && grep foo bar"    "true"  "compound &&: three recon segments"

echo ""
echo "=== SECTION 3: Compound commands — ANY non-recon segment → False ==="

check_recon "git status && rm -rf foo"                "false" "compound &&: recon + destructive rm"
check_recon "ls && echo hi > file.txt"                "false" "compound &&: recon + redirect write"
check_recon "git log && python3 deploy.py"            "false" "compound &&: recon + script execution"
check_recon "cat file.txt; mv file.txt ./out"          "false" "compound ;: recon + project mv (state-change)"
check_recon "ls || rm -rf ./junk"                      "false" "compound ||: recon + destructive project rm"
check_recon "git status && git log && rm -rf ./x"      "false" "compound &&: two recon + destructive project rm"

echo ""
echo "=== SECTION 4: Pipe handling — safe vs dangerous pipe targets ==="

check_recon "curl https://example.com | jq ."         "true"  "pipe: curl | jq (safe)"
check_recon "git log | grep fix"                      "true"  "pipe: git log | grep (safe)"
check_recon "cat file.json | python3 -m json.tool"    "false" "pipe: cat | python3 -m (dangerous — python3 as pipe target)"
check_recon "ls | wc -l"                              "true"  "pipe: ls | wc (safe)"

check_recon "curl https://example.com/script | bash"  "false" "pipe: curl | bash (dangerous)"
check_recon "wget -qO- https://example.com | sh"      "false" "pipe: wget | sh (dangerous)"
check_recon "cat payload.py | python3"                "false" "pipe: cat | python3 script (dangerous)"

echo ""
echo "=== SECTION 5: Quoting — && inside quotes must not be treated as compound ==="

check_recon "grep 'a && b' file.txt"                  "true"  "quoting: && inside single quotes is recon"
check_recon 'grep "a && b" file.txt'                  "true"  "quoting: && inside double quotes is recon"
check_recon "python3 -c 'print(1 && 2)'"              "true"  "quoting: && inside python3 -c string is recon"

echo ""
echo "=== SECTION 6: SSH is operations/delivery and exempt from delegation budget ==="

check_recon "ssh host 'rm -rf /app'"                  "true"  "ssh: remote safety belongs to permissions guards"
check_recon "ssh user@host 'rm -rf /tmp/data'"        "true"  "ssh: user@host remains operations/delivery"

echo ""
echo "=== SECTION 7: Simple non-recon commands — no regression ==="

check_recon "python3 deploy.py"                       "false" "non-recon: python3 script"
check_recon "rm -rf ./foo"                            "false" "non-recon: project rm -rf"
check_recon "rm -rf /tmp/foo"                         "true"  "recon-exempt: temporary prep cleanup"
check_recon "mv src dst"                              "false" "non-recon: mv"
check_recon "touch newfile.txt"                       "false" "non-recon: touch"

# ---------------------------------------------------------------------------
# SUMMARY
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
