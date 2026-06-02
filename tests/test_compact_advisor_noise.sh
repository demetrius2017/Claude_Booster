#!/usr/bin/env bash
# Acceptance test — compact_advisor noise-reduction contract
#
# Artifact Contract objective:
#   compact_advisor.py and compact_advisor_inject.py must NOT write log entries
#   for: below_threshold, marker_exists, no_marker events.
#   They MUST still write: marker_written (threshold crossed) and injected
#   (marker consumed).
#
# Cases:
#   1. advisor: below-threshold → exit 0, NO log entry written
#   2. advisor: marker already present (fresh) → exit 0, NO log entry written
#   3. advisor: above-threshold, no marker → exit 0, writes "marker_written" log entry
#   4. inject: no marker present → exit 0, NO log entry written
#   5. inject: marker present → exit 0, writes "injected" log entry, marker deleted
#
# Exit 0 = all assertions passed
# Exit 1 = one or more assertions failed

set -euo pipefail

ADVISOR_PATH="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts/compact_advisor.py"
INJECT_PATH="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts/compact_advisor_inject.py"

PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()

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

# Canonical test UUID
UUID="cafebabe-dead-beef-cafe-babe00000001"

# ---------------------------------------------------------------------------
# Isolated environment — fake HOME so logs and markers don't touch real ~/.claude
# ---------------------------------------------------------------------------

FAKE_HOME="$(mktemp -d)"
FAKE_CLAUDE_DIR="$FAKE_HOME/.claude"
FAKE_LOGS_DIR="$FAKE_CLAUDE_DIR/logs"
mkdir -p "$FAKE_LOGS_DIR"

JSONL_LOG="$FAKE_LOGS_DIR/compact_advisor.jsonl"

trap 'rm -rf "$FAKE_HOME"' EXIT

run_advisor() {
    HOME="$FAKE_HOME" python3 "$ADVISOR_PATH" "$@"
}

run_inject() {
    HOME="$FAKE_HOME" python3 "$INJECT_PATH" "$@"
}

# Count lines in log matching a given event name (exact JSON field match)
count_log_event() {
    local event="$1"
    if [[ ! -f "$JSONL_LOG" ]]; then
        echo 0
        return
    fi
    python3 -c "
import json, sys
count = 0
for line in open('$JSONL_LOG', encoding='utf-8'):
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
        if d.get('event') == sys.argv[1]:
            count += 1
    except Exception:
        pass
print(count)
" "$event"
}

count_log_lines() {
    python3 -c "
import sys
try:
    lines = [l.strip() for l in open('$JSONL_LOG', encoding='utf-8') if l.strip()]
    print(len(lines))
except FileNotFoundError:
    print(0)
" 2>/dev/null || echo 0
}

# ---------------------------------------------------------------------------
# CASE 1 — advisor: below-threshold → exit 0, NO log entry of any kind
# ---------------------------------------------------------------------------

> "$JSONL_LOG"

SMALL_FILE="$(mktemp)"
# 100 bytes → 100//4 = 25 tokens, well below 120000
python3 -c "open('$SMALL_FILE','wb').write(b'x'*100)"

EXIT_CODE=0
echo "{\"session_id\":\"$UUID\",\"transcript_path\":\"$SMALL_FILE\",\"cwd\":\"/tmp\"}" \
    | run_advisor >/dev/null 2>&1 || EXIT_CODE=$?

rm -f "$SMALL_FILE"

# Must exit 0
if [[ "$EXIT_CODE" -ne 0 ]]; then
    fail_test "case1 advisor below-threshold: exit 0" "exit=$EXIT_CODE"
else
    pass_test "case1 advisor below-threshold: exit 0"
fi

# Must NOT write below_threshold event
BELOW_COUNT="$(count_log_event "below_threshold")"
if [[ "$BELOW_COUNT" -eq 0 ]]; then
    pass_test "case1 advisor below-threshold: no 'below_threshold' log entry"
else
    LOGGED_LINES="$(cat "$JSONL_LOG" 2>/dev/null || echo '')"
    fail_test "case1 advisor below-threshold: no 'below_threshold' log entry" \
        "found $BELOW_COUNT entries; log=$LOGGED_LINES"
fi

# Must NOT write any log entry at all (silent path)
LOG_LINE_COUNT="$(count_log_lines)"
if [[ "$LOG_LINE_COUNT" -eq 0 ]]; then
    pass_test "case1 advisor below-threshold: log file silent (no lines written)"
else
    LOGGED_LINES="$(cat "$JSONL_LOG" 2>/dev/null || echo '')"
    fail_test "case1 advisor below-threshold: log file silent" \
        "found $LOG_LINE_COUNT lines; log=$LOGGED_LINES"
fi

# ---------------------------------------------------------------------------
# CASE 2 — advisor: marker already present (fresh, < 2h) → exit 0, NO log entry
# ---------------------------------------------------------------------------

