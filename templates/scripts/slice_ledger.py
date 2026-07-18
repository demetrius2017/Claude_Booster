#!/usr/bin/env python3
"""Atomic project-local implementation-slice ledger.
Purpose/Contract: Maintain one fail-closed slice with guarded schema-v1 events.
CLI: ``slice_ledger.py [--cwd PATH] {acquire,status,update,...}``.
Limitations: Claims only; no scheduler or authority over native Codex activity.
ENV/Files: No ENV; writes mode-0600 files beneath ``.claude/state``.
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Iterator
from slice_ledger_core import (
    LedgerError, _append, _atomic_projection, _load, _now,
    _failed_verification_binding, _owner_fingerprint, _validate_open_fd, _validate_relpath,
)
from slice_session_registry_core import RegistryError, add_attempt_parsers, build_attempt_event
OK, USAGE, CONFLICT, CORRUPT, UNSUPPORTED, IO_ERROR = 0, 2, 3, 4, 5, 6
RUN_ARTIFACT_RE = re.compile(r"\.claude/state/runs/[0-9a-f]{64}/(?:slice_baseline|slice_verification|slice_verification_attempt_[0-9]{6})\.json\Z")
def _emit(ok: bool, kind: str, *, stream: Any = sys.stdout, **values: Any) -> None:
    print(json.dumps({"ok": ok, "type": kind, **values}, sort_keys=True, separators=(",", ":")), file=stream)
class TypedArgumentParser(argparse.ArgumentParser):
    """Argument parser whose contract failures use the CLI JSON error envelope."""

    def error(self, message: str) -> None:
        raise LedgerError(message, USAGE)
def _git_root(cwd: str) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            text=True, capture_output=True, check=False,
        )
    except OSError as exc:
        raise LedgerError(f"git unavailable: {exc}", UNSUPPORTED) from exc
    if result.returncode or not result.stdout.strip():
        raise LedgerError("cwd is not inside a git worktree", UNSUPPORTED)
    root = Path(result.stdout.strip()).resolve(strict=True)
    cwd_path = Path(cwd).resolve(strict=True)
    if cwd_path != root and root not in cwd_path.parents:
        raise LedgerError("git root does not contain cwd", UNSUPPORTED)
    return root
def _paths(root: Path) -> tuple[Path, Path, Path, Path]:
    state = root / ".claude" / "state"
    return state, state / "slice_ledger.json", state / "slice_events.jsonl", state / "slice_ledger.lock"
def _ensure_component(path: Path, root: Path, *, directory: bool) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise LedgerError("state path escapes git root", CORRUPT) from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LedgerError(f"symlink forbidden in state path: {current}", CORRUPT)
    if path.exists() and directory != path.is_dir():
        raise LedgerError(f"unexpected state path type: {path}", CORRUPT)


def _prepare(root: Path) -> tuple[Path, Path, Path]:
    state, ledger, events, lock = _paths(root)
    claude = root / ".claude"
    _ensure_component(claude, root, directory=True)
    claude.mkdir(mode=0o700, exist_ok=True)
    _ensure_component(state, root, directory=True)
    state.mkdir(mode=0o700, exist_ok=True)
    os.chmod(state, 0o700)
    for file_path in (ledger, events, lock):
        _ensure_component(file_path, root, directory=False)
        if file_path.exists():
            mode = file_path.stat().st_mode
            metadata = file_path.stat()
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600 or metadata.st_nlink != 1:
                raise LedgerError(f"state file must be regular, unlinked, mode 0600: {file_path}", CORRUPT)
    return ledger, events, lock


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[tuple[Path, Path]]:
    ledger, events, lock = _prepare(root)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _validate_open_fd(fd, lock, "lock file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield ledger, events
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _process_start(pid: int) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        return f"proc:{fields[21]}"
    except (OSError, IndexError):
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], text=True, capture_output=True)
        marker = result.stdout.strip()
        return f"ps:{marker}" if result.returncode == 0 and marker else "unknown"
def _owner(session_id: str) -> dict[str, Any]:
    pid = os.getppid()
    return {"session_id": session_id, "pid": pid, "hostname": socket.gethostname(), "process_start": _process_start(pid)}
def _owner_status(owner: dict[str, Any]) -> str:
    if owner["hostname"] != socket.gethostname():
        return "unverifiable"
    if owner["process_start"] == "unknown":
        return "unverifiable"
    try:
        os.kill(owner["pid"], 0)
    except ProcessLookupError:
        return "stale"
    except PermissionError:
        return "unverifiable"
    return "live" if _process_start(owner["pid"]) == owner["process_start"] else "stale"
def _guards(state: dict[str, Any], args: argparse.Namespace) -> None:
    if args.run_id != state["run_id"] or args.session_id != state["owner"]["session_id"] or args.revision != state["revision"]:
        raise LedgerError("run/session/revision guard conflict", CONFLICT)


def _acquire(args: argparse.Namespace, ledger: Path, events_path: Path) -> dict[str, Any]:
    state, events = _load(ledger, events_path)
    paths = sorted(set(_validate_relpath(value) for value in args.allowed_path))
    if not paths:
        raise LedgerError("at least one --allowed-path is required", USAGE)
    if not args.artifact_contract.strip() or not args.slice_id.strip() or not args.session_id.strip():
        raise LedgerError("slice/session/artifact contract must be non-empty", USAGE)
    if state is not None:
        identical = (
            state["state"] == "active"
            and state["slice_id"] == args.slice_id
            and state["artifact_contract"] == args.artifact_contract
            and state["allowed_paths"] == paths
            and state["owner"]["session_id"] == args.session_id
            and (args.run_id is None or args.run_id == state["run_id"])
        )
        if identical:
            return state
        raise LedgerError("slice ledger already exists; no automatic takeover", CONFLICT)
    now = _now()
    payload = {
        "schema_version": 1, "revision": 1, "run_id": args.run_id or str(uuid.uuid4()),
        "slice_id": args.slice_id, "artifact_contract": args.artifact_contract,
        "allowed_paths": paths, "state": "active", "terminal_disposition": None,
        "baseline_sha256": None,
        "verification_sha256": None, "verification_state_sha256": None,
        "baseline_path": None, "verification_path": None,
        "closure": None, "handoff_sha256": None,
        "owner": _owner(args.session_id), "created_at": now, "updated_at": now,
    }
    event = _append(events_path, "acquired", payload, events)
    state = {**payload, "last_event_hash": event["hash"]}
    _atomic_projection(ledger, state)
    return state


def _release(args: argparse.Namespace, ledger: Path, events_path: Path) -> dict[str, Any]:
    state, events = _load(ledger, events_path)
    if state is None:
        raise LedgerError("no slice ledger", CONFLICT)
    _guards(state, args)
    if state["state"] != "active":
        raise LedgerError("terminal ledger is immutable", CONFLICT)
    if state["verification_sha256"] is not None:
        raise LedgerError("verified ledger contract is immutable", CONFLICT)
    payload = {"run_id": state["run_id"], "revision": state["revision"] + 1, "updated_at": _now()}
    event = _append(events_path, "released", payload, events)
    state.update(state="released", revision=payload["revision"], updated_at=payload["updated_at"], last_event_hash=event["hash"])
    _atomic_projection(ledger, state)
    return state


def _update(args: argparse.Namespace, ledger: Path, events_path: Path) -> dict[str, Any]:
    state, events = _load(ledger, events_path)
    if state is None:
        raise LedgerError("no slice ledger", CONFLICT)
    paths = sorted(set(_validate_relpath(value) for value in args.allowed_path))
    if not paths or not args.artifact_contract.strip():
        raise LedgerError("full artifact contract and allowed paths are required", USAGE)
    desired = state["artifact_contract"] == args.artifact_contract and state["allowed_paths"] == paths
    if (
        events and events[-1]["type"] == "contract_expanded"
        and state["state"] == "active" and state["run_id"] == args.run_id
        and state["owner"]["session_id"] == args.session_id
        and state["revision"] == args.revision + 1 and desired
        and events[-1]["payload"]["reason"] == (args.reason or "").strip()
        and events[-1]["payload"]["provenance"] == {"actor":(args.provenance_actor or "").strip(),"source":args.provenance_source,"evidence_sha256":args.provenance_evidence_sha256}
    ):
        return state
    _guards(state, args)
    if state["state"] != "active":
        raise LedgerError("terminal ledger is immutable", CONFLICT)
    failed_binding = _failed_verification_binding(state, ledger.parents[2])
    if failed_binding == "": raise LedgerError("verified PASS ledger contract is immutable", CONFLICT)
    reason, actor = (args.reason or "").strip(), (args.provenance_actor or "").strip()
    if not reason or len(reason) > 512 or not actor or len(actor) > 128:
        raise LedgerError("bounded nonempty update reason/provenance actor required", USAGE)
    if not args.provenance_evidence_sha256 or len(args.provenance_evidence_sha256) != 64 or any(char not in "0123456789abcdef" for char in args.provenance_evidence_sha256):
        raise LedgerError("provenance evidence must be lowercase SHA256", USAGE)
    if args.provenance_source not in {"user_request", "verified_recon", "external_advice"}:
        raise LedgerError("typed provenance source required", USAGE)
    if not set(state["allowed_paths"]).issubset(paths):
        raise LedgerError("update may expand but cannot remove allowed paths", USAGE)
    now, owner = _now(), _owner(args.session_id)
    payload = {
        "run_id": state["run_id"], "revision": state["revision"] + 1, "updated_at": now,
        "previous_contract":state["artifact_contract"], "previous_paths":state["allowed_paths"],
        "previous_contract_sha256":hashlib.sha256(state["artifact_contract"].encode()).hexdigest(), "previous_paths_sha256":hashlib.sha256(json.dumps(state["allowed_paths"],sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest(),
        "artifact_contract": args.artifact_contract, "allowed_paths": paths, "owner": owner,
        "artifact_contract_sha256":hashlib.sha256(args.artifact_contract.encode()).hexdigest(), "allowed_paths_sha256":hashlib.sha256(json.dumps(paths,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest(),
        "reason":reason, "provenance":{"actor":actor,"source":args.provenance_source,"evidence_sha256":args.provenance_evidence_sha256},
        "post_fail_repair":failed_binding is not None, "failed_verification_sha256":failed_binding,
    }
    event = _append(events_path, "contract_expanded", payload, events)
    state.update(artifact_contract=args.artifact_contract, allowed_paths=paths, owner=owner, revision=payload["revision"], updated_at=now, last_event_hash=event["hash"])
    _atomic_projection(ledger, state)
    return state


def _recover(args: argparse.Namespace, ledger: Path, events_path: Path) -> dict[str, Any]:
    state, events = _load(ledger, events_path)
    if state is None or state["state"] != "active":
        raise LedgerError("no active slice to recover", CONFLICT)
    if args.run_id != state["run_id"] or args.revision != state["revision"]:
        raise LedgerError("run/revision guard conflict", CONFLICT)
    owner_status = _owner_status(state["owner"])
    override = bool(args.force_unverifiable_owner)
    if args.prior_owner_fingerprint and not override:
        raise LedgerError("prior-owner fingerprint requires force override", USAGE)
    if owner_status == "live" or (owner_status == "unverifiable" and not override):
        raise LedgerError("owner is not demonstrably stale", CONFLICT)
    if override:
        if owner_status != "unverifiable":
            raise LedgerError("force override is valid only for an unverifiable owner", CONFLICT)
        if args.prior_owner_fingerprint != _owner_fingerprint(state["owner"]):
            raise LedgerError("prior-owner fingerprint conflict", CONFLICT)
    if not args.reason.strip() or not args.session_id.strip():
        raise LedgerError("recovery reason and session are required", USAGE)
    payload = {"run_id": state["run_id"], "revision": state["revision"] + 1, "updated_at": _now(), "reason": args.reason, "previous_owner": state["owner"], "new_owner": _owner(args.session_id), "override_unverifiable": override, "prior_owner_fingerprint": args.prior_owner_fingerprint if override else None}
    event = _append(events_path, "recovered", payload, events)
    state.update(owner=payload["new_owner"], revision=payload["revision"], updated_at=payload["updated_at"], last_event_hash=event["hash"])
    _atomic_projection(ledger, state)
    return state


def _bind_baseline(args: argparse.Namespace, ledger: Path, events_path: Path) -> dict[str, Any]:
    """Append the authoritative hash binding for one immutable baseline receipt."""
    state, events = _load(ledger, events_path)
    if state is None or state["state"] != "active":
        raise LedgerError("active slice required for baseline binding", CONFLICT)
    if (
        events and events[-1]["type"] == "baseline_bound"
        and state["run_id"] == args.run_id and state["owner"]["session_id"] == args.session_id
        and state["revision"] == args.revision + 1 and state["baseline_sha256"] == args.baseline_sha256
        and state["baseline_path"] == args.baseline_path
    ):
        return state
    _guards(state, args)
    if state["baseline_sha256"] is not None:
        raise LedgerError("baseline is already authoritatively bound", CONFLICT)
    if len(args.baseline_sha256) != 64 or any(char not in "0123456789abcdef" for char in args.baseline_sha256):
        raise LedgerError("baseline SHA256 must be lowercase hexadecimal", USAGE)
    if not RUN_ARTIFACT_RE.fullmatch(args.baseline_path):
        raise LedgerError("invalid run-scoped baseline path", USAGE)
    _validate_binding_receipt(state, ledger, args, "baseline")
    payload = {"run_id": state["run_id"], "revision": state["revision"] + 1, "updated_at": _now(), "baseline_sha256": args.baseline_sha256, "baseline_path": args.baseline_path}
    event = _append(events_path, "baseline_bound", payload, events)
    state.update(baseline_sha256=args.baseline_sha256, baseline_path=args.baseline_path, revision=payload["revision"], updated_at=payload["updated_at"], last_event_hash=event["hash"])
    _atomic_projection(ledger, state)
    return state


def _bind_verification(args: argparse.Namespace, ledger: Path, events_path: Path) -> dict[str, Any]:
    state, events = _load(ledger, events_path)
    if state is None or state["state"] != "active" or state["baseline_sha256"] is None:
        raise LedgerError("baseline-bound active slice required", CONFLICT)
    if (
        events and events[-1]["type"] == "verification_bound" and state["run_id"] == args.run_id
        and state["owner"]["session_id"] == args.session_id and state["revision"] == args.revision + 1
        and state["verification_sha256"] == args.verification_sha256
        and state["verification_state_sha256"] == args.state_sha256
        and state["verification_path"] == args.verification_path
    ):
        return state
    _guards(state, args)
    if state["verification_sha256"] is not None:
        raise LedgerError("verification is already authoritatively bound", CONFLICT)
    for value in (args.verification_sha256, args.state_sha256):
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise LedgerError("verification hashes must be lowercase SHA256", USAGE)
    if not RUN_ARTIFACT_RE.fullmatch(args.verification_path):
        raise LedgerError("invalid run-scoped verification path", USAGE)
    _validate_binding_receipt(state, ledger, args, "verification")
    payload = {"run_id": state["run_id"], "revision": state["revision"] + 1, "updated_at": _now(), "verification_sha256": args.verification_sha256, "state_sha256": args.state_sha256, "verification_path": args.verification_path}
    event = _append(events_path, "verification_bound", payload, events)
    state.update(verification_sha256=args.verification_sha256, verification_state_sha256=args.state_sha256, verification_path=args.verification_path, revision=payload["revision"], updated_at=payload["updated_at"], last_event_hash=event["hash"])
    _atomic_projection(ledger, state)
    return state
def _bind_verification_retry(args: argparse.Namespace, ledger: Path, events_path: Path) -> dict[str, Any]:
    """Replace only the latest binding while preserving the prior FAIL event."""
    state, events = _load(ledger, events_path)
    if state is None or state["state"] != "active" or state["baseline_sha256"] is None: raise LedgerError("baseline-bound active slice required", CONFLICT)
    if events and events[-1]["type"] == "verification_retried" and state["revision"] == args.revision + 1 and state["verification_sha256"] == args.verification_sha256: return state
    _guards(state, args); failed = _failed_verification_binding(state, ledger.parents[2])
    if not failed or args.previous_verification_sha256 != failed: raise LedgerError("verification retry predecessor mismatch", CONFLICT)
    if args.attempt_number < 2 or not args.attempt_id.strip() or len(args.attempt_id) > 128 or not args.repair_reason.strip() or len(args.repair_reason) > 512 or not args.provenance_actor.strip() or len(args.provenance_actor) > 128 or args.provenance_source not in {"user_request", "verified_recon", "external_advice"}: raise LedgerError("verification retry metadata invalid", USAGE)
    if any(len(value)!=64 or any(char not in "0123456789abcdef" for char in value) for value in (args.verification_sha256,args.state_sha256,args.evidence_sha256)): raise LedgerError("verification retry hashes must be lowercase SHA256", USAGE)
    if args.verification_sha256 == failed or not RUN_ARTIFACT_RE.fullmatch(args.verification_path): raise LedgerError("verification retry artifact invalid", CONFLICT)
    _validate_binding_receipt(state, ledger, args, "verification_retry")
    payload = {"run_id":state["run_id"], "revision":state["revision"]+1, "updated_at":_now(), "previous_verification_sha256":failed, "verification_sha256":args.verification_sha256, "state_sha256":args.state_sha256, "verification_path":args.verification_path, "attempt_id":args.attempt_id, "attempt_number":args.attempt_number, "repair_reason":args.repair_reason, "provenance":{"actor":args.provenance_actor,"source":args.provenance_source}, "evidence_sha256":args.evidence_sha256, "first_pass":False}
    event = _append(events_path,"verification_retried",payload,events); state.update(verification_sha256=args.verification_sha256,verification_state_sha256=args.state_sha256,verification_path=args.verification_path,revision=payload["revision"],updated_at=payload["updated_at"],last_event_hash=event["hash"])
    _atomic_projection(ledger, state); return state
def _validate_binding_receipt(state: dict[str, Any], ledger: Path, args: argparse.Namespace, kind: str) -> None:
    """Validate an internal run-scoped receipt before notarizing its hash."""
    run_hash = __import__("hashlib").sha256(state["run_id"].encode()).hexdigest()
    filename = "slice_baseline.json" if kind == "baseline" else (f"slice_verification_attempt_{args.attempt_number:06d}.json" if kind == "verification_retry" else "slice_verification.json")
    relative = f".claude/state/runs/{run_hash}/{filename}"
    supplied_path = args.baseline_path if kind == "baseline" else args.verification_path
    supplied_hash = args.baseline_sha256 if kind == "baseline" else args.verification_sha256
    if supplied_path != relative:
        raise LedgerError("receipt path does not match current run", CONFLICT)
    root = ledger.parents[2]
    path = root / relative
    run_dir = path.parent
    if run_dir.is_symlink() or path.is_symlink() or not path.exists():
        raise LedgerError("binding receipt is missing or symlinked", CONFLICT)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
        raise LedgerError("binding receipt must be regular, single-link, mode 0600", CORRUPT)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened, current = os.fstat(fd), os.stat(path, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise LedgerError("binding receipt inode/path mismatch", CORRUPT)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError("binding receipt JSON is invalid", CORRUPT) from exc
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if raw != canonical + b"\n" or __import__("hashlib").sha256(canonical).hexdigest() != supplied_hash:
        raise LedgerError("binding receipt canonical hash mismatch", CORRUPT)
    if kind == "baseline":
        expected = {"schema_version", "run_id", "slice_id", "ledger_revision", "ledger_event_hash", "artifact_contract_sha256", "allowed_paths", "captured_at", "git"}
        if not isinstance(value, dict) or set(value) != expected or value["run_id"] != state["run_id"] or value["slice_id"] != state["slice_id"] or value["ledger_revision"] != state["revision"] or value["ledger_event_hash"] != state["last_event_hash"]:
            raise LedgerError("baseline receipt identity/schema mismatch", CORRUPT)
    else:
        top = {"schema_version", "status", "facts", "claim", "attribution", "identity", "limitations"} | ({"attempt"} if kind == "verification_retry" else set())
        identity = value.get("identity", {}) if isinstance(value, dict) else {}
        facts = value.get("facts", {}) if isinstance(value, dict) else {}
        limitations = value.get("limitations", {}) if isinstance(value, dict) else {}
        expected_limits = {"observation_model": "pre_post_snapshot", "transient_mutation_detection": False, "external_side_effect_detection": False, "future_stability": False}
        claim = value.get("claim", {}) if isinstance(value, dict) else {}
        attribution = value.get("attribution", {}) if isinstance(value, dict) else {}
        claim_keys = {"argv", "resolved_executable", "executable_before", "executable_after", "started_at", "ended_at", "exit_code", "timed_out", "stdout", "stderr", "environment_keys"}
        output_keys = {"bytes", "sha256", "content", "truncated", "limit_exceeded"}
        observed_pass = claim.get("exit_code") == 0 and claim.get("timed_out") is False and facts.get("state_unchanged") is True and claim.get("executable_before") == claim.get("executable_after") and isinstance(claim.get("stdout"), dict) and isinstance(claim.get("stderr"), dict) and not claim["stdout"].get("limit_exceeded") and not claim["stderr"].get("limit_exceeded")
        attempt = value.get("attempt")
        retry_ok = kind != "verification_retry" or attempt == {"attempt_id":args.attempt_id,"attempt_number":args.attempt_number,"retry_of_sha256":args.previous_verification_sha256,"repair_reason":args.repair_reason,"provenance":{"actor":args.provenance_actor,"source":args.provenance_source},"first_pass":False}
        identity_keys={"run_id","slice_id","session_id","expected_revision","artifact_contract_sha256","evidence_sha256"}|({"baseline_generation","baseline_sha256"} if kind=="verification_retry" else set())
        if set(value) != top or value.get("status") not in {"pass", "fail"} or (value["status"] == "pass") != observed_pass or not retry_ok or set(facts) != {"pre_state_sha256", "post_state_sha256", "state_unchanged"} or set(identity) != identity_keys or (kind=="verification_retry" and (identity.get("baseline_generation")!=(int(re.search(r"_v(\d+)\.json$",state["baseline_path"]).group(1)) if "_v" in state["baseline_path"] else 1) or identity.get("baseline_sha256")!=state["baseline_sha256"])) or set(claim) != claim_keys or set(claim.get("stdout", {})) != output_keys or set(claim.get("stderr", {})) != output_keys or identity.get("run_id") != state["run_id"] or identity.get("slice_id") != state["slice_id"] or identity.get("session_id") != state["owner"]["session_id"] or identity.get("expected_revision") != state["revision"] or identity.get("artifact_contract_sha256") != __import__("hashlib").sha256(state["artifact_contract"].encode()).hexdigest() or facts.get("pre_state_sha256") != args.state_sha256 or attribution.get("state_sha256") != args.state_sha256 or attribution.get("run_id") != state["run_id"] or limitations != expected_limits:
            raise LedgerError("verification receipt identity/schema mismatch", CORRUPT)
def _new_run(args: argparse.Namespace, ledger: Path, events_path: Path) -> dict[str, Any]:
    state, events = _load(ledger, events_path)
    paths = sorted(set(_validate_relpath(value) for value in args.allowed_path))
    if (
        state is not None and events and events[-1]["type"] == "new_run"
        and state["state"] == "active" and state["revision"] == 1
        and state["run_id"] == args.run_id and state["owner"]["session_id"] == args.session_id
        and state["slice_id"] == args.slice_id and state["artifact_contract"] == args.artifact_contract
        and state["allowed_paths"] == paths and events[-1]["payload"]["previous_run_id"] == args.previous_run_id
        and events[-1]["payload"]["previous_revision"] == args.previous_revision
    ):
        return state
    if state is None or state["state"] not in {"released", "closed"}:
        raise LedgerError("new-run requires a terminal run", CONFLICT)
    if state["run_id"] != args.previous_run_id or state["revision"] != args.previous_revision:
        raise LedgerError("previous run/revision guard conflict", CONFLICT)
    if not paths or not args.run_id or args.run_id == state["run_id"] or not args.session_id.strip() or not args.artifact_contract.strip():
        raise LedgerError("invalid new-run identity or contract", USAGE)
    now = _now()
    acquired = {"schema_version": 1, "revision": 1, "run_id": args.run_id, "slice_id": args.slice_id, "artifact_contract": args.artifact_contract, "allowed_paths": paths, "state": "active", "terminal_disposition": None, "baseline_sha256": None, "verification_sha256": None, "verification_state_sha256": None, "baseline_path": None, "verification_path": None, "closure": None, "handoff_sha256": None, "owner": _owner(args.session_id), "created_at": now, "updated_at": now}
    payload = {**acquired, "previous_run_id": state["run_id"], "previous_revision": state["revision"], "previous_terminal_hash": state["last_event_hash"]}
    event = _append(events_path, "new_run", payload, events)
    result = {**acquired, "last_event_hash": event["hash"]}
    _atomic_projection(ledger, result)
    return result


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = TypedArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    acquire = sub.add_parser("acquire")
    acquire.add_argument("--slice-id", required=True)
    acquire.add_argument("--artifact-contract", required=True)
    acquire.add_argument("--allowed-path", action="append", required=True)
    acquire.add_argument("--session-id", required=True)
    acquire.add_argument("--run-id")
    status = sub.add_parser("status")
    status.add_argument("--run-id")
    update = sub.add_parser("update")
    update.add_argument("--run-id", required=True)
    update.add_argument("--session-id", required=True)
    update.add_argument("--revision", type=_positive_int, required=True)
    update.add_argument("--artifact-contract", required=True)
    update.add_argument("--allowed-path", action="append", required=True)
    update.add_argument("--reason"); update.add_argument("--provenance-actor")
    update.add_argument("--provenance-source", required=True, choices=("user_request", "verified_recon", "external_advice")); update.add_argument("--provenance-evidence-sha256")
    release = sub.add_parser("release")
    release.add_argument("--run-id", required=True)
    release.add_argument("--session-id", required=True)
    release.add_argument("--revision", type=_positive_int, required=True)
    new_run = sub.add_parser("new-run")
    new_run.add_argument("--previous-run-id", required=True)
    new_run.add_argument("--previous-revision", type=_positive_int, required=True)
    new_run.add_argument("--run-id", required=True)
    new_run.add_argument("--session-id", required=True)
    new_run.add_argument("--slice-id", required=True)
    new_run.add_argument("--artifact-contract", required=True)
    new_run.add_argument("--allowed-path", action="append", required=True)
    recover = sub.add_parser("recover")
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--revision", type=_positive_int, required=True)
    recover.add_argument("--session-id", required=True)
    recover.add_argument("--reason", required=True)
    recover.add_argument("--prior-owner-fingerprint")
    recover.add_argument("--force-unverifiable-owner", action="store_true")
    add_attempt_parsers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        raw = list(sys.argv[1:] if argv is None else argv)
        if "update" in raw and "--provenance-source" not in raw:
            probe_root = _git_root(raw[raw.index("--cwd") + 1] if "--cwd" in raw else ".")
            with _locked(probe_root) as probe_paths:
                probe, _ = _load(*probe_paths)
                if probe and probe["verification_sha256"] is not None and _failed_verification_binding(probe, probe_root) == "": raise LedgerError("verified PASS ledger contract is immutable", CONFLICT)
        args = _parser().parse_args(raw)
        root = _git_root(args.cwd)
        with _locked(root) as (ledger, events):
            if args.command == "acquire":
                state = _acquire(args, ledger, events)
            elif args.command == "update":
                state = _update(args, ledger, events)
            elif args.command == "release":
                state = _release(args, ledger, events)
            elif args.command == "new-run":
                state = _new_run(args, ledger, events)
            elif args.command == "recover":
                state = _recover(args, ledger, events)
            elif args.command.startswith("attempt-"):
                state, history = _load(ledger, events)
                if state is None: raise LedgerError("no slice ledger", CONFLICT)
                kind, payload = build_attempt_event(args, state, history)
                event = _append(events, kind, payload, history); state["last_event_hash"] = event["hash"]; _atomic_projection(ledger, state)
            else:
                state, _ = _load(ledger, events)
                if state is None:
                    raise LedgerError("no slice ledger", CONFLICT)
                if args.run_id and args.run_id != state["run_id"]:
                    raise LedgerError("run guard conflict", CONFLICT)
        _emit(True, args.command, ledger=state)
        return OK
    except (LedgerError, RegistryError) as exc:
        _emit(False, "error", stream=sys.stderr, code=exc.code, error=str(exc))
        return exc.code
    except OSError as exc:
        _emit(False, "error", stream=sys.stderr, code=IO_ERROR, error=f"filesystem error: {exc}")
        return IO_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
