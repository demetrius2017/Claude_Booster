#!/usr/bin/env python3
"""
check_fast_mode.py — SessionStart hook: fast mode guardian.

Purpose:
  Detects configuration threats that disable or compromise Claude Code's fast
  mode, and injects a warning into the session context so the user can act
  before they notice degraded performance.

Contract:
  Reads JSON from stdin (Claude Code SessionStart event — we ignore it, only
  need the trigger). If any of the 5 guards trip, emits a combined warning via
  additionalContext on stdout per the SessionStart hook protocol. Always exits
  0 — never block session start.

CLI / Examples:
  # Dry-run (pipe empty JSON to satisfy stdin):
  echo '{}' | ${PYTHON} ${CLAUDE_HOME}/scripts/check_fast_mode.py

  # Test a specific threat:
  echo '{}' | ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-8 ${PYTHON} ${CLAUDE_HOME}/scripts/check_fast_mode.py

Limitations:
  - Only checks the 5 known fast mode threat vectors; new env vars are not
    auto-discovered.
  - Does NOT auto-fix settings.json — report only.
  - If settings.json is absent or malformed, warns and continues (no crash).

ENV / Files:
  ${CLAUDE_HOME}/settings.json  — read to check fastMode and fastModePerSessionOptIn
  ANTHROPIC_DEFAULT_OPUS_MODEL  — if set, may override model and break fast mode
  CLAUDE_CODE_DISABLE_FAST_MODE — if "1", unconditionally kills fast mode
  ANTHROPIC_BASE_URL            — if set, availability check may fail against
                                  non-standard endpoints
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def check_fast_mode() -> list[str]:
    """Return list of warning strings. Empty list means all clear."""
    warnings: list[str] = []

    # Guard 1: ANTHROPIC_DEFAULT_OPUS_MODEL
    opus_model = os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
    if opus_model is not None:
        warnings.append(
            f"⚡ FAST MODE THREAT: ANTHROPIC_DEFAULT_OPUS_MODEL={opus_model} is set. "
            "This can override the model and disable fast mode. "
            "Remove from shell profile."
        )

    # Guard 2: CLAUDE_CODE_DISABLE_FAST_MODE
    disable_fast = os.environ.get("CLAUDE_CODE_DISABLE_FAST_MODE")
    if disable_fast == "1":
        warnings.append(
            "⚡ FAST MODE KILLED: CLAUDE_CODE_DISABLE_FAST_MODE=1 is set. "
            "Fast mode is unconditionally disabled. Remove this env var."
        )

    # Guard 3: ANTHROPIC_BASE_URL
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url is not None:
        warnings.append(
            f"⚡ FAST MODE THREAT: ANTHROPIC_BASE_URL={base_url} is set. "
            "Fast mode availability check may fail against non-standard endpoints."
        )

    # Guards 4 & 5: read settings.json
    settings: dict = {}
    if not SETTINGS_PATH.exists():
        warnings.append(
            f"⚡ FAST MODE CHECK: {SETTINGS_PATH} not found. "
            "Cannot verify fastMode setting."
        )
    else:
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            warnings.append(
                f"⚡ FAST MODE CHECK: {SETTINGS_PATH} parse error ({exc}). "
                "Cannot verify fastMode setting."
            )

    if settings:
        # Guard 4: fastMode cost warning — fast mode is extra usage,
        # NOT included in Max subscription ($30/$150 MTok from first token)
        fast_mode = settings.get("fastMode")
        if fast_mode is True:
            warnings.append(
                "⚡ FAST MODE ON — COST WARNING: Fast mode is billed as extra usage "
                "at $30/$150 per MTok, even on Max subscription. Not included in plan "
                "limits. Disable with /fast if cost matters more than speed."
            )

        # Guard 5: fastModePerSessionOptIn
        per_session = settings.get("fastModePerSessionOptIn")
        if per_session is True:
            warnings.append(
                "⚡ FAST MODE RESETS EACH SESSION: fastModePerSessionOptIn is true "
                "(likely org-managed). Fast mode must be enabled manually each session."
            )

    return warnings


def main() -> None:
    # Consume stdin — Claude Code feeds us the SessionStart event JSON;
    # we don't need its content but must drain it to avoid broken-pipe errors.
    try:
        sys.stdin.read()
    except Exception:
        pass

    warnings = check_fast_mode()
    if not warnings:
        return  # silent = healthy

    text = "\n".join(warnings)
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }
    sys.stdout.write(json.dumps(payload))


if __name__ == "__main__":
    main()