> "$JSONL_LOG"

# Create a fresh marker
echo "150000" > "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

BIG_FILE="$(mktemp)"
python3 -c "open('$BIG_FILE','wb').write(b'x'*600000)"

EXIT_CODE=0
echo "{\"session_id\":\"$UUID\",\"transcript_path\":\"$BIG_FILE\",\"cwd\":\"/tmp\"}" \
    | run_advisor >/dev/null 2>&1 || EXIT_CODE=$?

rm -f "$BIG_FILE"

# Must exit 0
if [[ "$EXIT_CODE" -ne 0 ]]; then
    fail_test "case2 advisor marker-exists: exit 0" "exit=$EXIT_CODE"
else
    pass_test "case2 advisor marker-exists: exit 0"
fi

# Must NOT write marker_exists event (the noisy log call being removed)
MARKER_EXISTS_COUNT="$(count_log_event "marker_exists")"
if [[ "$MARKER_EXISTS_COUNT" -eq 0 ]]; then
    pass_test "case2 advisor marker-exists: no 'marker_exists' log entry"
else
    LOGGED_LINES="$(cat "$JSONL_LOG" 2>/dev/null || echo '')"
    fail_test "case2 advisor marker-exists: no 'marker_exists' log entry" \
        "found $MARKER_EXISTS_COUNT entries; log=$LOGGED_LINES"
fi

# Log must be empty (no writes at all on this path)
LOG_LINE_COUNT="$(count_log_lines)"
if [[ "$LOG_LINE_COUNT" -eq 0 ]]; then
    pass_test "case2 advisor marker-exists: log file silent (no lines written)"
else
    LOGGED_LINES="$(cat "$JSONL_LOG" 2>/dev/null || echo '')"
    fail_test "case2 advisor marker-exists: log file silent" \
        "found $LOG_LINE_COUNT lines; log=$LOGGED_LINES"
fi

# Clean up the pre-created marker so case3 starts fresh
rm -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

# ---------------------------------------------------------------------------
# CASE 3 — advisor: above-threshold, no marker → exit 0, writes "marker_written"
# ---------------------------------------------------------------------------

> "$JSONL_LOG"
rm -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

UUID3="cafebabe-dead-beef-cafe-babe00000003"

# 2.4 MB plain file → no usage block → byte fallback 2400000//4 = 600000 ≥ 600k default threshold.
# (Calibrated for the 1M-window default: fire at 60% of 1M = 600k, not the legacy 120k.)
BIG_FILE3="$(mktemp)"
python3 -c "open('$BIG_FILE3','wb').write(b'x'*2400000)"

EXIT_CODE=0
echo "{\"session_id\":\"$UUID3\",\"transcript_path\":\"$BIG_FILE3\",\"cwd\":\"/tmp\"}" \
    | run_advisor >/dev/null 2>&1 || EXIT_CODE=$?

rm -f "$BIG_FILE3"

# Must exit 0
if [[ "$EXIT_CODE" -ne 0 ]]; then
    fail_test "case3 advisor above-threshold: exit 0" "exit=$EXIT_CODE"
else
    pass_test "case3 advisor above-threshold: exit 0"
fi

