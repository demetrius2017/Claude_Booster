#!/usr/bin/env python3
"""Route unattended-session questions to a BIO advisor instead of the owner.

Purpose:
  In bypassPermissions / dontAsk / auto sessions (Desktop or CLI) an
  AskUserQuestion dialog, a plan-approval prompt or a turn that ends on a
  plain-text question freezes the session for hours. This hook answers those
  questions synchronously with ``advisor_consult.py`` so the session never
  waits for a human. Hard owner boundaries (real broker orders, secrets,
  irreversible prod/user-data deletion, payments, external publication) are
  deferred to an owner queue WITHOUT waiting: the agent continues other work.

Events (Claude Code hook JSON on stdin):
  PreToolUse/AskUserQuestion   -> deny with ADVISOR_ANSWER reason (default) or
                                  allow + updatedInput.answers (delivery=answers)
  PermissionRequest/AskUserQuestion -> allow + updatedInput.answers (belt and braces)
  PermissionRequest/ExitPlanMode    -> plan reviewer: allow (approve) or deny (revise)
  Stop                         -> plain-text question at turn end: block with the answer
  SessionStart (session-summary)   -> per-project summary of advisor activity

CLI:
  question_router.py session-summary   print advisor activity since last summary

Config ``~/.claude/question_router.json`` (all optional):
  enabled (true), modes ([bypassPermissions, dontAsk, auto]), delivery (deny|answers),
  budget_s (240), stop_blocks_per_hour (3), plan_max_revisions (2),
  codex_fallback (true), codex_homes, codex_model, notify (true).

Safety:
  Never fabricates provenance: every answer is labelled with BIO/advisor/model.
  No-op inside advisor children (BOOSTER_ADVISOR_CHILD=1) and inside Codex
  (CODEX_THREAD_ID). Fails open to "do nothing" on malformed input; it never
  shows a dialog itself.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
CONFIG_PATH = Path(os.environ.get("BOOSTER_QROUTER_CONFIG") or (CLAUDE_HOME / "question_router.json"))
STATE_DIR = Path(os.environ.get("BOOSTER_QROUTER_STATE") or (CLAUDE_HOME / "state" / "question_router"))
LOG_PATH = STATE_DIR / "decisions.jsonl"
QUEUE_PATH = STATE_DIR / "owner_queue.jsonl"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "modes": ["bypassPermissions", "dontAsk", "auto"],
    "delivery": "deny",
    "budget_s": 240,
    "stop_blocks_per_hour": 3,
    "plan_max_revisions": 2,
    "codex_fallback": True,
    "notify": True,
}
MAX_TRANSCRIPT_TAIL = 400_000
BRIEF_CONTEXT_CHARS = 9000

# Narrow hard owner boundary. Everything else is decided by an advisor.
_ORDER_NOUN = r"(?:orders?|trades?|заявк\w*|ордер\w*|сделк\w*)"
_BROKERS = (r"(?:broker\w*|брокер\w*|tinkoff|тинькофф|t-invest|т-инвест\w*|ibkr|interactive brokers|alpaca|binance|"
            r"bybit|okx|kraken|finam|финам\w*|бкс|bcs|quik|алор|alor|freedom ?finance)")
HARD_BOUNDARY = [
    ("live_order", re.compile(
        rf"\b(?:real|live|реальн\w*|боев\w*|живо\w*)\s+(?:\w+\s+){{0,3}}{_ORDER_NOUN}"
        rf"|\b(?:place|submit|send|execute|fire|route|отправ\w*|выстав\w*|подать|пода\w*|исполн\w*|размест\w*)\s+(?:\w+\s+){{0,3}}{_ORDER_NOUN}"
        rf"|\b(?:buy|sell|купить|продать|купи|продай|покупк\w*|продаж\w*)\b[^.?!\n]{{0,80}}(?:{_BROKERS}|real account|live account|"
        r"реальн\w* сч[её]т\w*|боев\w* сч[её]т\w*|\b\d+\s*(?:shares?|lots?|contracts?|акци\w*|лот\w*|контракт\w*)\b|(?-i:\b[A-Z]{3,6}\b))"
        r"|\b(?:trading_mode\s*=\s*live|dry[_-]?run\s*=\s*(?:false|0|no)|--live\b|live trading|боев\w* режим\w*|"
        r"включ\w* (?:реальн\w*|боев\w*) торговл\w*)",
        re.I)),
    ("secret", re.compile(
        r"\b(?:enter|provide|paste|share|send|give|reveal|type|set|supply|введ\w*|вставь\w*|пришл\w*|дай\w*|сообщ\w*|укаж\w*|скинь\w*)\b[^.?!\n]{0,60}"
        r"(?:\b(?:password|passphrase|api[ _-]?key|secret|credential|token|2fa|otp|seed phrase|private key|парол\w*|секрет\w*|"
        r"токен\w*|ключ\w*\s+api|api[- ]ключ\w*|приватн\w* ключ\w*|код\w*\s+из\s+sms)|[A-Z0-9]*_?(?:API_KEY|TOKEN|SECRET|PASSWORD)\b)",
        re.I)),
    ("payment", re.compile(
        r"\b(?:pay\s+(?:for|the|an?|\$|€)|payment|purchase|buy (?:a |the )?(?:plan|subscription|license|credits)|"
        r"charge\s+(?:the |my |a )?(?:card|account|customer|client)|refund|wire transfer|"
        r"оплат\w*|плат[её]ж\w*|купить\s+(?:подписк|тариф|лиценз|кредит)\w*|перевод\w*\s+денег|возврат\w*\s+денег)\b",
        re.I)),
    ("external_publication", re.compile(
        r"\b(?:publish|post|tweet|announce|send|email|mail)\b[^.?!\n]{0,40}\b(?:e-?mail|letter|newsletter|tweet|announcement|"
        r"press release|(?:message|reply) to|to (?:the )?(?:client|customer|users?|team|public|channel|supplier|vendor))\b"
        r"|\b(?:опубликов\w*|разослать|отправ\w*|напис\w*)\b[^.?!\n]{0,40}\b(?:письм\w*|рассылк\w*|клиент\w*|"
        r"поставщик\w*|в\s+telegram|в\s+телеграм\w*|в\s+slack|в\s+канал|в\s+чат|пост\w*\s+в)",
        re.I)),
    ("irreversible_prod_delete", re.compile(
        r"\b(?:drop|truncate|delete|remove|wipe|purge|erase|rm\s+-rf|удал\w*|стер\w*|сотр\w*|очист\w*|снест\w*|дропн\w*)\b"
        r"[^.?!\n]{0,60}\b(?:tables?|databases?|db|buckets?|users?|records?|rows?|collections?|volumes?|backups?|таблиц\w*|баз\w*|"
        r"запис\w*|бакет\w*|пользовател\w*|клиент\w*|бэкап\w*|данн\w*)\b[^.?!\n]{0,60}"
        r"\b(?:prod|production|продакшн\w*|прод(?:е|а|у|ом)?|боев\w*)\b"
        r"|\b(?:drop|truncate|delete|wipe|purge|удал\w*|стер\w*|снест\w*|дропн\w*)\b[^.?!\n]{0,30}\b(?:prod|production|продакшн\w*|прод(?:е|а|у|ом)?|боев\w*)\b"
        r"[^.?!\n]{0,40}\b(?:tables?|databases?|db|buckets?|users?|records?|данн\w*|таблиц\w*|баз\w*|пользовател\w*|клиент\w*)\b",
        re.I)),
]
# Paper/sandbox trading is explicitly allowed to the agent (owner rule) unless a live marker is present.
SANDBOX = re.compile(r"\b(?:sandbox|paper|demo|mock|dry[- ]?run|testnet|stub|fixture|песочниц\w*|бумажн\w*|демо\w*|тестов\w*\s+сч[её]т\w*|мок\w*)\b", re.I)
LIVE_MARKER = re.compile(r"\b(?:real|live|prod|production|реальн\w*|боев\w*|живо\w*)\b|dry[_-]?run\s*=\s*(?:false|0|no)", re.I)
# Loose risk vocabulary: when every advisor failed, a deterministic default must not decide these.
RISKY_LOOSE = re.compile(rf"{_BROKERS}|{_ORDER_NOUN}|\b(?:secret|password|token|api[ _-]?key|парол\w*|токен\w*|секрет\w*)\b", re.I)

QUESTION_HINT = re.compile(
    r"\b(should|which|do you|would you|may i|can i|want me|shall|prefer|or should|ok to|okay to|"
    r"нужно ли|стоит ли|какой|какую|какие|что делаем|можно ли|хочешь|хотите|продолжать|делать|"
    r"запуска(?:ть|ем)|начина(?:ть|ем)|согласен|подтверд\w*|выбер\w*|ли\b)",
    re.I,
)


# ───────────────────────────── helpers ─────────────────────────────

def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg.update(raw)
    except (OSError, ValueError):
        pass
    return cfg


def _append(path: Path, record: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _session_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")[:120]
    return STATE_DIR / "sessions" / f"{safe}.json"


def load_session(session_id: str) -> dict[str, Any]:
    if not session_id:
        return {}
    try:
        value = json.loads(_session_file(session_id).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_session(session_id: str, state: dict[str, Any]) -> None:
    if not session_id:
        return  # never share state between unidentified sessions
    path = _session_file(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def transcript_tail(path_value: Any, limit: int = MAX_TRANSCRIPT_TAIL) -> list[dict[str, Any]]:
    if not isinstance(path_value, str) or not path_value:
        return []
    try:
        path = Path(path_value)
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(-limit, os.SEEK_END)
            raw = handle.read(limit)
    except OSError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > limit and lines:
        lines = lines[1:]
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def transcript_mode(events: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Return (latest mode, latest non-plan mode) recorded in the transcript."""
    latest = non_plan = None
    for event in events:
        mode = event.get("permissionMode")
        if isinstance(mode, str) and mode:
            latest = mode
            if mode != "plan":
                non_plan = mode
    return latest, non_plan


