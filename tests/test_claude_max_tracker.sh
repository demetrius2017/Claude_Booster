#!/usr/bin/env bash
# test_claude_max_tracker.sh — Verifier: claude_max_tracker.py + schema v8 + model_balancer fallback.
# Tests S1..S10 per Artifact Contract.
# Exit 0 = all pass. Exit 1 = at least one failure.

set -uo pipefail

TRACKER="$HOME/.claude/scripts/claude_max_tracker.py"
TRACKER_TPL="$HOME/Projects/Claude_Booster/templates/scripts/claude_max_tracker.py"
ROLLING_MEM="$HOME/.claude/scripts/rolling_memory.py"
ROLLING_MEM_TPL="$HOME/Projects/Claude_Booster/templates/scripts/rolling_memory.py"
BALANCER="$HOME/.claude/scripts/model_balancer.py"
BALANCER_TPL="$HOME/Projects/Claude_Booster/templates/scripts/model_balancer.py"
DB="$HOME/.claude/rolling_memory.db"

S2_JSONL="/tmp/test_max_tracker_s2.jsonl"

PASS=0
FAIL=0
FINAL_EXIT=0

# ── Helpers ───────────────────────────────────────────────────────────────────
pass_t() { echo "PASS $1: $2"; PASS=$((PASS + 1)); }
fail_t() { echo "FAIL $1: $2"; FAIL=$((FAIL + 1)); FINAL_EXIT=1; }

cleanup() {
  # Remove test rows inserted during the test run
  sqlite3 "$DB" "DELETE FROM claude_max_usage WHERE session_id IN ('test-s1','test-s2','test-s9','test-s10');" 2>/dev/null || true
  rm -f "$S2_JSONL"
}
trap cleanup EXIT

# ── Create synthetic JSONL for S2 ─────────────────────────────────────────────
cat > "$S2_JSONL" <<'JSONL'
{"type":"assistant","message":{"model":"claude-opus-4-7","usage":{"input_tokens":100,"cache_creation_input_tokens":500,"output_tokens":200}}}
{"type":"assistant","message":{"model":"claude-opus-4-7","usage":{"input_tokens":50,"cache_creation_input_tokens":300,"output_tokens":150}}}
{"type":"user","message":{"role":"user","content":"hello"}}
{"type":"assistant","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":75,"cache_creation_input_tokens":200,"output_tokens":100}}}
JSONL

# ── S1: empty transcript → exit 0, no crash ───────────────────────────────────
if echo '{"session_id":"test-s1","transcript_path":"/dev/null","cwd":"/tmp"}' \
     | python3 "$TRACKER" > /tmp/s1_out.txt 2>&1; then
  pass_t S1 "hook mode: empty transcript exits 0"
else
  fail_t S1 "hook mode: empty transcript crashed (exit $?); output: $(cat /tmp/s1_out.txt)"
fi

# ── S2: synthetic JSONL → row inserted with correct sums ─────────────────────
if echo "{\"session_id\":\"test-s2\",\"transcript_path\":\"$S2_JSONL\",\"cwd\":\"/tmp\"}" \
     | python3 "$TRACKER" > /tmp/s2_out.txt 2>&1; then

  row_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM claude_max_usage WHERE session_id='test-s2';")
  if [[ "$row_count" -eq 0 ]]; then
    fail_t S2 "exit 0 but no row inserted for session_id='test-s2'"
  else
    inp=$(sqlite3 "$DB" "SELECT input_tokens FROM claude_max_usage WHERE session_id='test-s2';")
    cc=$(sqlite3 "$DB" "SELECT cache_creation_tokens FROM claude_max_usage WHERE session_id='test-s2';")
    out=$(sqlite3 "$DB" "SELECT output_tokens FROM claude_max_usage WHERE session_id='test-s2';")
    errors=""
    [[ "$inp" -eq 225 ]] || errors="${errors} input_tokens=${inp}(expected 225)"
    [[ "$cc"  -eq 1000 ]] || errors="${errors} cache_creation_tokens=${cc}(expected 1000)"
    [[ "$out" -eq 450  ]] || errors="${errors} output_tokens=${out}(expected 450)"
    if [[ -z "$errors" ]]; then
      pass_t S2 "row inserted with correct token sums (225/1000/450)"
    else
      fail_t S2 "token sums wrong:$errors"
    fi
  fi
else
  fail_t S2 "hook mode: JSONL run crashed (exit $?); output: $(cat /tmp/s2_out.txt)"
fi

# ── S3: idempotent UPSERT — run same session_id again ────────────────────────
if echo "{\"session_id\":\"test-s2\",\"transcript_path\":\"$S2_JSONL\",\"cwd\":\"/tmp\"}" \
     | python3 "$TRACKER" > /tmp/s3_out.txt 2>&1; then
  dup_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM claude_max_usage WHERE session_id='test-s2';")
  if [[ "$dup_count" -eq 1 ]]; then
    pass_t S3 "UPSERT idempotent: still exactly 1 row after second run"
  else
    fail_t S3 "UPSERT failed: found $dup_count rows for session_id='test-s2' (expected 1)"
  fi
else
  fail_t S3 "second run crashed (exit $?); output: $(cat /tmp/s3_out.txt)"
fi

# ── S4: DB schema v8+ + claude_max_usage table ────────────────────────────────
db_ver=$(sqlite3 "$DB" "PRAGMA user_version;" 2>/dev/null)
if [[ "$db_ver" -ge 8 ]]; then
  pass_t S4a "DB schema version = $db_ver (>= 8)"
else
  fail_t S4a "DB schema version = $db_ver (expected >= 8)"
fi

