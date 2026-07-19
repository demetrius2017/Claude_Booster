"""Authoritative append-only autopilot session registry primitives.

Purpose: Record activation population, domains, ordered control observations,
verification attempts, domain outcomes, and evidence-bound exclusions.
Contract: Events are schema-v1, hash chained, generated-time ordered, and use
hashed run/session identities; one activation and one outcome per session.
CLI/Examples: Used by ``slice_calibration.py session-start|observe|exclude``.
Limitations: Advisory evidence only; it does not activate or enforce autopilot.
ENV/Files: Callers persist the returned events as mode-0600 JSONL.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any

EVENT_KEYS = {"schema_version", "sequence", "timestamp", "monotonic_ns", "type", "payload", "previous_hash", "hash"}
TYPES = {"activated", "control_started", "control_ended", "control_unavailable", "verification_attempt", "terminal", "domain_outcome", "excluded"}
PROVIDERS = {"codex_rollout_v1", "booster_wrapper_v1"}
CONTROL_KINDS = {"ledger", "git", "verification", "closure", "telemetry", "calibration"}
CONTROL_NA_REASONS = {"native_surface_unavailable", "operation_failed", "capability_missing"}
EXCLUSION_REASONS = {"unsupported_provider", "corrupt_source", "operator_cancelled"}
WORKER_ROLES = {"worker", "recon", "verifier", "reviewer"}
ATTEMPT_TYPES = {"worker_attempt_started", "worker_attempt_observed", "worker_attempt_completed", "worker_attempt_failed"}
ATTEMPT_COMMON = {"run_id", "session_id", "attempt_id", "role", "brief_sha256", "parent_id", "task_id"}
ATTEMPT_KEYS = {
    "worker_attempt_started": ATTEMPT_COMMON | {"retry_of", "retry_number", "retry_evidence_sha256", "retry_failure_reason"},
    "worker_attempt_observed": ATTEMPT_COMMON | {"evidence_delta_sha256"},
    "worker_attempt_completed": ATTEMPT_COMMON | {"evidence_delta_sha256"},
    "worker_attempt_failed": ATTEMPT_COMMON | {"evidence_delta_sha256", "failure_reason"},
}


class RegistryError(Exception):
    def __init__(self, message: str, code: int = 3) -> None: super().__init__(message); self.code = code


def normalized_brief_hash(value: Any) -> str:
    """Hash one bounded whitespace-normalized worker brief, rejecting blanks."""
    if not isinstance(value, str): raise RegistryError("worker brief must be text", 2)
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 4096: raise RegistryError("worker brief must be nonempty and bounded", 2)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _bounded(value: Any, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum: raise RegistryError(f"invalid {name}", 4)
    return value


def apply_attempt_event(attempts: dict[Any, dict[str, Any]], event_type: str, payload: Any) -> None:
    """Validate and apply one immutable attempt transition during replay."""
    if event_type not in ATTEMPT_TYPES or not isinstance(payload, dict) or set(payload) != ATTEMPT_KEYS[event_type]: raise RegistryError("attempt event schema mismatch", 4)
    for name in ("run_id", "session_id", "attempt_id", "parent_id", "task_id"): _bounded(payload.get(name), name)
    if payload["role"] not in WORKER_ROLES: raise RegistryError("invalid worker role", 4)
    _hash(payload["brief_sha256"], "brief hash")
    attempt_id = (payload["run_id"], payload["session_id"], payload["attempt_id"]); existing = attempts.get(attempt_id)
    if event_type == "worker_attempt_started":
        if existing is not None: raise RegistryError("duplicate/conflicting attempt id", 4)
        retry_of, retry_number = payload["retry_of"], payload["retry_number"]
        if not isinstance(retry_number, int) or isinstance(retry_number, bool) or retry_number < 0: raise RegistryError("invalid retry number", 4)
        for name in ("retry_evidence_sha256", "retry_failure_reason"):
            if payload[name] is not None and not isinstance(payload[name], str): raise RegistryError("invalid retry provenance", 4)
        same_brief = [item for item in attempts.values() if item["payload"]["run_id"] == payload["run_id"] and item["payload"]["session_id"] == payload["session_id"] and item["payload"]["brief_sha256"] == payload["brief_sha256"]]
        if same_brief:
            _bounded(retry_of, "retry_of"); _bounded(payload["retry_failure_reason"], "retry failure reason", 512)
            if any(item["status"] in {"started", "observed"} for item in same_brief): raise RegistryError("same brief already has a nonterminal attempt", 4)
            numbers = [item["payload"]["retry_number"] for item in same_brief]
            if len(numbers) != len(set(numbers)): raise RegistryError("retry chain contains a branch/fork", 4)
            maximum = max(numbers); latest = [item for item in same_brief if item["payload"]["retry_number"] == maximum]
            prior = latest[0] if len(latest) == 1 else None
            if prior is None or prior["status"] != "failed" or prior["payload"]["attempt_id"] != retry_of or retry_number != maximum + 1: raise RegistryError("retry must extend unique latest failed attempt", 4)
            _hash(payload["retry_evidence_sha256"], "retry evidence")
            if payload["retry_evidence_sha256"] == prior["evidence"] or " ".join(payload["retry_failure_reason"].split()).casefold() == " ".join(prior["failure_reason"].split()).casefold(): raise RegistryError("retry must change both evidence and failure provenance", 4)
        elif retry_number != 0 or any(payload[name] is not None for name in ("retry_of", "retry_evidence_sha256", "retry_failure_reason")): raise RegistryError("first attempt cannot claim retry provenance", 4)
        attempts[attempt_id] = {"status":"started", "payload":payload, "evidence":None, "failure_reason":None}
        return
    if existing is None: raise RegistryError("attempt transition without start", 4)
    if any(payload[name] != existing["payload"][name] for name in ATTEMPT_COMMON): raise RegistryError("attempt identity changed", 4)
    _hash(payload["evidence_delta_sha256"], "evidence delta")
    if event_type == "worker_attempt_observed":
        if existing["status"] != "started": raise RegistryError("illegal attempt observation order", 4)
        existing.update(status="observed", evidence=payload["evidence_delta_sha256"]); return
    if event_type == "worker_attempt_completed":
        if existing["status"] != "observed" or not existing["evidence"]: raise RegistryError("completion requires observation/evidence", 4)
        if payload["evidence_delta_sha256"] == existing["evidence"]: raise RegistryError("evidence delta must change", 4)
        existing.update(status="completed", evidence=payload["evidence_delta_sha256"]); return
    if existing["status"] not in {"started", "observed"}: raise RegistryError("illegal attempt failure order", 4)
    if payload["evidence_delta_sha256"] == existing["evidence"]: raise RegistryError("evidence delta must change", 4)
    existing.update(status="failed", evidence=payload["evidence_delta_sha256"], failure_reason=_bounded(payload["failure_reason"], "failure reason", 512))


def add_attempt_parsers(subparsers: Any) -> None:
    """Attach typed advisory worker-attempt commands to the ledger CLI."""
    start = subparsers.add_parser("attempt-start")
    for parser in (start,):
        parser.add_argument("--run-id", required=True); parser.add_argument("--session-id", required=True); parser.add_argument("--attempt-id", required=True)
    start.add_argument("--role", required=True, choices=sorted(WORKER_ROLES)); start.add_argument("--brief", required=True); start.add_argument("--parent-id", required=True); start.add_argument("--task-id", required=True)
    start.add_argument("--retry-of"); start.add_argument("--retry-number", type=int, default=0); start.add_argument("--retry-evidence-sha256"); start.add_argument("--retry-failure-reason")
    for name in ("attempt-observe", "attempt-complete", "attempt-fail"):
        parser = subparsers.add_parser(name); parser.add_argument("--run-id", required=True); parser.add_argument("--session-id", required=True); parser.add_argument("--attempt-id", required=True); parser.add_argument("--evidence-delta-sha256", required=True)
        if name == "attempt-fail": parser.add_argument("--failure-reason", required=True)


def build_attempt_event(args: Any, state: dict[str, Any], events: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Build and validate the next attempt event against immutable history."""
    if args.run_id != state["run_id"] or args.session_id != state["owner"]["session_id"] or state["state"] != "active": raise RegistryError("attempt run/session/lifecycle conflict", 3)
    attempts: dict[Any, dict[str, Any]] = {}
    for event in events:
        if event["type"] in ATTEMPT_TYPES: apply_attempt_event(attempts, event["type"], event["payload"])
    kind = {"attempt-start":"worker_attempt_started", "attempt-observe":"worker_attempt_observed", "attempt-complete":"worker_attempt_completed", "attempt-fail":"worker_attempt_failed"}[args.command]
    if kind == "worker_attempt_started":
        common = {"run_id":args.run_id,"session_id":args.session_id,"attempt_id":args.attempt_id,"role":args.role,"brief_sha256":normalized_brief_hash(args.brief),"parent_id":args.parent_id,"task_id":args.task_id}
        payload = {**common,"retry_of":args.retry_of,"retry_number":args.retry_number,"retry_evidence_sha256":args.retry_evidence_sha256,"retry_failure_reason":args.retry_failure_reason}
    else:
        existing = attempts.get((args.run_id, args.session_id, args.attempt_id))
        if existing is None: raise RegistryError("attempt transition without start", 3)
        if existing["payload"]["run_id"] != args.run_id or existing["payload"]["session_id"] != args.session_id: raise RegistryError("attempt owner changed; start a new attempt", 3)
        payload = {**{name:existing["payload"][name] for name in ATTEMPT_COMMON}, "evidence_delta_sha256":args.evidence_delta_sha256}
        if kind == "worker_attempt_failed": payload["failure_reason"] = args.failure_reason
    trial = {key:{**value,"payload":dict(value["payload"])} for key,value in attempts.items()}
    try: apply_attempt_event(trial, kind, payload)
    except RegistryError as exc: raise RegistryError(str(exc), 3) from exc
    return kind, payload


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value): raise RegistryError(f"invalid {name}", 4)
    return value


