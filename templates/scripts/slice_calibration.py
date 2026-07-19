#!/usr/bin/env python3
"""Immutable human calibration and promotion-decision CLI.

Purpose: Bind reviewed labels to an exact slice-ledger tail and telemetry
receipt, expose status, and evaluate the roadmap promotion bundle.
Contract: ``bootstrap`` binds one real root transcript; ``record`` is
idempotent only for identical human labels/bindings; the append log is hash
chained; window close seals membership and source tails; ``evaluate``
revalidates every source hash/join and writes an immutable decision.
CLI/Examples: use ``window-create`` once, ``bootstrap`` before work,
``labels-template --output labels.json`` after closure, then ``record``;
finish the collection period with ``window-close`` and ``evaluate``.
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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from slice_calibration_core import CalibrationError, LABEL_KEYS, canonical, evaluate, sha256, validate_labels, validate_window
from slice_bootstrap_core import BINDING_KEYS, binding_value, resolve_binding_reference, resolve_root_transcript, secure_binding_delete, secure_binding_read, secure_binding_write, secure_state_log_append, secure_state_log_read, validate_binding
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
    for name in ("record", "status", "labels-template"):
        command = sub.add_parser(name); command.add_argument("--binding"); command.add_argument("--run-id"); command.add_argument("--session-id")
        if name == "record": command.add_argument("--labels-file", required=True)
        elif name == "labels-template": command.add_argument("--output")
    evaluate_cmd = sub.add_parser("evaluate"); evaluate_cmd.add_argument("--window-file")
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--session-id"); bootstrap.add_argument("--transcript")
    bootstrap.add_argument("--artifact-domain", required=True)
    bootstrap.add_argument("--expected-control", action="append", required=True)
    sub.add_parser("window-create")
    sub.add_parser("window-status")
    sub.add_parser("window-close")
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


def _resolve_identity(root: Path, args: argparse.Namespace) -> None:
    """Resolve a protected binding without placing raw routing fields in argv."""
    if args.binding:
        if args.run_id is not None or args.session_id is not None:
            raise CalibrationError("--binding is mutually exclusive with raw run/session arguments", 2)
        binding = resolve_binding_reference(root, args.binding)
        args.run_id, args.session_id = binding["run_id"], binding["session_id"]
    elif args.run_id is None or args.session_id is None:
        raise CalibrationError("provide --binding or both --run-id and --session-id", 2)


def _registry(path: Path) -> list[dict[str, Any]]:
    raw = secure_state_log_read(path.parents[2], path.name)
    return read_events(raw.splitlines())


def _activation_identity(path: str | None, root: Path, session_id: str) -> dict[str, str]:
    if not path: raise CalibrationError("Codex session-start requires explicit transcript", 2)
    try:
        source_path = Path(path)
        if source_path.is_symlink(): raise CodexIdentityError("transcript symlink rejected")
        transcript = source_path.resolve()
        rows, facts = secure_jsonl(transcript); row = rows[0] if rows else None
        payload = validate_session_meta(row, root, expected_session_id=session_id, require_root=True)
    except CodexIdentityError as exc:
        raise CalibrationError(str(exc), exc.code) from exc
    return {
        "thread_id_hash": hashlib.sha256(payload["id"].encode()).hexdigest(),
        "session_meta_sha256": hashlib.sha256(registry_canonical(row)).hexdigest(),
        "transcript_path_hash": hashlib.sha256(str(transcript).encode()).hexdigest(),
        "project_hash": hashlib.sha256(str(root.resolve()).encode()).hexdigest(),
    }


def _bootstrap(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    _registry(root / ".claude/state/slice_session_events.jsonl")
    path, payload, facts, source = resolve_root_transcript(root, args.transcript, args.session_id)
    args.command, args.run_id, args.session_id = "session-start", str(uuid.uuid4()), payload["session_id"]
    args.provider, args.transcript = "codex_rollout_v1", str(path)
    rows, _ = secure_jsonl(path)
    binding = binding_value(root, args.run_id, path, payload, facts, rows[0])
    binding_path = secure_binding_write(root, args.run_id, binding)
    try:
        event = _registry_event(root, args)
    except Exception:
        secure_binding_delete(root, args.run_id)
        raise
    return {
        "run_id": args.run_id,
        "session_id_hash": binding["session_id_hash"],
        "transcript_path_hash": binding["transcript_path_hash"],
        "binding_path": _relative(root, binding_path),
        "resolution": source,
        "event_hash": event["hash"],
    }


def _registry_event(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    path = root / ".claude/state/slice_session_events.jsonl"; events = _registry(path)
    common = {"run_id_hash": hashlib.sha256(args.run_id.encode()).hexdigest(), "session_id_hash": hashlib.sha256(args.session_id.encode()).hexdigest()}
    kind = {"session-start":"activated", "session-terminal":"terminal", "control-start":"control_started", "control-end":"control_ended", "control-na":"control_unavailable", "exclude-session":"excluded"}.get(args.command, args.command.replace("-", "_"))
    if kind == "activated":
        proof = _activation_identity(args.transcript, root, args.session_id) if args.provider == "codex_rollout_v1" else {}
        payload = {**common, "provider": args.provider, "artifact_domain": args.artifact_domain, "expected_controls":args.expected_control, **proof}
        for existing in events:
            if existing["type"] != "activated": continue
            prior = existing["payload"]
            if prior["session_id_hash"] == common["session_id_hash"] or (proof and prior.get("transcript_path_hash") == proof["transcript_path_hash"]):
                raise CalibrationError("root session/transcript already activated; start a new top-level session", 3)
    elif kind in {"control_started", "control_ended"}: payload = {**common, "kind": args.kind}
    elif kind == "control_unavailable": payload = {**common, "kind":args.kind, "reason":args.reason}
    elif kind == "verification_attempt": payload = {**common, "status": args.status, "receipt_sha256": hashlib.sha256(Path(args.receipt_file).read_bytes()).hexdigest()}
    elif kind == "terminal": payload = {**common, "ledger_tail_hash":args.ledger_tail_hash, "handoff_sha256":args.handoff_sha256, "terminal_at":args.terminal_at}
    elif kind == "domain_outcome": payload = {**common, "next_domain": args.next_domain}
    else: payload = {**common, "reason": args.reason, "evidence_sha256": hashlib.sha256(Path(args.evidence_file).read_bytes()).hexdigest()}
    unsigned = {"schema_version":1,"sequence":len(events)+1,"timestamp":datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z"),"monotonic_ns":time.monotonic_ns(),"type":kind,"payload":payload,"previous_hash":events[-1]["hash"] if events else "0"*64}
    event = {**unsigned, "hash":hashlib.sha256(registry_canonical(unsigned)).hexdigest()}
    read_events([*(registry_canonical(item) for item in events), registry_canonical(event)])
    existing = b"".join(registry_canonical(item)+b"\n" for item in events)
    secure_state_log_append(root, path.name, existing, registry_canonical(event)+b"\n")
    return event


def _emit(ok: bool, kind: str, *, stream: Any = sys.stdout, **values: Any) -> None:
    print(json.dumps({"ok": ok, "type": kind, **values}, sort_keys=True, separators=(",", ":")), file=stream)


def _log(path: Path) -> list[dict[str, Any]]:
    raw = secure_state_log_read(path.parents[2], path.name)
    rows, previous = [], "0" * 64
    for line in raw.splitlines():
        try: item = json.loads(line)
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
    binding_path = _run_dir(root, run_id) / "slice_session_binding.json"
    binding = secure_binding_read(root, run_id)
    validate_binding(root, binding, run_id=run_id, session_id=session_id)
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
    existing = b"".join(canonical(existing_item)+b"\n" for existing_item in rows)
    secure_state_log_append(path.parents[2], path.name, existing, canonical(item)+b"\n")


def _record(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    _log(root / ".claude/state/slice_calibration_labels.jsonl")
    state, events, telemetry, telemetry_path, telemetry_sha = _binding(root, args.run_id, args.session_id)
    machine, telemetry_fact, sources = _machine_facts(root, state, events, telemetry, telemetry_path, telemetry_sha)
    labels = validate_labels(read_secure_json(Path(args.labels_file), max_bytes=64 * 1024, expected_keys=LABEL_KEYS), [{"path":p["path"],"classification":p["classification"]} for p in machine["paths"]])
    if labels["docs_only_dirty"] == "unknown" or any(item["truth"] == "unknown" for item in labels["path_reviews"]):
        raise CalibrationError("human labels remain unknown; complete every truth and docs_only_dirty before record", 2)
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


def _labels_template(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state, events, telemetry, telemetry_path, telemetry_sha = _binding(root, args.run_id, args.session_id)
    machine, _, _ = _machine_facts(root, state, events, telemetry, telemetry_path, telemetry_sha)
    labels = {
        "schema_version": 1,
        "path_reviews": [
            {"path": item["path"], "classification": item["classification"], "truth": "unknown"}
            for item in machine["paths"]
        ],
        "docs_only_dirty": "unknown",
    }
    validate_labels(labels, [{"path": item["path"], "classification": item["classification"]} for item in machine["paths"]])
    if args.output:
        destination = Path(args.output)
        if destination.exists():
            raise CalibrationError("labels template output already exists", 3)
        _atomic_projection(destination, labels)
    return {"labels": labels, "output": str(Path(args.output).resolve()) if args.output else None, "human_edit_required": True}


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


WINDOW_STATE_KEYS = {"schema_version", "window_id", "status", "started_at", "ended_at", "created_at", "registry_tail_hash", "label_log_tail_hash", "members"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _window_path(root: Path) -> Path:
    return root / ".claude/state/slice_calibration_window.json"


def _read_window_state(root: Path) -> dict[str, Any]:
    value = read_secure_json(_window_path(root), max_bytes=128 * 1024, expected_keys=WINDOW_STATE_KEYS)
    if value["schema_version"] != 1 or value["status"] not in {"open", "closed"} or not isinstance(value["members"], list):
        raise CalibrationError("calibration window state invalid", 4)
    if value["status"] == "open" and (value["ended_at"] is not None or value["members"]):
        raise CalibrationError("open calibration window mutated", 4)
    if value["status"] == "closed":
        validate_window({"schema_version": 1, "window_id": value["window_id"], "started_at": value["started_at"], "ended_at": value["ended_at"]})
    return value


def _window_create(root: Path) -> dict[str, Any]:
    path = _window_path(root)
    if path.exists():
        raise CalibrationError("canonical calibration window already exists", 3)
    now = _now()
    state = {"schema_version": 1, "window_id": str(uuid.uuid4()), "status": "open", "started_at": now, "ended_at": None, "created_at": now, "registry_tail_hash": None, "label_log_tail_hash": None, "members": []}
    _atomic_projection(path, state)
    return state


def _window_population(root: Path, state: dict[str, Any], *, closed: bool = False) -> tuple[list[dict[str, str]], int]:
    from slice_session_registry_core import session_views
    events = _registry(root / ".claude/state/slice_session_events.jsonl")
    if closed:
        tails = [-1] if state["registry_tail_hash"] == "0" * 64 and not events else [index for index, item in enumerate(events) if item["hash"] == state["registry_tail_hash"]]
        if len(tails) != 1: raise CalibrationError("sealed window registry snapshot unavailable", 4)
        events = events[: tails[0] + 1]
    start = datetime.fromisoformat(state["started_at"].replace("Z", "+00:00")).timestamp()
    end_text = state["ended_at"] if closed else _now()
    end = datetime.fromisoformat(end_text.replace("Z", "+00:00")).timestamp()
    views = session_views(events)
    members = []
    for sid, view in views.items():
        activation = view["activation"]
        if activation:
            stamp = datetime.fromisoformat(activation["timestamp"].replace("Z", "+00:00")).timestamp()
            if start <= stamp < end and not view["excluded"]:
                payload = activation["payload"]
                if not {"transcript_path_hash", "project_hash"} <= set(payload):
                    raise CalibrationError("prospective window contains unbound root activation", 4)
                binding_path = root / f".claude/state/runs/{payload['run_id_hash']}/slice_session_binding.json"
                binding = secure_binding_read(root, payload["run_id_hash"], hashed=True)
                validate_binding(root, binding)
                if hashlib.sha256(binding["run_id"].encode()).hexdigest() != payload["run_id_hash"] or binding["session_id_hash"] != sid or binding["transcript_path_hash"] != payload["transcript_path_hash"] or binding["project_hash"] != payload["project_hash"]:
                    raise CalibrationError("activation/session binding join mismatch", 4)
                members.append({"run_id_hash": payload["run_id_hash"], "session_id_hash": sid})
    rows = _verified_rows(root)
    available = {(item["run_id_hash"], item["session_id_hash"]) for item in rows}
    if closed:
        logs = _log(root / ".claude/state/slice_calibration_labels.jsonl")
        if state["label_log_tail_hash"] == "0" * 64:
            available = set()
        else:
            indexes = [index for index, item in enumerate(logs) if item["hash"] == state["label_log_tail_hash"]]
            if len(indexes) != 1: raise CalibrationError("sealed label snapshot unavailable", 4)
            allowed = {(item["run_id_hash"], item["session_id_hash"]) for item in logs[: indexes[0] + 1]}
            available &= allowed
    return sorted(members, key=lambda item: (item["session_id_hash"], item["run_id_hash"])), sum((item["run_id_hash"], item["session_id_hash"]) in available for item in members)


def _window_status(root: Path) -> dict[str, Any]:
    state = _read_window_state(root)
    members, eligible = _window_population(root, state, closed=state["status"] == "closed")
    if state["status"] == "closed" and members != state["members"]:
        raise CalibrationError("sealed calibration membership changed", 4)
    return {"window": state, "activated": len(members), "eligible": eligible, "target": 10, "counter": f"{eligible}/10"}


def _window_close(root: Path) -> dict[str, Any]:
    state = _read_window_state(root)
    if state["status"] != "open": raise CalibrationError("calibration window already closed", 3)
    events = _registry(root / ".claude/state/slice_session_events.jsonl")
    logs = _log(root / ".claude/state/slice_calibration_labels.jsonl")
    closed = {**state, "status": "closed", "ended_at": _now(), "registry_tail_hash": events[-1]["hash"] if events else "0" * 64, "label_log_tail_hash": logs[-1]["hash"] if logs else "0" * 64}
    members, _ = _window_population(root, closed, closed=False)
    closed["members"] = members
    _atomic_projection(_window_path(root), closed)
    return closed


def _evaluate(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    sealed_state = None
    if args.window_file:
        if _window_path(root).exists():
            raise CalibrationError("external window is non-authoritative while canonical window exists", 3)
        window = validate_window(read_secure_json(Path(args.window_file), max_bytes=128 * 1024, expected_keys={"schema_version", "window_id", "started_at", "ended_at"}))
    else:
        sealed_state = _read_window_state(root)
        if sealed_state["status"] != "closed": raise CalibrationError("calibration window must be closed before evaluation", 3)
        window = validate_window({key: sealed_state[key] for key in ("schema_version", "window_id", "started_at", "ended_at")})
    window_hash = sha256(window); window_path = root / f".claude/state/slice_window_{window_hash[:16]}.json"
    if window_path.exists():
        if json.loads(window_path.read_text()) != window: raise CalibrationError("sealed window conflict", 4)
    else: _atomic_projection(window_path, window)
    verified = _verified_rows(root)
    log_rows = _log(root / ".claude/state/slice_calibration_labels.jsonl"); tail = log_rows[-1]["hash"] if log_rows else "0" * 64
    registry = _registry(root / ".claude/state/slice_session_events.jsonl")
    if sealed_state is not None:
        registry_indexes = [i for i, item in enumerate(registry) if item["hash"] == sealed_state["registry_tail_hash"]]
        if sealed_state["registry_tail_hash"] == "0" * 64 and not registry: registry_indexes = [-1]
        if len(registry_indexes) != 1: raise CalibrationError("sealed registry snapshot unavailable", 4)
        registry = registry[: registry_indexes[0] + 1]
        if sealed_state["label_log_tail_hash"] == "0" * 64:
            allowed = set(); tail = "0" * 64
        else:
            label_indexes = [i for i, item in enumerate(log_rows) if item["hash"] == sealed_state["label_log_tail_hash"]]
            if len(label_indexes) != 1: raise CalibrationError("sealed label snapshot unavailable", 4)
            log_rows = log_rows[: label_indexes[0] + 1]; tail = log_rows[-1]["hash"]
            allowed = {(item["run_id_hash"], item["session_id_hash"]) for item in log_rows}
        member_ids = {(item["run_id_hash"], item["session_id_hash"]) for item in sealed_state["members"]}
        verified = [item for item in verified if (item["run_id_hash"], item["session_id_hash"]) in allowed & member_ids]
    manifest = [item.pop("source_manifest") for item in verified]
    decision = evaluate(verified, window, manifest, tail, registry)
    if args.window_file:
        decision = {**decision, "authority": "legacy_non_promotable", "legacy_verdict": decision["verdict"], "verdict": "LEGACY_NON_PROMOTABLE"}
    dataset_sha = sha256(decision)
    artifact = {**decision, "dataset_sha256": dataset_sha}
    path = root / f".claude/state/slice_promotion_{dataset_sha[:16]}.json"
    if path.exists():
        if read_secure_json(path, max_bytes=128 * 1024, expected_keys=set(artifact)) != artifact: raise CalibrationError("promotion artifact conflict", 4)
    else: _atomic_projection(path, artifact)
    return {"decision": artifact, "decision_sha256": sha256(artifact), "decision_path": _relative(root, path)}


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv); root = _git_root(args.cwd)
        with _locked(root):
            if args.command in {"record", "status", "labels-template"}:
                _resolve_identity(root, args)
            result = (
                _record(root, args) if args.command == "record" else
                _status(root, args) if args.command == "status" else
                _labels_template(root, args) if args.command == "labels-template" else
                _evaluate(root, args) if args.command == "evaluate" else
                _bootstrap(root, args) if args.command == "bootstrap" else
                _window_create(root) if args.command == "window-create" else
                _window_status(root) if args.command == "window-status" else
                _window_close(root) if args.command == "window-close" else
                {"event": _registry_event(root, args)}
            )
        _emit(True, args.command, result=result); return 0
    except (CalibrationError, LedgerError, VerifyError, RegistryError, json.JSONDecodeError) as exc:
        _emit(False, "error", stream=sys.stderr, code=getattr(exc, "code", 4), error=str(exc)); return getattr(exc, "code", 4)
    except OSError as exc:
        _emit(False, "error", stream=sys.stderr, code=6, error=f"filesystem error: {exc}"); return 6


if __name__ == "__main__": raise SystemExit(main())
