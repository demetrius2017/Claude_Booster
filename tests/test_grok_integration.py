#!/usr/bin/env python3
"""Regression tests for Grok fourth-model integration.

These tests avoid network calls. They import template scripts directly and
monkeypatch subprocess execution so no real Grok/xAI request is made.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "templates" / "scripts"


def _import_script(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def test_grok_cli_builds_read_only_command(monkeypatch) -> None:
    monkeypatch.setenv("GROK_CLI_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("GROK_BIN", "/usr/bin/grok")
    grok_cli = _import_script("grok_cli")
    assert grok_cli.DEFAULT_MODEL == "grok-4.6"
    assert grok_cli.DEFAULT_CODER_MODEL == "grok-4.6"
    captured: dict[str, object] = {}

    def fake_which(binary):  # noqa: ANN001
        return binary

    def fake_run(cmd, *, stdout, stderr, check, timeout):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        captured["check"] = check
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(cmd, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(grok_cli.shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = grok_cli._run_grok(
        "review this",
        model="grok-4.6",
        budget_turns=3,
        read_only=True,
        task_category="audit_tertiary",
    )

    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[:2] == ["/usr/bin/grok", "-p"]
    assert "grok-4.6" in cmd
    assert "--permission-mode" in cmd
    assert "dontAsk" in cmd
    assert "--disallowed-tools" in cmd
    assert "Edit,Write,NotebookEdit,create_goal,update_goal" in cmd
    assert captured["timeout"] >= 180


def test_grok_cli_records_model_metrics(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "metrics.db"
    with sqlite3.connect(db_path) as conn:
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

    monkeypatch.setenv("CLAUDE_BOOSTER_METRICS_DB", str(db_path))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-grok-session")
    grok_cli = _import_script("grok_cli")

    grok_cli._record_metric(
        model="grok-4.6",
        task_category="audit_tertiary",
        duration_ms=4321,
        success=True,
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT provider, model, task_category, duration_ms, per_turn_ms,
                   tokens_in, tokens_out, success, session_id
            FROM model_metrics
            """
        ).fetchone()

    assert row == (
        "grok-cli",
        "grok-4.6",
        "audit_tertiary",
        4321,
        4321,
        None,
        None,
        1,
        "test-grok-session",
    )


def test_model_balancer_exposes_grok_routes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_MODEL_BALANCER_PATH", str(tmp_path / "balancer.json"))
    model_balancer = _import_script("model_balancer")

    routing = model_balancer.DEFAULTS["routing"]

    assert routing["audit_tertiary"] == {
        "provider": "grok-cli",
        "model": "grok-4.6",
    }
    assert routing["hackathon_coder"] == {
        "provider": "grok-cli",
        "model": "grok-4.6",
    }
    assert model_balancer._get_intelligence_score("grok-cli", "grok-4.6") == 17


