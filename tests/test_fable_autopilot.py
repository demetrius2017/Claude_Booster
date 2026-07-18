#!/usr/bin/env python3
"""Executable acceptance contract for Fable autopilot.

The test intentionally treats the hook as a black box: it supplies Claude
PreToolUse JSON on stdin and checks only the documented permission decision.
No live Fable call is made.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import re
import importlib.util
import typing
import threading


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "templates/settings.json.template"
HOOK = ROOT / "templates/scripts/fable_autopilot.py"
COMMAND = ROOT / "templates/commands/autopilot.md"
CODEX_SKILL = ROOT / "templates/codex/skills/autopilot/SKILL.md"
CODEX_RUNNER = ROOT / "templates/codex/skills/booster-command/SKILL.md"
AGENTS = ROOT / "AGENTS.md"
STATE_TOOL = ROOT / "templates/scripts/fable_autopilot_state.py"


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS {label}")


def render_settings() -> dict:
    text = SETTINGS.read_text(encoding="utf-8")
    replacements = {
        "${PYTHON}": "python3",
        "${CLAUDE_HOME}": "/tmp/claude",
        "${BOOSTER_VERSION}": "acceptance",
        "${INSTALLED_AT}": "2026-07-17T00:00:00Z",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return json.loads(text)


def commands_for(settings: dict, event: str, matcher: str | None = None) -> list[str]:
    result: list[str] = []
    for block in settings.get("hooks", {}).get(event, []):
        if matcher is not None and block.get("matcher") != matcher:
            continue
        result.extend(h.get("command", "") for h in block.get("hooks", []))
    return result


def run_hook(payload: dict, home: Path) -> tuple[int, dict, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CLAUDE_HOME"] = str(home / ".claude")
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=False,
    )
    try:
        body = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise AssertionError(f"hook emitted invalid JSON: {proc.stdout!r}") from exc
    return proc.returncode, body, proc.stderr


def decision(rc: int, body: dict, stderr: str) -> tuple[str, str]:
    specific = body.get("hookSpecificOutput", body)
    explicit = str(specific.get("permissionDecision", specific.get("decision", ""))).lower()
    # Claude hooks support both structured permission decisions and the older,
    # still-valid exit-2 deny protocol. The latter carries its instruction on
    # stderr and must not be mistaken for a hook crash.
    if explicit:
        return explicit, str(specific.get("permissionDecisionReason", specific.get("reason", "")))
    return ("deny", stderr) if rc == 2 else ("allow", stderr)


def main() -> int:
    for path in (SETTINGS, HOOK, COMMAND, CODEX_SKILL, CODEX_RUNNER, AGENTS, STATE_TOOL):
        check(path.is_file() and path.stat().st_size > 0, f"artifact exists: {path.relative_to(ROOT)}")
    compile_result = subprocess.run([sys.executable, "-m", "py_compile", str(HOOK), str(STATE_TOOL)], capture_output=True, text=True, check=False)
    check(compile_result.returncode == 0, f"autopilot Python modules compile ({compile_result.stderr.strip()})")
    sys.path.insert(0, str(HOOK.parent))
    spec = importlib.util.spec_from_file_location("autopilot_acceptance_module", HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    check(bool(typing.get_type_hints(module._load_state)), "runtime type hints resolve Optional and imported annotations")

    settings = render_settings()
    pre = commands_for(settings, "PreToolUse", "AskUserQuestion")
    stop = commands_for(settings, "Stop")
    check(any("fable_autopilot.py" in cmd for cmd in pre), "AskUserQuestion is intercepted at PreToolUse")
    check(any("fable_autopilot.py" in cmd for cmd in stop), "plain-text Stop fallback is wired")
    settings_text = SETTINGS.read_text(encoding="utf-8")
    check("phase-event plan_complete" in settings_text and "phase-event final_diff" in settings_text, "automatic checkpoint triggers are wired to plan and final events")
    check("phase-event first_slice" not in settings_text, "automatic checkpoint lane is bounded to two calls")

    command_text = COMMAND.read_text(encoding="utf-8").lower()
    skill_text = CODEX_SKILL.read_text(encoding="utf-8").lower()
    combined = command_text + "\n" + skill_text
    for term, label in (
        ("north star", "state exposes North Star"),
        ("provenance", "state records provenance"),
        ("budget", "state has a bounded-call budget"),
        ("ui", "personal UI acceptance remains with Dmitry"),
        ("destructive", "destructive boundary remains user-required"),
        ("secret", "secret boundary remains user-required"),
        ("external", "external side effects remain user-required"),
        ("phase", "drift checks are phase/event based"),
        ("degrad", "budget/failure degradation is documented"),
        ("watchlist", "/go fable watchlist is preserved"),
    ):
        check(term in combined, label)
    check("polling" in combined and any(x in combined for x in ("no polling", "not polling", "do not poll")), "periodic checks are not timer polling")
    hook_text = HOOK.read_text(encoding="utf-8").lower()
    check(
        ("never fabricates a user answer" in hook_text or "do not fabricate" in hook_text)
        and ("synthesize a user-response" in hook_text or "user answered" in hook_text),
        "hook explicitly forbids fabricated User answered events",
    )
    check("autopilot" in CODEX_RUNNER.read_text(encoding="utf-8").lower(), "Codex runner exposes autopilot")
    check("autopilot" in AGENTS.read_text(encoding="utf-8").lower(), "AGENTS command contract exposes autopilot")
    codex_goal_contract = skill_text + "\n" + CODEX_RUNNER.read_text(encoding="utf-8").lower()
    for term, label in (
        ("get_goal", "Codex activation inspects the existing host goal"),
        ("create_goal", "Codex activation creates a persistent host goal"),
        ("without `token_budget`", "Codex activation omits an unsolicited goal budget"),
        ("matching unfinished goal", "Codex activation retains a matching goal"),
        ("different unfinished goal", "Codex activation detects an unrelated active-goal conflict"),
        ("same turn", "Codex activation begins North-Star work immediately"),
    ):
        check(term in codex_goal_contract, label)
    check(
        "`status` and `off` never create a goal" in combined,
        "status/off cannot accidentally activate a Codex goal",
    )
    check(
        "setup-only command" in command_text
        and "only reports enabled/status" in command_text,
        "activation-only completion is forbidden",
    )
    check(
        "opaque host feature" in command_text
        and "do not claim that booster invoked it" in command_text,
        "Claude /goal boundary is documented honestly",
    )

    with tempfile.TemporaryDirectory(prefix="fable-autopilot-") as tmp:
        home = Path(tmp)
        project = home / "project"
        state_dir = project / ".claude"
        state_dir.mkdir(parents=True)
        # The installed contract uses this stable state filename. Extra fields
        # are deliberate: old/new hook versions must ignore forward-compatible
        # state rather than silently disabling the gate.
        state_path = state_dir / "autopilot.json"

        def write_state(max_calls: int = 20, *, usage_percent: float = 0) -> None:
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "enabled": True,
                        "scope": str(project.resolve()),
                        "north_star": "Ship the accepted feature without local-scope drift",
                        "provenance": [],
                        "checkpoints": [],
                        "reservations": {},
                        "max_fable_calls": max_calls,
                        "calls_used": 0,
                        "degraded": False,
                        "usage_percent": usage_percent,
                        "future_field": "must be ignored",
                    }
                ),
                encoding="utf-8",
            )

        write_state()

        base = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "cwd": str(project)}
        cases = [
            ("Should sealed holdout gold become a permanent CI oracle?", "deny", "ordinary engineering decision routes to Fable"),
            ("Which implementation is simpler while preserving the API contract?", "deny", "reversible implementation choice routes to Fable"),
            (
                "What order should the isolated unit tests run in?",
                "deny",
                "technical ordering is not mistaken for an external order",
            ),
            (
                "Which error message should the local validator return?",
                "deny",
                "local diagnostic wording is not mistaken for an external message",
            ),
            (
                "May I run a read-only security analysis on temporary copies under /tmp, "
                "with no credentials, network access, or project writes?",
                "deny",
                "security label alone does not prevent reversible sandbox delegation",
            ),
            ("Should the UI service call the API through an adapter?", "deny", "UI architecture is not personal UI acceptance"),
            ("How should these backend services interact?", "deny", "service interaction is an engineering decision"),
            ("Choose the layout algorithm for the UI architecture?", "deny", "generic UI layout algorithm delegates"),
            ("Which UI click-handler architecture should dispatch this internal event?", "deny", "technical click-handler design delegates"),
            ("Dmitry, please click this UI control and visually approve the result?", "allow", "personally addressed UI click and visual approval stays with Dmitry"),
            ("Please visually approve this UI and choose the preferred layout.", "allow", "personal UI acceptance stays with Dmitry"),
            ("May I permanently delete the production records?", "allow", "irreversible action stays with Dmitry"),
            ("Send this announcement to the customer now?", "allow", "external message stays with Dmitry"),
            ("Paste the missing production API secret.", "allow", "missing secret stays with Dmitry"),
            (
                "Replace the project's real signing credentials and persist the new secret in its config?",
                "allow",
                "real secret and persistent project-integrity risk stay with Dmitry",
            ),
            ("Overwrite the generated fixture under /tmp/autopilot-output?", "deny", "temporary generated overwrite is reversible"),
            ("Reset the disposable sandbox fixture?", "deny", "sandbox reset is reversible"),
            ("Format the unknown target?", "allow", "unknown-scope format stays with Dmitry"),
            ("chmod the unknown file?", "allow", "unknown-scope chmod stays with Dmitry"),
            ("Move the unknown directory?", "allow", "unknown-scope move stays with Dmitry"),
            ("Replace the unknown state?", "allow", "unknown-scope replace stays with Dmitry"),
            ("Reset main and then remove /tmp/generated-fixture?", "allow", "one safe clause cannot launder destructive main reset"),
            ("After checking /tmp, chmod the repository files?", "allow", "temporary preface cannot launder persistent chmod"),
            ("Reset main after removing /tmp/generated-fixture?", "allow", "after-clause cannot launder destructive main reset"),
            ("While inspecting /tmp, replace repository state?", "allow", "while-clause cannot launder persistent replacement"),
            ("Using /tmp as scratch, chmod the repository?", "allow", "using-clause cannot launder persistent chmod"),
            ("Delete /tmp fixture and release branch?", "allow", "safe temp delete cannot coordinate an unsafe branch release"),
            ("Remove temporary artifact and main branch?", "allow", "temporary artifact cannot launder main branch removal"),
            ("Delete /tmp fixture plus main branch?", "allow", "plus cannot launder persistent branch deletion"),
            ("Remove temporary artifact as well as release branch?", "allow", "as-well-as cannot launder release branch removal"),
            ("Delete /tmp fixture & repository branch?", "allow", "ampersand cannot launder repository branch deletion"),
        ]
        for question, expected, label in cases:
            write_state()
            payload = dict(base)
            payload["tool_input"] = {"questions": [{"question": question, "header": "Decision", "options": []}]}
            rc, body, stderr = run_hook(payload, home)
            got, reason = decision(rc, body, stderr)
            check(rc in (0, 2), f"{label}: hook uses allow/deny exit protocol ({stderr.strip()})")
            check(got == expected, f"{label}: decision={expected} (reason={reason!r})")
            if expected == "deny":
                check("fable" in reason.lower(), f"{label}: deny instructs Lead to consult Fable")
                check(
                    "synthesize a user-response" in reason.lower() or "do not fabricate" in reason.lower(),
                    f"{label}: deny does not fake a user answer",
                )

        write_state()
        subagent_payload = dict(base)
        subagent_payload.update({"agent_id": "child-1", "agent_type": "general-purpose"})
        subagent_payload["tool_input"] = {"question": "Which reversible implementation should we use?"}
        rc, body, stderr = run_hook(subagent_payload, home)
        got, reason = decision(rc, body, stderr)
        check(got == "deny", "subagent AskUserQuestion cannot bypass Fable routing")

        # The entire AskUserQuestion surface is security-relevant. A harmless
        # question must not conceal a hard boundary in header/options/description.
        hidden_risk = dict(base)
        write_state()
        hidden_risk["tool_input"] = {
            "questions": [
                {
                    "header": "Production credentials",
                    "question": "Which approach should we use?",
                    "options": [
                        {"label": "Proceed", "description": "Overwrite the real customer API secret"},
                        {"label": "Pause", "description": "Do nothing"},
                    ],
                }
            ]
        }
        rc, body, stderr = run_hook(hidden_risk, home)
        got, reason = decision(rc, body, stderr)
        check(got == "allow", f"header/options/descriptions cannot hide USER_REQUIRED risk (reason={reason!r})")

        oversized = dict(base)
        write_state()
        oversized["tool_input"] = {"question": "Delete production customer data?" + (" x" * 10000)}
        rc, body, stderr = run_hook(oversized, home)
        got, reason = decision(rc, body, stderr)
        check(got == "allow", f"oversized/truncated question fails safely to user (reason={reason!r})")

        # State is project-scoped. An enabled state in HOME must never silently
        # authorize another cwd/project.
        home_state = home / ".claude" / "fable_autopilot.json"
        home_state.parent.mkdir(exist_ok=True)
        home_state.write_text(state_path.read_text(encoding="utf-8"), encoding="utf-8")
        foreign = home / "foreign-project"
        foreign.mkdir()
        foreign_payload = dict(base)
        foreign_payload["cwd"] = str(foreign)
        foreign_payload["tool_input"] = {"question": "Which local implementation should we use?"}
        rc, body, stderr = run_hook(foreign_payload, home)
        got, reason = decision(rc, body, stderr)
        check(got == "allow", f"autopilot state does not leak through HOME fallback (reason={reason!r})")
        check(rc == 0 and body == {} and stderr == "", "absent project state is a quiet no-op")

        ambient = home / "ambient"
        child = ambient / "nested-workspace"
        (ambient / ".claude").mkdir(parents=True)
        child.mkdir()
        spoof = json.loads(state_path.read_text(encoding="utf-8"))
        spoof["scope"] = str(ambient.resolve())
        (ambient / ".claude" / "autopilot.json").write_text(json.dumps(spoof), encoding="utf-8")
        ambient_payload = dict(base)
        ambient_payload["cwd"] = str(child)
        ambient_payload["tool_input"] = {"question": "Which implementation should we choose?"}
        rc, body, stderr = run_hook(ambient_payload, home)
        got, reason = decision(rc, body, stderr)
        check(got == "allow" and body == {} and stderr == "", "non-git workspace rejects ambient parent .claude state spoof quietly")

        mismatch = json.loads(state_path.read_text(encoding="utf-8"))
        mismatch["scope"] = str((home / "wrong-scope").resolve())
        state_path.write_text(json.dumps(mismatch), encoding="utf-8")
        rc, body, stderr = gate_once() if "gate_once" in locals() else run_hook({**base, "tool_input": {"question": "Which implementation?"}}, home)
        got, reason = decision(rc, body, stderr)
        check(got == "allow", "state scope mismatch fails safely to user")
        write_state()

        def state_cli(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, str(STATE_TOOL), "--cwd", str(project), *args],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )

        def phase_event(event: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, str(HOOK), "phase-event", event], cwd=project, text=True, capture_output=True, timeout=5, check=False)

        for mutation, label in (
            ({"enabled": False}, "disabled"),
            ({"degraded": True}, "degraded"),
            ({"calls_used": 20, "max_fable_calls": 20}, "exhausted"),
        ):
            write_state()
            phase_state = json.loads(state_path.read_text(encoding="utf-8")); phase_state.update(mutation)
            state_path.write_text(json.dumps(phase_state), encoding="utf-8")
            result = phase_event("plan_complete")
            check(result.returncode == 0 and not result.stdout.strip() and not result.stderr.strip(), f"{label} phase-event emits nothing")
        write_state()
        duplicate_state = json.loads(state_path.read_text(encoding="utf-8"))
        duplicate_state["checkpoints"] = [{"kind":"checkpoint","phase":"plan_pfd","status":"COMPLETED"}]
        state_path.write_text(json.dumps(duplicate_state), encoding="utf-8")
        result = phase_event("plan_complete")
        check(result.returncode == 0 and not result.stdout.strip(), "duplicate phase-event emits nothing")
        write_state()
        result = phase_event("first_slice")
        check(result.returncode == 0 and not result.stdout.strip(), "ineligible non-automatic phase-event emits nothing")

        def gate_once() -> tuple[int, dict, str]:
            payload = dict(base)
            payload["tool_input"] = {"question": "Which reversible implementation should we use?"}
            return run_hook(payload, home)

        prompt = home / "brief.txt"
        prompt.write_text("Verified facts brief", encoding="utf-8")
        wrapper_behavior: dict[str, object] = {}
        def set_wrapper(verdict: str = "ON_COURSE", directive: str = "", watchlist: list[dict] | None = None, *, fail: bool = False, delay: float = 0) -> None:
            payload = json.dumps({"verdict": verdict, "directive": directive, "watchlist": watchlist or []})
            wrapper_behavior.clear()
            wrapper_behavior.update(payload=payload.encode(), fail=fail, delay=delay)

        trusted_lock = threading.Lock()
        def trusted(*args: str) -> subprocess.CompletedProcess[str]:
            import time
            def fake_run(argv, **kwargs):
                check(Path(argv[0]).name == "fable_consult.sh", "trusted runner invokes only canonical sibling wrapper")
                child_env = kwargs.get("env", {})
                check(
                    all(key not in child_env for key in ("BASH_ENV", "PYTHONPATH", "CLAUDE_BIN", "FABLE_CONSULT_WRAPPER")),
                    "trusted wrapper subprocess strips executable environment injection variables",
                )
                time.sleep(float(wrapper_behavior.get("delay", 0)))
                return subprocess.CompletedProcess(argv, 17 if wrapper_behavior.get("fail") else 0, stdout=b"" if wrapper_behavior.get("fail") else wrapper_behavior["payload"], stderr=b"")
            with trusted_lock:
                original_run = module.subprocess.run
                module.subprocess.run = fake_run
                try:
                    if args[0] == "consult-decision":
                        rc = module._trusted_consult(str(project), "decision", None, prompt)
                    else:
                        event = args[1]
                        phase = {"plan_complete":"plan_pfd", "first_slice":"implementation_slice", "final_diff":"final_diff"}[event]
                        rc = module._trusted_consult(str(project), "checkpoint", phase, prompt)
                    return subprocess.CompletedProcess(args, rc, stdout="", stderr="")
                finally:
                    module.subprocess.run = original_run

        hook_source = HOOK.read_text(encoding="utf-8")
        check("FABLE_CONSULT_WRAPPER" not in hook_source and "CLAUDE_BIN" not in hook_source, "trusted runner has no environment-selected wrapper or Claude binary")
        override_cli = subprocess.run([sys.executable, str(HOOK), "consult-decision", "--prompt-file", str(prompt), "--wrapper", "/tmp/evil"], cwd=project, text=True, capture_output=True, check=False)
        check(override_cli.returncode != 0, "trusted runner CLI rejects arbitrary wrapper selection")
        poisoned = {key: os.environ.get(key) for key in ("BASH_ENV", "PYTHONPATH", "CLAUDE_BIN", "FABLE_CONSULT_WRAPPER")}
        os.environ.update({"BASH_ENV":"/tmp/evil-bash", "PYTHONPATH":"/tmp/evil-python", "CLAUDE_BIN":"/tmp/evil-claude", "FABLE_CONSULT_WRAPPER":"/tmp/evil-wrapper"})
        write_state(max_calls=1)
        set_wrapper()
        check(trusted("consult-decision", "--prompt-file", str(prompt)).returncode == 0, "poisoned parent environment cannot influence canonical trusted wrapper")
        for key, value in poisoned.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        # Reservation and charging are owned by the trusted wrapper runner.
        write_state(max_calls=2)
        set_wrapper(delay=0.25)
        with ThreadPoolExecutor(max_workers=6) as pool:
            parallel = list(pool.map(lambda _: trusted("consult-decision", "--prompt-file", str(prompt)), range(6)))
        successes = [p for p in parallel if p.returncode == 0]
        check(len(successes) == 2, f"concurrent trusted reservations never exceed Fable budget (completed={len(successes)})")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        check(state["calls_used"] == 2 and not state["reservations"], "trusted runner atomically reconciles successful wrapper calls")
        check(all(row.get("output_sha256") and row.get("receipt_digest") for row in state["provenance"]), "trusted runner persists nonce-bound output provenance")

        fabricated = subprocess.run([sys.executable, str(HOOK), "complete", "fake", "--receipt-json", "{}"], cwd=project, text=True, capture_output=True, check=False)
        fabricated_state = state_cli("complete", "--token", "fake", "--receipt-json", "{}")
        check(fabricated.returncode != 0 and fabricated_state.returncode != 0, "caller cannot invoke a fabricated receipt completion API")

        write_state(max_calls=2)
        set_wrapper(fail=True)
        failed = trusted("consult-decision", "--prompt-file", str(prompt))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        check(failed.returncode != 0 and state["degraded"] is True and not state["reservations"], "wrapper failure releases reservation and degrades safely")

        write_state(max_calls=1)
        set_wrapper()
        check(trusted("consult-decision", "--prompt-file", str(prompt)).returncode == 0, "one-slot trusted call succeeds")
        check(trusted("consult-decision", "--prompt-file", str(prompt)).returncode != 0, "budget exhaustion rejects another trusted call")

        write_state()
        usage = state_cli("usage", "--percent", "80")
        check(usage.returncode == 0, f">=80% usage transition succeeds ({usage.stderr.strip()})")
        rc, body, stderr = gate_once()
        got, reason = decision(rc, body, stderr)
        check(got == "allow", ">=80% Fable usage degrades delegated decisions to Dmitry")
        write_state()
        check(state_cli("usage", "--percent", "79").returncode == 0, "79% usage is accepted")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        check(state["degraded"] is False and state["usage_percent"] == 79, "79% usage does not degrade autopilot")
        check(state_cli("usage", "--percent", "-1").returncode != 0 and state_cli("usage", "--percent", "101").returncode != 0, "invalid usage bounds are rejected")
        check(state_cli("usage", "--percent", "80").returncode == 0, "usage can enter 80% degradation")
        blocked_recover = state_cli("recover", "--reason", "premature recovery")
        check(blocked_recover.returncode != 0, "recovery is rejected while usage remains >=80%")
        check(state_cli("usage", "--percent", "79").returncode == 0, "usage can fall below recovery threshold")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["degraded"] = True
        state["degradation_reason"] = "test failure"
        state["provenance"].append({"status": "HISTORICAL", "reason": "keep"})
        state_path.write_text(json.dumps(state), encoding="utf-8")
        recovered_cli = state_cli("recover", "--reason", "operator restored Fable")
        recovered_state = json.loads(state_path.read_text(encoding="utf-8"))
        check(recovered_cli.returncode == 0 and recovered_state["degraded"] is False, "explicit recovery clears degraded state")
        check(any(row.get("status") == "HISTORICAL" for row in recovered_state["provenance"]) and any(row.get("status") == "RECOVERED" for row in recovered_state["provenance"]), "recovery preserves history and appends provenance")

        # Two automatic checkpoint calls plus one reserved decision capacity.
        write_state(max_calls=3)
        set_wrapper("ON_COURSE", watchlist=[{"id":"north-star","status":"OPEN"}])
        check(trusted("checkpoint", "plan_complete", "--prompt-file", str(prompt)).returncode == 0, "trusted plan checkpoint executes")
        set_wrapper("ON_COURSE", watchlist=[{"id":"north-star","status":"CLOSED","closure_evidence":"pytest exit=0"}])
        check(trusted("checkpoint", "final_diff", "--prompt-file", str(prompt)).returncode == 0, "trusted final checkpoint executes")
        set_wrapper()
        check(trusted("consult-decision", "--prompt-file", str(prompt)).returncode == 0, "two checkpoints preserve one decision-call capacity")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        phases = [row.get("phase") for row in state["checkpoints"]]
        verdicts = [row.get("verdict") for row in state["checkpoints"]]
        check(phases == ["plan_pfd", "final_diff"], "automatic plan/PFD and final diff checkpoints persist in order")
        check(set(verdicts) <= {"ON_COURSE", "REFOCUS", "REPLAN", "ASK_USER"}, "checkpoint verdict schema is closed and typed")
        final_items = state["checkpoints"][-1]["watchlist"]
        check(final_items[0]["status"] == "CLOSED" and final_items[0]["closure_evidence"], "final diff persists watchlist reconciliation evidence")

        fabricated = subprocess.run(
            [sys.executable, str(HOOK), "checkpoint", "plan_complete", "ON_COURSE"],
            cwd=project, text=True, capture_output=True, timeout=5, check=False,
        )
        check(fabricated.returncode != 0, "caller cannot inject/fabricate a checkpoint verdict")

        write_state(max_calls=3)
        set_wrapper()
        out_of_order = subprocess.run(
            [sys.executable, str(HOOK), "checkpoint", "first_slice", "--prompt-file", str(prompt)], cwd=project,
            text=True, capture_output=True, timeout=5, check=False,
        )
        check(out_of_order.returncode != 0, "checkpoint event order rejects first slice before plan/PFD")
        check(trusted("checkpoint", "plan_complete", "--prompt-file", str(prompt)).returncode == 0, "plan checkpoint succeeds through trusted runner")
        duplicate_plan = trusted("checkpoint", "plan_complete", "--prompt-file", str(prompt))
        check(duplicate_plan.returncode != 0, "checkpoint phases are unique and duplicate plan trigger is rejected")

        set_wrapper("ON_COURSE", watchlist=[{"id":"unresolved","status":"OPEN"}])
        open_final = trusted("checkpoint", "final_diff", "--prompt-file", str(prompt))
        check(open_final.returncode != 0, "final checkpoint cannot pass with OPEN watchlist items")

        # Reservations carry timestamps and stale ones recover without permanently
        # consuming budget or silently disappearing from provenance.
        write_state(max_calls=2)
        stale_state = json.loads(state_path.read_text(encoding="utf-8"))
        stale_token = "d" * 32
        stale_state["reservations"][stale_token] = {
            "kind": "decision", "phase": None, "created_at": "2000-01-01T00:00:00+00:00"
        }
        state_path.write_text(json.dumps(stale_state), encoding="utf-8")
        set_wrapper()
        stale_run = trusted("consult-decision", "--prompt-file", str(prompt))
        check(stale_run.returncode == 0, f"stale reservation is recovered before new trusted reservation ({stale_run.stderr.strip()})")
        recovered = json.loads(state_path.read_text(encoding="utf-8"))
        check(any(row.get("status") == "RELEASED_STALE" and row.get("reservation_id") == stale_token for row in recovered["provenance"]), "stale reservation recovery is timestamped/audited in provenance")

        write_state(max_calls=3)
        set_wrapper("REPLAN", "Rebuild the plan around the North Star")
        check(trusted("checkpoint", "plan_complete", "--prompt-file", str(prompt)).returncode == 0, "REPLAN receipt with directive is accepted")
        set_wrapper("ASK_USER", "Personal product authority is required", [{"id":"decision","status":"CLOSED","closure_evidence":"escalated"}])
        check(trusted("checkpoint", "final_diff", "--prompt-file", str(prompt)).returncode == 0, "ASK_USER receipt with directive is accepted")
        write_state(max_calls=3)
        set_wrapper("ON_COURSE", watchlist=[{"id":"bad","status":"CLOSED"}])
        invalid_watch = trusted("checkpoint", "plan_complete", "--prompt-file", str(prompt))
        check(invalid_watch.returncode != 0, "invalid closed watchlist without evidence is rejected")

        write_state()
        malformed = json.loads(state_path.read_text(encoding="utf-8"))
        malformed["reservations"] = {"not-a-token": {"kind":"decision","phase":None}}
        state_path.write_text(json.dumps(malformed), encoding="utf-8")
        rc, body, stderr = gate_once()
        got, reason = decision(rc, body, stderr)
        check(got == "allow", "malformed reservation schema fails safely to user")

        write_state(max_calls=3)
        set_wrapper(delay=0.2)
        with ThreadPoolExecutor(max_workers=4) as pool:
            checkpoint_runs = list(pool.map(lambda _: trusted("checkpoint", "plan_complete", "--prompt-file", str(prompt)), range(4)))
        check(sum(r.returncode == 0 for r in checkpoint_runs) == 1, "concurrent identical checkpoint triggers reserve at most one phase")

        codex_contract = (CODEX_SKILL.read_text(encoding="utf-8") + CODEX_RUNNER.read_text(encoding="utf-8") + (ROOT / "templates/codex/prompts/autopilot.md").read_text(encoding="utf-8")).lower()
        for term in ("plan_complete", "first_slice", "final_diff", "on_course", "refocus", "replan", "ask_user"):
            check(term in codex_contract, f"Codex parity exposes checkpoint contract: {term}")

        # Tail reader must handle large valid transcripts without loading the
        # entire file, but an incomplete final JSONL record fails safe.
        write_state()
        large_transcript = home / "large-session.jsonl"
        filler = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "progress" * 200}]}}) + "\n"
        final_question = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Which reversible implementation should we use?"}]}}) + "\n"
        large_transcript.write_text(filler * 80 + final_question, encoding="utf-8")
        rc, body, stderr = run_hook({"hook_event_name": "Stop", "cwd": str(project), "transcript_path": str(large_transcript), "stop_hook_active": False}, home)
        got, reason = decision(rc, body, stderr)
        check(got in {"block", "deny"} and "fable" in reason.lower(), ">64KiB transcript with complete final record Stop-delegates")
        write_state()
        incomplete = home / "incomplete-session.jsonl"
        incomplete.write_text(filler * 80 + '{"type":"assistant","message":', encoding="utf-8")
        rc, body, stderr = run_hook({"hook_event_name": "Stop", "cwd": str(project), "transcript_path": str(incomplete), "stop_hook_active": False}, home)
        got, reason = decision(rc, body, stderr)
        check(got == "allow", "incomplete final transcript record fails safely to user")

        write_state()
        transcript = home / "session.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Should the sealed holdout become a permanent CI oracle?"}
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        rc, body, stderr = run_hook(
            {
                "hook_event_name": "Stop",
                "cwd": str(project),
                "transcript_path": str(transcript),
                "stop_hook_active": False,
            },
            home,
        )
        stop_decision, stop_reason = decision(rc, body, stderr)
        # Stop hooks use decision=block rather than permissionDecision=deny.
        check(stop_decision in {"block", "deny"}, "plain-text Stop question is blocked for Fable delegation")
        check("fable" in stop_reason.lower(), "plain-text Stop fallback carries Fable instruction")
        write_state()
        rc2, body2, stderr2 = run_hook(
            {"hook_event_name":"Stop","cwd":str(project),"transcript_path":str(transcript),"stop_hook_active":False}, home
        )
        duplicate_decision, duplicate_reason = decision(rc2, body2, stderr2)
        check(duplicate_decision == stop_decision and ("fable" in duplicate_reason.lower()), "duplicate Stop behavior is deterministic")

        for text_value, label in (
            ("Code example: result = condition ? left : right", "code-question-mark false positive"),
            ("Was this not inevitable?", "rhetorical-question false positive"),
        ):
            write_state()
            transcript.write_text(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":text_value}]}}) + "\n", encoding="utf-8")
            rc3, body3, stderr3 = run_hook({"hook_event_name":"Stop","cwd":str(project),"transcript_path":str(transcript),"stop_hook_active":False}, home)
            stop_kind, _ = decision(rc3, body3, stderr3)
            check(stop_kind == "allow", f"{label} is filtered by the bounded Stop question heuristic")

    print("PASS Fable autopilot acceptance contract")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise SystemExit(1)
