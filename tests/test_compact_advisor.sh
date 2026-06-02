#!/usr/bin/env bash
# Acceptance test for compact_advisor.py + compact_advisor_inject.py
# Tests observable behaviour only — does NOT reference implementation internals.
#
# Covered assertions (12 original + 3 new):
#   1.  advisor: below-threshold → no marker written
#   2.  advisor: above-threshold (real usage block) → marker written
#   3.  advisor: marker contains "<observed> <window>" (two ints)
#   4.  inject:  no-marker → silent (no stdout)
#   5.  inject:  marker present → advisory injected in stdout JSON
#   6.  inject:  advisory text contains estimated token count
#   7.  inject:  advisory text shows "% of 1000k window" at default threshold
#   8.  inject:  marker deleted after inject (one-shot semantics)
#   9.  inject:  JSONL event key is "tokens" (not "token_count"/"estimated_tokens")
#   10. advisor: JSONL event key is "observed" (not "token_count"/"estimated_tokens")
#   11. inject:  malformed JSON stdin → exit 0, no crash
#   12. inject:  missing session_id field → exit 0, no crash
#  [NEW]
#   13. inject:  CLAUDE_BOOSTER_COMPACT_THRESHOLD=80000 → advisory contains ">80k"
#   14. inject:  CLAUDE_BOOSTER_COMPACT_THRESHOLD=80000 → advisory does NOT contain ">120k"
#   15. source:  grep finds no "token_count" literal in templates/scripts/compact_advisor*.py
#
# Exit 0 = all assertions passed
# Exit 1 = one or more assertions failed

set -uo pipefail

ADVISOR_PATH="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts/compact_advisor.py"
INJECT_PATH="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts/compact_advisor_inject.py"
JSONL_LOG=""   # set per-test in a tempdir

PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

pass_test() {
    local name="$1"
    PASS_COUNT=$((PASS_COUNT + 1))
    RESULTS+=("  PASS  $name")
}

fail_test() {
    local name="$1"
    local detail="${2:-}"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    RESULTS+=("  FAIL  $name${detail:+  ($detail)}")
}

# Canonical UUID used across tests
UUID="12345678-1234-1234-1234-123456789abc"

# ---------------------------------------------------------------------------
# Setup — isolated home dir so markers don't collide with real ~/.claude
# ---------------------------------------------------------------------------

FAKE_HOME="$(mktemp -d)"
FAKE_CLAUDE_DIR="$FAKE_HOME/.claude"
FAKE_LOGS_DIR="$FAKE_CLAUDE_DIR/logs"
mkdir -p "$FAKE_LOGS_DIR"

# logs_dir() writes to ~/.claude/logs/ (via _gate_common.logs_dir)
JSONL_LOG="$FAKE_LOGS_DIR/compact_advisor.jsonl"

# Scripts look for _gate_common via sys.path insertion; provide a minimal stub
SCRIPT_DIR="$(dirname "$ADVISOR_PATH")"

# Cleanup
trap 'rm -rf "$FAKE_HOME"' EXIT

# Helper: run a script with HOME pointing to FAKE_HOME so markers land there
run_advisor() {
    HOME="$FAKE_HOME" python3 "$ADVISOR_PATH" "$@"
}

run_inject() {
    HOME="$FAKE_HOME" python3 "$INJECT_PATH" "$@"
}

# ---------------------------------------------------------------------------
# ASSERTION 1 — advisor: below-threshold → no marker written
# ---------------------------------------------------------------------------

TMPFILE="$(mktemp)"
# Write 100 bytes → 100//4 = 25 tokens → well below 120000
python3 -c "import os; open('$TMPFILE','wb').write(b'x'*100)"

echo '{"session_id":"'"$UUID"'","transcript_path":"'"$TMPFILE"'","cwd":"/tmp"}' \
    | run_advisor >/dev/null 2>&1

