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


class RegistryError(Exception):
    def __init__(self, message: str, code: int = 3) -> None: super().__init__(message); self.code = code


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
        if event["type"] == "activated" and (set(payload) != {"run_id_hash", "session_id_hash", "provider", "artifact_domain", "expected_controls"} or payload["provider"] not in PROVIDERS or not isinstance(payload["artifact_domain"], str) or not payload["artifact_domain"] or not isinstance(payload["expected_controls"], list) or not payload["expected_controls"] or len(set(payload["expected_controls"])) != len(payload["expected_controls"]) or any(kind not in CONTROL_KINDS for kind in payload["expected_controls"])): raise RegistryError("activation payload invalid", 4)
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
        view = views.setdefault(session, {"activation": None, "terminal": None, "outcome": None, "excluded": None, "attempts": [], "controls": [], "unavailable": set(), "open": {}})
        if event["type"] == "activated":
            if view["activation"] is not None: raise RegistryError("duplicate activation session", 4)
            view["activation"] = event
            continue
        if view["activation"] is None: raise RegistryError("session event precedes activation", 4)
        if payload["run_id_hash"] != view["activation"]["payload"]["run_id_hash"]: raise RegistryError("session run identity changed", 4)
        if view["outcome"] is not None: raise RegistryError("session event follows domain outcome", 4)
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
            if payload["kind"] in view["open"]: raise RegistryError("overlapping control observation", 4)
            view["open"][payload["kind"]] = event
        elif event["type"] == "control_unavailable":
            view["unavailable"].add(payload["kind"])
        elif event["type"] == "control_ended":
            start = view["open"].pop(payload["kind"], None)
            if start is None: raise RegistryError("control end without start", 4)
            view["controls"].append((start, event))
    return views
