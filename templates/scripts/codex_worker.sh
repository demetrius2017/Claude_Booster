#!/usr/bin/env bash
set -euo pipefail

CODEX_BIN="${CODEX_BIN:-/opt/homebrew/bin/codex}"
CODEX_REASONING_EFFORT="${CODEX_REASONING_EFFORT:-medium}"

if [[ $# -lt 1 ]]; then
    echo "usage: codex_worker.sh <MODEL> [extra args...]" >&2
    exit 2
fi

MODEL="$1"
shift

if [[ ! -x "$CODEX_BIN" ]]; then
    echo "codex_worker.sh: codex binary not found at $CODEX_BIN" >&2
    exit 127
fi

exec "$CODEX_BIN" exec \
    -c "model_reasoning_effort=\"$CODEX_REASONING_EFFORT\"" \
    --skip-git-repo-check \
    -m "$MODEL" \
    "$@" -
