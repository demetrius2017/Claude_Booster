#!/usr/bin/env python3
"""Contract checks for the non-polling Codex agent-status bridge surface.

Run:
    python3 tests/test_agent_status_contract.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "templates/codex/skills/agent-status/SKILL.md"
PROMPT = ROOT / "templates/codex/prompts/agent-status.md"
COMMAND = ROOT / "templates/commands/agent-status.md"
LEAD_SKILL = ROOT / "templates/codex/skills/lead/SKILL.md"
LEAD_PROMPT = ROOT / "templates/codex/prompts/lead.md"
INSTALL = ROOT / "install.py"
IDENTITY = ["--name", "Test User", "--email", "test@example.invalid"]


def require(text: str, *terms: str) -> None:
    missing = [term for term in terms if term not in text]
    assert not missing, f"missing terms {missing}"


def normalize_agent_rows(rows: list[dict[str, object]]) -> tuple[dict[str, int], list[str]]:
    """Model the documented list_agents contract for the command fixture."""
    counts = {"active": 0, "done": 0, "other": 0}
    rendered: list[str] = []
    for row in rows:
        name = row.get("agent_name")
        if name == "/root":
            continue
        status = row.get("agent_status")
        if status == "running":
            counts["active"] += 1
            rendered.append(f"{name} — running")
        elif isinstance(status, dict) and isinstance(status.get("completed"), str):
            counts["done"] += 1
            summary = status["completed"][:160]
            rendered.append(f"{name} — completed: {summary}")
        else:
            counts["other"] += 1
            rendered.append(f"{name} — {status!r}")
    return counts, rendered


def test_aliases_have_a_complete_command_pairing() -> None:
    """The installer validator sees a same-named skill, prompt, and spec."""
    assert SKILL.is_file() and PROMPT.is_file() and COMMAND.is_file()
    require(SKILL.read_text(encoding="utf-8"), 'name: "agent-status"', "`agent-status`", "agent_name", 'agent_name: "/root"')
    require(PROMPT.read_text(encoding="utf-8"), "command `agent-status`", "agent_name", "agent_status")
    require(COMMAND.read_text(encoding="utf-8"), "$agent-status", "list_agents", "top-level Codex slash command", "agent_name", "agent_status", "160 characters")


def test_list_agents_fixture_excludes_root_and_normalizes_actual_status_shape() -> None:
    """Root is not a child; only known native status shapes drive counts."""
    long_summary = "x" * 200
    counts, rendered = normalize_agent_rows([
        {"agent_name": "/root", "agent_status": "running"},
        {"agent_name": "/root/recon", "agent_status": "running"},
        {"agent_name": "/root/verify", "agent_status": {"completed": long_summary}},
        {"agent_name": "/root/odd", "agent_status": {"paused": "unknown"}},
    ])
    assert counts == {"active": 1, "done": 1, "other": 1}
    assert all("/root —" not in line for line in rendered)
    assert rendered[0] == "/root/recon — running"
    assert rendered[1] == "/root/verify — completed: " + ("x" * 160)
    assert rendered[2] == "/root/odd — {'paused': 'unknown'}"


def test_non_polling_and_native_ui_boundaries_are_explicit() -> None:
    """Reject misleading advice that causes repeated waiting or UI promises."""
    command = COMMAND.read_text(encoding="utf-8")
    lead = LEAD_SKILL.read_text(encoding="utf-8") + LEAD_PROMPT.read_text(encoding="utf-8")
    require(command, "Do not call `wait_agent`", "No agents completed yet", "Do not claim that Booster can suppress, remove, or modify")
    require(lead, "preserve every task path", "`list_agents` snapshots", "`send_message`", "`followup_task`", "30–60 seconds", "Never repeatedly")
    assert "suppress native UI" not in command.lower()


def test_installer_delivers_the_three_agent_status_artifacts() -> None:
    """The source contract survives a clean bridge installation unchanged."""
    with tempfile.TemporaryDirectory(prefix="booster-agent-status-") as home:
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
            (Path(home) / ".agents/skills/agent-status/SKILL.md", SKILL),
            (Path(home) / ".codex/prompts/agent-status.md", PROMPT),
            (Path(home) / ".agents/skills/booster-command/references/commands/agent-status.md", COMMAND),
        )
        for destination, source in installed:
            assert destination.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
        manifest = json.loads((Path(home) / ".codex/claude-booster-bridge-manifest.json").read_text(encoding="utf-8"))
        sources = {entry["source"] for entry in manifest["files"]}
        assert "templates/commands/agent-status.md" in sources


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
