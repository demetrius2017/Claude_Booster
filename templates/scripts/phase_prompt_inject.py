#!/usr/bin/env python3
"""
UserPromptSubmit hook: inject current phase + rule into context.
Non-blocking — always exit 0. Stdout is added to Claude's context.

Contract:
  stdin  — UserPromptSubmit JSON (cwd, prompt, ...)
  stdout — "[phase: X] <rule>" one line
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
    "PLAN":      "design + TaskCreate + consilium if uncertainty; no code edits.",
    "IMPLEMENT": (
        f"code edits via delegated agents; run tests after."
        f" Lead: delegate coding via Agent (paired Worker+Verifier),"
        f" budget={_DELEGATE_BUDGET} direct action per delegation window."
    ),
    "AUDIT":     "review + PAL second opinion; no new code.",
    "VERIFY":    "real curl/pytest/DevTools — collect evidence.",
    "MERGE":     "git push after user acceptance; post-merge verification required.",
}

LEAD_CUE = {
    "RECON": "Lead: separate current code/runtime facts from inherited reports or memory.",
    "PLAN": "Lead: name the key assumption, an alternative, and what would falsify it.",
    "IMPLEMENT": "Lead: protect contracts, callers, downstream consumers, and integration boundaries.",
    "AUDIT": "Lead: seek a reason to reject; agreement without attempted falsification is not evidence.",
    "VERIFY": "Lead: PASS requires observable results and exit codes, not impressions.",
    "MERGE": "Lead: state residual risk and confirm downstream consumers accept the result.",
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
    print(f"[phase: {phase}] {rule} {cue} — advance: `python3 ~/.claude/scripts/phase.py set <NAME>`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
