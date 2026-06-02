#!/usr/bin/env bash
# Independent acceptance test for the window-aware compact-advisor.
#
# Purpose: verify the compact-advisor hooks report REAL context-window
#   occupancy (from the last assistant `usage` block in the session JSONL)
#   and fire the /compact reminder at 60% of the ACTUAL window (600k on a
#   1M window) rather than a hardcoded 120k. Also verifies env overrides,
#   >200k auto-bump, one-shot inject, exit-0-everywhere, and deployed==template
#   byte-parity for all three files.
#
# Contract under test (see Artifact Contract):
#   _gate_common.real_context_tokens(transcript_path) -> int | None
#   _gate_common.effective_compact_threshold(observed) -> (threshold, window)
#   compact_advisor.py (PostToolUse): writes "~/.claude/.compact_recommended_<sid>"
#     containing "<observed> <window>" when observed >= threshold; always exit 0.
#   compact_advisor_inject.py (UserPromptSubmit): one-shot, prints
#     additionalContext JSON, deletes marker; always exit 0.
#
# Independence: this test does NOT read or reimplement hook internals. It builds
#   JSONL fixtures, drives the helpers via `python3 -c`, pipes stdin JSON to the
#   hooks, and asserts observable behavior + marker side-effects only.
#
# Hermetic: all fixtures/markers live under a per-run TMP dir and a unique
#   session_id; a trap removes every marker we create under ~/.claude.
#
# CLI: bash test_compact_advisor_go.sh   (no args)
# Exit: 0 iff all cases pass; 1 otherwise.

set -u

SCRIPTS_DIR="/Users/dmitrijnazarov/.claude/scripts"
TEMPLATES_DIR="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts"
GATE_COMMON="$SCRIPTS_DIR/_gate_common.py"
ADVISOR="$SCRIPTS_DIR/compact_advisor.py"
INJECT="$SCRIPTS_DIR/compact_advisor_inject.py"

# Unique session id (valid UUID format 8-4-4-4-12) for the end-to-end cases.
SID="aaaaaaaa-bbbb-cccc-dddd-$(printf '%012x' $((RANDOM*RANDOM*RANDOM % 281474976710655)) | tail -c 12)"
# Guarantee 12 hex chars in the last group:
SID="aaaaaaaa-bbbb-cccc-dddd-$(python3 -c 'import uuid;print(uuid.uuid4().hex[:12])')"
MARKER="$HOME/.claude/.compact_recommended_$SID"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/compact_go.XXXXXX")"