def agent_entered_plan(events: list[dict[str, Any]]) -> bool:
    """True when the agent itself called EnterPlanMode after the last non-plan entry.

    An owner who switches a session into plan mode by hand (Shift+Tab or
    ``--permission-mode plan``) wants to review the plan; only agent-initiated
    plan mode inherits the earlier unattended mode.
    """
    last_non_plan = -1
    for index, event in enumerate(events):
        mode = event.get("permissionMode")
        if isinstance(mode, str) and mode and mode != "plan":
            last_non_plan = index
    if last_non_plan < 0:
        return False
    for event in events[last_non_plan + 1:]:
        if event.get("type") != "assistant":
            continue
        content = (event.get("message") or {}).get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "EnterPlanMode" for b in content
        ):
            return True
    return False


def effective_mode(data: dict[str, Any], session: dict[str, Any], events: list[dict[str, Any]] | None = None) -> str | None:
    """Resolve the mode that decides routing; agent-entered plan mode inherits the prior mode."""
    mode = data.get("permission_mode") if isinstance(data.get("permission_mode"), str) else None
    if mode and mode != "plan":
        session["last_mode"] = mode
        return mode
    if events is None:
        events = transcript_tail(data.get("transcript_path"))
    latest, non_plan = transcript_mode(events)
    if not mode and latest and latest != "plan":
        session["last_mode"] = latest
        return latest
    if not agent_entered_plan(events):
        return mode or latest
    return non_plan or session.get("last_mode")


