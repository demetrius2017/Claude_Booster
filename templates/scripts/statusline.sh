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

        # Optional rate-limit suffix
        rl_str=""
        if [ -n "$raw_rl" ]; then
            rl_pct=${raw_rl%.*}
            [[ "$rl_pct" =~ ^-?[0-9]+$ ]] || rl_pct=0
            rl_str=" | 5h: $(_color "$rl_pct")${rl_pct}%${RST}"
        fi

        if [ -n "$raw_model" ]; then
            model_info=" ${raw_model} ${ctx_str}${rl_str}"
        else
            model_info=" ${ctx_str}${rl_str}"
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
if [ -f "$fable_cache" ] && command -v jq >/dev/null 2>&1; then
    fable_enabled=$(jq -r 'select(.display_enabled == true) | .display_enabled // empty' "$fable_cache" 2>/dev/null)
    if [ "$fable_enabled" = "true" ]; then
        current_session=""
        if [ -n "$input" ]; then
            current_session=$(printf '%s' "$input" | jq -r '.session_id // .sessionId // empty' 2>/dev/null)
        fi
        fable_session=$(jq -r '.last_task.session_id // empty' "$fable_cache" 2>/dev/null)
        fable_last=$(jq -r '.last_task.cost_usd // empty' "$fable_cache" 2>/dev/null)
        if [ -n "$current_session" ] && [ "$current_session" = "$fable_session" ] && [ -n "$fable_last" ] && [ "$fable_last" != "0.0000" ]; then
            fable_info=" | Fable session est \$${fable_last}"
        fi
    fi
fi

if [ -n "$progress" ]; then
    printf '[%s] %s%s%s\n' "$phase" "$progress" "$model_info" "$fable_info"
else
    printf '[%s]%s%s\n' "$phase" "$model_info" "$fable_info"
fi
exit 0
