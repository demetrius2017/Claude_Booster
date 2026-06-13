#!/usr/bin/env python3
"""Record and report /go pipeline rework KPIs.

Purpose:
    Capture each /go pipeline outcome as a JSONL record and aggregate those
    records later. The metric is aimed at proving whether SHIP-1..4 pipeline
    changes reduce returns-to-code by defect category.

Contract:
    Input is a CLI invocation. ``record`` validates all business fields before
    opening the log and appends exactly one JSON line on success. ``report``
    reads the log line by line, skips malformed rows, filters by inclusive UTC
    window and optional project, and prints either prose or a JSON envelope.

CLI/Examples:
    kpi_rework.py record --task "SHIP-1 verifier gate" --outcome pass \
        --worker-spawns 2 --verifier-fails 0 \
        --category weak_verification:1
    kpi_rework.py report --window 14
    kpi_rework.py report --project /path/to/repo --json
    kpi_rework.py --log-path /tmp/kpi.jsonl report --json

Limitations:
    - Stdlib only; no file locking is attempted.
    - Malformed historical log rows are ignored during reporting.
    - Project identity is the basename of the resolved project root, or
      ``unknown`` when no root can be resolved.

ENV/Files:
    - Reads  : env $CLAUDE_BOOSTER_LOGS_DIR for the default log directory
               override.
    - Writes : --log-path, or $CLAUDE_BOOSTER_LOGS_DIR/kpi_rework.jsonl, or
               ~/.claude/logs/kpi_rework.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


try:
    from _gate_common import iso_now, project_root_from
except ImportError:
    import pathlib as _pl

    sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    from _gate_common import iso_now, project_root_from  # type: ignore[no-redef]


LOG_NAME = "kpi_rework.jsonl"
OUTCOME_PASS = "pass"
OUTCOME_FAIL_EXHAUSTED = "fail_exhausted"
ALLOWED_OUTCOMES = (OUTCOME_PASS, OUTCOME_FAIL_EXHAUSTED)
ALLOWED_CATEGORIES = (
    "contract_ambiguity",
    "missed_failure_mode",
    "integration_mismatch",
    "weak_verification",
    "capability",
)
REQUIRED_RECORD_FIELDS = (
    "ts",
    "project",
    "task",
    "outcome",
    "worker_spawn_count",
    "verifier_fail_count",
    "first_pass_clean",
    "defect_categories",
)


def non_negative_int(value: str) -> int:
    """Return a non-negative integer or raise argparse.ArgumentTypeError."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be >= 0")
    return parsed


def positive_int(value: str) -> int:
    """Return a positive integer or raise argparse.ArgumentTypeError."""
    parsed = non_negative_int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be > 0")
    return parsed


def resolve_log_path(log_path: str | None) -> Path:
    """Resolve the effective KPI log file path with script-owned precedence."""
    if log_path:
        return Path(log_path)
    logs_dir = os.environ.get("CLAUDE_BOOSTER_LOGS_DIR")
    if logs_dir:
        return Path(logs_dir) / LOG_NAME
    return Path.home() / ".claude" / "logs" / LOG_NAME


def resolve_project_name(cwd_hint: str | None) -> str:
    """Return the basename of the resolved project root, or ``unknown``."""
    root = project_root_from(cwd_hint)
    if root is None:
        return "unknown"
    name = root.name
    return name if name else "unknown"


def parse_category_token(token: str) -> dict[str, int | str]:
    """Parse one ``<category>:<count>`` token and enforce category contract."""
    if token.count(":") != 1:
        raise ValueError(f"invalid --category {token!r}: expected <name>:<int>")
    name, count_raw = token.split(":", 1)
    if name not in ALLOWED_CATEGORIES:
        raise ValueError(
            f"invalid --category {token!r}: unknown category {name!r}"
        )
    try:
        count = int(count_raw)
    except ValueError as exc:
        raise ValueError(
            f"invalid --category {token!r}: count must be an integer"
        ) from exc
    if count < 0:
        raise ValueError(f"invalid --category {token!r}: count must be >= 0")
    return {"category": name, "count": count}


def validate_task(task: str) -> str:
    """Return the original task string if non-empty after stripping."""
    if not task or not task.strip():
        raise ValueError("--task must not be empty or whitespace")
    return task


def append_record(path: Path, record: dict[str, object]) -> None:
    """Append one JSON line after ensuring the parent directory exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)


def handle_record(args: argparse.Namespace) -> int:
    """Validate, build, and append one KPI record."""
    try:
        task = validate_task(args.task)
        categories = [parse_category_token(token) for token in args.category]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    record: dict[str, object] = {
        "ts": iso_now(),
        "project": resolve_project_name(None),
        "task": task,
        "outcome": args.outcome,
        "worker_spawn_count": args.worker_spawns,
        "verifier_fail_count": args.verifier_fails,
        "first_pass_clean": (
            args.outcome == OUTCOME_PASS and args.verifier_fails == 0
        ),
        "defect_categories": categories,
    }

    try:
        append_record(resolve_log_path(args.log_path), record)
    except OSError as exc:
        print(f"failed to append KPI rework log: {exc}", file=sys.stderr)
        return 1
    return 0


def parse_row_ts(value: object) -> datetime | None:
    """Parse a row timestamp in the required ``...Z`` UTC format."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def row_has_required_fields(row: object) -> bool:
    """Return True when a decoded JSON row has the required record keys."""
    return isinstance(row, dict) and all(key in row for key in REQUIRED_RECORD_FIELDS)


