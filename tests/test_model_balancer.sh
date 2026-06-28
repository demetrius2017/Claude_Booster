#!/usr/bin/env bash
# test_model_balancer.sh — Verifier for model_balancer.py (paired Worker+Verifier pattern)
# Exit 0 = PASS, exit 1 = FAIL

set -uo pipefail

LIVE_SCRIPT="$HOME/.claude/scripts/model_balancer.py"
MIRROR_SCRIPT="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts/model_balancer.py"
LIVE_JSON="$HOME/.claude/model_balancer.json"
BACKUP="/tmp/mb_test_backup_$$.json"
TEMP_JSON="/tmp/mb_test_temp.json"
STALE_DATE=$(python3 -c "from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d'))")
STALE_BAK="$HOME/.claude/model_balancer.json.bak.$STALE_DATE"

PASS=0
FAIL=0
FAILURES=""
FINAL_EXIT=0

# ── Backup + restore trap ─────────────────────────────────────────────────────
if [[ -f "$LIVE_JSON" ]]; then
  cp "$LIVE_JSON" "$BACKUP"
fi
trap '
  if [[ -f "$BACKUP" ]]; then
    cp "$BACKUP" "$LIVE_JSON"
  else
    rm -f "$LIVE_JSON"
  fi
  rm -f "$TEMP_JSON" "$STALE_BAK" "/tmp/mb_test_temp.json" 2>/dev/null
' EXIT

# ── Helpers ───────────────────────────────────────────────────────────────────
pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); FAILURES="${FAILURES}\n  - $1"; FINAL_EXIT=1; }

assert_exit0()   { "$@" > /dev/null 2>&1 && pass "$*" || fail "$* — expected exit 0"; }
assert_nonzero() { "$@" > /dev/null 2>&1 && fail "$* — expected non-zero exit" || pass "$* (expected non-zero, got $?)"; }