def test_grok_cli_preserves_raw_stdout_bytes(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GROK_CLI_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("GROK_BIN", "/usr/bin/grok")
    grok_cli = _import_script("grok_cli")
    payload = b"line1\r\ncaf\xc3\xa9\x00tail"

    monkeypatch.setattr(grok_cli.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *, stdout, stderr, check, timeout: subprocess.CompletedProcess(
            cmd, 0, stdout=payload, stderr=b""
        ),
    )

    rc = grok_cli._run_grok(
        "review this",
        model="grok-4.6",
        budget_turns=3,
        read_only=True,
        task_category="audit_tertiary",
    )

    assert rc == 0
    assert capsys.readouterr().out.encode("utf-8", "surrogateescape") == payload


def test_grok_cli_rejects_whitespace_only_success_and_records_truth(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "metrics.db"
    with sqlite3.connect(db_path) as conn:
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
    monkeypatch.setenv("CLAUDE_BOOSTER_METRICS_DB", str(db_path))
    monkeypatch.setenv("GROK_BIN", "/usr/bin/grok")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(tmp_path / "events.jsonl"))
    grok_cli = _import_script("grok_cli")

    monkeypatch.setattr(grok_cli.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *, stdout, stderr, check, timeout: subprocess.CompletedProcess(
            cmd, 0, stdout=b" \n\t", stderr=b""
        ),
    )

    rc = grok_cli._run_grok(
        "review this",
        model="grok-4.6",
        budget_turns=3,
        read_only=True,
        task_category="audit_tertiary",
    )

    with sqlite3.connect(db_path) as conn:
        success = conn.execute("SELECT success FROM model_metrics").fetchone()[0]
    assert rc == 69
    assert success == 0


def test_grok_cli_preserves_partial_stdout_and_nonzero(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setenv("GROK_CLI_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("GROK_BIN", "/usr/bin/grok")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(tmp_path / "events.jsonl"))
    grok_cli = _import_script("grok_cli")

    monkeypatch.setattr(grok_cli.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *, stdout, stderr, check, timeout: subprocess.CompletedProcess(
            cmd, 23, stdout=b"PARTIAL", stderr=b"ERR"
        ),
    )

    rc = grok_cli._run_grok(
        "review this",
        model="grok-4.6",
        budget_turns=3,
        read_only=True,
        task_category="audit_tertiary",
    )

    captured = capsys.readouterr()
    assert rc == 23
    assert captured.out.encode("utf-8", "surrogateescape") == b"PARTIAL"
    assert captured.err.startswith("ERR")


def _events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _status_env(monkeypatch, tmp_path, *, binary: bool, auth: bool) -> None:
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("GROK_CLI_DISABLE_TELEMETRY", "1")
    bin_path = tmp_path / "grok"
    if binary:
        bin_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        bin_path.chmod(0o755)
    monkeypatch.setenv("GROK_BIN", str(bin_path))
    auth_path = tmp_path / "auth.json"
    if auth:
        auth_path.write_text('{"token": "xai-secret-token-value"}', encoding="utf-8")
    monkeypatch.setenv("GROK_AUTH_FILE", str(auth_path))


def test_grok_status_available_without_smoke(monkeypatch, tmp_path, capsys) -> None:
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest_fail_no_child(),
    )

    rc = grok_cli._status(model="grok-4.6", run_smoke=False, task_category="grok_status")

    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 0
    assert len(out) == 1
    assert out[0].startswith("grok_cli: status=available model=grok-4.6 binary=")


def pytest_fail_no_child():  # noqa: D103
    raise AssertionError("status without --smoke must not launch a child process")


def test_grok_status_binary_missing(monkeypatch, tmp_path, capsys) -> None:
    _status_env(monkeypatch, tmp_path, binary=False, auth=True)
    grok_cli = _import_script("grok_cli")
    monkeypatch.setattr(grok_cli.shutil, "which", lambda binary: None)

    rc = grok_cli._status(model="grok-4.6", run_smoke=False, task_category="grok_status")

    out = capsys.readouterr().out.strip()
    assert rc == 127
    assert out == "grok_cli: status=unavailable reason=binary_missing model=grok-4.6"
    events = _events(tmp_path / "events.jsonl")
    assert [event["failure_type"] for event in events] == ["binary_missing"]
    assert events[0]["permanent"] is True
    assert events[0]["returncode"] == 127


def test_grok_status_auth_missing(monkeypatch, tmp_path, capsys) -> None:
    _status_env(monkeypatch, tmp_path, binary=True, auth=False)
    grok_cli = _import_script("grok_cli")

    rc = grok_cli._status(model="grok-4.6", run_smoke=False, task_category="grok_status")

    out = capsys.readouterr().out.strip()
    assert rc == 69
    assert out.startswith("grok_cli: status=unavailable reason=auth_missing model=grok-4.6")
    events = _events(tmp_path / "events.jsonl")
    assert [event["failure_type"] for event in events] == ["auth_missing"]
    assert events[0]["permanent"] is True
    assert events[0]["returncode"] == 69


def test_grok_status_smoke_failure_records_event(monkeypatch, tmp_path, capsys) -> None:
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *, stdout, stderr, check, timeout: subprocess.CompletedProcess(
            cmd, 4, stdout=b"", stderr=b"upstream refused"
        ),
    )

    rc = grok_cli._status(model="grok-4.6", run_smoke=True, task_category="grok_status")

    assert rc == 4
    assert capsys.readouterr().out.strip() == (
        "grok_cli: status=unavailable reason=smoke_failed model=grok-4.6 returncode=4"
    )
    events = _events(tmp_path / "events.jsonl")
    assert len(events) == 1
    assert events[0]["provider"] == "grok-cli"
    assert events[0]["task_category"] == "grok_status"
    assert events[0]["failure_type"] == "nonzero_exit"


def test_grok_status_smoke_success_records_metric(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "metrics.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE model_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                task_category TEXT, duration_ms INTEGER, num_turns INTEGER,
                per_turn_ms INTEGER, tokens_in INTEGER, tokens_out INTEGER,
                success INTEGER NOT NULL DEFAULT 1, session_id TEXT, project_root TEXT
            )
            """
        )
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    monkeypatch.delenv("GROK_CLI_DISABLE_TELEMETRY", raising=False)
    monkeypatch.setenv("CLAUDE_BOOSTER_METRICS_DB", str(db_path))
    grok_cli = _import_script("grok_cli")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *, stdout, stderr, check, timeout: subprocess.CompletedProcess(
            cmd, 0, stdout=b"OK", stderr=b""
        ),
    )

    rc = grok_cli._status(model="grok-4.6", run_smoke=True, task_category="grok_status")

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT task_category, success FROM model_metrics").fetchone()
    assert rc == 0
    assert row == ("grok_status", 1)


def test_grok_status_without_smoke_records_no_metric(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "metrics.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE model_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                task_category TEXT, duration_ms INTEGER, num_turns INTEGER,
                per_turn_ms INTEGER, tokens_in INTEGER, tokens_out INTEGER,
                success INTEGER NOT NULL DEFAULT 1, session_id TEXT, project_root TEXT
            )
            """
        )
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    monkeypatch.delenv("GROK_CLI_DISABLE_TELEMETRY", raising=False)
    monkeypatch.setenv("CLAUDE_BOOSTER_METRICS_DB", str(db_path))
    grok_cli = _import_script("grok_cli")

    rc = grok_cli._status(model="grok-4.6", run_smoke=False, task_category="grok_status")

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM model_metrics").fetchone()[0]
    assert rc == 0
    assert rows == 0