def router_active(data: dict[str, Any], config: dict[str, Any] | None = None) -> bool:
    """Public helper for ask_gate / fable_autopilot: will the router own this event?"""
    if os.environ.get("BOOSTER_ADVISOR_CHILD") == "1" or os.environ.get("CODEX_THREAD_ID"):
        return False
    config = config or load_config()
    if not config.get("enabled", True):
        return False
    session = load_session(str(data.get("session_id") or ""))
    mode = effective_mode(data, session)
    return bool(mode and mode in set(config.get("modes") or []))


def hard_boundary(text: str) -> str | None:
    for name, pattern in HARD_BOUNDARY:
        if pattern.search(text or ""):
            if name == "live_order" and SANDBOX.search(text) and not LIVE_MARKER.search(text):
                continue
            return name
    return None


def _strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", " ", text)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def trailing_question(text: str) -> str | None:
    """Return the closing question block of an assistant message, or None."""
    cleaned = _strip_code(text or "").strip()
    if not cleaned:
        return None
    tail = cleaned[-1500:]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", tail) if p.strip()]
    if not paragraphs:
        return None
    last = paragraphs[-1]
    # A short trailing sign-off ("Say the word.") after the question still counts.
    block = "\n\n".join(paragraphs[-2:]) if len(last) < 80 and len(paragraphs) > 1 else last
    if "?" not in block and "？" not in block:
        return None
    last_q = max(block.rfind("?"), block.rfind("？"))
    after = block[last_q + 1:].strip()
    if len(after) > 200:
        return None
    ends_on_question = len(after) <= 2
    if not ends_on_question and not QUESTION_HINT.search(block) and not re.search(r"(?m)^\s*(?:[-*]|\d+[.)]|[A-Da-d]\))\s+", block):
        # A bare rhetorical "?" without an ask or menu is not an owner question.
        return None
    return block


