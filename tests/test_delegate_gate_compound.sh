#!/usr/bin/env bash
# Acceptance test: compound-command parsing in _bash_is_recon()
#
# Tests observable behavior of the compound-aware _bash_is_recon logic:
#
#   RECON cases (expect: is_recon=True):
#     T1  Both segments are recon:   git status && git log
#     T2  Semicolon separator:       git status; ls
#     T3  OR operator:               git log || git status
#     T4  Simple (no compound):      git status
#     T5  Safe pipe:                 curl http://example.com | jq .
#     T6  ssh without destructive:   ssh user@host ls
#     T12 ssh with rm payload:       ssh user@host 'rm -rf /app'  (all ssh = recon)
#     T13 ssh with docker stop:      ssh host docker stop myapp   (all ssh = recon)
#     T7  Safe command substitution: git log $(git rev-parse HEAD)
#
#   NON-RECON cases (expect: is_recon=False):
#     T8  Compound bypass:           git status && rm -rf foo
#     T9  Destructive second seg:    ls && dd if=/dev/zero of=/dev/sda
#     T10 Pipe to bash:              curl http://example.com | bash
#     T11 Pipe to python3:           ls | python3
#     T14 Arbitrary command subst:   ls $(arbitrary_cmd)
#     T15 Three-segment partial:     git status && git log && rm foo
#     T16 Quote preservation:        echo "&&" (the && is inside quotes, single segment)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$REPO_ROOT/templates/scripts"
PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }

# ---------------------------------------------------------------------------
# Helper: call _bash_is_recon for a given command string.
# Prints "True" or "False" (Python bool repr).
# ---------------------------------------------------------------------------
is_recon() {
    # $1 = command string
    python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')

# Stub _gate_common so we don't need the installed version
import types
m = types.ModuleType('_gate_common')
m.BYPASS_LOG_NAME = 'bypass.jsonl'
m.DECISION_ALLOW = 'allow'
m.DECISION_AUTO_SKIP = 'auto_skip'
m.DECISION_BLOCK = 'block'
m.DECISION_ADVISORY = 'advisory'
m.DECISION_BYPASS_HONOURED = 'bypass_honoured'
m.DECISION_BYPASS_REFUSED = 'bypass_refused'
m.DELEGATE_LOG_NAME = 'delegate.jsonl'
m.append_jsonl = lambda *a, **kw: None
m.is_subagent_context = lambda d: False
m.iso_now = lambda: '2026-01-01T00:00:00Z'
m.project_root_from = lambda cwd: None
m.redact_secrets = lambda s: s
sys.modules['_gate_common'] = m

from delegate_gate import _bash_is_recon
print(_bash_is_recon(sys.argv[1]))
" "$1"
}

# ---------------------------------------------------------------------------
# RECON cases — all must return True
# ---------------------------------------------------------------------------

# T1: Both segments are recon (&&)
result=$(is_recon "git status && git log")
if [[ "$result" == "True" ]]; then
    pass "T1: 'git status && git log' → recon (both segments safe)"
else
    fail "T1: 'git status && git log' expected True, got $result"
fi

# T2: Semicolon separator, both recon
result=$(is_recon "git status; ls")
if [[ "$result" == "True" ]]; then
    pass "T2: 'git status; ls' → recon (semicolon, both safe)"
else
    fail "T2: 'git status; ls' expected True, got $result"
fi

# T3: OR operator, both recon
result=$(is_recon "git log || git status")
if [[ "$result" == "True" ]]; then
    pass "T3: 'git log || git status' → recon (|| operator, both safe)"
else
    fail "T3: 'git log || git status' expected True, got $result"
fi

# T4: Simple non-compound command
result=$(is_recon "git status")
if [[ "$result" == "True" ]]; then
    pass "T4: 'git status' → recon (simple command, unchanged behavior)"
else
    fail "T4: 'git status' expected True, got $result"
fi

# T5: Safe pipe target (curl | jq)
result=$(is_recon "curl http://example.com | jq .")
if [[ "$result" == "True" ]]; then
    pass "T5: 'curl http://example.com | jq .' → recon (safe pipe target)"
else
    fail "T5: 'curl http://example.com | jq .' expected True, got $result"
fi

