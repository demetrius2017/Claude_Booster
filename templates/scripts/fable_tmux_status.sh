#!/usr/bin/env bash
# fable_tmux_status.sh — compact tmux status widget.
# Shows remaining Max rate-limit windows (5h/7d) and Fable spend (last task +
# month-to-date). Rate limits come from ~/.claude/.rate_limits_cache.json, which the
# Claude Code statusline (statusline.sh) refreshes from its stdin while a session
# is active; Fable spend comes from the fable_usage.py summary cache. Both are
# best-effort — the widget exits 0 on every failure and simply omits a segment
# whose source is missing or stale.

summary="${HOME}/.claude/fable_usage_summary.json"
rlcache="${HOME}/.claude/.rate_limits_cache.json"
RL_STALE_S=900   # hide rate limits if the statusline hasn't refreshed in 15 min

command -v jq >/dev/null 2>&1 || exit 0

parts=""

# --- Rate limits (remaining %) — only if fresh ---
if [ -f "$rlcache" ]; then
    now=$(date +%s 2>/dev/null || echo 0)
    upd=$(jq -r '.updated_at // 0' "$rlcache" 2>/dev/null)
    [[ "$upd" =~ ^[0-9]+$ ]] || upd=0
    if [ "$upd" -gt 0 ] && [ $(( now - upd )) -le "$RL_STALE_S" ]; then
        h5=$(jq -r '.five_hour_remaining // empty' "$rlcache" 2>/dev/null)
        d7=$(jq -r '.seven_day_remaining // empty' "$rlcache" 2>/dev/null)
        [ -n "$h5" ] && parts="${parts}5h:${h5}% "
        [ -n "$d7" ] && parts="${parts}7d:${d7}% "
    fi
fi

# --- Fable spend (last task + month-to-date) ---
# session/today both legitimately go $0 (fresh session in another project, or
# just past the Dubai-midnight rollover) and read as "broken". last_task + mtd
# are always populated once any Fable spend exists, so the widget stays useful.
if [ -f "$summary" ]; then
    enabled=$(jq -r 'select(.display_enabled == true) | .display_enabled // empty' "$summary" 2>/dev/null)
    if [ "$enabled" = "true" ]; then
        # Format inside jq (always '.' decimal, locale-independent + always 2dp).
        # printf '%.2f' honours LC_NUMERIC ("0,15" / thousands-grouped garbage on
        # comma-locale hosts); plain jq division drops the decimal on whole values
        # ("$1" not "$1.00"). Build cents as an integer, then pad to D.CC.
        # Compact single-letter labels (l/d/m) because status-right-length is 40
        # and shared with pane_title + clock; the CC statusline uses full words.
        last=$(jq -r 'if (.last_task.cost_usd_nanos // 0) > 0 then (.last_task.cost_usd_nanos / 1e7 | round) as $c | "\(($c / 100) | floor).\(($c % 100 | tostring) | if length < 2 then "0" + . else . end)" else empty end' "$summary" 2>/dev/null)
        # today (local Dubai day) is shown even at $0 — that IS the running-total signal.
        today=$(jq -r '(.today.cost_usd_nanos // 0) as $n | ($n / 1e7 | round) as $c | "\(($c / 100) | floor).\(($c % 100 | tostring) | if length < 2 then "0" + . else . end)"' "$summary" 2>/dev/null)
        # m$ = billable credits only (post-cutover). Falls back to the raw mtd
        # total on an old cache without the split. Hidden at $0 so the pre-cutover
        # widget stays clean (no credits owed yet); it lights up once real credit
        # spend accrues after FABLE_CREDIT_CUTOVER_UTC.
        mtd=$(jq -r 'if ((.mtd_credits.cost_usd_nanos // .mtd.cost_usd_nanos) // 0) > 0 then ((.mtd_credits.cost_usd_nanos // .mtd.cost_usd_nanos) / 1e9 | round) else empty end' "$summary" 2>/dev/null)
        seg="F"
        [ -n "$last" ] && seg="${seg} l\$${last}"
        [ -n "$today" ] && seg="${seg} d\$${today}"
        [ -n "$mtd" ] && seg="${seg} m\$${mtd}"
        if [ "$seg" != "F" ]; then
            [ -n "$parts" ] && parts="${parts}· "
            parts="${parts}${seg}"
        fi
    fi
fi

# Trim trailing space and print (nothing if we have no data).
parts="${parts% }"
[ -n "$parts" ] && printf '%s' "$parts"
exit 0