def build_brief(data: dict[str, Any], events: list[dict[str, Any]], body: str) -> str:
    cwd = str(data.get("cwd") or "")
    parts = [f"Working directory: {cwd}", f"Permission mode: {data.get('permission_mode') or 'unknown'}"]
    root = Path(cwd) if cwd else None
    if root and root.is_dir():
        auto = root / ".claude" / "autopilot.json"
        try:
            north = json.loads(auto.read_text(encoding="utf-8")).get("north_star")
            if north:
                parts.append(f"North Star: {north}")
        except (OSError, ValueError, AttributeError):
            pass
        for name in ("roadmap.md", "README.md"):
            path = root / name
            if path.is_file():
                try:
                    parts.append(f"{name} (head):\n" + path.read_text(encoding="utf-8", errors="replace")[:1500])
                except OSError:
                    pass
                break
    convo: list[str] = []
    for event in reversed(events):
        if event.get("type") not in ("user", "assistant") or event.get("isMeta"):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(block.get("text", "")) for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        text = text.strip()
        if not text or text.startswith("<system-reminder>") or "<task-notification>" in text[:200]:
            continue
        convo.append(f"[{event.get('type')}] {text[:2500]}")
        if sum(len(item) for item in convo) > BRIEF_CONTEXT_CHARS:
            break
    if convo:
        parts.append("Recent conversation (oldest first):\n" + "\n---\n".join(reversed(convo)))
    parts.append(body)
    return "\n\n".join(parts)


def notify(title: str, message: str, config: dict[str, Any]) -> None:
    if not config.get("notify", True) or sys.platform != "darwin":
        return
    script = f"display notification {json.dumps(message[:200])} with title {json.dumps(title[:80])}"
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def defer_to_owner(data: dict[str, Any], reason: str, question: str, config: dict[str, Any]) -> str:
    record = {
        "ts": now_iso(), "session_id": data.get("session_id"), "cwd": data.get("cwd"),
        "reason": reason, "question": question[:4000], "status": "open",
    }
    _append(QUEUE_PATH, record)
    cwd = Path(str(data.get("cwd") or ""))
    if cwd.is_dir() and (cwd / "reports").is_dir():
        try:
            with (cwd / "reports" / "owner_queue.md").open("a", encoding="utf-8") as handle:
                handle.write(f"\n## {record['ts']} — {reason}\n\n{question.strip()[:4000]}\n\nStatus: open (deferred by question_router; session continued)\n")
        except OSError:
            pass
    notify("Claude: owner decision deferred", f"{reason}: {question[:150]}", config)
    return (
        f"DEFERRED_TO_OWNER ({reason}). This decision belongs to Dmitry and is recorded in "
        f"{QUEUE_PATH} (and reports/owner_queue.md when present); he was notified. Do NOT wait "
        "and do NOT ask again. Prepare the gated action completely (exact account/target, amounts "
        "or caps, exact command/button) so Dmitry only has to press it, skip only that step, and "
        "continue all other work of the task. In your final report, list the deferred item with "
        "the one-line instruction 'Готово, нажмите X'."
    )