cleanup() {
  rm -rf "$TMP"
  rm -f "$MARKER"
  # Defensive: remove any marker for our SID prefix we might have created.
  rm -f "$HOME/.claude/.compact_recommended_$SID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

PASS=0
FAIL=0
pass() { echo "[PASS] $1"; PASS=$((PASS+1)); }
fail() { echo "[FAIL] $1 — expected $2 got $3"; FAIL=$((FAIL+1)); }

# Helper: run a python snippet against _gate_common with a clean env (env -u of
# all CLAUDE_BOOSTER_* vars) unless caller exports them inside a subshell.
PYHEAD="import sys; sys.path.insert(0,'$SCRIPTS_DIR'); from _gate_common import real_context_tokens, effective_compact_threshold"

# ----- Fixture builders -----------------------------------------------------
# Build an assistant-usage JSONL line. Sum = i + cr + cc.
asst_line() { # $1=input $2=cache_read $3=cache_creation
  python3 -c "import json;print(json.dumps({'type':'assistant','message':{'role':'assistant','model':'claude-opus-4-8','usage':{'input_tokens':$1,'cache_read_input_tokens':$2,'cache_creation_input_tokens':$3,'output_tokens':50}},'uuid':'u'}))"
}
user_line() {
  python3 -c "import json;print(json.dumps({'type':'user','message':{'role':'user','content':'hi'},'uuid':'u'}))"
}

# Fixture: real total 650300 (100 + 650000 + 200)
F_650300="$TMP/f_650300.jsonl"
{ user_line; asst_line 100 650000 200; } > "$F_650300"

# Fixture: no assistant message
F_NOASST="$TMP/f_noasst.jsonl"
{ user_line; user_line; } > "$F_NOASST"

# Fixture: valid assistant-usage line, then a truncated/garbage final line
F_TRUNC="$TMP/f_trunc.jsonl"
{ asst_line 100 650000 200; printf '%s\n' '{"type":"assistant","message":{"role":"assist'; } > "$F_TRUNC"

# Fixture: two assistant lines, LAST one sums to 12345 (1+12000+344)
F_TWO="$TMP/f_two.jsonl"
{ asst_line 100 650000 200; asst_line 1 12000 344; } > "$F_TWO"

# Fixture: 113038-token session (NO marker expected). 38 + 113000 + 0 = 113038
F_113K="$TMP/f_113k.jsonl"
{ user_line; asst_line 38 113000 0; } > "$F_113K"

# Fixture: exactly 650000 tokens (0 + 650000 + 0)
F_650K="$TMP/f_650k.jsonl"
{ user_line; asst_line 0 650000 0; } > "$F_650K"

# Fixture: 130000 tokens (0 + 130000 + 0)
F_130K="$TMP/f_130k.jsonl"
{ user_line; asst_line 0 130000 0; } > "$F_130K"

# ===========================================================================
# Helper-level cases (real_context_tokens)
# ===========================================================================

# Case 1: real_context_tokens sums the usage block (NOT file byte size).
out=$(env -u CLAUDE_BOOSTER_CONTEXT_WINDOW -u CLAUDE_BOOSTER_COMPACT_PCT -u CLAUDE_BOOSTER_COMPACT_THRESHOLD \
  python3 -c "$PYHEAD; print(real_context_tokens('$F_650300'))" 2>&1)
[ "$out" = "650300" ] && pass "1 real_context_tokens sums usage -> 650300" \
  || fail "1 real_context_tokens sums usage" "650300" "$out"

# Case 2: no assistant message -> None
out=$(python3 -c "$PYHEAD; print(real_context_tokens('$F_NOASST'))" 2>&1)
[ "$out" = "None" ] && pass "2 no assistant -> None" \
  || fail "2 no assistant -> None" "None" "$out"

# Case 3: garbage final line, valid prior line -> prior sum, no crash
out=$(python3 -c "$PYHEAD; print(real_context_tokens('$F_TRUNC'))" 2>&1)
[ "$out" = "650300" ] && pass "3 garbage tail, prior valid -> 650300 (no crash)" \
  || fail "3 garbage tail recovery" "650300" "$out"

# Case 4: LAST assistant usage wins (12345, not 650300)
out=$(python3 -c "$PYHEAD; print(real_context_tokens('$F_TWO'))" 2>&1)
[ "$out" = "12345" ] && pass "4 last assistant usage wins -> 12345" \
  || fail "4 last assistant usage wins" "12345" "$out"

# ===========================================================================
# Helper-level cases (effective_compact_threshold)
# ===========================================================================
# Each runs in a subshell with all CLAUDE_BOOSTER_* unset unless explicitly set.

# Case 5: 113038, no env -> (600000, 1000000)
out=$(env -u CLAUDE_BOOSTER_CONTEXT_WINDOW -u CLAUDE_BOOSTER_COMPACT_PCT -u CLAUDE_BOOSTER_COMPACT_THRESHOLD \
  python3 -c "$PYHEAD; t,w=effective_compact_threshold(113038); print(t,w)" 2>&1)
[ "$out" = "600000 1000000" ] && pass "5 thr(113038) -> (600000, 1000000)" \
  || fail "5 thr(113038)" "600000 1000000" "$out"

# Case 6: 650000, no env -> (600000, 1000000)
out=$(env -u CLAUDE_BOOSTER_CONTEXT_WINDOW -u CLAUDE_BOOSTER_COMPACT_PCT -u CLAUDE_BOOSTER_COMPACT_THRESHOLD \
  python3 -c "$PYHEAD; t,w=effective_compact_threshold(650000); print(t,w)" 2>&1)
[ "$out" = "600000 1000000" ] && pass "6 thr(650000) -> (600000, 1000000)" \
  || fail "6 thr(650000)" "600000 1000000" "$out"

# Case 7: 250000, no env -> window auto-bumps to 1000000 (threshold 600000)
out=$(env -u CLAUDE_BOOSTER_CONTEXT_WINDOW -u CLAUDE_BOOSTER_COMPACT_PCT -u CLAUDE_BOOSTER_COMPACT_THRESHOLD \
  python3 -c "$PYHEAD; t,w=effective_compact_threshold(250000); print(w)" 2>&1)
[ "$out" = "1000000" ] && pass "7 thr(250000) window auto-bump -> 1000000" \
  || fail "7 thr(250000) window auto-bump" "1000000" "$out"

# Case 8: None + CLAUDE_BOOSTER_COMPACT_THRESHOLD=120000 -> threshold == 120000
out=$(env -u CLAUDE_BOOSTER_CONTEXT_WINDOW -u CLAUDE_BOOSTER_COMPACT_PCT CLAUDE_BOOSTER_COMPACT_THRESHOLD=120000 \
  python3 -c "$PYHEAD; t,w=effective_compact_threshold(None); print(t)" 2>&1)
[ "$out" = "120000" ] && pass "8 absolute override THRESHOLD=120000 -> 120000" \
  || fail "8 absolute override" "120000" "$out"

# Case 9: None + CLAUDE_BOOSTER_COMPACT_PCT=0 -> threshold >= 1 (clamped, not 0)
out=$(env -u CLAUDE_BOOSTER_CONTEXT_WINDOW -u CLAUDE_BOOSTER_COMPACT_THRESHOLD CLAUDE_BOOSTER_COMPACT_PCT=0 \
  python3 -c "$PYHEAD; t,w=effective_compact_threshold(None); print(t)" 2>&1)
if [[ "$out" =~ ^[0-9]+$ ]] && [ "$out" -ge 1 ]; then
  pass "9 PCT=0 clamps threshold >= 1 (got $out)"
else
  fail "9 PCT=0 clamp" ">=1 int" "$out"
fi

# Case 10: CONTEXT_WINDOW=notanint -> window == 1000000, no exception
out=$(env -u CLAUDE_BOOSTER_COMPACT_PCT -u CLAUDE_BOOSTER_COMPACT_THRESHOLD CLAUDE_BOOSTER_CONTEXT_WINDOW=notanint \
  python3 -c "$PYHEAD; t,w=effective_compact_threshold(50000); print(w)" 2>&1)
[ "$out" = "1000000" ] && pass "10 CONTEXT_WINDOW=notanint -> window 1000000 (no exc)" \
  || fail "10 bad CONTEXT_WINDOW" "1000000" "$out"

# ===========================================================================
# End-to-end hook cases (compact_advisor.py)
# ===========================================================================
clean_marker() { rm -f "$MARKER"; }

# Case 11: 113k fixture, no env -> exit 0 AND no marker
clean_marker
env -u CLAUDE_BOOSTER_CONTEXT_WINDOW -u CLAUDE_BOOSTER_COMPACT_PCT -u CLAUDE_BOOSTER_COMPACT_THRESHOLD -u CLAUDE_BOOSTER_SKIP_COMPACT_ADVISOR \
  bash -c "echo '{\"session_id\":\"$SID\",\"transcript_path\":\"$F_113K\"}' | python3 '$ADVISOR'" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ] && [ ! -f "$MARKER" ]; then
  pass "11 113k -> exit 0, no marker"
