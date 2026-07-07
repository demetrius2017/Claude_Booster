#!/usr/bin/env python3
"""Fable 5 usage ledger and display helper.

Purpose:
    Parse Claude/Codex transcript JSONL rows for Fable 5 assistant usage,
    persist an immutable deduped event ledger, and maintain a tiny statusline
    cache with API-equivalent credit-rate cost estimates.

Contract:
    Hook mode reads Stop JSON from stdin with session_id, transcript_path, cwd.
    It ingests the transcript and sibling subagents/*.jsonl files, updates the
    ledger/cache when possible, and exits 0 on every error.

CLI/Examples:
    echo '{"session_id":"s","transcript_path":"/tmp/t.jsonl"}' | python3 fable_usage.py
    python3 fable_usage.py ingest --transcript /tmp/t.jsonl --json
    python3 fable_usage.py summary --brief
    python3 fable_usage.py display
    python3 fable_usage.py refresh-display

Limitations:
    This is not a billing ledger. It is an API-equivalent / credit-rate
    estimate from transcript token usage. Unknown model names are ignored
    fail-open until explicitly mapped.

ENV/Files:
    ~/.claude/rolling_memory.db
    ~/.claude/fable_usage_summary.json
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path.home() / ".claude" / "rolling_memory.db"
SUMMARY_CACHE_PATH = Path.home() / ".claude" / "fable_usage_summary.json"
REFRESH_LOCK_PATH = Path.home() / ".claude" / ".fable_refresh.lock"

MODEL_MAP = {
    "claude-fable-5": "fable-5",
}

# Fable 5 was included in Pro/Max/Team plan limits (up to 50% of the weekly
# usage limit) *through* 2026-07-07; on/after this UTC instant continued use
# moves to metered usage credits at API rates. Events before the cutover are
# "included" (no real credit spend); events on/after are "credits" (real money).
# If Anthropic publishes a different flip time, change ONLY this constant.
FABLE_CREDIT_CUTOVER_UTC = "2026-07-08T00:00:00Z"

USD_NANOS_PER_TOKEN = {
    "input": 10_000,          # $10 / MTok
    "output": 50_000,         # $50 / MTok
    "cache_read": 1_000,      # $1 / MTok
    "cache_write_5m": 12_500, # $12.50 / MTok
    "cache_write_1h": 20_000, # $20 / MTok
}

PRICING_JSON = json.dumps(
    {
        "basis": "API-equivalent / credit-rate estimate; not an actual billing ledger",
        "usd_per_mtok": {
            "input": 10.0,
            "output": 50.0,
            "cache_read": 1.0,
            "cache_write_5m": 12.5,
            "cache_write_1h": 20.0,
        },
    },
    sort_keys=True,
)

DDL = """
CREATE TABLE IF NOT EXISTS fable_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL,
    assistant_message_id TEXT,
    source_path TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    task_key TEXT NOT NULL DEFAULT '',
    project_root TEXT,
    ts_utc TEXT NOT NULL,
    month_utc TEXT NOT NULL,
    model TEXT NOT NULL,
    canonical_model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cache_creation_5m_tokens >= 0),
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cache_creation_1h_tokens >= 0),
    cache_read_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cache_read_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
    cost_usd_nanos INTEGER NOT NULL DEFAULT 0 CHECK(cost_usd_nanos >= 0),
    pricing_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fable_usage_event_key
    ON fable_usage_events(event_key);
