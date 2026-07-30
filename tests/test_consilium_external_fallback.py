#!/usr/bin/env python3
"""Executable contract tests for consilium external-review degradation."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMMAND = (ROOT / "templates/commands/consilium.md").read_text(encoding="utf-8")
CODEX = (
    ROOT / "templates/codex/skills/booster-command/SKILL.md"
).read_text(encoding="utf-8")


def _flat(contract: str) -> str:
    return " ".join(contract.split())


def test_pal_runtime_failures_are_unavailable_not_opinions() -> None:
    for contract in (COMMAND, CODEX):
        contract = _flat(contract)
        for marker in (
            "429 insufficient_quota",
            "`401`",
            "`403`",
            "`5xx`",
            "timeout",
            "tool exception",
        ):
            assert marker in contract
        assert "usable opinion" in contract


def test_fallback_order_and_independence_label_are_explicit() -> None:
    expected = "Z.ai → Grok → Codex native second opinion"
    for contract in (COMMAND, CODEX):
        assert expected in _flat(contract)
        assert "degraded_external_independence" in contract


def test_successful_pal_remains_primary_and_grok_model_is_supported() -> None:
    for contract in (COMMAND, CODEX):
        assert "successful PAL" in contract
        assert "--model grok-4.5" in contract
    for relative in (
        "templates/commands/go.md",
        "templates/commands/hackathon.md",
    ):
        consumer = (ROOT / relative).read_text(encoding="utf-8")
        assert "grok_sandbox_worker.sh grok-4.5" in consumer
        assert "grok_sandbox_worker.sh grok-build" not in consumer
