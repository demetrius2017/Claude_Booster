#!/usr/bin/env bash
# Acceptance test for Fable 5 usage/cost visibility.
# Runs deterministic local fixtures only; no live model/API calls.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && git rev-parse --show-toplevel)"
FABLE_USAGE="$REPO_ROOT/templates/scripts/fable_usage.py"
STATUSLINE="$REPO_ROOT/templates/scripts/statusline.sh"
ROLLING_MEMORY="$REPO_ROOT/templates/scripts/rolling_memory.py"
SETTINGS_TEMPLATE="$REPO_ROOT/templates/settings.json.template"

PASS=0
FAIL=0

pass() {
    printf 'PASS: %s\n' "$1"
    PASS=$((PASS + 1))
}

fail() {
    printf 'FAIL: %s\n' "$1"
    printf '      expected: %s\n' "$2"
    printf '      got:      %s\n' "$3"
    FAIL=$((FAIL + 1))
}

assert_file_exists() {
    local label="$1" path="$2"
    if [[ -f "$path" ]]; then
        pass "$label"
    else
        fail "$label" "$path exists" "missing"
    fi
}

assert_executable() {
    local label="$1" path="$2"
    if [[ -x "$path" ]]; then
        pass "$label"
    else
        fail "$label" "$path is executable" "not executable or missing"
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

assert_contains() {
    local label="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$label"
    else
        fail "$label" "contains '$needle'" "$haystack"
    fi
}

assert_not_contains() {
    local label="$1" haystack="$2" needle="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        pass "$label"
    else
        fail "$label" "does not contain '$needle'" "$haystack"
    fi
}

assert_exit_zero_quiet() {
    local label="$1" code="$2" stdout="$3" stderr="$4"
    if [[ "$code" -eq 0 && -z "$stdout" && -z "$stderr" ]]; then
        pass "$label"
    else
        fail "$label" "exit 0 with empty stdout/stderr" "exit=$code stdout='$stdout' stderr='$stderr'"
    fi
}

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/fable_usage_test.XXXXXX")"
cleanup() {
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

FAKE_HOME="$TMP_ROOT/home"
FAKE_CLAUDE_HOME="$FAKE_HOME/.claude"
mkdir -p "$FAKE_CLAUDE_HOME/logs" "$TMP_ROOT/project"

TRANSCRIPT="$TMP_ROOT/project/transcript.jsonl"
MALFORMED_TRANSCRIPT="$TMP_ROOT/project/malformed.jsonl"
CODEX_ROOT="$TMP_ROOT/codex_sessions"
CODEX_TRANSCRIPT="$CODEX_ROOT/session.jsonl"
mkdir -p "$CODEX_ROOT"

cat > "$TRANSCRIPT" <<'JSONL'
{"type":"assistant","timestamp":"2026-06-30T23:59:59Z","message":{"id":"msg_prev_month","model":"claude-fable-5","usage":{"input_tokens":1000000,"output_tokens":1000000,"cache_read_input_tokens":1000000,"cache_creation_input_tokens":1000000}}}
{"type":"assistant","timestamp":"2026-07-01T00:00:00Z","message":{"id":"msg_current_full","model":"claude-fable-5","usage":{"input_tokens":100000,"output_tokens":10000,"cache_read_input_tokens":200000,"cache_creation":{"ephemeral_5m_input_tokens":100000,"ephemeral_1h_input_tokens":50000}}}}
{"type":"assistant","timestamp":"2026-07-01T00:00:00Z","message":{"id":"msg_current_full","model":"claude-fable-5","usage":{"input_tokens":100000,"output_tokens":10000,"cache_read_input_tokens":200000,"cache_creation":{"ephemeral_5m_input_tokens":100000,"ephemeral_1h_input_tokens":50000}}}}
{"type":"assistant","timestamp":"2026-07-02T12:00:00Z","message":{"id":"msg_other_model","model":"claude-opus-4-8","usage":{"input_tokens":999999,"output_tokens":999999,"cache_read_input_tokens":999999}}}
{"type":"assistant","timestamp":"2026-07-01T00:30:00+03:00","message":{"id":"msg_july_local_but_june_utc","model":"claude-fable-5","usage":{"input_tokens":1000000,"output_tokens":1000000}}}
{"type":"assistant","timestamp":"2026-07-03T12:00:00Z","message":{"id":"msg_current_last","model":"claude-fable-5","usage":{"input_tokens":50000,"output_tokens":5000,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}
JSONL

cat > "$MALFORMED_TRANSCRIPT" <<'JSONL'
not-json
JSONL

cat > "$CODEX_TRANSCRIPT" <<'JSONL'
{"type":"response_item","timestamp":"2026-07-04T00:00:00Z","payload":{"session_id":"codex-session","cwd":"/tmp/codex-project","message":{"id":"codex_fable_msg","role":"assistant","model":"claude-fable-5","usage":{"input_tokens":1000,"output_tokens":1000}}}}
JSONL

hook_event() {
    local transcript="$1"
    python3 - "$transcript" <<'PY'
import json
import sys
print(json.dumps({
    "session_id": "acceptance-session",
    "transcript_path": sys.argv[1],
    "cwd": "/tmp/fable-usage-acceptance",
}))
PY
}

run_fable_hook() {
    local transcript="$1"
    local stdout="$2"
    local stderr="$3"
    hook_event "$transcript" | env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" \
        python3 "$FABLE_USAGE" >"$stdout" 2>"$stderr"
}

summary_json() {
    env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" \
        python3 "$FABLE_USAGE" summary --json
}

json_field() {
    local json="$1" expr="$2"
    JSON_INPUT="$json" python3 - "$expr" <<'PY'
import json
import os
import sys
data = json.loads(os.environ["JSON_INPUT"])
cur = data
try:
    for part in sys.argv[1].split("."):
        cur = cur[part]
except Exception:
    print("__MISSING__")
    raise SystemExit(0)
if isinstance(cur, float):
    print(f"{cur:.6f}")
else:
    print(cur)
PY
}

sqlite_value() {
    local sql="$1"
    env DB_PATH="$FAKE_CLAUDE_HOME/rolling_memory.db" python3 - "$sql" <<'PY'
import os
import sqlite3
import sys

conn = sqlite3.connect(os.environ["DB_PATH"])
try:
    row = conn.execute(sys.argv[1]).fetchone()
    value = "" if row is None else row[0]
    if isinstance(value, float):
        print(f"{value:.6f}")
    else:
        print(value)
finally:
    conn.close()
PY
}

echo "=== Section 1: artifact files ==="
assert_file_exists "fable_usage.py exists" "$FABLE_USAGE"
assert_executable "fable_usage.py is executable" "$FABLE_USAGE"
assert_file_exists "statusline.sh exists" "$STATUSLINE"
assert_file_exists "rolling_memory.py exists" "$ROLLING_MEMORY"
assert_file_exists "settings template exists" "$SETTINGS_TEMPLATE"

if [[ ! -f "$FABLE_USAGE" ]]; then
    echo
    echo "=== Results ==="
    printf 'Passed: %d\nFailed: %d\n' "$PASS" "$FAIL"
    echo "RESULT: FAIL"
    exit 1
fi

echo
echo "=== Section 2: hook mode and cost aggregation ==="
EMPTY_TRANSCRIPT="$TMP_ROOT/project/empty.jsonl"
: > "$EMPTY_TRANSCRIPT"
env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" \
    python3 "$FABLE_USAGE" ingest --transcript "$EMPTY_TRANSCRIPT" --json >/dev/null 2>&1

OUT1="$TMP_ROOT/hook1.out"
ERR1="$TMP_ROOT/hook1.err"
HOOK1_EXIT=0
run_fable_hook "$TRANSCRIPT" "$OUT1" "$ERR1" || HOOK1_EXIT=$?
assert_exit_zero_quiet "hook scan exits 0 quietly" "$HOOK1_EXIT" "$(cat "$OUT1")" "$(cat "$ERR1")"

SUMMARY1="$(summary_json 2>"$TMP_ROOT/summary1.err")"
SUMMARY1_EXIT=$?
assert_eq "summary command exits 0" "0" "$SUMMARY1_EXIT"
assert_contains "summary declares API-equivalent estimate basis" "$SUMMARY1" "API-equivalent"

MONTH_UTC="$(json_field "$SUMMARY1" "month_utc")"
REQUEST_COUNT="$(json_field "$SUMMARY1" "mtd.events")"
LAST_USD="$(json_field "$SUMMARY1" "last_task.cost_usd")"
MTD_USD="$(json_field "$SUMMARY1" "mtd.cost_usd")"
CACHE_READ_USD="$(sqlite_value "SELECT printf('%.6f', COALESCE(SUM(cache_read_tokens * 1000), 0) / 1000000000.0) FROM fable_usage_events WHERE month_utc = '2026-07';")"
CACHE_WRITE_5M_USD="$(sqlite_value "SELECT printf('%.6f', COALESCE(SUM(cache_creation_5m_tokens * 12500), 0) / 1000000000.0) FROM fable_usage_events WHERE month_utc = '2026-07';")"
CACHE_WRITE_1H_USD="$(sqlite_value "SELECT printf('%.6f', COALESCE(SUM(cache_creation_1h_tokens * 20000), 0) / 1000000000.0) FROM fable_usage_events WHERE month_utc = '2026-07';")"
LATEST_MSG_ID="$(sqlite_value "SELECT assistant_message_id FROM fable_usage_events ORDER BY ts_utc DESC, id DESC LIMIT 1;")"
MODEL_ROW_COUNT="$(sqlite_value "SELECT COUNT(*) FROM fable_usage_events WHERE model = 'claude-fable-5';")"

assert_eq "UTC month-to-date bucket is July 2026" "2026-07" "$MONTH_UTC"
assert_eq "dedupe by message.id counts two current-month Fable requests" "2" "$REQUEST_COUNT"
assert_eq "ledger stores parsed claude-fable-5 rows only once per message.id" "4" "$MODEL_ROW_COUNT"
assert_eq "last request is newest UTC Fable message" "msg_current_last" "$LATEST_MSG_ID"
assert_eq "last request/task USD is reported from cached summary" "138.2000" "$LAST_USD"
assert_eq "MTD USD excludes previous month, non-Fable rows, UTC-June local row, and duplicate rows" "4.7000" "$MTD_USD"
assert_eq "cache read is billed at \$1/MTok" "0.200000" "$CACHE_READ_USD"
assert_eq "5m cache write is billed at 1.25x Fable input rate" "1.250000" "$CACHE_WRITE_5M_USD"
assert_eq "1h cache write is billed at 2x Fable input rate" "1.000000" "$CACHE_WRITE_1H_USD"

SCAN_DRY="$(env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" \
    python3 "$FABLE_USAGE" scan-month --month 2026-07 --root "$TMP_ROOT/project" --dry-run --json)"
assert_eq "scan-month dry-run finds current UTC month Fable events" "2" "$(json_field "$SCAN_DRY" "events")"
assert_eq "scan-month dry-run computes current UTC month cost" "4.7000" "$(json_field "$SCAN_DRY" "cost_usd")"

CODEX_SCAN_DRY="$(env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" \
    python3 "$FABLE_USAGE" scan-month --month 2026-07 --root "$CODEX_ROOT" --dry-run --json)"
assert_eq "scan-month dry-run parses Codex-like payload.message Fable event" "1" "$(json_field "$CODEX_SCAN_DRY" "events")"
assert_eq "scan-month dry-run prices Codex-like Fable event" "0.0600" "$(json_field "$CODEX_SCAN_DRY" "cost_usd")"

SCAN_WRITE="$(env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" \
    python3 "$FABLE_USAGE" scan-month --month 2026-07 --root "$TMP_ROOT/project" --json)"
assert_eq "scan-month write does not duplicate already-ingested events" "0" "$(json_field "$SCAN_WRITE" "inserted")"

OUT2="$TMP_ROOT/hook2.out"
ERR2="$TMP_ROOT/hook2.err"
HOOK2_EXIT=0
run_fable_hook "$TRANSCRIPT" "$OUT2" "$ERR2" || HOOK2_EXIT=$?
assert_exit_zero_quiet "second hook scan of same transcript exits 0 quietly" "$HOOK2_EXIT" "$(cat "$OUT2")" "$(cat "$ERR2")"

SUMMARY2="$(summary_json)"
assert_eq "repeated hook run does not double-count MTD spend" "$MTD_USD" "$(json_field "$SUMMARY2" "mtd.cost_usd")"
assert_eq "repeated hook run preserves deduped request count" "$REQUEST_COUNT" "$(json_field "$SUMMARY2" "mtd.events")"

rm -f "$FAKE_CLAUDE_HOME/fable_usage_summary.json"
REFRESH_OUT="$(env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" \
    python3 "$FABLE_USAGE" refresh-display --month 2026-07 --root "$TMP_ROOT/project")"
