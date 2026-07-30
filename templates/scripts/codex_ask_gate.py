#!/usr/bin/env python3
"""Codex Stop-hook adapter for the Claude-oriented ``ask_gate``.

Purpose:
  Preserve ``ask_gate`` classification and logging while translating its
  legacy ``stderr + exit 2`` block signal into Codex's JSON Stop contract.

Contract:
  stdin  — one Codex/Claude Stop event JSON object.
  stdout — empty on allow, or exactly one JSON object on block.
  exit   — 0 after a classified allow/block; non-zero only on adapter failure.

Limitations:
  This adapter intentionally delegates all policy to ``ask_gate.py``.  It must
  live beside that module in ``~/.claude/scripts``.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys

import ask_gate


def main() -> int:
    """Run the canonical gate and emit only output accepted by Codex Stop."""
    captured_stderr = io.StringIO()
    with contextlib.redirect_stderr(captured_stderr):
        result = ask_gate.main()

    if result == 0:
        return 0
    if result != 2:
        raise RuntimeError(f"ask_gate returned unsupported exit status {result!r}")

    reason = captured_stderr.getvalue().strip()
    if not reason:
        reason = "ask_gate blocked the turn without a diagnostic"
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
