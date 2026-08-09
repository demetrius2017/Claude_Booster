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
         Timeout returns 124. Local contract failures return 65..67.

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
          ``~/.claude/logs/model_provider_failures.jsonl`` for typed
          provider/account/config failures, best-effort and schema-free.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = "https://api.z.ai/api/anthropic"
PREFLIGHT_URL = f"{BASE_URL}/v1/messages"
DEFAULT_MODEL = "glm-5.2"
DEFAULT_AIR_MODEL = "glm-5.2-air"
PROVIDER = "zai-cli"
DEFAULT_DB_PATH = Path.home() / ".claude" / "rolling_memory.db"
DEFAULT_SECRET_PATH = Path.home() / ".claude" / "secrets" / "zai_api_key"
DEFAULT_FAILURE_LOG_PATH = Path.home() / ".claude" / "logs" / "model_provider_failures.jsonl"
DEFAULT_FAILURE_LOG_MAX_BYTES = 256 * 1024
DEFAULT_FAILURE_LOG_RETAIN_LINES = 1000
INSERT_METRIC_SQL = """
INSERT INTO model_metrics
    (ts_utc, provider, model, task_category, duration_ms, num_turns,
     per_turn_ms, tokens_in, tokens_out, success, session_id, project_root)
VALUES
    (datetime('now'), ?, ?, ?, ?, 1, ?, NULL, NULL, ?, ?, ?)
"""
try:
    DEFAULT_TIMEOUT_S = float(os.environ.get("ZAI_CLI_TIMEOUT_S", "90"))
except (TypeError, ValueError):
    DEFAULT_TIMEOUT_S = 90.0
try:
    DEFAULT_PREFLIGHT_TIMEOUT_S = float(os.environ.get("ZAI_PREFLIGHT_TIMEOUT_S", "8"))
except (TypeError, ValueError):
    DEFAULT_PREFLIGHT_TIMEOUT_S = 8.0
PERMANENT_FAILURE_TYPES = frozenset(
    {"invalid_model", "insufficient_balance", "auth_error", "account_error"}
)
_PROVIDER_EVENT_THREAD_LOCK = threading.Lock()


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


def _failure_log_path() -> Path:
    """Return the schema-free provider failure log path."""
    override = os.environ.get("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", "").strip()
    return Path(override).expanduser() if override else DEFAULT_FAILURE_LOG_PATH


