#!/usr/bin/env python3
"""Capability-aware Codex worker boundary.

Purpose: Run one Codex text worker and, only for Booster balancer-selected Sol
routes, retry once with the known-working GPT-5.5 model after the canonical
ChatGPT-account entitlement error.
Contract: stdin is forwarded byte-for-byte to each attempt; stdout is only the
effective Codex stdout; stderr retains Codex diagnostics plus one sanitized
routing-provenance JSON line. Explicit model selections never downgrade.
CLI: ``codex_worker.py MODEL [codex exec args...]``.
Limitations: only the exact 400 invalid_request_error entitlement response is
classified; the negative capability cache is deliberately bounded by TTL.
ENV/Files: CODEX_BIN, CODEX_REASONING_EFFORT,
CLAUDE_BOOSTER_TASK_CATEGORY, CLAUDE_BOOSTER_ROUTE_SOURCE,
CLAUDE_BOOSTER_CODEX_CAPABILITY_CACHE, CLAUDE_BOOSTER_CODEX_CAPABILITY_TTL;
writes a versioned mode-0600 cache under ``~/.claude/state`` by default.
"""

from __future__ import annotations

import json
import os
import fcntl
import subprocess
import sys
import tempfile
import time
import stat
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SOL = "gpt-5.6-sol"
FALLBACK = "gpt-5.5"
MANAGED_CATEGORIES = frozenset({"hard", "lead", "consilium_bio"})
MANAGED_SOURCES = frozenset({"balancer", "policy"})
DEFAULT_TTL = 24 * 60 * 60
MAX_CACHE_BYTES = 16 * 1024
LOCK_WAIT_SECONDS = 240


def _cache_path() -> Path:
    override = os.environ.get("CLAUDE_BOOSTER_CODEX_CAPABILITY_CACHE")
    return Path(override).expanduser() if override else Path.home() / ".claude/state/codex_capabilities.json"


def _ttl() -> int:
    try:
        value = int(os.environ.get("CLAUDE_BOOSTER_CODEX_CAPABILITY_TTL", str(DEFAULT_TTL)))
    except ValueError:
        return DEFAULT_TTL
    return value if 60 <= value <= 7 * 24 * 60 * 60 else DEFAULT_TTL


def _managed(model: str) -> tuple[bool, str, str]:
    category = os.environ.get("CLAUDE_BOOSTER_TASK_CATEGORY", "")
    source = os.environ.get("CLAUDE_BOOSTER_ROUTE_SOURCE", "")
    return model == SOL and category in MANAGED_CATEGORIES and source in MANAGED_SOURCES, category, source


def _read_cache(path: Path, now: int) -> tuple[bool, int | None]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            return False, None
        if info.st_size > MAX_CACHE_BYTES:
            return False, None
        data = json.loads(path.read_text(encoding="utf-8"))
        if set(data) != {"schema_version", "model", "reason", "observed_at", "expires_at"}:
            return False, None
        if data["schema_version"] != SCHEMA_VERSION or data["model"] != SOL or data["reason"] != "chatgpt_account_unsupported":
            return False, None
        observed, expires = data["observed_at"], data["expires_at"]
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (observed, expires)):
            return False, None
        return now < expires and observed <= now, max(0, now - observed)
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return False, None


def _write_cache(path: Path, now: int) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "model": SOL,
        "reason": "chatgpt_account_unsupported",
        "observed_at": now,
        "expires_at": now + _ttl(),
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _canonical_unsupported(stderr: bytes, requested: str, returncode: int) -> bool:
    if returncode == 0 or requested != SOL:
        return False
    text = stderr.decode("utf-8", errors="replace")
    for line in text.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            payload = json.loads(line[start:])
        except (json.JSONDecodeError, TypeError):
            continue
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            continue
        message = error.get("message")
        expected = f"The '{requested}' model is not supported when using Codex with a ChatGPT account."
        normalized = " ".join(message.split()) if isinstance(message, str) else None
        if (
            payload.get("status") == 400
            and error.get("type") == "invalid_request_error"
            and normalized == expected
        ):
            return True
    return False


def _run(model: str, extra: list[str], prompt: bytes) -> tuple[subprocess.CompletedProcess[bytes], int]:
    binary = os.environ.get("CODEX_BIN", "/opt/homebrew/bin/codex")
    effort = os.environ.get("CODEX_REASONING_EFFORT", "medium")
    if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        raise FileNotFoundError(binary)
    started = time.monotonic_ns()
    result = subprocess.run(
        [binary, "exec", "-c", f'model_reasoning_effort="{effort}"', "--skip-git-repo-check", "-m", model, *extra, "-"],
        input=prompt,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result, max(0, (time.monotonic_ns() - started) // 1_000_000)


def _acquire_probe_lock(path: Path) -> Any:
    """Acquire a bounded advisory lock whose ownership is the open descriptor."""
    lock = path.with_name(path.name + ".probe-lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock, flags, 0o600)
    handle = os.fdopen(fd, "r+b", buffering=0)
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        handle.close()
        raise PermissionError(f"unsafe capability probe lock: {lock}")
    os.fchmod(handle.fileno(), 0o600)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError("timed out waiting for capability probe lock")
            time.sleep(0.05)


def _release_probe_lock(lock: Any | None) -> None:
    if lock is not None:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def _provenance(requested: str, effective: str, reason: str, source: str, category: str, age: int | None, attempts: list[dict[str, Any]]) -> bytes:
    row: dict[str, Any] = {
        "event": "codex_route",
        "requested_model": requested,
        "effective_model": effective,
        "reason": reason,
        "source": source or "explicit",
        "category": category or "unclassified",
        "cache_age_seconds": age,
        "attempts": attempts,
    }
    return ("codex_worker: " + json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: codex_worker.py <MODEL> [extra args...]", file=sys.stderr)
        return 2
    requested, extra = argv[0], argv[1:]
    managed, category, source = _managed(requested)
    prompt = sys.stdin.buffer.read()
    now = int(time.time())
    cache_path = _cache_path()
    cached, age = _read_cache(cache_path, now) if managed else (False, None)
    lock = None
    if managed and not cached:
        cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            lock = _acquire_probe_lock(cache_path)
        except TimeoutError as exc:
            print(f"codex_worker.py: {exc}", file=sys.stderr)
            return 75
        cached, age = _read_cache(cache_path, int(time.time()))
    effective = FALLBACK if cached else requested
    reason = "cached_chatgpt_account_unsupported" if cached else "requested"
    attempts: list[dict[str, Any]] = []
    try:
        result, duration = _run(effective, extra, prompt)
    except FileNotFoundError as exc:
        _release_probe_lock(lock)
        print(f"codex_worker.py: codex binary not found at {exc.args[0]}", file=sys.stderr)
        return 127
    attempts.append({"model": effective, "success": result.returncode == 0, "duration_ms": duration})
    diagnostics = result.stderr
    if managed and not cached and _canonical_unsupported(result.stderr, requested, result.returncode):
        _write_cache(cache_path, now)
        _release_probe_lock(lock)
        lock = None
        effective, reason, age = FALLBACK, "observed_chatgpt_account_unsupported", 0
        result, duration = _run(effective, extra, prompt)
        attempts.append({"model": effective, "success": result.returncode == 0, "duration_ms": duration})
        diagnostics += result.stderr
    _release_probe_lock(lock)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(diagnostics)
    sys.stderr.buffer.write(_provenance(requested, effective, reason, source, category, age, attempts))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