else
  fail "11 113k no-marker" "rc=0 & no marker" "rc=$rc marker_exists=$([ -f "$MARKER" ] && echo yes || echo no)"
fi

# Case 12: 650k fixture, no env -> exit 0 AND marker content "650000 1000000"
clean_marker
env -u CLAUDE_BOOSTER_CONTEXT_WINDOW -u CLAUDE_BOOSTER_COMPACT_PCT -u CLAUDE_BOOSTER_COMPACT_THRESHOLD -u CLAUDE_BOOSTER_SKIP_COMPACT_ADVISOR \
  bash -c "echo '{\"session_id\":\"$SID\",\"transcript_path\":\"$F_650K\"}' | python3 '$ADVISOR'" >/dev/null 2>&1
rc=$?
content=""
[ -f "$MARKER" ] && content="$(tr -d '\n' < "$MARKER" | sed 's/[[:space:]]*$//')"
if [ "$rc" -eq 0 ] && [ "$content" = "650000 1000000" ]; then
  pass "12 650k -> marker content '650000 1000000'"
else
  fail "12 650k marker content" "rc=0 & '650000 1000000'" "rc=$rc content='$content'"
fi

# Case 13: THRESHOLD=120000 + 130k fixture -> marker written
clean_marker
env -u CLAUDE_BOOSTER_CONTEXT_WINDOW -u CLAUDE_BOOSTER_COMPACT_PCT -u CLAUDE_BOOSTER_SKIP_COMPACT_ADVISOR CLAUDE_BOOSTER_COMPACT_THRESHOLD=120000 \
  bash -c "echo '{\"session_id\":\"$SID\",\"transcript_path\":\"$F_130K\"}' | python3 '$ADVISOR'" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ] && [ -f "$MARKER" ]; then
  pass "13 THRESHOLD=120000 + 130k -> marker written"
