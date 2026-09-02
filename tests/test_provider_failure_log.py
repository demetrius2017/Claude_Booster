#!/usr/bin/env python3
"""Regression tests for the shared provider failure/success event sidecar.

These tests never touch the real ``~/.claude`` log: every case redirects
``CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG`` at a tmp_path file.
"""
from __future__ import annotations

import importlib
import json
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "templates" / "scripts"


def _import_script(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def _module(monkeypatch, tmp_path: Path):
    log = tmp_path / "nested" / "events.jsonl"
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG", str(log))
    monkeypatch.delenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_MAX_BYTES", raising=False)
    monkeypatch.delenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_RETAIN_LINES", raising=False)
    return _import_script("provider_failure_log"), log


def test_append_creates_parent_and_private_mode(monkeypatch, tmp_path) -> None:
    module, log = _module(monkeypatch, tmp_path)

    module.append_provider_event({"provider": "grok-cli", "n": 1}, log_label="grok_cli")

    assert log.exists()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"n": 1, "provider": "grok-cli"}]


def test_env_path_override_is_honoured(monkeypatch, tmp_path) -> None:
    module, log = _module(monkeypatch, tmp_path)

    assert module.failure_log_path() == log
    assert module.failure_log_path() != module.DEFAULT_FAILURE_LOG_PATH


def test_event_bounds_read_env_with_floors(monkeypatch, tmp_path) -> None:
    module, _ = _module(monkeypatch, tmp_path)

    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_MAX_BYTES", "9000")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_RETAIN_LINES", "42")
    assert module.event_bounds() == (9000, 42)

    # Below-floor and unparsable values fall back to safe minimums.
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_MAX_BYTES", "10")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_RETAIN_LINES", "1")
    assert module.event_bounds() == (4096, 10)

    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_MAX_BYTES", "not-a-number")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_RETAIN_LINES", "not-a-number")
    assert module.event_bounds() == (
        module.DEFAULT_FAILURE_LOG_MAX_BYTES,
        module.DEFAULT_FAILURE_LOG_RETAIN_LINES,
    )


def test_log_is_rotated_within_the_byte_bound(monkeypatch, tmp_path) -> None:
    module, log = _module(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_MAX_BYTES", "4096")
    monkeypatch.setenv("CLAUDE_BOOSTER_PROVIDER_EVENTS_RETAIN_LINES", "10")

    for index in range(80):
        module.append_provider_event(
            {"seq": index, "pad": "x" * 100}, log_label="grok_cli"
        )

    raw = log.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    assert len(raw) <= 4096
    # Rotation drops the oldest lines and always keeps the newest event.
    assert rows[-1]["seq"] == 79
    assert len(rows) < 80


def test_empty_log_label_raises_value_error(monkeypatch, tmp_path) -> None:
    module, log = _module(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="log_label must be non-empty"):
        module.append_provider_event({"provider": "grok-cli"}, log_label="   ")
    assert not log.exists()


def test_oserror_is_swallowed_and_reported(monkeypatch, tmp_path, capsys) -> None:
    module, _ = _module(monkeypatch, tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(module.os, "open", _boom)

    module.append_provider_event({"provider": "grok-cli"}, log_label="grok_cli")

    err = capsys.readouterr().err
    assert "grok_cli: provider-event telemetry skipped" in err
    assert "read-only file system" in err
