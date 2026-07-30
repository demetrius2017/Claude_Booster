#!/usr/bin/env python3
"""Regression tests for the Codex Stop-hook repair and ask-gate adapter."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REPAIR = ROOT / "scripts/repair_codex_stop_hooks.py"
ADAPTER = ROOT / "templates/scripts/codex_ask_gate.py"
ASK_GATE = ROOT / "templates/scripts/ask_gate.py"
GATE_COMMON = ROOT / "templates/scripts/_gate_common.py"


def _load_repair():
    spec = importlib.util.spec_from_file_location("repair_codex_stop_hooks", REPAIR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_codex_contract(result: subprocess.CompletedProcess[str]) -> dict | None:
    assert result.returncode == 0, result.stderr
    if not result.stdout:
        return None
    assert result.stdout.endswith("\n")
    assert len(result.stdout.strip().splitlines()) == 1
    body = json.loads(result.stdout)
    assert isinstance(body, dict)
    return body


def test_repair_only_changes_codex_stop_hooks() -> None:
    module = _load_repair()
    original = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "/x/on_stop.sh"}]},
                {"hooks": [{"type": "command", "command": "/x/cc-status"}]},
                {"hooks": [
                    {"type": "command", "command": "python /x/ask_gate.py"},
                    {"type": "command", "command": "python /x/memory_session_end.py"},
                ]},
            ],
            "SessionStart": [{"hooks": [{"command": "/x/cc-status"}]}],
        },
        "other": {"preserved": True},
    }
    repaired, summary = module.repair_hooks(original)
    commands = [
        hook["command"]
        for group in repaired["hooks"]["Stop"]
        for hook in group["hooks"]
    ]
    assert commands == [
        "/x/cc-status",
        "python /x/codex_ask_gate.py",
        "python /x/memory_session_end.py",
    ]
    assert repaired["hooks"]["SessionStart"] == original["hooks"]["SessionStart"]
    assert repaired["other"] == original["other"]
    assert summary["removed"] == ["on_stop.sh"]


def test_target_detection_handles_quotes_args_and_argument_lookalikes() -> None:
    module = _load_repair()
    original = {
        "hooks": {
            "Stop": [{"hooks": [
                {"command": '"/path with spaces/on_stop.sh" --notify'},
                {"command": 'python3 "/path with spaces/ask_gate.py" --strict'},
                {"command": '/usr/bin/env python3 "/path with spaces/ask_gate.py" --mode stop'},
                {"command": '/usr/bin/printf "%s" /tmp/on_stop.sh'},
                {"command": 'python3 /safe/worker.py /tmp/ask_gate.py'},
            ]}]
        }
    }
    repaired, summary = module.repair_hooks(original)
    commands = [hook["command"] for hook in repaired["hooks"]["Stop"][0]["hooks"]]
    assert commands == [
        "python3 '/path with spaces/codex_ask_gate.py' --strict",
        "/usr/bin/env python3 '/path with spaces/codex_ask_gate.py' --mode stop",
        '/usr/bin/printf "%s" /tmp/on_stop.sh',
        "python3 /safe/worker.py /tmp/ask_gate.py",
    ]
    assert summary["removed"] == ["on_stop.sh"]
    assert summary["adapted_ask_gate"] == 2


def test_adapter_allow_and_block_contracts() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        temp = Path(temporary)
        scripts = temp / "scripts"
        scripts.mkdir()
        for source in (ADAPTER, ASK_GATE, GATE_COMMON):
            (scripts / source.name).write_bytes(source.read_bytes())

        project = temp / "project"
        project.mkdir()
        home = temp / "home"
        env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(scripts)}

        allow = subprocess.run(
            [sys.executable, str(scripts / ADAPTER.name)],
            input=json.dumps({"cwd": str(project), "messages": [{"role": "assistant", "content": "Done and verified."}]}),
            text=True,
            capture_output=True,
            env=env,
        )
        assert _assert_codex_contract(allow) is None
        assert allow.stderr == ""

        block = subprocess.run(
            [sys.executable, str(scripts / ADAPTER.name)],
            input=json.dumps({"cwd": str(project), "messages": [{"role": "assistant", "content": "Запускаю."}]}),
            text=True,
            capture_output=True,
            env=env,
        )
        body = _assert_codex_contract(block)
        assert body is not None
        assert body["decision"] == "block"
        assert "forbidden stop pattern" in body["reason"]
        assert block.stderr == ""


def test_installer_installs_adapter_then_repairs_idempotently() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        codex = home / ".codex"
        claude = home / ".claude"
        codex.mkdir()
        claude.mkdir()
        settings = claude / "settings.json"
        settings_bytes = b'{"claude_only":true}\n'
        settings.write_bytes(settings_bytes)
        hooks = codex / "hooks.json"
        fixture = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"command": str(claude / "scripts/on_stop.sh")}]},
                    {"hooks": [{"command": f'{sys.executable} "{claude / "scripts/ask_gate.py"}" --strict'}]},
                ],
                "SessionStart": [{"hooks": [{"command": "/user/session-hook"}]}],
            },
            "preserved": {"value": 7},
        }
        hooks.write_text(json.dumps(fixture), encoding="utf-8")
        command = [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(ROOT)!r}); "
                "import install; "
                "raise SystemExit(install.install_codex_bridge(False, False))"
            ),
        ]
        env = {**os.environ, "HOME": str(home)}

        first = subprocess.run(command, text=True, capture_output=True, env=env)
        assert first.returncode == 0, first.stderr
        installed_adapter = claude / "scripts/codex_ask_gate.py"
        assert installed_adapter.read_bytes() == ADAPTER.read_bytes()
        repaired_once = hooks.read_bytes()
        repaired = json.loads(repaired_once)
        assert settings.read_bytes() == settings_bytes
        assert repaired["hooks"]["SessionStart"] == fixture["hooks"]["SessionStart"]
        assert repaired["preserved"] == fixture["preserved"]
        stop_commands = [
            hook["command"]
            for group in repaired["hooks"]["Stop"]
            for hook in group["hooks"]
        ]
        assert len(stop_commands) == 1
        assert "codex_ask_gate.py" in stop_commands[0]
        assert "--strict" in stop_commands[0]

        second = subprocess.run(command, text=True, capture_output=True, env=env)
        assert second.returncode == 0, second.stderr
        assert hooks.read_bytes() == repaired_once
        assert installed_adapter.read_bytes() == ADAPTER.read_bytes()
        assert settings.read_bytes() == settings_bytes


if __name__ == "__main__":
    test_repair_only_changes_codex_stop_hooks()
    test_target_detection_handles_quotes_args_and_argument_lookalikes()
    test_adapter_allow_and_block_contracts()
    test_installer_installs_adapter_then_repairs_idempotently()
    print("4 passed")
