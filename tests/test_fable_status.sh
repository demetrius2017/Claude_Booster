#!/usr/bin/env bash
# ============================================================================
# test_fable_status.sh — INDEPENDENT acceptance test for the Fable statusline
# feature (fable_usage.py summary/refresh-session + statusline.sh rendering).
#
# Isolation strategy: fable_usage.py derives DB_PATH / SUMMARY_CACHE_PATH from
# Path.home(), and statusline.sh reads ${HOME}/.claude/... . Both respect $HOME,
# so we run every case under a throwaway fake HOME. The real ledger
# (~/.claude/rolling_memory.db) and real cache (~/.claude/fable_usage_summary.json)
# are NEVER touched. As a defensive belt-and-suspenders, we also snapshot their
# mtimes at start and verify they are unchanged at the end.
#
# Covers assertions A1..A16, the stated invariants, and the required branches.
# Prints [PASS]/[FAIL] per case, a final "Results: N passed, M failed", and
# exits 0 iff all pass.
# ============================================================================
set -u

REPO="/Users/dmitrijnazarov/Projects/Claude_Booster"
FABLE_PY="${HOME}/.claude/scripts/fable_usage.py"
STATUSLINE="${HOME}/.claude/scripts/statusline.sh"
TMPL_PY="${REPO}/templates/scripts/fable_usage.py"
TMPL_SL="${REPO}/templates/scripts/statusline.sh"

REAL_DB="${HOME}/.claude/rolling_memory.db"
REAL_CACHE="${HOME}/.claude/fable_usage_summary.json"

RED=$'\033[31m'
GREEN=$'\033[32m'

PASS=0
FAIL=0

WORKROOT="$(mktemp -d "${TMPDIR:-/tmp}/fable_test.XXXXXX")"

# Snapshot real-data mtimes to prove non-mutation.
_mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo MISSING; }
REAL_DB_MT0="$(_mtime "$REAL_DB")"
REAL_CACHE_MT0="$(_mtime "$REAL_CACHE")"

cleanup() {
    # Best-effort: reap any lingering backgrounded refresh children spawned into
    # a fake HOME, then remove the work tree. Never touches real data.
    pkill -f "$WORKROOT" 2>/dev/null
    rm -rf "$WORKROOT" 2>/dev/null
}
trap cleanup EXIT

