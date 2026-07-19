"""Fail-closed discovery for a Codex root-session calibration bootstrap.

Purpose: Resolve exactly one immutable Codex root transcript for a project.
Contract: Explicit inputs win; ambient discovery accepts only a unique root
session matching ``CODEX_THREAD_ID`` and the resolved project root.
CLI/Examples: Used by ``slice_calibration.py bootstrap``.
Limitations: Understands the documented ``$CODEX_HOME/sessions`` JSONL store;
it never reads authentication material or chooses a newest transcript.
ENV/Files: Reads ``CODEX_THREAD_ID``, optional ``CODEX_HOME``, and JSONL files.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from slice_calibration_core import CalibrationError
from slice_telemetry_core import CodexIdentityError, secure_jsonl, validate_session_meta

BINDING_KEYS = {"schema_version", "run_id", "session_id", "transcript", "transcript_dev", "transcript_ino", "transcript_path_hash", "session_meta_sha256", "session_id_hash", "thread_id_hash", "project_hash"}


def _trusted_directory(root: Path, parts: tuple[str, ...], *, create: bool) -> tuple[int, tuple[tuple[int, int], ...]]:
    """Open a root-contained, owner-controlled directory chain without links."""
    canonical_root = root.resolve(strict=True)
    if root.resolve(strict=True) != canonical_root or any(part in {"", ".", ".."} or "/" in part for part in parts):
        raise CalibrationError("state path escapes canonical project root", 4)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(canonical_root, flags)
    opened: list[int] = [fd]
    identities: list[tuple[int, int]] = []
    try:
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise CalibrationError("trusted state directory missing", 4)
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
                os.fchmod(child, 0o700)
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022 or (hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
                os.close(child)
                raise CalibrationError("state directory ownership/type/mode invalid", 4)
            identities.append((info.st_dev, info.st_ino)); opened.append(child); fd = child
        result = os.dup(fd)
        return result, tuple(identities)
    except OSError as exc:
        raise CalibrationError(f"unsafe state directory chain: {exc}", 4) from exc
    finally:
        for item in reversed(opened):
            os.close(item)


def _recheck_directory(root: Path, parts: tuple[str, ...], identities: tuple[tuple[int, int], ...]) -> None:
    fd, current = _trusted_directory(root, parts, create=False)
    os.close(fd)
    if current != identities:
        raise CalibrationError("state directory chain replaced during operation", 4)


def secure_binding_write(root: Path, run_id: str, value: dict[str, Any]) -> Path:
    """Atomically create/replace a mode-0600 binding beneath trusted dirfds."""
    run_hash = hashlib.sha256(run_id.encode()).hexdigest()
    parts = (".claude", "state", "runs", run_hash)
    directory_fd, identities = _trusted_directory(root, parts, create=True)
    temporary = f".slice-binding-{secrets.token_hex(16)}"
    installed = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
        try:
            payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
            pending = memoryview(payload)
            while pending:
                written = os.write(fd, pending)
                if written <= 0: raise OSError("short binding write")
                pending = pending[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        _recheck_directory(root, parts, identities)
        os.replace(temporary, "slice_session_binding.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        installed = True
        os.fsync(directory_fd)
        _recheck_directory(root, parts, identities)
    except Exception:
        try: os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError: pass
        if installed:
            try: os.unlink("slice_session_binding.json", dir_fd=directory_fd)
            except FileNotFoundError: pass
        raise
    finally:
        os.close(directory_fd)
    return root.resolve(strict=True).joinpath(*parts, "slice_session_binding.json")


def secure_binding_read(root: Path, run_id: str, *, hashed: bool = False, optional: bool = False) -> dict[str, Any] | None:
    """Read one binding via a trusted chain and require protected metadata."""
    run_hash = run_id if hashed else hashlib.sha256(run_id.encode()).hexdigest()
    if len(run_hash) != 64 or any(char not in "0123456789abcdef" for char in run_hash):
        raise CalibrationError("binding run identity invalid", 4)
    parts = (".claude", "state", "runs", run_hash)
    directory_fd, identities = _trusted_directory(root, parts, create=False)
    try:
        try:
            fd = os.open("slice_session_binding.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        except FileNotFoundError:
            if optional: return None
            raise CalibrationError("session binding missing", 4)
        except OSError as exc:
            raise CalibrationError(f"unsafe session binding: {exc}", 4) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or (hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
                raise CalibrationError("session binding must be owner-owned regular mode 0600 with one link", 4)
            if info.st_size > 32 * 1024: raise CalibrationError("session binding too large", 4)
            raw = b""
            while True:
                chunk = os.read(fd, 8192)
                if not chunk: break
                raw += chunk
        finally:
            os.close(fd)
        _recheck_directory(root, parts, identities)
        try: value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise CalibrationError("session binding JSON invalid", 4) from exc
        if not isinstance(value, dict) or set(value) != BINDING_KEYS: raise CalibrationError("session binding schema mismatch", 4)
        return value
    finally:
        os.close(directory_fd)


def resolve_binding_reference(root: Path, reference: str) -> dict[str, Any]:
    """Resolve one canonical project-relative protected binding reference.

    The reference is deliberately a capability-like path: callers do not need
    to copy the raw run, session, or transcript identities into argv.  Only the
    exact path emitted by ``bootstrap`` is accepted; all filesystem access is
    still performed by :func:`secure_binding_read` through trusted dirfds.
    """
    if not isinstance(reference, str) or not reference:
        raise CalibrationError("binding reference required", 2)
    candidate = Path(reference)
    parts = candidate.parts
    if candidate.is_absolute() or len(parts) != 5 or parts[:3] != (".claude", "state", "runs") or parts[4] != "slice_session_binding.json":
        raise CalibrationError("binding must be canonical project-relative .claude/state/runs/<hash>/slice_session_binding.json", 2)
    run_hash = parts[3]
    value = secure_binding_read(root, run_hash, hashed=True)
    if value is None:  # Defensive: non-optional reads must never return None.
        raise CalibrationError("session binding missing", 4)
    if hashlib.sha256(value["run_id"].encode()).hexdigest() != run_hash:
        raise CalibrationError("binding path/run identity mismatch", 4)
    return validate_binding(root, value)


def secure_binding_delete(root: Path, run_id: str) -> None:
    """Remove a bootstrap binding without resolving attacker-controlled ancestors."""
    run_hash = hashlib.sha256(run_id.encode()).hexdigest()
    parts = (".claude", "state", "runs", run_hash)
    directory_fd, identities = _trusted_directory(root, parts, create=False)
    try:
        _recheck_directory(root, parts, identities)
        try: os.unlink("slice_session_binding.json", dir_fd=directory_fd)
        except FileNotFoundError: pass
        os.fsync(directory_fd)
        _recheck_directory(root, parts, identities)
    finally:
        os.close(directory_fd)


def secure_state_log_read(root: Path, name: str) -> bytes:
    """Read a protected state log, rejecting links and unsafe metadata."""
    if not name or "/" in name or name in {".", ".."}: raise CalibrationError("state log name invalid", 4)
    parts = (".claude", "state")
    directory_fd, identities = _trusted_directory(root, parts, create=True)
    try:
        try: fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        except FileNotFoundError: return b""
        except OSError as exc: raise CalibrationError(f"unsafe state log: {exc}", 4) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or (hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
                raise CalibrationError("state log must be owner-owned regular mode 0600 with one link", 4)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk: break
                chunks.append(chunk)
        finally: os.close(fd)
        _recheck_directory(root, parts, identities)
        return b"".join(chunks)
    finally: os.close(directory_fd)


def secure_state_log_append(root: Path, name: str, expected: bytes, payload: bytes) -> None:
    """Append only when the protected leaf still equals the validated snapshot."""
    if not payload: raise CalibrationError("empty state log append rejected", 4)
    parts = (".claude", "state")
    directory_fd, identities = _trusted_directory(root, parts, create=True)
    created = False
    try:
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try: fd = os.open(name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            if expected: raise CalibrationError("state log disappeared before append", 4)
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd); os.fchmod(fd, 0o600); created = True
        except OSError as exc: raise CalibrationError(f"unsafe state log: {exc}", 4) from exc
        original_size = 0
        try:
            info = os.fstat(fd); original_size = info.st_size
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or (hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
                raise CalibrationError("state log must be owner-owned regular mode 0600 with one link", 4)
            os.lseek(fd, 0, os.SEEK_SET); current = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk: break
                current += chunk
            if current != expected: raise CalibrationError("state log changed before append", 4)
            _recheck_directory(root, parts, identities)
            pending = memoryview(payload)
            while pending:
                written = os.write(fd, pending)
                if written <= 0: raise OSError("short state log append")
                pending = pending[written:]
            os.fsync(fd)
            _recheck_directory(root, parts, identities)
        except Exception:
            try: os.ftruncate(fd, original_size); os.fsync(fd)
            except OSError: pass
            raise
        finally: os.close(fd)
    except Exception:
        if created:
            try: os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError: pass
        raise
    finally: os.close(directory_fd)


def _candidate(path: Path, root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        rows, facts = secure_jsonl(path)
        payload = validate_session_meta(rows[0] if rows else None, root, require_root=True)
    except CodexIdentityError as exc:
        raise CalibrationError(str(exc), exc.code) from exc
    return payload, facts


def resolve_root_transcript(
    root: Path,
    transcript: str | None,
    session_id: str | None,
    environ: dict[str, str] | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any], str]:
    """Return one root transcript, its metadata/facts, and resolution source."""
    env = dict(os.environ if environ is None else environ)
    if transcript:
        source = Path(transcript).expanduser()
        if source.is_symlink():
            raise CalibrationError("transcript symlink rejected", 4)
        path = source.resolve()
        payload, facts = _candidate(path, root)
        if session_id is not None and payload["session_id"] != session_id:
            raise CalibrationError("activation root session identity mismatch", 3)
        return path, payload, facts, "explicit"

    thread_id = env.get("CODEX_THREAD_ID")
    if not thread_id:
        raise CalibrationError("root transcript discovery requires CODEX_THREAD_ID or --transcript", 2)
    codex_home = Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    store = codex_home / "sessions"
    candidates: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    if store.is_dir() and not store.is_symlink():
        for path in store.glob("*/*/*/*.jsonl"):
            try:
                payload, facts = _candidate(path, root)
            except (CalibrationError, OSError):
                continue
            if payload["id"] == thread_id and (session_id is None or payload["session_id"] == session_id):
                candidates.append((path.resolve(), payload, facts))
    if len(candidates) != 1:
        metadata = [
            {"name": path.name, "session_id_hash_prefix": __import__("hashlib").sha256(payload["session_id"].encode()).hexdigest()[:12]}
            for path, payload, _ in candidates[:20]
        ]
        raise CalibrationError(f"root transcript discovery ambiguous: count={len(candidates)} candidates={metadata}", 3)
    path, payload, facts = candidates[0]
    return path, payload, facts, "codex_thread_id"


def binding_value(root: Path, run_id: str, path: Path, payload: dict[str, Any], facts: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Build the protected local reference for an append-only transcript."""
    dev, ino, _, _ = facts["stat"]
    return {
        "schema_version": 1, "run_id": run_id, "session_id": payload["session_id"],
        "transcript": str(path), "transcript_dev": dev, "transcript_ino": ino,
        "transcript_path_hash": hashlib.sha256(str(path).encode()).hexdigest(),
        "session_meta_sha256": hashlib.sha256(json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
        "session_id_hash": hashlib.sha256(payload["session_id"].encode()).hexdigest(),
        "thread_id_hash": hashlib.sha256(payload["id"].encode()).hexdigest(),
        "project_hash": hashlib.sha256(str(root.resolve()).encode()).hexdigest(),
    }


def validate_binding(root: Path, value: Any, *, run_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
    """Re-read and validate the immutable transcript prefix and file identity."""
    if not isinstance(value, dict) or set(value) != BINDING_KEYS or value.get("schema_version") != 1:
        raise CalibrationError("session binding schema mismatch", 4)
    strings = ("run_id", "session_id", "transcript", "transcript_path_hash", "session_meta_sha256", "session_id_hash", "thread_id_hash", "project_hash")
    if any(not isinstance(value.get(name), str) or not value[name] for name in strings) or any(not isinstance(value.get(name), int) or isinstance(value[name], bool) or value[name] < 0 for name in ("transcript_dev", "transcript_ino")):
        raise CalibrationError("session binding field types invalid", 4)
    for name in ("transcript_path_hash", "session_meta_sha256", "session_id_hash", "thread_id_hash", "project_hash"):
        if len(value[name]) != 64 or any(char not in "0123456789abcdef" for char in value[name]):
            raise CalibrationError("session binding hash invalid", 4)
    if (run_id is not None and value["run_id"] != run_id) or (session_id is not None and value["session_id"] != session_id):
        raise CalibrationError("session binding run/session mismatch", 3)
    path = Path(value["transcript"])
    if path.is_symlink() or hashlib.sha256(str(path.resolve()).encode()).hexdigest() != value["transcript_path_hash"]:
        raise CalibrationError("session transcript path binding changed", 4)
    try:
        rows, facts = secure_jsonl(path)
        payload = validate_session_meta(rows[0] if rows else None, root, expected_session_id=value["session_id"], require_root=True)
    except CodexIdentityError as exc:
        raise CalibrationError(str(exc), exc.code) from exc
    dev, ino, _, _ = facts["stat"]
    meta_sha = hashlib.sha256(json.dumps(rows[0], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    checks = (
        (dev == value["transcript_dev"] and ino == value["transcript_ino"], "session transcript file replaced"),
        (meta_sha == value["session_meta_sha256"], "leading session metadata changed"),
        (hashlib.sha256(payload["session_id"].encode()).hexdigest() == value["session_id_hash"], "root session binding changed"),
        (hashlib.sha256(payload["id"].encode()).hexdigest() == value["thread_id_hash"], "root thread binding changed"),
        (hashlib.sha256(str(root.resolve()).encode()).hexdigest() == value["project_hash"], "project binding changed"),
    )
    for passed, message in checks:
        if not passed: raise CalibrationError(message, 4)
    return value
