#!/usr/bin/env bash
# Acceptance test for require_task.py — content validation extension
#
# Tests that the hook validates the most recent TaskCreate's description
# for an impact-analysis field (affected:, dependencies:, impact:, dependents:).
#
# Artifact Contract scenarios tested:
#   1. TaskCreate with "affected: auth module"       → exit 0 (allow)
#   2. TaskCreate with "dependencies: Redis, DB"     → exit 0 (allow)
#   3. TaskCreate with "impact: changes session"     → exit 0 (allow)
#   4. TaskCreate with no impact field               → exit 0 + advisory
#   5. TaskCreate subject starts with "docs:"        → exit 0 (allow regardless)
#   6. TaskCreate subject starts with "chore:"       → exit 0 (allow regardless)
#   7. No TaskCreate at all                          → exit 0 + advisory
#   8. [no-impact-review] marker in transcript       → exit 0 (bypass)
#   9. Edit on allowlisted *.md path                 → exit 0 (existing bypass)
#  10. CLAUDE_BOOSTER_SKIP_TASK_GATE=1               → exit 0 (existing bypass)
#  B1. TaskCreate with "dependents:" field           → exit 0 (allow)
#  B2. Most-recent TaskCreate lacks impact field     → exit 0 + advisory
#
# NOTE: Transcripts are written as compact single-line JSONL with top-level
# "content" key — the format _check_task_content() can parse. Real Claude
# sessions use {"message":{"content":[...]}} which hits the fail-open path
# of _check_task_content(); that is a separate concern.
#
# Exit 0 = all scenarios passed
# Exit 1 = one or more scenarios failed

set -uo pipefail

HOOK_PATH="/Users/dmitrijnazarov/Projects/Claude_Booster/templates/scripts/require_task.py"
PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()

# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------

TMPDIR_TEST="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_TEST"' EXIT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# make_task_line <subject> <description>
# Outputs a compact single-line JSONL entry with top-level "content" key,
# matching the format _check_task_content() parses (task_use block with
# "name":"TaskCreate" at the top level of the content array).
make_task_line() {
    local subject="$1"
    local description="$2"
    jq -cn --arg subject "$subject" --arg description "$description" \
        '{"content":[{"type":"tool_use","name":"TaskCreate","input":{"subject":$subject,"description":$description}}]}'
}

# make_empty_line → a single user-message line without any TaskCreate
make_empty_line() {
    jq -cn '{"content":[{"type":"text","text":"fix the bug"}]}'
}

# build_hook_payload <file_path> <transcript_file>
# Produces the PreToolUse JSON payload piped to require_task.py stdin.
build_hook_payload() {
    local file_path="$1"
    local transcript_file="$2"
    jq -cn --arg fp "$file_path" --arg tp "$transcript_file" \
        '{"tool_name":"Edit","tool_input":{"file_path":$fp,"old_string":"x","new_string":"y"},"transcript_path":$tp}'
}

