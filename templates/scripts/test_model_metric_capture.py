#!/usr/bin/env python3
"""Acceptance test for templates/scripts/model_metric_capture.py (PostToolUse hook).

Purpose:
    Independent, executable acceptance test for the model-metric capture hook.
    Verifies OBSERVABLE behavior only — pipe a JSON event to the hook on stdin
    with a CLAUDE_BOOSTER_METRICS_DB override, then read the temp DB and assert
    on the inserted row. The Worker's implementation is NOT consulted.

Contract under test (binding):
    - Codex (Bash) calls: command may carry a LEADING shell env-assignment
      prefix `CLAUDE_BOOSTER_TASK_CATEGORY=<cat> <codex command...>`. The hook
      must strip leading VAR=val assignments to a BARE command, match the codex
      model on the bare command (so prefix does NOT suppress matching), and
      parse the category from the stripped leading env span.
    - Known categories: trivial, recon, medium, coding, hard, consilium_bio,
      audit_external, lead, high_blast_radius. Unknown/absent/quoted -> "medium".
    - duration: from event top-level `duration_ms`; num_turns=1 (codex);
      per_turn_ms=duration; 0 -> 0 (NOT NULL); absent -> NULL (no crash);
      a bool must NOT be treated as a duration.
    - CLAUDE_BOOSTER_METRICS_DB env -> DB target for BOTH the codex AND the
      Task/Agent path (shared resolver), provider="anthropic" for Task/Agent.

CLI / Examples:
    python3 templates/scripts/test_model_metric_capture.py
    Exit 0 iff all cases pass; prints [PASS]/[FAIL] per labeled case and a
    final "Results: N passed, M failed".

Limitations:
    - stdlib only (json, sqlite3, subprocess, tempfile, os, shutil, pathlib).
    - Asserts ~/.claude/rolling_memory.db is untouched (mtime + existence
      captured pre/post). Never writes to the real DB.

ENV / Files:
    - Writes only into per-case temp dirs under the system temp root.
    - Reads HOOK at: <repo>/templates/scripts/model_metric_capture.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "templates" / "scripts" / "model_metric_capture.py"
REAL_DB = Path.home() / ".claude" / "rolling_memory.db"

CREATE_TABLE = """
CREATE TABLE model_metrics (
    ts_utc TEXT,
    provider TEXT,
    model TEXT,
    task_category TEXT,
    duration_ms INTEGER,
    num_turns INTEGER,
    per_turn_ms INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    success INTEGER,
    session_id TEXT,
    project_root TEXT
);
"""

_results = []  # (label, ok, detail)


def _record(label, ok, detail=""):
    _results.append((label, ok, detail))
    tag = "PASS" if ok else "FAIL"
    line = f"[{tag}] {label}"
    if detail:
        line += f" -- {detail}"
    print(line)


def _run_hook(event, db_path):
    """Run the hook with stdin=event (str or dict) and the DB override.

    Returns (returncode, rows) where rows is the list of model_metrics rows
    as dicts (column name -> value).
    """
    if isinstance(event, (dict, list)):
        stdin_data = json.dumps(event)
    else:
        stdin_data = event  # raw string, possibly malformed
    env = {**os.environ, "CLAUDE_BOOSTER_METRICS_DB": str(db_path)}
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    rows = _read_rows(db_path)
    return proc, rows


def _read_rows(db_path):
    if not Path(db_path).exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute("SELECT * FROM model_metrics")
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def _fresh_db():
    """Create a fresh temp dir + sqlite with the model_metrics table.

    Returns (tmpdir_obj, db_path). Keep tmpdir_obj alive until done.
    """
    tmp = tempfile.TemporaryDirectory(prefix="mmc_test_")
    db = Path(tmp.name) / "m.db"
    con = sqlite3.connect(str(db))
    try:
        con.executescript(CREATE_TABLE)
        con.commit()
    finally:
        con.close()
    return tmp, db


def _real_db_state():
    if REAL_DB.exists():
        st = REAL_DB.stat()
        return (True, st.st_mtime_ns, st.st_size)
    return (False, None, None)


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------

def case_1_prefix_still_matches():
    label = "1 PREFIX-STILL-MATCHES codex prefix + category=coding"
    tmp, db = _fresh_db()
    try:
        cmd = ("CLAUDE_BOOSTER_TASK_CATEGORY=coding "
               "/Users/x/.claude/scripts/codex_worker.sh gpt-5.5 -")
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "duration_ms": 1234,
            "session_id": "s",
        }
        proc, rows = _run_hook(event, db)
        if len(rows) != 1:
            return _record(label, False,
                           f"expected exactly 1 row, got {len(rows)} "
                           f"(rc={proc.returncode}, stderr={proc.stderr!r})")
        r = rows[0]
        checks = {
            "provider": ("codex-cli", r["provider"]),
            "model": ("gpt-5.5", r["model"]),
            "task_category": ("coding", r["task_category"]),
            "duration_ms": (1234, r["duration_ms"]),
            "num_turns": (1, r["num_turns"]),
            "per_turn_ms": (1234, r["per_turn_ms"]),
        }
        bad = {k: v for k, v in checks.items() if v[0] != v[1]}
        if bad:
            return _record(label, False, f"mismatches {bad}")
        _record(label, True)
    finally:
        tmp.cleanup()


def case_2_no_prefix_codex():
    label = "2 no-prefix codex -> category medium"
    tmp, db = _fresh_db()
    try:
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "~/.claude/scripts/codex_worker.sh gpt-5.5"},
            "duration_ms": 500,
            "session_id": "s",
        }
        proc, rows = _run_hook(event, db)
        if len(rows) != 1:
            return _record(label, False,
                           f"expected 1 row, got {len(rows)} "
                           f"(stderr={proc.stderr!r})")
        r = rows[0]
        exp = {"model": "gpt-5.5", "task_category": "medium",
               "duration_ms": 500, "per_turn_ms": 500}
        bad = {k: (v, r[k]) for k, v in exp.items() if r[k] != v}
        if bad:
            return _record(label, False, f"mismatches {bad}")
        _record(label, True)
    finally:
        tmp.cleanup()


def case_3_bogus_category():
    label = "3 bogus category -> medium"
    tmp, db = _fresh_db()
    try:
        event = {
            "tool_name": "Bash",
            "tool_input": {"command":
                           "CLAUDE_BOOSTER_TASK_CATEGORY=bogus codex_worker.sh gpt-5.5"},
            "duration_ms": 10,
            "session_id": "s",
        }
        proc, rows = _run_hook(event, db)
        if len(rows) != 1:
            return _record(label, False,
                           f"expected 1 row, got {len(rows)} "
                           f"(stderr={proc.stderr!r})")
        got = rows[0]["task_category"]
        if got != "medium":
            return _record(label, False, f"expected medium, got {got!r}")
        _record(label, True)
    finally:
        tmp.cleanup()


def case_4_duration_absent_null():
    label = "4 duration absent -> NULL"
    tmp, db = _fresh_db()
    try:
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "~/.claude/scripts/codex_worker.sh gpt-5.5"},
            "session_id": "s",
        }
        proc, rows = _run_hook(event, db)
        if proc.returncode != 0:
            return _record(label, False,
                           f"expected exit 0, got {proc.returncode} "
                           f"(stderr={proc.stderr!r})")
        if len(rows) != 1:
            return _record(label, False, f"expected 1 row, got {len(rows)}")
        r = rows[0]
        if r["duration_ms"] is not None or r["per_turn_ms"] is not None:
            return _record(label, False,
                           f"expected NULL duration/per_turn, got "
                           f"{r['duration_ms']!r}/{r['per_turn_ms']!r}")
        _record(label, True)
    finally:
        tmp.cleanup()


def case_5_duration_zero():
    label = "5 duration 0 -> 0 (not NULL)"
    tmp, db = _fresh_db()
    try:
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "~/.claude/scripts/codex_worker.sh gpt-5.5"},
            "duration_ms": 0,
            "session_id": "s",
        }
        proc, rows = _run_hook(event, db)
        if len(rows) != 1:
            return _record(label, False, f"expected 1 row, got {len(rows)}")
        r = rows[0]
        if r["duration_ms"] != 0:
            return _record(label, False,
                           f"expected duration_ms==0, got {r['duration_ms']!r}")
        _record(label, True)
    finally:
        tmp.cleanup()


def case_6_false_match_negative():
    label = "6 false-match (grep ... no codex token) -> 0 rows"
    tmp, db = _fresh_db()
    try:
        event = {
            "tool_name": "Bash",
            "tool_input": {"command":
                           "grep CLAUDE_BOOSTER_TASK_CATEGORY=hard logs/"},
            "duration_ms": 99,
            "session_id": "s",
        }
        proc, rows = _run_hook(event, db)
        if len(rows) != 0:
            return _record(label, False, f"expected 0 rows, got {len(rows)}: {rows}")
        _record(label, True)
    finally:
        tmp.cleanup()


def case_7_non_codex_bash():
    label = "7 non-codex Bash (ls -la) -> 0 rows"
    tmp, db = _fresh_db()
    try:
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "duration_ms": 5,
            "session_id": "s",
        }
        proc, rows = _run_hook(event, db)
        if len(rows) != 0:
            return _record(label, False, f"expected 0 rows, got {len(rows)}: {rows}")
        _record(label, True)
    finally:
        tmp.cleanup()


def case_8_shared_db_task_path():
    label = "8 shared DB override on Task/Agent path -> row in temp db"
    tmp, db = _fresh_db()
    try:
        # Try a Task/Agent event with both usage and top-level duration/num_turns
        # so the implementation can extract whichever it uses.
        event = {
            "tool_name": "Task",
            "tool_input": {"description": "do work"},
            "tool_response": {"usage": {"input_tokens": 100, "output_tokens": 50}},
            "duration_ms": 2000,
            "num_turns": 4,
            "session_id": "s",
        }
        proc, rows = _run_hook(event, db)
        if len(rows) != 1:
            return _record(label, False,
                           f"expected exactly 1 row in TEMP db, got {len(rows)} "
                           f"(rc={proc.returncode}, stderr={proc.stderr!r})")
        r = rows[0]
        if r["provider"] != "anthropic":
            return _record(label, False,
                           f"expected provider=anthropic, got {r['provider']!r}")
        _record(label, True)
    finally:
        tmp.cleanup()


def case_9_other_codex_form_prefix():
    label = "9 codex exec form + prefix category=hard"
    tmp, db = _fresh_db()
    try:
        event = {
            "tool_name": "Bash",
            "tool_input": {"command":
                           "CLAUDE_BOOSTER_TASK_CATEGORY=hard codex exec -m gpt-5.5 -"},
            "duration_ms": 42,
            "session_id": "s",
        }
        proc, rows = _run_hook(event, db)
        if len(rows) != 1:
            return _record(label, False,
                           f"expected 1 row, got {len(rows)} "
                           f"(stderr={proc.stderr!r})")
        r = rows[0]
        exp = {"model": "gpt-5.5", "task_category": "hard"}
        bad = {k: (v, r[k]) for k, v in exp.items() if r[k] != v}
        if bad:
            return _record(label, False, f"mismatches {bad}")
        _record(label, True)
    finally:
        tmp.cleanup()


def case_10_fail_soft_malformed():
    label = "10 fail-soft: malformed stdin -> exit 0, no row"
    tmp, db = _fresh_db()
    try:
        proc, rows = _run_hook("this is not json {{{", db)
        if proc.returncode != 0:
            return _record(label, False,
                           f"expected exit 0, got {proc.returncode} "
                           f"(stderr={proc.stderr!r})")
        if len(rows) != 0:
            return _record(label, False, f"expected 0 rows, got {len(rows)}: {rows}")
        _record(label, True)
    finally:
        tmp.cleanup()


def main():
    if not HOOK.exists():
        print(f"[FAIL] HOOK not found at {HOOK}")
        print("Results: 0 passed, 1 failed")
        sys.exit(1)

    pre = _real_db_state()

    cases = [
        case_1_prefix_still_matches,
        case_2_no_prefix_codex,
        case_3_bogus_category,
        case_4_duration_absent_null,
        case_5_duration_zero,
        case_6_false_match_negative,
        case_7_non_codex_bash,
        case_8_shared_db_task_path,
        case_9_other_codex_form_prefix,
        case_10_fail_soft_malformed,
    ]
    for c in cases:
        try:
            c()
        except Exception as e:  # noqa: BLE001 - a case crash is a test failure
            _record(c.__name__, False, f"unexpected exception: {e!r}")

    # Guard: the real rolling_memory.db must be untouched.
    post = _real_db_state()
    real_ok = pre == post
    _record("REAL-DB-UNTOUCHED ~/.claude/rolling_memory.db unchanged", real_ok,
            "" if real_ok else f"pre={pre} post={post}")

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
