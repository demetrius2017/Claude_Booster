#!/usr/bin/env python3
"""PostToolUse hook — advisory: write a one-shot marker when context is large.

Purpose:
    Measure REAL context-window occupancy after every tool call by reading the
    last assistant ``usage`` block from the session transcript (falling back to
    a byte-size estimate if no usage block exists yet). When occupancy crosses
    the effective threshold (default 60% of the actual window — 600k on a 1M
    window) and no marker for this session exists yet, write a marker file so
    the next UserPromptSubmit hook can inject a one-line /compact reminder.

    This replaces self-discipline with deterministic automation: Lead no longer
    has to remember to check context size; the harness signals proactively. And
    it no longer mis-fires at the old 200k-calibrated absolute 120k on machines
    running a 1M-token window.

Contract:
    stdin  — PostToolUse JSON: {session_id, transcript_path, cwd, ...}
    stdout — silent (advisory; nothing emitted to Claude's context)
    exit   — 0 always (never blocks tool use)

Bypass:
    CLAUDE_BOOSTER_SKIP_COMPACT_ADVISOR=1  → exit 0 immediately, no-op

Files:
    ~/.claude/.compact_recommended_<session_id>  — one-shot marker
        (content = "<observed_tokens> <window>", two space-separated ints)
    CLAUDE_BOOSTER_CONTEXT_WINDOW   — env override for assumed window (default 1_000_000)
    CLAUDE_BOOSTER_COMPACT_PCT      — env override for fire fraction (default 0.6)
    CLAUDE_BOOSTER_COMPACT_THRESHOLD — absolute token override (back-compat; wins when set)
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

try:
    from _gate_common import (
        append_jsonl,
        iso_now,
        real_context_tokens,
        effective_compact_threshold,
    )
except ImportError:
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    from _gate_common import (  # type: ignore[no-redef]
        append_jsonl,
        iso_now,
        real_context_tokens,
        effective_compact_threshold,
    )

_SKIP = os.environ.get("CLAUDE_BOOSTER_SKIP_COMPACT_ADVISOR", "")

_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def main() -> int:
    if _SKIP:
        append_jsonl("compact_advisor.jsonl", {"ts": iso_now(), "event": "env_skip"})
        return 0

    # Parse stdin — malformed JSON is a silent no-op
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)
    except Exception:
        append_jsonl("compact_advisor.jsonl", {"ts": iso_now(), "event": "invalid_input", "reason": "malformed_json"})
        return 0

    if not isinstance(data, dict):
        append_jsonl("compact_advisor.jsonl", {"ts": iso_now(), "event": "invalid_input", "reason": "malformed_json"})
        return 0

    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")

    if not session_id or not transcript_path:
        append_jsonl("compact_advisor.jsonl", {"ts": iso_now(), "event": "invalid_input", "reason": "missing_field"})
        return 0

    # Defense-in-depth: session_id must be a valid UUID to be used in a filesystem path
    if not _SESSION_ID_RE.match(session_id):
        append_jsonl("compact_advisor.jsonl", {"ts": iso_now(), "event": "invalid_input", "reason": "invalid_uuid", "session_id": session_id[:32]})
        return 0

    marker = Path.home() / ".claude" / f".compact_recommended_{session_id}"

    # One-shot: if marker already exists, nothing to do.
    # Guard against stale markers left by dead sessions (> 2 hours old).
    try:
        mtime = marker.stat().st_mtime
    except FileNotFoundError:
        pass  # no marker — fall through to threshold check
    except OSError:
        return 0  # can't stat — treat as existing, skip
    else:
        if time.time() - mtime < 7200:
            return 0  # marker is fresh, reminder already issued
        try:
            marker.unlink(missing_ok=True)  # stale marker from dead session — clean it up
        except OSError:
            return 0  # can't unlink — treat as existing, skip

    # Prefer REAL context occupancy from the last assistant usage block.
    # Fall back to byte-size estimate (bytes // 4 ≈ tokens) only when no
    # usage block exists yet (e.g. very early in the session).
    try:
        size_bytes = os.stat(transcript_path).st_size
    except OSError:
        append_jsonl("compact_advisor.jsonl", {"ts": iso_now(), "event": "invalid_input", "reason": "stat_failed", "transcript_path": transcript_path[:200]})
        return 0

    real = real_context_tokens(transcript_path)
    observed = real if real is not None else (size_bytes // 4)
    observed = int(observed)

    threshold, window = effective_compact_threshold(observed)

    if observed < threshold:
        return 0

    # Write marker atomically to avoid partial writes / race conditions.
    # Content: "<observed> <window>" (two space-separated ints).
    try:
        marker_dir = marker.parent
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=marker_dir,
            delete=False,
            prefix=".compact_tmp_",
            suffix=f"_{session_id}",
        ) as tmp:
            tmp.write(f"{observed} {window}")
            tmp_path = tmp.name
        os.replace(tmp_path, marker)
        append_jsonl("compact_advisor.jsonl", {"ts": iso_now(), "event": "marker_written", "session_id": session_id, "observed": observed, "threshold": threshold, "window": window, "source": "usage" if real is not None else "bytes"})
    except Exception:
        # Best-effort advisory: never raise, never fail the hook
        pass

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never let the advisory crash the hook
        try:
            sys.stderr.write(f"compact_advisor: unhandled exception: {exc}\n")
        except Exception:
            pass
        sys.exit(0)