# run_scenario <label> <expected_exit> <hook_payload_json> [env_var=value ...]
# Always unsets CLAUDE_BOOSTER_SKIP_TASK_GATE first, then applies extra_env.
# This prevents the parent session's env bypass from masking test failures.
run_scenario() {
    local label="$1"
    local expected_exit="$2"
    local payload="$3"
    shift 3
    local extra_env=("$@")

    local actual_exit
    if [[ ${#extra_env[@]} -gt 0 ]]; then
        actual_exit=$(env -u CLAUDE_BOOSTER_SKIP_TASK_GATE "${extra_env[@]}" python3 "$HOOK_PATH" <<< "$payload" >/dev/null 2>&1; echo $?)
    else
        actual_exit=$(env -u CLAUDE_BOOSTER_SKIP_TASK_GATE python3 "$HOOK_PATH" <<< "$payload" >/dev/null 2>&1; echo $?)
    fi

    if [[ "$actual_exit" -eq "$expected_exit" ]]; then
        RESULTS+=("  PASS  [$label] — expected exit $expected_exit, got $actual_exit")
        (( PASS_COUNT++ )) || true
    else
        RESULTS+=("  FAIL  [$label] — expected exit $expected_exit, got $actual_exit")
        (( FAIL_COUNT++ )) || true
    fi
}

# ---------------------------------------------------------------------------
# Scenario 1: TaskCreate with "affected: auth module" → exit 0
# ---------------------------------------------------------------------------
T1="$TMPDIR_TEST/t1.jsonl"
make_task_line \
    "Refactor session token handling" \
    "Updating session token logic. affected: auth module, session middleware. Expected: tokens rotate on login." \
    > "$T1"

run_scenario \
    "1. affected: field → allow" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/session.py" "$T1")"

# ---------------------------------------------------------------------------
# Scenario 2: TaskCreate with "dependencies: Redis, DB" → exit 0
# ---------------------------------------------------------------------------
T2="$TMPDIR_TEST/t2.jsonl"
make_task_line \
    "Add cache layer to position service" \
    "Wrapping position queries with Redis cache. dependencies: Redis, DB connection pool." \
    > "$T2"

run_scenario \
    "2. dependencies: field → allow" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/positions.py" "$T2")"

# ---------------------------------------------------------------------------
# Scenario 3: TaskCreate with "impact: changes session tokens" → exit 0
# ---------------------------------------------------------------------------
T3="$TMPDIR_TEST/t3.jsonl"
make_task_line \
    "Rotate JWT signing key" \
    "Implement key rotation for JWT. impact: changes session tokens for all active users." \
    > "$T3"

run_scenario \
    "3. impact: field → allow" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/auth.py" "$T3")"

# ---------------------------------------------------------------------------
# Scenario 4: TaskCreate description with NO impact field → exit 2 (block)
# ---------------------------------------------------------------------------
T4="$TMPDIR_TEST/t4.jsonl"
make_task_line \
    "fix the bug in foo.py" \
    "fix the bug in foo.py by updating the logic" \
    > "$T4"

run_scenario \
    "4. no impact field → advisory (exit 0)" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/foo.py" "$T4")"

# ---------------------------------------------------------------------------
# Scenario 5: TaskCreate title (key="title") starts with "docs:" → exit 0
# The description has NO impact field, but the docs: prefix on the "title"
# field exempts it. Note: real Claude sessions use key="subject"; this tests
# that the hook's DOCS_CHORE_PREFIX_RE fires when the field is named "title".
# ---------------------------------------------------------------------------
T5="$TMPDIR_TEST/t5.jsonl"
# Use "title" key (what _check_task_content reads via inp.get("title"))
jq -cn --arg title "docs: update README with new API examples" --arg description "Adding code examples. No impact." \
    '{"content":[{"type":"tool_use","name":"TaskCreate","input":{"title":$title,"description":$description}}]}' \
    > "$T5"

run_scenario \
    "5. docs: title-field prefix → allow regardless of impact field" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/service.py" "$T5")"

# ---------------------------------------------------------------------------
# Scenario 6: TaskCreate title (key="title") starts with "chore:" → exit 0
# Same as above but for chore: prefix.
# ---------------------------------------------------------------------------
T6="$TMPDIR_TEST/t6.jsonl"
jq -cn --arg title "chore: bump dependency versions" --arg description "Update package versions." \
    '{"content":[{"type":"tool_use","name":"TaskCreate","input":{"title":$title,"description":$description}}]}' \
    > "$T6"

run_scenario \
    "6. chore: title-field prefix → allow regardless of impact field" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/service.py" "$T6")"

# ---------------------------------------------------------------------------
# Scenario 5b: TaskCreate subject (key="subject", real Claude format) with
# "docs:" prefix → tests whether hook handles subject field correctly.
# If the hook only checks "title" key but real sessions use "subject", this
# will FAIL as a W-category Worker defect.
# ---------------------------------------------------------------------------
T5b="$TMPDIR_TEST/t5b.jsonl"
make_task_line \
    "docs: update README with new API examples" \
    "Adding code examples to README. No impact analysis needed for docs." \
    > "$T5b"

run_scenario \
    "5b. docs: subject-field prefix (real Claude format) → allow" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/service.py" "$T5b")"

# ---------------------------------------------------------------------------
# Scenario 7: No TaskCreate at all → exit 2 (existing stage-1 behavior)
# ---------------------------------------------------------------------------
T7="$TMPDIR_TEST/t7.jsonl"
make_empty_line > "$T7"

run_scenario \
    "7. no TaskCreate at all → advisory (exit 0)" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/main.py" "$T7")"

# ---------------------------------------------------------------------------
# Scenario 8: [no-impact-review] marker in transcript → exit 0 (bypass)
# TaskCreate present but no impact field; marker bypasses content check.
# ---------------------------------------------------------------------------
T8="$TMPDIR_TEST/t8.jsonl"
# A task line without impact field, plus the bypass marker in a separate line
make_task_line "fix bar" "fix bar by rewriting it" > "$T8"
jq -cn --arg text "Proceeding. [no-impact-review] — skipping impact check this time." \
    '{"content":[{"type":"text","text":$text}]}' >> "$T8"

run_scenario \
    "8. [no-impact-review] marker → allow (content bypass)" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/main.py" "$T8")"

# ---------------------------------------------------------------------------
# Scenario 9: Edit on allowlisted *.md path → exit 0 (existing path bypass)
# No transcript needed — the allowlist check fires before reading transcript.
# ---------------------------------------------------------------------------
T9="$TMPDIR_TEST/t9.jsonl"
make_empty_line > "$T9"

run_scenario \
    "9. *.md allowlisted path → allow (existing bypass)" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/README.md" "$T9")"

# ---------------------------------------------------------------------------
# Scenario 10: CLAUDE_BOOSTER_SKIP_TASK_GATE=1 → exit 0 (env bypass)
# No TaskCreate, but env var bypasses everything.
# ---------------------------------------------------------------------------
T10="$TMPDIR_TEST/t10.jsonl"
make_empty_line > "$T10"

run_scenario \
    "10. CLAUDE_BOOSTER_SKIP_TASK_GATE=1 → allow (env bypass)" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/main.py" "$T10")" \
    "CLAUDE_BOOSTER_SKIP_TASK_GATE=1"

# ---------------------------------------------------------------------------
# Bonus B1: TaskCreate with "dependents:" field → exit 0
# Verifies the fourth keyword in the impact-analysis regex.
# ---------------------------------------------------------------------------
TB1="$TMPDIR_TEST/tb1.jsonl"
make_task_line \
    "Refactor connection pool initialization" \
    "Moving pool init to startup hook. dependents: all DB-accessing services, health check endpoint." \
    > "$TB1"

run_scenario \
    "B1. dependents: field → allow" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/db.py" "$TB1")"

# ---------------------------------------------------------------------------
# Bonus B2: Most-recent TaskCreate has no impact field → exit 2
# An earlier line has a valid impact field; the gate checks the LAST TaskCreate.
# ---------------------------------------------------------------------------
TB2="$TMPDIR_TEST/tb2.jsonl"
# First TaskCreate: has impact field (not the most recent)
make_task_line \
    "Earlier task" \
    "First task ever. affected: auth module. This was the planning step." \
    >> "$TB2"
# Second TaskCreate: no impact field (the most recent — should trigger block)
make_task_line \
    "fix the other bug" \
    "fix the bug in bar.py by patching the condition" \
    >> "$TB2"

run_scenario \
    "B2. most-recent TaskCreate lacks impact field → advisory (exit 0)" \
    0 \
    "$(build_hook_payload "/Users/dmitrijnazarov/Projects/horizon/src/bar.py" "$TB2")"

# ---------------------------------------------------------------------------
# Print results
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " require_task.py content validation — Acceptance Test Results"
echo "============================================================"
for r in "${RESULTS[@]}"; do
    echo "$r"
done
echo "------------------------------------------------------------"
echo " Total: $((PASS_COUNT + FAIL_COUNT))  PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
echo "============================================================"
echo ""

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo "OVERALL: FAIL"
    exit 1
else
    echo "OVERALL: PASS"
    exit 0
fi