def log_decision(data: dict[str, Any], event: str, mode: str | None, outcome: str, result: dict[str, Any] | None, question: str) -> None:
    _append(LOG_PATH, {
        "ts": now_iso(), "event": event, "session_id": data.get("session_id"), "cwd": data.get("cwd"),
        "agent_id": data.get("agent_id"), "mode": mode, "outcome": outcome,
        "question_sha": hashlib.sha256(question.encode("utf-8")).hexdigest()[:16],
        "question": question[:500],
        "bio": (result or {}).get("bio"), "advisor": (result or {}).get("advisor"),
        "model": (result or {}).get("model"), "latency_s": (result or {}).get("latency_s"),
        "confidence": (result or {}).get("confidence"), "errors": (result or {}).get("errors"),
        "answers": (result or {}).get("answers"), "verdict": (result or {}).get("verdict"),
    })


HOOK_TIMEOUT_S = 300


def consult(kind: str, brief: str, questions: list[dict[str, Any]] | None, data: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    import advisor_consult
    try:
        budget = float(config.get("budget_s", 240))
    except (TypeError, ValueError):
        budget = 240.0
    # Leave headroom under the hook timeout for notification/logging overhead.
    budget = max(30.0, min(budget, HOOK_TIMEOUT_S - 40))
    return advisor_consult.consult(
        kind, brief, questions=questions, cwd=str(data.get("cwd") or "."), budget_s=budget, config=config,
    )


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _questions(data: dict[str, Any]) -> list[dict[str, Any]]:
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    questions = tool_input.get("questions")
    if isinstance(questions, list):
        return [q for q in questions if isinstance(q, dict)]
    if isinstance(tool_input.get("question"), str):
        return [{"question": tool_input["question"], "options": []}]
    return []


def _question_text(questions: list[dict[str, Any]]) -> str:
    lines = []
    for q in questions:
        lines.append(f"Q: {q.get('question', '')}" + (" (multi-select)" if q.get("multiSelect") else ""))
        for option in q.get("options", []) or []:
            if isinstance(option, dict):
                lines.append(f"  - {option.get('label', '')}: {option.get('description', '')}")
    return "\n".join(lines)


def _answers_map(questions: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, str]:
    """Exact text matches first, then unused answers in order; never reuse one answer."""
    given = [a for a in (result.get("answers") or []) if isinstance(a, dict)]
    used: set[int] = set()
    out: dict[str, str] = {}
    pending: list[str] = []
    for q in questions:
        text = str(q.get("question", ""))
        hit = next((i for i, a in enumerate(given) if i not in used and str(a.get("question", "")).strip() == text.strip()), None)
        if hit is None:
            pending.append(text)
            continue
        used.add(hit)
        out[text] = str(given[hit].get("answer", ""))
    spare = [i for i in range(len(given)) if i not in used]
    for text in pending:
        if spare:
            out[text] = str(given[spare.pop(0)].get("answer", ""))
    if len(out) < len(questions):
        import advisor_consult
        fallback = advisor_consult.deterministic_fallback("questions", [q for q in questions if str(q.get("question", "")) not in out])
        for item in fallback["answers"]:
            out.setdefault(item["question"], item["answer"])
    return out


def _answer_reason(questions: list[dict[str, Any]], result: dict[str, Any]) -> str:
    answers = _answers_map(questions, result)
    lines = [f"- {q}\n  → {a}" for q, a in answers.items()]
    return (
        f"ADVISOR_ANSWER — the session is unattended, so this question was answered by the "
        f"'{result.get('bio')}' advisor ({result.get('advisor')}:{result.get('model')}, confidence "
        f"{result.get('confidence')}), not by Dmitry. Treat it as the decision and continue the task "
        "now; do not re-ask or wait.\n" + "\n".join(lines) +
        f"\nRationale: {str(result.get('rationale', ''))[:1500]}"
    )


def _emit(obj: dict[str, Any]) -> int:
    print(json.dumps(obj, ensure_ascii=False))
    return 0


def _deny(event: str, reason: str) -> int:
    if event == "PermissionRequest":
        return _emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "message": reason}}})
    return _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}})