def test_grok_status_accepts_auth_status_alias(monkeypatch, tmp_path, capsys) -> None:
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")
    monkeypatch.setattr(sys, "argv", ["grok_cli.py", "auth-status"])

    rc = grok_cli.main()

    assert rc == 0
    assert capsys.readouterr().out.startswith("grok_cli: status=available")


def test_grok_review_default_budget_is_eight(monkeypatch, tmp_path, capsys) -> None:
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")
    captured: dict[str, object] = {}

    def fake_run(cmd, *, stdout, stderr, check, timeout):  # noqa: ANN001
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["grok_cli.py", "review"])
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": staticmethod(lambda: "review this")})())

    rc = grok_cli.main()
    capsys.readouterr()

    cmd = captured["cmd"]
    assert rc == 0
    assert cmd[cmd.index("--max-turns") + 1] == "8"


def test_grok_failure_event_on_nonzero_exit(monkeypatch, tmp_path) -> None:
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *, stdout, stderr, check, timeout: subprocess.CompletedProcess(
            cmd, 3, stdout=b"", stderr=b"boom api_key=xai-abcdefghijklmnop failed " + b"z" * 600
        ),
    )

    rc = grok_cli._run_grok(
        "review this",
        model="grok-4.6",
        budget_turns=8,
        read_only=True,
        task_category="audit_tertiary",
    )

    events = _events(tmp_path / "events.jsonl")
    assert rc == 3
    assert len(events) == 1
    assert events[0]["failure_type"] == "nonzero_exit"
    assert events[0]["returncode"] == 3
    assert events[0]["provider"] == "grok-cli"
    assert len(events[0]["detail"].encode("utf-8")) <= 300
    assert "xai-abcdefghijklmnop" not in events[0]["detail"]


def test_grok_failure_event_on_empty_response(monkeypatch, tmp_path) -> None:
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *, stdout, stderr, check, timeout: subprocess.CompletedProcess(
            cmd, 0, stdout=b"   ", stderr=b""
        ),
    )

    rc = grok_cli._run_grok(
        "review this",
        model="grok-4.6",
        budget_turns=8,
        read_only=True,
        task_category="audit_tertiary",
    )

    events = _events(tmp_path / "events.jsonl")
    assert rc == 69
    assert [event["failure_type"] for event in events] == ["empty_response"]


def test_grok_failure_event_on_timeout(monkeypatch, tmp_path) -> None:
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")

    def timing_out(cmd, *, stdout, stderr, check, timeout):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd, timeout, output=b"", stderr=b"deadline")

    monkeypatch.setattr(subprocess, "run", timing_out)

    rc = grok_cli._run_grok(
        "review this",
        model="grok-4.6",
        budget_turns=8,
        read_only=True,
        task_category="audit_tertiary",
    )

    events = _events(tmp_path / "events.jsonl")
    assert rc == 124
    assert [event["failure_type"] for event in events] == ["timeout"]


def test_grok_failure_event_on_binary_missing(monkeypatch, tmp_path) -> None:
    _status_env(monkeypatch, tmp_path, binary=False, auth=True)
    grok_cli = _import_script("grok_cli")
    monkeypatch.setattr(grok_cli.shutil, "which", lambda binary: None)

    rc = grok_cli._run_grok(
        "review this",
        model="grok-4.6",
        budget_turns=8,
        read_only=True,
        task_category="audit_tertiary",
    )

    events = _events(tmp_path / "events.jsonl")
    assert rc == 127
    assert [event["failure_type"] for event in events] == ["binary_missing"]
    assert events[0]["permanent"] is True


