"""Offline contract tests for discovery, qualification and guarded activation."""
import copy
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "model_auto_update_contract", Path(__file__).resolve().parents[1]
    / "templates/scripts/model_auto_update.py")
auto = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auto)


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_MODEL_AUTO_UPDATE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("CLAUDE_MODEL_AUTO_UPDATE_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setenv("CLAUDE_MODEL_BALANCER_PATH", str(tmp_path / "balancer.json"))
    # Any accidental real provider invocation is a test failure.
    def forbidden(*args, **kwargs):
        raise AssertionError("unit test attempted a real subprocess")
    monkeypatch.setattr(auto.subprocess, "Popen", forbidden)


def route(model="gpt-6-astra", provider="codex-cli", **extra):
    return dict(provider=provider, model=model, reasoning_effort="high", **extra)


def catalog(model, efforts=("high",), **extra):
    return dict(model=model, supportedReasoningEfforts=[
        {"reasoningEffort": effort} for effort in efforts], **extra)


def passed(*args):
    return {"suite": auto.SUITE, "status": "passed", "checked_at": 123.0}


def receipt(source, target):
    return dict(passed(), target=auto.core(target), accepted_sources=[
        auto.fingerprint(source), auto.fingerprint(target)])


def save(routes):
    auto.atomic(auto.state_path(), {"schema_version": 1, "routes": routes})


def test_discovery_preserves_class_and_requires_supported_effort():
    models = [catalog("gpt-99-sol"), catalog("gpt-7-astra", hidden=True),
              catalog("gpt-8-astra", efforts=("low",)), catalog("gpt-6.1-astra"),
              catalog("gpt-5.9-astra"), catalog("unknown-99", isDefault=True)]
    assert auto.choose_codex(route(), models) == "gpt-6.1-astra"
    assert auto.choose_codex(route(), [catalog("gpt-6-astra"), catalog("gpt-5-astra")]) is None


def test_verified_upgrade_applies_without_mutating_original():
    source, target = route(), route("gpt-7-astra")
    decision = {"routing": {"heavy": source}}
    original = copy.deepcopy(decision)
    state = auto.refresh(decision, discover=lambda: [catalog(target["model"])], probe=passed)
    auto.atomic(auto.state_path(), state)
    applied = auto.apply_verified(decision)
    assert applied["routing"]["heavy"]["model"] == target["model"]
    assert applied["routing"]["heavy"]["auto_update_verified"] is True
    assert decision == original


def test_failed_probe_retains_last_known_good_and_reuses_failure():
    source, current = route(), route("gpt-6.1-astra")
    prior = receipt(source, current)
    save({"heavy": prior})
    calls = []
    def fail(*args):
        calls.append(args)
        raise RuntimeError("qualification_answer_failed")
    state = auto.refresh({"routing": {"heavy": current, "review": current}},
                         discover=lambda: [catalog("gpt-7-astra")], probe=fail)
    assert len(calls) == 1
    assert state["routes"]["heavy"] == prior
    assert "review" not in state["routes"]
    assert all(c["status"] == "failed" for c in state["checks"].values())


def test_pinned_route_never_discovers_or_applies():
    source = route(selection_mode="pinned")
    save({"heavy": receipt(source, route("gpt-7-astra"))})
    def forbidden():
        pytest.fail("pinned route must not discover")
    decision = {"routing": {"heavy": source}}
    state = auto.refresh(decision, discover=forbidden, probe=passed)
    assert state["checks"]["heavy"]["status"] == "held"
    assert auto.apply_verified(decision)["routing"]["heavy"] == source


@pytest.mark.parametrize("replacement", [route("gpt-6-sol"), dict(route(), reasoning_effort="low")])
def test_manual_route_change_invalidates_previous_receipt(replacement):
    save({"heavy": receipt(route(), route("gpt-7-astra"))})
    assert auto.apply_verified({"routing": {"heavy": replacement}})["routing"]["heavy"] == replacement


@pytest.mark.parametrize("target", [route("gpt-8-sol"), route("gpt-5-astra"),
                                    route("claude-opus-5", "anthropic")])
def test_receipt_cannot_cross_class_provider_or_downgrade(target):
    source = route()
    save({"heavy": receipt(source, target)})
    assert auto.apply_verified({"routing": {"heavy": source}})["routing"]["heavy"] == source


@pytest.mark.parametrize("field,value", [("suite", "obsolete-suite"), ("status", "failed"),
                                         ("accepted_sources", []), ("accepted_sources", None),
                                         ("checked_at", None), ("checked_at", float("nan")),
                                         ("checked_at", -1), ("checked_at", 10**15)])
def test_incomplete_or_stale_suite_receipt_is_ignored(field, value):
    source = route()
    stored = receipt(source, route("gpt-7-astra"))
    stored[field] = value
    save({"heavy": stored})
    assert auto.apply_verified({"routing": {"heavy": source}})["routing"]["heavy"] == source


@pytest.mark.parametrize("routes", [None, [], {"heavy": None}, {"heavy": []},
                                    {"heavy": {"target": None}}])
def test_malformed_state_fails_closed(routes):
    source = route()
    save(routes)
    assert auto.apply_verified({"routing": {"heavy": source}})["routing"]["heavy"] == source


def test_broken_json_fails_closed():
    auto.state_path().write_text('{"routes":')
    source = route()
    assert auto.apply_verified({"routing": {"heavy": source}})["routing"]["heavy"] == source


def test_claude_alias_fallback_to_other_class_is_rejected():
    source = route("claude-opus-4-6", "anthropic")
    calls = []
    state = auto.refresh({"routing": {"heavy": source}},
                         resolve=lambda alias: "claude-sonnet-99", probe=lambda *a: calls.append(a))
    assert state["checks"]["heavy"]["reason"] == "candidate_class_mismatch"
    assert state["routes"] == {}
    assert calls == []


def test_claude_alias_resolves_once_and_qualifies_exact_version():
    source = route("claude-opus-4-6", "anthropic")
    aliases, probes = [], []
    def resolve(alias):
        aliases.append(alias)
        return "claude-opus-4-7"
    def probe(*args):
        probes.append(args)
        return passed()
    state = auto.refresh({"routing": {"heavy": source, "review": source}}, resolve=resolve, probe=probe)
    assert aliases == ["opus"]
    assert probes == [("anthropic", "claude-opus-4-7", "high")]
    assert all(r["target"]["model"] == "claude-opus-4-7" for r in state["routes"].values())


def test_probe_pass_from_obsolete_suite_cannot_activate():
    state = auto.refresh({"routing": {"heavy": route()}},
                         discover=lambda: [catalog("gpt-7-astra")],
                         probe=lambda *args: dict(passed(), suite="old-suite"))
    assert state["routes"] == {}


def test_initial_claude_baseline_is_qualified_before_alias_mapping(tmp_path):
    source = route("claude-opus-5", "anthropic")
    calls = []
    def probe(*args):
        calls.append(args)
        return passed()
    decision = {"routing": {"heavy": source}}
    state = auto.refresh(decision, resolve=lambda alias: source["model"], probe=probe)
    assert calls == [("anthropic", "claude-opus-5", "high")]
    assert not (tmp_path / "settings.json").exists()
    auto.publish(state, decision)
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == source["model"]
    assert auto.apply_verified(decision)["routing"]["heavy"]["auto_update_verified"]


def test_failed_initial_baseline_preserves_model_without_verification(tmp_path):
    decision = {"routing": {"heavy": route("claude-opus-5", "anthropic")}}
    state = auto.refresh(decision, resolve=lambda alias: "claude-opus-5",
                         probe=lambda *args: {"status": "failed"})
    auto.publish(state, decision)
    assert auto.read_json(tmp_path / "settings.json")["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-5"
    assert auto.read_json(auto.state_path())["routes"] == {}
    assert not auto.apply_verified(decision)["routing"]["heavy"].get("auto_update_verified")


def test_publish_preserves_user_override_and_holds_receipt(tmp_path):
    source = route("claude-opus-5", "anthropic")
    decision = {"routing": {"heavy": source}}
    settings = {"env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "user-private-model", "KEEP": "value"},
                "permissions": {"allow": ["Read"]}}
    auto.atomic(tmp_path / "settings.json", settings)
    state = auto.refresh(decision, resolve=lambda alias: "claude-opus-6", probe=passed)
    auto.publish(state, decision)
    assert json.loads((tmp_path / "settings.json").read_text()) == settings
    saved = auto.read_json(auto.state_path())
    assert saved["checks"]["heavy"]["reason"] == "alias_mapping_conflict"
    assert saved["routes"] == {}
    assert auto.apply_verified(decision)["routing"]["heavy"] == source


def test_publish_updates_owned_mapping_preserving_other_settings(tmp_path):
    key = "ANTHROPIC_DEFAULT_OPUS_MODEL"
    auto.atomic(auto.state_path(), {"managed_aliases": {key: "claude-opus-5"}})
    auto.atomic(tmp_path / "settings.json", {"env": {key: "claude-opus-5", "KEEP": "x"}, "theme": "dark"})
    decision = {"routing": {"heavy": route("claude-opus-5", "anthropic")}}
    state = auto.refresh(decision, resolve=lambda alias: "claude-opus-6", probe=passed)
    auto.publish(state, decision)
    assert auto.read_json(tmp_path / "settings.json") == {
        "env": {key: "claude-opus-6", "KEEP": "x"}, "theme": "dark"}
    assert auto.read_json(auto.state_path())["managed_aliases"][key] == "claude-opus-6"


def test_same_alias_explicit_pin_prevents_discovery_and_publish(tmp_path):
    source = route("claude-opus-5", "anthropic")
    decision = {"routing": {"heavy": source, "pinned": dict(source, selection_mode="pinned")}}
    def forbidden(*args):
        pytest.fail("same alias pin must hold automatic discovery")
    state = auto.refresh(decision, resolve=forbidden, probe=forbidden)
    assert state["checks"]["heavy"]["reason"] == "class_contains_explicit_pin"
    # Also test a pin inserted while the probe was in flight.
    state["routes"]["heavy"] = receipt(source, route("claude-opus-6", "anthropic"))
    auto.publish(state, decision)
    assert auto.read_json(auto.state_path())["routes"] == {}
    assert auto.read_json(tmp_path / "settings.json")["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-5"
    assert not auto.apply_verified(decision)["routing"]["heavy"].get("auto_update_verified")


@pytest.fixture
def balancer(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "model_auto_update", auto)
    spec = importlib.util.spec_from_file_location("isolated_model_balancer", SPEC.origin.replace(
        "model_auto_update.py", "model_balancer.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert str(module._DB_PATH).startswith(str(tmp_path))
    return module


def test_balancer_preserves_explicit_pin_on_retired_bootstrap(balancer):
    category = "trivial"
    source = dict(balancer._LEGACY_BOOTSTRAP_ROUTES[category][0], selection_mode="pinned")
    result = balancer._with_default_routes({"routing": {category: source}})
    assert result["routing"][category]["model"] == source["model"]
    assert result["routing"][category]["selection_mode"] == "pinned"


@pytest.mark.parametrize("protection", ["verified", "pinned"])
def test_balancer_scorer_cannot_revert_protected_route(balancer, protection):
    source = dict(route("gpt-5.6-luna"), reasoning_effort="low")
    target = dict(route("gpt-6-luna"), reasoning_effort="low")
    save({"trivial": receipt(source, target)})
    if protection == "pinned":
        source["selection_mode"] = "pinned"
    control = {k: v for k, v in source.items() if k != "selection_mode"}
    prior = balancer._with_default_routes({"routing": {"trivial": source, "control": control}})
    balancer._DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(balancer._DB_PATH) as db:
        db.execute("CREATE TABLE model_metrics (task_category TEXT, provider TEXT, model TEXT, per_turn_ms INTEGER, success INTEGER, ts_utc TEXT)")
        for category in ("trivial", "control"):
            db.executemany("INSERT INTO model_metrics VALUES (?, 'anthropic', 'claude-opus-5', 1, 1, datetime('now'))",
                           [(category,)] * 10)
    result = balancer._active_decide(prior)
    assert result["routing"]["trivial"]["model"] == (target if protection == "verified" else source)["model"]
    if protection == "verified":
        assert result["routing"]["trivial"]["auto_update_verified"]
    else:
        assert result["routing"]["trivial"]["selection_mode"] == "pinned"
    # Positive control: real historical metrics reached and drove the old scorer.
    assert result["routing"]["control"]["model"] == "claude-opus-5"


@pytest.mark.parametrize("invalid", [
    {"status": "failed"}, {"suite": "obsolete-suite"},
    {"checked_at": -1}, {"checked_at": True},
    {"accepted_sources": []},
])
def test_invalid_receipt_cannot_publish_new_claude_alias(tmp_path, invalid):
    """An invalid saved receipt cannot change actual Claude execution via env."""
    source = route("claude-opus-5", "anthropic")
    target = route("claude-opus-6", "anthropic")
    bad = dict(receipt(source, target), **invalid)
    state = {"routes": {"heavy": bad}, "checks": {}}
    auto.publish(state, {"routing": {"heavy": source}})
    settings = auto.read_json(tmp_path / "settings.json")
    # Publishing the unchanged historical baseline is allowed; publishing the
    # failed/new version is not, even if activation would separately reject it.
    assert settings.get("env", {}).get("ANTHROPIC_DEFAULT_OPUS_MODEL") in (None, source["model"])
    applied = auto.apply_verified({"routing": {"heavy": source}})["routing"]["heavy"]
    assert applied["model"] == source["model"]
    assert not applied.get("auto_update_verified")


@pytest.mark.parametrize("invalid", [{"status": "failed"}, {"suite": "obsolete-suite"}])
def test_stale_receipt_requalifies_unchanged_claude_baseline(invalid):
    source = route("claude-opus-5", "anthropic")
    save({"heavy": dict(receipt(source, source), **invalid)})
    calls = []
    def probe(*args):
        calls.append(args)
        return passed()
    state = auto.refresh({"routing": {"heavy": source}},
                         resolve=lambda alias: source["model"], probe=probe)
    assert calls == [("anthropic", source["model"], "high")]
    assert auto.valid_receipt(source, state["routes"]["heavy"])
    assert state["routes"]["heavy"]["suite"] == auto.SUITE


def test_launch_is_async_daily_and_recursion_safe(monkeypatch):
    calls = []
    monkeypatch.setattr(auto.subprocess, 'Popen', lambda *args, **kwargs: calls.append((args, kwargs)))
    auto.launch()
    assert len(calls) == 1
    assert calls[0][0][0][-1] == 'refresh'
    assert calls[0][1]['start_new_session'] is True
    auto.atomic(auto.state_path(), {'checked_at': auto.time.time()})
    auto.launch()
    assert len(calls) == 1
    auto.atomic(auto.state_path(), {})
    monkeypatch.setenv('CLAUDE_MODEL_AUTO_UPDATE_CHILD', '1')
    auto.launch()
    assert len(calls) == 1


def test_manual_model_edit_cannot_keep_verified_marker():
    source, target = route(), route('gpt-7-astra')
    save({'heavy': receipt(source, target)})
    manual = dict(source, model='gpt-8-astra', auto_update_verified=True)
    result = auto.apply_verified({'routing': {'heavy': manual}})['routing']['heavy']
    assert result['model'] == 'gpt-8-astra'
    assert 'auto_update_verified' not in result
