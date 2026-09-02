#!/usr/bin/env python3
"""Shared, schema-free provider failure/success event sidecar.

Purpose
-------
External provider bridges (``zai_cli.py``, ``grok_cli.py``) must record typed
availability events so ``model_balancer.py`` can route around a provider that
is failing. This module owns the single on-disk format and the concurrency-safe
append, so every bridge writes identical rows without a DB migration.

Contract
--------
Input  : one JSON-serialisable event dict per call.
Output : one JSON line appended to the sidecar log; no return value.
         Only ``OSError`` is swallowed (reported on stderr as best-effort
         telemetry). An empty ``log_label`` raises ``ValueError``, and a
         non-JSON-serialisable event raises ``TypeError``, because both are
         caller bugs rather than environment failures.
Exit   : not a CLI; importable module only.

CLI
---
    Not applicable. Import from a sibling bridge script:

        from provider_failure_log import append_provider_event, failure_log_path

Limitations
-----------
- The log is bounded and rotated in place; old lines are dropped, not archived.
- Callers own event schema and secret redaction before calling in.
- Only ``OSError`` is best-effort; caller-contract errors propagate.

ENV / Files
-----------
- Reads: ``CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG``,
  ``CLAUDE_BOOSTER_PROVIDER_EVENTS_MAX_BYTES``,
  ``CLAUDE_BOOSTER_PROVIDER_EVENTS_RETAIN_LINES``.
- Writes: ``~/.claude/logs/model_provider_failures.jsonl`` (chmod 600).
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any


DEFAULT_FAILURE_LOG_PATH = Path.home() / ".claude" / "logs" / "model_provider_failures.jsonl"
DEFAULT_FAILURE_LOG_MAX_BYTES = 256 * 1024
DEFAULT_FAILURE_LOG_RETAIN_LINES = 1000
_PROVIDER_EVENT_THREAD_LOCK = threading.Lock()


def failure_log_path() -> Path:
    """Return the schema-free provider failure log path."""
    override = os.environ.get("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", "").strip()
    return Path(override).expanduser() if override else DEFAULT_FAILURE_LOG_PATH


def event_bounds() -> tuple[int, int]:
    """Return provider-event sidecar bounds from env with safe floors."""
    try:
        max_bytes = int(os.environ.get("CLAUDE_BOOSTER_PROVIDER_EVENTS_MAX_BYTES", "262144"))
    except (TypeError, ValueError):
        max_bytes = DEFAULT_FAILURE_LOG_MAX_BYTES
    try:
        retain_lines = int(os.environ.get("CLAUDE_BOOSTER_PROVIDER_EVENTS_RETAIN_LINES", "1000"))
    except (TypeError, ValueError):
        retain_lines = DEFAULT_FAILURE_LOG_RETAIN_LINES
    return max(4096, max_bytes), max(10, retain_lines)


def append_provider_event(event: dict[str, Any], *, log_label: str) -> None:
    """Append one provider event under an exclusive Unix file lock."""
    if not log_label.strip():
        raise ValueError("log_label must be non-empty")
    path = failure_log_path()
    max_bytes, retain_lines = event_bounds()
    line = json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n"
    with _PROVIDER_EVENT_THREAD_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "r+", encoding="utf-8") as fh:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                    try:
                        fh.seek(0, os.SEEK_END)
                        if fh.tell() + len(line.encode("utf-8")) > max_bytes:
                            fh.seek(0)
                            existing = fh.read().splitlines()
                            kept = existing[-retain_lines:]
                            payload = ("\n".join(kept) + ("\n" if kept else "") + line)
                            payload_lines = payload.splitlines()
                            while payload_lines and len(("\n".join(payload_lines) + "\n").encode("utf-8")) > max_bytes:
                                payload_lines.pop(0)
                            payload = "\n".join(payload_lines) + ("\n" if payload_lines else "")
                            fh.seek(0)
                            fh.truncate(0)
                            fh.write(payload)
                        else:
                            fh.write(line)
                        fh.flush()
                        os.fsync(fh.fileno())
                    finally:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        except OSError as exc:
            print(f"{log_label}: provider-event telemetry skipped: {exc}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover - module is import-only
    print(__doc__, file=sys.stderr)
    raise SystemExit(2)