def test_grok_status_smoke_timeout_exits_124(monkeypatch, tmp_path, capsys) -> None:
    """A smoke probe that times out must surface 124, not the generic 69."""
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")

    def fake_run(cmd, *, stdout, stderr, check, timeout):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd, timeout, output=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = grok_cli._status(model="grok-4.6", run_smoke=True, task_category="grok_status")

    assert rc == 124
    assert capsys.readouterr().out.strip() == (
        "grok_cli: status=unavailable reason=smoke_timeout model=grok-4.6 returncode=124"
    )
    events = _events(tmp_path / "events.jsonl")
    assert [event["failure_type"] for event in events] == ["timeout"]
    assert events[0]["permanent"] is False


def test_grok_status_smoke_empty_response_exits_69(monkeypatch, tmp_path, capsys) -> None:
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *, stdout, stderr, check, timeout: subprocess.CompletedProcess(
            cmd, 0, stdout=b"   \n", stderr=b""
        ),
    )

    rc = grok_cli._status(model="grok-4.6", run_smoke=True, task_category="grok_status")

    assert rc == 69
    assert capsys.readouterr().out.strip() == (
        "grok_cli: status=unavailable reason=smoke_empty_response model=grok-4.6 returncode=0"
    )
    events = _events(tmp_path / "events.jsonl")
    assert [event["failure_type"] for event in events] == ["empty_response"]


def test_sanitize_detail_redacts_dotted_jwt_at_the_tail(monkeypatch, tmp_path) -> None:
    """Redaction runs before tail truncation, so a trailing JWT cannot survive."""
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    stderr = ("filler " * 200) + f"Authorization: Bearer {jwt}"

    detail = grok_cli._sanitize_detail(stderr)

    assert "[redacted]" in detail
    assert jwt not in detail
    assert "eyJhbGciOiJIUzI1NiJ9" not in detail
    assert "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk" not in detail
    assert len(detail.encode("utf-8")) <= grok_cli.DETAIL_LIMIT_BYTES


def test_sanitize_detail_redacts_vendor_keys_and_assignments(monkeypatch, tmp_path) -> None:
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")

    detail = grok_cli._sanitize_detail(
        "xai-abcdefghijklmnop1234 sk-ABCDEFGHIJKLMNOP "
        "api_key=Super-Secret-Value-9f2 token=ghp_A1b2C3d4E5f6G7h8I9j0"
    )

    assert "xai-abcdefghijklmnop1234" not in detail
    assert "sk-ABCDEFGHIJKLMNOP" not in detail
    assert "Super-Secret-Value-9f2" not in detail
    assert "ghp_A1b2C3d4E5f6G7h8I9j0" not in detail


