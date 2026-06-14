#!/usr/bin/env bash
# codex_sandbox_worker.sh — run Codex CLI in an isolated git worktree, return diff
#
# Purpose:
#     Runs OpenAI Codex CLI in a git worktree (isolated checkout sharing .git
#     objects with the main repo). Codex gets full read/write power inside the
#     worktree. After Codex exits, captures `git diff HEAD` and outputs it on
#     stdout. The worktree is cleaned up automatically on exit.
#
#     Uses git worktree for ~10x faster isolation vs the old rsync-to-temp-dir
#     approach (~2-4s vs ~2min for large projects). Only tracked files are
#     checked out — node_modules, .venv, __pycache__ etc. are excluded by
#     design (they are gitignored).
#
# Contract:
#     stdin  — task description (piped to Codex via `-`)
#     stdout — unified diff of all changes Codex made (ONLY output on stdout)
#     stderr — status messages, Codex output, errors, verification reminder
#     exit   — 0 success (with or without diff), 1 Codex failure,
#              2 usage error, 127 binary not found
#
# Cleanup:
#     Worktree is removed via `git worktree remove --force` in an EXIT trap.
#     At startup, `git worktree prune` cleans stale worktrees from crashed runs.
#     This ensures worktrees never accumulate.
#
# CLI:
#     printf '%s\n' 'fix the bug in parser.py' | codex_sandbox_worker.sh gpt-5.3-codex
#     cat task.txt | codex_sandbox_worker.sh gpt-5.3-codex --json
#
# Limitations:
#     - Only tracked files appear in the worktree (gitignored dirs excluded).
#       .env/.env.local are copied explicitly if they exist.
#     - Requires the project to be a git repo (exits with error if not).
#     - Codex shell commands in the worktree can have side effects (network
#       calls, package installs) that are NOT reverted by worktree removal.
#
# ENV:
#     CODEX_BIN              — override Codex binary path (default: /opt/homebrew/bin/codex)
#     CODEX_REASONING_EFFORT — Codex effort for delegated workers (default: medium)
#     TMPDIR                 — base for worktree path (default: /tmp)

set -euo pipefail

CODEX_BIN="${CODEX_BIN:-/opt/homebrew/bin/codex}"
CODEX_REASONING_EFFORT="${CODEX_REASONING_EFFORT:-medium}"

# --- argument check ---
if [[ $# -lt 1 ]]; then
    echo "usage: codex_sandbox_worker.sh <MODEL> [extra codex args...]" >&2
    exit 2
fi

MODEL="$1"; shift

if [[ ! -x "$CODEX_BIN" ]]; then
    echo "codex_sandbox_worker.sh: codex binary not found at $CODEX_BIN" >&2
    exit 127
fi

# --- determine project root ---
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "codex_sandbox_worker.sh: not a git repo, cannot use worktree" >&2
    exit 1
}

# --- prune stale worktrees from previous crashed runs ---
git -C "$PROJECT_ROOT" worktree prune 2>/dev/null || true

# --- create worktree ---
WORKTREE_PATH="${TMPDIR:-/tmp}/codex_wt_$(date +%s)_$$"

git -C "$PROJECT_ROOT" worktree add --detach "$WORKTREE_PATH" HEAD -q 2>/dev/null || {
    echo "codex_sandbox_worker.sh: git worktree add failed" >&2
    exit 1
}

echo "codex_sandbox_worker.sh: worktree ready at $WORKTREE_PATH" >&2
chmod 700 "$WORKTREE_PATH"

# --- guaranteed cleanup ---
cleanup() {
    if [[ -d "$WORKTREE_PATH" ]]; then
        git -C "$PROJECT_ROOT" worktree remove --force "$WORKTREE_PATH" 2>/dev/null || {
            rm -rf "$WORKTREE_PATH"
            git -C "$PROJECT_ROOT" worktree prune 2>/dev/null || true
        }
    fi
}
trap cleanup EXIT

# --- copy essential gitignored config files ---
for f in .env .env.local .env.development.local; do
    [[ -f "$PROJECT_ROOT/$f" ]] && cp "$PROJECT_ROOT/$f" "$WORKTREE_PATH/$f" 2>/dev/null || true
done

# --- run Codex in worktree (stdout→stderr so it doesn't mix with diff) ---
CODEX_EXIT=0
"$CODEX_BIN" exec \
    -c "model_reasoning_effort=\"$CODEX_REASONING_EFFORT\"" \
    -C "$WORKTREE_PATH" \
    -s workspace-write \
    --ephemeral \
    -m "$MODEL" \
    "$@" - \
    >&2 || CODEX_EXIT=$?

if [[ "$CODEX_EXIT" -ne 0 ]]; then
    echo "codex_sandbox_worker.sh: codex exec failed (exit $CODEX_EXIT)" >&2
    exit 1
fi

# --- stage all changes so git diff sees new files ---
git -C "$WORKTREE_PATH" add -A || true

# --- capture diff (only stdout output from this script) ---
DIFF="$(git -C "$WORKTREE_PATH" diff --cached HEAD 2>/dev/null || true)"

if [[ -z "$DIFF" ]]; then
    echo "codex_sandbox_worker.sh: no changes detected" >&2
    exit 0
fi

echo "codex_sandbox_worker.sh: diff captured. Apply via Edit → run tests → /simplify → commit." >&2

printf '%s\n' "$DIFF"
