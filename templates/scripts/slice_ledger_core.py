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
from slice_session_registry_core import ATTEMPT_KEYS, ATTEMPT_TYPES, RegistryError, apply_attempt_event

CORRUPT, IO_ERROR, USAGE = 4, 6, 2
GENESIS = "0" * 64
STATES = {"active", "released", "closed"}
DISPOSITIONS = {None, "committed", "quarantined", "delivered_uncommitted", "blocked"}
LEDGER_KEYS = {
    "schema_version", "revision", "run_id", "slice_id", "artifact_contract",
    "allowed_paths", "state", "terminal_disposition", "owner", "created_at",
    "updated_at", "last_event_hash", "baseline_sha256", "verification_sha256",
    "verification_state_sha256",
    "baseline_path", "verification_path",
    "closure", "handoff_sha256",
}
LEGACY_LEDGER_KEYS = LEDGER_KEYS - {"baseline_sha256", "verification_sha256", "verification_state_sha256", "baseline_path", "verification_path"}
OWNER_KEYS = {"session_id", "pid", "hostname", "process_start"}
EVENT_KEYS = {"schema_version", "sequence", "timestamp", "type", "payload", "previous_hash", "hash"}
ACQUIRE_PAYLOAD_KEYS = LEDGER_KEYS - {"last_event_hash"}
RELEASE_PAYLOAD_KEYS = {"run_id", "revision", "updated_at"}
UPDATE_PAYLOAD_KEYS = RELEASE_PAYLOAD_KEYS | {"artifact_contract", "allowed_paths", "owner"}
CONTRACT_PAYLOAD_KEYS = RELEASE_PAYLOAD_KEYS | {"previous_contract", "previous_paths", "previous_contract_sha256", "previous_paths_sha256", "artifact_contract", "allowed_paths", "artifact_contract_sha256", "allowed_paths_sha256", "reason", "provenance", "owner", "post_fail_repair", "failed_verification_sha256"}
RECOVER_PAYLOAD_KEYS = RELEASE_PAYLOAD_KEYS | {"reason", "previous_owner", "new_owner", "override_unverifiable", "prior_owner_fingerprint"}
BASELINE_PAYLOAD_KEYS = RELEASE_PAYLOAD_KEYS | {"baseline_sha256", "baseline_path"}
BASELINE_REFRESH_KEYS = BASELINE_PAYLOAD_KEYS | {"previous_baseline_sha256", "root_baseline_sha256", "generation", "failed_verification_sha256", "expansion_event_hash", "state_sha256"}
VERIFY_PAYLOAD_KEYS = RELEASE_PAYLOAD_KEYS | {"verification_sha256", "state_sha256", "verification_path"}
VERIFY_RETRY_PAYLOAD_KEYS = VERIFY_PAYLOAD_KEYS | {"previous_verification_sha256", "attempt_id", "attempt_number", "repair_reason", "provenance", "evidence_sha256", "first_pass"}
NEW_RUN_PAYLOAD_KEYS = ACQUIRE_PAYLOAD_KEYS | {"previous_run_id", "previous_revision", "previous_terminal_hash"}
CLOSED_PAYLOAD_KEYS = RELEASE_PAYLOAD_KEYS | {"disposition", "state_sha256", "verification_sha256", "commit_oid", "excluded_paths", "backlog_path", "backlog_tail_hash", "backlog_count", "handoff_path", "handoff_sha256", "commit_class"}


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


