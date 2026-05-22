#!/usr/bin/env python3
"""
Purpose:
    PostToolUse hook -- captures per-tool-call latency + token usage into the
    model_metrics table of ~/.claude/rolling_memory.db.

Contract:
    Reads a JSON event from stdin.  Silently exits 0 on: empty stdin,
    malformed JSON, missing required fields, DB error, or env var
    CLAUDE_BOOSTER_SKIP_METRIC_CAPTURE=1.

    For Task/Agent tools: inserts a row with provider=anthropic, timing and
    token data extracted defensively. Duration comes from top-level
    `duration_ms` (CC v2.1.139+, pure tool time excl. hooks/permissions),
    falling back to nested usage paths (tool_response.usage, etc.).
    If no usage data is found anywhere, logs a once-per-UTC-day diagnostic
    sample to ~/.claude/logs/model_metric_capture_sample.jsonl and exits.

    For Bash tools: inserts a row only when the command invokes codex_worker.sh
    or "codex exec -m" with a model in the known allowlist; uses token-boundary
    anchored regex to avoid matching file paths, grep invocations, or heredoc
    fragments.  provider=codex-cli, duration_ms=NULL.

CLI:
    echo '<json-event>' | python3 model_metric_capture.py

Limitations:
    Requires sqlite3 (stdlib).  No retry on DB lock -- timeout=2.0 s then silent.

ENV:
    CLAUDE_BOOSTER_SKIP_METRIC_CAPTURE=1  -- skip all processing, exit 0.

Files:
    ~/.claude/rolling_memory.db  -- target SQLite database (model_metrics table).
    ~/.claude/logs/model_metric_capture_sample.jsonl  -- no-usage diagnostic log.
    ~/.claude/logs/.metric_capture_sample_YYYYMMDD  -- daily gate marker.
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime

DB_PATH = os.path.expanduser("~/.claude/rolling_memory.db")
LOGS_DIR = os.path.expanduser("~/.claude/logs")
SAMPLE_LOG = os.path.join(LOGS_DIR, "model_metric_capture_sample.jsonl")

# Provider name constants -- must match templates/scripts/model_balancer.py
# (kept local to avoid hot-path import of model_balancer on every tool call).
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_CODEX = "codex-cli"

# Live ChatGPT-subscription model allowlist (verified 2026-05-12).
# Only commands that reference one of these models get a DB row.
_CODEX_ALLOWLIST = frozenset({
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.2",
})

# Codex command patterns — Group 1 captures the model token.
# Anchor `[/;&|]` matches path separator (full-path invocations like
# ~/.claude/scripts/codex_worker.sh) and shell operators. Model charset
# `[a-zA-Z][a-zA-Z0-9._-]*` stops at shell metacharacters.
# Keep in sync with delegate_gate.py CODEX_WORKER_PATTERNS.
_RE_CODEX_WORKER = re.compile(
    r'(?:^|[/;&|])\s*codex_worker\.sh\s+([a-zA-Z][a-zA-Z0-9._-]*)',
)
_RE_CODEX_SANDBOX_WORKER = re.compile(
    r'(?:^|[/;&|])\s*codex_sandbox_worker\.sh\s+([a-zA-Z][a-zA-Z0-9._-]*)',
)
_RE_CODEX_EXEC = re.compile(
    r'(?:^|[/;&|])\s*codex\s+exec\s+(?:[^|;&\n]+?\s)?-m\s+([a-zA-Z][a-zA-Z0-9._-]*)',
)

# ts_utc uses SQL datetime('now') so format matches the comparison bound
# `datetime('now','-14 days')` used by model_balancer._query_metrics.
# Python isoformat() emits "2026-05-12T15:30:00+00:00" which lexically
# diverges from SQLite\'s "2026-05-12 15:30:00" -- same row, different sort.
INSERT_SQL = """
INSERT INTO model_metrics
    (ts_utc, provider, model, task_category, duration_ms, num_turns,
     per_turn_ms, tokens_in, tokens_out, success, session_id, project_root)
