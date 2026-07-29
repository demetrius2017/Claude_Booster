#!/usr/bin/env python3
"""
Stop hook: physically block Claude from ending a turn with a forbidden
"Apply? / Proceed? / Want me to?" question or an unfulfilled immediate-action
commitment after a research agent returned.

Purpose:
  pipeline.md and core.md say "don't ask Apply patch?" — soft rules get
  ignored. This hook inspects the transcript's last assistant message
  at Stop time, regex-matches against a forbidden-question vocabulary,
  and exits 2 to REFUSE the stop, forcing Claude to continue with an
  Agent or /supervise spawn. Stop payloads can contain only assistant text,
  so the hook enforces the textual contract ("I am doing X now" cannot be the
  end of the turn); it does not claim to prove whether a tool ran earlier.

Contract:
  stdin  — Stop event JSON {session_id, transcript_path, stop_hook_active,
           cwd, agent_id, agent_type}
  stderr — feedback when blocking
  exit   — 0 allow stop, 2 block stop (Claude continues generating)

Sub-agent auto-skip:
  Claude Code v2.1.114+ passes ``agent_id``/``agent_type`` for sub-agent
  sessions. The "don't ask Apply?" pressure is on the Lead (where a
  research→apply chain is interrupted by a clarifying question). Inside
  a sub-agent the semantic is different — the sub-agent may legitimately
  return "I recommend X; see evidence Y" which can trip the regex on
  the transcript's tail. Auto-skip when agent_id is set.

Forbidden patterns (last assistant message's final ~600 chars only):
  - "Apply patch?", "Apply the fix?", "Apply this?"
  - "Proceed?", "Proceed with X?"
  - "Deploy?", "Deploy now?", "Deploy to prod?"
  - "Want me to X?", "Shall I X?", "Should I (apply|deploy|proceed|commit|push|run|add|remove)?"
  - Multi-option asks: A) / B) / C) or 1) / 2) / 3) + ?
  - "Which option" / "which do you want"
  - Russian: "применить?", "делать?", "запушить?"
  - Immediate first-person action commitments at message end:
    "Запускаю.", "Сейчас проверю.", "I'll run it now."

Bypass (LEAD ONLY — sub-agents cannot self-disable):
  env  CLAUDE_BOOSTER_SKIP_ASK_GATE=1
  flag stop_hook_active=true (Claude Code's own re-entrancy guard — respect it)
  repo <project>/.claude/.ask_gate=off (honoured only in Lead context; if a
       sub-agent writes this file the gate refuses the bypass and logs
       the attempt to ~/.claude/logs/gate_bypass_attempts.jsonl)

  The stderr block message deliberately does NOT mention the bypass file
  path — sub-agents read stderr and adopt it as a fix-recipe.

Decision telemetry:
  Every invocation appends one JSON line to
  ~/.claude/logs/ask_gate_decisions.jsonl with
  {ts, gate, decision, reason, agent_id, agent_type, tool_name, cwd,
  project, session_id, matched_pattern, message_excerpt}. Fail-soft on
  log errors.

Limitations:
  - Regex on last-600-chars; false positives possible on legitimate
    pedagogical questions. Escape hatches available.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

try:
    from _gate_common import (
        ASK_LOG_NAME,
        BYPASS_LOG_NAME,
        DECISION_ALLOW,
        DECISION_AUTO_SKIP,
        DECISION_BLOCK,
        DECISION_BYPASS_HONOURED,
        DECISION_BYPASS_REFUSED,
        append_jsonl,
        find_upward,
        is_subagent_context,
        iso_now,
        project_root_from,
        redact_secrets,
    )
except ImportError:
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    from _gate_common import (  # type: ignore[no-redef]
        ASK_LOG_NAME,
        BYPASS_LOG_NAME,
        DECISION_ALLOW,
        DECISION_AUTO_SKIP,
        DECISION_BLOCK,
        DECISION_BYPASS_HONOURED,
        DECISION_BYPASS_REFUSED,
        append_jsonl,
        find_upward,
        is_subagent_context,
        iso_now,
        project_root_from,
        redact_secrets,
    )

TAIL_SCAN_CHARS = 600

ASK_GATE_FILE_REL = ".claude/.ask_gate"


def _find_ask_gate_flag(cwd_hint: str) -> Optional[Path]:
    """Return the .claude/.ask_gate path if present up the tree."""
    return find_upward(cwd_hint, ASK_GATE_FILE_REL)


def _project_root_for_log(cwd_hint: str) -> str:
    root = project_root_from(cwd_hint)
    if root is not None:
        return root.name
    try:
        return (Path(cwd_hint) if cwd_hint else Path.cwd()).name
    except (FileNotFoundError, OSError):
        return ""

FORBIDDEN_REGEXES = [
    # Direct apply/deploy/proceed questions
    re.compile(r"\bapply\s+(patch|the\s+fix|this|it|now|it\s+now)\b[^.!]*\?", re.I),
    re.compile(r"\bproceed\b[^.!?\n]{0,80}\?", re.I),
    re.compile(r"\bdeploy\b[^.!?\n]{0,80}\?", re.I),

    # "Want me to ... ?" / "Shall I ... ?"
    re.compile(r"\bwant\s+me\s+to\b[^.!?]{0,100}\?", re.I),
    re.compile(r"\bshall\s+i\b[^.!?]{0,100}\?", re.I),

    # "Should I (apply|deploy|proceed|commit|push|run|try|add|remove|fix)...?"
    re.compile(
        r"\bshould\s+i\s+(apply|deploy|proceed|commit|push|run|try|add|remove|fix|patch|merge|restart)\b[^.!?]{0,80}\?",
        re.I,
    ),

    # "Which option" / "which do you want"
    re.compile(r"\bwhich\s+(option|one|path|approach|do\s+you\s+want|would\s+you)\b[^.!?]{0,60}\?", re.I),

    # Multi-option A/B/C patterns ending with ?
    re.compile(r"\(?[aA]\)[^\n]{0,80}\n.*\(?[bB]\)[^\n]{0,80}[^.!?]*\?", re.DOTALL),
    re.compile(r"\n\s*1\)[^\n]{0,80}\n\s*2\)[^\n]{0,80}[^.!?]*\?", re.DOTALL),

    # Russian equivalents (field-log vocabulary)
    re.compile(r"\bделать\s*\?\s*$", re.I | re.M),
    re.compile(r"\bприменить\s+(патч|исправлен)[^.!?]{0,60}\?", re.I),
    re.compile(r"\bзапушить\b[^.!?]{0,60}\?", re.I),
]


def _last_assistant_text(transcript_path: Path) -> str:
    if not transcript_path.exists():
        return ""
    try:
        lines = transcript_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "assistant":
            continue
        msg = evt.get("message") or {}
        content = msg.get("content") or []
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        combined = "\n".join(t for t in texts if t)
        if combined.strip():
            return combined
    return ""


_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)  # triple-backtick fenced block
_BACKTICK_RE = re.compile(r"`[^`\n]*`")          # single-backtick inline span
_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\"verified\"[^{}]*\}", re.DOTALL)  # handover evidence
_BLOCKQUOTE_RE = re.compile(r"(?m)^\s*>\s?.*$")
_QUOTED_SPAN_RE = re.compile(r"«[^»\n]*»|“[^”\n]*”|\"[^\"\n]*\"")

# These patterns are deliberately narrower than a general future-tense
# classifier. They require a first-person action commitment, immediacy, and
# proximity to the end of the message. Completed-action status prose therefore
# remains allowed.
IMMEDIATE_COMMITMENT_REGEXES = [
    re.compile(
        r"(?:^|[.!?\n]\s*)"
        r"(?:(?:дальше|сначала|следующим\s+(?:шагом|кругом))\s+)?"
        r"(?:я\s+)?(?:сейчас\s+)?"
        r"(?:запускаю|проверяю|измеряю|тестирую|применяю|исправляю)"
        r"(?:\s+[^.!?\n]{0,240})?[.!]?\s*$",
        re.I,
    ),
    re.compile(
        r"(?:^|[.!?\n]\s*)"
        r"(?:"
        r"(?:я\s+)?сейчас\s+|"
        r"(?:дальше|сначала|следующим\s+(?:шагом|кругом))\s+(?:я\s+)?"
        r")"
        r"(?:запущу|проверю|измерю|протестирую|применю|исправлю)"
        r"(?:\s+[^.!?\n]{0,240})?[.!]?\s*$",
        re.I,
    ),
    re.compile(
        r"(?:^|[.!?\n]\s*)"
        r"i(?:['’]ll|\s+will)\s+"
        r"(?:run|check|measure|test|verify|start|apply|fix|implement)"
        r"(?:\s+[^.!?\n]{0,80})?\s+(?:now|right\s+now)[.!]?\s*$",
        re.I,
    ),
]


def _strip_quoted_content(text: str) -> str:
    # Code fences and inline-code spans are stripped so that pattern
    # DOCUMENTATION does not trip the gate (a handover literally containing
    # `Apply patch?` as an example of what's blocked should be harmless).
    text = _JSON_BLOCK_RE.sub(" ", text)
    text = _FENCE_RE.sub(" ", text)
    text = _BACKTICK_RE.sub(" ", text)
    text = _BLOCKQUOTE_RE.sub(" ", text)
    text = _QUOTED_SPAN_RE.sub(" ", text)
    return text


def _matches_forbidden(text: str) -> Tuple[bool, str]:
    cleaned = _strip_quoted_content(text)
    tail = cleaned[-TAIL_SCAN_CHARS:] if len(cleaned) > TAIL_SCAN_CHARS else cleaned
    for pat in FORBIDDEN_REGEXES:
        m = pat.search(tail)
        if m:
            return True, m.group(0)[:120]
    for pat in IMMEDIATE_COMMITMENT_REGEXES:
        m = pat.search(tail)
        if m:
            return True, m.group(0).strip()[:120]
    return False, ""


def _repo_disabled(cwd_hint: str) -> bool:
    flag = _find_ask_gate_flag(cwd_hint)
    if flag is None:
        return False
    try:
        return flag.read_text().strip().lower() == "off"
    except OSError:
        return False


def _autopilot_owns_stop(cwd_hint: str) -> bool:
    """Subordinate this legacy Stop classifier to enabled scoped autopilot."""
    try:
        base = Path(cwd_hint or ".").resolve()
        result = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            text=True, capture_output=True, check=True, timeout=3,
        )
        root = Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        root = Path(cwd_hint or ".").resolve()
    path = root / ".claude" / "autopilot.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return bool(state.get("enabled") is True and Path(str(state.get("scope", ""))).resolve() == root)
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return False


def _build_base_record(data: dict) -> dict:
    cwd = data.get("cwd") or ""
    return {
        "ts": iso_now(),
        "gate": "ask",
        "agent_id": data.get("agent_id") or "",
        "agent_type": data.get("agent_type") or "",
        "tool_name": data.get("tool_name") or "",
        "cwd": cwd,
        "project": _project_root_for_log(cwd),
        "session_id": data.get("session_id") or "",
    }


def _extract_messages_tail(data: dict) -> str:
    """Pull the last assistant string from a stdin ``messages`` array.

    Used by test harnesses that pipe the assistant tail directly instead of
    a transcript file. Flat early-continues keep the nesting shallow.
    """
    msgs = data.get("messages") or []
    if not isinstance(msgs, list):
        return ""
    for m in reversed(msgs):
        if not isinstance(m, dict):
            continue
        if m.get("role") != "assistant":
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            return c
    return ""


def main() -> int:
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        raw = ""
    # Fail-closed on malformed/non-dict stdin. An empty dict would skip all
    # subsequent checks (no text → allow) — which is the same silent bypass
    # as a crafted payload that strips the assistant tail.
    parse_ok = True
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        data = {}
        parse_ok = False
    if not isinstance(data, dict):
        data = {}
        parse_ok = False
    if not parse_ok:
        append_jsonl(ASK_LOG_NAME, {
            "ts": iso_now(),
            "gate": "ask",
            "decision": DECISION_BLOCK,
            "reason": "invalid hook payload (malformed or non-dict stdin)",
        })
        sys.stderr.write("ask_gate: malformed hook payload, blocking fail-closed\n")
        return 2

    is_subagent = is_subagent_context(data)
    base = _build_base_record(data)

    if is_subagent:
        # Surveillance: log self-disable attempts even though auto-skip
        # would have allowed the call anyway.
        attempted_bypass = _repo_disabled(data.get("cwd") or "")
        if attempted_bypass:
            append_jsonl(BYPASS_LOG_NAME, {
                **base,
                "decision": DECISION_BYPASS_REFUSED,
                "reason": "sub-agent cannot disable gate",
                "bypass_file": str(_find_ask_gate_flag(data.get("cwd") or "") or ""),
            })
        auto_skip_rec = {
            **base,
            "decision": DECISION_AUTO_SKIP,
            "reason": "sub-agent context (agent_id/agent_type set)",
            "matched_pattern": "",
        }
        if attempted_bypass:
            auto_skip_rec["attempted_bypass"] = True
        append_jsonl(ASK_LOG_NAME, auto_skip_rec)
        return 0

    if data.get("stop_hook_active"):
        append_jsonl(ASK_LOG_NAME, {
            **base,
            "decision": DECISION_ALLOW,
            "reason": "stop_hook_active re-entrancy",
            "matched_pattern": "",
        })
        return 0

    if os.environ.get("CLAUDE_BOOSTER_SKIP_ASK_GATE") == "1":
        append_jsonl(ASK_LOG_NAME, {
            **base,
            "decision": DECISION_ALLOW,
            "reason": "env CLAUDE_BOOSTER_SKIP_ASK_GATE=1",
            "matched_pattern": "",
        })
        return 0

    if _repo_disabled(data.get("cwd") or ""):
        bypass_path = _find_ask_gate_flag(data.get("cwd") or "")
        append_jsonl(BYPASS_LOG_NAME, {
            **base,
            "decision": DECISION_BYPASS_HONOURED,
            "reason": "lead context honoured .ask_gate=off",
            "bypass_file": str(bypass_path or ""),
        })
        append_jsonl(ASK_LOG_NAME, {
            **base,
            "decision": DECISION_BYPASS_HONOURED,
            "reason": ".ask_gate=off (lead)",
            "matched_pattern": "",
            "attempted_bypass": True,
        })
        return 0

    if _autopilot_owns_stop(data.get("cwd") or ""):
        append_jsonl(ASK_LOG_NAME, {
            **base, "decision": DECISION_ALLOW,
            "reason": "subordinate to enabled project-scoped Fable autopilot Stop hook",
            "matched_pattern": "",
        })
        return 0

    transcript_path = data.get("transcript_path")
    text = ""
    if transcript_path:
        text = _last_assistant_text(Path(transcript_path))
    if not text:
        text = _extract_messages_tail(data)

    if not text:
        append_jsonl(ASK_LOG_NAME, {
            **base,
            "decision": DECISION_ALLOW,
            "reason": "no assistant text found",
            "matched_pattern": "",
        })
        return 0

    matched, sample = _matches_forbidden(text)
    if not matched:
        # Allow path: do NOT persist message_excerpt — assistant messages
        # can quote user-pasted secrets, and the log file would become a
        # sensitive-data sink (~/.claude/logs is not encrypted at rest).
        # `matched_pattern: ""` is enough for analytics.
        append_jsonl(ASK_LOG_NAME, {
            **base,
            "decision": DECISION_ALLOW,
            "reason": "no forbidden pattern",
            "matched_pattern": "",
        })
        return 0

    sys.stderr.write(
        f"ask_gate: your last message ended with a forbidden stop pattern "
        f"({sample!r}).\n"
        f"The user pre-approved the full research → apply → verify → commit chain. "
        f"Do NOT stop after asking for permission or promising immediate action.\n"
        f"Spawn an Agent (Explore/Plan/general-purpose) or /supervise to continue."
    )
    # Block path: keep excerpt for post-mortem analysis but redact FIRST
    # (before [:200]) so we don't split a secret mid-match and leave a
    # half-token visible after truncation.
    append_jsonl(ASK_LOG_NAME, {
        **base,
        "decision": DECISION_BLOCK,
        "reason": "matched forbidden pattern",
        "matched_pattern": sample,
        "message_excerpt": redact_secrets(text)[:200],
    })
    return 2


if __name__ == "__main__":
    sys.exit(main())
