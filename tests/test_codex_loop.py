"""Black-box lifecycle regressions for the bounded Codex loop helper."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "templates" / "scripts" / "codex_loop.py"


def _fixture(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, dict[str, str], Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    state_home = tmp_path / "state"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "codex-argv.jsonl"
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CODEX_LOG'], 'a', encoding='utf-8') as out:\n"
        "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "raise SystemExit(int(os.environ.get('FAKE_CODEX_EXIT', '0')))\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(state_home),
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "FAKE_CODEX_LOG": str(log),
        "FAKE_CODEX_EXIT": str(exit_code),
    }
    return project, env, state_home, log


def _run(env: dict[str, str], *args: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(LOOP), *args], env=env, text=True, capture_output=True, timeout=timeout)


def _status(env: dict[str, str], project: Path) -> dict[str, object]:
    result = _run(env, "status", "--cwd", str(project))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _wait_for(env: dict[str, str], project: Path, statuses: set[str], timeout: float = 5) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _status(env, project)
        if state["status"] in statuses:
            return state
        time.sleep(0.05)
    raise AssertionError(f"loop did not reach {statuses}: {_status(env, project)}")


@pytest.mark.parametrize("interval", ["0s", "01s", "1", "1d", "366d", "1.5s"])
def test_start_rejects_invalid_intervals(tmp_path: Path, interval: str) -> None:
    project, env, _, log = _fixture(tmp_path)
    result = _run(env, "start", interval, "--thread", "thread-1", "--cwd", str(project))
    assert result.returncode != 0
    assert "interval" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
def test_start_rejects_non_finite_queue_timeout_without_state_or_spawn(tmp_path: Path, timeout: str) -> None:
    project, env, state_home, log = _fixture(tmp_path)
    result = _run(
        env, "start", "1s", "--thread", "thread-timeout", f"--queue-timeout={timeout}", "--cwd", str(project)
    )
    assert result.returncode != 0
    assert "queue timeout" in result.stderr
    assert not log.exists()
    assert not (state_home / "claude-booster" / "loop").exists()


def test_explicit_thread_no_wake_before_deadline_and_exact_argv(tmp_path: Path) -> None:
    project, env, _, log = _fixture(tmp_path)
    message = "keep $HOME; 'quoted' ; && | < > * ? [x]"
    started = _run(env, "start", "1s", "--thread", "thread-123", "--message", message, "--max-wakes", "1", "--cwd", str(project))
    assert started.returncode == 0, started.stderr
    time.sleep(0.2)
    assert not log.exists(), "scheduler queued before the first deadline"
    terminal = _wait_for(env, project, {"completed"})
    assert terminal["attempts"] == 1
    assert [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] == [
        ["queue", "--thread", "thread-123", "--message", message]
    ]


def test_max_wakes_is_terminal_and_queue_failure_does_not_retry(tmp_path: Path) -> None:
    project, env, _, log = _fixture(tmp_path, exit_code=23)
    started = _run(env, "start", "1s", "--thread", "thread-fail", "--max-wakes", "3", "--cwd", str(project))
    assert started.returncode == 0, started.stderr
    terminal = _wait_for(env, project, {"failed"})
    assert terminal["attempts"] == 1
    assert "queue failed exit=23" in str(terminal["detail"])
    time.sleep(1.1)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_stop_during_sleep_prevents_wake_and_marks_stopped(tmp_path: Path) -> None:
    project, env, _, log = _fixture(tmp_path)
    assert _run(env, "start", "2s", "--thread", "thread-stop", "--cwd", str(project)).returncode == 0
    stopped = _run(env, "stop", "--cwd", str(project))
    assert stopped.returncode == 0, stopped.stderr
    state = _wait_for(env, project, {"stopped"})
    assert state["stop_requested"] is True
    time.sleep(0.2)
    assert not log.exists()


def test_stop_recovers_terminal_state_when_recorded_scheduler_is_dead(tmp_path: Path) -> None:
    project, env, state_home, _ = _fixture(tmp_path)
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    assert exited.wait() == 0
    state_dir = state_home / "claude-booster" / "loop" / hashlib.sha256(
        str(project.resolve()).encode("utf-8")
    ).hexdigest()
    state_dir.mkdir(parents=True, mode=0o700)
    state_path = state_dir / "state.json"
    state_path.write_text(json.dumps({
        "schema": 1, "project": str(project.resolve()), "status": "active", "pid": exited.pid,
        "thread": "thread-dead", "message": "wake", "interval_s": 60.0, "max_wakes": 1,
        "queue_timeout_s": 120.0, "attempts": 0, "stop_requested": False,
        "updated_at": "2026-09-03T00:00:00Z", "detail": "",
    }), encoding="utf-8")
    state_path.chmod(0o600)

    stopped = _run(env, "stop", "--cwd", str(project))

    assert stopped.returncode == 0, stopped.stderr
    state = json.loads(stopped.stdout)
    assert state["status"] == "stopped"
    assert state["stop_requested"] is True
    assert state["detail"] == f"stop recovered: recorded scheduler pid {exited.pid} is not alive"


def test_state_is_private_outside_project_and_status_never_invokes_codex(tmp_path: Path) -> None:
    project, env, state_home, log = _fixture(tmp_path)
    absent = _status(env, project)
    assert absent["status"] == "absent"
    assert not log.exists()
    assert _run(env, "start", "2s", "--thread", "thread-private", "--cwd", str(project)).returncode == 0
    state = _wait_for(env, project, {"active"})
    state_files = list((state_home / "claude-booster" / "loop").glob("*/state.json"))
    assert len(state_files) == 1
    state_file = state_files[0]
    assert project not in state_file.parents
    assert stat.S_IMODE(state_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert state["project"] == str(project.resolve())
    assert _run(env, "stop", "--cwd", str(project)).returncode == 0
    _wait_for(env, project, {"stopped"})


def test_duplicate_start_is_rejected_while_scheduler_is_live(tmp_path: Path) -> None:
    project, env, _, _ = _fixture(tmp_path)
    assert _run(env, "start", "2s", "--thread", "thread-first", "--cwd", str(project)).returncode == 0
    duplicate = _run(env, "start", "2s", "--thread", "thread-second", "--cwd", str(project))
    assert duplicate.returncode == 1
    assert "scheduler" in duplicate.stderr
    assert _run(env, "stop", "--cwd", str(project)).returncode == 0
    _wait_for(env, project, {"stopped"})
