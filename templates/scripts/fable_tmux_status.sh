#!/usr/bin/env bash
# fable_tmux_status.sh — compact tmux status widget.
# Shows remaining Max rate-limit windows (5h/7d). They come from
# ~/.claude/.rate_limits_cache.json, which the Claude Code statusline
# (statusline.sh) refreshes from its stdin while a session is active.
# Fable spend is no longer shown here (removed 2026-11-01); use /fable.
# Best-effort — exits 0 on every failure and omits a stale/missing segment.

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

# Trim trailing space and print (nothing if we have no data).
parts="${parts% }"
[ -n "$parts" ] && printf '%s' "$parts"
exit 0