CREATE INDEX IF NOT EXISTS idx_fable_usage_month
    ON fable_usage_events(month_utc, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_fable_usage_task
    ON fable_usage_events(task_key, ts_utc DESC);
CREATE TRIGGER IF NOT EXISTS fable_usage_events_no_update
BEFORE UPDATE ON fable_usage_events
BEGIN
    SELECT RAISE(ABORT, 'fable_usage_events is immutable');
END;
CREATE TRIGGER IF NOT EXISTS fable_usage_events_no_delete
BEFORE DELETE ON fable_usage_events
BEGIN
    SELECT RAISE(ABORT, 'fable_usage_events is immutable');
END;
"""


@dataclass(frozen=True)
class FableEvent:
    event_key: str
    assistant_message_id: str | None
    source_path: str
    line_number: int
    session_id: str
    task_key: str
    project_root: str | None
    ts_utc: str
    month_utc: str
    model: str
    canonical_model: str
    input_tokens: int
    cache_creation_5m_tokens: int
    cache_creation_1h_tokens: int
    cache_read_tokens: int
    output_tokens: int
    cost_usd_nanos: int


def _int_token(value: Any) -> int:
    try:
        tokens = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(tokens, 0)


def _iso_utc(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_utc(ts_utc: str) -> str:
    return ts_utc[:7]


def _cache_creation_tokens(usage: dict[str, Any]) -> tuple[int, int]:
    nested = usage.get("cache_creation")
    if isinstance(nested, dict):
        five = _int_token(
            nested.get("ephemeral_5m_input_tokens")
            or nested.get("cache_creation_5m_input_tokens")
            or nested.get("cache_creation_input_tokens_5m")
        )
        one_hour = _int_token(
            nested.get("ephemeral_1h_input_tokens")
            or nested.get("cache_creation_1h_input_tokens")
            or nested.get("cache_creation_input_tokens_1h")
        )
        if five or one_hour:
            return five, one_hour

    five = _int_token(
        usage.get("cache_creation_5m_input_tokens")
        or usage.get("cache_creation_input_tokens_5m")
    )
    one_hour = _int_token(
        usage.get("cache_creation_1h_input_tokens")
        or usage.get("cache_creation_input_tokens_1h")
    )
    if five or one_hour:
        return five, one_hour

    # Existing Claude JSONL usually has no TTL split. Per contract, treat it
    # as 5-minute cache write pricing.
    return _int_token(usage.get("cache_creation_input_tokens")), 0


def _cost_nanos(
    input_tokens: int,
    cache_creation_5m_tokens: int,
    cache_creation_1h_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
) -> int:
    return (
        input_tokens * USD_NANOS_PER_TOKEN["input"]
        + cache_creation_5m_tokens * USD_NANOS_PER_TOKEN["cache_write_5m"]
        + cache_creation_1h_tokens * USD_NANOS_PER_TOKEN["cache_write_1h"]
        + cache_read_tokens * USD_NANOS_PER_TOKEN["cache_read"]
        + output_tokens * USD_NANOS_PER_TOKEN["output"]
    )


def _event_key(
    assistant_message_id: str | None,
    source_path: Path,
    line_number: int,
    model: str,
    token_tuple: tuple[int, int, int, int, int],
) -> str:
    if assistant_message_id:
        return f"assistant-message:{assistant_message_id}"
    tokens = ",".join(str(part) for part in token_tuple)
    return f"fallback:{source_path}:{line_number}:{model}:{tokens}"


def _task_key(source_path: Path, session_id: str) -> str:
    return f"{session_id}:{source_path}" if session_id else str(source_path)


def _iter_transcript_paths(transcript: Path) -> list[Path]:
    paths: list[Path] = []
    if transcript.exists() and transcript.is_file():
        paths.append(transcript)
    subagents_dir = transcript.parent / "subagents"
    if subagents_dir.exists() and subagents_dir.is_dir():
        paths.extend(sorted(p for p in subagents_dir.glob("*.jsonl") if p.is_file()))
    return paths


def _iter_project_jsonl(root: Path) -> list[Path]:
    """Return candidate Claude transcript JSONL files under ``root``.

    This is intentionally used only by explicit CLI scans, never by statusline
    rendering or the Stop hook hot path.
    """
    try:
        if not root.exists() or not root.is_dir():
            return []
        return sorted(p for p in root.rglob("*.jsonl") if p.is_file())
    except OSError:
        return []


def _message_from_row(obj: dict[str, Any]) -> dict[str, Any] | None:
    if obj.get("type") == "assistant" and isinstance(obj.get("message"), dict):
        return obj["message"]
    payload = obj.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("message"), dict):
        message = payload["message"]
        if message.get("role") == "assistant" or message.get("model"):
            return message
    return None


def _session_id_from_row(obj: dict[str, Any], default: str) -> str:
    payload = obj.get("payload")
    if isinstance(payload, dict):
        return str(obj.get("sessionId") or payload.get("session_id") or payload.get("id") or default or "")
    return str(obj.get("sessionId") or default or "")


def _project_root_from_row(obj: dict[str, Any], default: str | None) -> str | None:
    payload = obj.get("payload")
    if isinstance(payload, dict):
        return str(obj.get("cwd") or payload.get("cwd") or default or "") or None
    return str(obj.get("cwd") or default or "") or None


def parse_transcripts(
    transcript_paths: Iterable[Path],
    *,
    session_id: str = "",
    project_root: str | None = None,
) -> list[FableEvent]:
    events: list[FableEvent] = []
    seen_keys: set[str] = set()
    for path in transcript_paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line_number, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    message = _message_from_row(obj)
                    if message is None:
                        continue
                    model = str(message.get("model") or "")
                    canonical_model = MODEL_MAP.get(model)
                    if canonical_model is None:
                        continue
                    usage = message.get("usage")
                    if not isinstance(usage, dict):
                        continue

                    input_tokens = _int_token(usage.get("input_tokens"))
                    cache_5m, cache_1h = _cache_creation_tokens(usage)
                    cache_read_tokens = _int_token(usage.get("cache_read_input_tokens"))
                    output_tokens = _int_token(usage.get("output_tokens"))
                    token_tuple = (
                        input_tokens,
                        cache_5m,
                        cache_1h,
                        cache_read_tokens,
                        output_tokens,
                    )
                    if sum(token_tuple) <= 0:
                        continue

                    msg_id = message.get("id")
                    assistant_message_id = str(msg_id) if msg_id else None
                    event_session_id = _session_id_from_row(obj, session_id)
                    event_project_root = _project_root_from_row(obj, project_root)
                    ts_utc = _iso_utc(obj.get("timestamp"))
                    key = _event_key(assistant_message_id, path, line_number, model, token_tuple)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    events.append(
                        FableEvent(
                            event_key=key,
                            assistant_message_id=assistant_message_id,
                            source_path=str(path),
                            line_number=line_number,
                            session_id=event_session_id,
                            task_key=_task_key(path, event_session_id),
                            project_root=event_project_root,
                            ts_utc=ts_utc,
                            month_utc=_month_utc(ts_utc),
                            model=model,
                            canonical_model=canonical_model,
                            input_tokens=input_tokens,
                            cache_creation_5m_tokens=cache_5m,
                            cache_creation_1h_tokens=cache_1h,
                            cache_read_tokens=cache_read_tokens,
                            output_tokens=output_tokens,
                            cost_usd_nanos=_cost_nanos(
                                input_tokens,
                                cache_5m,
                                cache_1h,
                                cache_read_tokens,
                                output_tokens,
                            ),
                        )
                    )
        except OSError:
            continue
    return events


def _connect(*, create: bool) -> sqlite3.Connection | None:
    if not DB_PATH.exists() and not create:
        return None
    if create:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    if create:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(DDL)
    return conn


def persist_events(events: list[FableEvent], *, create_db: bool) -> tuple[int, int]:
    if not events:
        return 0, 0
    conn = _connect(create=create_db)
    if conn is None:
        return 0, 0
    inserted = 0
    try:
        for event in events:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO fable_usage_events (
                    event_key, assistant_message_id, source_path, line_number,
                    session_id, task_key, project_root, ts_utc, month_utc, model,
                    canonical_model, input_tokens, cache_creation_5m_tokens,
                    cache_creation_1h_tokens, cache_read_tokens, output_tokens,
                    cost_usd_nanos, pricing_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_key,
                    event.assistant_message_id,
                    event.source_path,
                    event.line_number,
                    event.session_id,
                    event.task_key,
                    event.project_root,
                    event.ts_utc,
                    event.month_utc,
                    event.model,
                    event.canonical_model,
                    event.input_tokens,
                    event.cache_creation_5m_tokens,
                    event.cache_creation_1h_tokens,
                    event.cache_read_tokens,
                    event.output_tokens,
                    event.cost_usd_nanos,
                    PRICING_JSON,
                ),
            )
            inserted += cur.rowcount if cur.rowcount else 0
        conn.commit()
        return inserted, len(events)
    finally:
        conn.close()


def _usd(cost_usd_nanos: int, places: int = 4) -> str:
    value = Decimal(cost_usd_nanos) / Decimal(1_000_000_000)
    quantum = Decimal(1).scaleb(-places)
    return str(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _current_month_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def build_summary(*, create_db: bool = False, session_id: str | None = None) -> dict[str, Any]:
    conn = _connect(create=create_db)
    if conn is None:
        return {"display_enabled": False}
    try:
        month = _current_month_utc()
        mtd = conn.execute(
            """
            SELECT COUNT(*) AS events, COALESCE(SUM(cost_usd_nanos), 0) AS cost
            FROM fable_usage_events
            WHERE month_utc = ?
            """,
            (month,),
        ).fetchone()
        # Split the month into the free "included" window (before the credit
        # cutover) and the billable "credits" window (on/after it). ts_utc is a
        # fixed-width ISO string, so a lexicographic '<' comparison is a correct
        # instant comparison. mtd_credits is the only figure that maps to real
        # money; mtd/mtd_included stay for reference.
        mtd_included = conn.execute(
            """
            SELECT COUNT(*) AS events, COALESCE(SUM(cost_usd_nanos), 0) AS cost
            FROM fable_usage_events
            WHERE month_utc = ? AND ts_utc < ?
            """,
            (month, FABLE_CREDIT_CUTOVER_UTC),
        ).fetchone()
        mtd_credits = conn.execute(
            """
            SELECT COUNT(*) AS events, COALESCE(SUM(cost_usd_nanos), 0) AS cost
            FROM fable_usage_events
            WHERE month_utc = ? AND ts_utc >= ?
            """,
            (month, FABLE_CREDIT_CUTOVER_UTC),
        ).fetchone()
        last = conn.execute(
            """
            SELECT task_key, session_id, source_path, MAX(ts_utc) AS ts_utc,
                   COUNT(*) AS events, COALESCE(SUM(cost_usd_nanos), 0) AS cost
            FROM fable_usage_events
            GROUP BY task_key, session_id, source_path
            ORDER BY MAX(ts_utc) DESC, MAX(id) DESC
            LIMIT 1
            """
        ).fetchone()
        last_event = conn.execute(
            """
            SELECT event_key, assistant_message_id, session_id, source_path,
                   line_number, ts_utc, model, canonical_model,
                   input_tokens, cache_creation_5m_tokens,
                   cache_creation_1h_tokens, cache_read_tokens, output_tokens,
                   cost_usd_nanos
            FROM fable_usage_events
            ORDER BY ts_utc DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        # Local (Dubai UTC+4) day bucket. Strip the trailing 'Z' before date()
        # because older SQLite rejects the 'Z' suffix; MTD stays on the UTC month.
        today = conn.execute(
            """
            SELECT COUNT(*) AS events, COALESCE(SUM(cost_usd_nanos), 0) AS cost
            FROM fable_usage_events
            WHERE date(replace(ts_utc, 'Z', ''), '+4 hours') = date('now', '+4 hours')
            """
        ).fetchone()
        sid = session_id or ""
        if sid:
            # True per-session sum: session_id only, NO source_path grouping, so a
            # session spanning main + subagents/*.jsonl is fully counted.
            session_row = conn.execute(
                """
                SELECT COUNT(*) AS events, COALESCE(SUM(cost_usd_nanos), 0) AS cost
                FROM fable_usage_events
                WHERE session_id = ?
                """,
                (sid,),
            ).fetchone()
            session_events = int(session_row["events"] if session_row else 0)
            session_cost = int(session_row["cost"] if session_row else 0)
        else:
            session_events = 0
            session_cost = 0
        mtd_cost = int(mtd["cost"] if mtd else 0)
        mtd_included_cost = int(mtd_included["cost"] if mtd_included else 0)
        mtd_credits_cost = int(mtd_credits["cost"] if mtd_credits else 0)
        last_cost = int(last["cost"] if last else 0)
        today_cost = int(today["cost"] if today else 0)
        summary: dict[str, Any] = {
            "schema_version": 2,
            "display_enabled": bool(mtd_cost or last_cost),
            "basis": "API-equivalent / credit-rate estimate; not an actual billing ledger",
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "month_utc": month,
            "credit_cutover_utc": FABLE_CREDIT_CUTOVER_UTC,
            "mtd": {
                "events": int(mtd["events"] if mtd else 0),
                "cost_usd_nanos": mtd_cost,
                "cost_usd": _usd(mtd_cost),
            },
            "mtd_included": {
                "events": int(mtd_included["events"] if mtd_included else 0),
                "cost_usd_nanos": mtd_included_cost,
                "cost_usd": _usd(mtd_included_cost),
            },
            "mtd_credits": {
                "events": int(mtd_credits["events"] if mtd_credits else 0),
                "cost_usd_nanos": mtd_credits_cost,
                "cost_usd": _usd(mtd_credits_cost),
            },
            "today": {
                "events": int(today["events"] if today else 0),
                "cost_usd_nanos": today_cost,
                "cost_usd": _usd(today_cost),
            },
            "session": {
                "session_id": sid,
                "events": session_events,
                "cost_usd_nanos": session_cost,
                "cost_usd": _usd(session_cost),
            },
            "last_event": None,
            "last_task": None,
        }
        if last_event:
            summary["last_event"] = {
                "event_key": last_event["event_key"],
                "assistant_message_id": last_event["assistant_message_id"],
                "session_id": last_event["session_id"],
                "source_path": last_event["source_path"],
                "line_number": int(last_event["line_number"]),
                "ts_utc": last_event["ts_utc"],
                "model": last_event["model"],
                "canonical_model": last_event["canonical_model"],
                "tokens": {
                    "input": int(last_event["input_tokens"]),
                    "cache_creation_5m": int(last_event["cache_creation_5m_tokens"]),
                    "cache_creation_1h": int(last_event["cache_creation_1h_tokens"]),
                    "cache_read": int(last_event["cache_read_tokens"]),
                    "output": int(last_event["output_tokens"]),
                },
                "cost_usd_nanos": int(last_event["cost_usd_nanos"]),
                "cost_usd": _usd(int(last_event["cost_usd_nanos"])),
            }
        if last:
            summary["last_task"] = {
                "task_key": last["task_key"],
                "session_id": last["session_id"],
                "source_path": last["source_path"],
                "ts_utc": last["ts_utc"],
                "events": int(last["events"]),
                "cost_usd_nanos": last_cost,
                "cost_usd": _usd(last_cost),
            }
        return summary
    finally:
        conn.close()


def write_summary_cache(summary: dict[str, Any]) -> None:
    # Per-process-unique tmp name so concurrent writers (Stop hook + backgrounded
    # refresh) never clobber a shared tmp before os.replace promotes it.
    tmp = SUMMARY_CACHE_PATH.with_suffix(f".{os.getpid()}.json.tmp")
    try:
        SUMMARY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, SUMMARY_CACHE_PATH)
    except OSError:
        # Fail-open: keep the previous valid cache; drop any half-written tmp.
        try:
            tmp.unlink()
        except OSError:
            pass
        return


