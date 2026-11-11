"""Black-box contract for question_router.py + advisor_consult.py.

The hook is driven with Claude Code hook JSON on stdin. The advisor CLI is a
stub (BOOSTER_CLAUDE_BIN) so no model is called; state/config live in tmp.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "templates" / "scripts"
ROUTER = SCRIPTS / "question_router.py"
ASK_GATE = SCRIPTS / "ask_gate.py"
FABLE = SCRIPTS / "fable_autopilot.py"

ETIM_Q = {
    "question": "Принимать каталог ABB (PDF) как доказательство ETIM, если код напечатан в полной таблице под заголовком изделия, со ссылкой на страницу?",
    "header": "Каталог ABB",
    "multiSelect": False,
    "options": [
        {"label": "Да, с замером", "description": "Технолог сначала считает, сколько кодов даст каталог."},
        {"label": "Нет", "description": "Остаток ABB ждёт данных от ABB (письмо)."},
    ],
}

STUB = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    prompt = sys.stdin.read()
    log = os.environ["STUB_LOG"]
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "child": os.environ.get("BOOSTER_ADVISOR_CHILD"), "prompt": prompt[:4000]}) + "\\n")
    mode = os.environ.get("STUB_MODE", "answer")
    if mode == "fail":
        sys.exit(3)
    if mode == "revise":
        out = {"verdict": "revise", "feedback": "Add a rollback step.", "defer_steps": [], "confidence": "high"}
    elif mode == "approve":
        out = {"verdict": "approve", "feedback": "ok", "defer_steps": ["send the real order"], "confidence": "high"}
    elif mode == "defer":
        out = {"answers": [{"question": "q", "answer": "x"}], "rationale": "owner boundary", "confidence": "high", "defer_to_owner": True}
    else:
        out = {"answers": [{"question": "q", "answer": os.environ.get("STUB_ANSWER", "Да, с замером")}], "rationale": "measure first", "confidence": "high", "defer_to_owner": False}
    print(json.dumps({"type": "result", "result": "", "structured_output": out}))
    """
)


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    stub = tmp_path / "claude_stub.py"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    config = tmp_path / "qr.json"
    config.write_text(json.dumps({"codex_fallback": False, "notify": False, "budget_s": 60}), encoding="utf-8")
    e = {k: v for k, v in os.environ.items() if k not in ("BOOSTER_ADVISOR_CHILD", "CODEX_THREAD_ID")}
    e.update({
        "BOOSTER_CLAUDE_BIN": str(stub),
        "BOOSTER_QROUTER_CONFIG": str(config),
        "BOOSTER_QROUTER_STATE": str(tmp_path / "state"),
        "STUB_LOG": str(tmp_path / "stub.log"),
        "HOME": str(tmp_path / "home"),
    })
    (tmp_path / "home").mkdir()
    return e


def run(script: Path, payload: dict, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args], input=json.dumps(payload), text=True,
        capture_output=True, env=env, timeout=60,
    )


