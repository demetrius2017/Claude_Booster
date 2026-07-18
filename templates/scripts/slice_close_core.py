"""Verification runner and receipt primitives for Slice 3A.

Purpose: Execute one argv-only verifier and bind its evidence to immutable Git
attribution facts without granting closure or commit authority.
Contract: Evidence is strict/bounded, subprocesses never use a shell, output is
bounded but fully hashed, and PASS requires exit zero plus unchanged state.
CLI/Examples: Library for slice_close.py; no standalone CLI.
Limitations: No dispositions, commits, quarantine, backlog, handoff, telemetry,
hooks, or integration. Pre/post snapshots cannot observe a verifier that mutates
bytes and restores them exactly before exit; command provenance records this
boundary but does not claim prevention. Verifier output remains a claim.
ENV/Files: Uses the caller environment for executable lookup and explicit
evidence/state paths; writes nothing itself.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

MAX_EVIDENCE = 32 * 1024
MAX_OUTPUT = 16 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
EVIDENCE_KEYS = {"schema_version", "argv", "timeout_seconds"}


class VerifyError(Exception):
    """Typed verification failure carrying a stable CLI exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def read_secure_json(path: Path, *, max_bytes: int, expected_keys: set[str]) -> dict[str, Any]:
    if path.is_symlink() or not path.exists():
        raise VerifyError("evidence file missing or symlinked", 2)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > max_bytes:
        raise VerifyError("evidence file must be regular, single-link, and bounded", 2)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened, current = os.fstat(fd), os.stat(path, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise VerifyError("evidence inode/path mismatch", 4)
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifyError("invalid evidence JSON", 2) from exc
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise VerifyError("evidence schema mismatch", 2)
    return value


def validate_evidence(value: dict[str, Any]) -> dict[str, Any]:
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        raise VerifyError("unsupported evidence schema", 2)
    argv = value["argv"]
    timeout = value["timeout_seconds"]
    if not isinstance(argv, list) or not argv or len(argv) > 64 or any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
        raise VerifyError("argv must be a bounded non-empty string array", 2)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
        raise VerifyError("timeout_seconds must be in [1,300]", 2)
    return value


def resolve_executable(root: Path, argv0: str) -> str:
    if "/" in argv0:
        candidate = Path(argv0)
        candidate = candidate if candidate.is_absolute() else root / candidate
        resolved = candidate.resolve(strict=True)
        if not candidate.is_absolute() and root.resolve() not in resolved.parents:
            raise VerifyError("relative verifier escapes project root", 2)
        executable = str(resolved)
    else:
        found = shutil.which(argv0)
        if not found:
            raise VerifyError("verifier executable not found", 2)
        executable = str(Path(found).resolve(strict=True))
    if not os.access(executable, os.X_OK) or not stat.S_ISREG(os.stat(executable).st_mode):
        raise VerifyError("verifier is not an executable regular file", 2)
    return executable


def _output(stream: Any) -> dict[str, Any]:
    stream.seek(0)
    digest, total, retained = hashlib.sha256(), 0, bytearray()
    while chunk := stream.read(65536):
        digest.update(chunk)
        total += len(chunk)
        if len(retained) < MAX_OUTPUT:
            retained.extend(chunk[:MAX_OUTPUT - len(retained)])
    return {"bytes": total, "sha256": digest.hexdigest(), "content": bytes(retained).decode("utf-8", "replace"), "truncated": total > MAX_OUTPUT, "limit_exceeded": total > MAX_OUTPUT_BYTES}


def _executable_identity(path: str) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened, current = os.fstat(fd), os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise VerifyError("executable inode/path mismatch", 4)
        digest = hashlib.sha256()
        while chunk := os.read(fd, 65536):
            digest.update(chunk)
        return {"dev": opened.st_dev, "ino": opened.st_ino, "mode": stat.S_IMODE(opened.st_mode), "size": opened.st_size, "sha256": digest.hexdigest()}
    finally:
        os.close(fd)


def _group_members(pgid: int) -> list[int]:
    """Return extant members without signaling a potentially recycled group id."""
    result = subprocess.run(["/bin/ps", "-axo", "pid=,pgid="], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, check=False)
    members: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pid_text, group_text = line.split()
            if int(group_text) == pgid:
                members.append(int(pid_text))
        except (ValueError, IndexError):
            continue
    return members


def run_verifier(root: Path, evidence: dict[str, Any], state_reader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    pre = state_reader()
    executable = resolve_executable(root, evidence["argv"][0])
    identity_before = _executable_identity(executable)
    argv = [executable, *evidence["argv"][1:]]
    started = now()
    timed_out = False
    clean_env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"}
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(argv, cwd=root, stdout=stdout, stderr=stderr, shell=False, env=clean_env, start_new_session=True)
        try:
            process.wait(timeout=evidence["timeout_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        for descendant in _group_members(process.pid):
            try:
                os.kill(descendant, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 0.5
        while _group_members(process.pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        for descendant in _group_members(process.pid):
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
        stdout_fact, stderr_fact = _output(stdout), _output(stderr)
    ended = now()
    try:
        identity_after = _executable_identity(executable)
    except (OSError, VerifyError):
        identity_after = None
    post = state_reader()
    unchanged = pre["state_sha256"] == post["state_sha256"]
    executable_unchanged = identity_before == identity_after
    output_bounded = not stdout_fact["limit_exceeded"] and not stderr_fact["limit_exceeded"]
    status_value = "pass" if process.returncode == 0 and not timed_out and unchanged and executable_unchanged and output_bounded else "fail"
    return {
        "schema_version": 1, "status": status_value,
        "limitations": {"observation_model": "pre_post_snapshot", "transient_mutation_detection": False, "external_side_effect_detection": False, "future_stability": False},
        "facts": {"pre_state_sha256": pre["state_sha256"], "post_state_sha256": post["state_sha256"], "state_unchanged": unchanged},
        "claim": {"argv": evidence["argv"], "resolved_executable": executable, "executable_before": identity_before, "executable_after": identity_after, "started_at": started, "ended_at": ended, "exit_code": process.returncode, "timed_out": timed_out, "stdout": stdout_fact, "stderr": stderr_fact, "environment_keys": sorted(clean_env)},
        "attribution": pre,
    }
