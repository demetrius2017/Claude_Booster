#!/usr/bin/env python3
"""Atomic project-local implementation-slice ledger.

Purpose: Maintain one fail-closed, auditable active slice for Booster wrappers.
Contract: Commands operate only in a strict git worktree and validate schema-v1
inputs, owner guards, revisions, paths, the event hash chain, and projection.
CLI/Examples: ``slice_ledger.py [--cwd PATH]
{acquire,status,update,release,recover} ...``; use ``status`` for JSON state.
Limitations: This is a claim ledger, not git attribution, verification, backlog,
telemetry, a scheduler, or an authority over native Codex activity.
ENV/Files: No environment variables. Writes mode-0600 files beneath
``<git-root>/.claude/state/{slice_ledger.json,slice_events.jsonl,slice_ledger.lock}``.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import socket
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Iterator

from slice_ledger_core import (
    LedgerError, _append, _atomic_projection, _load, _now,
    _owner_fingerprint, _validate_open_fd, _validate_relpath,
)

OK, USAGE, CONFLICT, CORRUPT, UNSUPPORTED, IO_ERROR = 0, 2, 3, 4, 5, 6


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
        events and events[-1]["type"] == "updated"
        and state["state"] == "active" and state["run_id"] == args.run_id
        and state["owner"]["session_id"] == args.session_id
        and state["revision"] == args.revision + 1 and desired
    ):
        return state
    _guards(state, args)
    if state["state"] != "active":
        raise LedgerError("terminal ledger is immutable", CONFLICT)
    now, owner = _now(), _owner(args.session_id)
    payload = {
        "run_id": state["run_id"], "revision": state["revision"] + 1, "updated_at": now,
        "artifact_contract": args.artifact_contract, "allowed_paths": paths, "owner": owner,
    }
    event = _append(events_path, "updated", payload, events)
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
    ):
        return state
    _guards(state, args)
    if state["baseline_sha256"] is not None:
        raise LedgerError("baseline is already authoritatively bound", CONFLICT)
    if len(args.baseline_sha256) != 64 or any(char not in "0123456789abcdef" for char in args.baseline_sha256):
        raise LedgerError("baseline SHA256 must be lowercase hexadecimal", USAGE)
    payload = {"run_id": state["run_id"], "revision": state["revision"] + 1, "updated_at": _now(), "baseline_sha256": args.baseline_sha256}
    event = _append(events_path, "baseline_bound", payload, events)
    state.update(baseline_sha256=args.baseline_sha256, revision=payload["revision"], updated_at=payload["updated_at"], last_event_hash=event["hash"])
    _atomic_projection(ledger, state)
    return state


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
    release = sub.add_parser("release")
    release.add_argument("--run-id", required=True)
    release.add_argument("--session-id", required=True)
    release.add_argument("--revision", type=_positive_int, required=True)
    bind = sub.add_parser("bind-baseline")
    bind.add_argument("--run-id", required=True)
    bind.add_argument("--session-id", required=True)
    bind.add_argument("--revision", type=_positive_int, required=True)
    bind.add_argument("--baseline-sha256", required=True)
    recover = sub.add_parser("recover")
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--revision", type=_positive_int, required=True)
    recover.add_argument("--session-id", required=True)
    recover.add_argument("--reason", required=True)
    recover.add_argument("--prior-owner-fingerprint")
    recover.add_argument("--force-unverifiable-owner", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        root = _git_root(args.cwd)
        with _locked(root) as (ledger, events):
            if args.command == "acquire":
                state = _acquire(args, ledger, events)
            elif args.command == "update":
                state = _update(args, ledger, events)
            elif args.command == "release":
                state = _release(args, ledger, events)
            elif args.command == "bind-baseline":
                state = _bind_baseline(args, ledger, events)
            elif args.command == "recover":
                state = _recover(args, ledger, events)
            else:
                state, _ = _load(ledger, events)
                if state is None:
                    raise LedgerError("no slice ledger", CONFLICT)
                if args.run_id and args.run_id != state["run_id"]:
                    raise LedgerError("run guard conflict", CONFLICT)
        _emit(True, args.command, ledger=state)
        return OK
    except LedgerError as exc:
        _emit(False, "error", stream=sys.stderr, code=exc.code, error=str(exc))
        return exc.code
    except OSError as exc:
        _emit(False, "error", stream=sys.stderr, code=IO_ERROR, error=f"filesystem error: {exc}")
        return IO_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