def brief_lines(summary: dict[str, Any]) -> list[str]:
    if not summary.get("display_enabled") or not summary.get("last_task"):
        return []
    last_cost = summary["last_task"]["cost_usd"]
    mtd_cost = summary["mtd"]["cost_usd"]
    # mtd_credits is the billable (post-cutover) slice; fall back to the total on
    # an older cache that predates the split so the line never KeyErrors.
    credits = summary.get("mtd_credits") or {}
    credits_cost = credits.get("cost_usd", "0.0000")
    month = summary.get("month_utc") or _current_month_utc()
    return [
        f"Fable last request/task estimate: ${last_cost} (API-equivalent credit rate)",
        f"Fable month-to-date billable credits ({month} UTC): ${credits_cost} "
        f"(estimate; excludes included window, not a billing ledger)",
    ]


def cmd_ingest(args: argparse.Namespace) -> int:
    transcript = Path(args.transcript).expanduser()
    paths = _iter_transcript_paths(transcript)
    events = parse_transcripts(paths, session_id=args.session_id or "", project_root=args.project_root)
    inserted = 0
    if not args.dry_run:
        inserted, _ = persist_events(events, create_db=True)
        summary = build_summary(create_db=True)
        write_summary_cache(summary)
    result = {
        "transcript_paths": [str(p) for p in paths],
        "events": len(events),
        "inserted": inserted,
        "dry_run": bool(args.dry_run),
        "cost_usd": _usd(sum(event.cost_usd_nanos for event in events)),
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Fable usage ingest: events={len(events)} inserted={inserted} cost=${result['cost_usd']}")
    return 0


def _scan_roots(root_args: list[str] | None) -> list[Path]:
    if root_args:
        return [Path(root).expanduser() for root in root_args]
    return [
        Path.home() / ".claude" / "projects",
        Path.home() / ".codex" / "sessions",
    ]


def _scan_month_events(args: argparse.Namespace) -> list[FableEvent]:
    roots = _scan_roots(args.root)
    paths: list[Path] = []
    for root in roots:
        paths.extend(_iter_project_jsonl(root))
    return [
        event
        for event in parse_transcripts(
            paths,
            session_id=args.session_id or "",
            project_root=args.project_root,
        )
        if event.month_utc == args.month
    ]


def cmd_scan_month(args: argparse.Namespace) -> int:
    roots = _scan_roots(args.root)
    events = _scan_month_events(args)
    inserted = 0
    if not args.dry_run:
        inserted, _ = persist_events(events, create_db=True)
        summary = build_summary(create_db=True)
        write_summary_cache(summary)
    result = {
        "roots": [str(root) for root in roots],
        "month_utc": args.month,
        "events": len(events),
        "inserted": inserted,
        "dry_run": bool(args.dry_run),
        "cost_usd": _usd(sum(event.cost_usd_nanos for event in events)),
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            "Fable usage scan-month: "
            f"month={args.month} events={len(events)} inserted={inserted} "
            f"cost=${result['cost_usd']}"
        )
    return 0


def cmd_refresh_display(args: argparse.Namespace) -> int:
    events = _scan_month_events(args)
    persist_events(events, create_db=True)
    summary = build_summary(create_db=True)
    write_summary_cache(summary)
    lines = brief_lines(summary)
    if lines:
        print("\n".join(lines))
    return 0


def _acquire_refresh_lock() -> int | None:
    """Non-blocking exclusive lock via fcntl.flock (portable: macOS + Linux).

    Returns an open fd on success, or None if another refresh holds the lock (or
    the lock file cannot be opened). fcntl.flock auto-releases on process death,
    so there is no stale-lock hazard.
    """
    try:
        REFRESH_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(REFRESH_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return fd


def _release_refresh_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def cmd_refresh_session(args: argparse.Namespace) -> int:
    # Stampede guard: if another refresh is running, exit 0 immediately.
    fd = _acquire_refresh_lock()
    if fd is None:
        return 0
    try:
        session_id = args.session or ""
        transcript = Path(args.transcript).expanduser()
        paths = _iter_transcript_paths(transcript)
        events = parse_transcripts(paths, session_id=session_id, project_root=args.project_root)
        # create_db=True bootstraps schema on a fresh install so events are not
        # silently dropped into a $0 cache.
        persist_events(events, create_db=True)
        # If build_summary raises, we skip write_summary_cache entirely and keep
        # the prior valid cache (never write a half-populated file).
        summary = build_summary(create_db=True, session_id=session_id)
        write_summary_cache(summary)
    except Exception:
        return 0
    finally:
        _release_refresh_lock(fd)
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    summary = build_summary(create_db=False, session_id=(getattr(args, "session", "") or None))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    elif args.brief:
        lines = brief_lines(summary)
        if lines:
            print("\n".join(lines))
    else:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_display(_: argparse.Namespace) -> int:
    try:
        if SUMMARY_CACHE_PATH.exists():
            summary = json.loads(SUMMARY_CACHE_PATH.read_text(encoding="utf-8"))
        else:
            summary = build_summary(create_db=False)
    except Exception:
        return 0
    lines = brief_lines(summary)
    if lines:
        print("\n".join(lines))
    return 0


def hook_main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        event = json.loads(raw)
        if not isinstance(event, dict):
            return 0
        transcript_path = event.get("transcript_path")
        if not transcript_path:
            return 0
        session_id = str(event.get("session_id") or "")
        cwd = str(event.get("cwd") or "") or None
        transcript = Path(str(transcript_path)).expanduser()
        paths = _iter_transcript_paths(transcript)
        events = parse_transcripts(paths, session_id=session_id, project_root=cwd)
        db_exists = DB_PATH.exists()
        persist_events(events, create_db=db_exists)
        summary = build_summary(create_db=db_exists, session_id=session_id)
        if summary.get("display_enabled"):
            write_summary_cache(summary)
        return 0
    except Exception:
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fable 5 usage estimate ledger")
    sub = parser.add_subparsers(dest="cmd")

    ingest = sub.add_parser("ingest", help="ingest a transcript and sibling subagent JSONL files")
    ingest.add_argument("--transcript", required=True)
    ingest.add_argument("--session-id", default="")
    ingest.add_argument("--project-root")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--json", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    scan = sub.add_parser("scan-month", help="scan Claude project JSONL files for a UTC month")
    scan.add_argument("--month", default=_current_month_utc())
    scan.add_argument("--root", action="append", default=[])
    scan.add_argument("--session-id", default="")
    scan.add_argument("--project-root")
    scan.add_argument("--dry-run", action="store_true")
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan_month)

    refresh = sub.add_parser("refresh-display", help="scan current month, update cache, then print display lines")
    refresh.add_argument("--month", default=_current_month_utc())
    refresh.add_argument("--root", action="append", default=[])
    refresh.add_argument("--session-id", default="")
    refresh.add_argument("--project-root")
    refresh.set_defaults(func=cmd_refresh_display)

    refresh_session = sub.add_parser(
        "refresh-session",
        help="re-ingest one session transcript (locked, idempotent) then rewrite cache",
    )
    refresh_session.add_argument("--session", default="")
    refresh_session.add_argument("--transcript", required=True)
    refresh_session.add_argument("--project-root")
    refresh_session.set_defaults(func=cmd_refresh_session)

    summary = sub.add_parser("summary", help="print recomputed ledger summary")
    summary.add_argument("--session", default="")
    group = summary.add_mutually_exclusive_group()
    group.add_argument("--json", action="store_true")
    group.add_argument("--brief", action="store_true")
    summary.set_defaults(func=cmd_summary)

    display = sub.add_parser("display", help="print two human-facing lines when data exists")
    display.set_defaults(func=cmd_display)
    return parser


def main() -> int:
    if len(sys.argv) == 1:
        return hook_main()
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        return hook_main()
    try:
        return int(args.func(args) or 0)
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