def test_grok_child_stderr_is_redacted_before_passthrough(monkeypatch, tmp_path, capsys) -> None:
    """Item 7: raw child stderr must never reach our stderr unredacted."""
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib29zdGVyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    child_stderr = f"grok: request failed\nAuthorization: Bearer {jwt}\n".encode()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *, stdout, stderr, check, timeout: subprocess.CompletedProcess(
            cmd, 0, stdout=b"body", stderr=child_stderr
        ),
    )

    rc = grok_cli._run_grok(
        "review this",
        model="grok-4.6",
        budget_turns=8,
        read_only=True,
        task_category="audit_tertiary",
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert jwt not in captured.err
    assert "[redacted]" in captured.err
    # The non-secret diagnostic text survives at full length (no tail truncation).
    assert "grok: request failed" in captured.err


def test_max_turns_is_classified_as_non_permanent_budget_failure(
    monkeypatch, tmp_path, capsys
) -> None:
    """A real 79 KB repo-reading review exited 1 with 'Max turns reached'.

    That is a budget failure, not a Grok outage: the recorded event must be
    typed ``max_turns`` and non-permanent so the balancer does not mark the
    provider DEGRADED, and the child exit code 1 must be preserved.
    """
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")
    child_stderr = (
        b"tool_error: tool_output_error tool_name=\"read_file\"\n"
        b"Max turns reached\nError: max turns reached\n"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *, stdout, stderr, check, timeout: subprocess.CompletedProcess(
            cmd, 1, stdout=b"I will start by reading the repository...", stderr=child_stderr
        ),
    )

    rc = grok_cli._run_grok(
        "review this",
        model="grok-4.6",
        budget_turns=8,
        read_only=True,
        task_category="audit_tertiary",
    )

    assert rc == 1
    assert "failure_type=max_turns" in capsys.readouterr().err
    events = _events(tmp_path / "events.jsonl")
    assert [event["failure_type"] for event in events] == ["max_turns"]
    assert events[0]["permanent"] is False
    assert events[0]["returncode"] == 1


def test_max_turns_wins_over_auth_missing_fallback(monkeypatch, tmp_path) -> None:
    """Budget exhaustion must not be misreported as a missing-auth failure."""
    _status_env(monkeypatch, tmp_path, binary=True, auth=False)
    grok_cli = _import_script("grok_cli")

    assert (
        grok_cli._classify_failure(1, is_empty=False, stderr=b"Error: MAX TURNS REACHED")
        == "max_turns"
    )
    assert grok_cli._classify_failure(1, is_empty=False, stderr=b"boom") == "auth_missing"
    assert "max_turns" not in grok_cli.PERMANENT_FAILURE_TYPES


def test_sanitize_detail_strips_ansi_escape_sequences(monkeypatch, tmp_path) -> None:
    """TUI colour codes must not survive into the recorded detail."""
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")

    detail = grok_cli._sanitize_detail(
        "\x1b[3m\x1b[38;5;244mtool_error\x1b[0m: \x1b[1mMax turns reached\x1b[m"
    )

    assert detail == "tool_error: Max turns reached"
    assert "\x1b" not in detail
    assert "[3m" not in detail


def test_redaction_preserves_ordinary_diagnostic_identifiers(monkeypatch, tmp_path) -> None:
    """Over-broad redaction destroyed the diagnostics it was meant to protect.

    These identifiers and prose fragments contain no credential, so they must
    survive byte-for-byte; otherwise a failure log reads only ``[redacted]``.
    """
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")

    survivors = (
        "api_response_code",
        "token_budget_exhausted",
        "key_not_found_in_cache",
        "token_count_exceeded",
        "error key: missing_field",
        "token: expired",
        "password: authentication required",
    )
    for text in survivors:
        assert grok_cli._sanitize_detail(text) == text, text
        assert "[redacted]" not in grok_cli._redact(text), text

    combined = grok_cli._sanitize_detail(" ".join(survivors))
    assert "[redacted]" not in combined


def test_redaction_still_catches_real_credential_shapes(monkeypatch, tmp_path) -> None:
    """Tightening must not open a hole: real key shapes stay redacted."""
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib29zdGVyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    secrets = {
        "api_key=xai-abc123def456ghi789jkl": "xai-abc123def456ghi789jkl",
        f"Authorization: Bearer {jwt}": jwt,
        "token=ghp_A1b2C3d4E5f6G7h8I9j0K1l2": "ghp_A1b2C3d4E5f6G7h8I9j0K1l2",
    }
    for line, secret in secrets.items():
        detail = grok_cli._sanitize_detail(line)
        assert secret not in detail, line
        assert "[redacted]" in detail, line


def test_redaction_matches_reported_diagnostic_and_credential_lines(
    monkeypatch, tmp_path
) -> None:
    """Exact lines from the second-round review: prose intact, credentials gone."""
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")

    unchanged = (
        "api_response_code token_budget_exhausted key_not_found_in_cache token_count_exceeded",
        "error key: missing_field token: expired password: authentication required",
    )
    for line in unchanged:
        assert grok_cli._sanitize_detail(line) == line

    assert grok_cli._sanitize_detail("api_key=xai-abc123def456ghi789jkl012") == "[redacted]"
    assert (
        grok_cli._sanitize_detail(
            "Authorization: Bearer eyJhbGciOi.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4"
        )
        == "Authorization: [redacted]"
    )
    assert (
        grok_cli._sanitize_detail("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123") == "[redacted]"
    )


def test_redaction_catches_bare_jwt_aws_and_slack_shapes(monkeypatch, tmp_path) -> None:
    """Shapes with no assignment prefix must still be redacted."""
    _status_env(monkeypatch, tmp_path, binary=True, auth=True)
    grok_cli = _import_script("grok_cli")

    for secret in (
        "eyJhbGciOi.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4",
        "AKIAIOSFODNN7EXAMPLE1234",
        "xoxb-" + "0" * 10 + "-" + "A" * 16,
    ):
        detail = grok_cli._sanitize_detail(f"request failed with {secret} attached")
        assert secret not in detail, secret
        assert detail.startswith("request failed with [redacted]")