def category_totals_zeroed() -> dict[str, int]:
    """Return canonical zero-filled category totals."""
    return {category: 0 for category in ALLOWED_CATEGORIES}


def iter_valid_rows(path: Path):
    """Yield decoded rows that have all required top-level fields."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row_has_required_fields(row):
                    yield row
    except FileNotFoundError:
        return


def envelope_from_rows(rows: list[dict[str, object]]) -> dict[str, object]:
    """Aggregate included rows into the deterministic report envelope."""
    totals = category_totals_zeroed()
    task_count = len(rows)
    if task_count == 0:
        return {
            "task_count": 0,
            "first_pass_clean_rate_percent": None,
            "mean_verifier_fail_count": None,
            "per_category_totals": totals,
        }

    clean_count = 0
    verifier_fail_sum = 0
    for row in rows:
        if row.get("first_pass_clean") is True:
            clean_count += 1
        verifier_fail_count = row.get("verifier_fail_count")
        if isinstance(verifier_fail_count, int):
            verifier_fail_sum += verifier_fail_count

        defect_categories = row.get("defect_categories")
        if not isinstance(defect_categories, list):
            continue
        for entry in defect_categories:
            if not isinstance(entry, dict):
                continue
            category = entry.get("category")
            count = entry.get("count")
            if category in totals and isinstance(count, int) and count >= 0:
                totals[category] += count

    return {
        "task_count": task_count,
        "first_pass_clean_rate_percent": 100.0 * clean_count / task_count,
        "mean_verifier_fail_count": verifier_fail_sum / task_count,
        "per_category_totals": totals,
    }


def included_rows(
    path: Path,
    cutoff: datetime,
    project_filter: str | None,
) -> list[dict[str, object]]:
    """Return log rows included by timestamp and optional project filter."""
    rows: list[dict[str, object]] = []
    for row in iter_valid_rows(path):
        row_ts = parse_row_ts(row.get("ts"))
        if row_ts is None or row_ts < cutoff:
            continue
        if project_filter is not None and row.get("project") != project_filter:
            continue
        rows.append(row)
    return rows


def print_prose_report(envelope: dict[str, object], window: int) -> None:
    """Print a human-readable report summary."""
    task_count = envelope["task_count"]
    if task_count == 0:
        print(f"no data for the last {window} day(s)")
        return

    print(f"task_count: {task_count}")
    print(
        "first_pass_clean_rate_percent: "
        f"{envelope['first_pass_clean_rate_percent']:.2f}"
    )
    print(
        "mean_verifier_fail_count: "
        f"{envelope['mean_verifier_fail_count']:.2f}"
    )
    print("per_category_totals:")
    totals = envelope["per_category_totals"]
    if isinstance(totals, dict):
        for category in ALLOWED_CATEGORIES:
            print(f"  {category}: {totals.get(category, 0)}")


def handle_report(args: argparse.Namespace) -> int:
    """Aggregate and print KPI records."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.window)
    project_filter = resolve_project_name(args.project) if args.project else None
    rows = included_rows(resolve_log_path(args.log_path), cutoff, project_filter)
    envelope = envelope_from_rows(rows)

    if args.json:
        print(json.dumps(envelope, ensure_ascii=False))
    else:
        print_prose_report(envelope, args.window)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        description="Record and report /go pipeline rework KPIs."
    )
    add_log_path_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="append one pipeline outcome")
    add_log_path_argument(record, default=argparse.SUPPRESS)
    record.add_argument("--task", required=True)
    record.add_argument("--outcome", required=True, choices=ALLOWED_OUTCOMES)
    record.add_argument("--worker-spawns", required=True, type=non_negative_int)
    record.add_argument("--verifier-fails", required=True, type=non_negative_int)
    record.add_argument("--category", action="append", default=[])
    record.set_defaults(func=handle_record)

    report = subparsers.add_parser("report", help="aggregate pipeline outcomes")
    add_log_path_argument(report, default=argparse.SUPPRESS)
    report.add_argument("--window", type=positive_int, default=30)
    report.add_argument("--project")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=handle_report)

    return parser


def add_log_path_argument(
    parser: argparse.ArgumentParser,
    default: object = None,
) -> None:
    """Add the shared log-path option to the top parser or a subcommand."""
    parser.add_argument(
        "--log-path",
        default=default,
        help="Full JSONL log file path. Overrides CLAUDE_BOOSTER_LOGS_DIR.",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
