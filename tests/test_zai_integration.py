#!/usr/bin/env python3
"""Regression tests for Z.ai third-model integration.

These tests avoid network calls. They import template scripts directly and
monkeypatch subprocess execution so no real Claude/Z.ai request is made.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "templates" / "scripts"


def _import_script(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def test_zai_cli_requires_key(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.setenv("ZAI_API_KEY_FILE", str(ROOT / ".missing-zai-key-for-test"))
    zai_cli = _import_script("zai_cli")

    try:
        zai_cli._env()
    except SystemExit as exc:
        assert exc.code == 64
    else:  # pragma: no cover - defensive
        raise AssertionError("_env() did not reject missing ZAI_API_KEY")

    err = capsys.readouterr().err
    assert "missing ZAI_API_KEY" in err


def test_zai_cli_reads_local_secret_file(monkeypatch, tmp_path) -> None:
    key_path = tmp_path / "zai_api_key"
    key_path.write_text("secret-from-file\n", encoding="utf-8")
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.setenv("ZAI_API_KEY_FILE", str(key_path))
    zai_cli = _import_script("zai_cli")

    env = zai_cli._env()

    assert env["ANTHROPIC_AUTH_TOKEN"] == "secret-from-file"


def test_zai_cli_builds_read_only_claude_command(monkeypatch) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-value-that-must-not-print")
    monkeypatch.setenv("ZAI_CLI_DISABLE_TELEMETRY", "1")
    zai_cli = _import_script("zai_cli")
    captured: dict[str, object] = {}

    def fake_run(cmd, *, input, text, env, check):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["input"] = input
        captured["env"] = env
        captured["check"] = check
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = zai_cli._run_claude(
        "review this",
        model="glm-5.2[1m]",
        budget="5",
        tools="",
        read_only=True,
        task_category="audit_secondary",
    )

    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[:3] == ["claude", "--bare", "--print"]
    assert "glm-5.2[1m]" in cmd
    assert "Edit,Write,NotebookEdit" in cmd
    env = captured["env"]
    assert env["ANTHROPIC_AUTH_TOKEN"] == "secret-value-that-must-not-print"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"


def test_zai_cli_records_model_metrics(monkeypatch, tmp_path) -> None:
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
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-zai-session")
    zai_cli = _import_script("zai_cli")

    zai_cli._record_metric(
        model="glm-5.2[1m]",
        task_category="audit_secondary",
        duration_ms=1234,
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
        "zai-cli",
        "glm-5.2[1m]",
        "audit_secondary",
        1234,
        1234,
        None,
        None,
        1,
        "test-zai-session",
    )


def test_model_balancer_exposes_zai_routes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_MODEL_BALANCER_PATH", str(tmp_path / "balancer.json"))
    model_balancer = _import_script("model_balancer")

    routing = model_balancer.DEFAULTS["routing"]
    assert routing["audit_secondary"] == {
        "provider": "zai-cli",
        "model": "glm-5.2[1m]",
    }
    assert routing["hackathon_external"] == {
        "provider": "zai-cli",
        "model": "glm-5.2[1m]",
    }
    assert model_balancer._get_intelligence_score("zai-cli", "glm-5.2[1m]") == 18


def test_model_balancer_merges_new_routes_into_existing_file(monkeypatch, tmp_path) -> None:
    balancer_path = tmp_path / "balancer.json"
    balancer_path.write_text(
        '{"schema_version": 2, "routing": {"audit_external": {"provider": "pal", "model": "gpt-5.5"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_MODEL_BALANCER_PATH", str(balancer_path))
    model_balancer = _import_script("model_balancer")

    decision = model_balancer.current_decision()

    assert decision["routing"]["audit_external"]["provider"] == "pal"
    assert decision["routing"]["audit_secondary"]["provider"] == "zai-cli"
    assert decision["routing"]["hackathon_external"]["model"] == "glm-5.2[1m]"


def test_model_balancer_persists_merged_routes_for_fresh_file(monkeypatch, tmp_path) -> None:
    balancer_path = tmp_path / "balancer.json"
    monkeypatch.setenv("CLAUDE_MODEL_BALANCER_PATH", str(balancer_path))
    model_balancer = _import_script("model_balancer")
    today = model_balancer._today_utc()
    balancer_path.write_text(
        (
            '{"schema_version": 2, '
            f'"decision_date": "{today}", '
            '"valid_until": "2099-01-01T00:00:00Z", '
            '"routing": {"audit_external": {"provider": "pal", "model": "gpt-5.5"}}, '
            '"rationale": "bootstrap — test fresh old shape"}'
        ),
        encoding="utf-8",
    )

    decision = model_balancer.decide()
    persisted = balancer_path.read_text(encoding="utf-8")

    assert decision["routing"]["audit_secondary"]["provider"] == "zai-cli"
    assert '"audit_secondary"' in persisted
    assert '"hackathon_external"' in persisted
