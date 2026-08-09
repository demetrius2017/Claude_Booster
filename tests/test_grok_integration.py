#!/usr/bin/env python3
"""Regression tests for Grok fourth-model integration.

These tests avoid network calls. They import template scripts directly and
monkeypatch subprocess execution so no real Grok/xAI request is made.
"""
from __future__ import annotations

import importlib
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
        model="grok-4.5",
        budget_turns=3,
        read_only=True,
        task_category="audit_tertiary",
    )

    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[:2] == ["/usr/bin/grok", "-p"]
    assert "grok-4.5" in cmd
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
        model="grok-4.5",
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
        "grok-4.5",
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
        "model": "grok-4.5",
    }
    assert routing["hackathon_coder"] == {
        "provider": "grok-cli",
        "model": "grok-4.5",
    }
    assert model_balancer._get_intelligence_score("grok-cli", "grok-4.5") == 17


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
        model="grok-4.5",
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
        model="grok-4.5",
        budget_turns=3,
        read_only=True,
        task_category="audit_tertiary",
    )

    with sqlite3.connect(db_path) as conn:
        success = conn.execute("SELECT success FROM model_metrics").fetchone()[0]
    assert rc == 69
    assert success == 0


def test_grok_cli_preserves_partial_stdout_and_nonzero(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GROK_CLI_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("GROK_BIN", "/usr/bin/grok")
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
        model="grok-4.5",
        budget_turns=3,
        read_only=True,
        task_category="audit_tertiary",
    )

    captured = capsys.readouterr()
    assert rc == 23
    assert captured.out.encode("utf-8", "surrogateescape") == b"PARTIAL"
    assert captured.err.encode("utf-8", "surrogateescape") == b"ERR"
