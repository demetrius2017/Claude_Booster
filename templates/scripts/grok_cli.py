#!/usr/bin/env python3
"""Run Grok Build CLI as an external Booster reviewer.

Purpose
-------
Provides a deterministic read-only bridge to xAI Grok for Booster audit and
hackathon review stages. Write-capable coding uses ``grok_sandbox_worker.sh``
instead, so this script can stay review-only by default.

Contract
--------
Input  : prompt text on stdin.
Output : Grok response on stdout; diagnostics on stderr.
Exit   : child ``grok`` exit code; 65 for empty prompt; 69 for empty successful
         output; 124 for timeout; 127 if binary missing.

CLI
---
    printf 'Reply GROK_OK' | python3 ~/.claude/scripts/grok_cli.py smoke
    printf '<review prompt>' | python3 ~/.claude/scripts/grok_cli.py review --budget-turns 3

Limitations
-----------
- Requires Grok CLI authentication via ``grok login`` or another supported xAI
  auth mechanism.
- ``review`` denies edit/write-style tools. Use ``grok_sandbox_worker.sh`` for
  code-writing tasks.

ENV / Files
-----------
- Reads: Grok CLI auth/config under ``~/.grok``.
- Writes: ``~/.claude/rolling_memory.db`` ``model_metrics`` row, best-effort.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_MODEL = "grok-4.5"
DEFAULT_CODER_MODEL = "grok-4.5"
PROVIDER = "grok-cli"
DEFAULT_DB_PATH = Path.home() / ".claude" / "rolling_memory.db"
DEFAULT_GROK_BIN = Path.home() / ".grok" / "bin" / "grok"
try:
    DEFAULT_TIMEOUT_S = float(os.environ.get("GROK_CLI_TIMEOUT_S", "180"))
except (TypeError, ValueError):
    DEFAULT_TIMEOUT_S = 180.0
READ_ONLY_DENIED_TOOLS = (
    "Edit",
    "Write",
    "NotebookEdit",
    "create_goal",
    "update_goal",
)
INSERT_METRIC_SQL = """
INSERT INTO model_metrics
    (ts_utc, provider, model, task_category, duration_ms, num_turns,
     per_turn_ms, tokens_in, tokens_out, success, session_id, project_root)
VALUES
    (datetime('now'), ?, ?, ?, ?, 1, ?, NULL, NULL, ?, ?, ?)
"""


def _grok_bin() -> str:
    override = os.environ.get("GROK_BIN", "").strip()
    if override:
        return override
    if DEFAULT_GROK_BIN.exists():
        return str(DEFAULT_GROK_BIN)
    return shutil.which("grok") or "grok"


def _metrics_db_path() -> Path:
    override = os.environ.get("CLAUDE_BOOSTER_METRICS_DB", "").strip()
    return Path(override).expanduser() if override else DEFAULT_DB_PATH


def _record_metric(
    *,
    model: str,
    task_category: str,
    duration_ms: int,
    success: bool,
) -> None:
    """Record Grok price/perf telemetry without affecting review output."""
    if os.environ.get("GROK_CLI_DISABLE_TELEMETRY") == "1":
        return
    if not model.strip():
        raise ValueError("model must be non-empty")
    if not task_category.strip():
        raise ValueError("task_category must be non-empty")
    if duration_ms < 0:
        raise ValueError(f"duration_ms must be >= 0, got {duration_ms}")

    db_path = _metrics_db_path()
    if not db_path.exists():
        return
    try:
        project_root = os.getcwd()
    except OSError:
        project_root = ""
    session_id = os.environ.get("CLAUDE_SESSION_ID", "")

    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0, isolation_level=None)
        try:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_metrics'"
            ).fetchone()
            if table_exists is None:
                return
            conn.execute(
                INSERT_METRIC_SQL,
                (
                    PROVIDER,
                    model,
                    task_category,
                    duration_ms,
                    duration_ms,
                    1 if success else 0,
                    session_id,
                    project_root,
                ),
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"grok_cli: telemetry skipped: {exc}", file=sys.stderr)


def _run_grok(
    prompt: str,
    *,
    model: str,
    budget_turns: int,
    read_only: bool,
    task_category: str,
) -> int:
    if not prompt.strip():
        print("grok_cli: empty stdin prompt", file=sys.stderr)
        return 65
    if not model.strip():
        print("grok_cli: empty model", file=sys.stderr)
        return 66
    if not task_category.strip():
        print("grok_cli: empty task category", file=sys.stderr)
        return 67
    if budget_turns < 1:
        print(f"grok_cli: budget_turns must be >= 1, got {budget_turns}", file=sys.stderr)
        return 68

    binary = _grok_bin()
    if shutil.which(binary) is None and not Path(binary).exists():
        print(f"grok_cli: grok binary not found: {binary}", file=sys.stderr)
        return 127

    cmd = [
        binary,
        "-p",
        prompt,
        "--model",
        model,
        "--max-turns",
        str(budget_turns),
        "--output-format",
        "plain",
        "--disable-web-search",
        "--permission-mode",
        "dontAsk",
        "--no-subagents",
    ]
    if read_only:
        for tool in READ_ONLY_DENIED_TOOLS:
            cmd.extend(["--deny", tool])
        cmd.extend(["--disallowed-tools", ",".join(READ_ONLY_DENIED_TOOLS)])

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=DEFAULT_TIMEOUT_S,
        )
        raw = proc.stdout
        stderr = proc.stderr
        final_returncode = int(proc.returncode)
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout if isinstance(exc.stdout, bytes) else b""
        stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
        final_returncode = 124
    duration_ms = int((time.monotonic() - started) * 1000)
    if stderr:
        sys.stderr.buffer.write(stderr)
        sys.stderr.buffer.flush()
    is_empty = raw.decode("utf-8", "replace").strip() == ""
    if final_returncode == 0 and is_empty:
        print("grok_cli: empty response", file=sys.stderr)
        final_returncode = 69
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()
    _record_metric(
        model=model,
        task_category=task_category,
        duration_ms=duration_ms,
        success=final_returncode == 0 and not is_empty,
    )
    return final_returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Grok through xAI Grok CLI.")
    parser.add_argument("mode", choices=("smoke", "review"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--budget-turns", type=int, default=3)
    parser.add_argument(
        "--category",
        default=None,
        help="model_metrics task_category; defaults to audit_tertiary for review and grok_smoke for smoke.",
    )
    args = parser.parse_args()

    prompt = sys.stdin.read()
    if args.mode == "smoke":
        return _run_grok(
            prompt,
            model=args.model,
            budget_turns=args.budget_turns,
            read_only=True,
            task_category=args.category or "grok_smoke",
        )
    return _run_grok(
        prompt,
        model=args.model,
        budget_turns=args.budget_turns,
        read_only=True,
        task_category=args.category or "audit_tertiary",
    )


if __name__ == "__main__":
    raise SystemExit(main())