def _sanitize_excerpt(data: bytes, *, limit: int = 500) -> str:
    """Return a bounded printable diagnostic without leaking obvious secrets."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    text = data.decode("utf-8", "replace")
    for api_key in {os.environ.get("ZAI_API_KEY", ""), _api_key()}:
        if api_key:
            text = text.replace(api_key, "[redacted]")
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _sanitize_text(text: str, *, limit: int = 500) -> str:
    """Return a bounded printable diagnostic without leaking obvious secrets."""
    return _sanitize_excerpt(text.encode("utf-8", "replace"), limit=limit)


def _classify_failure(
    returncode: int,
    stderr: bytes,
    stdout: bytes,
    *,
    timed_out: bool = False,
) -> str:
    """Classify a Z.ai/Claude failure into a stable telemetry type."""
    if timed_out:
        return "timeout"
    blob = (stderr + b"\n" + stdout).decode("utf-8", "replace").casefold()
    if "invalid model" in blob or "invalid_model" in blob or ("400" in blob and "model" in blob):
        return "invalid_model"
    if "insufficient balance" in blob or "insufficient_balance" in blob or "credit_balance_exhausted" in blob:
        return "insufficient_balance"
    if "401" in blob or "403" in blob or "unauthorized" in blob or "forbidden" in blob:
        return "auth_error"
    if "account" in blob and ("disabled" in blob or "suspended" in blob):
        return "account_error"
    if "429" in blob:
        return "rate_limited"
    if "529" in blob or "overloaded" in blob or "temporarily unavailable" in blob:
        return "backend_transient"
    if returncode == 0 and stdout.decode("utf-8", "replace").strip() == "":
        return "empty_output"
    return "nonzero_exit"


def _event_bounds() -> tuple[int, int]:
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


def _append_provider_event(event: dict[str, Any]) -> None:
    """Append one provider event under an exclusive Unix file lock."""
    path = _failure_log_path()
    max_bytes, retain_lines = _event_bounds()
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
            print(f"zai_cli: provider-event telemetry skipped: {exc}", file=sys.stderr)


def _record_provider_event(
    *,
    event_type: str,
    model: str,
    task_category: str,
    failure_type: str | None = None,
    returncode: int = 0,
    duration_ms: int,
    detail: str = "",
) -> None:
    """Append typed provider telemetry without a DB schema migration."""
    if event_type not in {"failure", "success", "recovery"}:
        raise ValueError(f"unexpected event_type: {event_type!r}")
    if not model.strip():
        raise ValueError("model must be non-empty")
    if not task_category.strip():
        raise ValueError("task_category must be non-empty")
    if event_type == "failure" and not (failure_type or "").strip():
        raise ValueError("failure_type must be non-empty for failure events")
    if duration_ms < 0:
        raise ValueError(f"duration_ms must be >= 0, got {duration_ms}")

    event = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event_type": event_type,
        "provider": PROVIDER,
        "model": model,
        "task_category": task_category,
        "failure_type": failure_type,
        "permanent": failure_type in PERMANENT_FAILURE_TYPES if failure_type else False,
        "returncode": int(returncode),
        "duration_ms": int(duration_ms),
        "detail": _sanitize_text(detail) if detail else "",
    }
    _append_provider_event(event)


def _record_failure_event(**kwargs: Any) -> None:
    """Record a typed provider failure event."""
    _record_provider_event(event_type="failure", **kwargs)


def _record_success_event(*, model: str, task_category: str, duration_ms: int) -> None:
    """Record a successful provider contact that can supersede old failures."""
    _record_provider_event(
        event_type="success",
        model=model,
        task_category=task_category,
        duration_ms=duration_ms,
        detail="provider contact ok",
    )


def _preflight_disabled() -> bool:
    """Return True when tests explicitly disable direct network preflight."""
    return os.environ.get("ZAI_PREFLIGHT_DISABLE", "0") == "1"


def _extract_error_detail(status: int, body: bytes) -> str:
    """Extract a bounded provider diagnostic from an Anthropic-compatible error."""
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return f"HTTP {status} {_sanitize_excerpt(body)}"
    error = parsed.get("error", parsed) if isinstance(parsed, dict) else parsed
    if isinstance(error, dict):
        parts = [
            f"HTTP {status}",
            str(error.get("type", "")),
            str(error.get("code", "")),
            str(error.get("message", "")),
        ]
        return _sanitize_text(" ".join(part for part in parts if part.strip()))
    return _sanitize_text(f"HTTP {status} {error}")


def _classify_preflight_failure(status: int | None, detail: str, *, timed_out: bool = False) -> str:
    """Classify direct Z.ai preflight failures without relying on Claude CLI."""
    if timed_out:
        return "timeout"
    blob = detail.casefold()
    if "invalid model" in blob or "invalid_model" in blob or (status == 400 and "model" in blob):
        return "invalid_model"
    if "insufficient balance" in blob or "insufficient_balance" in blob or "credit_balance_exhausted" in blob:
        return "insufficient_balance"
    if status in {401, 403} or "unauthorized" in blob or "forbidden" in blob:
        return "auth_error"
    if "account" in blob and ("disabled" in blob or "suspended" in blob):
        return "account_error"
    if status == 429:
        return "rate_limited"
    if status is not None and 500 <= status <= 599:
        return "backend_transient"
    return "transient"


def _direct_preflight(key: str, *, model: str, timeout_s: float) -> tuple[bool, str | None, str, int]:
    """Call Z.ai directly once; return (ok, failure_type, detail, http_status)."""
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Reply with OK."}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        PREFLIGHT_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "authorization": f"Bearer {key}",
            "x-api-key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp.read(4096)
        return True, None, "preflight ok", 0
    except TimeoutError:
        return False, "timeout", "preflight timeout", 0
    except urllib.error.HTTPError as exc:
        detail = _extract_error_detail(exc.code, exc.read(4096))
        return False, _classify_preflight_failure(exc.code, detail), detail, int(exc.code)
    except urllib.error.URLError as exc:
        reason = _sanitize_text(str(exc.reason))
        timed_out = "timed out" in reason.casefold() or isinstance(exc.reason, TimeoutError)
        failure_type = _classify_preflight_failure(None, reason, timed_out=timed_out)
        return False, failure_type, reason, 0
    except OSError as exc:
        detail = _sanitize_text(str(exc))
        return False, _classify_preflight_failure(None, detail), detail, 0


def _preflight_or_record(*, model: str, task_category: str) -> int | None:
    """Return an exit code when Z.ai is unavailable; otherwise None."""
    if _preflight_disabled():
        return None
    key = _api_key()
    if not key:
        print(
            "zai_cli: missing ZAI_API_KEY; export it or create ~/.claude/secrets/zai_api_key.",
            file=sys.stderr,
        )
        raise SystemExit(64)
    started = time.monotonic()
    ok, failure_type, detail, status = _direct_preflight(
        key,
        model=model,
        timeout_s=DEFAULT_PREFLIGHT_TIMEOUT_S,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    if ok:
        return None

    failure = failure_type or "transient"
    sanitized = _sanitize_text(detail)
    print(
        f"zai_cli: preflight unavailable failure_type={failure} "
        f"model={model} http_status={status} detail={sanitized}",
        file=sys.stderr,
    )
    _record_failure_event(
        model=model,
        task_category=task_category,
        failure_type=failure,
        returncode=status or 75,
        duration_ms=duration_ms,
        detail=sanitized,
    )
    _record_metric(
        model=model,
        task_category=task_category,
        duration_ms=duration_ms,
        success=False,
    )
    if failure == "timeout":
        return 124
    return 75


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

    preflight_exit = _preflight_or_record(model=model, task_category=task_category)
    if preflight_exit is not None:
        return preflight_exit

    started = time.monotonic()
    env = _env()
    stderr = b""
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=DEFAULT_TIMEOUT_S,
            check=False,
        )
        raw = proc.stdout
        stderr = proc.stderr
        final_returncode = int(proc.returncode)
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout if isinstance(exc.stdout, bytes) else b""
        stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
        timed_out = True
        final_returncode = 124

    duration_ms = int((time.monotonic() - started) * 1000)
    is_empty = raw.decode("utf-8", "replace").strip() == ""
    if stderr:
        print(f"zai_cli: backend stderr: {_sanitize_excerpt(stderr)}", file=sys.stderr)
    if final_returncode == 0 and is_empty:
        print("zai_cli: empty response", file=sys.stderr)
        final_returncode = 1
    if timed_out or final_returncode != 0:
        failure_type = _classify_failure(final_returncode, stderr, raw, timed_out=timed_out)
        detail = _sanitize_excerpt(stderr or raw)
        print(
            f"zai_cli: unavailable failure_type={failure_type} "
            f"model={model} returncode={final_returncode} detail={detail}",
            file=sys.stderr,
        )
        _record_failure_event(
            model=model,
            task_category=task_category,
            failure_type=failure_type,
            returncode=final_returncode,
            duration_ms=duration_ms,
            detail=detail,
        )
    elif final_returncode == 0 and not is_empty:
        _record_success_event(
            model=model,
            task_category=task_category,
            duration_ms=duration_ms,
        )

    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()
    try:
        _record_metric(
            model=model,
            task_category=task_category,
            duration_ms=duration_ms,
            success=final_returncode == 0 and not is_empty,
        )
    except OSError as exc:
        print(f"zai_cli: telemetry skipped: {exc}", file=sys.stderr)
    return final_returncode


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
