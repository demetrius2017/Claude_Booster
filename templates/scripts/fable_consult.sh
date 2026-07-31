#!/usr/bin/env bash
# Run one non-interactive, tool-disabled Fable consult with the prompt on stdin.
# Local exits: 64 = invalid input, 69 = missing dependency, 70 = wrapper/output
# contract failure, 74 = stdin read failure. Claude CLI stderr and nonzero exit
# statuses pass through; successful JSON output is validated and its final
# result text is written to stdout.

set -u

claude_bin="${CLAUDE_BIN:-claude}"
identity_file="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fable_identity_preamble.txt"
prompt_file="$(mktemp "${TMPDIR:-/tmp}/fable-consult.XXXXXX")" || exit 70
combined_prompt_file="$(mktemp "${TMPDIR:-/tmp}/fable-prompt.XXXXXX")" || {
    rm -f "$prompt_file"
    exit 70
}
result_file="$(mktemp "${TMPDIR:-/tmp}/fable-result.XXXXXX")" || {
    rm -f "$prompt_file" "$combined_prompt_file"
    exit 70
}
trap 'rm -f "$prompt_file" "$combined_prompt_file" "$result_file"' EXIT HUP INT TERM

if ! cat >"$prompt_file"; then
    printf '%s\n' 'fable_consult: failed to read prompt from stdin' >&2
    exit 74
fi

if ! LC_ALL=C grep -q '[^[:space:]]' "$prompt_file"; then
    printf '%s\n' 'fable_consult: nonblank prompt required on stdin (local contract failure)' >&2
    exit 64
fi

if [[ ! -f "$identity_file" ]]; then
    printf 'fable_consult: identity preamble missing: %s (local environment failure)\n' "$identity_file" >&2
    exit 69
fi
if ! {
    cat "$identity_file"
    printf '\n\n'
    cat "$prompt_file"
} >"$combined_prompt_file"; then
    printf '%s\n' 'fable_consult: failed to assemble identity preamble and prompt' >&2
    exit 70
fi

if ! command -v "$claude_bin" >/dev/null 2>&1; then
    printf 'fable_consult: Claude CLI not found: %s (local environment failure)\n' "$claude_bin" >&2
    exit 69
fi

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'fable_consult: python3 not found (local environment failure)' >&2
    exit 69
fi

"$claude_bin" --model fable --print --tools "" --output-format json \
    <"$combined_prompt_file" >"$result_file"
status=$?
if [[ $status -ne 0 ]]; then
    exit "$status"
fi

python3 - "$result_file" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    print(f"fable_consult: invalid Claude CLI JSON output: {exc}", file=sys.stderr)
    raise SystemExit(70)

if not isinstance(payload, dict):
    print("fable_consult: Claude CLI JSON output must be a top-level object", file=sys.stderr)
    raise SystemExit(70)

result = payload.get("result")
if not isinstance(result, str) or not result.strip():
    print("fable_consult: Claude CLI returned no final response text", file=sys.stderr)
    raise SystemExit(70)

sys.stdout.write(result)
if not result.endswith("\n"):
    sys.stdout.write("\n")
PY
status=$?
exit "$status"