else
  fail "13 abs override fires" "rc=0 & marker exists" "rc=$rc marker_exists=$([ -f "$MARKER" ] && echo yes || echo no)"
fi

# Case 18: nonexistent transcript -> exit 0 (no crash). (Run before inject cases.)
clean_marker
env -u CLAUDE_BOOSTER_SKIP_COMPACT_ADVISOR \
  bash -c "echo '{\"session_id\":\"$SID\",\"transcript_path\":\"/nonexistent/x.jsonl\"}' | python3 '$ADVISOR'" >/dev/null 2>&1
rc=$?
[ "$rc" -eq 0 ] && pass "18 nonexistent transcript -> exit 0" \
  || fail "18 nonexistent transcript exit 0" "rc=0" "rc=$rc"

# ===========================================================================
# End-to-end hook cases (compact_advisor_inject.py)
# ===========================================================================
write_marker() { printf '%s' "$1" > "$MARKER"; }
run_inject() { echo "{\"session_id\":\"$SID\"}" | python3 "$INJECT" 2>/dev/null; }

# Case 14: marker "650300 1000000" -> additionalContext contains 650,300 / 65% / 1000k; exit 0; marker deleted
clean_marker; write_marker "650300 1000000"
stdout="$(run_inject)"; rc=$?
ctx="$(python3 -c "import sys,json;
try:
  d=json.loads(sys.stdin.read() or '{}')
  print(d.get('hookSpecificOutput',{}).get('additionalContext',''))
except Exception: print('')" <<<"$stdout")"
ok=1
[ "$rc" -eq 0 ] || ok=0
[[ "$ctx" == *"650,300"* ]] || ok=0
[[ "$ctx" == *"65%"* ]] || ok=0
[[ "$ctx" == *"1000k"* ]] || ok=0
[ ! -f "$MARKER" ] || ok=0
if [ "$ok" -eq 1 ]; then
  pass "14 inject '650300 1000000' -> 650,300/65%/1000k, exit 0, marker deleted"
else
  fail "14 inject full advisory" "650,300 & 65% & 1000k & marker gone & rc0" "rc=$rc marker_exists=$([ -f "$MARKER" ] && echo yes || echo no) ctx='${ctx:0:120}'"
fi

# Case 15: legacy single-int marker "650300" -> non-empty advisory, exit 0, marker deleted
clean_marker; write_marker "650300"
stdout="$(run_inject)"; rc=$?
ctx="$(python3 -c "import sys,json;
try:
  d=json.loads(sys.stdin.read() or '{}'); print(d.get('hookSpecificOutput',{}).get('additionalContext',''))
except Exception: print('')" <<<"$stdout")"
if [ "$rc" -eq 0 ] && [ -n "$ctx" ] && [ ! -f "$MARKER" ]; then
  pass "15 legacy single-int marker -> advisory emitted, marker deleted"
else
  fail "15 legacy marker" "rc0 & non-empty ctx & marker gone" "rc=$rc ctx_len=${#ctx} marker_exists=$([ -f "$MARKER" ] && echo yes || echo no)"
fi

# Case 16: malformed marker "garbage" -> exit 0, NO additionalContext, marker removed
clean_marker; write_marker "garbage"
stdout="$(run_inject)"; rc=$?
ctx="$(python3 -c "import sys,json;
try:
  d=json.loads(sys.stdin.read() or '{}'); print(d.get('hookSpecificOutput',{}).get('additionalContext',''))
except Exception: print('')" <<<"$stdout")"
if [ "$rc" -eq 0 ] && [ -z "$ctx" ] && [ ! -f "$MARKER" ]; then
  pass "16 malformed marker -> exit 0, no ctx, marker removed"
else
  fail "16 malformed marker" "rc0 & empty ctx & marker gone" "rc=$rc ctx='${ctx:0:80}' marker_exists=$([ -f "$MARKER" ] && echo yes || echo no)"
fi