assert_contains "refresh-display prints current last request/task estimate" "$REFRESH_OUT" "Fable last request/task estimate"
assert_contains "refresh-display prints current month-to-date estimate" "$REFRESH_OUT" "Fable month-to-date estimate"

SUMMARY3="$(summary_json)"
assert_eq "cache deletion recomputes MTD from immutable ledger" "$MTD_USD" "$(json_field "$SUMMARY3" "mtd.cost_usd")"
assert_eq "cache deletion recomputes last request from immutable ledger" "$LAST_USD" "$(json_field "$SUMMARY3" "last_task.cost_usd")"

echo
echo "=== Section 3: fail-open malformed input ==="
BAD_STDIN_OUT="$TMP_ROOT/bad_stdin.out"
BAD_STDIN_ERR="$TMP_ROOT/bad_stdin.err"
BAD_STDIN_EXIT=0
printf 'not-json\n' | env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" \
    python3 "$FABLE_USAGE" >"$BAD_STDIN_OUT" 2>"$BAD_STDIN_ERR" || BAD_STDIN_EXIT=$?
assert_exit_zero_quiet "malformed hook stdin exits 0 quietly" "$BAD_STDIN_EXIT" "$(cat "$BAD_STDIN_OUT")" "$(cat "$BAD_STDIN_ERR")"

