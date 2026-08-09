#!/usr/bin/env python3
"""Regression tests for Z.ai third-model integration.

These tests avoid network calls. They import template scripts directly and
monkeypatch subprocess execution so no real Claude/Z.ai request is made.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sqlite3
import sys
import threading
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
    monkeypatch.setenv("ZAI_PREFLIGHT_DISABLE", "1")
    zai_cli = _import_script("zai_cli")
    captured: dict[str, object] = {}

    def fake_run(cmd, *, input, env, check, **kwargs):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["input"] = input
        captured["env"] = env
        captured["check"] = check
        # New impl captures stdout as bytes; return a non-empty payload so the
        # empty-retry path is not triggered by this command-construction test.
        return subprocess.CompletedProcess(cmd, 0, stdout=b"ok")

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = zai_cli._run_claude(
        "review this",
        model="glm-5.2",
        budget="5",
        tools="",
        read_only=True,
        task_category="audit_secondary",
    )

    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[:3] == ["claude", "--bare", "--print"]
    assert "glm-5.2" in cmd
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
        model="glm-5.2",
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
        "glm-5.2",
        "audit_secondary",
        1234,
        1234,
        None,
        None,
        1,
        "test-zai-session",
    )


def test_zai_preflight_400_invalid_model_blocks_child_and_sanitizes(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-token")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("CLAUDE_BOOSTER_METRICS_DB", str(tmp_path / "missing.db"))
    zai_cli = _import_script("zai_cli")

    def fake_preflight(key, *, model, timeout_s):  # noqa: ANN001
        assert key == "secret-token"
        assert model == "glm-bad"
        return False, "invalid_model", "HTTP 400 invalid model secret-token", 400

    def child_must_not_launch(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("claude child should not launch after failed preflight")

    monkeypatch.setattr(zai_cli, "_direct_preflight", fake_preflight)
    monkeypatch.setattr(subprocess, "run", child_must_not_launch)

    rc = zai_cli._run_claude(
        "review this",
        model="glm-bad",
        budget="3",
        tools="",
        read_only=True,
        task_category="audit_secondary",
    )

    err = capsys.readouterr().err
    event = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rc == 75
    assert event["event_type"] == "failure"
    assert event["failure_type"] == "invalid_model"
    assert event["permanent"] is True
    assert "secret-token" not in event["detail"]
    assert "secret-token" not in err


def test_zai_preflight_429_insufficient_balance_blocks_child(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-token")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("CLAUDE_BOOSTER_METRICS_DB", str(tmp_path / "missing.db"))
    zai_cli = _import_script("zai_cli")
    monkeypatch.setattr(
        zai_cli,
        "_direct_preflight",
        lambda key, *, model, timeout_s: (
            False,
            "insufficient_balance",
            "HTTP 429 insufficient balance secret-token",
            429,
        ),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("child launched")),
    )

    rc = zai_cli._run_claude(
        "review this",
        model="glm-5.2",
        budget="3",
        tools="",
        read_only=True,
        task_category="audit_secondary",
    )

    event = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rc == 75
    assert event["failure_type"] == "insufficient_balance"
    assert event["permanent"] is True


def test_zai_preflight_success_allows_child_and_records_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-token")
    monkeypatch.setenv("ZAI_CLI_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(tmp_path / "events.jsonl"))
    zai_cli = _import_script("zai_cli")
    calls: dict[str, int] = {"child": 0}
    monkeypatch.setattr(
        zai_cli,
        "_direct_preflight",
        lambda key, *, model, timeout_s: (True, None, "ok", 0),
    )

    def fake_run(cmd, *, input, stdout, stderr, env, timeout, check):  # noqa: ANN001
        calls["child"] += 1
        return subprocess.CompletedProcess(cmd, 0, stdout=b"GLM_OK", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = zai_cli._run_claude(
        "review this",
        model="glm-5.2",
        budget="3",
        tools="",
        read_only=True,
        task_category="audit_secondary",
    )

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rc == 0
    assert calls["child"] == 1
    assert [event["event_type"] for event in events] == ["success"]


def test_zai_preflight_ok_then_permanent_child_failure_records_failure_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-token")
    monkeypatch.setenv("ZAI_CLI_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(tmp_path / "events.jsonl"))
    zai_cli = _import_script("zai_cli")
    monkeypatch.setattr(
        zai_cli,
        "_direct_preflight",
        lambda key, *, model, timeout_s: (True, None, "ok", 0),
    )

    def fake_run(cmd, *, input, stdout, stderr, env, timeout, check):  # noqa: ANN001
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=b"",
            stderr=b"API Error 429 insufficient balance: credit_balance_exhausted secret-token",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = zai_cli._run_claude(
        "review this",
        model="glm-5.2",
        budget="3",
        tools="",
        read_only=True,
        task_category="audit_secondary",
    )

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rc == 1
    assert [event["event_type"] for event in events] == ["failure"]
    assert events[0]["failure_type"] == "insufficient_balance"
    assert events[0]["permanent"] is True
    assert "secret-token" not in events[0]["detail"]


def test_zai_preflight_timeout_records_failure_and_never_launches_child(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "secret-token")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("CLAUDE_BOOSTER_METRICS_DB", str(tmp_path / "missing.db"))
    zai_cli = _import_script("zai_cli")
    monkeypatch.setattr(
        zai_cli,
        "_direct_preflight",
        lambda key, *, model, timeout_s: (False, "timeout", "preflight timeout", 0),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("child launched")),
    )

    rc = zai_cli._run_claude(
        "review this",
        model="glm-5.2",
        budget="3",
        tools="",
        read_only=True,
        task_category="audit_secondary",
    )

    event = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rc == 124
    assert event["failure_type"] == "timeout"
    assert event["permanent"] is False


def test_provider_event_sidecar_is_bounded_locked_and_private(monkeypatch, tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(path))
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_MAX_BYTES", "4096")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_RETAIN_LINES", "25")
    zai_cli = _import_script("zai_cli")

    def write_event(index: int) -> None:
        zai_cli._record_failure_event(
            model="glm-5.2",
            task_category="audit_secondary",
            failure_type="invalid_model",
            returncode=400,
            duration_ms=index,
            detail="x" * 200,
        )

    threads = [threading.Thread(target=write_event, args=(idx,)) for idx in range(80)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_size <= 4096
    assert len(lines) <= 25
    assert all(json.loads(line)["provider"] == "zai-cli" for line in lines)


def test_model_balancer_exposes_zai_routes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_MODEL_BALANCER_PATH", str(tmp_path / "balancer.json"))
    model_balancer = _import_script("model_balancer")

    routing = model_balancer.DEFAULTS["routing"]
    assert routing["audit_secondary"] == {
        "provider": "zai-cli",
        "model": "glm-5.2",
    }
    assert routing["hackathon_external"] == {
        "provider": "zai-cli",
        "model": "glm-5.2",
    }
    assert model_balancer._get_intelligence_score("zai-cli", "glm-5.2") == 18


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
    assert decision["routing"]["hackathon_external"]["model"] == "glm-5.2"


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


def test_model_balancer_demotes_unhealthy_zai_external_routes(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "metrics.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE model_metrics (
                ts_utc TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                task_category TEXT,
                per_turn_ms INTEGER,
                success INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        for _ in range(5):
            conn.execute(
                """
                INSERT INTO model_metrics
                    (ts_utc, provider, model, task_category, per_turn_ms, success)
                VALUES
                    (datetime('now'), 'zai-cli', 'glm-5.2', 'audit_secondary', 200000, 0)
                """
            )

    monkeypatch.setenv("CLAUDE_MODEL_BALANCER_PATH", str(tmp_path / "balancer.json"))
    model_balancer = _import_script("model_balancer")
    monkeypatch.setattr(model_balancer, "_DB_PATH", db_path)

    prior = json.loads(json.dumps(model_balancer.DEFAULTS))
    prior["decision_date"] = "2000-01-01"
    prior["valid_until"] = "2000-01-02T00:00:00Z"

    decision = model_balancer._active_decide(prior)

    assert decision["routing"]["audit_secondary"] == {
        "provider": "grok-cli",
        "model": "grok-4.5",
    }
    assert decision["routing"]["hackathon_external"] == {
        "provider": "grok-cli",
        "model": "grok-4.5",
    }
    health = decision["provider_health"]["zai-cli:glm-5.2"]
    assert health["status"] == "degraded"
    assert health["sample_count"] == 5
    assert health["failure_count"] == 5
    assert "health_fallbacks=2" in decision["rationale"]


def test_model_balancer_migrates_legacy_zai_alias(monkeypatch, tmp_path) -> None:
    balancer_path = tmp_path / "balancer.json"
    balancer_path.write_text(
        """
        {
          "schema_version": 2,
          "decision_date": "2026-01-01",
          "routing": {
            "audit_secondary": {"provider": "zai-cli", "model": "glm-5.2[1m]"},
            "hackathon_external": {"provider": "zai-cli", "model": "glm-5.2[1m]"}
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_MODEL_BALANCER_PATH", str(balancer_path))
    model_balancer = _import_script("model_balancer")

    decision = model_balancer.current_decision()

    assert decision["routing"]["audit_secondary"]["model"] == "glm-5.2"
    assert decision["routing"]["hackathon_external"]["model"] == "glm-5.2"


def test_model_balancer_immediately_demotes_permanent_zai_failure(monkeypatch, tmp_path) -> None:
    failure_log = tmp_path / "provider_failures.jsonl"
    failure_log.write_text(
        json.dumps(
            {
                "ts_utc": "2099-01-01T00:00:00Z",
                "provider": "zai-cli",
                "model": "glm-5.2",
                "task_category": "audit_secondary",
                "failure_type": "insufficient_balance",
                "permanent": True,
                "returncode": 1,
                "duration_ms": 100,
                "detail": "429 insufficient balance",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_MODEL_BALANCER_PATH", str(tmp_path / "balancer.json"))
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(failure_log))
    model_balancer = _import_script("model_balancer")
    monkeypatch.setattr(model_balancer, "_DB_PATH", tmp_path / "missing.db")

    prior = json.loads(json.dumps(model_balancer.DEFAULTS))
    decision = model_balancer._active_decide(prior)

    assert decision["routing"]["audit_secondary"] == {
        "provider": "grok-cli",
        "model": "grok-4.5",
    }
    health = decision["provider_health"]["zai-cli:glm-5.2"]
    assert health["status"] == "degraded"
    assert health["permanent_failure_count"] == 1
    assert health["degrade_reason"] == "insufficient_balance"


def test_model_balancer_success_event_supersedes_older_permanent_failure(monkeypatch, tmp_path) -> None:
    failure_log = tmp_path / "provider_events.jsonl"
    failure_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts_utc": "2099-01-01T00:00:00Z",
                        "event_type": "failure",
                        "provider": "zai-cli",
                        "model": "glm-5.2",
                        "task_category": "audit_secondary",
                        "failure_type": "insufficient_balance",
                        "permanent": True,
                        "returncode": 429,
                        "duration_ms": 100,
                        "detail": "429 insufficient balance",
                    }
                ),
                json.dumps(
                    {
                        "ts_utc": "2099-01-01T00:01:00Z",
                        "event_type": "success",
                        "provider": "zai-cli",
                        "model": "glm-5.2",
                        "task_category": "audit_secondary",
                        "failure_type": None,
                        "permanent": False,
                        "returncode": 0,
                        "duration_ms": 90,
                        "detail": "provider contact ok",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_MODEL_BALANCER_PATH", str(tmp_path / "balancer.json"))
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(failure_log))
    model_balancer = _import_script("model_balancer")
    monkeypatch.setattr(model_balancer, "_DB_PATH", tmp_path / "missing.db")

    prior = json.loads(json.dumps(model_balancer.DEFAULTS))
    decision = model_balancer._active_decide(prior)

    assert decision["routing"]["audit_secondary"] == {
        "provider": "zai-cli",
        "model": "glm-5.2",
    }
    assert decision["provider_health"] == {}


def test_model_balancer_decide_fast_path_applies_and_reverses_typed_health(monkeypatch, tmp_path) -> None:
    balancer_path = tmp_path / "balancer.json"
    failure_log = tmp_path / "provider_events.jsonl"
    monkeypatch.setenv("CLAUDE_MODEL_BALANCER_PATH", str(balancer_path))
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(failure_log))
    model_balancer = _import_script("model_balancer")
    monkeypatch.setattr(model_balancer, "_DB_PATH", tmp_path / "missing.db")

    today = model_balancer._today_utc()
    balancer_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "decision_date": today,
                "valid_until": "2099-01-01T00:00:00Z",
                "routing": {
                    "audit_secondary": {"provider": "zai-cli", "model": "glm-5.2"},
                    "hackathon_external": {"provider": "zai-cli", "model": "glm-5.2"},
                    "lead": {"provider": "anthropic", "model": "claude-opus-5"},
                },
                "transitions": [],
            }
        ),
        encoding="utf-8",
    )
    failure_log.write_text(
        json.dumps(
            {
                "ts_utc": "2099-01-01T00:00:00Z",
                "event_type": "failure",
                "provider": "zai-cli",
                "model": "glm-5.2",
                "task_category": "audit_secondary",
                "failure_type": "insufficient_balance",
                "permanent": True,
                "returncode": 429,
                "duration_ms": 20,
                "detail": "429 insufficient balance",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    degraded = model_balancer.decide()
    degraded_again = model_balancer.decide()
    assert degraded["routing"]["audit_secondary"] == {
        "provider": "grok-cli",
        "model": "grok-4.5",
    }
    assert degraded_again["routing"]["audit_secondary"] == degraded["routing"]["audit_secondary"]
    assert len(
        [
            transition
            for transition in degraded_again["transitions"]
            if transition["category"] == "audit_secondary"
            and transition["note"].startswith("provider health fallback:")
        ]
    ) == 1

    failure_log.write_text(
        failure_log.read_text(encoding="utf-8")
        + json.dumps(
            {
                "ts_utc": "2099-01-01T00:01:00Z",
                "event_type": "success",
                "provider": "zai-cli",
                "model": "glm-5.2",
                "task_category": "audit_secondary",
                "failure_type": None,
                "permanent": False,
                "returncode": 0,
                "duration_ms": 10,
                "detail": "wrapper call ok",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    recovered = model_balancer.decide()
    assert recovered["routing"]["audit_secondary"] == {
        "provider": "zai-cli",
        "model": "glm-5.2",
    }
    assert recovered["routing"]["lead"] == {"provider": "anthropic", "model": "claude-opus-5"}
    assert recovered["provider_health"] == {}
    assert recovered["transitions"][-1]["note"].startswith("provider health fallback reversed:")