# Case 17: one-shot — first run emits, second run does not
clean_marker; write_marker "650300 1000000"
s1="$(run_inject)"; rc1=$?
s2="$(run_inject)"; rc2=$?
ctx1="$(python3 -c "import sys,json;
try: print(json.loads(sys.stdin.read() or '{}').get('hookSpecificOutput',{}).get('additionalContext',''))
except Exception: print('')" <<<"$s1")"
ctx2="$(python3 -c "import sys,json;
try: print(json.loads(sys.stdin.read() or '{}').get('hookSpecificOutput',{}).get('additionalContext',''))
except Exception: print('')" <<<"$s2")"
if [ "$rc1" -eq 0 ] && [ "$rc2" -eq 0 ] && [ -n "$ctx1" ] && [ -z "$ctx2" ]; then
  pass "17 one-shot: first emits, second silent"
else
  fail "17 one-shot" "ctx1 non-empty, ctx2 empty, both rc0" "rc1=$rc1 rc2=$rc2 ctx1_len=${#ctx1} ctx2_len=${#ctx2}"
fi

# Case 19: NO marker present -> exit 0, no additionalContext
clean_marker
stdout="$(run_inject)"; rc=$?
ctx="$(python3 -c "import sys,json;
try: print(json.loads(sys.stdin.read() or '{}').get('hookSpecificOutput',{}).get('additionalContext',''))
except Exception: print('')" <<<"$stdout")"
if [ "$rc" -eq 0 ] && [ -z "$ctx" ]; then
  pass "19 no marker -> exit 0, no ctx"
else
  fail "19 no marker" "rc0 & empty ctx" "rc=$rc ctx='${ctx:0:80}'"
fi

# ===========================================================================
# Parity: deployed == template (byte-identical) for all three files
# ===========================================================================
# Case 20a
if diff -q "$GATE_COMMON" "$TEMPLATES_DIR/_gate_common.py" >/dev/null 2>&1; then
  pass "20a _gate_common.py deployed == template"
else
  fail "20a _gate_common.py parity" "identical" "differ"
fi
# Case 20b
if diff -q "$ADVISOR" "$TEMPLATES_DIR/compact_advisor.py" >/dev/null 2>&1; then
  pass "20b compact_advisor.py deployed == template"
else
  fail "20b compact_advisor.py parity" "identical" "differ"
fi
# Case 20c
if diff -q "$INJECT" "$TEMPLATES_DIR/compact_advisor_inject.py" >/dev/null 2>&1; then
  pass "20c compact_advisor_inject.py deployed == template"
else
  fail "20c compact_advisor_inject.py parity" "identical" "differ"
fi

# ===========================================================================
# Audit-fix regression cases (code-review 2026-06-02)
# ===========================================================================

# Case 21 [#8] — explicit CLAUDE_BOOSTER_CONTEXT_WINDOW is NOT overridden by the
# >200k auto-bump. Explicit 500k + observed 250k must stay (300000, 500000).
out="$(CLAUDE_BOOSTER_CONTEXT_WINDOW=500000 python3 -c "$PYHEAD; print(effective_compact_threshold(250000))")"
if [ "$out" = "(300000, 500000)" ]; then
  pass "21 explicit window 500k respected (no auto-bump) -> (300000, 500000)"
else
  fail "21 explicit window respected" "(300000, 500000)" "$out"
fi

# Case 22 [#2] — a non-positive CLAUDE_BOOSTER_CONTEXT_WINDOW falls back to the
# 1M default; threshold must NOT clamp to 1 (which would fire every call).
out="$(CLAUDE_BOOSTER_CONTEXT_WINDOW=0 python3 -c "$PYHEAD; print(effective_compact_threshold(5000))")"
if [ "$out" = "(600000, 1000000)" ]; then
  pass "22 window=0 -> default 1M, threshold 600k (not 1)"
else
  fail "22 window=0 sanitized" "(600000, 1000000)" "$out"
fi

# Case 23 [#1] — a present-but-null usage field must not crash real_context_tokens;
# null coerces to 0 and the non-null fields still sum.
NULLF="$TMP/null_usage.jsonl"
python3 -c "import json;open('$NULLF','w').write(json.dumps({'type':'assistant','message':{'role':'assistant','usage':{'input_tokens':None,'cache_read_input_tokens':50000,'cache_creation_input_tokens':None}}})+'\n')"
out="$(python3 -c "$PYHEAD; print(real_context_tokens('$NULLF'))")"
if [ "$out" = "50000" ]; then
  pass "23 null usage field -> 50000 (no crash)"
else
  fail "23 null usage field" "50000" "$out"
fi

echo "----------------------------------------"
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