today_utc() { python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%d'))"; }

cd "$HOME/.claude/scripts" 2>/dev/null || { echo "ABORT: $HOME/.claude/scripts not found"; exit 1; }

echo ""
echo "=== model_balancer — acceptance test ==="
echo ""

# ── C1: Files exist, live is executable, both have python3 shebang ────────────
echo "C1: Script + mirror exist; live executable; python3 shebangs"
if [[ -f "$LIVE_SCRIPT" ]]; then pass "live script exists"; else fail "live script missing: $LIVE_SCRIPT"; fi
if [[ -f "$MIRROR_SCRIPT" ]]; then pass "mirror script exists"; else fail "mirror script missing: $MIRROR_SCRIPT"; fi
if [[ -x "$LIVE_SCRIPT" ]]; then pass "live script is executable"; else fail "live script not executable"; fi

SHEBANG_LIVE=$(head -1 "$LIVE_SCRIPT" 2>/dev/null || echo "")
if echo "$SHEBANG_LIVE" | grep -q "python3"; then pass "live has python3 shebang"; else fail "live shebang wrong: '$SHEBANG_LIVE'"; fi

SHEBANG_MIRROR=$(head -1 "$MIRROR_SCRIPT" 2>/dev/null || echo "")
if echo "$SHEBANG_MIRROR" | grep -q "python3"; then pass "mirror has python3 shebang"; else fail "mirror shebang wrong: '$SHEBANG_MIRROR'"; fi

echo ""

# ── C2: status exit 0, output matches regex ───────────────────────────────────
echo "C2: 'status' subcommand format"
STATUS_OUT=$(python3 model_balancer.py status 2>&1) && STATUS_EC=0 || STATUS_EC=$?
if [[ $STATUS_EC -eq 0 ]]; then pass "status exits 0"; else fail "status exited $STATUS_EC: $STATUS_OUT"; fi
if echo "$STATUS_OUT" | grep -qP 'decision_date=\d{4}-\d{2}-\d{2}, age=\S+, source=\S+, (fresh|stale)' 2>/dev/null \
   || echo "$STATUS_OUT" | grep -qE 'decision_date=[0-9]{4}-[0-9]{2}-[0-9]{2}, age=[^ ]+, source=[^ ]+, (fresh|stale)'; then
  pass "status output matches expected regex"
else
  fail "status output doesn't match regex: '$STATUS_OUT'"
fi

echo ""

# ── C3: get coding → valid JSON with provider + model ─────────────────────────
echo "C3: 'get coding' returns valid JSON with provider+model"
CODING_OUT=$(python3 model_balancer.py get coding 2>&1) && CODING_EC=0 || CODING_EC=$?
if [[ $CODING_EC -eq 0 ]]; then pass "get coding exits 0"; else fail "get coding exited $CODING_EC: $CODING_OUT"; fi
if echo "$CODING_OUT" | python3 -c "import sys, json; d=json.load(sys.stdin); assert 'provider' in d and 'model' in d" 2>/dev/null; then
  pass "get coding output has provider+model keys"
else
  fail "get coding output invalid JSON or missing keys: '$CODING_OUT'"
fi

echo ""

# ── C4: All categories exit 0 with provider+model ────────────────────────────
echo "C4: All routing categories return provider+model"
CATEGORIES=(trivial recon medium coding hard consilium_bio audit_external audit_secondary hackathon_external lead high_blast_radius)
for cat in "${CATEGORIES[@]}"; do
  OUT=$(python3 model_balancer.py get "$cat" 2>&1) && EC=0 || EC=$?
  if [[ $EC -eq 0 ]]; then
    if echo "$OUT" | python3 -c "import sys, json; d=json.load(sys.stdin); assert 'provider' in d and 'model' in d" 2>/dev/null; then
      pass "get $cat: exit 0 + provider+model"
    else
      fail "get $cat: exit 0 but missing provider/model: '$OUT'"
    fi
  else
    fail "get $cat: exited $EC: '$OUT'"
  fi
done

echo ""

# ── C5: Unknown category → non-zero exit ─────────────────────────────────────
echo "C5: Unknown category returns non-zero exit"
UNKNOWN_OUT=$(python3 model_balancer.py get unknown_category_xyz 2>&1); UNKNOWN_EC=$?
if [[ $UNKNOWN_EC -ne 0 ]]; then
  pass "get unknown_category_xyz exits non-zero ($UNKNOWN_EC)"
else
  fail "get unknown_category_xyz unexpectedly exited 0"
fi
if echo "$UNKNOWN_OUT" | grep -qi "unknown\|not found\|invalid\|error"; then
  pass "get unknown_category_xyz output contains error message"
else
  fail "get unknown_category_xyz output has no error message: '$UNKNOWN_OUT'"
fi

echo ""

# ── C6: decide is idempotent within same UTC day ──────────────────────────────
echo "C6: decide is idempotent (same UTC day)"
# Ensure file exists first
python3 model_balancer.py decide > /dev/null 2>&1 || true

if [[ -f "$LIVE_JSON" ]]; then
  # Use sha256 for content comparison (more reliable than mtime on fast FS)
  SHA_BEFORE=$(python3 -c "import hashlib; print(hashlib.sha256(open('$LIVE_JSON','rb').read()).hexdigest())")
  sleep 1
  python3 model_balancer.py decide > /dev/null 2>&1 || true
  SHA_AFTER=$(python3 -c "import hashlib; print(hashlib.sha256(open('$LIVE_JSON','rb').read()).hexdigest())")
  if [[ "$SHA_BEFORE" == "$SHA_AFTER" ]]; then
    pass "decide is idempotent (sha256 unchanged)"
  else
    fail "decide mutated file on second same-day run (sha256 changed)"
  fi
else
  fail "decide did not create $LIVE_JSON"
fi

echo ""

# ── C7: Missing-file bootstrap ────────────────────────────────────────────────
echo "C7: Missing-file bootstrap creates file with today's date"
if [[ -f "$LIVE_JSON" ]]; then
  mv "$LIVE_JSON" "$TEMP_JSON"
fi
python3 model_balancer.py decide > /dev/null 2>&1 && DECIDE_EC=0 || DECIDE_EC=$?
if [[ $DECIDE_EC -eq 0 ]]; then pass "decide exits 0 after bootstrap"; else fail "decide exited $DECIDE_EC on bootstrap"; fi

if [[ -f "$LIVE_JSON" ]]; then
  pass "decide created $LIVE_JSON from scratch"
else
  fail "decide did NOT create $LIVE_JSON after missing-file bootstrap"
fi

if [[ -f "$LIVE_JSON" ]]; then
  FILE_DATE=$(python3 -c "import json; d=json.load(open('$LIVE_JSON')); print(d.get('decision_date','MISSING'))" 2>/dev/null || echo "PARSE_ERROR")
  TODAY=$(today_utc)
  if [[ "$FILE_DATE" == "$TODAY" ]]; then
    pass "bootstrap decision_date == today ($TODAY)"
  else
    fail "bootstrap decision_date '$FILE_DATE' != today '$TODAY'"
  fi

  # Check rationale or provider shape
  BOOTSTRAP_CHECK=$(python3 -c "
import json
d = json.load(open('$LIVE_JSON'))
rationale = str(d).lower()
routing = d.get('routing', d)
# Accept either 'bootstrap' in rationale OR all main categories have provider=anthropic
categories = ['trivial', 'recon', 'medium', 'coding', 'hard', 'consilium_bio', 'audit_secondary', 'hackathon_external', 'lead']
all_anthropic = all(
  routing.get(cat, {}).get('provider','') == 'anthropic'
  for cat in categories
  if cat in routing
)
has_bootstrap_word = 'bootstrap' in rationale
print('bootstrap_in_rationale:', has_bootstrap_word)
print('all_anthropic:', all_anthropic)
if has_bootstrap_word or all_anthropic:
  print('OK')
else:
  print('FAIL')
" 2>/dev/null || echo "FAIL")
  if echo "$BOOTSTRAP_CHECK" | grep -q "^OK"; then
    pass "bootstrap shape is hardcoded Anthropic defaults or rationale says bootstrap"
  else
    fail "bootstrap shape unexpected: $BOOTSTRAP_CHECK"
  fi
fi

# Restore original for C8 setup
if [[ -f "$TEMP_JSON" ]]; then
  mv "$TEMP_JSON" "$LIVE_JSON"
fi

echo ""

# ── C8: Stale-file regeneration ───────────────────────────────────────────────
echo "C8: Stale file triggers regeneration with backup"
TODAY=$(today_utc)
rm -f "$STALE_BAK" "$HOME/.claude/model_balancer.json.bak.$STALE_DATE"* 2>/dev/null || true
# Write a clearly stale file
python3 -c "
import json
stale = {
  'decision_date': '$STALE_DATE',
  'valid_until': '${STALE_DATE}T23:59:59Z',
  'source': 'test',
  'routing': {
    'coding': {'provider': 'anthropic', 'model': 'STALE-MARKER'},
    'trivial': {'provider': 'anthropic', 'model': 'haiku-test'},
    'recon': {'provider': 'anthropic', 'model': 'haiku-test'},
    'medium': {'provider': 'anthropic', 'model': 'sonnet-test'},
    'hard': {'provider': 'anthropic', 'model': 'opus-test'},
    'consilium_bio': {'provider': 'anthropic', 'model': 'opus-test'},
    'audit_external': {'provider': 'anthropic', 'model': 'opus-test'},
    'lead': {'provider': 'anthropic', 'model': 'opus-test'},
    'high_blast_radius': {'provider': 'anthropic', 'model': 'opus-test', 'applies_to': []}
  }
}
json.dump(stale, open('$LIVE_JSON', 'w'), indent=2)
"

python3 model_balancer.py decide > /dev/null 2>&1 && STALE_EC=0 || STALE_EC=$?
if [[ $STALE_EC -eq 0 ]]; then pass "decide exits 0 on stale file"; else fail "decide exited $STALE_EC on stale file"; fi

REGEN_DATE=$(python3 -c "import json; d=json.load(open('$LIVE_JSON')); print(d.get('decision_date','MISSING'))" 2>/dev/null || echo "PARSE_ERROR")
if [[ "$REGEN_DATE" == "$TODAY" ]]; then
  pass "stale file regenerated with today's date ($TODAY)"
else
  fail "stale file regeneration: decision_date '$REGEN_DATE' != today '$TODAY'"
fi

if compgen -G "$HOME/.claude/model_balancer.json.bak.$STALE_DATE*" > /dev/null; then
  pass "backup file exists for stale $STALE_DATE decision"
else
  fail "backup file NOT created for stale $STALE_DATE decision"
fi

# Cleanup stale backup (trap will also do it, but be explicit)
rm -f "$STALE_BAK"

echo ""

# ── C9: Library import works ──────────────────────────────────────────────────
echo "C9: Library import — get_routing returns provider+model"
LIB_OUT=$(python3 -c "
import sys
sys.path.insert(0, '$HOME/.claude/scripts')
from model_balancer import get_routing
r = get_routing('lead')
assert 'provider' in r and 'model' in r, f'Missing keys: {r}'
print('OK')
" 2>&1) && LIB_EC=0 || LIB_EC=$?

if [[ $LIB_EC -eq 0 ]] && echo "$LIB_OUT" | grep -q "^OK"; then
  pass "library import: get_routing('lead') returns provider+model"
else
  fail "library import failed (exit=$LIB_EC): '$LIB_OUT'"
fi

echo ""

# ── C10: Atomic write — no leftover .tmp file ─────────────────────────────────
echo "C10: Atomic write leaves no .tmp file"
# Force stale to trigger a write
python3 -c "
import json, os
d = json.load(open('$LIVE_JSON')) if os.path.exists('$LIVE_JSON') else {}
d['decision_date'] = '2025-01-01'
json.dump(d, open('$LIVE_JSON', 'w'))
" 2>/dev/null || true

python3 model_balancer.py decide > /dev/null 2>&1 || true

TMP_FILE="${LIVE_JSON}.tmp"
if [[ -f "$TMP_FILE" ]]; then
  fail "leftover .tmp file found: $TMP_FILE"
else
  pass "no leftover .tmp file after decide"
fi

# Also check any tmp in scripts dir
if ls "$HOME/.claude/scripts/model_balancer"*.tmp 2>/dev/null | grep -q .; then
  fail "leftover .tmp file in scripts dir"
else
  pass "no leftover .tmp in scripts dir"
fi

# Cleanup stale bak from this run
rm -f "$HOME/.claude/model_balancer.json.bak.2025-01-01" 2>/dev/null || true

echo ""

# ── C11: Mirror line-count parity (within 5 LOC) ─────────────────────────────
echo "C11: Mirror line-count parity with live (±5 LOC)"
if [[ -f "$LIVE_SCRIPT" ]] && [[ -f "$MIRROR_SCRIPT" ]]; then
  LIVE_LINES=$(wc -l < "$LIVE_SCRIPT")
  MIRROR_LINES=$(wc -l < "$MIRROR_SCRIPT")
  DIFF=$(( LIVE_LINES - MIRROR_LINES ))
  ABS_DIFF=${DIFF#-}   # portable abs value
  if [[ $ABS_DIFF -le 5 ]]; then
    pass "line-count parity: live=$LIVE_LINES mirror=$MIRROR_LINES diff=$DIFF"
  else
    fail "line-count divergence: live=$LIVE_LINES mirror=$MIRROR_LINES diff=$DIFF (>5 LOC)"
  fi
else
  fail "cannot check parity — one or both files missing"
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
