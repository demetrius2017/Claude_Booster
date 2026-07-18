"""Evidence-derived sealed-window promotion evaluation.

Purpose: Join item-level path reviews, machine receipts, telemetry and the
authoritative activation registry into roadmap promotion KPIs.
Contract: Window population comes only from activation events; every eligible
session has one row and outcome, path reviews exactly cover handoff paths, any
gate-affecting unknown blocks promotion, and foreign commits always STOP.
CLI/Examples: Used by ``slice_calibration.py evaluate --window-file FILE``.
Limitations: Advisory only; v1 has no measured-benefit overhead exception.
ENV/Files: No environment access or writes.
"""

from __future__ import annotations
import hashlib, json, math, statistics
from datetime import datetime
from typing import Any
from slice_session_registry_core import session_views

LABEL_KEYS = {"schema_version", "path_reviews", "docs_only_dirty"}
MACHINE_KEYS = {"terminal_at", "paths", "foreign_managed_commit", "repair_required", "routing_detected", "routing_routed", "delivery_terminal"}
TELEMETRY_KEYS = {"parser_observed", "parser_expected", "parser_unknown", "spawns", "waits", "provider", "adapter"}
WINDOW_KEYS = {"schema_version", "window_id", "started_at", "ended_at"}
MANIFEST_KEYS = {"run_id_hash", "session_id_hash", "label_sha256", "telemetry_sha256", "ledger_tail_hash", "handoff_sha256", "verification_sha256", "baseline_sha256", "backlog_sha256"}

class CalibrationError(Exception):
    def __init__(self, message: str, code: int = 3) -> None: super().__init__(message); self.code = code

def canonical(value: Any) -> bytes: return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
def sha256(value: Any) -> str: return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()
def _int(v: Any, n: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0: raise CalibrationError(f"invalid {n}", 4)
    return v
def _hash(v: Any, n: str) -> str:
    if not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v): raise CalibrationError(f"invalid {n}", 4)
    return v
def _time(v: Any) -> float:
    try: return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError) as exc: raise CalibrationError("invalid timestamp", 4) from exc

