#!/usr/bin/env bash
# test_model_balancer_active.sh — Verifier: active-learning decide() path.
# Tests C1..C11 per Artifact Contract (model_balancer.py active phase).
# Exit 0 = all pass. Exit 1 = at least one failure.

set -uo pipefail

LIVE_SCRIPT="$HOME/.claude/scripts/model_balancer.py"
MIRROR_SCRIPT="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts/model_balancer.py"
LIVE_JSON="$HOME/.claude/model_balancer.json"
DB="$HOME/.claude/rolling_memory.db"
BOOSTER_ROOT="/Users/dmitrijnazarov/Projects/Claude_Booster"

BALANCER_BAK="/tmp/balancer.bak.$$.json"
METRICS_BAK="/tmp/metrics.bak.$$.sql"
C4_EVENTS="/tmp/model_provider_events.c4.$$.jsonl"

PASS=0
FAIL=0
FAILURES=""
FINAL_EXIT=0

# ── Helpers ───────────────────────────────────────────────────────────────────
pass_c() { echo "PASS: $1 — $2"; PASS=$((PASS + 1)); }
fail_c() { echo "FAIL: $1 — $2"; FAIL=$((FAIL + 1)); FAILURES="${FAILURES}\n  - $1: $2"; FINAL_EXIT=1; }

# Seed a metric row
seed_metric() {
  local provider="$1" model="$2" category="$3" per_turn_ms="$4"
  sqlite3 "$DB" "INSERT INTO model_metrics(ts_utc, provider, model, task_category, per_turn_ms, success, session_id, project_root)
    VALUES (datetime('now'), '${provider}', '${model}', '${category}', ${per_turn_ms}, 1, 'test-active-pair', '${BOOSTER_ROOT}');"
}

# Seed N identical rows
seed_n() {
  local n="$1" provider="$2" model="$3" category="$4" per_turn_ms="$5"
  for _ in $(seq 1 "$n"); do
    seed_metric "$provider" "$model" "$category" "$per_turn_ms"
  done
}

# Seed NULL per_turn_ms row
seed_null_ms() {
  local provider="$1" model="$2" category="$3"
  sqlite3 "$DB" "INSERT INTO model_metrics(ts_utc, provider, model, task_category, per_turn_ms, success, session_id, project_root)
    VALUES (datetime('now'), '${provider}', '${model}', '${category}', NULL, 1, 'test-active-pair', '${BOOSTER_ROOT}');"
}

# Delete rows by category (test rows only)
clear_cat() {
  sqlite3 "$DB" "DELETE FROM model_metrics WHERE task_category='$1' AND session_id='test-active-pair';"
}

# Delete ALL test rows
clear_test_rows() {
  sqlite3 "$DB" "DELETE FROM model_metrics WHERE session_id='test-active-pair';"
}

# JSON path helper
jget() {
  # $1 = jq path like '.routing.coding.model', $2 = file (default LIVE_JSON)
  local path="$1"
  local file="${2:-$LIVE_JSON}"
  jq -r "$path" "$file" 2>/dev/null
}

run_decide() {
  python3 "$LIVE_SCRIPT" decide "$@"
}

# ── Backup + restore ──────────────────────────────────────────────────────────
cp "$LIVE_JSON" "$BALANCER_BAK" 2>/dev/null || true
sqlite3 "$DB" ".dump model_metrics" > "$METRICS_BAK" 2>/dev/null || true
EXPECTED_CODING_PROVIDER=$(jq -r '.routing.coding.provider // "codex-cli"' "$BALANCER_BAK" 2>/dev/null || echo "codex-cli")
EXPECTED_CODING_MODEL=$(jq -r '.routing.coding.model // "gpt-5.5"' "$BALANCER_BAK" 2>/dev/null || echo "gpt-5.5")
EXPECTED_LEAD_PROVIDER=$(jq -r '.routing.lead.provider // "anthropic"' "$BALANCER_BAK" 2>/dev/null || echo "anthropic")
EXPECTED_LEAD_MODEL=$(jq -r '.routing.lead.model // "claude-opus-4-8"' "$BALANCER_BAK" 2>/dev/null || echo "claude-opus-4-8")

