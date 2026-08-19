#!/usr/bin/env bash
set -euo pipefail

readonly runner_path="tests/run_shell_contracts.sh"
repo_root="$(git rev-parse --show-toplevel)"
readonly repo_root
readonly expected_full_count=42
runner_tmp_root="$(mktemp -d "${TMPDIR:-/tmp}/claude-booster-shell-contracts.XXXXXX")"
readonly runner_tmp_root
readonly baseline_home="$runner_tmp_root/baseline-home"
readonly inventory_file="$runner_tmp_root/inventory"
trap 'rm -rf -- "$runner_tmp_root"' EXIT

fail() {
  printf 'shell-contracts: %s\n' "$1" >&2
  exit 2
}

[[ -f "$repo_root/install.py" && ! -L "$repo_root/install.py" ]] || fail "unsafe or missing install.py"
[[ -d "$repo_root/templates/commands" && ! -L "$repo_root/templates/commands" ]] || fail "unsafe or missing templates"

if (( $# > 1 )); then
  fail "usage: $runner_path [tests/test_NAME.sh]"
fi
if (( $# == 1 )); then
  selected="$1"
  [[ "$selected" =~ ^tests/test_[A-Za-z0-9_.-]+\.sh$ ]] || fail "invalid exact shell test path: $selected"
  git -C "$repo_root" ls-files --error-unmatch -- "$selected" >/dev/null 2>&1 || fail "shell test is not tracked: $selected"
  [[ -f "$repo_root/$selected" && ! -L "$repo_root/$selected" ]] || fail "shell test is missing or unsafe: $selected"
  printf '%s\n' "$selected" >"$inventory_file"
else
  git -C "$repo_root" ls-files -- 'tests/test_*.sh' | LC_ALL=C sort >"$inventory_file"
  actual_count="$(wc -l <"$inventory_file" | tr -d ' ')"
  [[ "$actual_count" == "$expected_full_count" ]] || fail "inventory omission: expected $expected_full_count tracked shell tests, found $actual_count"
fi

# Build one clean candidate install without consulting the invoking user's HOME.
# Child copies isolate mutations while avoiding 42 repeated installer runs.
mkdir -p "$baseline_home"
HOME="$baseline_home" python3 "$repo_root/install.py" --yes --force --no-codex-bridge >/dev/null
[[ -f "$baseline_home/.claude/scripts/rolling_memory.py" ]] || fail "candidate install omitted rolling_memory.py"
HOME="$baseline_home" python3 "$baseline_home/.claude/scripts/rolling_memory.py" stats >/dev/null
if [[ -f "$baseline_home/.claude/scripts/model_balancer.py" ]]; then
  sqlite3 "$baseline_home/.claude/rolling_memory.db" \
    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<5)
     INSERT INTO model_metrics(ts_utc,provider,model,task_category,per_turn_ms,success,session_id,project_root)
     SELECT datetime('now'),'anthropic','baseline-contract','trivial',1000,1,'shell-baseline','$repo_root' FROM n;"
  sqlite3 "$baseline_home/.claude/rolling_memory.db" \
    "INSERT INTO agent_memory(memory_type,content,priority,scope,source)
     VALUES ('directive','shell baseline directive',100,'global','shell-contracts'),
            ('feedback','shell baseline feedback',100,'global','shell-contracts');"
  HOME="$baseline_home" python3 "$baseline_home/.claude/scripts/model_balancer.py" decide --force >/dev/null
  HOME="$baseline_home" python3 "$baseline_home/.claude/scripts/model_balancer.py" status >/dev/null
fi
# --no-codex-bridge intentionally omits the Agents compatibility mirror.  The
# shell suite also contracts that shipped artifact, so assemble it solely from
# candidate repository files rather than borrowing an ambient installation.
mkdir -p "$baseline_home/.agents/skills/booster-command/references/commands"
cp "$repo_root/templates/codex/skills/booster-command/SKILL.md" "$baseline_home/.agents/skills/booster-command/SKILL.md"
cp -R "$repo_root/templates/commands/." "$baseline_home/.agents/skills/booster-command/references/commands/"
mkdir -p "$baseline_home/Projects"
ln -s "$repo_root" "$baseline_home/Projects/Claude_Booster"

test_count=0
while IFS= read -r test_file; do
  [[ -n "$test_file" ]] || continue
  [[ "$test_file" != "$runner_path" ]] || fail "runner cannot inventory itself"
  test_count=$((test_count + 1))
  printf 'shell-contracts: running %s\n' "$test_file"
  child_root="$runner_tmp_root/child-$test_count"
  child_home="$child_root/home"
  child_tmp="$child_root/tmp"
  mkdir -p "$child_root" "$child_tmp"
  cp -R "$baseline_home" "$child_home"
  set +e
  (
    export HOME="$child_home"
    export TMPDIR="$child_tmp"
    bash "$repo_root/$test_file"
  )
  status=$?
  set -e
  if (( status != 0 )); then
    printf 'shell-contracts: FAILED %s (exit %d)\n' "$test_file" "$status" >&2
    exit "$status"
  fi
done <"$inventory_file"

(( test_count > 0 )) || fail "no shell contracts selected"
printf 'shell-contracts: passed %d test files\n' "$test_count"