def validate_labels(value: Any, expected_paths: list[dict[str, str]] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != LABEL_KEYS or value.get("schema_version") != 1 or value["docs_only_dirty"] not in {"none", "docs_only_with_implementation_dirty", "unknown"} or not isinstance(value["path_reviews"], list): raise CalibrationError("label schema mismatch", 2)
    seen = set()
    for item in value["path_reviews"]:
        if not isinstance(item, dict) or set(item) != {"path", "classification", "truth"} or item["truth"] not in {"legitimate", "not_legitimate", "unknown"} or not all(isinstance(item[name], str) and item[name] for name in ("path", "classification")) or item["path"] in seen: raise CalibrationError("invalid/duplicate path review", 2)
        seen.add(item["path"])
    if expected_paths is not None and sorted((item["path"], item["classification"]) for item in value["path_reviews"]) != sorted((item["path"], item["classification"]) for item in expected_paths): raise CalibrationError("path reviews omit/add/misclassify handoff paths", 2)
    return value

def validate_machine(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != MACHINE_KEYS or not isinstance(value["paths"], list): raise CalibrationError("machine schema mismatch", 4)
    _time(value["terminal_at"])
    for name in ("foreign_managed_commit", "repair_required", "delivery_terminal"):
        if not isinstance(value[name], bool): raise CalibrationError("machine boolean invalid", 4)
    _int(value["routing_detected"], "routing_detected"); _int(value["routing_routed"], "routing_routed")
    if value["routing_routed"] > value["routing_detected"]: raise CalibrationError("routing numerator exceeds denominator", 4)
    seen = set()
    for item in value["paths"]:
        if not isinstance(item, dict) or set(item) != {"path", "classification", "delivered"} or not isinstance(item["delivered"], bool) or item["path"] in seen: raise CalibrationError("machine path schema invalid", 4)
        seen.add(item["path"])
    return value

def validate_telemetry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != TELEMETRY_KEYS: raise CalibrationError("telemetry schema mismatch", 4)
    for name in ("parser_observed", "parser_expected", "parser_unknown", "spawns", "waits"): _int(value[name], name)
    if value["parser_unknown"] != value["parser_expected"] - value["parser_observed"] or not all(isinstance(value[n], str) and value[n] for n in ("provider", "adapter")): raise CalibrationError("telemetry coverage inconsistent", 4)
    return value

def validate_window(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != WINDOW_KEYS or value.get("schema_version") != 1 or not isinstance(value.get("window_id"), str) or not value["window_id"] or _time(value["started_at"]) >= _time(value["ended_at"]): raise CalibrationError("window invalid", 2)
    return value

def _ratio(n: str, num: int, den: int, unknown: int, threshold: str, passed: bool | None) -> dict[str, Any]: return {"name": n, "numerator": num, "denominator": den, "unknown": unknown, "applicable": den > 0, "value": num / den if den else None, "threshold": threshold, "pass": passed if den and unknown == 0 else None}

def evaluate(rows: list[dict[str, Any]], window: dict[str, Any], manifest: list[dict[str, str]], log_tail: str, registry_events: list[dict[str, Any]]) -> dict[str, Any]:
    window = validate_window(window); _hash(log_tail, "log tail"); views = session_views(registry_events)
    start, end = _time(window["started_at"]), _time(window["ended_at"])
    population = {sid: view for sid, view in views.items() if view["activation"] and start <= _time(view["activation"]["timestamp"]) < end}
    excluded = {sid for sid, view in population.items() if view["excluded"]}; eligible = set(population) - excluded
    if excluded: exclusion_unknown = len(excluded)
    else: exclusion_unknown = 0
    identities, checked = [], []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"run_id_hash", "session_id_hash", "labels", "machine", "telemetry"}: raise CalibrationError("row schema mismatch", 4)
        run, sid = _hash(row["run_id_hash"], "run"), _hash(row["session_id_hash"], "session"); identities.append((run, sid))
        machine = validate_machine(row["machine"]); expected = [{"path": p["path"], "classification": p["classification"]} for p in machine["paths"]]
        checked.append((validate_labels(row["labels"], expected), machine, validate_telemetry(row["telemetry"])))
    if len({sid for _, sid in identities}) != len(identities) or {sid for _, sid in identities} != eligible: raise CalibrationError("authoritative window omission/duplicate/extra session", 4)
    for run, sid in identities:
        activation = population[sid]["activation"]
        if activation["payload"]["run_id_hash"] != run: raise CalibrationError("registry run/session join mismatch", 4)
    if len(manifest) != len(rows) or {(m.get("run_id_hash"), m.get("session_id_hash")) for m in manifest} != set(identities): raise CalibrationError("source manifest mismatch", 4)
    for item in manifest:
        if not isinstance(item, dict) or set(item) != MANIFEST_KEYS: raise CalibrationError("source manifest schema mismatch", 4)
        for name in MANIFEST_KEYS: _hash(item[name], f"manifest {name}")
    labels, machines, telemetries = zip(*checked) if checked else ([], [], [])
    manifest_by_session = {item["session_id_hash"]: item for item in manifest}
    false = reviewed = truth_unknown = 0
    for label, machine in zip(labels, machines):
        by_path = {item["path"]: item for item in machine["paths"]}
        for review in label["path_reviews"]:
            if review["truth"] == "unknown": truth_unknown += 1; continue
            if review["truth"] == "legitimate":
                reviewed += 1
                if not by_path[review["path"]]["delivered"]: false += 1
    routing_d, routing_r = sum(m["routing_detected"] for m in machines), sum(m["routing_routed"] for m in machines)
    delivery_ok = first_pass = overhead_unknown = 0; overheads = []
    for (_, sid), machine in zip(identities, machines):
        view = population[sid]; outcome, terminal = view["outcome"], view["terminal"]
        source = manifest_by_session[sid]
        if terminal and (terminal["payload"]["ledger_tail_hash"] != source["ledger_tail_hash"] or terminal["payload"]["handoff_sha256"] != source["handoff_sha256"] or terminal["payload"]["terminal_at"] != machine["terminal_at"]): raise CalibrationError("terminal registry/source binding mismatch", 4)
        if terminal and _time(machine["terminal_at"]) < _time(view["activation"]["timestamp"]): raise CalibrationError("authoritative terminal precedes activation", 4)
        if outcome and machine["delivery_terminal"] and _time(machine["terminal_at"]) <= _time(outcome["timestamp"]): delivery_ok += 1
        attempts = view["attempts"]
        if attempts and attempts[0]["payload"]["status"] == "pass": first_pass += 1
        expected_controls = set(view["activation"]["payload"]["expected_controls"])
        observed_controls = {begin["payload"]["kind"] for begin, _ in view["controls"]}
        if not terminal or view["open"] or view["unavailable"] or not view["controls"] or observed_controls != expected_controls: overhead_unknown += 1; continue
        total_seconds = _time(machine["terminal_at"]) - _time(view["activation"]["timestamp"])
        control_seconds = sum(finish["monotonic_ns"] - begin["monotonic_ns"] for begin, finish in view["controls"]) / 1_000_000_000
        if total_seconds <= 0 or control_seconds < 0 or control_seconds > total_seconds: overhead_unknown += 1
        else: overheads.append(control_seconds / total_seconds)
    parser_unknown = sum(t["parser_unknown"] + (1 if t["parser_expected"] == 0 else 0) for t in telemetries); sample = len(rows)
    changed = sum(len(m["paths"]) for m in machines); attributed = sum(len(m["paths"]) for m in machines)
    overhead = statistics.median(overheads) if overheads else None
    gate_unknown = parser_unknown + exclusion_unknown
    metrics = {
      "false_quarantine": _ratio("false_quarantine", false, reviewed, truth_unknown + gate_unknown, "<15%", false / reviewed < .15 if reviewed else None),
      "manual_repair": _ratio("manual_repair", sum(m["repair_required"] for m in machines), sample, gate_unknown, "<20%", sum(m["repair_required"] for m in machines) / sample < .2 if sample else None),
      "foreign_managed_commit": _ratio("foreign_managed_commit", sum(m["foreign_managed_commit"] for m in machines), sample, 0, "=0 STOP", not any(m["foreign_managed_commit"] for m in machines) if sample else None),
      "attributed_path_closure": _ratio("attributed_path_closure", attributed, changed, gate_unknown, "=100%", attributed == changed if changed else None),
      "routing_compliance": _ratio("routing_compliance", routing_r, routing_d, gate_unknown if routing_d else sample + gate_unknown, ">=95%", routing_r / routing_d >= .95 if routing_d else None),
      "delivery_domain_transition": _ratio("delivery_domain_transition", delivery_ok, sample, sum(population[s]["outcome"] is None for s in eligible) + gate_unknown, ">=80%", delivery_ok / sample >= .8 if sample else None),
      "exact_state_first_pass": _ratio("exact_state_first_pass", first_pass, sample, sum(not population[s]["attempts"] for s in eligible) + gate_unknown, "reported", None),
      "parser_observability": _ratio("parser_observability", sum(t["parser_observed"] for t in telemetries), sum(t["parser_expected"] for t in telemetries), parser_unknown, "gate-affecting unknown=0", None),
      "overhead": {"name":"overhead","numerator":sum(overheads),"denominator":len(overheads),"unknown":overhead_unknown + gate_unknown,"applicable":bool(overheads),"value":overhead,"threshold":"median <10%; v1 no exception","pass": overhead < .1 if overhead is not None and overhead_unknown + gate_unknown == 0 else None},
    }
    gates=("false_quarantine","manual_repair","foreign_managed_commit","attributed_path_closure","routing_compliance","delivery_domain_transition","overhead")
    missing=[n for n in gates if metrics[n]["pass"] is None]; failed=[n for n in gates if metrics[n]["pass"] is False]
    verdict="STOP_FOREIGN_COMMIT" if metrics["foreign_managed_commit"]["numerator"] else "INSUFFICIENT_SAMPLE" if sample<10 or missing else "FAIL" if failed else "PASS"
    return {"schema_version":1,"window":window,"window_sha256":sha256(window),"registry_tail_hash":registry_events[-1]["hash"] if registry_events else "0"*64,"sample_size":sample,"verdict":verdict,"missing_evidence":missing,"failed_thresholds":failed,"metrics":metrics,"source_manifest":sorted(manifest,key=lambda x:(x["session_id_hash"],x["run_id_hash"])),"calibration_log_tail_hash":log_tail}
