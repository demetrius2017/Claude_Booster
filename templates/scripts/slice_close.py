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

from slice_close_core import EVIDENCE_KEYS, VerifyError, append_backlog, backlog_state, build_handoff, canonical, read_secure_json, run_verifier, validate_evidence, validate_exclusions
from slice_git import _relative, _run_dir, current_attribution
from slice_ledger import _bind_verification, _git_root, _locked
from slice_ledger_core import LedgerError, _append, _atomic_projection, _load, _now


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
    close = subs.add_parser("close")
    close.add_argument("--run-id", required=True)
    close.add_argument("--session-id", required=True)
    close.add_argument("--revision", type=_positive, required=True)
    close.add_argument("--disposition", choices=("committed", "quarantined", "delivered_uncommitted", "blocked"), required=True)
    close.add_argument("--delivered-path", action="append", default=[])
    close.add_argument("--exclude", action="append", default=[])
    close.add_argument("--commit-oid")
    close.add_argument("--blocked-category", choices=("verification_failed", "ambiguous_state", "external_blocker", "other"))
    close.add_argument("--blocked-reason")
    close.add_argument("--next-safe-action")
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
    state_dir = root / ".claude/state"
    state, _ = _load(state_dir / "slice_ledger.json", state_dir / "slice_events.jsonl")
    if state is None or state["state"] not in {"active", "closed"} or state["run_id"] != args.run_id or state["owner"]["session_id"] != args.session_id or state["revision"] != args.revision:
        raise VerifyError("run/session/revision status guard conflict", 3)
    receipt = _read_receipt(_receipt_path(root, args.run_id))
    # Status uses the post-bind revision; receipt records its predecessor.
    predecessor_args = argparse.Namespace(session_id=args.session_id, revision=args.revision - (2 if state["state"] == "closed" else 1))
    _assert_identity(receipt, state, predecessor_args)
    if hashlib.sha256(canonical(receipt)).hexdigest() != state["verification_sha256"]:
        raise VerifyError("verification receipt disagrees with authoritative event", 4)
    current = current_attribution(root, state)
    return {"receipt": receipt, "stale": current["state_sha256"] != state["verification_state_sha256"], "current_state_sha256": current["state_sha256"]}


def _git(root: Path, *args: str, allow: tuple[int, ...] = (0,)) -> bytes:
    import subprocess
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode not in allow:
        raise VerifyError(f"git {' '.join(args)} failed", 3)
    return result.stdout


def _git_result(root: Path, *args: str, allow: tuple[int, ...] = (0,)) -> tuple[int, bytes]:
    import subprocess
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode not in allow:
        raise VerifyError(f"git {' '.join(args)} failed", 3)
    return result.returncode, result.stdout


