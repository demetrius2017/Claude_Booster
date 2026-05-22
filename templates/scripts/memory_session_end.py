#!/usr/bin/env python3
"""Stop hook — process batched events and create session summary.

Назначение:
    При завершении сессии Claude Code читает батч-файл memory_batch_{session_id}.jsonl,
    извлекает error_lessons из упавших команд, классифицирует их по таксономии
    Phase 2b, создаёт session_summary, записывает в БД.

Контракт:
    Вход: JSON на stdin {session_id, transcript_path, cwd, ...}
        CC v2.1.145+ also includes: background_tasks (list), session_crons (list).
    Выход: нет (exit 0 всегда)

Ограничения:
    Никогда не крашит Claude — ошибки логируются, выход 0.

Таксономия (Phase 2b):
    Каждый error_lesson теперь получает category из 11 канонических slug'ов
    (или "unclassified" как fallback) через `_classify_error`. Источник правды:
    ~/.claude/rules/error-taxonomy.md. Правила зеркалят H2-секции из
    ~/.claude/rules/institutional.md. Priority-ordered keyword match.

ENV/Файлы:
    ~/.claude/memory_batch_{session_id}.jsonl — батч от PostToolUse
    ~/.claude/rolling_memory.db — БД
    ~/.claude/rules/error-taxonomy.md — источник таксономии
    ~/.claude/logs/memory_hooks.log — логи
"""

import json
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / ".claude" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "memory_hooks.log"

logger = logging.getLogger("memory_session_end")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    handler = RotatingFileHandler(
        str(LOG_PATH), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    )
    logger.addHandler(handler)


# Phase 2b — error_lesson taxonomy. Source of truth:
# ~/.claude/rules/error-taxonomy.md. Keep this tuple in lock-step with the
# "Rule ordering" section of that doc; every slug must trace back to an H2
# in ~/.claude/rules/institutional.md.
_ERROR_TAXONOMY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("argocd-gitops",    ("argocd", "kubectl", "helm", "kustomize", "kube-api")),
    ("db-asyncpg",       ("asyncpg", "pgbouncer", "prepared statement", "sqlalchemy",
                          "psycopg", "connect_args", "nullpool")),
    ("postgres-vacuum",  ("vacuum", "autovacuum", "dead tuples",
                          "pg_stat_user_tables", "xmin", "idle in transaction")),
    ("nginx-proxy",      ("nginx", "http/2", "http2", "proxy_pass", "upstream",
                          "proxy_set_header", "proxy_cache")),
    ("claude-tooling",   ("rolling_memory", "memory_session", "memory_post_tool",
                          "index_reports", "institutional.md", ".claude/",
                          "pal mcp", "thinkdeep", "subagent", "frontmatter",
                          "consolidate(")),
    ("trading",          ("alpaca", "binance", "broker", "commission", "vwap",
                          "partial fill", "order_id", "reconcile", "nav divergence")),
    ("monitoring-sre",   ("prometheus", "grafana", "oncall", "sre bot",
                          "phantom ip", "alertmanager")),
    ("deploy-cicd",      ("vercel", "next build", " deploy", "ci/cd", "pipeline",
                          ".env", "env var", "github actions", "demetrius2017",
                          "edge cache")),
    # infra-networking matches the CORS/gateway/IBKR-Gateway case BEFORE
    # security-auth — institutional.md places that rule under
    # "Infrastructure / Networking" because the fix is proxy config.
    ("infra-networking", ("docker", "container", "healthcheck", "cap-drop",
                          "alpine", "ipv6", "localhost", "dns", "keepalive",
                          "xray", "socksify", "pppoe", "mikrotik", "cors",
                          "gateway", "tls", "sni", "iptables", "reality",
                          "hkeepaliveperiod")),
    ("security-auth",    ("jwt", "oauth", "sso", "api_key", "api key",
                          "credential", "secret", "token expir", "opsec",
                          "sealedsecret", "externalsecret")),
    ("api-data",         ("websocket", "ws reconnect", "useeffect",
                          "visibilitychange", "swr", "progressive render",
                          "cache-control", "default period", "max_reconnects")),
)


def _classify_error(text: str) -> str:
    """Classify an error_lesson by keyword matching.

    Input is the concatenation of cmd + stderr + cwd + any additional
    context. Returns one of the 11 canonical slugs from
    ``~/.claude/rules/error-taxonomy.md`` or ``"unclassified"`` when no
    rule fires. Priority-ordered, first-match-wins.
    """
    haystack = (text or "").lower()
    if not haystack:
        return "unclassified"
    for slug, keywords in _ERROR_TAXONOMY_RULES:
        for kw in keywords:
            if kw in haystack:
                return slug
    return "unclassified"


def _extract_error_lessons(events: list[dict]) -> list[dict]:
    """Extract error_lesson memories from failed bash commands."""
    lessons = []
    for ev in events:
        if ev.get("event_type") != "bash_error":
            continue
        cmd = ev.get("command", "")[:200]
        stderr = ev.get("stderr", "")[:300]
        exit_code = ev.get("exit_code", "?")
        cwd = ev.get("cwd", "")
        content = f"Command failed (exit {exit_code}): `{cmd}`"
        if stderr:
            content += f"\nError: {stderr}"
        category = _classify_error(f"{cmd}\n{stderr}\n{cwd}")
        lessons.append({
            "content": content,
            "memory_type": "error_lesson",
            "category": category,
            "source": "hook:session_end",
            "related_files": cwd,
        })
    return lessons