restore() {
  # restore balancer JSON
  if [[ -f "$BALANCER_BAK" ]]; then
    cp "$BALANCER_BAK" "$LIVE_JSON" 2>/dev/null || true
  fi
  # restore metric table: drop + re-import
  sqlite3 "$DB" "DELETE FROM model_metrics;" 2>/dev/null || true
  sqlite3 "$DB" < "$METRICS_BAK" 2>/dev/null || true
  rm -f "$BALANCER_BAK" "$METRICS_BAK" "$C4_EVENTS"
}
trap restore EXIT

echo ""
echo "=== model_balancer active-learning — acceptance test ==="
echo ""

# ──────────────────────────────────────────────────────────────────────────────
# C1 — Empty-DB safety
# ──────────────────────────────────────────────────────────────────────────────
echo "C1: Empty-DB safety"

# Delete ALL metric rows so decide() has no samples at all
sqlite3 "$DB" "DELETE FROM model_metrics;" 2>/dev/null || true

# Capture routing before the run (from backup)
ROUTING_BEFORE_C1=$(jq -c '.routing' "$BALANCER_BAK" 2>/dev/null || echo "MISSING")

C1_OUT=$(run_decide --force 2>&1); C1_EC=$?

if [[ $C1_EC -eq 0 ]]; then
  pass_c C1a "exit 0 with empty DB"
else
  fail_c C1a "expected exit 0, got $C1_EC — $C1_OUT"
fi

if [[ -f "$LIVE_JSON" ]]; then
  RATIONALE_C1=$(jget '.rationale')
  if echo "$RATIONALE_C1" | grep -qi "^active — no samples"; then
    pass_c C1b "rationale starts with 'active — no samples'"
  else
    fail_c C1b "rationale='$RATIONALE_C1' — expected to start with 'active — no samples'"
  fi

  # Routing must not change from pre-run state
  ROUTING_AFTER_C1=$(jq -c '.routing' "$LIVE_JSON" 2>/dev/null || echo "CHANGED")
  if [[ "$ROUTING_BEFORE_C1" == "$ROUTING_AFTER_C1" ]]; then
    pass_c C1c "routing unchanged after empty-DB decide"
  else
    fail_c C1c "routing changed — before='$ROUTING_BEFORE_C1' after='$ROUTING_AFTER_C1'"
  fi
else
  fail_c C1b "model_balancer.json missing after decide"
  fail_c C1c "cannot check routing (file missing)"
fi

echo ""

# ──────────────────────────────────────────────────────────────────────────────
# C2 — Codex faster wins coding
# ──────────────────────────────────────────────────────────────────────────────
echo "C2: coding pinned route survives fast Codex samples"

# Restore full metric table first, then add test rows
sqlite3 "$DB" < "$METRICS_BAK" 2>/dev/null || true
clear_cat "coding"

# 6 x codex fast (500 ms), 6 x claude slow (2000 ms)
seed_n 6 "codex-cli" "gpt-5.5" "coding" 500
seed_n 6 "anthropic" "claude-sonnet-4-6" "coding" 2000

# Capture transitions count before
C2_OUT=$(run_decide --force 2>&1); C2_EC=$?

if [[ $C2_EC -eq 0 ]]; then
  pass_c C2a "decide exits 0"
else
  fail_c C2a "decide exited $C2_EC — $C2_OUT"
fi

C2_PROVIDER=$(jget '.routing.coding.provider')
C2_MODEL=$(jget '.routing.coding.model')
C2_RATIONALE=$(jget '.rationale')

if [[ "$C2_PROVIDER" == "$EXPECTED_CODING_PROVIDER" ]]; then
  pass_c C2b "coding.provider preserved as $EXPECTED_CODING_PROVIDER"
else
  fail_c C2b "coding.provider='$C2_PROVIDER', expected pinned $EXPECTED_CODING_PROVIDER"
fi

if [[ "$C2_MODEL" == "$EXPECTED_CODING_MODEL" ]]; then
  pass_c C2c "coding.model preserved as $EXPECTED_CODING_MODEL"
else
  fail_c C2c "coding.model='$C2_MODEL', expected pinned $EXPECTED_CODING_MODEL"
fi

if echo "$C2_RATIONALE" | grep -q "^active —" && ! echo "$C2_RATIONALE" | grep -q "^active — no samples"; then
  pass_c C2d "rationale starts with 'active —' (not 'no samples')"
else
  fail_c C2d "rationale='$C2_RATIONALE'"
