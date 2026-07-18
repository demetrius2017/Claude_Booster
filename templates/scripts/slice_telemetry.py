#!/usr/bin/env python3
"""Slice 4A diagnostic telemetry CLI.

Purpose: Inspect explicit versioned transcripts and optionally bind a compact,
privacy-preserving receipt to an exact slice ledger generation.
Contract: ``inspect`` is read-only; ``record`` writes one immutable run receipt
and one hash-chained calibration row; ``status`` validates both before output.
CLI/Examples: ``slice_telemetry.py --cwd ROOT inspect --provider
codex_rollout_v1 --transcript rollout.jsonl --run-id R --session-id S``.
Limitations: Advisory diagnostics only; no autopilot integration, policy gates,
native Codex control, semantic scoring, or transcript discovery.
ENV/Files: Writes only project-local ``.claude/state/runs`` receipt and
``.claude/state/slice_calibration.jsonl`` under the shared slice lock.
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

from slice_close_core import read_secure_json
from slice_git import _relative, _run_dir
from slice_ledger import _git_root, _locked
from slice_ledger_core import LedgerError, _atomic_projection, _load
from slice_telemetry_core import TelemetryError, canonical, digest, parse, report, timestamp

RECEIPT_KEYS = {"schema_version", "run_id_hash", "session_id_hash", "project_hash", "ledger", "observation"}
LOG_KEYS = {"schema_version", "run_id_hash", "receipt_sha256", "ledger_event_hash", "previous_hash", "hash"}


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise TelemetryError(message, 2)


def _parser() -> argparse.ArgumentParser:
    parser = Parser(description=__doc__)
    parser.add_argument("--cwd", default=".")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "record"):
        command = subs.add_parser(name)
        command.add_argument("--provider", required=True, choices=("codex_rollout_v1", "booster_wrapper_v1"))
        command.add_argument("--transcript", action="append", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--session-id", required=True)
    status = subs.add_parser("status")
    status.add_argument("--run-id", required=True)
    status.add_argument("--session-id", required=True)
    return parser


def _emit(ok: bool, kind: str, *, stream: Any = sys.stdout, **values: Any) -> None:
    print(json.dumps({"ok": ok, "type": kind, **values}, sort_keys=True, separators=(",", ":")), file=stream)


def _ledger(root: Path, run_id: str, session_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state_dir = root / ".claude/state"
    state, events = _load(state_dir / "slice_ledger.json", state_dir / "slice_events.jsonl")
    if state is None or state["run_id"] != run_id or state["owner"]["session_id"] != session_id:
        raise TelemetryError("exact run/session ledger join required", 3)
    return state, events


def _authoritative(root: Path, state: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    result = {"revision": state["revision"], "state": state["state"], "event_tail_hash": state["last_event_hash"], "baseline_sha256": state["baseline_sha256"], "verification_sha256": state["verification_sha256"], "handoff_sha256": state["handoff_sha256"], "terminal_disposition": state["terminal_disposition"], "commit_oid": None, "commit_class": None}
    if state["state"] == "closed":
        closure = state["closure"]
        handoff_path = root / closure["handoff_path"]
        handoff = read_secure_json(handoff_path, max_bytes=64 * 1024, expected_keys={"schema_version", "run_id", "slice_id", "disposition", "facts", "claims", "paths", "unknowns", "coverage", "created_at"})
        if hashlib.sha256(canonical(handoff)).hexdigest() != state["handoff_sha256"] or handoff["run_id"] != state["run_id"]:
            raise TelemetryError("handoff hash/identity mismatch", 4)
        result.update(commit_oid=closure["commit_oid"], commit_class=closure["commit_class"], closure_state_sha256=closure["state_sha256"], offscope_count=len(handoff["paths"]["off-scope"]), backlog_count=closure["backlog_count"])
    result["event_count"] = len(events)
    return result


def _coverage_metric(value: Any, status: str, reason: str | None, evidence: list[str], denominator: int = 1) -> dict[str, Any]:
    observed = int(value is not None)
    return {"value": value, "denominator": denominator, "observed": observed, "expected": denominator, "coverage_status": status, "unknown_reasons": [reason] if reason else [], "evidence_set_ids": evidence, "evidence_counts": {"recognized": observed, "unknown": denominator - observed}}


def _event_time(events: list[dict[str, Any]], event_type: str) -> tuple[float | None, str | None]:
    matches = [(index, timestamp(item.get("payload", {}).get("updated_at"))) for index, item in enumerate(events) if item.get("type") == event_type]
    valid = [(index, value) for index, value in matches if value is not None]
    return (valid[0][1], f"ledger_event:{valid[0][0]}:{event_type}") if len(valid) == 1 else (None, None)


def _receipt(root: Path, relative: str | None, expected_hash: str | None, keys: set[str], limit: int) -> dict[str, Any] | None:
    if not relative or not expected_hash:
        return None
    value = read_secure_json(root / relative, max_bytes=limit, expected_keys=keys)
    if hashlib.sha256(canonical(value)).hexdigest() != expected_hash:
        raise TelemetryError("authoritative receipt hash mismatch", 4)
    return value


def _joined_metrics(root: Path, state: dict[str, Any], events: list[dict[str, Any]], observation: dict[str, Any]) -> dict[str, Any]:
    activation, activation_ref = _event_time(events, "acquired")
    verification, verification_ref = _event_time(events, "verification_bound")
    closed, closed_ref = _event_time(events, "closed")
    session_start = observation["clock_facts"]["session_start"]
    worker = observation["clock_facts"]["first_worker"]

    def delay(later: float | None, earlier: float | None, refs: list[str]) -> dict[str, Any]:
        if later is None or earlier is None:
            return _coverage_metric(None, "partial", "missing_clock_endpoint", refs)
        if later < earlier:
            return _coverage_metric(None, "partial", "clock_skew_negative_duration", refs)
        return _coverage_metric(round(later - earlier, 6), "complete", None, refs)

    baseline_keys = {"schema_version", "run_id", "slice_id", "ledger_revision", "ledger_event_hash", "artifact_contract_sha256", "allowed_paths", "captured_at", "git"}
    verification_keys = {"schema_version", "status", "facts", "claim", "attribution", "identity", "limitations"}
    baseline = _receipt(root, state.get("baseline_path"), state.get("baseline_sha256"), baseline_keys, 2 * 1024 * 1024)
    verified = _receipt(root, state.get("verification_path"), state.get("verification_sha256"), verification_keys, 1024 * 1024)
    dirty = None
    if baseline:
        dirty = sum(1 for item in baseline["git"]["entries"] if item.get("path") not in {".claude", ".claude/state"} and not str(item.get("path", "")).startswith(".claude/state/"))
    classes = None
    if verified:
        classes = {name: 0 for name in ("candidate-owned", "foreign", "ambiguous", "off-scope")}
        for item in verified["attribution"]["classifications"]:
            if item.get("classification") not in classes:
                raise TelemetryError("unknown authoritative attribution class", 4)
            classes[item["classification"]] += 1
    commit_delay = None
    commit_reason = "no_authoritative_committed_closure"
    commit_ref: list[str] = []
    if state["state"] == "closed" and state["closure"]["commit_oid"]:
        import subprocess
        oid = state["closure"]["commit_oid"]
        process = subprocess.run(["git", "-C", str(root), "show", "-s", "--format=%cI", oid], capture_output=True, text=True, check=False)
        committed_at = timestamp(process.stdout.strip()) if process.returncode == 0 else None
        if committed_at is not None and activation is not None and committed_at >= activation:
            commit_delay, commit_reason, commit_ref = round(committed_at - activation, 6), None, ["closure:commit_oid"]
        else:
            commit_reason = "commit_clock_missing_or_before_activation"
    closure_value = None if state["state"] != "closed" else {"disposition": state["terminal_disposition"], "commit_class": state["closure"]["commit_class"]}
    observation["evidence_index"].extend([
        {"evidence_set_id": "ledger_events", "source_sha256": state["last_event_hash"], "recognized": {"count": len(events), "ranges": [f"1-{len(events)}"] if events else [], "ranges_truncated": False, "ranges_sha256": hashlib.sha256(canonical(list(range(1, len(events) + 1)))).hexdigest()}, "unknown": {"count": 0, "ranges": [], "ranges_truncated": False, "ranges_sha256": hashlib.sha256(canonical([])).hexdigest()}},
        {"evidence_set_id": "baseline_receipt", "source_sha256": state["baseline_sha256"], "recognized": {"count": int(baseline is not None), "ranges": ["1"] if baseline else [], "ranges_truncated": False, "ranges_sha256": hashlib.sha256(canonical([1] if baseline else [])).hexdigest()}, "unknown": {"count": int(baseline is None), "ranges": [], "ranges_truncated": False, "ranges_sha256": hashlib.sha256(canonical([])).hexdigest()}},
        {"evidence_set_id": "verification_receipt", "source_sha256": state["verification_sha256"], "recognized": {"count": int(verified is not None), "ranges": ["1"] if verified else [], "ranges_truncated": False, "ranges_sha256": hashlib.sha256(canonical([1] if verified else [])).hexdigest()}, "unknown": {"count": int(verified is None), "ranges": [], "ranges_truncated": False, "ranges_sha256": hashlib.sha256(canonical([])).hexdigest()}},
    ])
    return {
        "activation_delay_seconds": delay(activation, session_start, [item for item in (activation_ref, "transcript:session_start") if item]),
        "first_worker_delay_seconds": delay(worker, activation, [item for item in (activation_ref, "transcript:first_worker") if item]),
        "first_verification_delay_seconds": delay(verification, activation, [item for item in (activation_ref, verification_ref) if item]),
        "first_implementation_commit_delay_seconds": _coverage_metric(commit_delay, "complete" if commit_delay is not None else "partial", commit_reason, commit_ref),
        "baseline_dirty": _coverage_metric(dirty, "complete" if dirty is not None else "partial", None if dirty is not None else "baseline_unavailable", ["baseline_receipt"] if baseline else []),
        "dirty_delta_classes": _coverage_metric(classes, "complete" if classes is not None else "partial", None if classes is not None else "verification_unavailable", ["verification_receipt"] if verified else []),
        "scope_drift": _coverage_metric(classes["off-scope"] if classes else None, "complete" if classes is not None else "partial", None if classes is not None else "verification_unavailable", ["verification_receipt"] if verified else []),
        "slice_closure": _coverage_metric(closure_value, "complete" if closure_value is not None else "right_censored", None if closure_value is not None else "slice_not_terminal", [closed_ref] if closed_ref else []),
    }


def _build(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state, events = _ledger(root, args.run_id, args.session_id)
    observation = report(parse(args.provider, [Path(item) for item in args.transcript], root))
    if observation["root_session_hash"] != digest(args.session_id):
        raise TelemetryError("transcript root session does not match ledger owner", 3)
    if observation["project_hash"] != digest(str(root.resolve())):
        raise TelemetryError("transcript project does not match ledger root", 3)
    observation["metrics"].update(_joined_metrics(root, state, events, observation))
    receipt = {"schema_version": 1, "run_id_hash": digest(args.run_id), "session_id_hash": digest(args.session_id), "project_hash": digest(str(root.resolve())), "ledger": _authoritative(root, state, events), "observation": observation}
    if len(canonical(receipt)) > 256 * 1024:
        raise TelemetryError("telemetry observation exceeds 256 KiB bound", 5)
    return receipt


def _log_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise TelemetryError("calibration log must be regular mode 0600", 4)
    rows, previous = [], "0" * 64
    for number, raw in enumerate(path.read_bytes().splitlines(), start=1):
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TelemetryError(f"calibration log corrupt at row {number}", 4) from exc
        if not isinstance(item, dict) or set(item) != LOG_KEYS:
            raise TelemetryError("calibration log schema mismatch", 4)
        unsigned = {key: item[key] for key in LOG_KEYS - {"hash"}}
        expected = hashlib.sha256(canonical(unsigned)).hexdigest()
        if item["previous_hash"] != previous or item["hash"] != expected:
            raise TelemetryError("calibration log hash-chain mismatch", 4)
        previous, rows = expected, [*rows, item]
    return rows


def _append_log(path: Path, receipt: dict[str, Any], receipt_sha: str) -> None:
    rows = _log_rows(path)
    run_hash = receipt["run_id_hash"]
    matches = [item for item in rows if item["run_id_hash"] == run_hash]
    if matches:
        if len(matches) == 1 and matches[0]["receipt_sha256"] == receipt_sha:
            return
        raise TelemetryError("conflicting calibration record", 3)
    unsigned = {"schema_version": 1, "run_id_hash": run_hash, "receipt_sha256": receipt_sha, "ledger_event_hash": receipt["ledger"]["event_tail_hash"], "previous_hash": rows[-1]["hash"] if rows else "0" * 64}
    item = {**unsigned, "hash": hashlib.sha256(canonical(unsigned)).hexdigest()}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, canonical(item) + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)


def _receipt_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "slice_telemetry.json"


def _read_receipt(path: Path) -> dict[str, Any]:
    return read_secure_json(path, max_bytes=256 * 1024, expected_keys=RECEIPT_KEYS)


def _record(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    receipt = _build(root, args)
    path = _receipt_path(root, args.run_id)
    receipt_sha = hashlib.sha256(canonical(receipt)).hexdigest()
    if path.exists():
        if _read_receipt(path) != receipt:
            raise TelemetryError("immutable telemetry receipt conflict", 3)
    else:
        _atomic_projection(path, receipt)
    _append_log(root / ".claude/state/slice_calibration.jsonl", receipt, receipt_sha)
    return {"receipt": receipt, "receipt_sha256": receipt_sha, "receipt_path": _relative(root, path)}


def _status(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state, _ = _ledger(root, args.run_id, args.session_id)
    path = _receipt_path(root, args.run_id)
    receipt = _read_receipt(path)
    receipt_sha = hashlib.sha256(canonical(receipt)).hexdigest()
    if receipt["run_id_hash"] != digest(args.run_id) or receipt["session_id_hash"] != digest(args.session_id) or receipt["ledger"]["event_tail_hash"] != state["last_event_hash"]:
        raise TelemetryError("telemetry receipt is stale or misbound", 3)
    rows = _log_rows(root / ".claude/state/slice_calibration.jsonl")
    if not any(item["run_id_hash"] == receipt["run_id_hash"] and item["receipt_sha256"] == receipt_sha for item in rows):
        raise TelemetryError("telemetry receipt missing calibration binding", 4)
    return {"receipt": receipt, "receipt_sha256": receipt_sha, "receipt_path": _relative(root, path)}


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        root = _git_root(args.cwd)
        with _locked(root):
            result = _build(root, args) if args.command == "inspect" else _record(root, args) if args.command == "record" else _status(root, args)
        _emit(True, args.command, result=result)
        return 0
    except (TelemetryError, LedgerError) as exc:
        _emit(False, "error", stream=sys.stderr, code=getattr(exc, "code", 4), error=str(exc))
        return getattr(exc, "code", 4)
    except OSError as exc:
        _emit(False, "error", stream=sys.stderr, code=6, error=f"filesystem error: {exc}")
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
