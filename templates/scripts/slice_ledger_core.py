"""Schema, journal, and durability primitives for the slice ledger.

Purpose: Validate and persist the schema-v1 slice event chain and projection.
Contract: All reads fail closed on schema, hash, containment, link, or history
mismatch; writes fsync the append-only journal before atomically replacing its
projection. Callers must hold the project-local slice lock.
CLI/Examples: Not a standalone CLI; imported by slice_ledger.py.
Limitations: Contains no command parsing, lifecycle policy, git attribution,
closure verification, backlog, telemetry, or wrapper integration.
ENV/Files: No environment variables. Operates on explicit mode-0600 paths under
<git-root>/.claude/state supplied by the CLI module.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

CORRUPT, IO_ERROR, USAGE = 4, 6, 2
GENESIS = "0" * 64
STATES = {"active", "released"}
DISPOSITIONS = {None}
LEDGER_KEYS = {
    "schema_version", "revision", "run_id", "slice_id", "artifact_contract",
    "allowed_paths", "state", "terminal_disposition", "owner", "created_at",
    "updated_at", "last_event_hash",
}
OWNER_KEYS = {"session_id", "pid", "hostname", "process_start"}
EVENT_KEYS = {"schema_version", "sequence", "timestamp", "type", "payload", "previous_hash", "hash"}
ACQUIRE_PAYLOAD_KEYS = LEDGER_KEYS - {"last_event_hash"}
RELEASE_PAYLOAD_KEYS = {"run_id", "revision", "updated_at"}
UPDATE_PAYLOAD_KEYS = RELEASE_PAYLOAD_KEYS | {"artifact_contract", "allowed_paths", "owner"}
RECOVER_PAYLOAD_KEYS = RELEASE_PAYLOAD_KEYS | {"reason", "previous_owner", "new_owner", "override_unverifiable", "prior_owner_fingerprint"}


class LedgerError(Exception):
    """A typed user-facing failure carrying a stable CLI exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _owner_fingerprint(owner: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(owner)).hexdigest()

def _validate_open_fd(fd: int, path: Path, label: str) -> None:
    opened = os.fstat(fd)
    current = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise LedgerError(f"{label} inode/path containment mismatch", CORRUPT)


def _read_secure(path: Path, label: str) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        _validate_open_fd(fd, path, label)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _event_hash(event_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(event_without_hash)).hexdigest()


def _validate_timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise LedgerError(f"invalid {field}", CORRUPT)
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise LedgerError(f"invalid {field}", CORRUPT) from exc
    if parsed.tzinfo != dt.timezone.utc:
        raise LedgerError(f"invalid {field}", CORRUPT)


