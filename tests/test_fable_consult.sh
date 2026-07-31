#!/usr/bin/env bash
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WRAPPER="$ROOT/templates/scripts/fable_consult.sh"
IDENTITY="$ROOT/templates/scripts/fable_identity_preamble.txt"
IDENTITY_SKILL="$ROOT/templates/codex/skills/fable-identity/SKILL.md"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/test-fable-consult.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

skill_identity="$TMP_ROOT/skill-identity"
awk '
    /^```text$/ { in_block = 1; next }
    in_block && /^```$/ { exit }
    in_block { print }
' "$IDENTITY_SKILL" >"$skill_identity"
cmp -s "$IDENTITY" "$skill_identity" || fail 'skill identity block differs from canonical preamble'

FAKE="$TMP_ROOT/claude"
cat >"$FAKE" <<'EOF'
#!/usr/bin/env bash
printf '%s\0' "$@" >"$FAKE_ARGV"
cat >"$FAKE_STDIN"
if [[ -n ${FAKE_PAYLOAD+x} ]]; then
    printf '%s\n' "$FAKE_PAYLOAD"
elif [[ ${FAKE_JSON_EMPTY:-0} == 1 ]]; then
    printf '{"type":"result","subtype":"success","result":""}\n'
else
    printf '{"type":"result","subtype":"success","result":"fake stdout"}\n'
fi
printf 'fake stderr\n' >&2
exit "${FAKE_EXIT:-0}"
EOF
chmod +x "$FAKE"

export FAKE_ARGV="$TMP_ROOT/argv" FAKE_STDIN="$TMP_ROOT/stdin"
stdout="$TMP_ROOT/stdout" stderr="$TMP_ROOT/stderr"
printf 'line one\nline two\n' | CLAUDE_BIN="$FAKE" "$WRAPPER" >"$stdout" 2>"$stderr" || fail 'valid call failed'

python3 - "$FAKE_ARGV" <<'PY' || fail 'unexpected argv'
import sys
from pathlib import Path

parts = Path(sys.argv[1]).read_bytes().split(b"\0")
assert parts == [
    b"--model", b"fable", b"--print", b"--tools", b"",
    b"--output-format", b"json", b"",
], parts
PY
expected_stdin="$TMP_ROOT/expected-stdin"
{
    cat "$IDENTITY"
    printf '\n\nline one\nline two\n'
} >"$expected_stdin"
cmp -s "$expected_stdin" "$FAKE_STDIN" || fail 'identity preamble and prompt bytes differ'
[[ "$(grep -c '^# Model identity$' "$FAKE_STDIN")" == 1 ]] || fail 'identity preamble not injected exactly once'
[[ "$(cat "$stdout")" == 'fake stdout' ]] || fail 'stdout not preserved'
[[ "$(cat "$stderr")" == 'fake stderr' ]] || fail 'stderr not preserved'

set +e
printf 'prompt\n' | FAKE_JSON_EMPTY=1 CLAUDE_BIN="$FAKE" "$WRAPPER" >"$stdout" 2>"$stderr"
status=$?
set -e
[[ $status -eq 70 ]] || fail "empty successful CLI result was accepted: $status"
grep -q 'no final response text' "$stderr" || fail 'empty-result diagnostic missing'

for invalid_payload in null '[]' '"text"' '123'; do
    set +e
    printf 'prompt\n' | FAKE_PAYLOAD="$invalid_payload" CLAUDE_BIN="$FAKE" "$WRAPPER" >"$stdout" 2>"$stderr"
    status=$?
    set -e
    [[ $status -eq 70 ]] || fail "non-object JSON was accepted ($invalid_payload): $status"
    grep -q 'must be a top-level object' "$stderr" || fail "non-object diagnostic missing ($invalid_payload)"
    ! grep -q 'Traceback' "$stderr" || fail "traceback leaked for non-object JSON ($invalid_payload)"
done

set +e
printf 'prompt\n' | FAKE_PAYLOAD='{' CLAUDE_BIN="$FAKE" "$WRAPPER" >"$stdout" 2>"$stderr"
status=$?
set -e
[[ $status -eq 70 ]] || fail "malformed JSON was accepted: $status"
grep -q 'invalid Claude CLI JSON output' "$stderr" || fail 'malformed-JSON diagnostic missing'
! grep -q 'Traceback' "$stderr" || fail 'traceback leaked for malformed JSON'

set +e
printf 'prompt\n' | FAKE_EXIT=42 CLAUDE_BIN="$FAKE" "$WRAPPER" >"$stdout" 2>"$stderr"
status=$?
set -e
[[ $status -eq 42 ]] || fail "CLI exit status not preserved: $status"

rm -f "$FAKE_ARGV" "$FAKE_STDIN"
for blank_prompt in '' $'\n' $' \t\n  \r\n'; do
    rm -f "$FAKE_ARGV" "$FAKE_STDIN"
    set +e
    printf '%s' "$blank_prompt" | CLAUDE_BIN="$FAKE" "$WRAPPER" >"$stdout" 2>"$stderr"
    status=$?
    set -e
    [[ $status -eq 64 ]] || fail "blank input status: $status"
    [[ ! -e "$FAKE_ARGV" ]] || fail 'Claude CLI ran for blank input'
    grep -q 'local contract failure' "$stderr" || fail 'blank-input diagnostic missing'
done

set +e
printf 'prompt\n' | CLAUDE_BIN="$TMP_ROOT/missing-claude" "$WRAPPER" >"$stdout" 2>"$stderr"
status=$?
set -e
[[ $status -eq 69 ]] || fail "missing CLI status: $status"
grep -q 'local environment failure' "$stderr" || fail 'missing-CLI diagnostic missing'

orphan_wrapper="$TMP_ROOT/orphan-fable-consult.sh"
cp "$WRAPPER" "$orphan_wrapper"
chmod +x "$orphan_wrapper"
rm -f "$FAKE_STDIN"
set +e
printf 'prompt\n' | CLAUDE_BIN="$FAKE" "$orphan_wrapper" >"$stdout" 2>"$stderr"
status=$?
set -e
[[ $status -eq 69 ]] || fail "missing identity preamble status: $status"
grep -q 'identity preamble missing' "$stderr" || fail 'missing-identity diagnostic missing'
[[ ! -e "$FAKE_STDIN" ]] || fail 'Claude CLI ran without identity preamble'

printf 'PASS: fable_consult argv/stdin/error contract\n'
