#!/usr/bin/env python3
"""Verify GPT-5.6 route defaults, migration, effort, and live policy contracts."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(path: str):
    spec = importlib.util.spec_from_file_location("subject", ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    balancer = load("templates/scripts/model_balancer.py")
    capture = load("templates/scripts/model_metric_capture.py")
    # Codex lanes: cheap recon/medium work stays on the flat-fee provider.
    expected_codex = {
        "trivial": ("gpt-5.6-luna", "low"),
        "recon": ("gpt-5.6-luna", "low"),
        "medium": ("gpt-5.6-terra", "medium"),
        "consilium_bio": ("gpt-5.6-sol", "medium"),
    }
    for category, (model, effort) in expected_codex.items():
        route = balancer.DEFAULTS["routing"][category]
        assert route["provider"] == "codex-cli"
        assert (route["model"], route["reasoning_effort"]) == (model, effort)

    # Opus 5 lanes (user decision 2026-07-25): Lead and all heavy work on Claude.
    expected_opus = ("lead", "coding", "hard")
    for category in expected_opus:
        route = balancer.DEFAULTS["routing"][category]
        assert route["provider"] == "anthropic", category
        assert route["model"] == "claude-opus-5", category
        # reasoning_effort is Codex-only — it must never ride along on an
        # Anthropic route, including after migration off Codex.
        assert "reasoning_effort" not in route, category

    expected_models = {c: m for c, (m, _) in expected_codex.items()}
    expected_models.update({c: "claude-opus-5" for c in expected_opus})
    expected_models.update({
        "audit_external": "gpt-5.6-sol",
        "audit_secondary": "glm-5.1",
        "audit_tertiary": "grok-4.6",
        "hackathon_external": "glm-5.1",
        "hackathon_coder": "grok-4.6",
        "high_blast_radius": "claude-sonnet-5",
    })

    # Every retired generation listed for a category must migrate to the current
    # default. This is the guard against the silent pin no-op: DEFAULTS changed
    # but an installed JSON on an unlisted older route never moves.
    for category, retired_routes in balancer._LEGACY_BOOTSTRAP_ROUTES.items():
        if category not in expected_models:
            continue
        for retired in retired_routes:
            stale = {"routing": {category: dict(retired, reasoning_effort="medium")}}
            migrated = balancer._with_default_routes(stale)["routing"][category]
            assert migrated["model"] == expected_models[category], (category, retired)
            if migrated["provider"] != "codex-cli":
                assert "reasoning_effort" not in migrated, (category, retired)

    # The installed routes this change is migrating away from must be listed as
    # retired, or the live install silently keeps Codex on these categories.
    assert {"provider": "codex-cli", "model": "gpt-5.6-sol"} in balancer._LEGACY_BOOTSTRAP_ROUTES["lead"]
    assert {"provider": "codex-cli", "model": "gpt-5.6-sol"} in balancer._LEGACY_BOOTSTRAP_ROUTES["hard"]
    assert {"provider": "codex-cli", "model": "gpt-5.6-terra"} in balancer._LEGACY_BOOTSTRAP_ROUTES["coding"]
    assert {"provider": "pal", "model": "gpt-5.5"} in balancer._LEGACY_BOOTSTRAP_ROUTES["audit_external"]
    assert {"provider": "zai-cli", "model": "glm-5.2"} in balancer._LEGACY_BOOTSTRAP_ROUTES["audit_secondary"]
    assert {"provider": "grok-cli", "model": "grok-4.5"} in balancer._LEGACY_BOOTSTRAP_ROUTES["audit_tertiary"]
    assert {"provider": "anthropic", "model": "claude-sonnet-4-6"} in balancer._LEGACY_BOOTSTRAP_ROUTES["high_blast_radius"]

    # A route that matches no retired generation is a deliberate override.
    custom = {"provider": "codex-cli", "model": "custom-model", "note": "keep"}
    preserved = balancer._with_default_routes({"routing": {"coding": custom}})
    assert preserved["routing"]["coding"] == custom

    # Migration must persist through the public decision path, not merely alter
    # an in-memory dict. Historical provider routes upgrade while a deliberate
    # custom override remains untouched after reopening the JSON file.
    stale_routes = {
        "high_blast_radius": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "audit_external": {"provider": "pal", "model": "gpt-5.5"},
        "audit_secondary": {"provider": "zai-cli", "model": "glm-5.2"},
        "audit_tertiary": {"provider": "grok-cli", "model": "grok-4.5"},
        "coding": custom,
    }
    with tempfile.TemporaryDirectory() as directory:
        route_path = Path(directory) / "model_balancer.json"
        route_path.write_text(json.dumps({
            "decision_date": balancer._today_utc(), "routing": stale_routes,
        }), encoding="utf-8")
        original_path, original_cache = balancer._BALANCER_PATH, balancer._cached_decision
        try:
            balancer._BALANCER_PATH = route_path
            balancer._cached_decision = None
            balancer.decide()
            persisted = json.loads(route_path.read_text(encoding="utf-8"))
        finally:
            balancer._BALANCER_PATH = original_path
            balancer._cached_decision = original_cache
    assert persisted["routing"]["high_blast_radius"]["model"] == "claude-sonnet-5"
    assert persisted["routing"]["audit_external"]["model"] == "gpt-5.6-sol"
    assert persisted["routing"]["audit_secondary"]["model"] == "glm-5.1"
    assert persisted["routing"]["audit_tertiary"]["model"] == "grok-4.6"
    assert persisted["routing"]["coding"] == custom

    # Safety pin: hooks must keep firing on high-blast-radius work.
    assert balancer.DEFAULTS["routing"]["high_blast_radius"]["model"] == "claude-sonnet-5"
    assert balancer._QUALITY_SCORES_ANTHROPIC["claude-opus-5"] == 20
    for category in expected_opus:
        assert category in balancer._PINNED_CATEGORIES, category

    assert balancer.DEFAULTS["routing"]["high_blast_radius"]["provider"] == "anthropic"
    for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
        assert capture._match_codex_command(f"codex exec -m {model} -") == model

    go = (ROOT / "templates/commands/go.md").read_text()
    skill = (ROOT / "templates/codex/skills/booster-command/SKILL.md").read_text()
    assert "Sol, Terra, and Luna are all OpenAI/Codex" in go
    assert "never select `xhigh` automatically" in skill
    assert "CODEX_REASONING_EFFORT" in go
    print("PASS: GPT-5.6 routes and effort contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