if [[ ! -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID" ]]; then
    pass_test "advisor: below-threshold → no marker"
else
    fail_test "advisor: below-threshold → no marker" "marker unexpectedly created"
    rm -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"
fi
rm -f "$TMPFILE"

# ---------------------------------------------------------------------------
# ASSERTION 2 — advisor: above-threshold → marker written
# ---------------------------------------------------------------------------

TMPFILE2="$(mktemp)"
# Real transcript line: assistant usage sums to 660000 (10000+600000+50000) ≥ 600k default threshold.
python3 -c "
import json
line={'type':'assistant','message':{'role':'assistant','model':'claude-opus-4-8','usage':{'input_tokens':10000,'cache_read_input_tokens':600000,'cache_creation_input_tokens':50000,'output_tokens':100}},'uuid':'u1'}
open('$TMPFILE2','w').write(json.dumps(line)+'\n')
"

echo '{"session_id":"'"$UUID"'","transcript_path":"'"$TMPFILE2"'","cwd":"/tmp"}' \
    | run_advisor >/dev/null 2>&1

if [[ -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID" ]]; then
    pass_test "advisor: above-threshold → marker written"
else
    fail_test "advisor: above-threshold → marker written" "marker not created"
fi
rm -f "$TMPFILE2"

# ---------------------------------------------------------------------------
# ASSERTION 3 — advisor: marker contains numeric token estimate
# ---------------------------------------------------------------------------

MARKER_VAL="$(cat "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID" 2>/dev/null || echo '')"
# New format: "<observed> <window>" — two space-separated ints.
if [[ "$MARKER_VAL" =~ ^[0-9]+\ [0-9]+$ ]]; then
    pass_test "advisor: marker contains '<observed> <window>' ($MARKER_VAL)"
else
    fail_test "advisor: marker contains '<observed> <window>'" "got: '$MARKER_VAL'"
fi

# ---------------------------------------------------------------------------
# ASSERTION 4 — inject: no-marker → silent stdout
# ---------------------------------------------------------------------------

rm -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

INJECT_OUT="$(echo '{"session_id":"'"$UUID"'","prompt":"hello","cwd":"/tmp"}' \
    | run_inject 2>/dev/null)"

if [[ -z "$INJECT_OUT" ]]; then
    pass_test "inject: no-marker → silent stdout"
else
    fail_test "inject: no-marker → silent stdout" "got: $INJECT_OUT"
fi

# ---------------------------------------------------------------------------
# ASSERTION 5 — inject: marker present → advisory in stdout JSON
# ---------------------------------------------------------------------------

echo "150000" > "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

INJECT_OUT="$(echo '{"session_id":"'"$UUID"'","prompt":"hello","cwd":"/tmp"}' \
    | run_inject 2>/dev/null)"

if python3 -c "import json,sys; d=json.loads(sys.argv[1]); assert 'hookSpecificOutput' in d" "$INJECT_OUT" 2>/dev/null; then
    pass_test "inject: marker → hookSpecificOutput present"
else
    fail_test "inject: marker → hookSpecificOutput present" "got: $INJECT_OUT"
fi

# ---------------------------------------------------------------------------
# ASSERTION 6 — inject: advisory text contains token count
# ---------------------------------------------------------------------------

echo "150000" > "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

INJECT_OUT="$(echo '{"session_id":"'"$UUID"'","prompt":"hello","cwd":"/tmp"}' \
    | run_inject 2>/dev/null)"

ADVISORY="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d['hookSpecificOutput']['additionalContext'])" "$INJECT_OUT" 2>/dev/null || echo '')"
if echo "$ADVISORY" | grep -q "150,000"; then
    pass_test "inject: advisory text contains formatted token count"
else
    fail_test "inject: advisory text contains formatted token count" "advisory: '$ADVISORY'"
fi

# ---------------------------------------------------------------------------
# ASSERTION 7 — inject: advisory shows "% of 1000k window" at default threshold
# ---------------------------------------------------------------------------

# Legacy single-int marker → window derived as 1M default → "15% of 1000k window".
echo "150000" > "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

INJECT_OUT="$(echo '{"session_id":"'"$UUID"'","prompt":"hello","cwd":"/tmp"}' \
    | run_inject 2>/dev/null)"

ADVISORY="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d['hookSpecificOutput']['additionalContext'])" "$INJECT_OUT" 2>/dev/null || echo '')"
if echo "$ADVISORY" | grep -q "% of 1000k window"; then
    pass_test "inject: default threshold → advisory shows '% of 1000k window'"
else
    fail_test "inject: default threshold → advisory shows '% of 1000k window'" "advisory: '$ADVISORY'"
fi

# ---------------------------------------------------------------------------
# ASSERTION 8 — inject: marker deleted after inject (one-shot)
# ---------------------------------------------------------------------------

echo "150000" > "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

echo '{"session_id":"'"$UUID"'","prompt":"hello","cwd":"/tmp"}' \
    | run_inject >/dev/null 2>&1

if [[ ! -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID" ]]; then
    pass_test "inject: marker deleted after inject (one-shot)"
else
    fail_test "inject: marker deleted after inject (one-shot)" "marker still exists"
    rm -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"
fi

# ---------------------------------------------------------------------------
# ASSERTION 9 — inject: JSONL event uses "tokens" not "token_count"/"estimated_tokens"
# ---------------------------------------------------------------------------

echo "150000 1000000" > "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"
> "$JSONL_LOG"  # reset log

echo '{"session_id":"'"$UUID"'","prompt":"hello","cwd":"/tmp"}' \
    | run_inject >/dev/null 2>&1

if [[ -f "$JSONL_LOG" ]]; then
    INJECTED_LINE="$(grep '"event"' "$JSONL_LOG" | grep '"injected"' | tail -1)"
    HAS_TOKENS="$(echo "$INJECTED_LINE" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print('yes' if 'tokens' in d else 'no')" 2>/dev/null || echo 'no')"
    HAS_STALE="$(echo "$INJECTED_LINE" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print('yes' if ('token_count' in d or 'estimated_tokens' in d) else 'no')" 2>/dev/null || echo 'no')"
    if [[ "$HAS_TOKENS" == "yes" && "$HAS_STALE" == "no" ]]; then
        pass_test "inject JSONL: uses 'tokens', not 'token_count'/'estimated_tokens'"
    else
        fail_test "inject JSONL: uses 'tokens', not 'token_count'/'estimated_tokens'" \
            "has_tokens=$HAS_TOKENS has_stale=$HAS_STALE line=$INJECTED_LINE"
    fi
else
    fail_test "inject JSONL: log file not found" "path=$JSONL_LOG"
fi

# ---------------------------------------------------------------------------
# ASSERTION 10 — advisor: JSONL event uses "observed" not "token_count"/"estimated_tokens"
# ---------------------------------------------------------------------------

> "$JSONL_LOG"

TMPFILE3="$(mktemp)"
python3 -c "
import json
line={'type':'assistant','message':{'role':'assistant','model':'claude-opus-4-8','usage':{'input_tokens':10000,'cache_read_input_tokens':600000,'cache_creation_input_tokens':50000,'output_tokens':100}},'uuid':'u1'}
open('$TMPFILE3','w').write(json.dumps(line)+'\n')
"

UUID2="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
echo '{"session_id":"'"$UUID2"'","transcript_path":"'"$TMPFILE3"'","cwd":"/tmp"}' \
    | run_advisor >/dev/null 2>&1

rm -f "$TMPFILE3" "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID2"

if [[ -f "$JSONL_LOG" ]]; then
    MARKER_LINE="$(grep '"event"' "$JSONL_LOG" | grep '"marker_written"' | tail -1)"
    HAS_OBSERVED="$(echo "$MARKER_LINE" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print('yes' if 'observed' in d else 'no')" 2>/dev/null || echo 'no')"
    HAS_STALE="$(echo "$MARKER_LINE" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print('yes' if ('token_count' in d or 'estimated_tokens' in d) else 'no')" 2>/dev/null || echo 'no')"
    if [[ "$HAS_OBSERVED" == "yes" && "$HAS_STALE" == "no" ]]; then
        pass_test "advisor JSONL: uses 'observed', not 'token_count'/'estimated_tokens'"
    else
        fail_test "advisor JSONL: uses 'observed', not 'token_count'/'estimated_tokens'" \
            "has_observed=$HAS_OBSERVED has_stale=$HAS_STALE line=$MARKER_LINE"
    fi
