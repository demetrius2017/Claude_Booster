#!/usr/bin/env bash
# grok_sandbox_worker.sh — run Grok Build in an isolated git worktree, return diff
#
# Contract:
#   stdin  — task description
#   stdout — unified diff of all changes Grok made
#   stderr — Grok output/status
#   exit   — 0 success, 1 Grok/git failure, 2 usage error, 127 binary not found

set -euo pipefail

GROK_BIN="${GROK_BIN:-$HOME/.grok/bin/grok}"
GROK_MAX_TURNS="${GROK_MAX_TURNS:-20}"

if [[ $# -lt 1 ]]; then
    echo "usage: grok_sandbox_worker.sh <MODEL> [extra grok args...]" >&2
    exit 2
fi

MODEL="$1"
shift

if [[ ! -x "$GROK_BIN" ]]; then
    echo "grok_sandbox_worker.sh: grok binary not found at $GROK_BIN" >&2
    exit 127
fi

PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "grok_sandbox_worker.sh: not a git repo, cannot use worktree" >&2
    exit 1
}

PROMPT="$(cat)"
if [[ -z "${PROMPT//[[:space:]]/}" ]]; then
    echo "grok_sandbox_worker.sh: empty stdin prompt" >&2
    exit 2
fi

git -C "$PROJECT_ROOT" worktree prune 2>/dev/null || true

WORKTREE_PATH="${TMPDIR:-/tmp}/grok_wt_$(date +%s)_$$"
git -C "$PROJECT_ROOT" worktree add --detach "$WORKTREE_PATH" HEAD -q 2>/dev/null || {
    echo "grok_sandbox_worker.sh: git worktree add failed" >&2
    exit 1
}

echo "grok_sandbox_worker.sh: worktree ready at $WORKTREE_PATH" >&2
chmod 700 "$WORKTREE_PATH"

cleanup() {
    if [[ -d "$WORKTREE_PATH" ]]; then
        git -C "$PROJECT_ROOT" worktree remove --force "$WORKTREE_PATH" 2>/dev/null || {
            rm -rf "$WORKTREE_PATH"
            git -C "$PROJECT_ROOT" worktree prune 2>/dev/null || true
        }
    fi
}
trap cleanup EXIT

for f in .env .env.local .env.development.local; do
    [[ -f "$PROJECT_ROOT/$f" ]] && cp "$PROJECT_ROOT/$f" "$WORKTREE_PATH/$f" 2>/dev/null || true
done

GROK_EXIT=0
"$GROK_BIN" \
    -p "$PROMPT" \
    --cwd "$WORKTREE_PATH" \
    --model "$MODEL" \
    --max-turns "$GROK_MAX_TURNS" \
    --output-format plain \
    --permission-mode acceptEdits \
    --always-approve \
    --no-subagents \
    "$@" \
    >&2 || GROK_EXIT=$?

if [[ "$GROK_EXIT" -ne 0 ]]; then
    echo "grok_sandbox_worker.sh: grok failed (exit $GROK_EXIT)" >&2
    exit 1
fi

git -C "$WORKTREE_PATH" add -A || true
DIFF="$(git -C "$WORKTREE_PATH" diff --cached HEAD 2>/dev/null || true)"

if [[ -z "$DIFF" ]]; then
    echo "grok_sandbox_worker.sh: no changes detected" >&2
    exit 0
fi

echo "grok_sandbox_worker.sh: diff captured. Apply via Edit/Write, then verify." >&2
printf '%s\n' "$DIFF"
