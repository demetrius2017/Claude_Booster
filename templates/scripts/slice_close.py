#!/usr/bin/env python3
"""Exact-state verification transaction CLI for Slice 3A.

Purpose: Run an argv-only verifier, record bounded evidence, and bind its
immutable receipt into the authoritative slice event chain.
Contract: Exact run/session/revision guards apply; PASS requires exit zero and
identical pre/post attribution state; every receipt read is checked against the
ledger-bound canonical SHA-256.
CLI/Examples: ``slice_close.py --cwd ROOT verify --run-id R --session-id S
--revision N --evidence-file evidence.json`` or ``status`` after binding.
Limitations: No closure dispositions, commit proof, quarantine, backlog,
handoff, telemetry, hooks, autopilot integration, or shell execution.
ENV/Files: Uses normal executable lookup and writes only mode-0600
``<git-root>/.claude/state/runs/<run-hash>/slice_verification.json``.
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

from slice_close_core import EVIDENCE_KEYS, VerifyError, canonical, read_secure_json, run_verifier, validate_evidence
from slice_git import _relative, _run_dir, current_attribution
from slice_ledger import _bind_verification, _git_root, _locked
from slice_ledger_core import LedgerError, _atomic_projection, _load


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise VerifyError(message, 2)


def _positive(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be positive") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = Parser(description=__doc__)
    parser.add_argument("--cwd", default=".")
    subs = parser.add_subparsers(dest="command", required=True)
    verify = subs.add_parser("verify")
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--session-id", required=True)
    verify.add_argument("--revision", type=_positive, required=True)
    verify.add_argument("--evidence-file", required=True)
    status_cmd = subs.add_parser("status")
    status_cmd.add_argument("--run-id", required=True)
    status_cmd.add_argument("--session-id", required=True)
    status_cmd.add_argument("--revision", type=_positive, required=True)
    return parser


def _emit(ok: bool, kind: str, *, stream: Any = sys.stdout, **values: Any) -> None:
    print(json.dumps({"ok": ok, "type": kind, **values}, sort_keys=True, separators=(",", ":")), file=stream)


def _state(root: Path, args: argparse.Namespace, *, allow_retry: bool = False) -> dict[str, Any]:
    directory = root / ".claude/state"
    state, _ = _load(directory / "slice_ledger.json", directory / "slice_events.jsonl")
    expected = args.revision + 1 if allow_retry and state and state["verification_sha256"] else args.revision
    if state is None or state["state"] != "active" or state["run_id"] != args.run_id or state["owner"]["session_id"] != args.session_id or state["revision"] != expected:
        raise VerifyError("active run/session/revision guard conflict", 3)
    if state["baseline_sha256"] is None:
        raise VerifyError("baseline binding required", 3)
    return state


def _receipt_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "slice_verification.json"


def _read_receipt(path: Path) -> dict[str, Any]:
    _validate_receipt_path(path)
    value = read_secure_json(path, max_bytes=1024 * 1024, expected_keys={"schema_version", "status", "facts", "claim", "attribution", "identity", "limitations"})
    if value["schema_version"] != 1 or value["status"] not in {"pass", "fail"}:
        raise VerifyError("verification receipt schema mismatch", 4)
    expected_limits = {"observation_model": "pre_post_snapshot", "transient_mutation_detection": False, "external_side_effect_detection": False, "future_stability": False}
    if value["limitations"] != expected_limits:
        raise VerifyError("verification limitations schema mismatch", 4)
    if set(value["facts"]) != {"pre_state_sha256", "post_state_sha256", "state_unchanged"} or not isinstance(value["facts"]["state_unchanged"], bool):
        raise VerifyError("verification facts schema mismatch", 4)
    claim_keys = {"argv", "resolved_executable", "executable_before", "executable_after", "started_at", "ended_at", "exit_code", "timed_out", "stdout", "stderr", "environment_keys"}
    if not isinstance(value["claim"], dict) or set(value["claim"]) != claim_keys:
        raise VerifyError("verification claim schema mismatch", 4)
    output_keys = {"bytes", "sha256", "content", "truncated", "limit_exceeded"}
    if any(not isinstance(value["claim"].get(name), dict) or set(value["claim"][name]) != output_keys for name in ("stdout", "stderr")):
        raise VerifyError("verification output schema mismatch", 4)
    identity_keys = {"run_id", "slice_id", "session_id", "expected_revision", "artifact_contract_sha256", "evidence_sha256"}
    if not isinstance(value["identity"], dict) or set(value["identity"]) != identity_keys:
        raise VerifyError("verification identity schema mismatch", 4)
    observed_pass = (
        value["claim"]["exit_code"] == 0 and value["claim"]["timed_out"] is False
        and value["facts"]["state_unchanged"] is True
        and value["claim"]["executable_before"] == value["claim"]["executable_after"]
        and not value["claim"]["stdout"]["limit_exceeded"] and not value["claim"]["stderr"]["limit_exceeded"]
    )
    if (value["status"] == "pass") != observed_pass:
        raise VerifyError("verification status contradicts observed facts", 4)
    return value


def _validate_receipt_path(path: Path) -> None:
    if path.is_symlink():
        raise VerifyError("verification receipt symlink forbidden", 4)
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise VerifyError("verification receipt must be regular, single-link, mode 0600", 4)


def _assert_identity(receipt: dict[str, Any], state: dict[str, Any], args: argparse.Namespace) -> None:
    identity = receipt["identity"]
    if (
        identity["run_id"] != state["run_id"] or identity["slice_id"] != state["slice_id"]
        or identity["session_id"] != args.session_id or identity["expected_revision"] != args.revision
        or identity["artifact_contract_sha256"] != hashlib.sha256(state["artifact_contract"].encode()).hexdigest()
        or receipt["attribution"]["run_id"] != state["run_id"]
    ):
        raise VerifyError("verification receipt identity mismatch", 4)


def _bind(root: Path, args: argparse.Namespace, receipt: dict[str, Any]) -> None:
    verification_sha = hashlib.sha256(canonical(receipt)).hexdigest()
    path = _receipt_path(root, args.run_id)
    bind_args = argparse.Namespace(run_id=args.run_id, session_id=args.session_id, revision=args.revision, verification_sha256=verification_sha, state_sha256=receipt["facts"]["pre_state_sha256"], verification_path=_relative(root, path))
    state_dir = root / ".claude/state"
    _bind_verification(bind_args, state_dir / "slice_ledger.json", state_dir / "slice_events.jsonl")


def _verify(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    path = _receipt_path(root, args.run_id)
    _validate_receipt_path(path)
    state = _state(root, args, allow_retry=True)
    if path.exists():
        if not state["verification_sha256"]:
            raise VerifyError("unbound preexisting verification receipt is untrusted", 3)
        receipt = _read_receipt(path)
        _assert_identity(receipt, state, args)
        digest = hashlib.sha256(canonical(receipt)).hexdigest()
        if state["verification_sha256"]:
            if digest != state["verification_sha256"]:
                raise VerifyError("verification receipt disagrees with authoritative event", 4)
            return receipt
    if state["verification_sha256"]:
        raise VerifyError("bound verification receipt is missing", 4)
    evidence = validate_evidence(read_secure_json(Path(args.evidence_file), max_bytes=32 * 1024, expected_keys=EVIDENCE_KEYS))
    receipt = run_verifier(root, evidence, lambda: current_attribution(root, state))
    receipt["identity"] = {"run_id": state["run_id"], "slice_id": state["slice_id"], "session_id": args.session_id, "expected_revision": args.revision, "artifact_contract_sha256": hashlib.sha256(state["artifact_contract"].encode()).hexdigest(), "evidence_sha256": hashlib.sha256(canonical(evidence)).hexdigest()}
    if len(canonical(receipt)) > 1024 * 1024:
        raise VerifyError("verification receipt exceeds 1 MiB", 5)
    _atomic_projection(path, receipt)
    _bind(root, args, receipt)
    return receipt


def _status(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state = _state(root, args)
    receipt = _read_receipt(_receipt_path(root, args.run_id))
    # Status uses the post-bind revision; receipt records its predecessor.
    predecessor_args = argparse.Namespace(session_id=args.session_id, revision=args.revision - 1)
    _assert_identity(receipt, state, predecessor_args)
    if hashlib.sha256(canonical(receipt)).hexdigest() != state["verification_sha256"]:
        raise VerifyError("verification receipt disagrees with authoritative event", 4)
    current = current_attribution(root, state)
    return {"receipt": receipt, "stale": current["state_sha256"] != state["verification_state_sha256"], "current_state_sha256": current["state_sha256"]}


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        root = _git_root(args.cwd)
        with _locked(root):
            result = _verify(root, args) if args.command == "verify" else _status(root, args)
        _emit(True, args.command, result=result)
        return 0
    except (VerifyError, LedgerError) as exc:
        _emit(False, "error", stream=sys.stderr, code=exc.code, error=str(exc))
        return exc.code
    except OSError as exc:
        _emit(False, "error", stream=sys.stderr, code=6, error=f"filesystem error: {exc}")
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