def _validate_hash(value: Any, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise LedgerError(f"invalid {field}", CORRUPT)


def _validate_relpath(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise LedgerError(f"invalid allowed path: {value!r}", USAGE)
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(part in ("", ".", "..") for part in path.parts):
        raise LedgerError(f"allowed path must be normalized project-relative POSIX path: {value!r}", USAGE)
    if not path.parts or path.parts[0] == ".git" or path.parts[:2] == (".claude", "state"):
        raise LedgerError(f"protected path forbidden: {value!r}", USAGE)
    return value


def _validate_owner(owner: Any) -> None:
    if not isinstance(owner, dict) or set(owner) != OWNER_KEYS:
        raise LedgerError("invalid owner schema", CORRUPT)
    if not isinstance(owner["session_id"], str) or not owner["session_id"]:
        raise LedgerError("invalid owner session_id", CORRUPT)
    if not isinstance(owner["pid"], int) or isinstance(owner["pid"], bool) or owner["pid"] <= 0:
        raise LedgerError("invalid owner pid", CORRUPT)
    for key in ("hostname", "process_start"):
        if not isinstance(owner[key], str) or not owner[key]:
            raise LedgerError(f"invalid owner {key}", CORRUPT)


def _validate_ledger(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != LEDGER_KEYS:
        raise LedgerError("ledger schema keys mismatch", CORRUPT)
    if not isinstance(value["schema_version"], int) or isinstance(value["schema_version"], bool) or value["schema_version"] != 1:
        raise LedgerError("unsupported ledger schema", CORRUPT)
    if not isinstance(value["revision"], int) or isinstance(value["revision"], bool) or value["revision"] < 1:
        raise LedgerError("invalid ledger revision", CORRUPT)
    for key in ("run_id", "slice_id", "artifact_contract", "created_at", "updated_at", "last_event_hash"):
        if not isinstance(value[key], str) or not value[key]:
            raise LedgerError(f"invalid ledger {key}", CORRUPT)
    if value["state"] not in STATES or value["terminal_disposition"] not in DISPOSITIONS:
        raise LedgerError("invalid lifecycle state", CORRUPT)
    if not isinstance(value["allowed_paths"], list) or not value["allowed_paths"]:
        raise LedgerError("allowed_paths must be a non-empty list", CORRUPT)
    try:
        normalized = [_validate_relpath(item) for item in value["allowed_paths"]]
    except LedgerError as exc:
        raise LedgerError(str(exc), CORRUPT) from exc
    if normalized != sorted(set(normalized)):
        raise LedgerError("allowed_paths must be unique and sorted", CORRUPT)
    _validate_owner(value["owner"])
    _validate_timestamp(value["created_at"], "created_at")
    _validate_timestamp(value["updated_at"], "updated_at")
    _validate_hash(value["last_event_hash"], "last_event_hash")
    return value


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = _read_secure(path, "event log")
    except OSError as exc:
        raise LedgerError(f"cannot read event log: {exc}", IO_ERROR) from exc
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise LedgerError("truncated event log", CORRUPT)
    chain: list[dict[str, Any]] = []
    previous = GENESIS
    for sequence, line in enumerate(raw.splitlines(), 1):
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerError("corrupt event JSON", CORRUPT) from exc
        if (
            not isinstance(event, dict)
            or set(event) != EVENT_KEYS
            or not isinstance(event["schema_version"], int)
            or isinstance(event["schema_version"], bool)
            or event["schema_version"] != 1
        ):
            raise LedgerError("event schema mismatch", CORRUPT)
        if not isinstance(event["sequence"], int) or isinstance(event["sequence"], bool):
            raise LedgerError("invalid event sequence type", CORRUPT)
        _validate_timestamp(event["timestamp"], "event timestamp")
        _validate_hash(event["previous_hash"], "previous_hash")
        _validate_hash(event["hash"], "event hash")
        if event["sequence"] != sequence or event["previous_hash"] != previous:
            raise LedgerError("event history sequence/hash mismatch", CORRUPT)
        unsigned = {key: event[key] for key in EVENT_KEYS - {"hash"}}
        expected = _event_hash(unsigned)
        if event["hash"] != expected:
            raise LedgerError("event content hash mismatch", CORRUPT)
        if event["type"] not in {"acquired", "updated", "released", "recovered"} or not isinstance(event["payload"], dict):
            raise LedgerError("invalid event type/payload", CORRUPT)
        expected_payload_keys = {
            "acquired": ACQUIRE_PAYLOAD_KEYS,
            "updated": UPDATE_PAYLOAD_KEYS,
            "released": RELEASE_PAYLOAD_KEYS,
            "recovered": RECOVER_PAYLOAD_KEYS,
        }[event["type"]]
        if set(event["payload"]) != expected_payload_keys:
            raise LedgerError("event payload schema mismatch", CORRUPT)
        previous = expected
        chain.append(event)
    return chain


def _project(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    state: dict[str, Any] | None = None
    for event in events:
        payload = event["payload"]
        if event["type"] == "acquired":
            if state is not None:
                raise LedgerError("second acquire in one event history", CORRUPT)
            state = dict(payload)
            state["last_event_hash"] = event["hash"]
        elif event["type"] == "updated":
            if state is None or state["state"] != "active":
                raise LedgerError("update event violates lifecycle", CORRUPT)
            if payload["run_id"] != state["run_id"] or payload["revision"] != state["revision"] + 1:
                raise LedgerError("update event guard mismatch", CORRUPT)
            if not isinstance(payload["artifact_contract"], str) or not payload["artifact_contract"].strip():
                raise LedgerError("update artifact contract invalid", CORRUPT)
            if not isinstance(payload["allowed_paths"], list):
                raise LedgerError("update allowed_paths invalid", CORRUPT)
            normalized = [_validate_relpath(item) for item in payload["allowed_paths"]]
            if normalized != sorted(set(normalized)) or not normalized:
                raise LedgerError("update allowed_paths invalid", CORRUPT)
            _validate_owner(payload["owner"])
            state.update(
                artifact_contract=payload["artifact_contract"], allowed_paths=normalized,
                owner=payload["owner"], revision=payload["revision"], updated_at=payload["updated_at"],
                last_event_hash=event["hash"],
            )
        elif event["type"] == "released":
            if state is None or state["state"] != "active":
                raise LedgerError("release event violates lifecycle", CORRUPT)
            if payload.get("run_id") != state["run_id"] or payload.get("revision") != state["revision"] + 1:
                raise LedgerError("release event guard mismatch", CORRUPT)
            state["state"] = "released"
            state["revision"] = payload["revision"]
            state["updated_at"] = payload["updated_at"]
            state["last_event_hash"] = event["hash"]
        else:
            if state is None or state["state"] != "active":
                raise LedgerError("recover event violates lifecycle", CORRUPT)
            if payload.get("run_id") != state["run_id"] or payload.get("revision") != state["revision"] + 1:
                raise LedgerError("recover event guard mismatch", CORRUPT)
            if not isinstance(payload.get("reason"), str) or not payload["reason"]:
                raise LedgerError("recover event lacks reason", CORRUPT)
            if not isinstance(payload.get("override_unverifiable"), bool):
                raise LedgerError("recover override flag invalid", CORRUPT)
            _validate_owner(payload.get("new_owner"))
            _validate_owner(payload.get("previous_owner"))
            if payload["previous_owner"] != state["owner"]:
                raise LedgerError("recover previous-owner provenance mismatch", CORRUPT)
            expected_fingerprint = _owner_fingerprint(payload["previous_owner"]) if payload["override_unverifiable"] else None
            if payload["prior_owner_fingerprint"] != expected_fingerprint:
                raise LedgerError("recover owner fingerprint provenance mismatch", CORRUPT)
            state["owner"] = payload["new_owner"]
            state["revision"] = payload["revision"]
            state["updated_at"] = payload["updated_at"]
            state["last_event_hash"] = event["hash"]
    return _validate_ledger(state) if state is not None else None


def _read_projection(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return _validate_ledger(json.loads(_read_secure(path, "ledger projection")))
    except LedgerError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError("corrupt ledger projection", CORRUPT) from exc


def _atomic_projection(path: Path, state: dict[str, Any]) -> None:
    payload = _canonical(state) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".slice_ledger.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _load(ledger_path: Path, events_path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    events = _read_events(events_path)
    projected = _project(events)
    disk = _read_projection(ledger_path)
    if projected is None:
        if disk is not None:
            raise LedgerError("projection exists without event history", CORRUPT)
        return None, events
    if disk is None or disk != projected:
        # A valid log ahead by exactly one event is the supported crash window.
        if disk is None and len(events) == 1 or (disk is not None and disk.get("last_event_hash") == events[-2]["hash"] if len(events) > 1 else False):
            _atomic_projection(ledger_path, projected)
        else:
            raise LedgerError("ledger projection/history mismatch", CORRUPT)
    return projected, events


def _append(path: Path, event_type: str, payload: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    unsigned = {
        "schema_version": 1, "sequence": len(events) + 1, "timestamp": _now(),
        "type": event_type, "payload": payload,
        "previous_hash": events[-1]["hash"] if events else GENESIS,
    }
    event = {**unsigned, "hash": _event_hash(unsigned)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        _validate_open_fd(fd, path, "event log")
        os.fchmod(fd, 0o600)
        record = _canonical(event) + b"\n"
        written = 0
        while written < len(record):
            written += os.write(fd, record[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    return event


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
