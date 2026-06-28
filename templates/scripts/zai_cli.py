#!/usr/bin/env python3
"""Run GLM-5.2 via Z.ai's Anthropic-compatible Claude Code endpoint.

Purpose
-------
Provides a small, deterministic CLI bridge so Booster commands can use Z.ai
as a third external-review provider without mutating ``~/.claude/settings.json``
or granting write tools to the external reviewer.

Contract
--------
Input  : prompt text on stdin.
Output : model response on stdout; diagnostics on stderr.
Exit   : child ``claude`` exit code; 64 when no Z.ai credential is available.

CLI
---
    printf 'Reply GLM_OK' | python3 ~/.claude/scripts/zai_cli.py smoke
    printf '<review prompt>' | python3 ~/.claude/scripts/zai_cli.py review --budget 5

Limitations
-----------
- Requires Claude Code CLI on PATH. Z.ai is used only as the API backend.
- Does not grant write tools by default. ``review`` is read-only unless the
  caller explicitly changes this script in a future audited commit.
- The API key is read from ``ZAI_API_KEY`` first, then from a chmod-600 local
  secret file. It is never printed.
- Claude CLI text mode does not expose token usage reliably; telemetry records
  duration/success and leaves tokens NULL unless a future CLI mode provides them.

ENV / Files
-----------
- Reads: ``ZAI_API_KEY`` or ``~/.claude/secrets/zai_api_key``.
- Writes: ``~/.claude/rolling_memory.db`` ``model_metrics`` row, best-effort.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


BASE_URL = "https://api.z.ai/api/anthropic"
DEFAULT_MODEL = "glm-5.2[1m]"
DEFAULT_AIR_MODEL = "glm-5.2-air"
PROVIDER = "zai-cli"
DEFAULT_DB_PATH = Path.home() / ".claude" / "rolling_memory.db"
DEFAULT_SECRET_PATH = Path.home() / ".claude" / "secrets" / "zai_api_key"
INSERT_METRIC_SQL = """
INSERT INTO model_metrics
    (ts_utc, provider, model, task_category, duration_ms, num_turns,
     per_turn_ms, tokens_in, tokens_out, success, session_id, project_root)
VALUES
    (datetime('now'), ?, ?, ?, ?, 1, ?, NULL, NULL, ?, ?, ?)
"""


def _secret_path() -> Path:
    override = os.environ.get("ZAI_API_KEY_FILE", "").strip()
    return Path(override).expanduser() if override else DEFAULT_SECRET_PATH


def _api_key() -> str:
    """Return the Z.ai API key from env or local secret file."""
    key = os.environ.get("ZAI_API_KEY", "").strip()
    if key:
        return key

    path = _secret_path()
    try:
        if path.exists():
            key = path.read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    return key


def _env() -> dict[str, str]:
    key = _api_key()
    if not key:
        print(
            "zai_cli: missing ZAI_API_KEY; export it or create ~/.claude/secrets/zai_api_key.",
            file=sys.stderr,
        )
        raise SystemExit(64)
    env = os.environ.copy()
    env["ANTHROPIC_AUTH_TOKEN"] = key
    env.setdefault("ANTHROPIC_BASE_URL", BASE_URL)
    env.setdefault("ANTHROPIC_DEFAULT_OPUS_MODEL", DEFAULT_MODEL)
    env.setdefault("ANTHROPIC_DEFAULT_SONNET_MODEL", DEFAULT_MODEL)
    env.setdefault("ANTHROPIC_DEFAULT_HAIKU_MODEL", DEFAULT_AIR_MODEL)
    return env


def _metrics_db_path() -> Path:
    """Return the metrics DB path, allowing tests to redirect writes."""
    override = os.environ.get("CLAUDE_BOOSTER_METRICS_DB", "").strip()
    return Path(override).expanduser() if override else DEFAULT_DB_PATH


def _record_metric(
    *,
    model: str,
    task_category: str,
    duration_ms: int,
    success: bool,
) -> None:
    """Record Z.ai price/perf telemetry without affecting the review result."""
    if os.environ.get("ZAI_CLI_DISABLE_TELEMETRY") == "1":
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

    session_id = os.environ.get("CLAUDE_SESSION_ID", "")
    try:
        project_root = os.getcwd()
    except OSError:
        project_root = ""

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
        print(f"zai_cli: telemetry skipped: {exc}", file=sys.stderr)


def _run_claude(
    prompt: str,
    *,
    model: str,
    budget: str,
    tools: str,
    read_only: bool,
    task_category: str,
) -> int:
    if not prompt.strip():
        print("zai_cli: empty stdin prompt", file=sys.stderr)
        return 65
    if not model.strip():
        print("zai_cli: empty model", file=sys.stderr)
        return 66
    if not task_category.strip():
        print("zai_cli: empty task category", file=sys.stderr)
        return 67

    cmd = [
        "claude",
        "--bare",
        "--print",
        "--model",
        model,
        "--max-budget-usd",
        budget,
        "--permission-mode",
        "dontAsk",
        "--tools",
        tools,
    ]
    if read_only:
        cmd.extend(
            [
                "--disallowedTools",
                "Edit,Write,NotebookEdit",
            ]
        )

    started = time.monotonic()
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        env=_env(),
        check=False,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    _record_metric(
        model=model,
        task_category=task_category,
        duration_ms=duration_ms,
        success=proc.returncode == 0,
    )
    return int(proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run GLM-5.2 through Z.ai.")
    parser.add_argument("mode", choices=("smoke", "review"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--budget", default="3")
    parser.add_argument(
        "--category",
        default=None,
        help="model_metrics task_category; defaults to audit_secondary for review and zai_smoke for smoke.",
    )
    args = parser.parse_args()

    prompt = sys.stdin.read()
    if args.mode == "smoke":
        return _run_claude(
            prompt,
            model=args.model,
            budget=args.budget,
            tools="",
            read_only=True,
            task_category=args.category or "zai_smoke",
        )
    return _run_claude(
        prompt,
        model=args.model,
        budget=args.budget,
        tools="Read,Grep,Glob,Bash(git *),Bash(rg *),Bash(sed *),Bash(find *),Bash(ls *),Bash(wc *)",
        read_only=True,
        task_category=args.category or "audit_secondary",
    )


if __name__ == "__main__":
    raise SystemExit(main())
