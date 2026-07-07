#!/usr/bin/env bash
# statusline.sh — prints the current workflow phase for Claude Code's status bar.
# Walks up from CWD looking for .claude/.phase; defaults to RECON if not found.
# Claude Code pipes JSON session data via stdin; we consume it to enrich output.
# MUST exit 0 always — Claude Code may suppress output or break on non-zero.

# Consume stdin from Claude Code (unblocks the pipe; JSON session data)
input=$(cat)

# Pick ANSI color based on percentage (integer): green <70, yellow 70-89, red 90+
# Emits ONLY the ANSI opener (\033[3Xm). Caller MUST append \033[0m reset in the template string.
_color() {
    local pct=$1
    if [ "$pct" -ge 90 ]; then
        printf '\033[31m'  # red
    elif [ "$pct" -ge 70 ]; then
        printf '\033[33m'  # yellow
    else
        printf '\033[32m'  # green
    fi
}
RST=$'\033[0m'

# Extract model name, context percentage, and rate-limit percentage from JSON
model_info=""
if command -v jq >/dev/null 2>&1 && [ -n "$input" ]; then
    raw_model=$(printf '%s' "$input" | jq -r '.model.display_name // empty' 2>/dev/null)
    raw_ctx=$(printf '%s' "$input" | jq -r '.context_window.used_percentage // empty' 2>/dev/null)
    raw_rl=$(printf '%s' "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty' 2>/dev/null)
    raw_7d=$(printf '%s' "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty' 2>/dev/null)

    if [ -n "$raw_ctx" ]; then
        # Build progress bar: ▰ = filled, ▱ = empty, 20 blocks total
        pct=${raw_ctx%.*}  # truncate to integer
        [[ "$pct" =~ ^-?[0-9]+$ ]] || pct=0
        filled=$(( pct * 20 / 100 ))
        [ "$filled" -gt 20 ] && filled=20
        [ "$filled" -lt 0  ] && filled=0
        bar=""
        for ((i=0; i<filled; i++)); do bar+="▰"; done
        for ((i=filled; i<20; i++)); do bar+="▱"; done

        # Colored bar + percentage
        ctx_str="$(_color "$pct")${bar} ${pct}%${RST}"

        # Optional rate-limit suffix. Each window shows the REMAINING budget
        # (100 - used%), but _color is fed the USED% so near-exhaustion renders
        # red. Each window is independently // empty guarded.
        rl_str=""
        if [ -n "$raw_rl" ]; then
            rl_pct=${raw_rl%.*}
            [[ "$rl_pct" =~ ^-?[0-9]+$ ]] || rl_pct=0
            rl_str=" | 5h: $(_color "$rl_pct")$((100 - rl_pct))%${RST}"
        fi
        if [ -n "$raw_7d" ]; then
            d7_pct=${raw_7d%.*}
            [[ "$d7_pct" =~ ^-?[0-9]+$ ]] || d7_pct=0
            rl_str="${rl_str} | 7d: $(_color "$d7_pct")$((100 - d7_pct))%${RST}"
        fi

        # Persist remaining rate-limit budget for the tmux widget, which has no
        # access to Claude Code's stdin. Tiny atomic write; fail-open.
        if [ -n "$raw_rl" ] || [ -n "$raw_7d" ]; then
            rl_cache="${HOME}/.claude/.rate_limits_cache.json"
            rl_json="{"
            [ -n "$raw_rl" ] && rl_json="${rl_json}\"five_hour_remaining\":$((100 - rl_pct)),"
            [ -n "$raw_7d" ] && rl_json="${rl_json}\"seven_day_remaining\":$((100 - d7_pct)),"
            rl_json="${rl_json}\"updated_at\":$(date +%s 2>/dev/null || echo 0)}"
            printf '%s' "$rl_json" > "${rl_cache}.$$.tmp" 2>/dev/null && mv -f "${rl_cache}.$$.tmp" "$rl_cache" 2>/dev/null
        fi

        if [ -n "$raw_model" ]; then
            model_info=" ${raw_model} ${ctx_str}"
        else
            model_info=" ${ctx_str}"
        fi
    elif [ -n "$raw_model" ]; then
        model_info=" ${raw_model}"
    fi
fi

phase="RECON"
dir="${PWD:-$HOME}"

while true; do
    phase_file="$dir/.claude/.phase"
    if [ -f "$phase_file" ]; then
        read -r content < "$phase_file" 2>/dev/null
        content="${content//[[:space:]]/}"
        if [ -n "$content" ]; then
            phase="$content"
        fi
        break
    fi
    parent="${dir%/*}"
    # ${dir%/*} yields "" when dir is "/foo" — normalize to root
    [ -z "$parent" ] && parent="/"
    if [ "$parent" = "$dir" ]; then
        break
    fi
    dir="$parent"
done

progress=""
progress_file="$dir/.claude/.progress"
if [ -f "$progress_file" ]; then
    read -r progress < "$progress_file" 2>/dev/null
fi

fable_info=""
fable_cache="${HOME}/.claude/fable_usage_summary.json"
fable_script="${HOME}/.claude/scripts/fable_usage.py"

# Current session id (used both for the render and to target the refresh).
current_session=""
if [ -n "$input" ] && command -v jq >/dev/null 2>&1; then
    current_session=$(printf '%s' "$input" | jq -r '.session_id // .sessionId // empty' 2>/dev/null)
fi
# Harden: a session id must be a safe token before it reaches a glob or argv.
# Anything carrying shell/glob metacharacters is dropped (session ids are UUIDs).
case "$current_session" in *[!A-Za-z0-9_-]*) current_session="" ;; esac

# Render last-task cost + month-to-date from the cache. session/today both
# legitimately read $0 (fresh session in another project, or just past the
# Dubai-midnight rollover) and look broken; last_task + mtd are always populated
# once any Fable spend exists. Every read is // empty guarded.
if [ -f "$fable_cache" ] && command -v jq >/dev/null 2>&1; then
    fable_enabled=$(jq -r 'select(.display_enabled == true) | .display_enabled // empty' "$fable_cache" 2>/dev/null)
    if [ "$fable_enabled" = "true" ]; then
        # Format inside jq (always '.' decimal, locale-independent + always 2dp).
        # printf '%.2f' honours LC_NUMERIC ("0,15" / thousands-grouped garbage on
        # comma-locale hosts); plain jq division drops the decimal on whole values
        # ("$1" not "$1.00"). Build cents as an integer, then pad to D.CC.
        fable_last=$(jq -r 'if (.last_task.cost_usd_nanos // 0) > 0 then (.last_task.cost_usd_nanos / 1e7 | round) as $c | "\(($c / 100) | floor).\(($c % 100 | tostring) | if length < 2 then "0" + . else . end)" else empty end' "$fable_cache" 2>/dev/null)
        # today (local Dubai day) is shown even at $0 — that IS the running-total signal.
        fable_today=$(jq -r '(.today.cost_usd_nanos // 0) as $n | ($n / 1e7 | round) as $c | "\(($c / 100) | floor).\(($c % 100 | tostring) | if length < 2 then "0" + . else . end)"' "$fable_cache" 2>/dev/null)
        # m$ = billable credits only (post-cutover), not the included free window.
        # Falls back to raw mtd on an old cache without the split.
        fable_mtd=$(jq -r 'if ((.mtd_credits.cost_usd_nanos // .mtd.cost_usd_nanos) // 0) > 0 then ((.mtd_credits.cost_usd_nanos // .mtd.cost_usd_nanos) / 1e9 | round) else empty end' "$fable_cache" 2>/dev/null)
        if [ -n "$fable_last" ] || [ -n "$fable_today" ] || [ -n "$fable_mtd" ]; then
            [ -n "$fable_last" ] || fable_last="0.00"
            [ -n "$fable_today" ] || fable_today="0.00"
            [ -n "$fable_mtd" ] || fable_mtd="0"
            fable_info=" | Fable: last \$${fable_last} · today \$${fable_today} · m\$${fable_mtd}"
        fi
    fi
fi

# Throttled, backgrounded, self-healing refresh. The concurrency lock lives
# INSIDE fable_usage.py (Python fcntl.flock — macOS has no flock binary), so the
# shell only does the mtime throttle + a detached spawn. Never blocks, never
# errors: all output redirected, spawn backgrounded with no wait.
if [ -n "$current_session" ] && command -v jq >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
    now_epoch=$(date +%s 2>/dev/null || echo 0)
    cache_mtime=$(stat -f %m "$fable_cache" 2>/dev/null || stat -c %Y "$fable_cache" 2>/dev/null || echo 0)
    age=$(( now_epoch - cache_mtime ))
    # Clock skew / future-dated mtime must not permanently disable the throttle.
    [ "$age" -lt 0 ] && age=999999
    if [ ! -f "$fable_cache" ] || [ "$age" -gt 30 ]; then
        # Resolve the transcript deterministically so refresh is idempotent.
        tpath=""
        stdin_transcript=$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null)
        if [ -n "$stdin_transcript" ] && [ -f "$stdin_transcript" ]; then
            tpath="$stdin_transcript"
        else
            proj_hash=$(printf '%s' "${PWD:-}" | sed 's/[/_.]/-/g')
            cand="${HOME}/.claude/projects/${proj_hash}/${current_session}.jsonl"
            if [ -f "$cand" ]; then
                tpath="$cand"
            else
                for f in "${HOME}/.claude/projects"/*/"${current_session}.jsonl"; do
                    [ -f "$f" ] || continue
                    if [ -z "$tpath" ] || [ "$f" -nt "$tpath" ]; then
                        tpath="$f"
                    fi
                done
            fi
        fi
        if [ -n "$tpath" ]; then
            ( python3 "$fable_script" refresh-session --session "$current_session" --transcript "$tpath" ) >/dev/null 2>&1 &
        fi
    fi
fi

# Row 1: phase + progress + model/context. Row 2: rate limits + Fable spend on
# their own line so a narrow terminal does not clip them — Claude Code renders
# each printed line as a separate status row. Row 2 is omitted when empty.
if [ -n "$progress" ]; then
    printf '[%s] %s%s\n' "$phase" "$progress" "$model_info"
else
    printf '[%s]%s\n' "$phase" "$model_info"
fi
line2="${rl_str}${fable_info}"
line2="${line2# | }"
[ -n "$line2" ] && printf '%s\n' "$line2"
exit 0
