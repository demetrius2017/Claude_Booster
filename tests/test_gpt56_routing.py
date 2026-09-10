#!/usr/bin/env python3
"""Verify Codex route defaults, migration, effort, and live policy contracts."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from datetime import date, timedelta
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
        "consilium_bio": ("gpt-6-astra", "medium"),
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
        "audit_secondary": "glm-5.3",
        "audit_tertiary": "grok-4.6",
        "hackathon_external": "glm-5.3",
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
    canonical_consilium_sol = {
        "provider": "codex-cli",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
    }
    assert canonical_consilium_sol in balancer._LEGACY_BOOTSTRAP_ROUTES["consilium_bio"]
    migrated_consilium = balancer._with_default_routes({
        "routing": {"consilium_bio": canonical_consilium_sol},
    })["routing"]["consilium_bio"]
    assert migrated_consilium == {
        "provider": "codex-cli",
        "model": "gpt-6-astra",
        "reasoning_effort": "medium",
    }

    # The migration is deliberately exact: any extra operator-owned field
    # makes the same Sol provider/model route a custom override.
    custom_consilium_sol = dict(canonical_consilium_sol, operator="keep-sol")
    preserved_consilium = balancer._with_default_routes({
        "routing": {"consilium_bio": custom_consilium_sol},
    })["routing"]["consilium_bio"]
    assert preserved_consilium == custom_consilium_sol
    assert {"provider": "codex-cli", "model": "gpt-5.6-terra"} in balancer._LEGACY_BOOTSTRAP_ROUTES["coding"]
    assert {"provider": "pal", "model": "gpt-5.5"} in balancer._LEGACY_BOOTSTRAP_ROUTES["audit_external"]
    assert {"provider": "zai-cli", "model": "glm-5.1"} in balancer._LEGACY_BOOTSTRAP_ROUTES["audit_secondary"]
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
        "audit_secondary": {"provider": "zai-cli", "model": "glm-5.1"},
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
    assert persisted["routing"]["audit_secondary"]["model"] == "glm-5.3"
    assert persisted["routing"]["audit_tertiary"]["model"] == "grok-4.6"
    assert persisted["routing"]["coding"] == custom

    # Safety pin: hooks must keep firing on high-blast-radius work.
    assert balancer.DEFAULTS["routing"]["high_blast_radius"]["model"] == "claude-sonnet-5"
    assert balancer._QUALITY_SCORES_ANTHROPIC["claude-opus-5"] == 20
    for category in expected_opus:
        assert category in balancer._PINNED_CATEGORIES, category
    assert "consilium_bio" in balancer._PINNED_CATEGORIES

    # A fresh next-day scoring pass must not even query consilium_bio metrics,
    # so arbitrarily fast historical Sol samples cannot resurrect that route.
    hostile_metric_queries: list[str] = []
    hostile_prior = balancer._with_default_routes({
        "decision_date": (date.today() - timedelta(days=1)).isoformat(),
        "routing": {},
    })
    original_globals = {
        "_DB_PATH": balancer._DB_PATH,
        "_get_weekly_max_pct": balancer._get_weekly_max_pct,
        "_get_codex_quota_pct": balancer._get_codex_quota_pct,
        "_query_provider_failure_events": balancer._query_provider_failure_events,
        "_query_provider_health": balancer._query_provider_health,
        "_query_metrics": balancer._query_metrics,
    }
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "hostile.sqlite"
        db_path.touch()

        def hostile_metrics(category: str, _db_path: Path):
            hostile_metric_queries.append(category)
            if category == "consilium_bio":
                return [
                    {
                        "provider": "codex-cli",
                        "model": "gpt-5.6-sol",
                        "per_turn_ms": 1,
                        "success": 1,
                    }
                ] * balancer.MIN_SAMPLES
            return []

        try:
            balancer._DB_PATH = db_path
            balancer._get_weekly_max_pct = lambda _prior: 0.0
            balancer._get_codex_quota_pct = lambda _prior: 0.0
            balancer._query_provider_failure_events = lambda: {}
            balancer._query_provider_health = lambda _path: {}
            balancer._query_metrics = hostile_metrics
            hostile_decision = balancer._active_decide(hostile_prior)
        finally:
            for name, value in original_globals.items():
                setattr(balancer, name, value)

    assert "consilium_bio" not in hostile_metric_queries
    assert hostile_decision["routing"]["consilium_bio"] == {
        "provider": "codex-cli",
        "model": "gpt-6-astra",
        "reasoning_effort": "medium",
    }

    assert balancer.DEFAULTS["routing"]["high_blast_radius"]["provider"] == "anthropic"
    for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
        assert capture._match_codex_command(f"codex exec -m {model} -") == model

    go = (ROOT / "templates/commands/go.md").read_text()
    skill = (ROOT / "templates/codex/skills/booster-command/SKILL.md").read_text()
    assert "Sol, Terra, and Luna are all OpenAI/Codex" in go
    assert "never select `xhigh` automatically" in skill
    assert "CODEX_REASONING_EFFORT" in go
    print("PASS: Codex routes and effort contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
