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

    def fake_run(cmd, *, text, check):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["text"] = text
        captured["check"] = check
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(grok_cli.shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = grok_cli._run_grok(
        "review this",
        model="grok-composer-2.5-fast",
        budget_turns=3,
        read_only=True,
        task_category="audit_tertiary",
    )

    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[:2] == ["/usr/bin/grok", "-p"]
    assert "grok-composer-2.5-fast" in cmd
    assert "--permission-mode" in cmd
    assert "dontAsk" in cmd
    assert "--disallowed-tools" in cmd
    assert "Edit,Write,NotebookEdit" in cmd


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
        model="grok-composer-2.5-fast",
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
        "grok-composer-2.5-fast",
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
        "model": "grok-composer-2.5-fast",
    }
    assert routing["hackathon_coder"] == {
        "provider": "grok-cli",
        "model": "grok-build",
    }
    assert model_balancer._get_intelligence_score("grok-cli", "grok-build") == 18