tbl=$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='claude_max_usage';" 2>/dev/null)
if [[ "$tbl" == "claude_max_usage" ]]; then
  pass_t S4b "claude_max_usage table exists"
else
  fail_t S4b "claude_max_usage table missing (got: '$tbl')"
fi

# ── S5: template/installed byte identity ──────────────────────────────────────
for label in tracker rolling_mem balancer; do
  case $label in
    tracker)    live="$TRACKER";    tpl="$TRACKER_TPL" ;;
    rolling_mem) live="$ROLLING_MEM"; tpl="$ROLLING_MEM_TPL" ;;
    balancer)   live="$BALANCER";   tpl="$BALANCER_TPL" ;;
  esac
  if diff -q "$live" "$tpl" > /dev/null 2>&1; then
    pass_t "S5-$label" "installed == template: $label"
  else
    fail_t "S5-$label" "byte mismatch: $live vs $tpl"
  fi
done

# ── S6: AST syntax check ──────────────────────────────────────────────────────
for f in "$TRACKER" "$ROLLING_MEM" "$BALANCER"; do
  result=$(python3 -c "import ast; ast.parse(open('$f').read()); print('OK')" 2>&1)
  if [[ "$result" == "OK" ]]; then
    pass_t "S6-$(basename $f)" "AST valid: $(basename $f)"
  else
    fail_t "S6-$(basename $f)" "AST error in $(basename $f): $result"
  fi
done

# ── S7: --weekly-usage CLI output ─────────────────────────────────────────────
weekly_out=$(python3 "$TRACKER" --weekly-usage 2>&1)
weekly_exit=$?
if [[ $weekly_exit -eq 0 ]]; then
  missing_fields=""
  for field in sessions input_tokens cache_creation_tokens output_tokens total_tokens weekly_tokens_cap weekly_max_pct; do
    echo "$weekly_out" | grep -q "$field" || missing_fields="${missing_fields} $field"
  done
  if [[ -z "$missing_fields" ]]; then
    pass_t S7 "--weekly-usage prints all required fields"
  else
    fail_t S7 "--weekly-usage missing fields:$missing_fields; output: $weekly_out"
  fi
else
  fail_t S7 "--weekly-usage exited $weekly_exit; output: $weekly_out"
fi

# ── S8: model_balancer fallback — no cap → reads snapshot ────────────────────
s8_out=$(python3 - <<'PYEOF' 2>&1
import sys
import os
sys.path.insert(0, os.path.expanduser('~/.claude/scripts'))
from model_balancer import _get_weekly_max_pct
prior = {"inputs_snapshot": {"claude_max_weekly_used_pct": 0.75}}
result = _get_weekly_max_pct(prior)
assert result == 0.75, f"Expected 0.75, got {result}"
print("PASS")
PYEOF
)
s8_exit=$?
if [[ $s8_exit -eq 0 && "$s8_out" == *"PASS"* ]]; then
  pass_t S8 "model_balancer fallback returns snapshot value 0.75"
else
  fail_t S8 "model_balancer fallback failed (exit $s8_exit): $s8_out"
fi

# ── S9: model_balancer live path — cap configured → reads DB ─────────────────
s9_out=$(python3 - <<'PYEOF' 2>&1
import sqlite3, sys, pathlib, datetime, os
sys.path.insert(0, os.path.expanduser('~/.claude/scripts'))
db = pathlib.Path.home() / ".claude" / "rolling_memory.db"
conn = sqlite3.connect(str(db))
conn.execute(
    "INSERT OR REPLACE INTO claude_max_usage "
    "(session_id, ts_utc, input_tokens, cache_creation_tokens, output_tokens) "
    "VALUES ('test-s9', datetime('now'), 1000000, 2000000, 500000)"
)
conn.commit()
conn.close()

from model_balancer import _get_weekly_max_pct
prior = {"weekly_tokens_cap": 10000000, "inputs_snapshot": {"claude_max_weekly_used_pct": 0.5}}
result = _get_weekly_max_pct(prior)
assert isinstance(result, float), f"Expected float, got {type(result)}"
assert 0.0 <= result <= 1.0, f"Expected 0..1, got {result}"
assert result != 0.5, f"Expected live value != fallback 0.5, got {result}"
print(f"PASS: live weekly_max_pct={result:.4f}")

# Cleanup
conn = sqlite3.connect(str(db))
conn.execute("DELETE FROM claude_max_usage WHERE session_id='test-s9'")
conn.commit()
conn.close()
PYEOF
)
s9_exit=$?
if [[ $s9_exit -eq 0 && "$s9_out" == *"PASS"* ]]; then
  pass_t S9 "model_balancer live DB path: $s9_out"
else
  fail_t S9 "model_balancer live DB path failed (exit $s9_exit): $s9_out"
fi

# ── S10: SKIP env var → exit 0, no row inserted ──────────────────────────────
if CLAUDE_BOOSTER_SKIP_METRIC_CAPTURE=1 python3 "$TRACKER" \
     <<< '{"session_id":"test-s10","transcript_path":"/dev/null","cwd":"/tmp"}' \
     > /tmp/s10_out.txt 2>&1; then
  s10_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM claude_max_usage WHERE session_id='test-s10';" 2>/dev/null)
  if [[ "$s10_count" -eq 0 ]]; then
    pass_t S10 "SKIP env var: exit 0 and no row inserted"
  else
    fail_t S10 "SKIP env var: exit 0 but row was inserted (count=$s10_count)"
  fi
else
  fail_t S10 "SKIP env var: tracker crashed (exit $?); output: $(cat /tmp/s10_out.txt)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
total=$((PASS + FAIL))
echo ""
echo "Passed $PASS/$total tests"
exit $FINAL_EXIT