def _unsafe_result(result: dict[str, Any], questions_text: str) -> str | None:
    """A model answer that itself crosses a hard boundary, or a blind default on risky ground."""
    answers_text = "\n".join(str(a.get("answer", "")) for a in result.get("answers", []) if isinstance(a, dict))
    boundary = hard_boundary(answers_text)
    if boundary:
        return f"advisor_answer_{boundary}"
    if result.get("advisor") == "deterministic" and RISKY_LOOSE.search(questions_text):
        return "advisor_unavailable_risky"
    return None


# ───────────────────────────── handlers ─────────────────────────────

def handle_ask(data: dict[str, Any], config: dict[str, Any], session: dict[str, Any], events: list[dict[str, Any]], mode: str, event: str) -> int:
    questions = _questions(data)
    if not questions:
        return 0
    sid = str(data.get("session_id") or "")
    qtext = _question_text(questions)
    qsha = hashlib.sha256(qtext.encode("utf-8")).hexdigest()[:16]
    cache = session.setdefault("answers", {})
    boundary = hard_boundary(qtext)
    if boundary:
        reason = defer_to_owner(data, boundary, qtext, config)
        log_decision(data, event, mode, f"deferred:{boundary}", None, qtext)
        return _deny(event, reason)
    cached = cache.get(qsha)
    if cached and time.time() - cached.get("t", 0) < 1800:
        result = cached["result"]
        outcome = "answered:cached"
    else:
        brief = build_brief(data, events, "Questions the agent wanted to ask Dmitry (AskUserQuestion):\n" + qtext)
        result = consult("questions", brief, questions, data, config)
        outcome = "answered"
        unsafe = "advisor_deferred" if result.get("defer_to_owner") else _unsafe_result(result, qtext)
        if unsafe:
            reason = defer_to_owner(data, unsafe, qtext + "\nAdvisor: " + str(result.get("rationale", ""))[:800], config)
            log_decision(data, event, mode, f"deferred:{unsafe}", result, qtext)
            save_session(sid, session)
            return _deny(event, reason)
        cache[qsha] = {"t": time.time(), "result": result}
    save_session(sid, session)
    log_decision(data, event, mode, outcome, result, qtext)
    answers = _answers_map(questions, result)
    if event == "PermissionRequest":
        updated = dict(data.get("tool_input") or {})
        updated["answers"] = answers
        return _emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow", "updatedInput": updated}}})
    if config.get("delivery") == "answers":
        updated = dict(data.get("tool_input") or {})
        updated["answers"] = answers
        return _emit({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "permissionDecisionReason": _answer_reason(questions, result), "updatedInput": updated,
        }})
    return _deny(event, _answer_reason(questions, result))


def _plan_source(tool_input: dict[str, Any], events: list[dict[str, Any]]) -> tuple[str, str | None]:
    plan = str(tool_input.get("plan") or "")
    if plan:
        return plan, None
    plan_file = tool_input.get("planFilePath")
    if not isinstance(plan_file, str):
        # Some versions send ExitPlanMode with empty input; the plan path is
        # recorded in the transcript's plan_mode attachment.
        for event in reversed(events):
            attachment = event.get("attachment")
            if isinstance(attachment, dict) and isinstance(attachment.get("planFilePath"), str):
                plan_file = attachment["planFilePath"]
                break
    if isinstance(plan_file, str):
        try:
            return Path(plan_file).read_text(encoding="utf-8", errors="replace"), plan_file
        except OSError:
            return "", plan_file
    return "", None