def out(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def ask(mode: str | None, cwd: Path, question: dict = ETIM_Q, sid: str = "s1", event: str = "PreToolUse") -> dict:
    payload = {
        "hook_event_name": event, "tool_name": "AskUserQuestion", "session_id": sid,
        "cwd": str(cwd), "tool_input": {"questions": [question]},
    }
    if mode is not None:
        payload["permission_mode"] = mode
    return payload


@pytest.mark.parametrize("mode", ["bypassPermissions", "dontAsk", "auto"])
def test_unattended_modes_answer_without_dialog(env, tmp_path, mode):
    result = out(run(ROUTER, ask(mode, tmp_path), env))
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "ADVISOR_ANSWER" in hso["permissionDecisionReason"]
    assert "Да, с замером" in hso["permissionDecisionReason"]
    log = [json.loads(line) for line in Path(env["STUB_LOG"]).read_text().splitlines()]
    assert log[0]["child"] == "1", "advisor child must carry the recursion guard"
    assert "--no-session-persistence" in log[0]["argv"] and "Read,Grep,Glob" in log[0]["argv"]
    decisions = (tmp_path / "state" / "decisions.jsonl").read_text()
    assert '"outcome": "answered"' in decisions


@pytest.mark.parametrize("mode", ["default", "acceptEdits", None])
def test_attended_modes_pass_through(env, tmp_path, mode):
    proc = run(ROUTER, ask(mode, tmp_path), env)
    assert proc.returncode == 0 and proc.stdout.strip() == ""
    assert not Path(env["STUB_LOG"]).exists()


def test_answers_delivery_rewrites_input(env, tmp_path):
    Path(env["BOOSTER_QROUTER_CONFIG"]).write_text(json.dumps({"codex_fallback": False, "notify": False, "delivery": "answers"}))
    hso = out(run(ROUTER, ask("bypassPermissions", tmp_path), env))["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["questions"][0]["question"] == ETIM_Q["question"]
    assert hso["updatedInput"]["answers"] == {ETIM_Q["question"]: "Да, с замером"}


def test_permission_request_ask_allows_with_answers(env, tmp_path):
    decision = out(run(ROUTER, ask("bypassPermissions", tmp_path, event="PermissionRequest"), env))["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    assert decision["updatedInput"]["answers"][ETIM_Q["question"]] == "Да, с замером"


def test_plan_mode_inherits_previous_unattended_mode(env, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "permissionMode": "bypassPermissions", "message": {"content": "do it"}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "EnterPlanMode", "input": {}}]}}) + "\n"
        + json.dumps({"type": "user", "permissionMode": "plan", "message": {"content": "plan"}}) + "\n"
    )
    payload = {
        "hook_event_name": "PermissionRequest", "tool_name": "ExitPlanMode", "session_id": "p1",
        "permission_mode": "plan", "cwd": str(tmp_path), "transcript_path": str(transcript),
        "tool_input": {"plan": "# Plan\n1. edit\n2. test"},
    }
    env = {**env, "STUB_MODE": "approve"}
    decision = out(run(ROUTER, payload, env))["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    plan = decision["updatedInput"]["plan"]
    assert plan.startswith(payload["tool_input"]["plan"])
    assert "DEFERRED_TO_OWNER — do NOT execute" in plan and "send the real order" in plan, "agent must see the deferral"
    queue = (tmp_path / "state" / "owner_queue.jsonl").read_text()
    assert "send the real order" in queue


def test_owner_switched_plan_mode_is_not_routed(env, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "permissionMode": "bypassPermissions", "message": {"content": "do it"}}) + "\n"
        + json.dumps({"type": "user", "permissionMode": "plan", "message": {"content": "now plan it, I will review"}}) + "\n"
    )
    payload = {
        "hook_event_name": "PermissionRequest", "tool_name": "ExitPlanMode", "session_id": "p9",
        "permission_mode": "plan", "cwd": str(tmp_path), "transcript_path": str(transcript),
        "tool_input": {"plan": "# Plan"},
    }
    assert run(ROUTER, payload, env).stdout.strip() == "", "Shift+Tab into plan mode means the owner reviews"