def _failed_verification_binding(state: dict[str, Any], root: Path) -> str | None:
    """Return the canonical bound FAIL SHA, rejecting missing/tampered receipts."""
    if state["verification_sha256"] is None: return None
    path = root / state["verification_path"]
    try: value = json.loads(_read_secure(path, "verification receipt"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc: raise LedgerError("bound verification receipt unavailable", CORRUPT) from exc
    digest = hashlib.sha256(_canonical(value)).hexdigest()
    if digest != state["verification_sha256"]: raise LedgerError("bound verification receipt hash mismatch", CORRUPT)
    if not isinstance(value, dict) or value.get("status") not in {"pass", "fail"}: raise LedgerError("bound verification receipt status invalid", CORRUPT)
    return digest if value["status"] == "fail" else ""


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
    if value["baseline_sha256"] is not None:
        _validate_hash(value["baseline_sha256"], "baseline_sha256")
    for key in ("verification_sha256", "verification_state_sha256"):
        if value[key] is not None:
            _validate_hash(value[key], key)
    for key in ("baseline_path", "verification_path"):
        if value[key] is not None and (not isinstance(value[key], str) or not value[key].startswith(".claude/state/runs/")):
            raise LedgerError(f"invalid {key}", CORRUPT)
    if value["state"] == "closed":
        if value["terminal_disposition"] not in DISPOSITIONS - {None} or not isinstance(value["closure"], dict) or value["handoff_sha256"] is None:
            raise LedgerError("invalid closed projection", CORRUPT)
    elif value["terminal_disposition"] is not None or value["closure"] is not None or value["handoff_sha256"] is not None:
        raise LedgerError("non-terminal projection carries closure", CORRUPT)
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
        if event["type"] not in {"acquired", "new_run", "updated", "contract_expanded", "released", "recovered", "baseline_bound", "baseline_refreshed", "verification_bound", "verification_retried", "closed", *ATTEMPT_TYPES} or not isinstance(event["payload"], dict):
            raise LedgerError("invalid event type/payload", CORRUPT)
        expected_payload_keys = {
            "acquired": ACQUIRE_PAYLOAD_KEYS,
            "updated": UPDATE_PAYLOAD_KEYS,
            "contract_expanded": CONTRACT_PAYLOAD_KEYS,
            "released": RELEASE_PAYLOAD_KEYS,
            "recovered": RECOVER_PAYLOAD_KEYS,
            "baseline_bound": BASELINE_PAYLOAD_KEYS,
            "baseline_refreshed": BASELINE_REFRESH_KEYS,
            "verification_bound": VERIFY_PAYLOAD_KEYS,
            "verification_retried": VERIFY_RETRY_PAYLOAD_KEYS,
            "new_run": NEW_RUN_PAYLOAD_KEYS,
            "closed": CLOSED_PAYLOAD_KEYS,
            **ATTEMPT_KEYS,
        }[event["type"]]
        payload_keys = set(event["payload"])
        legacy_acquire = event["type"] == "acquired" and payload_keys == ACQUIRE_PAYLOAD_KEYS - {"baseline_sha256"}
        if payload_keys != expected_payload_keys and not legacy_acquire:
            raise LedgerError("event payload schema mismatch", CORRUPT)
        previous = expected
        chain.append(event)
    return chain


def _project(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    state: dict[str, Any] | None = None
    attempts: dict[Any, dict[str, Any]] = {}
    verification_attempt_number = 0
    baseline_generation = 0
    root_baseline_sha256: str | None = None
    for event in events:
        payload = event["payload"]
        if event["type"] == "acquired":
            if state is not None:
                raise LedgerError("second acquire in one event history", CORRUPT)
            state = dict(payload)
            state.setdefault("baseline_sha256", None)
            state.setdefault("verification_sha256", None)
            state.setdefault("verification_state_sha256", None)
            state.setdefault("baseline_path", None)
            state.setdefault("verification_path", None)
            state.setdefault("closure", None)
            state.setdefault("handoff_sha256", None)
            state["last_event_hash"] = event["hash"]
            baseline_generation, root_baseline_sha256 = 0, None
        elif event["type"] == "new_run":
            if state is None or state["state"] not in {"released", "closed"} or state["run_id"] != payload["previous_run_id"]:
                raise LedgerError("new-run event violates terminal transition", CORRUPT)
            if payload["previous_revision"] != state["revision"] or payload["previous_terminal_hash"] != state["last_event_hash"] or payload["run_id"] == state["run_id"]:
                raise LedgerError("new-run provenance mismatch", CORRUPT)
            state = {key: payload[key] for key in ACQUIRE_PAYLOAD_KEYS}
            state["last_event_hash"] = event["hash"]
            attempts = {}
            verification_attempt_number = 0
            baseline_generation, root_baseline_sha256 = 0, None
        elif event["type"] in {"updated", "contract_expanded"}:
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
            if event["type"] == "contract_expanded":
                provenance = payload["provenance"]
                if payload["previous_contract"] != state["artifact_contract"] or payload["previous_paths"] != state["allowed_paths"]: raise LedgerError("contract expansion previous state mismatch", CORRUPT)
                if not set(state["allowed_paths"]).issubset(normalized): raise LedgerError("contract expansion cannot remove allowed paths", CORRUPT)
                if not isinstance(payload["post_fail_repair"], bool) or payload["failed_verification_sha256"] != (state["verification_sha256"] if payload["post_fail_repair"] else None): raise LedgerError("post-fail repair binding invalid", CORRUPT)
                if hashlib.sha256(payload["previous_contract"].encode()).hexdigest() != payload["previous_contract_sha256"] or hashlib.sha256(_canonical(payload["previous_paths"])).hexdigest() != payload["previous_paths_sha256"] or hashlib.sha256(payload["artifact_contract"].encode()).hexdigest() != payload["artifact_contract_sha256"] or hashlib.sha256(_canonical(normalized)).hexdigest() != payload["allowed_paths_sha256"]: raise LedgerError("contract expansion hash provenance mismatch", CORRUPT)
                if not isinstance(payload["reason"], str) or not payload["reason"].strip() or len(payload["reason"]) > 512 or not isinstance(provenance, dict) or set(provenance) != {"actor", "source", "evidence_sha256"} or provenance["source"] not in {"user_request", "verified_recon", "external_advice"} or not isinstance(provenance["actor"], str) or not provenance["actor"].strip() or len(provenance["actor"]) > 128: raise LedgerError("contract expansion reason/provenance invalid", CORRUPT)
                _validate_hash(provenance["evidence_sha256"], "contract evidence")
            _validate_owner(payload["owner"])
            state.update(
                artifact_contract=payload["artifact_contract"], allowed_paths=normalized,
                owner=payload["owner"], revision=payload["revision"], updated_at=payload["updated_at"],
                last_event_hash=event["hash"],
            )
        elif event["type"] == "baseline_bound":
            if state is None or state["state"] != "active" or state["baseline_sha256"] is not None:
                raise LedgerError("baseline binding violates lifecycle", CORRUPT)
            if payload["run_id"] != state["run_id"] or payload["revision"] != state["revision"] + 1:
                raise LedgerError("baseline binding guard mismatch", CORRUPT)
            _validate_hash(payload["baseline_sha256"], "baseline_sha256")
            state.update(baseline_sha256=payload["baseline_sha256"], baseline_path=payload["baseline_path"], revision=payload["revision"], updated_at=payload["updated_at"], last_event_hash=event["hash"])
            baseline_generation, root_baseline_sha256 = 1, payload["baseline_sha256"]
        elif event["type"] == "baseline_refreshed":
            if state is None or state["state"] != "active" or payload["run_id"] != state["run_id"] or payload["revision"] != state["revision"]+1 or payload["previous_baseline_sha256"] != state["baseline_sha256"] or payload["failed_verification_sha256"] != state["verification_sha256"] or payload["expansion_event_hash"] != state["last_event_hash"] or payload["generation"] != baseline_generation + 1:
                raise LedgerError("baseline refresh guard mismatch", CORRUPT)
            for key in ("baseline_sha256","previous_baseline_sha256","root_baseline_sha256","failed_verification_sha256","expansion_event_hash","state_sha256"): _validate_hash(payload[key],key)
            if payload["root_baseline_sha256"] != root_baseline_sha256: raise LedgerError("baseline refresh lineage mismatch", CORRUPT)
            state.update(baseline_sha256=payload["baseline_sha256"],baseline_path=payload["baseline_path"],revision=payload["revision"],updated_at=payload["updated_at"],last_event_hash=event["hash"])
            baseline_generation = payload["generation"]
        elif event["type"] == "verification_bound":
            if state is None or state["state"] != "active" or state["baseline_sha256"] is None or state["verification_sha256"] is not None:
                raise LedgerError("verification binding violates lifecycle", CORRUPT)
            if payload["run_id"] != state["run_id"] or payload["revision"] != state["revision"] + 1:
                raise LedgerError("verification binding guard mismatch", CORRUPT)
            _validate_hash(payload["verification_sha256"], "verification_sha256")
            _validate_hash(payload["state_sha256"], "state_sha256")
            state.update(verification_sha256=payload["verification_sha256"], verification_state_sha256=payload["state_sha256"], verification_path=payload["verification_path"], revision=payload["revision"], updated_at=payload["updated_at"], last_event_hash=event["hash"])
            verification_attempt_number = 1
        elif event["type"] == "verification_retried":
            provenance = payload["provenance"]
            if state is None or state["state"] != "active" or payload["run_id"] != state["run_id"] or payload["revision"] != state["revision"] + 1 or payload["previous_verification_sha256"] != state["verification_sha256"]:
                raise LedgerError("verification retry guard mismatch", CORRUPT)
            if payload["first_pass"] is not False or payload["attempt_number"] != verification_attempt_number + 1 or not isinstance(payload["attempt_id"], str) or not payload["attempt_id"].strip() or len(payload["attempt_id"]) > 128:
                raise LedgerError("verification retry identity invalid", CORRUPT)
            if not isinstance(payload["repair_reason"], str) or not payload["repair_reason"].strip() or len(payload["repair_reason"]) > 512 or not isinstance(provenance, dict) or set(provenance) != {"actor", "source"} or provenance["source"] not in {"user_request", "verified_recon", "external_advice"} or not isinstance(provenance["actor"], str) or not provenance["actor"].strip() or len(provenance["actor"]) > 128:
                raise LedgerError("verification retry provenance invalid", CORRUPT)
            for key in ("previous_verification_sha256", "verification_sha256", "state_sha256", "evidence_sha256"): _validate_hash(payload[key], key)
            if payload["verification_sha256"] == payload["previous_verification_sha256"]:
                raise LedgerError("verification retry digest unchanged", CORRUPT)
            state.update(verification_sha256=payload["verification_sha256"], verification_state_sha256=payload["state_sha256"], verification_path=payload["verification_path"], revision=payload["revision"], updated_at=payload["updated_at"], last_event_hash=event["hash"])
            verification_attempt_number = payload["attempt_number"]
        elif event["type"] in ATTEMPT_TYPES:
            if state is None or state["state"] != "active" or payload["run_id"] != state["run_id"] or payload["session_id"] != state["owner"]["session_id"]: raise LedgerError("attempt cross-run/session or inactive", CORRUPT)
            try: apply_attempt_event(attempts, event["type"], payload)
            except RegistryError as exc: raise LedgerError(str(exc), CORRUPT) from exc
            state["last_event_hash"] = event["hash"]
        elif event["type"] == "released":
            if state is None or state["state"] != "active":
                raise LedgerError("release event violates lifecycle", CORRUPT)
            if payload.get("run_id") != state["run_id"] or payload.get("revision") != state["revision"] + 1:
                raise LedgerError("release event guard mismatch", CORRUPT)
            state["state"] = "released"
            state["revision"] = payload["revision"]
            state["updated_at"] = payload["updated_at"]
            state["last_event_hash"] = event["hash"]
        elif event["type"] == "closed":
            if state is None or state["state"] != "active" or state["verification_sha256"] != payload["verification_sha256"]:
                raise LedgerError("closed event violates lifecycle", CORRUPT)
            if payload["run_id"] != state["run_id"] or payload["revision"] != state["revision"] + 1 or payload["disposition"] not in DISPOSITIONS - {None}:
                raise LedgerError("closed event guard mismatch", CORRUPT)
            _validate_hash(payload["handoff_sha256"], "handoff_sha256")
            state.update(state="closed", terminal_disposition=payload["disposition"], closure=dict(payload), handoff_sha256=payload["handoff_sha256"], revision=payload["revision"], updated_at=payload["updated_at"], last_event_hash=event["hash"])
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
        value = json.loads(_read_secure(path, "ledger projection"))
        if isinstance(value, dict) and set(value) == LEGACY_LEDGER_KEYS:
            value.update(baseline_sha256=None, verification_sha256=None, verification_state_sha256=None, baseline_path=None, verification_path=None, closure=None, handoff_sha256=None)
        elif isinstance(value, dict):
            for key in LEDGER_KEYS - set(value):
                if key in {"baseline_path", "verification_path", "verification_sha256", "verification_state_sha256", "closure", "handoff_sha256"}:
                    value[key] = None
        return _validate_ledger(value)
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