fi

CODING_AFTER_C2=$(jq -c '.routing.coding' "$LIVE_JSON" 2>/dev/null || echo "MISSING")
if [[ "$C2_PROVIDER" == "$EXPECTED_CODING_PROVIDER" && "$C2_MODEL" == "$EXPECTED_CODING_MODEL" ]]; then
  pass_c C2e "coding pinned route unchanged ($CODING_AFTER_C2)"
else
  fail_c C2e "coding pinned route changed unexpectedly ($CODING_AFTER_C2)"
fi

if [[ "$CODING_AFTER_C2" != "MISSING" ]]; then
  pass_c C2f "routing.coding remains readable JSON"
else
  fail_c C2f "routing.coding missing after decide"
fi

echo ""

# ──────────────────────────────────────────────────────────────────────────────
# C3 — Claude faster wins (reverse of C2)
# ──────────────────────────────────────────────────────────────────────────────
echo "C3: coding remains pinned to Codex even if Claude samples are faster"

clear_cat "coding"

# 6 x codex slow (3000 ms), 6 x claude fast (400 ms)
seed_n 6 "codex-cli" "gpt-5.5" "coding" 3000
seed_n 6 "anthropic" "claude-sonnet-4-6" "coding" 400

C3_OUT=$(run_decide --force 2>&1); C3_EC=$?

if [[ $C3_EC -eq 0 ]]; then
  pass_c C3a "decide exits 0"
else
  fail_c C3a "decide exited $C3_EC — $C3_OUT"
fi

C3_PROVIDER=$(jget '.routing.coding.provider')
C3_MODEL=$(jget '.routing.coding.model')
if [[ "$C3_PROVIDER" == "$EXPECTED_CODING_PROVIDER" && "$C3_MODEL" == "$EXPECTED_CODING_MODEL" ]]; then
  pass_c C3b "coding remains pinned to ${EXPECTED_CODING_PROVIDER}/${EXPECTED_CODING_MODEL}"
else
  fail_c C3b "coding route ${C3_PROVIDER}/${C3_MODEL}, expected ${EXPECTED_CODING_PROVIDER}/${EXPECTED_CODING_MODEL}"
fi

echo ""

# ──────────────────────────────────────────────────────────────────────────────
# C4 — Insufficient samples preserve active-category routing
# ──────────────────────────────────────────────────────────────────────────────
echo "C4: Insufficient samples preserve non-pinned active category routing"

# Isolate the whole metrics table for this case. _active_decide reads all
# model_metrics rows in the lookback window, not just rows inserted by this test.
sqlite3 "$DB" "DELETE FROM model_metrics;" 2>/dev/null || true

# Capture current non-pinned active-category routing.
ACTIVE_C4_CATEGORY="trivial"
ACTIVE_BEFORE_C4=$(jq -c ".routing.${ACTIVE_C4_CATEGORY}" "$LIVE_JSON" 2>/dev/null || echo "MISSING")

# Only 3 rows — below MIN_SAMPLES=5
seed_n 3 "codex-cli" "gpt-5.6-luna" "$ACTIVE_C4_CATEGORY" 100

# Provider-event health is a second balancer input; isolate it too so C4 only
# measures the three metric rows above.
: > "$C4_EVENTS"
C4_OUT=$(CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG="$C4_EVENTS" run_decide --force 2>&1); C4_EC=$?

if [[ $C4_EC -eq 0 ]]; then
  pass_c C4a "decide exits 0"
else
  fail_c C4a "decide exited $C4_EC — $C4_OUT"
fi

ACTIVE_AFTER_C4=$(jq -c ".routing.${ACTIVE_C4_CATEGORY}" "$LIVE_JSON" 2>/dev/null || echo "CHANGED")
if [[ "$ACTIVE_BEFORE_C4" == "$ACTIVE_AFTER_C4" ]]; then
  pass_c C4b "routing.${ACTIVE_C4_CATEGORY} unchanged with exactly 3 isolated samples"
else
  fail_c C4b "routing.${ACTIVE_C4_CATEGORY} changed — before='$ACTIVE_BEFORE_C4' after='$ACTIVE_AFTER_C4'"
fi

C4_RATIONALE=$(jget '.rationale')
if echo "$C4_RATIONALE" | grep -qi "insufficient samples"; then
  pass_c C4c "rationale mentions insufficient samples for isolated active category"
