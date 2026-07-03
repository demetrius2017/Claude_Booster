#!/usr/bin/env bash
# fable_tmux_status.sh — tiny tmux status widget for Fable 5 usage estimates.
# Reads the cache produced by fable_usage.py and exits 0 on every failure.

cache="${HOME}/.claude/fable_usage_summary.json"

if [ ! -f "$cache" ] || ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

enabled=$(jq -r 'select(.display_enabled == true) | .display_enabled // empty' "$cache" 2>/dev/null)
if [ "$enabled" != "true" ]; then
    exit 0
fi

last=$(jq -r '.last_task.cost_usd // empty' "$cache" 2>/dev/null)

if [ -n "$last" ] && [ "$last" != "0.0000" ]; then
    printf 'Fable last-task est $%s' "$last"
fi

exit 0
