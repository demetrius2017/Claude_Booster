#!/usr/bin/env python3
"""Route eligible Lead questions to Fable while preserving user authority.

Purpose:
  Enforce Claude Booster's opt-in Fable autopilot at AskUserQuestion and Stop
  boundaries.  The hook never calls Fable and never fabricates a user answer;
  it denies an eligible question and tells the Lead to obtain a read-only
  Fable decision with explicit provenance.

Contract:
  stdin  — Claude Code hook JSON for PreToolUse/AskUserQuestion or Stop.
  stderr — actionable routing instruction on a denied question.
  exit   — 0 allows the event; 2 denies it and returns control to the Lead.

Files:
  Reads the nearest ``.claude/autopilot.json``.  State must be a JSON object
  with ``enabled``, a nonblank ``north_star``, and nonnegative call counters.

Limitations:
  Classification is deliberately conservative and based on blast radius, not
  a generic "security" keyword. UI acceptance, secrets/real data, persistent
  project or production state, irreversible/external effects, payments, and
  authority expansion always stay with Dmitry. Explicit task-local temporary
  fixtures and sandbox-only reversible changes may be delegated.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from fable_autopilot_state import checkpoint_eligible, complete, project_root, read, release, reserve

MAX_QUESTION_BYTES = 32768
MAX_TRANSCRIPT_TAIL_BYTES = 65536

UI_SUBJECT = re.compile(r"\b(ui|ux|visual|screenshot|layout|pixel|colour|color|font|animation|interaction|интерфейс|визуал|скриншот|макет)\w*\b", re.I)
UI_PERSONAL = re.compile(r"\b(visually|appearance|looks?|preferred|preference|screenshot|личн|визуальн|предпочт)\w*\b", re.I)
UI_ACCEPT = re.compile(r"\b(approve|accept|choose|prefer|confirm|click|tap|drag|submit|утверд|прин|выбер|предпоч|нажм)\w*\b", re.I)
UI_ACTION = re.compile(r"\b(click|tap|drag|submit|нажм|перетащ)\w*\b", re.I)
UI_PERSON = re.compile(r"\b(you|your|dmitry|personally|real browser|ты|тебе|дмитрий|лично)\b", re.I)
REAL_UI_IMPERATIVE = re.compile(
    r"^\s*(?:please\s+)?(?:click|tap|drag|submit|нажм|перетащ)\w*\b.*\b(?:browser|page|button|form|браузер|страниц|кнопк|форм)\w*\b",
    re.I,
)

USER_ONLY = (
    re.compile(r"\b(secret|credential|password|api[ _-]?key|private key)\b", re.I),
    re.compile(r"\b(access|auth|refresh|production|prod|real)\s+token\b|\btoken\s+(value|secret|credential)\b", re.I),
    re.compile(r"\b(irreversible|force[ -]?push|drop table|truncate)\b", re.I),
    re.compile(r"\b(real|user|customer|client|production|prod|persistent|project)\s+(data|file|state|database|db)\b", re.I),
    re.compile(
        r"\b(send|email|publish|post)\s+"
        r"(this|that|the|an?|announcement|message|email|reply|release|article|result)\b",
        re.I,
    ),
    re.compile(r"\b(payment|pay|purchase|charge|refund|place\s+(?:an?\s+)?order)\b", re.I),
    re.compile(r"\b(expand (the )?(scope|authority)|new authority|product intent|north star)\b", re.I),
    re.compile(r"\b(секрет|парол|ключ|токен|необрат|опубликов|отправ|плат[её]ж|оплат)\w*\b", re.I),
    re.compile(r"\b(реальн|пользовательск|клиентск|продакшн|постоянн|проектн)\w*\s+(данн|файл|состояни|баз)\w*\b", re.I),
)

DESTRUCTIVE = re.compile(r"\b(delete|destroy|remove|wipe|purge|recreate|overwrite|reset|format|chmod|move|replace|удал|стер|пересозда|перезапис|сброс|формат|перемест|замен)\w*\b", re.I)
PERSISTENT_INTEGRITY = re.compile(
    r"(?:\b(?:branch|main|master|release|repository|repo|worktree|tracked|persistent|"
    r"source|database|production|prod)\b|\bproject\s+(?:config|configuration|source|data|file|state)\b|"
    r"\b(?:ветк|релиз|репозитор|ворктри|отслеживаем|постоянн|исходник|продакшн)\w*\b|"
    r"\bпроектн\w*\s+(?:конфиг|исходник|данн|файл|состояни)\w*\b)",
    re.I,
)
EPHEMERAL_LOCAL = re.compile(
    r"(?:/tmp(?:/|\b)|\btmp(?:/|\b)|\btemp(?:orary)?\b|\bfixture\b|"
    r"\bsandbox(?:ed| only)?\b|\btask[- ](?:specific|local)\b|"
    r"\bvalidated\s+(?:generated|disposable)\b|\bgenerated\s+(?:file|artifact|fixture)\b|"
    r"\bвременн\w*\b|\bфикстур\w*\b|\bпесочниц\w*\b)",
    re.I,
)


def _load_state(cwd: str) -> tuple[dict[str, Any] | None, str]:
    """Return validated state or an explicit reason it cannot be trusted."""
    try:
        value = read(cwd)
    except FileNotFoundError:
        return None, "state-not-found"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return None, f"state-invalid:{exc}"
    if not value["enabled"]:
        return None, "autopilot-disabled"
    value["_path"] = str(project_root(cwd) / ".claude" / "autopilot.json")
    return value, "ok"


def _question_text(data: dict[str, Any]) -> tuple[str, bool]:
    """Serialize the complete bounded AskUserQuestion decision surface."""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return "", False
    questions = tool_input.get("questions")
    if questions is None and isinstance(tool_input.get("question"), str):
        text = tool_input["question"]
        return text, len(text.encode("utf-8")) > MAX_QUESTION_BYTES
    if not isinstance(questions, list) or not questions:
        return "", False
    parts: list[str] = []
    for item in questions:
        if not isinstance(item, dict):
            return "", True
        for key in ("header", "question"):
            value = item.get(key, "")
            if value and not isinstance(value, str): return "", True
            parts.append(str(value))
        options = item.get("options", [])
        if not isinstance(options, list): return "", True
        for option in options:
            if not isinstance(option, dict): return "", True
            for key in ("label", "description"):
                value = option.get(key, "")
                if value and not isinstance(value, str): return "", True
                parts.append(str(value))
    text = "\n".join(parts)
    return text, len(text.encode("utf-8")) > MAX_QUESTION_BYTES


def _last_assistant_text(path_value: Any) -> tuple[str, bool]:
    """Read the final assistant text from a JSONL transcript, bounded."""
    if not isinstance(path_value, str) or not path_value:
        return "", False
    try:
        path = Path(path_value)
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_TRANSCRIPT_TAIL_BYTES:
                handle.seek(-MAX_TRANSCRIPT_TAIL_BYTES, os.SEEK_END)
            raw_tail = handle.read(MAX_TRANSCRIPT_TAIL_BYTES)
        omitted_prefix = size > MAX_TRANSCRIPT_TAIL_BYTES
        if omitted_prefix:
            newline = raw_tail.find(b"\n")
            if newline < 0:
                return "", True
            raw_tail = raw_tail[newline + 1:]
        text_tail = raw_tail.decode("utf-8")
        lines = text_tail.splitlines()
    except (OSError, UnicodeDecodeError):
        return "", True
    saw_record = False
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return "", True
        saw_record = True
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        text = "\n".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text.strip():
            encoded = text.encode("utf-8")
            if len(encoded) > MAX_QUESTION_BYTES: return "", True
            return text, False
    return "", not saw_record


def _requires_user(text: str) -> bool:
    """Return True for authority boundaries Fable must never cross."""
    if any(pattern.search(text) for pattern in USER_ONLY):
        return True
    if UI_SUBJECT.search(text) and (
        (UI_ACCEPT.search(text) and UI_PERSONAL.search(text)) or
        (UI_ACTION.search(text) and UI_PERSON.search(text))
    ):
        return True
    if REAL_UI_IMPERATIVE.search(text):
        return True
    # Destructive wording is delegable only when the question itself makes the
    # reversible, local, ephemeral scope explicit. Absence of that evidence is
    # USER_REQUIRED; the gate must not infer that a target is harmless.
    if DESTRUCTIVE.search(text) and PERSISTENT_INTEGRITY.search(text):
        return True
    sentences = re.split(r"[;:.!?]", text)
    for sentence in sentences:
        actions = list(DESTRUCTIVE.finditer(sentence))
        for index, action in enumerate(actions):
            end = actions[index + 1].start() if index + 1 < len(actions) else len(sentence)
            target_text = sentence[action.end():end]
            targets = re.split(r"(?:,|&|\+|\b(?:and|plus|as\s+well\s+as|then|but|after|while|using|и|плюс|а\s+также|затем|но|после|пока|используя)\b)", target_text, flags=re.I)
            meaningful = [target.strip() for target in targets if target.strip()]
            # The action must bind tightly to at least one target, and every
            # coordinated object must carry its own ephemeral/local evidence.
            if not meaningful or any(not EPHEMERAL_LOCAL.search(target) for target in meaningful):
                return True
    return False


def _normalized(text: str) -> str:
    """Normalize bilingual matching without changing the original payload."""
    return text.casefold().replace("ё", "е")


def _is_cadence(text: str) -> bool:
    """Return whether text is only a local timing/sequencing decision."""
    normalized = _normalized(text)
    if re.search(
        r"\b(redesign|refactor\w*|schema|architect\w*|approach|strateg\w*|which|"
        r"rewrit\w*|migrat\w*|design|implement\w*|integrat\w*|endpoint\w*|"
        r"feature\w*|auth\w*)\b|\b(редизайн|рефактор|схем|архитектур|"
        r"подход|стратег|какой|какую|какие|перепис|мигрир|дизайн|"
        r"внедр|интеграц|фич)\w*\b",
        normalized,
    ):
        return False
    if re.search(
        r"\b(push|publish|deploy|release|ship)\b|\bmerge\s+(?:to\s+)?"
        r"(?:main|master)\b|\b(пуш|запуш|опублик|депло|релиз|мерж|"
        r"отправ(?:им|ить)?\s+в\s+(?:main|master))\w*\b",
        normalized,
    ):
        return False
    return bool(re.search(
        r"\b(?:start|begin|commit|close out|continue|shall we commit|"
        r"start .*?(?:now|next session|new session)|commit now|"
        r"now or (?:later|next)|next session|new session)\b|"
        r"\b(?:начинаем|начнем|стартуем|закрываемся|продолжаем)\b.*?"
        r"\b(?:сейчас|потом|нов(?:ую|ой)\s+сесси)\w*\b|"
        r"\b(?:сейчас\s+или|нов(?:ую|ой)\s+сесси|коммит\s+сейчас|"
        r"закрываемся\s+сейчас)\w*",
        normalized,
    ))


def _looks_like_question(text: str) -> bool:
    cleaned = re.sub(r"```.*?```", " ", text, flags=re.S)
    cleaned = re.sub(r"`[^`\n]*`", " ", cleaned)
    cleaned = "\n".join(line for line in cleaned.splitlines() if not line.lstrip().startswith(">"))
    if "?" not in cleaned and "？" not in cleaned:
        return False
    cleaned = _normalized(cleaned)
    return bool(re.search(
        r"\b(should|which|do you|would you|may i|can i|want me|want|prefer|shall we|"
        r"what (?:do we|should)|start(?:ing)?|next session|commit now|push now|"
        r"now or (?:later|next)|нужно ли|стоит ли|какой|какую|что делаем|можно ли|"
        r"хочешь|хотите|стартуем|начинаем|начнем|сейчас или|новую сессию|закрываемся)\b",
        cleaned,
    ))


def _allow_user(reason: str) -> int:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "permissionDecisionReason": f"USER_REQUIRED: {reason}; route this decision to Dmitry.",
    }}))
    return 0


def _cli() -> int:
    """Executable reservation lifecycle used after a hook denies a question."""
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    release_p = sub.add_parser("release"); release_p.add_argument("reservation_id"); release_p.add_argument("--reason", required=True)
    decision = sub.add_parser("consult-decision"); decision.add_argument("--prompt-file", required=True)
    checkpoint = sub.add_parser("checkpoint"); checkpoint.add_argument("event", choices=("plan_complete", "first_slice", "final_diff")); checkpoint.add_argument("--prompt-file", required=True)
    phase_event = sub.add_parser("phase-event"); phase_event.add_argument("event", choices=("plan_complete", "first_slice", "final_diff"))
    args = ap.parse_args()
    cwd = os.getcwd()
    try:
        if args.command == "release":
            release(cwd, args.reservation_id, args.reason)
        elif args.command == "consult-decision":
            return _trusted_consult(cwd, "decision", None, Path(args.prompt_file))
        elif args.command == "checkpoint":
            phase = {"plan_complete": "plan_pfd", "first_slice": "implementation_slice", "final_diff": "final_diff"}[args.event]
            return _trusted_consult(cwd, "checkpoint", phase, Path(args.prompt_file))
        else:
            phase = {"plan_complete": "plan_pfd", "first_slice": "implementation_slice", "final_diff": "final_diff"}[args.event]
            if not checkpoint_eligible(cwd, phase):
                return 0
            instruction = (
                f"FABLE_CHECKPOINT_TRIGGER phase={phase}. Run trusted "
                f"fable_autopilot.py checkpoint {args.event} --prompt-file <brief>; "
                "the runner owns reservation, wrapper invocation, hash, and completion."
            )
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": instruction}}))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if getattr(args, "command", "") == "phase-event":
            return 0
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


def _trusted_consult(cwd: str, kind: str, phase: str | None, prompt_file: Path) -> int:
    """Own reserve→wrapper→validated result→complete; callers cannot attest provenance."""
    token: str | None = None
    try:
        token = reserve(cwd, kind, phase)
        prompt = prompt_file.read_bytes()
        if not prompt.strip() or len(prompt) > 131072:
            raise ValueError("prompt file must be nonblank and <=128KiB")
        wrapper = Path(__file__).resolve().with_name("fable_consult.sh")
        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            raise ValueError("canonical installed fable_consult.sh missing or not executable")
        allowed_env = (
            "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
            "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR", "ANTHROPIC_API_KEY",
            "CLAUDE_CONFIG_DIR",
        )
        clean_env = {key: os.environ[key] for key in allowed_env if key in os.environ}
        clean_env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        proc = subprocess.run(
            [str(wrapper)], input=prompt, capture_output=True, timeout=180,
            check=False, env=clean_env,
        )
        if proc.returncode != 0:
            raise ValueError(f"fable wrapper exit={proc.returncode}")
        payload = json.loads(proc.stdout.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Fable typed verdict must be a JSON object")
        binding = token.encode() + b"\0" + (phase or "decision").encode() + b"\0" + proc.stdout
        receipt = {
            "reservation_id": token, "source": "fable_consult.sh", "wrapper_exit_code": 0,
            "verdict": payload.get("verdict"), "directive": payload.get("directive", ""),
            "watchlist": payload.get("watchlist", []),
            "output_sha256": hashlib.sha256(binding).hexdigest(),
        }
        complete(cwd, token, json.dumps(receipt))
        print(json.dumps({"reservation_id": token, "verdict": receipt["verdict"], "output_sha256": receipt["output_sha256"]}))
        return 0
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        if token is not None:
            try:
                release(cwd, token, f"trusted_runner_failure:{exc}")
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


def main() -> int:
    if len(sys.argv) > 1:
        return _cli()
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        sys.stderr.write("autopilot_gate: malformed hook payload; allowing user routing\n")
        return 0
    if not isinstance(data, dict):
        return 0
    state, reason = _load_state(str(data.get("cwd") or ""))
    if state is None:
        if reason not in {"state-not-found", "autopilot-disabled"}:
            sys.stderr.write(f"autopilot_gate: {reason}; allowing question to Dmitry\n")
        return 0
    if state.get("degraded") is True or state["calls_used"] >= state["max_fable_calls"]:
        return 0

    event = str(data.get("hook_event_name") or "")
    tool = str(data.get("tool_name") or "")
    if event == "PreToolUse" or tool == "AskUserQuestion":
        text, truncated = _question_text(data)
    elif event == "Stop" or data.get("stop_hook_active") is not None:
        if data.get("stop_hook_active"):
            return 0
        text, truncated = _last_assistant_text(data.get("transcript_path"))
        if not _looks_like_question(text):
            return 0
    else:
        return 0

    if truncated:
        if event == "Stop" or data.get("stop_hook_active") is not None:
            return 0
        return _allow_user("decision payload exceeded the safe inspection bound")
    if not text:
        return 0
    if _requires_user(text):
        if event == "Stop" or data.get("stop_hook_active") is not None:
            return 0
        return _allow_user("personal UI acceptance or hard authority boundary")

    if _is_cadence(text):
        cadence_reason = (
            "CADENCE: autopilot default — do not ask Dmitry. Proceed now with "
            "the currently planned next roadmap step; if none remains, close out "
            "per handover."
        )
        if event == "Stop" or data.get("stop_hook_active") is not None:
            cadence_reason += (
                " First re-deliver any completed substantive output from this turn, "
                "then continue."
            )
            print(json.dumps({"decision": "block", "reason": cadence_reason}))
        else:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": cadence_reason,
                }
            }))
        return 0

    reason_text = (
        "FABLE_DELEGATE: Do not ask Dmitry. Do not fabricate a 'User answered' "
        "event or synthesize any other user response. "
        "Build a concise Verified Facts Brief, include the "
        f"North Star from {state['_path']}. Run trusted "
        "`fable_autopilot.py consult-decision --prompt-file <brief>`, "
        "which owns wrapper invocation and provenance. Continue with "
        "decision_source=fable_autopilot. If Fable fails, route the original "
        "question to Dmitry. UI acceptance and hard safety "
        "authorization always remain with Dmitry."
    )
    if event == "Stop" or data.get("stop_hook_active") is not None:
        print(json.dumps({"decision": "block", "reason": reason_text}))
    else:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason_text,
            }
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