def handle_plan(data: dict[str, Any], config: dict[str, Any], session: dict[str, Any], events: list[dict[str, Any]], mode: str) -> int:
    sid = str(data.get("session_id") or "")
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    plan, plan_file = _plan_source(tool_input, events)
    revisions = _int(session.get("plan_revisions", 0), 0)
    max_rev = _int(config.get("plan_max_revisions", 2), 2)
    result: dict[str, Any] | None = None
    if plan.strip() and revisions < max_rev:
        brief = build_brief(data, events, "Plan submitted for approval (ExitPlanMode):\n" + plan[:30000])
        result = consult("plan", brief, None, data, config)
    verdict = (result or {}).get("verdict", "approve")
    if verdict == "revise" and revisions < max_rev:
        session["plan_revisions"] = revisions + 1
        save_session(sid, session)
        log_decision(data, "PermissionRequest", mode, "plan:revise", result, plan[:500])
        message = (
            f"PLAN_REVISE by the '{(result or {}).get('bio')}' advisor ({(result or {}).get('advisor')}:"
            f"{(result or {}).get('model')}), not Dmitry — the session is unattended. Revise the plan to "
            f"address this, then call ExitPlanMode again (revision {revisions + 1}/{max_rev}):\n"
            f"{str((result or {}).get('feedback', ''))[:3000]}"
        )
        return _deny("PermissionRequest", message)
    session["plan_revisions"] = 0
    save_session(sid, session)
    deferred = [str(s) for s in ((result or {}).get("defer_steps") or [])]
    boundary = hard_boundary(plan)
    updated = dict(tool_input)
    if deferred or boundary:
        items = deferred or [f"any step matching the hard owner boundary '{boundary}'"]
        defer_to_owner(data, f"plan_steps:{boundary or 'advisor'}", "Plan steps deferred to owner:\n- " + "\n- ".join(items), config)
        note = (
            "\n\n## DEFERRED_TO_OWNER — do NOT execute these steps\n"
            "Approved by the unattended plan-reviewer with these exclusions. Prepare each one completely "
            "(exact account/target, caps, command or button) but leave the final action to Dmitry; "
            "finish all other steps and end with «Готово, нажмите X».\n- " + "\n- ".join(items) + "\n"
        )
        if "plan" in tool_input:
            updated["plan"] = str(tool_input.get("plan") or "") + note
        elif plan_file:
            try:
                with open(plan_file, "a", encoding="utf-8") as handle:
                    handle.write(note)
            except OSError:
                pass
    log_decision(data, "PermissionRequest", mode, "plan:approve" + (":deferred" if deferred or boundary else ""), result, plan[:500])
    return _emit({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow", "updatedInput": updated},
    }})


def _current_turn_text(events: list[dict[str, Any]]) -> str:
    """Assistant text written after the last real user prompt (never an older turn)."""
    texts: list[str] = []
    for event in reversed(events):
        etype = event.get("type")
        content = (event.get("message") or {}).get("content")
        if etype == "user" and not event.get("isMeta"):
            is_prompt = isinstance(content, str) or (
                isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "text" for b in content)
            )
            if is_prompt:
                break
        if etype == "assistant" and isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                texts.append(text)
                break
    return texts[0] if texts else ""


def handle_stop(data: dict[str, Any], config: dict[str, Any], session: dict[str, Any], events: list[dict[str, Any]], mode: str) -> int:
    sid = str(data.get("session_id") or "")
    text = data.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        text = _current_turn_text(events)
    question = trailing_question(text or "")
    if not question:
        session["stop_chain"] = 0
        save_session(sid, session)
        return 0
    now = time.time()
    blocks = [t for t in session.get("stop_blocks", []) if now - t < 3600]
    chain = _int(session.get("stop_chain", 0), 0) if data.get("stop_hook_active") else 0
    if len(blocks) >= _int(config.get("stop_blocks_per_hour", 3), 3) or chain >= 2:
        log_decision(data, "Stop", mode, "stop:cap_reached", None, question)
        notify("Claude session stopped", f"Advisor cap reached; last question: {question[:120]}", config)
        session["stop_chain"] = 0
        save_session(sid, session)
        return 0
    # The boundary check sees the whole message, code included, not just the closing block.
    boundary = hard_boundary(text or "") or hard_boundary(question)
    result: dict[str, Any] | None = None
    if boundary:
        reason = defer_to_owner(data, boundary, question, config)
        outcome = f"deferred:{boundary}"
    else:
        brief = build_brief(data, events, "The agent ended its turn with this question to Dmitry:\n" + question)
        result = consult("questions", brief, [{"question": question, "options": []}], data, config)
        unsafe = "advisor_deferred" if result.get("defer_to_owner") else _unsafe_result(result, question)
        if unsafe:
            reason = defer_to_owner(data, unsafe, question, config)
            outcome = f"deferred:{unsafe}"
        else:
            answer = "; ".join(a.get("answer", "") for a in result.get("answers", []))
            reason = (
                f"ADVISOR_ANSWER — the session is unattended; your closing question was answered by the "
                f"'{result.get('bio')}' advisor ({result.get('advisor')}:{result.get('model')}), not Dmitry. "
                f"Answer: {answer}\nRationale: {str(result.get('rationale', ''))[:1200]}\n"
                "Continue the task now. Do not end the turn on a question again; if the work is finished, "
                "end with a summary (no question)."
            )
            outcome = "stop:answered"
    blocks.append(now)
    session["stop_blocks"] = blocks
    session["stop_chain"] = chain + 1
    save_session(sid, session)
    log_decision(data, "Stop", mode, outcome, result, question)
    return _emit({"decision": "block", "reason": reason})


