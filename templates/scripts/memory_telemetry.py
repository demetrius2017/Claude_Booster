#!/usr/bin/env python3
"""Memory injection telemetry for Rolling Memory.

Purpose:
    Append best-effort JSONL records when Rolling Memory context is rendered,
    and report which active memories were not surfaced during a recent window.

Contract:
    Input: emit_injection(...) calls from rolling_memory.py, or CLI
    ``memory_telemetry.py report [--window N] [--json]``.
    Output: append-only JSONL telemetry, or a summary/report on stdout.

CLI/Examples:
    python3 memory_telemetry.py report --window 30
    python3 memory_telemetry.py report --window 7 --json

Limitations:
    Telemetry is deliberately fail-open: emission errors are swallowed so memory
    rendering cannot be broken by logging. The report is read-only and exits 0
    even when the DB or log is absent.

ENV/Files:
    ~/.claude/logs/memory_injection.jsonl — append-only injection log
    ~/.claude/rolling_memory.db — read-only report source
    CLAUDE_BOOSTER_SKIP_MEMORY_TELEMETRY=1 disables emission.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / ".claude" / "rolling_memory.db"
LOG_PATH = Path.home() / ".claude" / "logs" / "memory_injection.jsonl"
MAX_RECORD_BYTES = 8192


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def emit_injection(
    *,
    log_path: str | os.PathLike[str] = LOG_PATH,
    project_root: str | None,
    source: str,
    memory_ids: list[int],
    memory_types: dict[str, int],
    char_count: int,
    token_estimate: int,
    session_id: str | None = None,
) -> None:
    """Append one bounded JSONL row with one non-blocking atomic write."""
    if os.environ.get("CLAUDE_BOOSTER_SKIP_MEMORY_TELEMETRY") == "1":
        return

    try:
        clean_ids = [int(memory_id) for memory_id in memory_ids]
        clean_types = {str(k): int(v) for k, v in memory_types.items()}
        if any(memory_id < 0 for memory_id in clean_ids):
            raise ValueError("memory_ids must be non-negative")
        if any(count < 0 for count in clean_types.values()):
            raise ValueError("memory type counts must be non-negative")
        if int(char_count) < 0 or int(token_estimate) < 0:
            raise ValueError("character and token counts must be non-negative")
        row_count = len(clean_ids)
        record = {
            "ts_utc": _format_ts(_utc_now()),
            "session_id": None if session_id is None else str(session_id),
            "project_root": project_root,
            "source": source,
            "memory_ids": clean_ids,
            "memory_types": clean_types,
            "row_count": row_count,
            "char_count": int(char_count),
            "token_estimate": int(token_estimate),
        }
        line = (json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")
        if len(line) > MAX_RECORD_BYTES:
            raise ValueError(f"telemetry record exceeds {MAX_RECORD_BYTES} bytes")
        path = Path(log_path)
        os.makedirs(path.parent, exist_ok=True)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            written = os.write(fd, line)
            if written != len(line):
                raise OSError(errno.EIO, "short telemetry write")
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        return


def _read_log_rows(log_path: Path, lower_bound: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(record.get("ts_utc"))
                if ts is None or ts < lower_bound:
                    continue
                if isinstance(record, dict):
                    rows.append(record)
    except FileNotFoundError:
        return []
    except OSError:
        return []
    return rows


def _active_memory_ids(db_path: Path) -> tuple[list[int], str | None]:
    if not db_path.exists():
        return [], f"database not found: {db_path}"

    uri = f"file:{db_path}?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        cur = conn.execute("SELECT id FROM agent_memory WHERE active = 1 ORDER BY id ASC")
        return [int(row[0]) for row in cur.fetchall()], None
    except Exception as exc:
        return [], f"database unavailable: {type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()


def build_report(window_days: int, log_path: Path = LOG_PATH, db_path: Path = DB_PATH) -> dict[str, Any]:
    lower_bound = _utc_now() - timedelta(days=window_days)
    rows = _read_log_rows(log_path, lower_bound)

    sessions: dict[str, int] = {}
    by_type: dict[str, int] = {}
    total_token_estimate = 0
    injected_ids: set[int] = set()

    for record in rows:
        row_count = int(record.get("row_count") or 0)
        session_key = "null" if record.get("session_id") is None else str(record.get("session_id"))
        sessions[session_key] = sessions.get(session_key, 0) + row_count

        memory_types = record.get("memory_types")
        if isinstance(memory_types, dict):
            for memory_type, count in memory_types.items():
                try:
                    by_type[str(memory_type)] = by_type.get(str(memory_type), 0) + int(count)
                except (TypeError, ValueError):
                    continue

        try:
            total_token_estimate += int(record.get("token_estimate") or 0)
        except (TypeError, ValueError):
            pass

        memory_ids = record.get("memory_ids")
        if isinstance(memory_ids, list):
            for memory_id in memory_ids:
                try:
                    injected_ids.add(int(memory_id))
                except (TypeError, ValueError):
                    continue

    active_ids, error = _active_memory_ids(db_path)
    report: dict[str, Any] = {
        "sessions": sessions,
        "by_type": by_type,
        "token_estimate": total_token_estimate,
        "never_injected_ids": [memory_id for memory_id in active_ids if memory_id not in injected_ids],
    }
    if error is not None:
        report["error"] = error
    return report


def _print_human(report: dict[str, Any], window_days: int) -> None:
    print(f"Memory injection telemetry ({window_days}d window)")

    print("\nSessions:")
    sessions = report.get("sessions") or {}
    if sessions:
        for session_id, count in sorted(sessions.items()):
            print(f"  {session_id}: {count}")
    else:
        print("  (none)")

    print("\nBy type:")
    by_type = report.get("by_type") or {}
    token_estimate = report.get("token_estimate", 0)
    type_items = list(by_type.items())
    if type_items:
        for memory_type, count in sorted(type_items):
            print(f"  {memory_type}: {count}")
    else:
        print("  (none)")
    print(f"  token_estimate: {token_estimate}")

    never_injected_ids = report.get("never_injected_ids") or []
    print("\nNever injected active IDs:")
    if never_injected_ids:
        print("  " + ", ".join(str(memory_id) for memory_id in never_injected_ids))
    else:
        print("  (none)")

    if report.get("error"):
        print(f"\nError: {report['error']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rolling Memory injection telemetry")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    report_parser = subparsers.add_parser("report", help="summarize memory injection telemetry")
    report_parser.add_argument("--window", type=int, default=30, help="window in days (default: 30)")
    report_parser.add_argument("--json", action="store_true", help="emit JSON")
    report_parser.add_argument("--log-path", type=Path, default=LOG_PATH, help=argparse.SUPPRESS)
    report_parser.add_argument("--db-path", type=Path, default=DB_PATH, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.cmd == "report":
        window = max(0, args.window)
        report = build_report(window_days=window, log_path=args.log_path, db_path=args.db_path)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        else:
            _print_human(report, window)
        return 0

    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