VALUES
    (datetime(\'now\'), ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
"""


def _task_category(subagent_type: str, description: str) -> str:
    """Derive task_category from subagent_type and description."""
    desc = description.lower()
    if subagent_type == "Explore" or "explore" in desc or "recon" in desc:
        return "recon"
    if "worker" in desc or "verifier" in desc:
        return "coding"
    if subagent_type == "Plan" or "plan" in desc:
        return "hard"
    if "audit" in desc or "consilium" in desc:
        return "hard"
    return "medium"


def _find_usage(event: dict):
    """
    Try multiple paths for the usage dict in priority order.
    Returns the first dict that contains a non-None 'duration_ms', or None.

    Note: since CC v2.1.139+, top-level `duration_ms` is preferred (checked
    in handle_event before calling this function). This function is the fallback.

    Priority:
        1. event["tool_response"]["usage"]
        2. event["toolUseResult"]["usage"]
        3. event["tool_response"]["toolUseResult"]["usage"]
        4. event["usage"]
    """
    tr = event.get("tool_response") or {}
    tur = event.get("toolUseResult") or {}
    candidates = [
        tr.get("usage"),
        tur.get("usage"),
        (tr.get("toolUseResult") or {}).get("usage"),
        event.get("usage"),
    ]
    for u in candidates:
        if isinstance(u, dict) and u.get("duration_ms") is not None:
            return u
    return None


def _log_no_usage_sample(event: dict) -> None:
    """
    Write a once-per-UTC-day diagnostic sample when no usage data is found.
    Gated by a marker file ~/.claude/logs/.metric_capture_sample_YYYYMMDD.
    Never raises -- all exceptions are swallowed.
    """
    try:
        now = datetime.utcnow()
        today = now.strftime("%Y%m%d")
        marker = os.path.join(LOGS_DIR, f".metric_capture_sample_{today}")
        os.makedirs(LOGS_DIR, exist_ok=True)
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            return
        tr = event.get("tool_response") or {}
        tur = event.get("toolUseResult") or {}
        sample = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": "no_usage_sample",
            "tool_name": event.get("tool_name", ""),
            "tool_input_keys": sorted((event.get("tool_input") or {}).keys()),
            "tool_response_keys": sorted(tr.keys()),
            "toolUseResult_keys": sorted(tur.keys()),
        }
        with open(SAMPLE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sample) + "\n")
    except Exception:
        pass


def _match_codex_command(command: str):
    """
    Return the model string if command is a valid codex invocation whose model
    is in _CODEX_ALLOWLIST; return None otherwise.

    Accepted patterns (token-boundary anchored):
        codex_worker.sh <MODEL> [...]
        codex_sandbox_worker.sh <MODEL> [...]
        codex exec [...] -m <MODEL> [...]

    Rejected:
        vim codex_worker.sh, grep codex_worker.sh logs/, heredoc fragments,
        codex --help, codex auth, codex exec without -m,
        codex exec -m <model-not-in-allowlist>.
    """
    for pattern in (_RE_CODEX_WORKER, _RE_CODEX_SANDBOX_WORKER, _RE_CODEX_EXEC):
        m = pattern.search(command)
        if m:
            model = m.group(1)
            return model if model in _CODEX_ALLOWLIST else None
    return None


def _get_project_root() -> str:
    try:
        return os.getcwd()
    except OSError:
        return ""


def handle_event(event: dict) -> bool:
    """
    Process a single parsed event dict.
    Returns True if a row was inserted, False otherwise.
    """
    tool_name = event.get("tool_name", "")
    session_id = event.get("session_id", "")
    project_root = _get_project_root()

    if tool_name in ("Task", "Agent"):
        top_level_duration = event.get("duration_ms")
        usage = _find_usage(event)
        if usage is None and top_level_duration is None:
            # No usage data found in any known location -- log a daily sample
            # for diagnostics and exit cleanly without inserting a row.
            _log_no_usage_sample(event)
            return False

        duration_ms = top_level_duration if top_level_duration is not None else (usage or {}).get("duration_ms")

        usage = usage or {}
        num_turns = usage.get("num_turns", 1) or 1
        tokens_in = usage.get("input_tokens")
        tokens_out = usage.get("output_tokens")

        tool_input = event.get("tool_input") or {}
        model = tool_input.get("model") or "inherit"
        description = tool_input.get("description") or ""
        subagent_type = tool_input.get("subagent_type") or ""

        category = _task_category(subagent_type, description)
        per_turn_ms = int(duration_ms / max(num_turns, 1))

        _insert_row(PROVIDER_ANTHROPIC, model, category,
                    duration_ms, num_turns, per_turn_ms,
                    tokens_in, tokens_out, session_id, project_root)
        return True

    if tool_name == "Bash":
        tool_input = event.get("tool_input") or {}
        command = tool_input.get("command") or ""
        model = _match_codex_command(command)
        if model is not None:
            _insert_row(PROVIDER_CODEX, model, "medium",
                        None, None, None,
                        None, None, session_id, project_root)
            return True

    return False


def _insert_row(provider, model, task_category,
                duration_ms, num_turns, per_turn_ms,
                tokens_in, tokens_out, session_id, project_root):
    # isolation_level=None -> autocommit; PRAGMA synchronous=NORMAL trades
    # one fsync per commit for ~3-8ms savings per PostToolUse invocation.
    conn = sqlite3.connect(
        f"file:{DB_PATH}?synchronous=NORMAL",
        timeout=2.0, isolation_level=None, uri=True,
    )
    try:
        conn.execute(INSERT_SQL, (
            provider, model, task_category,
            duration_ms, num_turns, per_turn_ms,
            tokens_in, tokens_out,
            session_id, project_root,
        ))
    finally:
        conn.close()


def main():
    if os.environ.get("CLAUDE_BOOSTER_SKIP_METRIC_CAPTURE") == "1":
        return

    raw = sys.stdin.read()
    if not raw.strip():
        return

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return

    if not isinstance(event, dict):
        return

    try:
        handle_event(event)
    except Exception:
        pass


if __name__ == "__main__":
    main()