def session_summary() -> int:
    """Print advisor activity for this project since its previous session start."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        data = {}
    cwd = str((data or {}).get("cwd") or os.getcwd())
    key = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16]
    marker = STATE_DIR / "summary_markers" / key
    try:
        since = marker.read_text(encoding="utf-8").strip()
    except OSError:
        since = ""
    answered = deferred = 0
    try:
        for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("cwd") != cwd or (since and str(rec.get("ts", "")) <= since):
                continue
            outcome = str(rec.get("outcome", ""))
            if outcome.startswith("deferred"):
                deferred += 1
            elif outcome.startswith(("answered", "stop:answered", "plan:")):
                answered += 1
    except OSError:
        pass
    week_ago = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=7)).isoformat(timespec="seconds")
    open_items = 0
    try:
        for line in QUEUE_PATH.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("status") == "open" and rec.get("cwd") == cwd and str(rec.get("ts", "")) >= week_ago:
                open_items += 1
    except OSError:
        pass
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(now_iso(), encoding="utf-8")
    except OSError:
        pass
    if answered or deferred or open_items:
        print(
            f"=== QUESTION ROUTER ===\n  advisor answers since last start: {answered}; deferred to owner: {deferred}; "
            f"open owner-queue items (7d, this project): {open_items}\n  log: {LOG_PATH}\n  queue: {QUEUE_PATH}"
        )
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "session-summary":
        return session_summary()
    if os.environ.get("BOOSTER_ADVISOR_CHILD") == "1" or os.environ.get("CODEX_THREAD_ID"):
        return 0
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    config = load_config()
    if not config.get("enabled", True):
        return 0
    session_id = str(data.get("session_id") or "")
    session = load_session(session_id)
    event = str(data.get("hook_event_name") or "")
    events = transcript_tail(data.get("transcript_path")) if event in ("PreToolUse", "PermissionRequest", "Stop") else []
    mode = effective_mode(data, session, events if events else None)
    if event in ("UserPromptSubmit", "SessionStart"):
        save_session(session_id, session)
        return 0
    if not mode or mode not in set(config.get("modes") or []):
        save_session(session_id, session)
        return 0
    tool = str(data.get("tool_name") or "")
    try:
        if event in ("PreToolUse", "PermissionRequest") and tool == "AskUserQuestion":
            return handle_ask(data, config, session, events, mode, event)
        if event == "PermissionRequest" and tool == "ExitPlanMode":
            return handle_plan(data, config, session, events, mode)
        if event == "Stop":
            return handle_stop(data, config, session, events, mode)
    except Exception as exc:  # noqa: BLE001 — a hook crash must never strand the session silently
        _append(LOG_PATH, {"ts": now_iso(), "event": event, "tool": tool, "outcome": "error", "error": repr(exc)[:500]})
        fallback = (
            "ADVISOR_UNAVAILABLE — the session is unattended and the question router failed. Do not wait for "
            "Dmitry: choose the most reversible option that completes the task, state the assumption in one "
            "line, and continue. Hard owner boundaries (real orders, secrets, prod data deletion, payments, "
            "external messages) stay undone: prepare them and report «Готово, нажмите X»."
        )
        if tool == "AskUserQuestion" and event in ("PreToolUse", "PermissionRequest"):
            return _deny(event, fallback)
        if event == "PermissionRequest" and tool == "ExitPlanMode":
            return _emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
                "behavior": "allow", "updatedInput": dict(data.get("tool_input") or {}),
            }}})
        if event == "Stop" and not data.get("stop_hook_active"):
            return _emit({"decision": "block", "reason": fallback})
        return 0
    save_session(session_id, session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
