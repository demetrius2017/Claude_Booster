#!/usr/bin/env python3
"""Contract checks for the fact-bound, non-polling Codex Gantt bridge.

Run:
    python3 tests/test_gantt_contract.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "templates/codex/skills/gantt/SKILL.md"
PROMPT = ROOT / "templates/codex/prompts/gantt.md"
COMMAND = ROOT / "templates/commands/gantt.md"
LEAD_SKILL = ROOT / "templates/codex/skills/lead/SKILL.md"
LEAD_PROMPT = ROOT / "templates/codex/prompts/lead.md"
AGENTS = ROOT / "AGENTS.md"
RUNNER = ROOT / "templates/codex/skills/booster-command/SKILL.md"
README = ROOT / "README.md"
README_RU = ROOT / "README.ru.md"
INSTALL = ROOT / "install.py"
IDENTITY = ["--name", "Test User", "--email", "test@example.invalid"]


def require(text: str, *terms: str) -> None:
    missing = [term for term in terms if term not in text]
    assert not missing, f"missing terms {missing}"


def test_source_surfaces_define_one_fact_bound_command() -> None:
    """Every public source surface names the same Gantt contract."""
    assert SKILL.is_file() and PROMPT.is_file() and COMMAND.is_file()
    require(SKILL.read_text(encoding="utf-8"), 'name: "gantt"', "`gantt`", "update_plan", "list_agents", "Do not poll")
    require(PROMPT.read_text(encoding="utf-8"), "command `gantt`", "update_plan", "list_agents", "неизвестно")
    command = COMMAND.read_text(encoding="utf-8")
    require(
        command,
        "$gantt",
        "`list_agents`",
        "`update_plan`",
        "Дорожка | Done (Сделано) | Now (Сейчас) | Next (Дальше) | State (Состояние)",
        "✅ complete",
        "▶️ active",
        "🟡 at-risk / needs verification",
        "⏸ dependency",
        "⛔ blocked",
        "Слоты:",
        "неизвестно",
        "Do not poll",
        "`wait_agent`",
        "persisted scheduler",
        "worker",
        "reassignment",
        "`followup_task`",
        "completion, failure или blocker",
        "`send_message`",
        "nonblocking",
    )
    require(LEAD_SKILL.read_text(encoding="utf-8"), "$gantt", "worker launch", "reassignment", "completion, failure, or blocker", "not after every tool call", "nonblocking")
    require(LEAD_PROMPT.read_text(encoding="utf-8"), "$gantt", "worker launch", "reassignment", "completion, failure, or blocker", "not per tool call", "send_message")
    require(AGENTS.read_text(encoding="utf-8"), "`gantt`", "$gantt", "/prompts:gantt")
    require(RUNNER.read_text(encoding="utf-8"), "gantt [detail]", "$gantt", "/prompts:gantt")
    require(README.read_text(encoding="utf-8"), "/gantt", "fact-bound Gantt snapshot")
    require(README_RU.read_text(encoding="utf-8"), "/gantt", "Gantt-снимок")


def test_installer_delivers_the_three_gant_artifacts() -> None:
    """A temp-HOME bridge installation carries the exact canonical command."""
    with tempfile.TemporaryDirectory(prefix="booster-gantt-") as home:
        result = subprocess.run(
            [sys.executable, str(INSTALL), "--yes", *IDENTITY],
            cwd=ROOT,
            env={**os.environ, "HOME": home},
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        installed = (
            (Path(home) / ".agents/skills/gantt/SKILL.md", SKILL),
            (Path(home) / ".codex/prompts/gantt.md", PROMPT),
            (Path(home) / ".agents/skills/booster-command/references/commands/gantt.md", COMMAND),
        )
        for destination, source in installed:
            assert destination.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
        manifest = json.loads((Path(home) / ".codex/claude-booster-bridge-manifest.json").read_text(encoding="utf-8"))
        sources = {entry["source"] for entry in manifest["files"]}
        assert {
            "templates/codex/skills/gantt/SKILL.md",
            "templates/codex/prompts/gantt.md",
            "templates/commands/gantt.md",
        } <= sources


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
