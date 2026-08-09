#!/usr/bin/env python3
"""Acceptance test for zai_cli.py provider-failure resilience.

OFFLINE, deterministic Verifier test. Written from the Artifact Contract only —
the Worker's implementation was NOT seen. Tests observable behavior:

- EMPTY stdout + exit 0  -> wrapper exits NON-ZERO without retry and records
  telemetry success=0.
- Non-empty success      -> stdout re-emitted BYTE-IDENTICAL, exit 0, success=1,
  no retry.
- Genuine non-zero child  -> NOT retried; child exit code preserved; success=0.
- Timeout                -> exit 124, success=0, typed failure event recorded.
- Permanent backend/account failures emit a sanitized typed failure event.
- stderr stays visible; byte fidelity (CRLF, UTF-8, 2MB) preserved.
- Missing credential still exits 64.

Mechanism: a fake ``claude`` executable is placed on PATH; it emits bytes per
STUB_SCENARIO and bumps a counter file so attempt counts are assertable. An
isolated sqlite telemetry DB (via CLAUDE_BOOSTER_METRICS_DB) captures the last
recorded success flag.

Run:  python3 tests/test_zai_resilience.py
Exit: 0 iff all cases pass.
"""
from __future__ import annotations

import os
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZAI_CLI = ROOT / "templates" / "scripts" / "zai_cli.py"
INTEGRATION_TEST = ROOT / "tests" / "test_zai_integration.py"

PASS_COUNT = 0
FAIL_COUNT = 0


