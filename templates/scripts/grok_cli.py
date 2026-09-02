#!/usr/bin/env python3
"""Run Grok Build CLI as an external Booster reviewer.

Purpose
-------
Provides a deterministic read-only bridge to xAI Grok for Booster audit and
hackathon review stages, plus a fast ``status`` probe so callers can tell
"Grok is unavailable" apart from "the caller used the wrong subcommand".
Write-capable coding uses ``grok_sandbox_worker.sh`` instead, so this script
can stay review-only by default.

Contract
--------
Input  : prompt text on stdin for ``smoke``/``review``; ``status`` reads no
         stdin and takes no prompt.
Output : Grok response on stdout for ``smoke``/``review``; exactly one
         sanitized ``grok_cli: status=...`` line on stdout for ``status``.
         Diagnostics go to stderr.
Exit   : ``smoke``/``review`` return the child ``grok`` exit code; 65 for an
         empty prompt; 66 empty model; 67 empty category; 68 bad budget;
         69 for empty successful output; 124 for timeout; 127 if the binary
         is missing. ``status`` returns 0 available, 127 binary missing,
         69 auth missing or an empty smoke response, 124 smoke timeout, and
         otherwise the smoke child's own nonzero exit code.

CLI
---
    python3 ~/.claude/scripts/grok_cli.py status
    python3 ~/.claude/scripts/grok_cli.py auth-status --smoke
    printf 'Reply GROK_OK' | python3 ~/.claude/scripts/grok_cli.py smoke
    printf '<review prompt>' | python3 ~/.claude/scripts/grok_cli.py review --budget-turns 8

Limitations
-----------
- Requires Grok CLI authentication via ``grok login`` or another supported xAI
  auth mechanism.
- ``review`` denies edit/write-style tools. Use ``grok_sandbox_worker.sh`` for
  code-writing tasks.
- ``status`` without ``--smoke`` proves only that the binary and auth file
  exist; it does not prove the account can complete a turn.

ENV / Files
-----------
- Reads: Grok CLI auth/config under ``~/.grok`` (``GROK_AUTH_FILE`` overrides
  the auth path), ``GROK_BIN``, ``GROK_CLI_TIMEOUT_S``.
- Writes: ``~/.claude/rolling_memory.db`` ``model_metrics`` row, best-effort.
          ``~/.claude/logs/model_provider_failures.jsonl`` typed failure
          events, best-effort and schema-free
          (``CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG`` redirects it).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from provider_failure_log import (  # noqa: E402
    append_provider_event as _shared_append_provider_event,
)


DEFAULT_MODEL = "grok-4.5"
DEFAULT_CODER_MODEL = "grok-4.5"
PROVIDER = "grok-cli"
DEFAULT_DB_PATH = Path.home() / ".claude" / "rolling_memory.db"
DEFAULT_GROK_BIN = Path.home() / ".grok" / "bin" / "grok"
DEFAULT_GROK_AUTH_FILE = Path.home() / ".grok" / "auth.json"
DETAIL_LIMIT_BYTES = 300
PERMANENT_FAILURE_TYPES = frozenset({"auth_missing", "binary_missing"})
# CSI/OSC and single-character escapes emitted by the Grok CLI's TUI renderer.
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_MAX_TURNS_RE = re.compile(r"(?i)max\s+turns\s+reached")
# A credential-shaped value: >=16 unbroken credential characters that also carry
# a digit or mixed case. Ordinary diagnostic words ("missing_field", "expired",
# "authentication") fail one of those two conditions and survive verbatim.
_SECRET_VALUE = (
    r"(?=[A-Za-z0-9._\-+/=]*\d"
    r"|[A-Za-z0-9._\-+/=]*[a-z][A-Za-z0-9._\-+/=]*[A-Z]"
    r"|[A-Za-z0-9._\-+/=]*[A-Z][A-Za-z0-9._\-+/=]*[a-z])"
    r"[A-Za-z0-9._\-+/=]{16,}"
)
_SECRET_PATTERNS = (
    # Authorization headers and bearer tokens, including dotted JWT segments.
    re.compile(r"(?i)\b(?:bearer|authorization:?)\s+" + _SECRET_VALUE),
    # key=/token=/secret= assignments. The separator must follow the key name
    # immediately, so identifiers such as ``token_budget_exhausted`` and
    # ``key_not_found_in_cache`` are never treated as assignments.
    re.compile(
        r"(?i)\b(?:api[_-]?key|auth[_-]?token|access[_-]?token|secret|password|passwd|key|token)"
        r"[:=]\s*" + _SECRET_VALUE
    ),
    # Bare JWTs: three dot-separated base64url segments starting with ``eyJ``.
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    # Vendor-prefixed opaque credentials in their real key shape only:
    # xai-..., sk-..., gsk_..., ghp_..., glpat-..., xoxb-..., each with a long
    # opaque tail. A separator is required, so ``api_response_code`` and other
    # identifiers that merely contain a vendor word cannot match.
    re.compile(
        r"(?i)\b(?:xai|sk|gsk|ghp|gho|ghs|ghu|github_pat|glpat|npm|pat|xox[abp])[-_]"
        r"[A-Za-z0-9._\-]{16,}"
    ),
    # AWS access key ids, which carry no separator after their prefix.
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16,}"),
)
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


def _auth_file() -> Path:
    """Return the Grok CLI auth file path, allowing tests to redirect it."""
    override = os.environ.get("GROK_AUTH_FILE", "").strip()
    return Path(override).expanduser() if override else DEFAULT_GROK_AUTH_FILE


def _binary_available(binary: str) -> bool:
    """Return True when the configured Grok binary can actually be launched."""
    return shutil.which(binary) is not None or Path(binary).exists()


def _auth_available() -> bool:
    """Return True when a readable Grok auth file exists."""
    path = _auth_file()
    try:
        return path.is_file() and os.access(path, os.R_OK)
    except OSError:
        return False


def _redact(data: bytes | str) -> str:
    """Return the full text with known credential shapes replaced, no truncation."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _sanitize_detail(data: bytes | str, *, limit: int = DETAIL_LIMIT_BYTES) -> str:
    """Return a bounded single-line diagnostic tail with secrets redacted.

    ANSI escape sequences are stripped first so TUI colour codes do not consume
    the byte budget or corrupt the recorded detail. Redaction runs before
    truncation, so a credential that straddles the tail boundary cannot survive
    as a partial-but-usable fragment.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = " ".join(text.split())
    text = _redact(text)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= limit:
        return text
    return encoded[-limit:].decode("utf-8", "replace")


def _is_max_turns(stderr: bytes | str) -> bool:
    """Return True when Grok stopped because its turn budget was exhausted."""
    if not stderr:
        return False
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    return bool(_MAX_TURNS_RE.search(_ANSI_ESCAPE_RE.sub("", text)))


def _classify_failure(
    returncode: int, *, is_empty: bool, stderr: bytes | str = ""
) -> str:
    """Classify a Grok CLI failure into a stable telemetry type.

    ``max_turns`` is a non-permanent budget failure: the prompt needed more
    agent turns than ``--budget-turns`` allowed, which says nothing about Grok
    availability. It is checked before the auth fallback so an exhausted budget
    is never misreported as ``auth_missing``.
    """
    if returncode == 124:
        return "timeout"
    if returncode == 127:
        return "binary_missing"
    if returncode == 69:
        return "empty_response"
    if returncode == 0 and is_empty:
        return "empty_response"
    if _is_max_turns(stderr):
        return "max_turns"
    if not _auth_available():
        return "auth_missing"
    return "nonzero_exit"


def _record_failure_event(
    *,
    model: str,
    task_category: str,
    failure_type: str,
    returncode: int,
    duration_ms: int,
    detail: bytes | str = "",
) -> None:
    """Append one typed Grok failure event to the shared provider sidecar."""
    if not model.strip():
        raise ValueError("model must be non-empty")
    if not task_category.strip():
        raise ValueError("task_category must be non-empty")
    if not failure_type.strip():
        raise ValueError("failure_type must be non-empty")
    if duration_ms < 0:
        raise ValueError(f"duration_ms must be >= 0, got {duration_ms}")

    event: dict[str, Any] = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event_type": "failure",
        "provider": PROVIDER,
        "model": model,
        "task_category": task_category,
        "failure_type": failure_type,
        "permanent": failure_type in PERMANENT_FAILURE_TYPES,
        "returncode": int(returncode),
        "duration_ms": int(duration_ms),
        "detail": _sanitize_detail(detail) if detail else "",
    }
    _shared_append_provider_event(event, log_label="grok_cli")


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


def _grok_command(
    binary: str,
    prompt: str,
    *,
    model: str,
    budget_turns: int,
    read_only: bool,
) -> list[str]:
    """Build the single canonical Grok CLI argv used by review and status probes."""
    if not binary.strip():
        raise ValueError("binary must be non-empty")
    if not prompt.strip():
        raise ValueError("prompt must be non-empty")
    if not model.strip():
        raise ValueError("model must be non-empty")
    if budget_turns < 1:
        raise ValueError(f"budget_turns must be >= 1, got {budget_turns}")

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
    return cmd


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
    if not _binary_available(binary):
        print(f"grok_cli: grok binary not found: {binary}", file=sys.stderr)
        _record_failure_event(
            model=model,
            task_category=task_category,
            failure_type="binary_missing",
            returncode=127,
            duration_ms=0,
            detail=f"grok binary not found: {binary}",
        )
        return 127

    cmd = _grok_command(
        binary,
        prompt,
        model=model,
        budget_turns=budget_turns,
        read_only=read_only,
    )

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
        sys.stderr.write(_redact(stderr))
        sys.stderr.flush()
    is_empty = raw.decode("utf-8", "replace").strip() == ""
    if final_returncode == 0 and is_empty:
        print("grok_cli: empty response", file=sys.stderr)
        final_returncode = 69
    if final_returncode != 0:
        failure_type = _classify_failure(
            final_returncode, is_empty=is_empty, stderr=stderr
        )
        raw_detail = stderr or raw
        detail = _sanitize_detail(raw_detail)
        print(
            f"grok_cli: unavailable failure_type={failure_type} "
            f"model={model} returncode={final_returncode} detail={detail}",
            file=sys.stderr,
        )
        _record_failure_event(
            model=model,
            task_category=task_category,
            failure_type=failure_type,
            returncode=final_returncode,
            duration_ms=duration_ms,
            detail=raw_detail,
        )
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()
    _record_metric(
        model=model,
        task_category=task_category,
        duration_ms=duration_ms,
        success=final_returncode == 0 and not is_empty,
    )
    return final_returncode


def _status(*, model: str, run_smoke: bool, task_category: str) -> int:
    """Print one sanitized availability line for Grok and return its exit code."""
    if not model.strip():
        raise ValueError("model must be non-empty")
    if not task_category.strip():
        raise ValueError("task_category must be non-empty")

    binary = _grok_bin()
    if not _binary_available(binary):
        _record_failure_event(
            model=model,
            task_category=task_category,
            failure_type="binary_missing",
            returncode=127,
            duration_ms=0,
            detail=f"grok binary not found: {binary}",
        )
        print(f"grok_cli: status=unavailable reason=binary_missing model={model}")
        return 127
    if not _auth_available():
        _record_failure_event(
            model=model,
            task_category=task_category,
            failure_type="auth_missing",
            returncode=69,
            duration_ms=0,
            detail=f"grok auth file not readable: {_auth_file()}",
        )
        print(
            f"grok_cli: status=unavailable reason=auth_missing model={model} "
            f"binary={_sanitize_detail(binary)}"
        )
        return 69
    if run_smoke:
        started = time.monotonic()
        cmd = _grok_command(
            binary,
            "Reply with exactly: OK",
            model=model,
            budget_turns=1,
            read_only=True,
        )
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=DEFAULT_TIMEOUT_S,
            )
            raw, stderr, returncode = proc.stdout, proc.stderr, int(proc.returncode)
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout if isinstance(exc.stdout, bytes) else b""
            stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
            returncode = 124
        duration_ms = int((time.monotonic() - started) * 1000)
        is_empty = raw.decode("utf-8", "replace").strip() == ""
        ok = returncode == 0 and not is_empty
        _record_metric(
            model=model,
            task_category=task_category,
            duration_ms=duration_ms,
            success=ok,
        )
        if not ok:
            failure_type = _classify_failure(
                returncode if returncode != 0 else 69,
                is_empty=is_empty,
                stderr=stderr,
            )
            _record_failure_event(
                model=model,
                task_category=task_category,
                failure_type=failure_type,
                returncode=returncode,
                duration_ms=duration_ms,
                detail=stderr or raw,
            )
            if returncode == 124:
                exit_code, reason = 124, "smoke_timeout"
            elif returncode == 0 or returncode == 69:
                exit_code, reason = 69, "smoke_empty_response"
            else:
                exit_code, reason = returncode, "smoke_failed"
            print(
                f"grok_cli: status=unavailable reason={reason} model={model} "
                f"returncode={returncode}"
            )
            return exit_code
    print(
        f"grok_cli: status=available model={model} binary={_sanitize_detail(binary)}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Grok through xAI Grok CLI.")
    parser.add_argument("mode", choices=("smoke", "review", "status", "auth-status"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--budget-turns", type=int, default=8)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="status/auth-status only: also run a 1-turn live smoke check.",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="model_metrics task_category; defaults to audit_tertiary for review and grok_smoke for smoke.",
    )
    args = parser.parse_args()

    if args.mode in {"status", "auth-status"}:
        return _status(
            model=args.model,
            run_smoke=args.smoke,
            task_category=args.category or "grok_status",
        )

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
