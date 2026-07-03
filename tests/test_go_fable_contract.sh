#!/usr/bin/env bash
# Acceptance test: /go fable Quality Chair contract.
# Verifies observable spec text only; no live Fable/model calls.

set -euo pipefail

ROOT="/Users/dmitrijnazarov/Projects/Claude_Booster"
GO="$ROOT/templates/commands/go.md"
SKILL="$ROOT/templates/codex/skills/go/SKILL.md"
PROMPT="$ROOT/templates/codex/prompts/go.md"
RUNNER="$ROOT/templates/codex/skills/booster-command/SKILL.md"
AGENTS="$ROOT/AGENTS.md"

for f in "$GO" "$SKILL" "$PROMPT" "$RUNNER" "$AGENTS"; do
  [[ -s "$f" ]] || {
    echo "FAIL missing file: $f"
    exit 1
  }
done

must_contain() {
  local file="$1" pattern="$2" label="$3"
  if ! grep -qF "$pattern" "$file"; then
    echo "FAIL missing: $label"
    echo "  pattern: $pattern"
    echo "  file: $file"
    exit 1
  fi
}

must_contain "$GO" 'argument-hint: "[fable]' "go argument hint includes fable"
must_contain "$GO" 'Opt-in mode — `/go fable`' "opt-in section"
must_contain "$GO" 'Fable as Quality Chair' "Fable Quality Chair role"
must_contain "$GO" 'Fable is not Lead, not Worker, not Verifier' "bounded Fable role"
must_contain "$GO" 'MUST NOT mutate `~/.claude/model_balancer.json`' "no routing mutation"
must_contain "$GO" 'fable_control:' "fable control artifact"
must_contain "$GO" 'fable_watchlist' "fable watchlist artifact"
must_contain "$GO" 'origin: fable-challenge' "origin tag"
must_contain "$GO" 'rework_log:' "rework log artifact"
must_contain "$GO" 'at most 2 Fable calls per `/go fable` run' "max two Fable calls"
must_contain "$GO" 'usage snapshot is `>=80%`' "budget downgrade gate"
must_contain "$GO" 'Worker retries, verifier retries, debugging' "no Fable polling/debug loops"
must_contain "$GO" 'FABLE_CHALLENGE_VERDICT:' "Fable challenge verdict"
must_contain "$GO" 'SOUND | ADDITIVE_GAPS | DESIGN_REWORK | CONTRACT_AMBIGUOUS' "challenge rework statuses"
must_contain "$GO" 'return to Phase 1, not Phase 1C' "challenge design rework route"
must_contain "$GO" 'FABLE_DIFF_REVIEW_VERDICT:' "Fable diff-review verdict"
must_contain "$GO" 'PASS | REWORK_REQUIRED | CONTRACT_AMBIGUOUS' "diff-review statuses"
must_contain "$GO" 'target_phase_if_open: worker | verifier | prototype | flow_designer | user' "typed phase routing"
must_contain "$GO" 'return to Phase 1C Prototype Gate' "prototype rework route"
must_contain "$GO" 'PASS stays "exit code of the test"' "exit-code axiom"
must_contain "$GO" 'fable control:' "verdict fable control line"
must_contain "$GO" '`/go fable` MUST keep Fable as Quality Chair, not Lead' "non-negotiable role constraint"
must_contain "$GO" 'Context between Fable calls MUST be carried by `fable_control`' "artifact-mediated context"
must_contain "$SKILL" 'go fable' "Codex skill advertises go fable"
must_contain "$SKILL" 'Quality Chair' "Codex skill preserves Fable role"
must_contain "$PROMPT" "argument-hint: '[fable]" "Codex prompt argument hint"
must_contain "$RUNNER" 'go [fable] <artifact contract>' "booster-command runner advertises go fable"
must_contain "$AGENTS" 'go [fable] <artifact contract>' "AGENTS command list advertises go fable"

echo "PASS go fable Quality Chair contract"
