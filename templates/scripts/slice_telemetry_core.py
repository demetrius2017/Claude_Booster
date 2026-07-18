"""Bounded, privacy-preserving Slice 4A telemetry primitives.

Purpose: Normalize explicit Codex rollout-v1 and Booster Claude wrapper-v1
JSONL into diagnostic metrics without retaining conversational content.
Contract: Input is line-bounded, provider metadata is exact, identifiers are
hashed, cumulative counters are differenced per thread, and unknown evidence
always lowers coverage rather than becoming a zero.
CLI/Examples: Used by ``slice_telemetry.py inspect|record|status``.
Limitations: Post-hoc advisory observation only; no native-agent enforcement,
semantic progress inference, efficiency score, or transcript-content storage.
ENV/Files: Reads caller-supplied JSONL files; performs no writes itself.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_SOURCE = 32 * 1024 * 1024
MAX_LINE = 256 * 1024
MAX_ROWS = 200_000
CODEX_VERSION_FAMILIES = ("0.145.",)


class TelemetryError(Exception):
    """Typed telemetry failure with stable CLI exit code."""

    def __init__(self, message: str, code: int = 3) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Thread:
    raw_id: str
    root_id: str
    parent_id: str | None
    depth: int
    starts: list[float] = field(default_factory=list)
    terminals: list[float] = field(default_factory=list)
    waits: int = 0
    spawns: set[str] = field(default_factory=set)
    spawn_times: list[float] = field(default_factory=list)
    progress: int = 0
    token_last: int | None = None
    token_max: int = 0
    token_delta: int = 0
    cached_max: int = 0
    token_reset: bool = False
    unknown: list[str] = field(default_factory=list)


@dataclass
class Parsed:
    provider: str
    adapter: str
    project_hash: str
    root_session_hash: str
    threads: dict[str, Thread]
    source_generations: list[dict[str, Any]]
    eligible: int = 0
    observed: int = 0
    dropped: int = 0
    unknown: list[str] = field(default_factory=list)
    evidence_sets: list[dict[str, Any]] = field(default_factory=list)
    unknown_fingerprints: set[str] = field(default_factory=set)


def _metric(value: Any, expected: int, observed: int, status: str, reasons: list[str], evidence_ids: list[str], evidence_counts: dict[str, int], denominator: int | None = None) -> dict[str, Any]:
    return {
        "value": value, "denominator": expected if denominator is None else denominator,
        "observed": observed, "expected": expected, "coverage_status": status,
        "unknown_reasons": sorted(set(reasons)), "evidence_set_ids": sorted(set(evidence_ids)), "evidence_counts": evidence_counts,
    }


def _ranges(rows: list[int]) -> dict[str, Any]:
    ordered = sorted(set(rows))
    ranges: list[str] = []
    if ordered:
        start = previous = ordered[0]
        for number in ordered[1:]:
            if number != previous + 1:
                ranges.append(str(start) if start == previous else f"{start}-{previous}")
                start = number
            previous = number
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
    encoded = canonical(ranges)
    return {"count": len(ordered), "ranges": ranges[:64], "ranges_truncated": len(ranges) > 64, "ranges_sha256": hashlib.sha256(encoded).hexdigest()}


def _secure_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise TelemetryError("transcript must be a regular non-symlink file", 4)
    info = path.stat()
    if info.st_size > MAX_SOURCE:
        raise TelemetryError("transcript exceeds 32 MiB bound", 5)
    rows: list[dict[str, Any]] = []
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, start=1):
            if number > MAX_ROWS:
                raise TelemetryError("transcript row bound exceeded", 5)
            hasher.update(raw)
            if len(raw) > MAX_LINE:
                raise TelemetryError("transcript line bound exceeded", 5)
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TelemetryError(f"truncated or malformed JSONL at row {number}", 4) from exc
            if not isinstance(value, dict):
                raise TelemetryError(f"non-object JSONL row {number}", 4)
            rows.append(value)
    generation = {"source_sha256": hasher.hexdigest(), "size": info.st_size, "rows": len(rows), "generation": digest(f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}")}
    return rows, generation


def _project_ok(raw: Any, root: Path) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        Path(raw).resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _new_thread(raw_id: str, root_id: str, parent: str | None, depth: int) -> Thread:
    if not raw_id or not root_id or depth < 0 or (depth == 0 and parent is not None) or (depth > 0 and not parent):
        raise TelemetryError("invalid thread identity metadata", 4)
    return Thread(raw_id, root_id, parent, depth)


def _tokens(thread: Thread, payload: dict[str, Any]) -> bool:
    cached = payload.get("cached_input_tokens", payload.get("cache_read_input_tokens", 0))
    total = payload.get("total_tokens")
    if total is None:
        input_tokens, output_tokens = payload.get("input_tokens"), payload.get("output_tokens")
        total = input_tokens + output_tokens if isinstance(input_tokens, int) and isinstance(output_tokens, int) else None
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in (total, cached)):
        thread.unknown.append("invalid_cumulative_token_counter")
        return False
    if thread.token_last is not None and total < thread.token_last:
        thread.token_reset = True
        thread.unknown.append("counter_reset")
        thread.token_delta += total
    else:
        thread.token_delta += total if thread.token_last is None else total - thread.token_last
    thread.token_last = total
    thread.token_max = max(thread.token_max, total)
    thread.cached_max = max(thread.cached_max, cached)
    return True


def _codex(paths: list[Path], root: Path) -> Parsed:
    parsed: Parsed | None = None
    for path in paths:
        rows, generation = _secure_rows(path)
        if not rows or rows[0].get("type") != "session_meta":
            raise TelemetryError("codex_rollout_v1 requires leading session_meta", 4)
        meta = rows[0].get("payload")
        if not isinstance(meta, dict) or not isinstance(meta.get("cli_version"), str) or not any(meta["cli_version"].startswith(item) for item in CODEX_VERSION_FAMILIES) or not _project_ok(meta.get("cwd"), root):
            raise TelemetryError("unsupported Codex metadata/version/project", 4)
        raw_id, root_id = meta.get("id"), meta.get("session_id")
        parent, source = meta.get("parent_thread_id"), meta.get("source")
        if meta.get("thread_source") == "user":
            parent, depth = None, 0
        elif meta.get("thread_source") == "subagent" and isinstance(source, dict) and isinstance(source.get("subagent"), dict) and isinstance(source["subagent"].get("thread_spawn"), dict):
            spawn_meta = source["subagent"]["thread_spawn"]
            parent, depth = spawn_meta.get("parent_thread_id", parent), spawn_meta.get("depth")
        else:
            raise TelemetryError("unsupported Codex thread source", 4)
        if not isinstance(raw_id, str) or not isinstance(root_id, str) or not isinstance(depth, int):
            raise TelemetryError("invalid Codex identity metadata", 4)
        if parsed is None:
            parsed = Parsed("codex", "codex_rollout_v1", digest(str(root.resolve())), digest(root_id), {}, [])
        if parsed.root_session_hash != digest(root_id) or raw_id in parsed.threads:
            raise TelemetryError("mixed root or duplicate Codex thread", 4)
        thread = _new_thread(raw_id, root_id, parent, depth)
        parsed.threads[raw_id] = thread
        parsed.source_generations.append(generation)
        ref_prefix = f"src:{generation['source_sha256'][:16]}"
        recognized_rows: list[int] = []
        unknown_rows: list[int] = []
        pending_spawns: dict[str, float] = {}
        for index, row in enumerate(rows[1:], start=2):
            parsed.eligible += 1
            when = timestamp(row.get("timestamp"))
            kind, payload = row.get("type"), row.get("payload")
            if when is None or not isinstance(payload, dict):
                parsed.unknown.append("missing_timestamp_or_payload")
                unknown_rows.append(index)
                continue
            event = payload.get("type")
            recognized = False
            if kind == "event_msg" and event == "task_started":
                thread.starts.append(when); recognized = True
            elif kind == "event_msg" and event == "task_completed":
                thread.terminals.append(when); recognized = True
            elif kind == "event_msg" and event == "token_count":
                info = payload.get("info")
                usage = info.get("total_token_usage") if isinstance(info, dict) else None
                recognized = _tokens(thread, usage) if isinstance(usage, dict) else False
                if not recognized:
                    thread.unknown.append("missing_token_usage")
            elif kind == "response_item" and event == "function_call":
                name = payload.get("name")
                if name in {"spawn_agent", "collaboration.spawn_agent"}:
                    call_id = payload.get("call_id")
                    if isinstance(call_id, str) and call_id:
                        pending_spawns[call_id] = when; recognized = True
                    else:
                        thread.unknown.append("spawn_missing_call_identity")
                elif name in {"wait_agent", "collaboration.wait_agent"}:
                    thread.waits += 1; recognized = True
                elif name in {"update_plan", "functions.update_plan"}:
                    thread.progress += 1; recognized = True
                elif name in {"exec_command", "write_stdin", "send_message", "list_agents", "view_image", "apply_patch"}:
                    recognized = True
                else:
                    parsed.unknown.append("unknown_tool"); parsed.unknown_fingerprints.add(digest(str(name)))
            elif kind == "response_item" and event == "function_call_output":
                call_id = payload.get("call_id")
                if isinstance(call_id, str) and call_id in pending_spawns:
                    thread.spawns.add(digest(call_id)); thread.spawn_times.append(pending_spawns.pop(call_id))
                recognized = True
            elif kind == "response_item" and event in {"message", "reasoning"}:
                recognized = True
            if recognized:
                parsed.observed += 1; recognized_rows.append(index)
            else:
                parsed.unknown.append("unsupported_row_shape"); parsed.unknown_fingerprints.add(digest(f"{kind}:{event}"))
                unknown_rows.append(index)
        parsed.evidence_sets.append({"evidence_set_id": ref_prefix, "source_sha256": generation["source_sha256"], "recognized": _ranges(recognized_rows), "unknown": _ranges(unknown_rows)})
    if parsed is None:
        raise TelemetryError("no Codex transcript sources", 2)
    return parsed


def _claude(paths: list[Path], root: Path) -> Parsed:
    if len(paths) != 1:
        raise TelemetryError("booster_wrapper_v1 uses one normalized source", 2)
    rows, generation = _secure_rows(paths[0])
    if not rows or rows[0].get("type") != "booster_wrapper_meta":
        raise TelemetryError("booster_wrapper_v1 requires leading metadata", 4)
    meta = rows[0]
    if meta.get("schema_version") != 1 or meta.get("provider") != "booster_wrapper_v1" or meta.get("wrapper_version") != 1 or not _project_ok(meta.get("cwd"), root):
        raise TelemetryError("unsupported Booster wrapper metadata/version/project", 4)
    root_id = meta.get("root_session_id")
    if not isinstance(root_id, str):
        raise TelemetryError("invalid wrapper root identity", 4)
    parsed = Parsed("claude", "booster_wrapper_v1", digest(str(root.resolve())), digest(root_id), {}, [generation])
    recognized_rows: list[int] = []
    unknown_rows: list[int] = []
    for index, row in enumerate(rows[1:], start=2):
        parsed.eligible += 1
        raw_id, event, when = row.get("thread_id"), row.get("event"), timestamp(row.get("timestamp"))
        if not isinstance(raw_id, str) or when is None:
            parsed.unknown.append("missing_wrapper_identity_or_timestamp"); unknown_rows.append(index); continue
        thread = parsed.threads.get(raw_id)
        if thread is None:
            parent, depth = row.get("parent_thread_id"), row.get("depth")
            if not isinstance(depth, int):
                parsed.unknown.append("missing_wrapper_depth"); unknown_rows.append(index); continue
            thread = _new_thread(raw_id, root_id, parent, depth); parsed.threads[raw_id] = thread
        recognized = True
        if event == "start": thread.starts.append(when)
        elif event == "terminal": thread.terminals.append(when)
        elif event == "spawn":
            child = row.get("child_thread_id")
            if isinstance(child, str): thread.spawns.add(child); thread.spawn_times.append(when)
            else: recognized = False; thread.unknown.append("spawn_missing_child_identity")
        elif event == "wait": thread.waits += 1
        elif event == "progress": thread.progress += 1
        elif event == "token_count": recognized = _tokens(thread, row)
        else: recognized = False; parsed.unknown.append("unsupported_wrapper_event"); parsed.unknown_fingerprints.add(digest(str(event)))
        if recognized:
            parsed.observed += 1; recognized_rows.append(index)
        else:
            unknown_rows.append(index)
    parsed.evidence_sets.append({"evidence_set_id": f"src:{generation['source_sha256'][:16]}", "source_sha256": generation["source_sha256"], "recognized": _ranges(recognized_rows), "unknown": _ranges(unknown_rows)})
    return parsed


def parse(provider: str, paths: list[Path], root: Path) -> Parsed:
    """Parse only an explicitly selected, versioned provider adapter."""
    if provider == "codex_rollout_v1":
        return _codex(paths, root)
    if provider == "booster_wrapper_v1":
        return _claude(paths, root)
    raise TelemetryError("unsupported provider adapter; guessing forbidden", 2)


def report(parsed: Parsed) -> dict[str, Any]:
    """Produce coverage-bearing diagnostics without an efficiency score."""
    threads = list(parsed.threads.values())
    roots = [item for item in threads if item.depth == 0]
    children = [item for item in threads if item.depth > 0]
    successful_spawns = sum(len(item.spawns) for item in threads)
    orphan_children = max(0, len(children) - successful_spawns)
    dedup_spawns = successful_spawns
    incomplete = [item for item in children if not item.terminals]
    unknown = [*parsed.unknown, *(reason for item in threads for reason in item.unknown)]
    if orphan_children:
        unknown.append("missing_parent_spawn")
    coverage = parsed.observed / parsed.eligible if parsed.eligible else None
    base_status = "complete" if not unknown else "partial"
    refs = [item["evidence_set_id"] for item in parsed.evidence_sets]
    counts = {"recognized": parsed.observed, "unknown": parsed.eligible - parsed.observed}
    waits = {"root": sum(item.waits for item in roots), "child": sum(item.waits for item in children), "all": sum(item.waits for item in threads)}
    token_delta = sum(item.token_delta for item in threads)
    metrics = {
        "spawns": _metric(dedup_spawns, max(1, len(threads)), len(threads), base_status, unknown, refs, counts, len(threads)),
        "waits": _metric(waits, len(threads), len(threads), base_status, unknown, refs, counts),
        "progress": _metric(sum(item.progress for item in threads), len(threads), len(threads), base_status, unknown, refs, counts),
        "tokens": _metric({"segmented_lower_bound": token_delta, "cumulative_max": sum(item.token_max for item in threads), "cached_snapshot_max": sum(item.cached_max for item in threads)}, len(threads), len(threads), "partial" if any(item.token_reset for item in threads) else base_status, [*unknown, *(["counter_reset"] if any(item.token_reset for item in threads) else [])], refs, counts),
        "background_completion": _metric(None if incomplete else len(children), len(children), len(children) - len(incomplete), "right_censored" if incomplete else "partial" if orphan_children else "complete", ["background_thread_without_terminal"] if incomplete else ["missing_parent_spawn"] if orphan_children else [], refs, counts),
        "parser_coverage": _metric(coverage, parsed.eligible, parsed.observed, "unsupported" if parsed.eligible and not parsed.observed else base_status, unknown, refs, counts, parsed.eligible),
    }
    identities = [{"thread_hash": digest(item.raw_id), "root_hash": digest(item.root_id), "parent_hash": digest(item.parent_id) if item.parent_id else None, "depth": item.depth} for item in threads]
    starts = [value for item in threads for value in item.starts]
    workers = [value for item in threads for value in item.spawn_times]
    clock_facts = {"clock": "transcript_wall", "session_start": min(starts) if starts else None, "first_worker": min(workers) if workers else None}
    return {"schema_version": 1, "adapter": parsed.adapter, "provider": parsed.provider, "project_hash": parsed.project_hash, "root_session_hash": parsed.root_session_hash, "thread_identities": sorted(identities, key=lambda item: (item["depth"], item["thread_hash"])), "source_generations": parsed.source_generations, "evidence_index": parsed.evidence_sets, "clock_facts": clock_facts, "metrics": metrics, "dropped_rows": parsed.dropped, "unknown_reasons": sorted(set(unknown)), "unknown_shape_hashes": sorted(parsed.unknown_fingerprints), "limitations": ["advisory_post_hoc_only", "wall_and_provider_cumulative_clocks_not_interchangeable", "no_native_enforcement_claim"]}
