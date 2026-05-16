#!/usr/bin/env python3
"""
PreToolUse hook: block direct Worker/coding Agent spawns when /go is not active.

Purpose:
  The тройка pipeline (Flow Designer → Worker + Verifier) is the mandated path
  for all coding work. This hook enforces that at the harness layer: if the
  Lead tries to spawn a coding Agent directly without /go being active (i.e.
  the .go_active marker is absent), the spawn is blocked with a clear message.

Contract:
  stdin  — PreToolUse JSON {tool_name, tool_input, cwd, agent_id, agent_type,
           session_id}
  stderr — feedback on block
  exit   — 0 allow, 2 block

Sub-agent auto-skip:
  When agent_id or agent_type is non-empty, the hook is already running inside
  a sub-agent — the gate's purpose is satisfied, exit 0 immediately.

Enforcement conditions (ALL must be true to block):
  1. tool_name == 'Agent'
  2. phase == 'IMPLEMENT' (from <project_root>/.claude/.phase)
  3. .go_active marker is absent (from <project_root>/.claude/.go_active)
  4. subagent_type is NOT in {'Explore', 'Plan'}
  5. description OR prompt contains at least one coding keyword

Bypass:
  env CLAUDE_BOOSTER_SKIP_GO_GATE=1 -> exit 0 unconditionally

Marker file:
  <project_root>/.claude/.go_active -- created by /go at Phase 0 completion,
  removed at Phase 4 end.

Decision telemetry:
  Every invocation appends one JSON line to
  ~/.claude/logs/go_gate_decisions.jsonl with fields
  {ts, gate, decision, reason, tool_name, session_id, has_marker}.
  Fail-soft: log failures are swallowed.

Limitations:
  - Marker file is per-project; two concurrent /go sessions on the same repo
    share the same marker (acceptable -- both are using тройка).
  - Gate fails-open on any unhandled exception -- never crashes Claude.

ENV/Files:
  Reads : env CLAUDE_BOOSTER_SKIP_GO_GATE (optional)
  Reads : <project_root>/.claude/.phase
  Reads : <project_root>/.claude/.go_active
  Writes: ~/.claude/logs/go_gate_decisions.jsonl
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ---- Shared helpers -------------------------------------------------------

try:
    from _gate_common import (
        DECISION_ALLOW,
        DECISION_AUTO_SKIP,
        DECISION_BLOCK,
        append_jsonl,
        is_subagent_context,
        iso_now,
        project_root_from,
    )
except ImportError:
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    from _gate_common import (  # type: ignore[no-redef]
        DECISION_ALLOW,
        DECISION_AUTO_SKIP,
        DECISION_BLOCK,
        append_jsonl,
        is_subagent_context,
        iso_now,
        project_root_from,
    )

# ---- Coding keywords (import from model_tag_enforcer, with fallback) ------

try:
    # model_tag_enforcer lives in the same scripts directory.
    import importlib.util as _ilu
    _mte_path = Path(__file__).resolve().parent / "model_tag_enforcer.py"
    _spec = _ilu.spec_from_file_location("model_tag_enforcer", str(_mte_path))
    _mte = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_mte)  # type: ignore[union-attr]
    _CODING_KEYWORDS: frozenset[str] = _mte._CODING_KEYWORDS
    del _ilu, _mte_path, _spec, _mte
except Exception:
    # Fallback -- verbatim copy from model_tag_enforcer.py as of 2026-05-16.
    _CODING_KEYWORDS = frozenset({
        "worker", "verifier", "implement", "fix", "refactor", "write code",
        "apply", "edit", "modify", "add", "change", "update",
    })

# ---- Constants ------------------------------------------------------------

GO_GATE_LOG_NAME = "go_gate_decisions.jsonl"
ENFORCE_PHASE = "IMPLEMENT"
MARKER_REL = ".claude/.go_active"
PHASE_REL = ".claude/.phase"
NON_CODING_SUBAGENT_TYPES: frozenset[str] = frozenset({"Explore", "Plan"})


# ---- Helpers --------------------------------------------------------------

def _read_phase(root: Path) -> "str | None":
    """Read current workflow phase from <root>/.claude/.phase. Returns UPPER CASE or None."""
    phase_file = root / PHASE_REL
    if not phase_file.exists():
        return None
    try:
        return phase_file.read_text().strip().upper()
    except OSError:
        return None


def _marker_exists(root: Path) -> bool:
    """Return True if the .go_active marker file exists."""
    try:
        return (root / MARKER_REL).exists()
    except OSError:
        return False


def _has_coding_keyword(text: str) -> bool:
    """Return True if any coding keyword appears in text (lowercased)."""
    lowered = text.lower()
    return any(kw in lowered for kw in _CODING_KEYWORDS)


def _log(record: dict) -> None:
    """Append a record to go_gate_decisions.jsonl. Fail-soft."""
    append_jsonl(GO_GATE_LOG_NAME, record)


def _base_record(data: dict, has_marker: bool) -> dict:
    return {
        "ts": iso_now(),
        "gate": "go",
        "tool_name": data.get("tool_name") or "",
        "session_id": data.get("session_id") or "",
        "has_marker": has_marker,
    }


# ---- Main gate logic ------------------------------------------------------

def main() -> int:
    """Entry point. Returns exit code: 0 = allow, 2 = block."""
    try:
        return _run()
    except Exception:
        # Fail-open: the gate must never crash Claude's session.
        # If something unexpected happens, silently allow and continue.
        try:
            _log({
                "ts": iso_now(),
                "gate": "go",
                "decision": DECISION_ALLOW,
                "reason": "unhandled exception -- fail-open",
                "tool_name": "",
                "session_id": "",
                "has_marker": False,
            })
        except Exception:
            pass
        return 0


def _run() -> int:
    # ---- Parse stdin -------------------------------------------------------
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        raw = ""

    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        data = {}

    if not isinstance(data, dict):
        data = {}

    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or ""
    session_id = data.get("session_id") or ""

    # ---- Directive 2: check sub-agent context FIRST -----------------------
    if is_subagent_context(data):
        # Gate's purpose (force тройка on the Lead) is already satisfied --
        # delegation has happened. Auto-skip without touching marker/phase.
        _log({
            "ts": iso_now(),
            "gate": "go",
            "decision": DECISION_AUTO_SKIP,
            "reason": "sub-agent context (agent_id/agent_type set)",
            "tool_name": tool,
            "session_id": session_id,
            "has_marker": False,
        })
        return 0

    # ---- Directive 7: bypass env var --------------------------------------
    if os.environ.get("CLAUDE_BOOSTER_SKIP_GO_GATE") == "1":
        _log({
            "ts": iso_now(),
            "gate": "go",
            "decision": DECISION_ALLOW,
            "reason": "env CLAUDE_BOOSTER_SKIP_GO_GATE=1",
            "tool_name": tool,
            "session_id": session_id,
            "has_marker": False,
        })
        return 0

    # ---- Directive 3: only intercept Agent tool ---------------------------
    if tool != "Agent":
        _log({
            "ts": iso_now(),
            "gate": "go",
            "decision": DECISION_ALLOW,
            "reason": f"tool {tool!r} is not 'Agent' -- gate only intercepts Agent calls",
            "tool_name": tool,
            "session_id": session_id,
            "has_marker": False,
        })
        return 0

    # ---- Directive 9: resolve project root --------------------------------
    root = project_root_from(cwd)
    if root is None:
        # No project context -- can't enforce, fail-open.
        _log({
            "ts": iso_now(),
            "gate": "go",
            "decision": DECISION_ALLOW,
            "reason": "project_root_from() returned None -- fail-open (no project context)",
            "tool_name": tool,
            "session_id": session_id,
            "has_marker": False,
        })
        return 0

    has_marker = _marker_exists(root)
    base = _base_record(data, has_marker)

    # ---- Directive 4: check subagent_type BEFORE keyword matching ----------
    subagent_type = tool_input.get("subagent_type") or ""
    if subagent_type in NON_CODING_SUBAGENT_TYPES:
        _log({**base, "decision": DECISION_ALLOW,
              "reason": f"subagent_type={subagent_type!r} is non-coding (Explore/Plan)"})
        return 0

    # ---- Directive 5: only enforce in IMPLEMENT phase ----------------------
    phase = _read_phase(root)
    if phase != ENFORCE_PHASE:
        _log({**base, "decision": DECISION_ALLOW,
              "reason": f"phase={phase!r} is not IMPLEMENT -- gate inactive"})
        return 0

    # ---- Directive 6: allow if .go_active marker is present ---------------
    if has_marker:
        _log({**base, "decision": DECISION_ALLOW,
              "reason": ".go_active marker present -- /go is active"})
        return 0

    # ---- Directive 12: keyword match against description AND prompt --------
    description = tool_input.get("description") or ""
    prompt = tool_input.get("prompt") or ""
    combined_text = description + " " + prompt
    if not _has_coding_keyword(combined_text):
        _log({**base, "decision": DECISION_ALLOW,
              "reason": "no coding keywords found in description/prompt"})
        return 0

    # ---- Directive 10: block with stderr message --------------------------
    _log({**base, "decision": DECISION_BLOCK,
          "reason": "IMPLEMENT phase + no .go_active marker + coding keywords detected"})
    sys.stderr.write(
        "go_gate: Use /go for coding tasks. "
        "Direct Worker/coding Agent spawn blocked.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
