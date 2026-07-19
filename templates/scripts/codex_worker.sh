#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -lt 1 ]]; then
    echo "usage: codex_worker.sh <MODEL> [extra args...]" >&2
    exit 2
fi

MODEL="$1"
shift

exec python3 "$SCRIPT_DIR/codex_worker.py" "$MODEL" "$@"
