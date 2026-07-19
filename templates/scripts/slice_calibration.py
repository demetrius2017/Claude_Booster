#!/usr/bin/env python3
"""Immutable human calibration and promotion-decision CLI.

Purpose: Bind reviewed labels to an exact slice-ledger tail and telemetry
receipt, expose status, and evaluate the roadmap promotion bundle.
Contract: ``record`` is idempotent only for identical labels/bindings; the
append log is hash chained; ``evaluate`` revalidates every source hash/join and
writes one immutable content-addressed promotion decision.
CLI/Examples: ``slice_calibration.py --cwd ROOT record --run-id R --session-id
S --labels-file labels.json``; use ``status`` or ``evaluate`` afterwards.
Limitations: Advisory evidence only; never activates autopilot, closes slices,
enforces WIP, or invents human labels.
ENV/Files: Writes mode-0600 run receipts, calibration JSONL, and immutable
promotion decisions below project-local ``.claude/state``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from slice_calibration_core import CalibrationError, LABEL_KEYS, canonical, evaluate, sha256, validate_labels, validate_window
from slice_close_core import VerifyError, _secure_lines, read_secure_json
from slice_git import _relative, _run_dir
from slice_ledger import _git_root, _locked
from slice_ledger_core import LedgerError, _atomic_projection, _load
from slice_session_registry_core import RegistryError, canonical as registry_canonical, read_events
from slice_telemetry_core import CodexIdentityError, secure_jsonl, validate_session_meta

RECEIPT_KEYS = {"schema_version", "run_id_hash", "session_id_hash", "ledger_tail_hash", "labels", "machine", "telemetry", "sources", "recorded_at"}
LOG_KEYS = {"schema_version", "run_id_hash", "session_id_hash", "ledger_tail_hash", "telemetry_path", "telemetry_sha256", "label_path", "label_sha256", "previous_hash", "hash"}


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CalibrationError(message, 2)


def _parser() -> argparse.ArgumentParser:
    parser = Parser(description=__doc__); parser.add_argument("--cwd", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "status"):
        command = sub.add_parser(name); command.add_argument("--run-id", required=True); command.add_argument("--session-id", required=True)
        if name == "record": command.add_argument("--labels-file", required=True)
    evaluate_cmd = sub.add_parser("evaluate"); evaluate_cmd.add_argument("--window-file", required=True)
    for name in ("session-start", "control-start", "control-end", "control-na", "verification-attempt", "session-terminal", "domain-outcome", "exclude-session"):
        command = sub.add_parser(name); command.add_argument("--run-id", required=True); command.add_argument("--session-id", required=True)
        if name == "session-start": command.add_argument("--provider", required=True); command.add_argument("--artifact-domain", required=True); command.add_argument("--expected-control", action="append", required=True); command.add_argument("--transcript")
        elif name in {"control-start", "control-end"}: command.add_argument("--kind", required=True)
        elif name == "control-na": command.add_argument("--kind", required=True); command.add_argument("--reason", required=True, choices=("native_surface_unavailable", "operation_failed", "capability_missing"))
        elif name == "verification-attempt": command.add_argument("--status", required=True); command.add_argument("--receipt-file", required=True)
        elif name == "session-terminal": command.add_argument("--ledger-tail-hash", required=True); command.add_argument("--handoff-sha256", required=True); command.add_argument("--terminal-at", required=True)
        elif name == "domain-outcome": command.add_argument("--next-domain", required=True)
        else: command.add_argument("--reason", required=True); command.add_argument("--evidence-file", required=True)
    return parser


def _registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists(): return []
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600: raise CalibrationError("session registry must be regular mode 0600", 4)
    return read_events(path.read_bytes().splitlines())


def _activation_identity(path: str | None, root: Path, session_id: str) -> dict[str, str]:
    if not path: raise CalibrationError("Codex session-start requires explicit transcript", 2)
    try:
        rows, _ = secure_jsonl(Path(path)); row = rows[0] if rows else None
        payload = validate_session_meta(row, root, expected_session_id=session_id, require_root=True)
    except CodexIdentityError as exc:
        raise CalibrationError(str(exc), exc.code) from exc
    return {"thread_id_hash": hashlib.sha256(payload["id"].encode()).hexdigest(), "session_meta_sha256": hashlib.sha256(registry_canonical(row)).hexdigest()}


def _registry_event(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    path = root / ".claude/state/slice_session_events.jsonl"; events = _registry(path)
    common = {"run_id_hash": hashlib.sha256(args.run_id.encode()).hexdigest(), "session_id_hash": hashlib.sha256(args.session_id.encode()).hexdigest()}
    kind = {"session-start":"activated", "session-terminal":"terminal", "control-start":"control_started", "control-end":"control_ended", "control-na":"control_unavailable", "exclude-session":"excluded"}.get(args.command, args.command.replace("-", "_"))
    if kind == "activated":
        proof = _activation_identity(args.transcript, root, args.session_id) if args.provider == "codex_rollout_v1" else {}
        payload = {**common, "provider": args.provider, "artifact_domain": args.artifact_domain, "expected_controls":args.expected_control, **proof}
    elif kind in {"control_started", "control_ended"}: payload = {**common, "kind": args.kind}
    elif kind == "control_unavailable": payload = {**common, "kind":args.kind, "reason":args.reason}
    elif kind == "verification_attempt": payload = {**common, "status": args.status, "receipt_sha256": hashlib.sha256(Path(args.receipt_file).read_bytes()).hexdigest()}
    elif kind == "terminal": payload = {**common, "ledger_tail_hash":args.ledger_tail_hash, "handoff_sha256":args.handoff_sha256, "terminal_at":args.terminal_at}
    elif kind == "domain_outcome": payload = {**common, "next_domain": args.next_domain}
    else: payload = {**common, "reason": args.reason, "evidence_sha256": hashlib.sha256(Path(args.evidence_file).read_bytes()).hexdigest()}
    unsigned = {"schema_version":1,"sequence":len(events)+1,"timestamp":datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z"),"monotonic_ns":time.monotonic_ns(),"type":kind,"payload":payload,"previous_hash":events[-1]["hash"] if events else "0"*64}
    event = {**unsigned, "hash":hashlib.sha256(registry_canonical(unsigned)).hexdigest()}
    read_events([*(registry_canonical(item) for item in events), registry_canonical(event)])
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd,0o600); pending=memoryview(registry_canonical(event)+b"\n")
        while pending:
            written=os.write(fd,pending)
            if written <= 0: raise OSError("short registry append")
            pending=pending[written:]
        os.fsync(fd)
    finally: os.close(fd)
    return event


def _emit(ok: bool, kind: str, *, stream: Any = sys.stdout, **values: Any) -> None:
    print(json.dumps({"ok": ok, "type": kind, **values}, sort_keys=True, separators=(",", ":")), file=stream)


def _log(path: Path) -> list[dict[str, Any]]:
    if not path.exists(): return []
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise CalibrationError("calibration log must be regular mode 0600", 4)
    rows, previous = [], "0" * 64
    for raw in path.read_bytes().splitlines():
        try: item = json.loads(raw)
        except json.JSONDecodeError as exc: raise CalibrationError("calibration log JSON corrupt", 4) from exc
        if not isinstance(item, dict) or set(item) != LOG_KEYS: raise CalibrationError("calibration log schema mismatch", 4)
        unsigned = {key: item[key] for key in LOG_KEYS - {"hash"}}
        expected = sha256(unsigned)
        if item["previous_hash"] != previous or item["hash"] != expected: raise CalibrationError("calibration log hash-chain mismatch", 4)
        previous, rows = expected, [*rows, item]
    return rows


def _read_receipt(path: Path) -> dict[str, Any]:
    value = read_secure_json(path, max_bytes=64 * 1024, expected_keys=RECEIPT_KEYS)
    return value


def _binding(root: Path, run_id: str, session_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], Path, str]:
    state, events = _load(root / ".claude/state/slice_ledger.json", root / ".claude/state/slice_events.jsonl")
    if state is None or state["run_id"] != run_id or state["owner"]["session_id"] != session_id:
        raise CalibrationError("exact ledger run/session required", 3)
    if state["state"] != "closed": raise CalibrationError("only terminal slices may be calibrated", 3)
    telemetry_path = _run_dir(root, run_id) / "slice_telemetry.json"
    telemetry = read_secure_json(telemetry_path, max_bytes=256 * 1024, expected_keys={"schema_version", "run_id_hash", "session_id_hash", "project_hash", "ledger", "observation"})
    telemetry_sha = hashlib.sha256(canonical(telemetry)).hexdigest()
    if telemetry["run_id_hash"] != hashlib.sha256(run_id.encode()).hexdigest() or telemetry["session_id_hash"] != hashlib.sha256(session_id.encode()).hexdigest() or telemetry["ledger"]["event_tail_hash"] != state["last_event_hash"]:
        raise CalibrationError("telemetry/ledger identity or generation mismatch", 3)
    return state, events, telemetry, telemetry_path, telemetry_sha


def _source(root: Path, path: Path, expected: str) -> dict[str, str]:
    if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes().rstrip(b"\n")).hexdigest() != expected:
        # JSON receipts are hashed canonically, not by serialized bytes.
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() and not path.is_symlink() else None
        if value is None or sha256(value) != expected: raise CalibrationError("machine source hash mismatch", 4)
    return {"path": _relative(root, path), "sha256": expected}


def _machine_facts(root: Path, state: dict[str, Any], events: list[dict[str, Any]], telemetry: dict[str, Any], telemetry_path: Path, telemetry_sha: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    closure = state["closure"]
    paths = {"telemetry": _source(root, telemetry_path, telemetry_sha), "handoff": _source(root, root / closure["handoff_path"], state["handoff_sha256"]), "verification": _source(root, root / state["verification_path"], state["verification_sha256"]), "baseline": _source(root, root / state["baseline_path"], state["baseline_sha256"])}
    handoff = json.loads((root / paths["handoff"]["path"]).read_text(encoding="utf-8"))
    verification = json.loads((root / paths["verification"]["path"]).read_text(encoding="utf-8"))
    delivered = set(handoff["paths"]["delivered"])
    foreign = False
    if closure["commit_oid"]:
        import subprocess
        parent = subprocess.run(["git", "-C", str(root), "rev-parse", f"{closure['commit_oid']}^"], capture_output=True, text=True, check=False)
        changed = subprocess.run(["git", "-C", str(root), "diff-tree", "--no-commit-id", "--name-only", "-r", parent.stdout.strip(), closure["commit_oid"]], capture_output=True, text=True, check=False)
        if parent.returncode or changed.returncode: raise CalibrationError("commit source unavailable", 4)
        foreign = any(path not in delivered for path in changed.stdout.splitlines())
    path_items = [{"path": path, "classification": classification, "delivered": path in delivered} for classification in ("candidate-owned", "foreign", "ambiguous", "off-scope") for path in handoff["paths"][classification]]
    offscope_paths = set(handoff["paths"]["off-scope"]); backlog_path = root / closure["backlog_path"]; backlog = _secure_lines(backlog_path)
    if (backlog[-1]["hash"] if backlog else None) != closure["backlog_tail_hash"] or len(backlog) != closure["backlog_count"]: raise CalibrationError("backlog source generation mismatch", 4)
    run_backlog = {item["path"] for item in backlog if item["run_id"] == state["run_id"]}
    if run_backlog != offscope_paths: raise CalibrationError("backlog does not exactly bind current-run off-scope paths", 4)
    paths["backlog"] = _source(root, backlog_path, hashlib.sha256(backlog_path.read_bytes().rstrip(b"\n")).hexdigest())
    offscope = len(offscope_paths)
    recovery = any(item["type"] == "recovered" and item["payload"].get("run_id") == state["run_id"] for item in events)
    machine = {"terminal_at": handoff["created_at"], "paths": path_items, "foreign_managed_commit": foreign, "repair_required": recovery, "routing_detected": offscope, "routing_routed": len(run_backlog), "delivery_terminal": state["terminal_disposition"] in {"committed", "quarantined", "delivered_uncommitted", "blocked"}}
    metrics = telemetry["observation"]["metrics"]
    parser, waits = metrics["parser_coverage"], metrics["waits"]["value"]
    telemetry_fact = {"parser_observed": parser["observed"], "parser_expected": parser["expected"], "parser_unknown": parser["evidence_counts"]["unknown"], "spawns": metrics["spawns"]["value"], "waits": waits["all"], "provider": telemetry["observation"]["provider"], "adapter": telemetry["observation"]["adapter"]}
    manifest = {"run_id_hash": telemetry["run_id_hash"], "session_id_hash": telemetry["session_id_hash"], "label_sha256": "", "telemetry_sha256": telemetry_sha, "ledger_tail_hash": state["last_event_hash"], "handoff_sha256": state["handoff_sha256"], "verification_sha256": state["verification_sha256"], "baseline_sha256": state["baseline_sha256"], "backlog_sha256": paths["backlog"]["sha256"]}
    return machine, telemetry_fact, {**paths, "manifest": manifest}


def _append(path: Path, row: dict[str, Any]) -> None:
    rows = _log(path)
    matches = [item for item in rows if item["run_id_hash"] == row["run_id_hash"]]
    if matches:
        if len(matches) == 1 and all(matches[0][key] == row[key] for key in LOG_KEYS - {"previous_hash", "hash"}): return
        raise CalibrationError("conflicting calibration run", 3)
    if any(item["session_id_hash"] == row["session_id_hash"] for item in rows):
        raise CalibrationError("session already has a canonical calibration row", 3)
    unsigned = {**row, "previous_hash": rows[-1]["hash"] if rows else "0" * 64}
    item = {**unsigned, "hash": sha256(unsigned)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        pending = memoryview(canonical(item) + b"\n")
        while pending:
            written = os.write(fd, pending)
            if written <= 0: raise OSError("short calibration append")
            pending = pending[written:]
        os.fsync(fd)
    finally: os.close(fd)


def _record(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state, events, telemetry, telemetry_path, telemetry_sha = _binding(root, args.run_id, args.session_id)
    machine, telemetry_fact, sources = _machine_facts(root, state, events, telemetry, telemetry_path, telemetry_sha)
    labels = validate_labels(read_secure_json(Path(args.labels_file), max_bytes=64 * 1024, expected_keys=LABEL_KEYS), [{"path":p["path"],"classification":p["classification"]} for p in machine["paths"]])
    run_hash, session_hash = hashlib.sha256(args.run_id.encode()).hexdigest(), hashlib.sha256(args.session_id.encode()).hexdigest()
    path = _run_dir(root, args.run_id) / "slice_calibration.json"
    stable = {"schema_version": 1, "run_id_hash": run_hash, "session_id_hash": session_hash, "ledger_tail_hash": state["last_event_hash"], "labels": labels, "machine": machine, "telemetry": telemetry_fact, "sources": sources}
    if path.exists():
        receipt = _read_receipt(path)
        if {key: receipt[key] for key in RECEIPT_KEYS - {"recorded_at"}} != stable: raise CalibrationError("immutable calibration receipt conflict", 3)
    else:
        receipt = {**stable, "recorded_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")}; _atomic_projection(path, receipt)
    label_sha = sha256(receipt)
    _append(root / ".claude/state/slice_calibration_labels.jsonl", {"schema_version": 1, "run_id_hash": run_hash, "session_id_hash": session_hash, "ledger_tail_hash": state["last_event_hash"], "telemetry_path": _relative(root, telemetry_path), "telemetry_sha256": telemetry_sha, "label_path": _relative(root, path), "label_sha256": label_sha})
    return {"receipt": receipt, "receipt_sha256": label_sha, "receipt_path": _relative(root, path)}


def _verified_rows(root: Path) -> list[dict[str, Any]]:
    _, events = _load(root / ".claude/state/slice_ledger.json", root / ".claude/state/slice_events.jsonl")
    tails = {item["hash"] for item in events}
    output: list[dict[str, Any]] = []
    for item in _log(root / ".claude/state/slice_calibration_labels.jsonl"):
        if item["ledger_tail_hash"] not in tails: raise CalibrationError("label references unknown ledger generation", 4)
        if not all(isinstance(item[name], str) and item[name].startswith(".claude/state/runs/") and ".." not in Path(item[name]).parts for name in ("label_path", "telemetry_path")):
            raise CalibrationError("calibration source path escapes run state", 4)
        receipt = _read_receipt(root / item["label_path"])
        telemetry = read_secure_json(root / item["telemetry_path"], max_bytes=256 * 1024, expected_keys={"schema_version", "run_id_hash", "session_id_hash", "project_hash", "ledger", "observation"})
        if sha256(receipt) != item["label_sha256"] or hashlib.sha256(canonical(telemetry)).hexdigest() != item["telemetry_sha256"] or receipt["ledger_tail_hash"] != item["ledger_tail_hash"] or receipt["run_id_hash"] != item["run_id_hash"] or receipt["session_id_hash"] != item["session_id_hash"]:
            raise CalibrationError("calibration source hash/join mismatch", 4)
        for source in receipt["sources"].values():
            if isinstance(source, dict) and set(source) == {"path", "sha256"}: _source(root, root / source["path"], source["sha256"])
        output.append({"run_id_hash": item["run_id_hash"], "session_id_hash": item["session_id_hash"], "labels": receipt["labels"], "machine": receipt["machine"], "telemetry": receipt["telemetry"], "source_manifest": {**receipt["sources"]["manifest"], "label_sha256": item["label_sha256"]}})
    return output


def _status(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state, _, _, _, _ = _binding(root, args.run_id, args.session_id)
    receipt = _read_receipt(_run_dir(root, args.run_id) / "slice_calibration.json")
    if receipt["ledger_tail_hash"] != state["last_event_hash"]: raise CalibrationError("calibration receipt stale", 3)
    _verified_rows(root)
    return {"receipt": receipt, "receipt_sha256": sha256(receipt)}


def _evaluate(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    window = validate_window(read_secure_json(Path(args.window_file), max_bytes=128 * 1024, expected_keys={"schema_version", "window_id", "started_at", "ended_at"}))
    window_hash = sha256(window); window_path = root / f".claude/state/slice_window_{window_hash[:16]}.json"
    if window_path.exists():
        if json.loads(window_path.read_text()) != window: raise CalibrationError("sealed window conflict", 4)
    else: _atomic_projection(window_path, window)
    verified = _verified_rows(root); manifest = [item.pop("source_manifest") for item in verified]
    log_rows = _log(root / ".claude/state/slice_calibration_labels.jsonl"); tail = log_rows[-1]["hash"] if log_rows else "0" * 64
    registry = _registry(root / ".claude/state/slice_session_events.jsonl")
    decision = evaluate(verified, window, manifest, tail, registry); dataset_sha = sha256(decision)
    artifact = {**decision, "dataset_sha256": dataset_sha}
    path = root / f".claude/state/slice_promotion_{dataset_sha[:16]}.json"
    if path.exists():
        if read_secure_json(path, max_bytes=128 * 1024, expected_keys=set(artifact)) != artifact: raise CalibrationError("promotion artifact conflict", 4)
    else: _atomic_projection(path, artifact)
    return {"decision": artifact, "decision_sha256": sha256(artifact), "decision_path": _relative(root, path)}


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv); root = _git_root(args.cwd)
        with _locked(root): result = _record(root, args) if args.command == "record" else _status(root, args) if args.command == "status" else _evaluate(root, args) if args.command == "evaluate" else {"event": _registry_event(root, args)}
        _emit(True, args.command, result=result); return 0
    except (CalibrationError, LedgerError, VerifyError, RegistryError, json.JSONDecodeError) as exc:
        _emit(False, "error", stream=sys.stderr, code=getattr(exc, "code", 4), error=str(exc)); return getattr(exc, "code", 4)
    except OSError as exc:
        _emit(False, "error", stream=sys.stderr, code=6, error=f"filesystem error: {exc}"); return 6


if __name__ == "__main__": raise SystemExit(main())