else
    fail_test "advisor JSONL: log file not found" "path=$JSONL_LOG"
fi

# ---------------------------------------------------------------------------
# ASSERTION 11 — inject: malformed JSON stdin → exit 0, no crash
# ---------------------------------------------------------------------------

EXIT_CODE=0
echo 'not valid json{{{' | run_inject >/dev/null 2>&1 || EXIT_CODE=$?
if [[ "$EXIT_CODE" -eq 0 ]]; then
    pass_test "inject: malformed JSON → exit 0"
else
    fail_test "inject: malformed JSON → exit 0" "exit=$EXIT_CODE"
fi

# ---------------------------------------------------------------------------
# ASSERTION 12 — inject: missing session_id → exit 0, no crash
# ---------------------------------------------------------------------------

EXIT_CODE=0
echo '{"prompt":"hello","cwd":"/tmp"}' | run_inject >/dev/null 2>&1 || EXIT_CODE=$?
if [[ "$EXIT_CODE" -eq 0 ]]; then
    pass_test "inject: missing session_id → exit 0"
else
    fail_test "inject: missing session_id → exit 0" "exit=$EXIT_CODE"
fi

# ---------------------------------------------------------------------------
# ASSERTION 13 — inject: advisory renders the marker's real token count (not the threshold)
# ---------------------------------------------------------------------------
# New design: the inject message reports actual occupancy (tokens/%/window), NOT the
# threshold. The threshold override only governs WHETHER compact_advisor.py fires
# (covered by the go-suite). Here we assert the marker's token value is rendered.