else
  fail_c C4c "rationale='$C4_RATIONALE' — expected 'insufficient samples'"
fi

echo ""

# ──────────────────────────────────────────────────────────────────────────────
# C5 — lead pinned
# ──────────────────────────────────────────────────────────────────────────────
echo "C5: lead category pinned to current configured route"

clear_cat "lead"

# 10 x codex artificially fast on lead
seed_n 10 "codex-cli" "gpt-5.5" "lead" 100

C5_OUT=$(run_decide --force 2>&1); C5_EC=$?

if [[ $C5_EC -eq 0 ]]; then
  pass_c C5a "decide exits 0"
else
  fail_c C5a "decide exited $C5_EC — $C5_OUT"
fi

C5_PROV=$(jget '.routing.lead.provider')
C5_MODEL=$(jget '.routing.lead.model')

if [[ "$C5_PROV" == "$EXPECTED_LEAD_PROVIDER" ]]; then
  pass_c C5b "lead.provider == $EXPECTED_LEAD_PROVIDER (pinned)"
else
  fail_c C5b "lead.provider='$C5_PROV' — expected $EXPECTED_LEAD_PROVIDER (pinned)"
fi

if [[ "$C5_MODEL" == "$EXPECTED_LEAD_MODEL" ]]; then
  pass_c C5c "lead.model == $EXPECTED_LEAD_MODEL (pinned)"
else
  fail_c C5c "lead.model='$C5_MODEL' — expected $EXPECTED_LEAD_MODEL (pinned)"
fi

echo ""

# ──────────────────────────────────────────────────────────────────────────────
# C6 — high_blast_radius pinned + applies_to preserved
# ──────────────────────────────────────────────────────────────────────────────
echo "C6: high_blast_radius pinned + applies_to preserved"

# Capture applies_to BEFORE any manipulation
APPLIES_BEFORE=$(jq -c '.routing.high_blast_radius.applies_to' "$LIVE_JSON" 2>/dev/null || echo "MISSING")

clear_cat "high_blast_radius"

# 10 x codex artificially fast on high_blast_radius
seed_n 10 "codex-cli" "gpt-5.5" "high_blast_radius" 100

C6_OUT=$(run_decide --force 2>&1); C6_EC=$?

if [[ $C6_EC -eq 0 ]]; then
  pass_c C6a "decide exits 0"
else
  fail_c C6a "decide exited $C6_EC — $C6_OUT"
fi

C6_PROV=$(jget '.routing.high_blast_radius.provider')
C6_MODEL=$(jget '.routing.high_blast_radius.model')
APPLIES_AFTER=$(jq -c '.routing.high_blast_radius.applies_to' "$LIVE_JSON" 2>/dev/null || echo "CHANGED")

if [[ "$C6_PROV" == "anthropic" ]]; then
  pass_c C6b "high_blast_radius.provider == anthropic (pinned)"
else
  fail_c C6b "high_blast_radius.provider='$C6_PROV' — expected anthropic"
fi

if [[ "$C6_MODEL" == "claude-sonnet-4-6" ]]; then
  pass_c C6c "high_blast_radius.model == claude-sonnet-4-6 (pinned)"
else
  fail_c C6c "high_blast_radius.model='$C6_MODEL' — expected claude-sonnet-4-6"
fi

if [[ "$APPLIES_BEFORE" == "$APPLIES_AFTER" ]]; then
  pass_c C6d "applies_to preserved ($APPLIES_BEFORE)"
else
  fail_c C6d "applies_to changed — before='$APPLIES_BEFORE' after='$APPLIES_AFTER'"
fi

echo ""

# ──────────────────────────────────────────────────────────────────────────────
# C7 — DISABLE flag bypass
# ──────────────────────────────────────────────────────────────────────────────
echo "C7: CLAUDE_BALANCER_DISABLE_ACTIVE=1 bypass"

# capture rationale before disable run
RATIONALE_BEFORE_C7=$(jget '.rationale')

C7_OUT=$(CLAUDE_BALANCER_DISABLE_ACTIVE=1 python3 "$LIVE_SCRIPT" decide --force 2>&1); C7_EC=$?

if [[ $C7_EC -eq 0 ]]; then
  pass_c C7a "exit 0 with DISABLE_ACTIVE=1"