def read_events(raw_lines: list[bytes]) -> list[dict[str, Any]]:
    events, previous = [], "0" * 64
    for sequence, raw in enumerate(raw_lines, start=1):
        try: event = json.loads(raw)
        except json.JSONDecodeError as exc: raise RegistryError("registry JSON corrupt", 4) from exc
        if not isinstance(event, dict) or set(event) != EVENT_KEYS or event.get("schema_version") != 1 or event.get("sequence") != sequence or event.get("type") not in TYPES: raise RegistryError("registry event schema mismatch", 4)
        unsigned = {key: event[key] for key in EVENT_KEYS - {"hash"}}
        expected = hashlib.sha256(canonical(unsigned)).hexdigest()
        if event["previous_hash"] != previous or event["hash"] != expected: raise RegistryError("registry hash chain mismatch", 4)
        try: datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc: raise RegistryError("registry timestamp invalid", 4) from exc
        if not isinstance(event["monotonic_ns"], int) or isinstance(event["monotonic_ns"], bool) or event["monotonic_ns"] < 0 or (events and event["monotonic_ns"] < events[-1]["monotonic_ns"]): raise RegistryError("registry monotonic clock invalid", 4)
        payload = event["payload"]
        if not isinstance(payload, dict): raise RegistryError("registry payload invalid", 4)
        for name in ("run_id_hash", "session_id_hash"): _hash(payload.get(name), name)
        activation_legacy = {"run_id_hash", "session_id_hash", "provider", "artifact_domain", "expected_controls"}
        activation_proven = activation_legacy | {"thread_id_hash", "session_meta_sha256"}
        activation_bound = activation_proven | {"transcript_path_hash", "project_hash"}
        if event["type"] == "activated" and (frozenset(payload) not in {frozenset(activation_legacy), frozenset(activation_proven), frozenset(activation_bound)} or payload["provider"] not in PROVIDERS or not isinstance(payload["artifact_domain"], str) or not payload["artifact_domain"] or not isinstance(payload["expected_controls"], list) or not payload["expected_controls"] or len(set(payload["expected_controls"])) != len(payload["expected_controls"]) or any(kind not in CONTROL_KINDS for kind in payload["expected_controls"])): raise RegistryError("activation payload invalid", 4)
        if event["type"] == "activated" and frozenset(payload) in {frozenset(activation_proven), frozenset(activation_bound)}:
            _hash(payload["thread_id_hash"], "activation thread"); _hash(payload["session_meta_sha256"], "activation metadata")
        if event["type"] == "activated" and set(payload) == activation_bound:
            for name in ("transcript_path_hash", "project_hash"): _hash(payload[name], f"activation {name}")
        if event["type"] in {"control_started", "control_ended"} and (set(payload) != {"run_id_hash", "session_id_hash", "kind"} or payload["kind"] not in CONTROL_KINDS): raise RegistryError("control payload invalid", 4)
        if event["type"] == "control_unavailable" and (set(payload) != {"run_id_hash", "session_id_hash", "kind", "reason"} or payload["kind"] not in CONTROL_KINDS or payload["reason"] not in CONTROL_NA_REASONS): raise RegistryError("control unavailable payload invalid", 4)
        if event["type"] == "verification_attempt" and (set(payload) != {"run_id_hash", "session_id_hash", "status", "receipt_sha256"} or payload["status"] not in {"pass", "fail"}): raise RegistryError("verification payload invalid", 4)
        if event["type"] == "verification_attempt": _hash(payload["receipt_sha256"], "verification receipt")
        if event["type"] == "terminal" and set(payload) != {"run_id_hash", "session_id_hash", "ledger_tail_hash", "handoff_sha256", "terminal_at"}: raise RegistryError("terminal payload invalid", 4)
        if event["type"] == "terminal":
            _hash(payload["ledger_tail_hash"], "terminal ledger"); _hash(payload["handoff_sha256"], "terminal handoff")
            try: datetime.fromisoformat(payload["terminal_at"].replace("Z", "+00:00"))
            except (AttributeError, ValueError) as exc: raise RegistryError("terminal authoritative timestamp invalid", 4) from exc
        if event["type"] == "domain_outcome" and (set(payload) != {"run_id_hash", "session_id_hash", "next_domain"} or (payload["next_domain"] is not None and (not isinstance(payload["next_domain"], str) or not payload["next_domain"]))): raise RegistryError("domain outcome invalid", 4)
        if event["type"] == "excluded" and (set(payload) != {"run_id_hash", "session_id_hash", "reason", "evidence_sha256"} or payload["reason"] not in EXCLUSION_REASONS): raise RegistryError("exclusion payload invalid", 4)
        if event["type"] == "excluded": _hash(payload["evidence_sha256"], "exclusion evidence")
        previous, events = expected, [*events, event]
    return events