BAD_JSONL_OUT="$TMP_ROOT/bad_jsonl.out"
BAD_JSONL_ERR="$TMP_ROOT/bad_jsonl.err"
BAD_JSONL_EXIT=0
run_fable_hook "$MALFORMED_TRANSCRIPT" "$BAD_JSONL_OUT" "$BAD_JSONL_ERR" || BAD_JSONL_EXIT=$?
assert_exit_zero_quiet "malformed transcript JSONL exits 0 quietly" "$BAD_JSONL_EXIT" "$(cat "$BAD_JSONL_OUT")" "$(cat "$BAD_JSONL_ERR")"

echo
echo "=== Section 4: statusline cache behavior ==="
PHASE_DIR="$TMP_ROOT/status_project"
mkdir -p "$PHASE_DIR/.claude"
printf 'RECON\n' > "$PHASE_DIR/.claude/.phase"
rm -f "$FAKE_CLAUDE_HOME/logs/fable_usage_summary.json"
rm -f "$FAKE_CLAUDE_HOME/fable_usage_summary.json"

STATUS_NO_CACHE="$(cd "$PHASE_DIR" && env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" bash "$STATUSLINE" </dev/null)"
assert_eq "statusline output unchanged when no Fable cache exists" "[RECON]" "$STATUS_NO_CACHE"