def _exclusions(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise VerifyError("exclusions use PATH=REASON", 2)
        path, reason = item.split("=", 1)
        if path in result:
            raise VerifyError("duplicate exclusion", 2)
        result[path] = reason
    return result


def _close_request(args: argparse.Namespace, exclusions: dict[str, str], revision: int) -> dict[str, Any]:
    return {
        "run_id": args.run_id, "session_id": args.session_id, "revision": revision,
        "disposition": args.disposition, "delivered_paths": sorted(args.delivered_path),
        "exclusions": [{"path": path, "reason": exclusions[path]} for path in sorted(exclusions)],
        "commit_oid": args.commit_oid, "blocked_category": args.blocked_category,
        "blocked_reason": args.blocked_reason, "next_safe_action": args.next_safe_action,
    }


def _commit_proof(root: Path, state: dict[str, Any], attribution: dict[str, Any], oid: str | None) -> str:
    object_format = attribution["anchors"]["object_format"]
    length = 40 if object_format == "sha1" else 64
    if not oid or len(oid) != length or any(char not in "0123456789abcdef" for char in oid):
        raise VerifyError("committed requires exact full lowercase OID", 2)
    if _git(root, "cat-file", "-t", oid).strip() != b"commit" or _git(root, "rev-parse", "HEAD").strip().decode() != oid:
        raise VerifyError("commit OID must be current HEAD commit", 3)
    baseline_head = attribution["anchors"]["head"]
    if _git(root, "rev-parse", f"{oid}^").strip().decode() != baseline_head:
        raise VerifyError("MVP requires verified baseline as direct parent", 3)
    raw_paths = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", baseline_head, oid)
    commit_paths = {part.decode("utf-8", "strict") for part in raw_paths.split(b"\0") if part}
    candidates = {item["path"] for item in attribution["classifications"] if item["classification"] == "candidate-owned"}
    if not candidates or commit_paths != candidates:
        raise VerifyError("commit path set must exactly equal verified candidates", 3)
    facts = attribution["scoped_facts"]
    for path in candidates:
        fact = facts[path]
        exists, blob = _git_result(root, "show", f"{oid}:{path}", allow=(0, 128))
        if fact["kind"] == "absent":
            if exists == 0:
                raise VerifyError("verified deletion still exists in commit", 3)
        elif exists != 0 or fact["kind"] != "regular" or fact["hash_status"] != "hashed" or __import__("hashlib").sha256(blob).hexdigest() != fact["sha256"]:
            raise VerifyError("commit blob differs from verified fact", 3)
    if _git(root, "status", "--porcelain=v2", "-z", "--", *sorted(candidates)):
        raise VerifyError("candidate index/worktree is not clean after commit", 3)
    docs = lambda path: path.endswith(".md") or path.startswith(("docs/", "reports/"))
    commit_class = "docs" if all(docs(path) for path in candidates) else "mixed" if any(docs(path) for path in candidates) else "implementation"
    implementation_contract = any(not docs(path) for path in state["allowed_paths"])
    if implementation_contract and commit_class == "docs":
        raise VerifyError("docs-only commit cannot satisfy implementation contract", 3)
    return commit_class


def _close(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state_dir = root / ".claude/state"
    ledger_path, events_path = state_dir / "slice_ledger.json", state_dir / "slice_events.jsonl"
    state, events = _load(ledger_path, events_path)
    run_dir = _run_dir(root, args.run_id)
    handoff_path = run_dir / "slice_handoff.json"
    supplied_exclusions = _exclusions(args.exclude)
    request = _close_request(args, supplied_exclusions, state["revision"] if state and state["state"] == "closed" else args.revision + 1)
    if state and state["state"] == "closed":
        if (state["run_id"] != args.run_id or state["owner"]["session_id"] != args.session_id
                or state["revision"] != args.revision or state["terminal_disposition"] != args.disposition):
            raise VerifyError("terminal closure is immutable", 3)
        backlog_path = root / state["closure"]["backlog_path"]
        if backlog_state(backlog_path) != (state["closure"]["backlog_tail_hash"], state["closure"]["backlog_count"]):
            raise VerifyError("terminal backlog disagrees with closure event", 4)
        handoff = _read_handoff(handoff_path, state["handoff_sha256"])
        if handoff["claims"].get("close_request") != request:
            raise VerifyError("close retry request conflicts with authoritative closure", 3)
        return handoff
    if state is None or state["state"] != "active" or state["run_id"] != args.run_id or state["owner"]["session_id"] != args.session_id or state["revision"] != args.revision or state["verification_sha256"] is None:
        raise VerifyError("verified active run/session/revision required", 3)
    receipt = _read_receipt(_receipt_path(root, args.run_id))
    predecessor = argparse.Namespace(session_id=args.session_id, revision=args.revision - 1)
    _assert_identity(receipt, state, predecessor)
    if __import__("hashlib").sha256(canonical(receipt)).hexdigest() != state["verification_sha256"]:
        raise VerifyError("verification receipt hash mismatch", 4)
    current = current_attribution(root, state)
    verified = receipt["attribution"]
    fresh = current["state_sha256"] == state["verification_state_sha256"]
    exclusions = dict(supplied_exclusions)
    delivered = set(args.delivered_path)
    commit_class, blocked = None, None
    if args.disposition == "committed":
        if receipt["status"] != "pass":
            raise VerifyError("committed requires passing verification", 3)
        commit_class = _commit_proof(root, state, verified, args.commit_oid)
        delivered = {item["path"] for item in verified["classifications"] if item["classification"] == "candidate-owned"}
        validate_exclusions(verified["classifications"], delivered, exclusions)
        current = current_attribution(root, state)
        verified_by_path = {item["path"]: item for item in verified["classifications"]}
        candidates = delivered
        for item in current["classifications"]:
            path = item["path"]
            if path in candidates:
                continue
            old = verified_by_path.get(path)
            unchanged_foreign = (
                old is not None and old["classification"] == "foreign" and path in exclusions
                and item["current"] == old["current"]
            )
            if not unchanged_foreign:
                raise VerifyError(f"unclassified post-verification delta: {path}", 3)
        closure_attribution = {**current, "classifications": verified["classifications"]}
    elif args.disposition == "delivered_uncommitted":
        if receipt["status"] != "pass" or not fresh:
            raise VerifyError("delivered_uncommitted requires fresh passing verification", 3)
        candidates = {item["path"] for item in verified["classifications"] if item["classification"] == "candidate-owned"}
        delivered = delivered or candidates
        if not delivered:
            raise VerifyError("delivered_uncommitted requires candidates", 3)
        validate_exclusions(verified["classifications"], delivered, exclusions)
    elif args.disposition == "quarantined":
        if not fresh or (delivered and receipt["status"] != "pass"):
            raise VerifyError("quarantine delivery requires fresh state and pass", 3)
        validate_exclusions(verified["classifications"], delivered, exclusions)
    else:
        if not fresh or not args.blocked_category or not args.blocked_reason or not args.next_safe_action:
            raise VerifyError("blocked requires fresh typed reason and next safe action", 3)
        blocked = {"category": args.blocked_category, "reason": args.blocked_reason, "next_safe_action": args.next_safe_action}
        delivered = set()
        exclusions = {item["path"]: exclusions.get(item["path"], "blocked unresolved fact") for item in verified["classifications"]}
        validate_exclusions(verified["classifications"], delivered, exclusions)
    offscope = [item["path"] for item in verified["classifications"] if item["classification"] == "off-scope"]
    timestamp = receipt["claim"]["ended_at"]
    backlog_path = run_dir / "slice_backlog.jsonl"
    tail, count = append_backlog(backlog_path, state["run_id"], state["slice_id"], verified["state_sha256"], offscope, timestamp)
    closure_attribution = closure_attribution if args.disposition == "committed" else verified
    handoff = build_handoff(state, args.disposition, closure_attribution, delivered, exclusions, args.commit_oid, commit_class, tail, count, blocked, timestamp, request)
    handoff_hash = __import__("hashlib").sha256(canonical(handoff)).hexdigest()
    if handoff_path.exists():
        existing = _read_handoff(handoff_path, handoff_hash)
        if existing != handoff:
            raise VerifyError("orphan handoff conflicts with deterministic closure", 4)
    else:
        _atomic_projection(handoff_path, handoff)
    payload = {"run_id": state["run_id"], "revision": state["revision"] + 1, "updated_at": _now(), "disposition": args.disposition, "state_sha256": closure_attribution["state_sha256"], "verification_sha256": state["verification_sha256"], "commit_oid": args.commit_oid, "excluded_paths": sorted(exclusions), "backlog_path": _relative(root, backlog_path), "backlog_tail_hash": tail, "backlog_count": count, "handoff_path": _relative(root, handoff_path), "handoff_sha256": handoff_hash, "commit_class": commit_class}
    event = _append(events_path, "closed", payload, events)
    state.update(state="closed", terminal_disposition=args.disposition, closure=dict(payload), handoff_sha256=handoff_hash, revision=payload["revision"], updated_at=payload["updated_at"], last_event_hash=event["hash"])
    _atomic_projection(ledger_path, state)
    return handoff


def _read_handoff(path: Path, expected_hash: str) -> dict[str, Any]:
    value = read_secure_json(path, max_bytes=64 * 1024, expected_keys={"schema_version", "run_id", "slice_id", "disposition", "facts", "claims", "paths", "unknowns", "coverage", "created_at"})
    if __import__("hashlib").sha256(canonical(value)).hexdigest() != expected_hash:
        raise VerifyError("handoff hash mismatch", 4)
    return value


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        root = _git_root(args.cwd)
        with _locked(root):
            result = _verify(root, args) if args.command == "verify" else _close(root, args) if args.command == "close" else _status(root, args)
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
