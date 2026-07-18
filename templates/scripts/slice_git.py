#!/usr/bin/env python3
"""Immutable Git baseline and conservative slice attribution CLI.

Purpose: Bind stable Git facts to an active slice ledger and later classify
changed paths without treating compatibility as proof of authorship.
Contract: Requires exact run/session/revision/event-hash identity, one immutable
mode-0600 baseline receipt, and the shared stable slice lock.
CLI/Examples: ``slice_git.py --cwd ROOT capture|attribute --run-id ID
--session-id ID --revision N``; successful output is typed JSON.
Limitations: No staging, commit authority, closure, backlog, telemetry, semantic
scope inference, hooks, integration, or automatic remediation.
ENV/Files: No environment variables. Reads the slice ledger/event log and writes
only ``<git-root>/.claude/state/runs/<run-hash>/slice_baseline.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from slice_git_core import GitFactError, attribution_receipt, snapshot
from slice_ledger import _bind_baseline, _git_root, _locked
from slice_ledger_core import LedgerError, _atomic_projection, _canonical, _load, _validate_relpath

OK, USAGE, CONFLICT, CORRUPT, UNSUPPORTED, IO_ERROR = 0, 2, 3, 4, 5, 6
RECEIPT_KEYS = {
    "schema_version", "run_id", "slice_id", "ledger_revision", "ledger_event_hash",
    "artifact_contract_sha256", "allowed_paths", "captured_at", "git",
}


class Parser(argparse.ArgumentParser):
    """Argument parser emitting the shared typed error contract."""

    def error(self, message: str) -> None:
        raise GitFactError(message, USAGE)


def _positive(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = Parser(description=__doc__)
    parser.add_argument("--cwd", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "attribute"):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)
        command.add_argument("--session-id", required=True)
        command.add_argument("--revision", type=_positive, required=True)
    return parser


def _emit(ok: bool, kind: str, *, stream: Any = sys.stdout, **values: Any) -> None:
    print(json.dumps({"ok": ok, "type": kind, **values}, sort_keys=True, separators=(",", ":")), file=stream)


def _ledger(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state_dir = root / ".claude" / "state"
    ledger, events = state_dir / "slice_ledger.json", state_dir / "slice_events.jsonl"
    state, _ = _load(ledger, events)
    if state is None or state["state"] != "active":
        raise GitFactError("active slice ledger required", CONFLICT)
    if (
        state["run_id"] != args.run_id or state["owner"]["session_id"] != args.session_id
        or state["revision"] != args.revision
    ):
        raise GitFactError("run/session/revision guard conflict", CONFLICT)
    return state


def _run_dir(root: Path, run_id: str) -> Path:
    directory = root / ".claude/state/runs" / hashlib.sha256(run_id.encode()).hexdigest()
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise GitFactError("run state directory is unsafe", CORRUPT)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _receipt_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "slice_baseline.json"


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _validate_receipt_file(path: Path) -> None:
    if path.is_symlink():
        raise GitFactError("baseline receipt symlink forbidden", CORRUPT)
    if path.exists():
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
            raise GitFactError("baseline receipt must be regular, single-link, mode 0600", CORRUPT)


def _read_receipt(path: Path) -> dict[str, Any]:
    _validate_receipt_file(path)
    if not path.exists():
        raise GitFactError("slice baseline not captured", CONFLICT)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened, current = os.fstat(fd), os.stat(path, follow_symlinks=False)
        if opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise GitFactError("baseline receipt inode/path mismatch", CORRUPT)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        value = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitFactError("corrupt baseline receipt JSON", CORRUPT) from exc
    if (
        not isinstance(value, dict) or set(value) != RECEIPT_KEYS
        or not isinstance(value["schema_version"], int) or isinstance(value["schema_version"], bool)
        or value["schema_version"] != 1
    ):
        raise GitFactError("baseline receipt schema mismatch", CORRUPT)
    if not isinstance(value["ledger_revision"], int) or isinstance(value["ledger_revision"], bool) or value["ledger_revision"] < 1:
        raise GitFactError("invalid baseline revision", CORRUPT)
    for key in ("run_id", "slice_id", "ledger_event_hash", "artifact_contract_sha256", "captured_at"):
        if not isinstance(value[key], str) or not value[key]:
            raise GitFactError(f"invalid baseline {key}", CORRUPT)
    if (
        not isinstance(value["allowed_paths"], list)
        or [_validate_relpath(path) for path in value["allowed_paths"]] != value["allowed_paths"]
        or value["allowed_paths"] != sorted(set(value["allowed_paths"]))
    ):
        raise GitFactError("invalid baseline allowed_paths", CORRUPT)
    if not isinstance(value["git"], dict):
        raise GitFactError("invalid baseline git snapshot", CORRUPT)
    return value


def _binding(ledger: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": ledger["run_id"], "slice_id": ledger["slice_id"],
        "ledger_revision": ledger["revision"], "ledger_event_hash": ledger["last_event_hash"],
        "artifact_contract_sha256": hashlib.sha256(ledger["artifact_contract"].encode()).hexdigest(),
        "allowed_paths": ledger["allowed_paths"],
    }


def _assert_binding(receipt: dict[str, Any], ledger: dict[str, Any]) -> None:
    expected = _binding(ledger)
    stable_keys = ("run_id", "slice_id", "artifact_contract_sha256", "allowed_paths")
    if any(receipt[key] != expected[key] for key in stable_keys):
        raise GitFactError("ledger facts changed since baseline capture", CONFLICT)
    receipt_hash = hashlib.sha256(_canonical(receipt)).hexdigest()
    if ledger["baseline_sha256"] != receipt_hash:
        raise GitFactError("baseline receipt disagrees with authoritative ledger hash", CORRUPT)


def _capture(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    path = _receipt_path(root, args.run_id)
    _validate_receipt_file(path)
    # Exact retry after binding accepts the original expected revision.
    state_dir = root / ".claude" / "state"
    current, _ = _load(state_dir / "slice_ledger.json", state_dir / "slice_events.jsonl")
    if current and current["revision"] == args.revision + 1 and current["baseline_sha256"] and path.exists():
        existing = _read_receipt(path)
        if current["run_id"] == args.run_id and current["owner"]["session_id"] == args.session_id and hashlib.sha256(_canonical(existing)).hexdigest() == current["baseline_sha256"]:
            return existing
    ledger = _ledger(root, args)
    facts = snapshot(root, ledger["allowed_paths"])
    receipt = {"schema_version": 1, **_binding(ledger), "captured_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"), "git": facts}
    if path.exists():
        existing = _read_receipt(path)
        # Timestamp is not part of idempotency; all authoritative facts must match.
        if {k: v for k, v in existing.items() if k != "captured_at"} == {k: v for k, v in receipt.items() if k != "captured_at"}:
            baseline_sha256 = hashlib.sha256(_canonical(existing)).hexdigest()
            bind_args = argparse.Namespace(run_id=args.run_id, session_id=args.session_id, revision=args.revision, baseline_sha256=baseline_sha256, baseline_path=_relative(root, path))
            _bind_baseline(bind_args, root / ".claude/state/slice_ledger.json", root / ".claude/state/slice_events.jsonl")
            return existing
        raise GitFactError("immutable baseline already exists with different facts", CONFLICT)
    _atomic_projection(path, receipt)
    baseline_sha256 = hashlib.sha256(_canonical(receipt)).hexdigest()
    bind_args = argparse.Namespace(run_id=args.run_id, session_id=args.session_id, revision=args.revision, baseline_sha256=baseline_sha256, baseline_path=_relative(root, path))
    _bind_baseline(bind_args, root / ".claude/state/slice_ledger.json", root / ".claude/state/slice_events.jsonl")
    return receipt


def _attribute(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    ledger = _ledger(root, args)
    baseline = _read_receipt(_receipt_path(root, args.run_id))
    _assert_binding(baseline, ledger)
    current = snapshot(root, ledger["allowed_paths"])
    return {**attribution_receipt(ledger, baseline, current), "candidate_owned_is_authorship": False, "current": current}


def current_attribution(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    """Return exact current attribution facts for verification consumers."""
    baseline = _read_receipt(_receipt_path(root, ledger["run_id"]))
    _assert_binding(baseline, ledger)
    return attribution_receipt(ledger, baseline, snapshot(root, ledger["allowed_paths"]))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        root = _git_root(args.cwd)
        with _locked(root):
            result = _capture(root, args) if args.command == "capture" else _attribute(root, args)
        _emit(True, args.command, result=result)
        return OK
    except (GitFactError, LedgerError) as exc:
        _emit(False, "error", stream=sys.stderr, code=exc.code, error=str(exc))
        return exc.code
    except OSError as exc:
        _emit(False, "error", stream=sys.stderr, code=IO_ERROR, error=f"filesystem error: {exc}")
        return IO_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