echo "90000" > "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

INJECT_OUT="$(echo '{"session_id":"'"$UUID"'","prompt":"hello","cwd":"/tmp"}' \
    | HOME="$FAKE_HOME" CLAUDE_BOOSTER_COMPACT_THRESHOLD=80000 python3 "$INJECT_PATH" 2>/dev/null)"

ADVISORY="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d['hookSpecificOutput']['additionalContext'])" "$INJECT_OUT" 2>/dev/null || echo '')"
if echo "$ADVISORY" | grep -q "90,000 tok"; then
    pass_test "inject: advisory renders real token count '90,000 tok'"
else
    fail_test "inject: advisory renders real token count '90,000 tok'" "advisory: '$ADVISORY'"
fi

# ---------------------------------------------------------------------------
# ASSERTION 14 [NEW] — inject: COMPACT_THRESHOLD=80000 → advisory does NOT contain ">120k"
# ---------------------------------------------------------------------------

echo "90000" > "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

INJECT_OUT="$(echo '{"session_id":"'"$UUID"'","prompt":"hello","cwd":"/tmp"}' \
    | HOME="$FAKE_HOME" CLAUDE_BOOSTER_COMPACT_THRESHOLD=80000 python3 "$INJECT_PATH" 2>/dev/null)"

ADVISORY="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d['hookSpecificOutput']['additionalContext'])" "$INJECT_OUT" 2>/dev/null || echo '')"
if ! echo "$ADVISORY" | grep -q ">120k"; then
    pass_test "inject: THRESHOLD=80000 → advisory does NOT contain '>120k'"
else
    fail_test "inject: THRESHOLD=80000 → advisory does NOT contain '>120k'" "advisory: '$ADVISORY'"
fi

# ---------------------------------------------------------------------------
# ASSERTION 15 [NEW] — source: no "token_count" literal in compact_advisor*.py templates
# ---------------------------------------------------------------------------

TEMPLATE_DIR="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts"
if grep -q "token_count" "$TEMPLATE_DIR"/compact_advisor*.py 2>/dev/null; then
    fail_test "source: no 'token_count' residue in templates/scripts/compact_advisor*.py" \
        "grep found hits: $(grep -n 'token_count' "$TEMPLATE_DIR"/compact_advisor*.py)"
else
    pass_test "source: no 'token_count' residue in templates/scripts/compact_advisor*.py"
fi

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

echo ""
echo "compact_advisor acceptance test results:"
for r in "${RESULTS[@]}"; do
    echo "$r"
done
echo ""
echo "Total: $PASS_COUNT passed, $FAIL_COUNT failed"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
exit 0