def test_plan_hard_boundary_without_advisor_defer_still_annotated(env, tmp_path):
    payload = {
        "hook_event_name": "PermissionRequest", "tool_name": "ExitPlanMode", "session_id": "p8",
        "permission_mode": "auto", "cwd": str(tmp_path),
        "tool_input": {"plan": "1. build\n2. Submit the SBER order through Tinkoff"},
    }
    env = {**env, "STUB_MODE": "fail"}
    decision = out(run(ROUTER, payload, env))["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow" and "DEFERRED_TO_OWNER" in decision["updatedInput"]["plan"]


def test_plan_revise_then_capped_approval(env, tmp_path):
    payload = {
        "hook_event_name": "PermissionRequest", "tool_name": "ExitPlanMode", "session_id": "p2",
        "permission_mode": "auto", "cwd": str(tmp_path), "tool_input": {"plan": "# Plan\n1. edit"},
    }
    env = {**env, "STUB_MODE": "revise"}
    behaviors = [out(run(ROUTER, payload, env))["hookSpecificOutput"]["decision"]["behavior"] for _ in range(3)]
    assert behaviors == ["deny", "deny", "allow"], "two revisions max, then approve"


def test_plan_mode_without_unattended_history_passes(env, tmp_path):
    payload = {
        "hook_event_name": "PermissionRequest", "tool_name": "ExitPlanMode", "session_id": "p3",
        "permission_mode": "plan", "cwd": str(tmp_path), "tool_input": {"plan": "x"},
    }
    proc = run(ROUTER, payload, env)
    assert proc.stdout.strip() == ""


@pytest.mark.parametrize("text,reason", [
    ("Отправить реальную заявку на покупку SBER на брокерский счёт?", "live_order"),
    ("Place a live order for 10 AAPL on the broker account?", "live_order"),
    ("Please paste the API key for the exchange so I can continue?", "secret"),
    ("Удалить таблицу orders на проде?", "irreversible_prod_delete"),
    ("Pay for the Pro subscription now?", "payment"),
    ("Send the announcement email to the customers?", "external_publication"),
    ("Submit the SBER order through Tinkoff now?", "live_order"),
    ("Set TRADING_MODE=live and restart the bot?", "live_order"),
    ("Купить 5 лотов GAZP?", "live_order"),
    ("Paste your BINANCE_API_KEY so I can continue?", "secret"),
    ("Отправить письмо поставщику ABB с запросом данных?", "external_publication"),
])
def test_hard_boundary_defers_without_waiting(env, tmp_path, text, reason):
    q = {"question": text, "header": "x", "multiSelect": False, "options": [{"label": "Да", "description": ""}, {"label": "Нет", "description": ""}]}
    hso = out(run(ROUTER, ask("bypassPermissions", tmp_path, q), env))["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"].startswith(f"DEFERRED_TO_OWNER ({reason})")
    assert not Path(env["STUB_LOG"]).exists(), "hard boundary must not be delegated to a model"
    assert reason in (tmp_path / "state" / "owner_queue.jsonl").read_text()


@pytest.mark.parametrize("text", [
    ETIM_Q["question"],
    "Deploy the release to prod now that tests are green?",
    "Should I refactor the parser or patch it in place?",
    "Обновить ключевой модуль или оставить как есть?",
    "Pay attention to flaky tests — rerun them or skip?",
    "Run the paper order test against the Alpaca sandbox?",
    "Should I buy more time for the refactor by stubbing it?",
    "Remove the stale build artifacts from prod/dist?",
    "Write the log message to stderr or a file?",
])
def test_ordinary_engineering_questions_are_not_deferred(env, tmp_path, text):
    q = {"question": text, "header": "x", "multiSelect": False, "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}
    reason = out(run(ROUTER, ask("bypassPermissions", tmp_path, q), env))["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("ADVISOR_ANSWER")


def test_advisor_defer_routes_to_queue(env, tmp_path):
    env = {**env, "STUB_MODE": "defer"}
    reason = out(run(ROUTER, ask("bypassPermissions", tmp_path), env))["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("DEFERRED_TO_OWNER (advisor_deferred)")


def test_advisor_failure_falls_back_to_recommended_option(env, tmp_path):
    env = {**env, "STUB_MODE": "fail"}
    q = {"question": "Which cache?", "header": "c", "multiSelect": False,
         "options": [{"label": "Redis", "description": ""}, {"label": "In-memory (Recommended)", "description": ""}]}
    reason = out(run(ROUTER, ask("bypassPermissions", tmp_path, q), env))["hookSpecificOutput"]["permissionDecisionReason"]
    assert "In-memory (Recommended)" in reason and "deterministic" in reason


def test_repeat_question_uses_cache(env, tmp_path):
    run(ROUTER, ask("bypassPermissions", tmp_path), env)
    run(ROUTER, ask("bypassPermissions", tmp_path), env)
    assert len(Path(env["STUB_LOG"]).read_text().splitlines()) == 1


def test_recursion_guard_and_codex_noop(env, tmp_path):
    for extra in ({"BOOSTER_ADVISOR_CHILD": "1"}, {"CODEX_THREAD_ID": "t"}):
        proc = run(ROUTER, ask("bypassPermissions", tmp_path), {**env, **extra})
        assert proc.returncode == 0 and proc.stdout.strip() == ""


def stop_payload(tmp_path: Path, text: str, sid: str = "st", active: bool = False) -> dict:
    return {
        "hook_event_name": "Stop", "session_id": sid, "cwd": str(tmp_path), "permission_mode": "bypassPermissions",
        "stop_hook_active": active, "last_assistant_message": text,
        "messages": [{"role": "assistant", "content": text}],
    }


def test_stop_on_question_blocks_with_answer(env, tmp_path):
    result = out(run(ROUTER, stop_payload(tmp_path, "Готово с шагом 1.\n\nПродолжать со шагом 2 или остановиться?"), env))
    assert result["decision"] == "block" and "ADVISOR_ANSWER" in result["reason"]


@pytest.mark.parametrize("text", [
    "All done. Tests pass (12/12).",
    "Why did it fail? The cache key was stale; fixed in a1b2c3 and verified with 14 passing tests, so the dashboard is green again.",
    "Итог: всё задеплоено, проверки зелёные.",
])
def test_stop_on_statement_allows(env, tmp_path, text):
    proc = run(ROUTER, stop_payload(tmp_path, text), env)
    assert proc.stdout.strip() == ""


def test_stop_cap_per_hour(env, tmp_path):
    decisions = []
    for _ in range(4):
        proc = run(ROUTER, stop_payload(tmp_path, "Should I continue with step 2?"), env)
        decisions.append(json.loads(proc.stdout)["decision"] if proc.stdout.strip() else "allow")
    assert decisions == ["block", "block", "block", "allow"]


def test_stop_hard_boundary_defers(env, tmp_path):
    result = out(run(ROUTER, stop_payload(tmp_path, "Всё готово. Отправить реальную заявку на брокерский счёт?"), env))
    assert result["decision"] == "block" and result["reason"].startswith("DEFERRED_TO_OWNER")


def test_ask_gate_yields_question_to_router(env, tmp_path):
    payload = stop_payload(tmp_path, "Want me to deploy it?")
    assert run(ASK_GATE, payload, env).returncode == 0
    payload["permission_mode"] = "default"
    assert run(ASK_GATE, payload, env).returncode == 2, "attended sessions keep the legacy gate"


def test_ask_gate_ignores_degraded_autopilot(env, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "autopilot.json").write_text(json.dumps({"enabled": True, "degraded": True, "scope": str(tmp_path.resolve())}))
    payload = stop_payload(tmp_path, "Want me to deploy it?")
    payload["permission_mode"] = "default"
    assert run(ASK_GATE, payload, env).returncode == 2


def test_fable_autopilot_yields_in_unattended_mode(env, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "autopilot.json").write_text(json.dumps({
        "version": 1, "enabled": True, "scope": str(tmp_path.resolve()), "north_star": "ship",
        "calls_used": 0, "max_fable_calls": 3, "degraded": False, "decision_policy": "x",
        "reservations": {}, "checkpoints": [], "provenance": [],
    }))
    proc = run(FABLE, ask("bypassPermissions", tmp_path), env)
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_settings_template_registers_router():
    text = (ROOT / "templates" / "settings.json.template").read_text()
    settings = json.loads(text)
    hooks = settings["hooks"]

    def cmds(event: str, matcher: str | None = None) -> list[dict]:
        return [h for g in hooks.get(event, []) if matcher is None or g.get("matcher") == matcher for h in g["hooks"]]

    pre = [h for h in cmds("PreToolUse", "AskUserQuestion") if "question_router.py" in h["command"]]
    assert pre and pre[0]["timeout"] >= 300
    perm = [h for h in cmds("PermissionRequest", "ExitPlanMode|AskUserQuestion") if "question_router.py" in h["command"]]
    assert perm and perm[0]["timeout"] >= 300
    assert any("question_router.py" in h["command"] and h.get("timeout", 0) >= 300 for h in cmds("Stop"))
    assert not any("question_router.py" in h["command"] for h in cmds("UserPromptSubmit")), "no per-prompt cost"


def test_advisor_child_has_no_mcp_or_write_tools(env, tmp_path):
    run(ROUTER, ask("bypassPermissions", tmp_path), env)
    argv = json.loads(Path(env["STUB_LOG"]).read_text().splitlines()[0])["argv"]
    assert "--strict-mcp-config" in argv and argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "mcp__*" in argv[argv.index("--disallowedTools") + 1]


def test_answers_map_never_reuses_an_answer():
    sys.path.insert(0, str(SCRIPTS))
    import question_router as q
    got = q._answers_map(
        [{"question": "Which DB?", "options": []}, {"question": "Run migration now?", "options": []}],
        {"answers": [{"question": "Run migration now?", "answer": "Yes"}, {"question": "Which database?", "answer": "Postgres"}]},
    )
    assert got == {"Which DB?": "Postgres", "Run migration now?": "Yes"}


def test_unsafe_advisor_answer_is_deferred(env, tmp_path):
    env = {**env, "STUB_ANSWER": "Yes — submit the SBER order through Tinkoff now"}
    reason = out(run(ROUTER, ask("bypassPermissions", tmp_path), env))["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("DEFERRED_TO_OWNER (advisor_answer_live_order)")


def test_deterministic_default_never_decides_risky_question(env, tmp_path):
    env = {**env, "STUB_MODE": "fail"}
    q = {"question": "Which broker account should the bot use for the next run?", "header": "b", "multiSelect": False,
         "options": [{"label": "Main (Recommended)", "description": ""}, {"label": "Second", "description": ""}]}
    reason = out(run(ROUTER, ask("bypassPermissions", tmp_path, q), env))["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("DEFERRED_TO_OWNER (advisor_unavailable_risky)")


@pytest.mark.parametrize("payload_kind", ["stop", "plan", "permission_ask"])
def test_internal_error_never_leaves_silence(env, tmp_path, payload_kind):
    Path(env["BOOSTER_QROUTER_CONFIG"]).write_text(json.dumps({"notify": False, "codex_fallback": False, "budget_s": {"bad": 1}}))
    if payload_kind == "stop":
        payload = stop_payload(tmp_path, "Should I proceed with option A or B?")
        assert out(run(ROUTER, payload, env))["decision"] == "block"
    elif payload_kind == "plan":
        payload = {"hook_event_name": "PermissionRequest", "tool_name": "ExitPlanMode", "session_id": "e1",
                   "permission_mode": "auto", "cwd": str(tmp_path), "tool_input": {"plan": "x"}}
        assert out(run(ROUTER, payload, env))["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    else:
        payload = ask("bypassPermissions", tmp_path, event="PermissionRequest")
        payload["tool_input"] = {"questions": "not-a-list"}
        payload["tool_input"] = {"questions": [ETIM_Q]}
        Path(env["BOOSTER_QROUTER_CONFIG"]).write_text(json.dumps({"notify": False, "codex_fallback": False, "plan_max_revisions": "x", "budget_s": "x"}))
        decision = out(run(ROUTER, payload, env))["hookSpecificOutput"]["decision"]
        assert decision["behavior"] in ("allow", "deny")


def test_stop_boundary_checks_whole_message(env, tmp_path):
    text = "Prepared `TRADING_MODE=live` in the bot config and restarted nothing yet.\n\nShall I continue?"
    result = out(run(ROUTER, stop_payload(tmp_path, text), env))
    assert result["reason"].startswith("DEFERRED_TO_OWNER (live_order)")


def test_session_summary_is_per_project(env, tmp_path):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "decisions.jsonl").write_text(
        json.dumps({"ts": "2026-11-11T00:00:00+00:00", "cwd": "/a", "outcome": "answered"}) + "\n"
        + json.dumps({"ts": "2026-11-11T00:00:00+00:00", "cwd": "/b", "outcome": "answered"}) + "\n"
    )
    first = run(ROUTER, {"cwd": "/a"}, env, "session-summary").stdout
    assert "advisor answers since last start: 1" in first
    assert run(ROUTER, {"cwd": "/a"}, env, "session-summary").stdout.strip() == ""
    assert "advisor answers since last start: 1" in run(ROUTER, {"cwd": "/b"}, env, "session-summary").stdout