# Marker file must exist
if [[ -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID3" ]]; then
    pass_test "case3 advisor above-threshold: marker file written"
else
    fail_test "case3 advisor above-threshold: marker file written" "marker not found at $FAKE_CLAUDE_DIR/.compact_recommended_$UUID3"
fi

# Must write exactly one "marker_written" log entry
MARKER_WRITTEN_COUNT="$(count_log_event "marker_written")"
if [[ "$MARKER_WRITTEN_COUNT" -ge 1 ]]; then
    pass_test "case3 advisor above-threshold: 'marker_written' log entry present"
else
    LOGGED_LINES="$(cat "$JSONL_LOG" 2>/dev/null || echo '')"
    fail_test "case3 advisor above-threshold: 'marker_written' log entry present" \
        "found $MARKER_WRITTEN_COUNT entries; log=$LOGGED_LINES"
fi

# Must NOT write below_threshold (sanity cross-check)
BELOW_COUNT3="$(count_log_event "below_threshold")"
if [[ "$BELOW_COUNT3" -eq 0 ]]; then
    pass_test "case3 advisor above-threshold: no spurious 'below_threshold' entry"
else
    fail_test "case3 advisor above-threshold: no spurious 'below_threshold' entry" \
        "found $BELOW_COUNT3 entries"
fi

# Clean up
rm -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID3"

# ---------------------------------------------------------------------------
# CASE 4 — inject: no marker → exit 0, NO log entry written
# ---------------------------------------------------------------------------

> "$JSONL_LOG"
rm -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

EXIT_CODE=0
INJECT_OUT="$(echo "{\"session_id\":\"$UUID\",\"prompt\":\"hello\",\"cwd\":\"/tmp\"}" \
    | run_inject 2>/dev/null)" || EXIT_CODE=$?

# Must exit 0
if [[ "$EXIT_CODE" -ne 0 ]]; then
    fail_test "case4 inject no-marker: exit 0" "exit=$EXIT_CODE"
else
    pass_test "case4 inject no-marker: exit 0"
fi

# Must produce no stdout (silent when no marker)
if [[ -z "$INJECT_OUT" ]]; then
    pass_test "case4 inject no-marker: silent stdout"
else
    fail_test "case4 inject no-marker: silent stdout" "got: $INJECT_OUT"
fi

# Must NOT write no_marker event
NO_MARKER_COUNT="$(count_log_event "no_marker")"
if [[ "$NO_MARKER_COUNT" -eq 0 ]]; then
    pass_test "case4 inject no-marker: no 'no_marker' log entry"
else
    LOGGED_LINES="$(cat "$JSONL_LOG" 2>/dev/null || echo '')"
    fail_test "case4 inject no-marker: no 'no_marker' log entry" \
        "found $NO_MARKER_COUNT entries; log=$LOGGED_LINES"
fi

# Log must be empty on this path
LOG_LINE_COUNT="$(count_log_lines)"
if [[ "$LOG_LINE_COUNT" -eq 0 ]]; then
    pass_test "case4 inject no-marker: log file silent (no lines written)"
else
    LOGGED_LINES="$(cat "$JSONL_LOG" 2>/dev/null || echo '')"
    fail_test "case4 inject no-marker: log file silent" \
        "found $LOG_LINE_COUNT lines; log=$LOGGED_LINES"
fi

# ---------------------------------------------------------------------------
# CASE 5 — inject: marker present → exit 0, writes "injected", marker deleted
# ---------------------------------------------------------------------------

> "$JSONL_LOG"
echo "150000" > "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"

EXIT_CODE=0
INJECT_OUT="$(echo "{\"session_id\":\"$UUID\",\"prompt\":\"hello\",\"cwd\":\"/tmp\"}" \
    | run_inject 2>/dev/null)" || EXIT_CODE=$?

# Must exit 0
if [[ "$EXIT_CODE" -ne 0 ]]; then
    fail_test "case5 inject with-marker: exit 0" "exit=$EXIT_CODE"
else
    pass_test "case5 inject with-marker: exit 0"
fi

# Must produce hookSpecificOutput JSON on stdout
ADVISORY_OK=0
if python3 -c "
import json, sys
d = json.loads(sys.argv[1])
assert 'hookSpecificOutput' in d, 'missing hookSpecificOutput'
assert 'additionalContext' in d['hookSpecificOutput'], 'missing additionalContext'
" "$INJECT_OUT" 2>/dev/null; then
    ADVISORY_OK=1
fi
if [[ "$ADVISORY_OK" -eq 1 ]]; then
    pass_test "case5 inject with-marker: hookSpecificOutput with additionalContext in stdout"
else
    fail_test "case5 inject with-marker: hookSpecificOutput with additionalContext in stdout" \
        "got: $INJECT_OUT"
fi

# Must write "injected" log entry
INJECTED_COUNT="$(count_log_event "injected")"
if [[ "$INJECTED_COUNT" -ge 1 ]]; then
    pass_test "case5 inject with-marker: 'injected' log entry present"
else
    LOGGED_LINES="$(cat "$JSONL_LOG" 2>/dev/null || echo '')"
    fail_test "case5 inject with-marker: 'injected' log entry present" \
        "found $INJECTED_COUNT entries; log=$LOGGED_LINES"
fi

# Must NOT write no_marker event (cross-check — marker WAS present)
NO_MARKER_COUNT5="$(count_log_event "no_marker")"
if [[ "$NO_MARKER_COUNT5" -eq 0 ]]; then
    pass_test "case5 inject with-marker: no spurious 'no_marker' entry"
else
    fail_test "case5 inject with-marker: no spurious 'no_marker' entry" \
        "found $NO_MARKER_COUNT5 entries"
fi

# Marker must be deleted (one-shot semantics)
if [[ ! -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID" ]]; then
    pass_test "case5 inject with-marker: marker deleted after inject"
else
    fail_test "case5 inject with-marker: marker deleted after inject" \
        "marker still exists at $FAKE_CLAUDE_DIR/.compact_recommended_$UUID"
    rm -f "$FAKE_CLAUDE_DIR/.compact_recommended_$UUID"
fi

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

echo ""
echo "compact_advisor noise-reduction acceptance test:"
for r in "${RESULTS[@]}"; do
    echo "$r"
done
echo ""
echo "Total: $PASS_COUNT passed, $FAIL_COUNT failed"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
exit 0