# T6: ssh without destructive payload
result=$(is_recon "ssh user@host ls")
if [[ "$result" == "True" ]]; then
    pass "T6: 'ssh user@host ls' → recon (ssh, no destructive payload)"
else
    fail "T6: 'ssh user@host ls' expected True, got $result"
fi

# T7: Safe command substitution (git rev-parse)
result=$(is_recon 'git log $(git rev-parse HEAD)')
if [[ "$result" == "True" ]]; then
    pass "T7: 'git log \$(git rev-parse HEAD)' → recon (trivially safe substitution)"
else
    fail "T7: 'git log \$(git rev-parse HEAD)' expected True, got $result"
fi

# ---------------------------------------------------------------------------
# NON-RECON cases — all must return False
# ---------------------------------------------------------------------------

# T8: Classic compound bypass: recon prefix + destructive suffix
result=$(is_recon "git status && rm -rf foo")
if [[ "$result" == "False" ]]; then
    pass "T8: 'git status && rm -rf foo' → NOT recon (compound bypass blocked)"
else
    fail "T8: 'git status && rm -rf foo' expected False, got $result"
fi

# T9: Destructive second segment (dd)
result=$(is_recon "ls && dd if=/dev/zero of=/dev/sda")
if [[ "$result" == "False" ]]; then
    pass "T9: 'ls && dd if=/dev/zero of=/dev/sda' → NOT recon (destructive second segment)"
else
    fail "T9: 'ls && dd if=/dev/zero of=/dev/sda' expected False, got $result"
fi

# T10: Pipe to bash
result=$(is_recon "curl http://example.com | bash")
if [[ "$result" == "False" ]]; then
    pass "T10: 'curl http://example.com | bash' → NOT recon (unsafe pipe target)"
else
    fail "T10: 'curl http://example.com | bash' expected False, got $result"
fi

# T11: Pipe to python3
result=$(is_recon "ls | python3")
if [[ "$result" == "False" ]]; then
    pass "T11: 'ls | python3' → NOT recon (unsafe pipe target)"
else
    fail "T11: 'ls | python3' expected False, got $result"
fi

# T12: ssh with rm payload — ALL ssh is recon. The gate is workflow-discipline
# (advisory), not a remote-command safety boundary; ssh safety is permissions.deny.
result=$(is_recon "ssh user@host 'rm -rf /app'")
if [[ "$result" == "True" ]]; then
    pass "T12: \"ssh user@host 'rm -rf /app'\" → recon (all ssh is ops/delivery, exempt)"
else
    fail "T12: \"ssh user@host 'rm -rf /app'\" expected True, got $result"
fi

# T13: ssh with docker stop — all ssh is recon (see T12).
result=$(is_recon "ssh host docker stop myapp")
if [[ "$result" == "True" ]]; then
    pass "T13: 'ssh host docker stop myapp' → recon (all ssh is ops/delivery, exempt)"
else
    fail "T13: 'ssh host docker stop myapp' expected True, got $result"
fi

# T14: Arbitrary command substitution
result=$(is_recon 'ls $(arbitrary_cmd)')
if [[ "$result" == "False" ]]; then
    pass "T14: 'ls \$(arbitrary_cmd)' → NOT recon (arbitrary command substitution)"
else
    fail "T14: 'ls \$(arbitrary_cmd)' expected False, got $result"
fi

# T15: Three-segment where last is destructive
result=$(is_recon "git status && git log && rm foo")
if [[ "$result" == "False" ]]; then
    pass "T15: 'git status && git log && rm foo' → NOT recon (third segment destructive)"
else
    fail "T15: 'git status && git log && rm foo' expected False, got $result"
fi

# T16: Quoted && — single segment, treated as simple recon command
# 'echo "&&"' is one segment whose text contains && inside quotes; it should
# fall back to normal RECON_BASH_PATTERNS. echo is matched by the recon patterns.
result=$(is_recon 'echo "a && b"')
if [[ "$result" == "True" ]]; then
    pass "T16: 'echo \"a && b\"' → recon (quoted && is not a compound operator)"
else
    fail "T16: 'echo \"a && b\"' expected True (quoted &&), got $result"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS passed, $FAIL failed"

if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
exit 0