def _report(ok: bool, label: str, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    if ok:
        PASS_COUNT += 1
        print(f"[PASS] {label}")
    else:
        FAIL_COUNT += 1
        print(f"[FAIL] {label}" + (f" :: {detail}" if detail else ""))


# Fake `claude` executable. Reads STUB_SCENARIO, increments STUB_COUNTER_FILE,
# writes exact bytes to stdout (binary, no implicit newline) and exits per case.
STUB_SOURCE = r'''#!/usr/bin/env python3
import os, sys

counter_file = os.environ["STUB_COUNTER_FILE"]
try:
    with open(counter_file, "r") as fh:
        n = int(fh.read().strip() or "0")
except (FileNotFoundError, ValueError):
    n = 0
n += 1
with open(counter_file, "w") as fh:
    fh.write(str(n))

scenario = os.environ.get("STUB_SCENARIO", "always_empty")
out = sys.stdout.buffer
err = sys.stderr.buffer

if scenario == "always_empty":
    sys.exit(0)
elif scenario == "always_nonempty":
    out.write(b"GLM_OK_PAYLOAD_NO_NEWLINE")
    out.flush()
    sys.exit(0)
elif scenario == "empty_then_nonempty":
    if n == 1:
        sys.exit(0)
    out.write(b"SECOND_CALL_PAYLOAD")
    out.flush()
    sys.exit(0)
elif scenario == "immediate_nonzero":
    sys.exit(23)
elif scenario == "partial_nonzero":
    out.write(b"PARTIAL_BEFORE_CRASH")
    out.flush()
    sys.exit(23)
elif scenario == "crlf":
    out.write(b"line1\r\nline2\r\n")
    out.flush()
    sys.exit(0)
elif scenario == "nonascii":
    out.write("café — 日本語".encode("utf-8"))
    out.flush()
    sys.exit(0)
elif scenario == "stderr_sentinel":
    out.write(b"STDOUT_OK")
    out.flush()
    err.write(b"SENTINEL_STDERR")
    err.flush()
    sys.exit(0)
elif scenario == "invalid_model":
    err.write(b"API Error 400 invalid model: glm-5.2[1m] secret-token")
    err.flush()
    sys.exit(1)
elif scenario == "insufficient_balance":
    err.write(b"API Error 429 insufficient balance: credit_balance_exhausted secret-token")
    err.flush()
    sys.exit(1)
elif scenario == "sleep":
    import time
    time.sleep(5)
    out.write(b"LATE_PAYLOAD")
    out.flush()
    sys.exit(0)
elif scenario == "large":
    out.write(b"A" * 2000000)
    out.flush()
    sys.exit(0)
else:
    sys.stderr.write("unknown scenario: %s\n" % scenario)
    sys.exit(99)
'''


def _make_stub_dir(base: Path) -> Path:
    bindir = base / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "claude"
    stub.write_text(STUB_SOURCE, encoding="utf-8")
    stub.chmod(0o755)
    return bindir


def _make_metrics_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE model_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                task_category TEXT,
                duration_ms INTEGER,
                num_turns INTEGER,
                per_turn_ms INTEGER,
                tokens_in INTEGER,
                tokens_out INTEGER,
                success INTEGER NOT NULL DEFAULT 1,
                session_id TEXT,
                project_root TEXT
            )
            """
        )


def _last_success(db_path: Path):
    """Return last recorded success flag (int) or None if no rows/unavailable."""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT success FROM model_metrics ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else row[0]


def _last_failure_event(tmp: Path):
    """Return last typed provider failure event, if any."""
    path = tmp / "provider_failures.jsonl"
    if not path.exists():
        return None
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])


def _run_wrapper(scenario: str, *, mode: str = "review", credential: str = "dummy-nonempty",
                 credential_file: str | None = None, prompt: bytes = b"do the review please",
                 timeout_s: str = "10"):
    """Run zai_cli.py with the stub on PATH. Returns (proc, counter, db_path, tmp)."""
    tmp = Path(tempfile.mkdtemp(prefix="zai_resil_"))
    bindir = _make_stub_dir(tmp)
    counter_file = tmp / "counter.txt"
    db_path = tmp / "metrics.db"
    _make_metrics_db(db_path)

    env = os.environ.copy()
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["STUB_SCENARIO"] = scenario
    env["STUB_COUNTER_FILE"] = str(counter_file)
    env["CLAUDE_BOOSTER_METRICS_DB"] = str(db_path)
    env["CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG"] = str(tmp / "provider_failures.jsonl")
    env["ZAI_CLI_TIMEOUT_S"] = timeout_s
    env["ZAI_PREFLIGHT_DISABLE"] = "1"
    env["CLAUDE_SESSION_ID"] = "zai-resilience-test"
    # Credential control
    if credential is None:
        env["ZAI_API_KEY"] = ""
    else:
        env["ZAI_API_KEY"] = credential
    # Point secret file away from any real key unless a specific one is provided.
    env["ZAI_API_KEY_FILE"] = credential_file or str(tmp / "no_such_secret")

    proc = subprocess.run(
        [sys.executable, str(ZAI_CLI), mode, "--budget", "3"],
        input=prompt,
        capture_output=True,
        env=env,
        cwd=str(tmp),
    )
    counter = 0
    try:
        counter = int(counter_file.read_text().strip() or "0")
    except (FileNotFoundError, ValueError):
        counter = 0
    return proc, counter, db_path, tmp


def _cleanup(tmp: Path) -> None:
    shutil.rmtree(tmp, ignore_errors=True)


def case_always_empty():
    proc, counter, db_path, tmp = _run_wrapper("always_empty", mode="review")
    try:
        ok = (proc.returncode != 0 and proc.stdout == b"" and counter == 1
              and _last_success(db_path) == 0)
        _report(ok, "always_empty",
                f"rc={proc.returncode} stdout={proc.stdout!r} attempts={counter} "
                f"success={_last_success(db_path)}")
    finally:
        _cleanup(tmp)


def case_always_nonempty():
    proc, counter, db_path, tmp = _run_wrapper("always_nonempty", mode="review")
    try:
        ok = (proc.returncode == 0 and proc.stdout == b"GLM_OK_PAYLOAD_NO_NEWLINE"
              and counter == 1 and _last_success(db_path) == 1)
        _report(ok, "always_nonempty",
                f"rc={proc.returncode} stdout={proc.stdout!r} attempts={counter} "
                f"success={_last_success(db_path)}")
    finally:
        _cleanup(tmp)


def case_no_retry_empty_then_nonempty():
    proc, counter, db_path, tmp = _run_wrapper("empty_then_nonempty", mode="review")
    try:
        ok = (proc.returncode != 0 and proc.stdout == b""
              and counter == 1 and _last_success(db_path) == 0)
        _report(ok, "no_retry_empty_then_nonempty",
                f"rc={proc.returncode} stdout={proc.stdout!r} attempts={counter} "
                f"success={_last_success(db_path)}")
    finally:
        _cleanup(tmp)


def case_immediate_nonzero():
    proc, counter, db_path, tmp = _run_wrapper("immediate_nonzero", mode="review")
    try:
        ok = (proc.returncode == 23 and counter == 1 and _last_success(db_path) == 0)
        _report(ok, "immediate_nonzero",
                f"rc={proc.returncode} (want 23) attempts={counter} "
                f"success={_last_success(db_path)}")
    finally:
        _cleanup(tmp)


def case_invalid_model_failure_event():
    proc, counter, db_path, tmp = _run_wrapper(
        "invalid_model", mode="review", credential="secret-token"
    )
    try:
        event = _last_failure_event(tmp)
        ok = (
            proc.returncode == 1
            and counter == 1
            and _last_success(db_path) == 0
            and event is not None
            and event["failure_type"] == "invalid_model"
            and event["permanent"] is True
            and "secret-token" not in event["detail"]
            and b"secret-token" not in proc.stderr
        )
        _report(ok, "invalid_model_failure_event", f"event={event!r} stderr={proc.stderr!r}")
    finally:
        _cleanup(tmp)


def case_insufficient_balance_failure_event():
    proc, counter, db_path, tmp = _run_wrapper(
        "insufficient_balance", mode="review", credential="secret-token"
    )
    try:
        event = _last_failure_event(tmp)
        ok = (
            proc.returncode == 1
            and counter == 1
            and _last_success(db_path) == 0
            and event is not None
            and event["failure_type"] == "insufficient_balance"
            and event["permanent"] is True
            and "secret-token" not in event["detail"]
            and b"secret-token" not in proc.stderr
        )
        _report(ok, "insufficient_balance_failure_event", f"event={event!r} stderr={proc.stderr!r}")
    finally:
        _cleanup(tmp)


def case_timeout():
    proc, counter, db_path, tmp = _run_wrapper("sleep", mode="review", timeout_s="0.05")
    try:
        event = _last_failure_event(tmp)
        ok = (
            proc.returncode == 124
            and counter in (0, 1)
            and _last_success(db_path) == 0
            and event is not None
            and event["failure_type"] == "timeout"
        )
        _report(ok, "timeout", f"rc={proc.returncode} event={event!r}")
    finally:
        _cleanup(tmp)


def case_partial_nonzero():
    proc, counter, db_path, tmp = _run_wrapper("partial_nonzero", mode="review")
    try:
        ok = (proc.returncode == 23 and proc.stdout == b"PARTIAL_BEFORE_CRASH"
              and counter == 1 and _last_success(db_path) == 0)
        _report(ok, "partial_nonzero",
                f"rc={proc.returncode} stdout={proc.stdout!r} attempts={counter} "
                f"success={_last_success(db_path)}")
    finally:
        _cleanup(tmp)


def case_crlf():
    proc, counter, db_path, tmp = _run_wrapper("crlf", mode="review")
    try:
        ok = (proc.returncode == 0 and proc.stdout == b"line1\r\nline2\r\n")
        _report(ok, "crlf_fidelity", f"rc={proc.returncode} stdout={proc.stdout!r}")
    finally:
        _cleanup(tmp)


def case_nonascii():
    proc, counter, db_path, tmp = _run_wrapper("nonascii", mode="review")
    try:
        expected = "café — 日本語".encode("utf-8")
        ok = (proc.returncode == 0 and proc.stdout == expected)
        _report(ok, "nonascii_fidelity", f"rc={proc.returncode} stdout={proc.stdout!r}")
    finally:
        _cleanup(tmp)


def case_stderr_sentinel():
    proc, counter, db_path, tmp = _run_wrapper("stderr_sentinel", mode="review")
    try:
        ok = (proc.returncode == 0 and b"SENTINEL_STDERR" in proc.stderr
              and proc.stdout == b"STDOUT_OK")
        _report(ok, "stderr_passthrough",
                f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}")
    finally:
        _cleanup(tmp)


def case_large():
    proc, counter, db_path, tmp = _run_wrapper("large", mode="review")
    try:
        ok = (proc.returncode == 0 and len(proc.stdout) == 2000000
              and proc.stdout == b"A" * 2000000)
        _report(ok, "large_output",
                f"rc={proc.returncode} len={len(proc.stdout)}")
    finally:
        _cleanup(tmp)


def case_missing_credential():
    # No env key, secret file points to a nonexistent path -> must exit 64.
    proc, counter, db_path, tmp = _run_wrapper(
        "always_nonempty", mode="review", credential=None)
    try:
        ok = (proc.returncode == 64 and counter == 0)
        _report(ok, "missing_credential_exit_64",
                f"rc={proc.returncode} (want 64) attempts={counter}")
    finally:
        _cleanup(tmp)


def case_smoke_mode():
    # smoke mode should exercise the same resilience path.
    proc, counter, db_path, tmp = _run_wrapper("always_nonempty", mode="smoke")
    try:
        ok = (proc.returncode == 0 and proc.stdout == b"GLM_OK_PAYLOAD_NO_NEWLINE"
              and counter == 1)
        _report(ok, "smoke_mode_nonempty",
                f"rc={proc.returncode} stdout={proc.stdout!r} attempts={counter}")
    finally:
        _cleanup(tmp)


def case_integration_regression():
    if not INTEGRATION_TEST.exists():
        _report(False, "integration_regression", "test_zai_integration.py missing")
        return
    # Prefer pytest (file uses pytest fixtures); fall back to direct run.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(INTEGRATION_TEST)],
        capture_output=True, cwd=str(ROOT),
    )
    ok = proc.returncode == 0
    detail = ""
    if not ok:
        detail = (proc.stdout.decode("utf-8", "replace")[-400:]
                  + proc.stderr.decode("utf-8", "replace")[-400:])
    _report(ok, "integration_regression", detail)


def main() -> int:
    if not ZAI_CLI.exists():
        print(f"[FAIL] setup :: zai_cli.py not found at {ZAI_CLI}")
        print("Results: 0 passed, 1 failed")
        return 1

    case_always_empty()
    case_always_nonempty()
    case_no_retry_empty_then_nonempty()
    case_immediate_nonzero()
    case_invalid_model_failure_event()
    case_insufficient_balance_failure_event()
    case_timeout()
    case_partial_nonzero()
    case_crlf()
    case_nonascii()
    case_stderr_sentinel()
    case_large()
    case_missing_credential()
    case_smoke_mode()
    case_integration_regression()

    print(f"Results: {PASS_COUNT} passed, {FAIL_COUNT} failed")
    return 0 if FAIL_COUNT == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