env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" \
    python3 "$FABLE_USAGE" ingest --transcript "$TRANSCRIPT" --session-id acceptance-session --project-root "$TMP_ROOT/project" --json >/dev/null
STATUS_WITH_CACHE="$(cd "$PHASE_DIR" && printf '{"session_id":"acceptance-session"}' | env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" bash "$STATUSLINE")"
assert_contains "statusline reports Fable label from cache" "$STATUS_WITH_CACHE" "Fable"
assert_contains "statusline reports current-session last request/task cost" "$STATUS_WITH_CACHE" "Fable session est \$138.2000"
assert_not_contains "statusline does not report cross-session MTD estimate" "$STATUS_WITH_CACHE" "MTD"
assert_not_contains "statusline does not print transcript path" "$STATUS_WITH_CACHE" "$TRANSCRIPT"

STATUS_OTHER_SESSION="$(cd "$PHASE_DIR" && printf '{"session_id":"other-session"}' | env HOME="$FAKE_HOME" CLAUDE_HOME="$FAKE_CLAUDE_HOME" bash "$STATUSLINE")"
assert_eq "statusline suppresses Fable cache from a different session" "[RECON]" "$STATUS_OTHER_SESSION"

echo
echo "=== Section 5: settings and rolling-memory integration ==="
SETTINGS_CHECK="$(python3 - "$SETTINGS_TEMPLATE" <<'PY'
import json
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
data = json.loads(text)
stop_hooks = data.get("hooks", {}).get("Stop", [])
commands = []
for block in stop_hooks:
    for hook in block.get("hooks", []):
        commands.append(hook.get("command", ""))
stop_count = sum("fable_usage.py" in cmd for cmd in commands)
total_count = text.count("fable_usage.py")
print(f"{stop_count}:{total_count}")
PY
)"
assert_eq "settings template contains Fable usage Stop hook exactly once" "1:1" "$SETTINGS_CHECK"

ROLLING_SOURCE="$(cat "$ROLLING_MEMORY")"
assert_contains "rolling_memory schema includes immutable Fable usage ledger" "$ROLLING_SOURCE" "fable_usage_events"
assert_contains "rolling_memory schema migration documents Fable usage visibility" "$ROLLING_SOURCE" "Fable 5 usage visibility"

echo
echo "=== Results ==="
printf 'Passed: %d\nFailed: %d\n' "$PASS" "$FAIL"
if [[ "$FAIL" -gt 0 ]]; then
    echo "RESULT: FAIL"
    exit 1
fi
echo "RESULT: PASS"
exit 0