pass() { PASS=$((PASS+1)); printf '[PASS] %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf '[FAIL] %s\n' "$1"; [ -n "${2:-}" ] && printf '       expected: %s\n' "$2"; [ -n "${3:-}" ] && printf '       got     : %s\n' "$3"; }

# newhome: create an isolated fake HOME with the real fable_usage.py installed at
# the path statusline.sh expects. Echoes the fake HOME dir.
newhome() {
    local fh; fh="$(mktemp -d "${WORKROOT}/home.XXXXXX")"
    mkdir -p "$fh/.claude/scripts"
    cp "$FABLE_PY" "$fh/.claude/scripts/fable_usage.py"
    echo "$fh"
}

# emit a single assistant Fable JSONL row: ts, msgid, output_tokens
row() { printf '{"type":"assistant","timestamp":"%s","message":{"id":"%s","role":"assistant","model":"claude-fable-5","usage":{"output_tokens":%s}}}\n' "$1" "$2" "$3"; }

# run fable_usage.py under a fake HOME
fu() { local fh="$1"; shift; HOME="$fh" python3 "$FABLE_PY" "$@"; }
# run sqlite against a fake HOME's ledger
db() { local fh="$1"; shift; sqlite3 "$fh/.claude/rolling_memory.db" "$1"; }

NOW_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ===========================================================================
# A1 — summary shows today (Dubai +4h) & session sums; today == independent SQL
# ===========================================================================
{
    FH="$(newhome)"; SID="sess-A1"
    T="$FH/t.jsonl"; row "$NOW_UTC" "m-a1-1" 20000 > "$T"   # $1.0000
    row "$NOW_UTC" "m-a1-2" 40000 >> "$T"                    # $2.0000
    fu "$FH" refresh-session --session "$SID" --transcript "$T" >/dev/null 2>&1
    J="$(fu "$FH" summary --session "$SID" --json 2>/dev/null)"
    has_today=$(printf '%s' "$J" | jq -r 'has("today")')
    has_sess=$(printf '%s' "$J" | jq -r 'has("session")')
    script_today=$(printf '%s' "$J" | jq -r '.today.cost_usd_nanos')
    indep_today=$(db "$FH" "SELECT COALESCE(SUM(cost_usd_nanos),0) FROM fable_usage_events WHERE date(replace(ts_utc,'Z',''),'+4 hours')=date('now','+4 hours')")
    sess=$(printf '%s' "$J" | jq -r '.session.cost_usd_nanos')
    if [ "$has_today" = "true" ] && [ "$has_sess" = "true" ] && [ "$script_today" = "$indep_today" ] && [ "$sess" = "3000000000" ]; then
        pass "A1 today+session present; today==independent SQL ($script_today); session=\$3"
    else
        fail "A1 today/session sums" "today keys=true, script_today==indep, session=3000000000" "has_today=$has_today has_sess=$has_sess script=$script_today indep=$indep_today session=$sess"
    fi
}

# ===========================================================================
# A2 — local-day boundary: 22:00Z counts under TOMORROW Dubai, not that UTC day
# ===========================================================================
{
    FH="$(newhome)"; SID="sess-A2"
    T="$FH/t.jsonl"; row "2026-06-15T22:00:00Z" "m-a2" 20000 > "$T"
    fu "$FH" refresh-session --session "$SID" --transcript "$T" >/dev/null 2>&1
    utc_day=$(db "$FH" "SELECT date(replace(ts_utc,'Z','')) FROM fable_usage_events LIMIT 1")
    local_day=$(db "$FH" "SELECT date(replace(ts_utc,'Z',''),'+4 hours') FROM fable_usage_events LIMIT 1")
    cnt_utcbucket=$(db "$FH" "SELECT COUNT(*) FROM fable_usage_events WHERE date(replace(ts_utc,'Z',''),'+4 hours')='2026-06-15'")
    cnt_nextbucket=$(db "$FH" "SELECT COUNT(*) FROM fable_usage_events WHERE date(replace(ts_utc,'Z',''),'+4 hours')='2026-06-16'")
    if [ "$local_day" = "2026-06-16" ] && [ "$utc_day" = "2026-06-15" ] && [ "$cnt_utcbucket" = "0" ] && [ "$cnt_nextbucket" = "1" ]; then
        pass "A2 22:00Z -> local $local_day (excluded from 06-15 bucket, in 06-16)"
    else
        fail "A2 local-day boundary" "local=2026-06-16 utc=2026-06-15 bucket15=0 bucket16=1" "local=$local_day utc=$utc_day b15=$cnt_utcbucket b16=$cnt_nextbucket"
    fi
}

# ===========================================================================
# A3 — per-session SUM includes a subagents/*.jsonl event of same session_id
# ===========================================================================
{
    FH="$(newhome)"; SID="sess-A3"
    D="$FH/proj"; mkdir -p "$D/subagents"
    row "$NOW_UTC" "m-a3-main" 20000 > "$D/main.jsonl"       # $1 main
    row "$NOW_UTC" "m-a3-sub"  40000 > "$D/subagents/s.jsonl" # $2 subagent
    fu "$FH" refresh-session --session "$SID" --transcript "$D/main.jsonl" >/dev/null 2>&1
    J="$(fu "$FH" summary --session "$SID" --json 2>/dev/null)"
    sess=$(printf '%s' "$J" | jq -r '.session.cost_usd_nanos')
    lasttask=$(printf '%s' "$J" | jq -r '.last_task.cost_usd_nanos')
    sess_ev=$(printf '%s' "$J" | jq -r '.session.events')
    # session sums BOTH ($3); last_task is one source_path only (<= $2 < $3)
    if [ "$sess" = "3000000000" ] && [ "$sess_ev" = "2" ] && [ "$sess" -gt "$lasttask" ]; then
        pass "A3 session sum spans main+subagent (\$3, 2 events) > last_task (\$$(python3 -c "print($lasttask/1e9)"))"
    else
        fail "A3 multi-source session sum" "session=3000000000 events=2 session>last_task" "session=$sess events=$sess_ev last_task=$lasttask"
    fi
}

# ===========================================================================
# A4 — refresh-session run TWICE => identical totals; 2nd run inserts 0 rows
# ===========================================================================
{
    FH="$(newhome)"; SID="sess-A4"
    T="$FH/t.jsonl"; row "$NOW_UTC" "m-a4-1" 20000 > "$T"; row "$NOW_UTC" "m-a4-2" 30000 >> "$T"
    fu "$FH" refresh-session --session "$SID" --transcript "$T" >/dev/null 2>&1
    rows1=$(db "$FH" "SELECT COUNT(*) FROM fable_usage_events")
    fu "$FH" summary --session "$SID" --json 2>/dev/null | jq -S 'del(.updated_at,.last_event.ts_utc,.last_task.ts_utc)' > "$FH/s1.json"
    fu "$FH" refresh-session --session "$SID" --transcript "$T" >/dev/null 2>&1
    rows2=$(db "$FH" "SELECT COUNT(*) FROM fable_usage_events")
    fu "$FH" summary --session "$SID" --json 2>/dev/null | jq -S 'del(.updated_at,.last_event.ts_utc,.last_task.ts_utc)' > "$FH/s2.json"
    if [ "$rows1" = "$rows2" ] && diff -q "$FH/s1.json" "$FH/s2.json" >/dev/null; then
        pass "A4 idempotent: rows stable ($rows1==$rows2), totals identical across 2 runs"
    else
        fail "A4 idempotency" "rows equal + identical totals" "rows1=$rows1 rows2=$rows2 diff=$(diff "$FH/s1.json" "$FH/s2.json" | head -5)"
    fi
}

# ===========================================================================
# A5 — statusline: both 5h+7d when present; absent => no 5h/7d; 5h-only branch
# ===========================================================================
sl() { local fh="$1" json="$2"; printf '%s' "$json" | HOME="$fh" bash "$STATUSLINE" 2>/dev/null; }
{
    FH="$(newhome)"
    both='{"model":{"display_name":"Fable"},"context_window":{"used_percentage":10},"rate_limits":{"five_hour":{"used_percentage":20},"seven_day":{"used_percentage":40}}}'
    absent='{"model":{"display_name":"Fable"},"context_window":{"used_percentage":10}}'
    only5='{"model":{"display_name":"Fable"},"context_window":{"used_percentage":10},"rate_limits":{"five_hour":{"used_percentage":20}}}'
    ob="$(sl "$FH" "$both")";     rcb=$?
    oa="$(sl "$FH" "$absent")";   rca=$?
    o5="$(sl "$FH" "$only5")";    rc5=$?
    ok=1; why=""
    printf '%s' "$ob" | grep -qF "5h:" && printf '%s' "$ob" | grep -qF "7d:" || { ok=0; why="both-missing-segment"; }
    if printf '%s' "$oa" | grep -qE "5h:|7d:"; then ok=0; why="$why absent-has-segment"; fi
    printf '%s' "$o5" | grep -qF "5h:" || { ok=0; why="$why only5-missing-5h"; }
    if printf '%s' "$o5" | grep -qF "7d:"; then ok=0; why="$why only5-has-7d"; fi
    [ "$rcb" -eq 0 ] && [ "$rca" -eq 0 ] && [ "$rc5" -eq 0 ] || { ok=0; why="$why nonzero-exit($rcb,$rca,$rc5)"; }
    if [ "$ok" -eq 1 ]; then
        pass "A5 both->5h+7d, absent->none (exit0), 5h-only->5h no 7d"
    else
        fail "A5 rate-limit rendering branches" "both:5h+7d absent:none 5h-only:5h" "$why | both=[$ob] absent=[$oa] only5=[$o5]"
    fi
}

# ===========================================================================
# A6 — REMAINING arithmetic: used=37 => shows 63 (100-37), NOT 37
# ===========================================================================
{
    FH="$(newhome)"
    j='{"model":{"display_name":"F"},"context_window":{"used_percentage":10},"rate_limits":{"five_hour":{"used_percentage":37},"seven_day":{"used_percentage":37}}}'
    o="$(sl "$FH" "$j")"
    # strip ANSI for the numeric check
    plain="$(printf '%s' "$o" | sed $'s/\033\\[[0-9;]*m//g')"
    if printf '%s' "$plain" | grep -qF "5h: 63%" && printf '%s' "$plain" | grep -qF "7d: 63%" && ! printf '%s' "$plain" | grep -qE "5h: 37%|7d: 37%"; then
        pass "A6 remaining=100-used: 5h/7d show 63% (not 37%)"
    else
        fail "A6 remaining arithmetic" "5h: 63% and 7d: 63%, not 37%" "plain=[$plain]"
    fi
}

# ===========================================================================
# A7 — COLOR keyed on USED: used=95 => RED, used=10 => GREEN (text=remaining)
# ===========================================================================
{
    FH="$(newhome)"
    # ctx green (10), 5h used=95 -> red present (ctx is not red -> proves 5h red)
    jred='{"model":{"display_name":"F"},"context_window":{"used_percentage":10},"rate_limits":{"five_hour":{"used_percentage":95}}}'
    # ctx red (95), 5h used=10 -> green present (ctx is not green -> proves 5h green)
    jgrn='{"model":{"display_name":"F"},"context_window":{"used_percentage":95},"rate_limits":{"five_hour":{"used_percentage":10}}}'
    ored="$(sl "$FH" "$jred")"
    ogrn="$(sl "$FH" "$jgrn")"
    ok=1; why=""
    printf '%s' "$ored" | grep -qF "$RED" || { ok=0; why="no-red-when-used95"; }
    printf '%s' "$ogrn" | grep -qF "$GREEN" || { ok=0; why="$why no-green-when-used10"; }
    if [ "$ok" -eq 1 ]; then
        pass "A7 color keyed on USED: used95->RED, used10->GREEN"
    else
        fail "A7 color semantics" "used95 red ANSI, used10 green ANSI" "$why"
    fi
}

# ===========================================================================
# A8 — cache back-compat keys + fable_tmux_status.sh prints NO Fable $ (removed 2026-11-01)
# ===========================================================================
{
    FH="$(newhome)"; SID="sess-A8"
    T="$FH/t.jsonl"; row "$NOW_UTC" "m-a8" 20000 > "$T"
    fu "$FH" refresh-session --session "$SID" --transcript "$T" >/dev/null 2>&1
    C="$FH/.claude/fable_usage_summary.json"
    de=$(jq -e '.display_enabled != null' "$C" >/dev/null 2>&1 && echo ok || echo no)
    lt=$(jq -e 'has("last_task")' "$C" >/dev/null 2>&1 && echo ok || echo no)
    mtd=$(jq -e '.mtd != null' "$C" >/dev/null 2>&1 && echo ok || echo no)
    se=$(jq -e '.session != null' "$C" >/dev/null 2>&1 && echo ok || echo no)
    td=$(jq -e '.today != null' "$C" >/dev/null 2>&1 && echo ok || echo no)
    tmux_out="$(HOME="$FH" bash "${HOME}/.claude/scripts/fable_tmux_status.sh" 2>/dev/null)"
    tmux_ok=yes; printf '%s' "$tmux_out" | grep -qE '\$|F l' && tmux_ok=no
    if [ "$de" = ok ] && [ "$lt" = ok ] && [ "$mtd" = ok ] && [ "$se" = ok ] && [ "$td" = ok ] && [ "$tmux_ok" = yes ]; then
        pass "A8 cache keys (display_enabled,last_task,mtd,session,today) + tmux shows no Fable \$ ($tmux_out)"
    else
        fail "A8 back-compat cache keys" "all keys present + tmux without Fable \$" "de=$de lt=$lt mtd=$mtd session=$se today=$td tmux=[$tmux_out]"
    fi
}

# ===========================================================================
# A9 — templates parity: deployed == templates (byte-identical)
# ===========================================================================
{
    d1="$(diff "$FABLE_PY" "$TMPL_PY" 2>&1)"
    d2="$(diff "$STATUSLINE" "$TMPL_SL" 2>&1)"
    if [ -z "$d1" ] && [ -z "$d2" ]; then
        pass "A9 templates parity: fable_usage.py & statusline.sh byte-identical to templates/"
    else
        fail "A9 templates parity" "empty diffs" "fable_usage.py:[$d1] statusline.sh:[$d2]"
    fi
}

# ===========================================================================
# A10 — all-zero-cost transcript => session/today render \$0.0000 (COALESCE)
# ===========================================================================
{
    FH="$(newhome)"; SID="sess-A10"
    T="$FH/t.jsonl"
    printf '{"type":"assistant","timestamp":"%s","message":{"id":"m-a10","role":"assistant","model":"claude-fable-5","usage":{"input_tokens":0,"output_tokens":0,"cache_read_input_tokens":0}}}\n' "$NOW_UTC" > "$T"
    fu "$FH" refresh-session --session "$SID" --transcript "$T" >/dev/null 2>&1
    J="$(fu "$FH" summary --session "$SID" --json 2>/dev/null)"
    valid=$(printf '%s' "$J" | jq -e . >/dev/null 2>&1 && echo ok || echo no)
    st=$(printf '%s' "$J" | jq -r '.session.cost_usd')
    tt=$(printf '%s' "$J" | jq -r '.today.cost_usd')
    if [ "$valid" = ok ] && [ "$st" = "0.0000" ] && [ "$tt" = "0.0000" ]; then
        pass "A10 all-zero transcript -> session/today \$0.0000, valid JSON, no crash"
    else
        fail "A10 zero-cost COALESCE" "session=today=0.0000, valid json" "valid=$valid session=$st today=$tt"
    fi
}

# ===========================================================================
# A11 — fresh install: DB absent => refresh-session CREATES DB, ingests (>0)
# ===========================================================================
{
    FH="$(newhome)"; SID="sess-A11"
    DBP="$FH/.claude/rolling_memory.db"
    pre="present"; [ -f "$DBP" ] || pre="absent"
    T="$FH/t.jsonl"; row "$NOW_UTC" "m-a11" 20000 > "$T"
    fu "$FH" refresh-session --session "$SID" --transcript "$T" >/dev/null 2>&1
    post="absent"; [ -f "$DBP" ] && post="present"
    cnt=$(db "$FH" "SELECT COUNT(*) FROM fable_usage_events" 2>/dev/null || echo 0)
    if [ "$pre" = absent ] && [ "$post" = present ] && [ "${cnt:-0}" -gt 0 ]; then
        pass "A11 fresh-install bootstrap: DB created, ingested $cnt rows"
    else
        fail "A11 fresh-install DB bootstrap" "pre=absent post=present count>0" "pre=$pre post=$post count=$cnt"
    fi
}

# ===========================================================================
# A12 — statusline NEVER blocks even on a slow refresh child (< ~1s wall)
# ===========================================================================
{
    FH="$(newhome)"
    # Replace the refresh binary with a 3s sleeper (ignores argv).
    printf 'import time\ntime.sleep(3)\n' > "$FH/.claude/scripts/fable_usage.py"
    TR="$FH/real.jsonl"; row "$NOW_UTC" "m-a12" 20000 > "$TR"   # existing transcript so tpath resolves
    j="{\"model\":{\"display_name\":\"F\"},\"context_window\":{\"used_percentage\":10},\"session_id\":\"sess-A12\",\"transcript_path\":\"$TR\"}"
    start=$(python3 -c 'import time;print(time.time())')
    sl "$FH" "$j" >/dev/null 2>&1
    end=$(python3 -c 'import time;print(time.time())')
    elapsed=$(python3 -c "print($end-$start)")
    fast=$(python3 -c "print(1 if ($end-$start) < 1.5 else 0)")
    if [ "$fast" = 1 ]; then
        pass "A12 statusline returns in ${elapsed}s (<1.5s) despite 3s background refresh"
    else
        fail "A12 non-blocking" "<1.5s wall" "elapsed=${elapsed}s"
    fi
}

# ===========================================================================
# A13 — throttle: cache mtime=now => two runs within 30s spawn at most ONE child
# ===========================================================================
{
    FH="$(newhome)"
    # Counter wrapper: each spawn appends a byte to ~/.claude/spawn_count (child
    # inherits HOME=$FH so ~ resolves into the fake home).
    printf 'import os\np=os.path.expanduser("~/.claude/spawn_count")\nopen(p,"a").write("1")\n' > "$FH/.claude/scripts/fable_usage.py"
    C="$FH/.claude/fable_usage_summary.json"
    printf '{"display_enabled":true,"session":{"cost_usd":"0.0000"},"today":{"cost_usd":"0.0000"}}\n' > "$C"
    touch "$C"   # mtime = now => age < 30 => throttled
    TR="$FH/real.jsonl"; row "$NOW_UTC" "m-a13" 20000 > "$TR"
    j="{\"model\":{\"display_name\":\"F\"},\"context_window\":{\"used_percentage\":10},\"session_id\":\"sess-A13\",\"transcript_path\":\"$TR\"}"
    sl "$FH" "$j" >/dev/null 2>&1
    sl "$FH" "$j" >/dev/null 2>&1
    sleep 0.5   # let any (wrongly) spawned child write
    n=0; [ -f "$FH/.claude/spawn_count" ] && n=$(wc -c < "$FH/.claude/spawn_count" | tr -d ' ')
    if [ "${n:-0}" -le 1 ]; then
        pass "A13 throttle holds: fresh cache => $n refresh spawns (<=1) across 2 runs"
    else
        fail "A13 throttle" "<=1 spawn" "spawns=$n"
    fi
}

# ===========================================================================
# A14 — future-dated cache mtime does NOT disable refresh (negative-age guard)
# ===========================================================================
{
    FH="$(newhome)"
    printf 'import os\np=os.path.expanduser("~/.claude/spawn_count")\nopen(p,"a").write("1")\n' > "$FH/.claude/scripts/fable_usage.py"
    C="$FH/.claude/fable_usage_summary.json"
    printf '{"display_enabled":true,"session":{"cost_usd":"0.0000"},"today":{"cost_usd":"0.0000"}}\n' > "$C"
    touch -t 203012312359 "$C"   # far-future mtime => age negative => guard => spawn
    TR="$FH/real.jsonl"; row "$NOW_UTC" "m-a14" 20000 > "$TR"
    j="{\"model\":{\"display_name\":\"F\"},\"context_window\":{\"used_percentage\":10},\"session_id\":\"sess-A14\",\"transcript_path\":\"$TR\"}"
    sl "$FH" "$j" >/dev/null 2>&1
    sleep 0.5
    n=0; [ -f "$FH/.claude/spawn_count" ] && n=$(wc -c < "$FH/.claude/spawn_count" | tr -d ' ')
    if [ "${n:-0}" -ge 1 ]; then
        pass "A14 future mtime still triggers refresh ($n spawn, negative-age guard works)"
    else
        fail "A14 future-mtime negative-age guard" ">=1 spawn" "spawns=$n"
    fi
}

# ===========================================================================
# A15 — MTD stays UTC month: 22:00Z on last UTC day of month stays that month
# ===========================================================================
{
    FH="$(newhome)"; SID="sess-A15"
    T="$FH/t.jsonl"; row "2026-06-30T22:00:00Z" "m-a15" 20000 > "$T"
    fu "$FH" refresh-session --session "$SID" --transcript "$T" >/dev/null 2>&1
    month_utc=$(db "$FH" "SELECT month_utc FROM fable_usage_events LIMIT 1")
    local_day=$(db "$FH" "SELECT date(replace(ts_utc,'Z',''),'+4 hours') FROM fable_usage_events LIMIT 1")
    # UTC month must be 2026-06 even though Dubai-local date is 2026-07-01
    if [ "$month_utc" = "2026-06" ] && [ "$local_day" = "2026-07-01" ]; then
        pass "A15 MTD UTC: month_utc=2026-06 (local day $local_day, NOT shifted +4h)"
    else
        fail "A15 MTD stays UTC" "month_utc=2026-06 while local=2026-07-01" "month_utc=$month_utc local=$local_day"
    fi
}

# ===========================================================================
# A16 — concurrent refresh-session writers never corrupt cache (atomic replace)
# ===========================================================================
{
    FH="$(newhome)"; SID="sess-A16"
    T="$FH/t.jsonl"; row "$NOW_UTC" "m-a16" 20000 > "$T"
    fu "$FH" refresh-session --session "$SID" --transcript "$T" >/dev/null 2>&1  # seed cache
    C="$FH/.claude/fable_usage_summary.json"
    # N parallel writers
    for i in $(seq 1 10); do
        HOME="$FH" python3 "$FABLE_PY" refresh-session --session "$SID" --transcript "$T" >/dev/null 2>&1 &
    done
    parse_err=0
    for k in $(seq 1 60); do
        if [ -f "$C" ]; then jq -e . "$C" >/dev/null 2>&1 || parse_err=$((parse_err+1)); fi
    done
    wait
    final_ok=no; jq -e . "$C" >/dev/null 2>&1 && final_ok=yes
    if [ "$parse_err" -eq 0 ] && [ "$final_ok" = yes ]; then
        pass "A16 concurrency: 10 parallel writers, 0 jq parse errors, final cache valid"
    else
        fail "A16 concurrent cache integrity" "0 parse errors + valid final" "parse_err=$parse_err final_ok=$final_ok"
    fi
}

# ===========================================================================
# INV1 — statusline exit code == 0 for ALL adversarial inputs
# ===========================================================================
{
    FH="$(newhome)"
    ok=1; details=""
    # empty stdin
    printf '' | HOME="$FH" bash "$STATUSLINE" >/dev/null 2>&1 || { ok=0; details="$details empty"; }
    # garbage (non-JSON)
    printf 'not json at all {{{' | HOME="$FH" bash "$STATUSLINE" >/dev/null 2>&1 || { ok=0; details="$details garbage"; }
    # rate_limits absent
    printf '{"model":{"display_name":"F"},"context_window":{"used_percentage":50}}' | HOME="$FH" bash "$STATUSLINE" >/dev/null 2>&1 || { ok=0; details="$details rl-absent"; }
    # corrupt cache present
    printf 'THIS IS NOT JSON' > "$FH/.claude/fable_usage_summary.json"
    printf '{"model":{"display_name":"F"},"context_window":{"used_percentage":50},"session_id":"x"}' | HOME="$FH" bash "$STATUSLINE" >/dev/null 2>&1 || { ok=0; details="$details corrupt-cache"; }
    # transcript_path missing file
    printf '{"context_window":{"used_percentage":50},"session_id":"x","transcript_path":"/no/such/file.jsonl"}' | HOME="$FH" bash "$STATUSLINE" >/dev/null 2>&1 || { ok=0; details="$details missing-transcript"; }
    if [ "$ok" -eq 1 ]; then
        pass "INV1 statusline exit 0 on all adversarial inputs (empty/garbage/rl-absent/corrupt-cache/missing-transcript)"
    else
        fail "INV1 statusline always exit 0" "exit 0 for every input" "nonzero on:$details"
    fi
}

# ===========================================================================
# INV2 — session.cost == COALESCE(SUM WHERE session_id) with NO source_path grouping
#         (verified against independent SQL over a multi-source session)
# ===========================================================================
{
    FH="$(newhome)"; SID="sess-INV2"
    D="$FH/proj"; mkdir -p "$D/subagents"
    row "$NOW_UTC" "m-i2-a" 20000 > "$D/main.jsonl"
    row "$NOW_UTC" "m-i2-b" 30000 > "$D/subagents/x.jsonl"
    row "$NOW_UTC" "m-i2-c" 50000 > "$D/subagents/y.jsonl"
    fu "$FH" refresh-session --session "$SID" --transcript "$D/main.jsonl" >/dev/null 2>&1
    script_sess=$(fu "$FH" summary --session "$SID" --json 2>/dev/null | jq -r '.session.cost_usd_nanos')
    indep=$(db "$FH" "SELECT COALESCE(SUM(cost_usd_nanos),0) FROM fable_usage_events WHERE session_id='$SID'")
    if [ "$script_sess" = "$indep" ] && [ "$script_sess" = "5000000000" ]; then
        pass "INV2 session.cost == SUM WHERE session_id (\$5 across 3 sources), no source grouping"
    else
        fail "INV2 session sum predicate" "script==indep==5000000000" "script=$script_sess indep=$indep"
    fi
}

# ===========================================================================
# INV3 — write_summary_cache uses a per-PROCESS tmp (os.getpid), not a fixed name
# ===========================================================================
{
    # Extract the write_summary_cache function body (until next top-level def).
    body=$(awk '/^def write_summary_cache/{f=1;print;next} f&&/^def /{exit} f{print}' "$FABLE_PY")
    if printf '%s' "$body" | grep -q 'getpid' && printf '%s' "$body" | grep -q 'os.replace('; then
        pass "INV3 write_summary_cache tmp is per-process (os.getpid) + atomic os.replace"
    else
        fail "INV3 unique per-process tmp" "getpid in tmp name + os.replace" "body=[$(printf '%s' "$body" | tr '\n' '|')]"
    fi
}

# ===========================================================================
# Non-mutation guard: real ledger & cache untouched
# ===========================================================================
{
    db1="$(_mtime "$REAL_DB")"; c1="$(_mtime "$REAL_CACHE")"
    if [ "$db1" = "$REAL_DB_MT0" ] && [ "$c1" = "$REAL_CACHE_MT0" ]; then
        pass "GUARD real ledger & cache mtimes unchanged (no mutation of user data)"
    else
        fail "GUARD non-mutation" "real db/cache mtimes unchanged" "db:$REAL_DB_MT0->$db1 cache:$REAL_CACHE_MT0->$c1"
    fi
}

echo "----------------------------------------------------------------------"
echo "Results: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
