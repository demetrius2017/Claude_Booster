#!/usr/bin/env python3
"""Shared pytest isolation for Claude Booster tests.

Purpose
    Keep provider telemetry written by tests away from the developer's real
    ``~/.claude`` state. ``provider_failure_log.py`` defaults to
    ``~/.claude/logs/model_provider_failures.jsonl`` and the CLI wrappers default
    the metrics DB to ``~/.claude/rolling_memory.db``; a test that exercises a
    failure path without overriding those env vars both pollutes the real files
    and makes assertions depend on machine state.

Contract
    Autouse fixture, applied to every test collected under ``tests/``. It points
    ``CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG`` and ``CLAUDE_BOOSTER_METRICS_DB`` at
    per-test paths inside pytest's ``tmp_path``. Individual tests remain free to
    override either variable with their own value.

Limitations
    Only affects in-process pytest tests. Tests that spawn subprocesses must pass
    the isolated paths explicitly in the child environment.

ENV/Files
    Sets CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG, CLAUDE_BOOSTER_METRICS_DB.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_provider_telemetry(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "CLAUDE_BOOSTER_PROVIDER_FAILURES_LOG",
        str(tmp_path / "provider_failures.jsonl"),
    )
    monkeypatch.setenv("CLAUDE_BOOSTER_METRICS_DB", str(tmp_path / "metrics.db"))
    yield