def session_views(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    views: dict[str, dict[str, Any]] = {}
    for event in events:
        payload, session = event["payload"], event["payload"]["session_id_hash"]
        view = views.setdefault(session, {"activation": None, "terminal": None, "outcome": None, "excluded": None, "attempts": [], "controls": [], "unavailable": set(), "open": {}, "ordering_unknown": False})
        if event["type"] == "activated":
            if view["activation"] is not None: raise RegistryError("duplicate activation session", 4)
            view["activation"] = event
            continue
        if view["activation"] is None: raise RegistryError("session event precedes activation", 4)
        if payload["run_id_hash"] != view["activation"]["payload"]["run_id_hash"]: raise RegistryError("session run identity changed", 4)
        if view["outcome"] is not None and not (event["type"] in {"control_started", "control_ended", "control_unavailable"} and payload.get("kind") in {"telemetry", "calibration"}): raise RegistryError("session event follows domain outcome", 4)
        if event["type"] == "terminal":
            if view["terminal"] is not None or view["open"]: raise RegistryError("invalid/duplicate terminal event", 4)
            view["terminal"] = event
            continue
        if event["type"] == "domain_outcome":
            if view["outcome"] is not None: raise RegistryError("duplicate domain outcome", 4)
            if view["terminal"] is None: raise RegistryError("domain outcome precedes terminal", 4)
            if payload["next_domain"] is None or payload["next_domain"] == view["activation"]["payload"]["artifact_domain"]: raise RegistryError("domain transition must be nonvacuous", 4)
            view["outcome"] = event
        elif event["type"] == "excluded":
            if view["excluded"] is not None: raise RegistryError("duplicate exclusion", 4)
            view["excluded"] = event
        elif event["type"] == "verification_attempt": view["attempts"].append(event)
        elif event["type"] == "control_started":
            view["open"].setdefault(payload["kind"], []).append(event)
        elif event["type"] == "control_unavailable":
            stack = view["open"].get(payload["kind"])
            if not stack:
                if view["outcome"] is not None: raise RegistryError("control unavailable without start", 4)
                view["ordering_unknown"] = True; view["unavailable"].add(payload["kind"]); continue
            view["unavailable"].add(payload["kind"]); stack.pop()
            if not stack: view["open"].pop(payload["kind"])
        elif event["type"] == "control_ended":
            stack = view["open"].get(payload["kind"])
            if not stack: raise RegistryError("control end without start", 4)
            start = stack.pop()
            if not stack: view["open"].pop(payload["kind"])
            view["controls"].append((start, event))
    return views