def _extract_session_insights(events: list[dict]) -> str:
    """Extract 3 targeted insights from session events (no LLM calls).

    a) Error + root cause: first line of stderr for each bash_error
    b) Decision made: commit messages as decision signals
    c) Remember next time: reference error lessons if any errors occurred
    """
    error_insights: list[str] = []
    decision_insights: list[str] = []
    remember_insight = ""

    has_errors = False
    for ev in events:
        if ev.get("event_type") == "bash_error":
            has_errors = True
            cmd = ev.get("command", "unknown")[:120]
            stderr = ev.get("stderr", "")
            first_line = stderr.split("\n")[0].strip()[:200] if stderr else "no stderr"
            error_insights.append(f"Error: Command `{cmd}` failed: {first_line}")

        elif ev.get("event_type") == "git_commit":
            msg = ev.get("message", "").strip()
            if msg:
                decision_insights.append(f"Decision: {msg[:120]}")

    if has_errors:
        remember_insight = "Remember: see error lessons above"

    lines: list[str] = []
    for item in error_insights[:5]:
        lines.append(f"- {item}")
    for item in decision_insights[:5]:
        lines.append(f"- {item}")
    if remember_insight:
        lines.append(f"- {remember_insight}")

    return "\n".join(lines) if lines else ""


def _build_session_summary(events: list[dict], session_id: str, cwd: str,
                           background_tasks: "list | None" = None,
                           session_crons: "list | None" = None) -> str:
    """Build a brief session summary from batch events."""
    error_count = sum(1 for e in events if e.get("event_type") == "bash_error")
    commit_count = sum(1 for e in events if e.get("event_type") == "git_commit")
    commit_msgs = [e.get("message", "") for e in events if e.get("event_type") == "git_commit"]

    parts = [f"Session {session_id[:8]}"]
    if cwd:
        parts.append(f"in {cwd}")
    if commit_count:
        parts.append(f"{commit_count} commit(s)")
        for msg in commit_msgs[:3]:
            if msg:
                parts.append(f"  - {msg[:80]}")
    if error_count:
        parts.append(f"{error_count} error(s)")
    if isinstance(background_tasks, list) and background_tasks:
        parts.append(f"{len(background_tasks)} background task(s) active at session end")
    if isinstance(session_crons, list) and session_crons:
        parts.append(f"{len(session_crons)} session cron(s) active at session end")
    if not events:
        parts.append("no significant events captured")

    base = "; ".join(parts[:2]) + ". " + ". ".join(parts[2:]) if len(parts) > 2 else "; ".join(parts)

    try:
        insights = _extract_session_insights(events)
    except Exception as e:
        logger.warning(f"insights extraction failed: {e}")
        insights = ""

    if insights:
        return base + "\n\nInsights:\n" + insights
    return base


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    session_id = data.get("session_id", "unknown")
    cwd = data.get("cwd", "")
    _bt = data.get("background_tasks")
    background_tasks = _bt if isinstance(_bt, list) else []
    _sc = data.get("session_crons")
    session_crons = _sc if isinstance(_sc, list) else []

    batch_path = Path.home() / ".claude" / f"memory_batch_{session_id}.jsonl"

    try:
        import rolling_memory

        rolling_memory.init_db()

        # Read batch file
        events: list[dict] = []
        if batch_path.exists():
            with open(batch_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            logger.info("session_end: read %d events from batch", len(events))

        # Extract and store error lessons
        lessons = _extract_error_lessons(events)
        for lesson in lessons:
            rolling_memory.memorize_with_merge(
                content=lesson["content"],
                memory_type=lesson["memory_type"],
                category=lesson.get("category", ""),
                source=lesson.get("source", "hook:session_end"),
                related_files=lesson.get("related_files", ""),
                session_id=session_id,
                scope=cwd if cwd else "global",
            )

        # Create session summary. Use an idempotency key so that session_end
        # re-firing (PostToolUse batching) replaces the prior row for this
        # (session_id, scope) pair instead of accumulating snapshot spam.
        if events or cwd:
            summary = _build_session_summary(events, session_id, cwd,
                                             background_tasks=background_tasks,
                                             session_crons=session_crons)
            summary_scope = cwd if cwd else "global"
            rolling_memory.memorize(
                content=summary,
                memory_type="session_summary",
                source="hook:session_end",
                session_id=session_id,
                scope=summary_scope,
                idempotency_key=f"session_summary:{session_id}:{summary_scope}",
            )

        # Cleanup batch file
        if batch_path.exists():
            batch_path.unlink()
            logger.info("session_end: cleaned up batch file %s", batch_path)

        logger.info("session_end: session=%s errors=%d commits=%d",
                     session_id, len(lessons),
                     sum(1 for e in events if e.get("event_type") == "git_commit"))

    except Exception as e:
        logger.exception("session_end hook failed: %s", e)

    # Always cleanup batch file even if processing failed
    try:
        if batch_path.exists():
            batch_path.unlink()
    except Exception:
        pass

    # Cleanup compact_advisor one-shot marker — prevents stale markers
    # from surfacing in resumed/crashed sessions.
    try:
        if session_id and session_id != "unknown":
            marker = Path.home() / ".claude" / f".compact_recommended_{session_id}"
            if marker.exists():
                marker.unlink()
    except Exception:
        pass  # best-effort cleanup; never block session end


if __name__ == "__main__":
    main()
