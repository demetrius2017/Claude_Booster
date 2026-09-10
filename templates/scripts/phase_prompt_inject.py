#!/usr/bin/env python3
"""
UserPromptSubmit hook: inject current phase + rule into context.
Non-blocking — always exit 0. Stdout is added to Claude's context.

Contract:
  stdin  — UserPromptSubmit JSON (cwd, prompt, ...)
  stdout — UserPromptSubmit hook JSON with ``additionalContext``
  exit   — 0 always
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_DELEGATE_BUDGET = os.environ.get("CLAUDE_BOOSTER_DELEGATE_BUDGET", "1")

HINT = {
    "RECON":     "read-only; no Edit/Write. Use Read/Grep/Glob/WebSearch.",
    "PLAN":      "define outcome + acceptance; consilium for material high risk; no code edits.",
    "IMPLEMENT": (
        f"code edits via delegated agents; run tests after."
        f" Lead: delegate coding via Agent (pair under core proportionality rule),"
        f" budget={_DELEGATE_BUDGET} direct action per delegation window."
    ),
    "AUDIT":     "review under core proportionality rule; PAL when required; no new code.",
    "VERIFY":    "real curl/pytest/DevTools — collect evidence.",
    "MERGE":     "git push after user acceptance; post-merge verification required.",
}

LEAD_CUE = {
    "RECON": "Lead: identify the business result; separate current code/runtime facts from reports or memory; reuse relevant evidence.",
    "PLAN": "Lead: define acceptance and domain invariants; name the key assumption, material alternative and falsifier; choose a coherent reversible slice.",
    "IMPLEMENT": "Lead: deliver the business slice; protect contracts, downstream integration and safety controls; avoid microdelegation.",
    "AUDIT": "Lead: test material falsifiers against business acceptance and domain invariants; one sufficient review, no repeated broad audit without a concrete trigger.",
    "VERIFY": "Lead: PASS requires observable business results, evidence and exit codes; stop when acceptance passes and material risks are covered.",
    "MERGE": "Lead: deliver the accepted result, state residual risk and confirm downstream consumers; reopen checks only for changes, failures or concrete unresolved risk.",
}


def _project_root(cwd_hint: str) -> Path:
    try:
        cwd = Path(cwd_hint) if cwd_hint else Path.cwd()
    except (FileNotFoundError, OSError):
        return Path.home()
    for p in [cwd, *cwd.parents]:
        if (p / ".git").exists() or (p / ".claude").exists():
            return p
    return cwd


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError, OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    cwd = payload.get("cwd", "")
    root = _project_root(cwd)
    f = root / ".claude" / ".phase"
    phase = "RECON"
    if f.exists():
        try:
            v = f.read_text(encoding="utf-8").strip().upper()
            if v:
                phase = v
        except (UnicodeError, OSError):
            phase = "RECON"

    rule = HINT.get(phase, "unknown phase")
    cue = LEAD_CUE.get(phase, LEAD_CUE["RECON"])
    context = (
        f"[phase: {phase}] {rule} {cue} — advance: "
        "`python3 ~/.claude/scripts/phase.py set <NAME>`"
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