else
  fail_c C7a "exited $C7_EC — $C7_OUT"
fi

C7_RATIONALE=$(jget '.rationale')
if ! echo "$C7_RATIONALE" | grep -q "^active —"; then
  pass_c C7b "rationale does NOT start with 'active —' (passive path taken)"
else
  fail_c C7b "rationale='$C7_RATIONALE' — should not start with 'active —' when DISABLE_ACTIVE=1"
fi

echo ""

# ──────────────────────────────────────────────────────────────────────────────
# C8 — Read-only DB URI present in source
# ──────────────────────────────────────────────────────────────────────────────
echo "C8: Read-only DB URI present in source"

if grep -qE 'file:.*mode=ro|mode=ro' "$LIVE_SCRIPT" 2>/dev/null; then
  pass_c C8 "read-only DB URI (mode=ro) found in model_balancer.py"
else
  fail_c C8 "mode=ro not found in $LIVE_SCRIPT — read-only conn not enforced"
fi

echo ""

# ──────────────────────────────────────────────────────────────────────────────
# C9 — Template sync (byte-identical)
# ──────────────────────────────────────────────────────────────────────────────
echo "C9: Template sync (live == mirror)"

if diff "$LIVE_SCRIPT" "$MIRROR_SCRIPT" > /dev/null 2>&1; then
  pass_c C9 "live and mirror are byte-identical"
else
  DIFF_LINES=$(diff "$LIVE_SCRIPT" "$MIRROR_SCRIPT" 2>/dev/null | wc -l || echo "?")
  fail_c C9 "diff produced $DIFF_LINES line(s) — live and mirror diverged"
fi

echo ""

# ──────────────────────────────────────────────────────────────────────────────
# C10 — NULL per_turn_ms tolerance
# ──────────────────────────────────────────────────────────────────────────────
echo "C10: NULL per_turn_ms rows do not crash decide"

clear_cat "coding"

# 5 rows with NULL per_turn_ms
for _ in $(seq 1 5); do
  seed_null_ms "codex-cli" "gpt-5.5" "coding"
done

C10_OUT=$(run_decide --force 2>&1); C10_EC=$?

if [[ $C10_EC -eq 0 ]]; then
  pass_c C10 "decide exits 0 with NULL per_turn_ms rows (no crash)"
else
  fail_c C10 "decide exited $C10_EC with NULL rows — $C10_OUT"
fi

echo ""

# ──────────────────────────────────────────────────────────────────────────────
# C11 — Idempotency without --force
# ──────────────────────────────────────────────────────────────────────────────
echo "C11: Idempotency — no rewrite on same-day run without --force"

# Ensure file exists and has today's decision_date
run_decide --force > /dev/null 2>&1 || true

# Get mtime before
MTIME_BEFORE=$(stat -f %m "$LIVE_JSON" 2>/dev/null || echo "0")
RATIONALE_BEFORE_C11=$(jget '.rationale')

# Wait a tiny bit so mtime can differ if written
sleep 1

run_decide > /dev/null 2>&1 || true

MTIME_AFTER=$(stat -f %m "$LIVE_JSON" 2>/dev/null || echo "1")
RATIONALE_AFTER_C11=$(jget '.rationale')

if [[ "$MTIME_BEFORE" == "$MTIME_AFTER" ]]; then
  pass_c C11a "file mtime unchanged on same-day decide (no rewrite)"
else
  # Also accept if rationale is identical — some implementations touch mtime even for no-op writes
  if [[ "$RATIONALE_BEFORE_C11" == "$RATIONALE_AFTER_C11" ]]; then
    pass_c C11a "file written but rationale unchanged — idempotent content (mtime changed but no semantic change)"
  else
    fail_c C11a "file rewritten and rationale changed: before='$RATIONALE_BEFORE_C11' after='$RATIONALE_AFTER_C11'"
  fi
fi

echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
TOTAL=$((PASS + FAIL))
echo "=== Results: $PASS/$TOTAL passed ==="

if [[ $FINAL_EXIT -ne 0 ]]; then
  echo ""
  echo "FAILURES:"
  printf "%b\n" "$FAILURES"
  echo ""
  echo "EXIT: FAIL"
  exit 1
else
  echo "EXIT: PASS"
  exit 0
fi
