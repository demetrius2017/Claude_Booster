#!/usr/bin/env python3
"""Acceptance tests for bounded capability-aware Codex worker routing."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import fcntl
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "templates/scripts/codex_worker.py"


FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
prompt = sys.stdin.buffer.read()
model = sys.argv[sys.argv.index("-m") + 1]
log = pathlib.Path(os.environ["FAKE_LOG"])
with log.open("a") as f:
    f.write(json.dumps({"model": model, "prompt": prompt.decode(), "argv": sys.argv[1:]}) + "\n")
mode = os.environ.get("FAKE_MODE", "success")
if model == "gpt-5.6-sol" and os.environ.get("FAKE_SOL_SLEEP"):
    time.sleep(float(os.environ["FAKE_SOL_SLEEP"]))
if model == "gpt-5.6-sol" and mode == "unsupported":
    sys.stderr.write('Model metadata for `gpt-5.6-sol` not found. Defaulting to fallback metadata\n')
    sys.stderr.write('■ {"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The \'gpt-5.6-sol\' model is not supported when using Codex with a ChatGPT account."}}\n')
    raise SystemExit(1)
if model == "gpt-5.6-sol" and mode == "metadata":
    sys.stderr.write('Model metadata for `gpt-5.6-sol` not found. Defaulting to fallback metadata\n')
    raise SystemExit(1)
if model == "gpt-5.6-sol" and mode == "appended":
    sys.stderr.write('■ {"status":400,"error":{"type":"invalid_request_error","message":"The \'gpt-5.6-sol\' model is not supported when using Codex with a ChatGPT account. SECRET"}}\n')
    raise SystemExit(1)
if model == "gpt-5.5" and mode == "fallback_fails":
    sys.stderr.write("fallback failed\n")
    raise SystemExit(9)
sys.stdout.buffer.write(b"OUT:" + model.encode() + b":" + prompt)
'''


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    binary = tmp_path / "codex"
    binary.write_text(FAKE)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return {
        **os.environ,
        "CODEX_BIN": str(binary),
        "FAKE_LOG": str(tmp_path / "calls.jsonl"),
        "CLAUDE_BOOSTER_CODEX_CAPABILITY_CACHE": str(tmp_path / "cap.json"),
        "CLAUDE_BOOSTER_CODEX_CAPABILITY_TTL": "3600",
    }


def run(env: dict[str, str], *, managed: bool = True, mode: str = "success") -> subprocess.CompletedProcess[bytes]:
    current = {**env, "FAKE_MODE": mode}
    if managed:
        current.update(CLAUDE_BOOSTER_TASK_CATEGORY="hard", CLAUDE_BOOSTER_ROUTE_SOURCE="balancer")
    return subprocess.run([sys.executable, str(WORKER), "gpt-5.6-sol", "--json"], input=b"same prompt", capture_output=True, env=current)


def calls(env: dict[str, str]) -> list[dict]:
    path = Path(env["FAKE_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_canonical_failure_retries_once_and_caches_without_sensitive_body(env: dict[str, str]) -> None:
    first = run(env, mode="unsupported")
    assert first.returncode == 0
    assert first.stdout == b"OUT:gpt-5.5:same prompt"
    assert [item["model"] for item in calls(env)] == ["gpt-5.6-sol", "gpt-5.5"]
    assert all(item["prompt"] == "same prompt" for item in calls(env))
    cache = Path(env["CLAUDE_BOOSTER_CODEX_CAPABILITY_CACHE"])
    payload = json.loads(cache.read_text())
    assert set(payload) == {"schema_version", "model", "reason", "observed_at", "expires_at"}
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert b'"effective_model":"gpt-5.5"' in first.stderr
    second = run(env, mode="success")
    assert second.returncode == 0
    assert [item["model"] for item in calls(env)][-1] == "gpt-5.5"


def test_metadata_warning_never_triggers_fallback(env: dict[str, str]) -> None:
    result = run(env, mode="metadata")
    assert result.returncode == 1
    assert [item["model"] for item in calls(env)] == ["gpt-5.6-sol"]
    assert not Path(env["CLAUDE_BOOSTER_CODEX_CAPABILITY_CACHE"]).exists()


def test_appended_message_does_not_classify_or_persist(env: dict[str, str]) -> None:
    result = run(env, mode="appended")
    assert result.returncode == 1
    assert [item["model"] for item in calls(env)] == ["gpt-5.6-sol"]
    assert not Path(env["CLAUDE_BOOSTER_CODEX_CAPABILITY_CACHE"]).exists()


@pytest.mark.parametrize("kind", ["world", "symlink", "tampered"])
def test_untrusted_cache_fails_closed(env: dict[str, str], tmp_path: Path, kind: str) -> None:
    cache = Path(env["CLAUDE_BOOSTER_CODEX_CAPABILITY_CACHE"])
    valid = {"schema_version": 1, "model": "gpt-5.6-sol", "reason": "chatgpt_account_unsupported", "observed_at": int(__import__("time").time()), "expires_at": int(__import__("time").time()) + 3600}
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text(json.dumps(valid))
        target.chmod(0o600)
        cache.symlink_to(target)
    else:
        cache.write_text(json.dumps(valid if kind == "world" else {**valid, "reason": "other"}))
        cache.chmod(0o644 if kind == "world" else 0o600)
    result = run(env)
    assert result.returncode == 0
    assert [item["model"] for item in calls(env)] == ["gpt-5.6-sol"]


def test_concurrent_first_calls_single_flight_sol_probe(env: dict[str, str]) -> None:
    current = {**env, "FAKE_MODE": "unsupported", "FAKE_SOL_SLEEP": "0.25", "CLAUDE_BOOSTER_TASK_CATEGORY": "hard", "CLAUDE_BOOSTER_ROUTE_SOURCE": "balancer"}
    command = [sys.executable, str(WORKER), "gpt-5.6-sol"]
    procs = [subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=current) for _ in range(2)]
    results = [proc.communicate(b"same prompt", timeout=10) + (proc.returncode,) for proc in procs]
    assert all(item[2] == 0 for item in results)
    models = [item["model"] for item in calls(env)]
    assert models.count("gpt-5.6-sol") == 1
    assert models.count("gpt-5.5") == 2


def _worker_module():
    spec = importlib.util.spec_from_file_location("capability_worker", WORKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_long_lived_probe_lock_cannot_be_stolen(env: dict[str, str]) -> None:
    cache = Path(env["CLAUDE_BOOSTER_CODEX_CAPABILITY_CACHE"])
    lock = cache.with_name(cache.name + ".probe-lock")
    lock.touch(mode=0o600)
    with lock.open("r+b", buffering=0) as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        module = _worker_module()
        module.LOCK_WAIT_SECONDS = 0.1
        with pytest.raises(TimeoutError):
            module._acquire_probe_lock(cache)
        # The contender neither removed nor replaced the owner's inode/lock.
        assert lock.exists()
        with pytest.raises(BlockingIOError):
            with lock.open("r+b", buffering=0) as third:
                fcntl.flock(third.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_only_owner_release_unlocks_without_removing_lock_file(env: dict[str, str]) -> None:
    cache = Path(env["CLAUDE_BOOSTER_CODEX_CAPABILITY_CACHE"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    module = _worker_module()
    owner = module._acquire_probe_lock(cache)
    lock_path = cache.with_name(cache.name + ".probe-lock")
    assert lock_path.exists()
    module._release_probe_lock(owner)
    successor = module._acquire_probe_lock(cache)
    module._release_probe_lock(successor)
    assert lock_path.exists()


def test_explicit_sol_surfaces_original_failure(env: dict[str, str]) -> None:
    result = run(env, managed=False, mode="unsupported")
    assert result.returncode == 1
    assert [item["model"] for item in calls(env)] == ["gpt-5.6-sol"]
    assert b'"source":"explicit"' in result.stderr


def test_fallback_failure_stops_without_loop(env: dict[str, str]) -> None:
    result = run(env, mode="fallback_fails")
    # First Sol succeeds in this fake mode, so seed the canonical cache explicitly.
    assert result.returncode == 0
    Path(env["FAKE_LOG"]).unlink()
    unsupported = run(env, mode="unsupported")
    assert unsupported.returncode == 0
    Path(env["FAKE_LOG"]).unlink()
    failed = run(env, mode="fallback_fails")
    assert failed.returncode == 9
    assert [item["model"] for item in calls(env)] == ["gpt-5.5"]


def test_expired_cache_recovers_to_preferred_sol(env: dict[str, str]) -> None:
    cache = Path(env["CLAUDE_BOOSTER_CODEX_CAPABILITY_CACHE"])
    cache.write_text(json.dumps({"schema_version": 1, "model": "gpt-5.6-sol", "reason": "chatgpt_account_unsupported", "observed_at": 1, "expires_at": 2}))
    cache.chmod(0o600)
    result = run(env)
    assert result.returncode == 0
    assert [item["model"] for item in calls(env)] == ["gpt-5.6-sol"]
