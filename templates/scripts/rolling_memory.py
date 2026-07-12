#!/usr/bin/env python3
"""Rolling Memory for Claude Code — SQLite-based persistent memory across sessions.

Назначение:
    Персистентная память между сессиями Claude Code CLI.
    Хранит директивы, фидбэк, уроки из ошибок, решения, контексты проектов.

Контракт:
    Вход: вызовы функций memorize/recall/search/build_context
    Выход: dict/list/str в зависимости от функции
    БД: ~/.claude/rolling_memory.db (SQLite WAL mode)

CLI/Примеры:
    python3 rolling_memory.py stats
    python3 rolling_memory.py list --type directive
    python3 rolling_memory.py search "docker deploy"
    python3 rolling_memory.py memorize --type directive --content "Always use ruff"
    python3 rolling_memory.py context --scope global
    python3 rolling_memory.py forget 42
    python3 rolling_memory.py backup
    python3 rolling_memory.py similar "Docker build failed" --type error_lesson
    python3 rolling_memory.py consolidate --dry-run
    python3 rolling_memory.py consolidate --scope global --type error_lesson

Ограничения:
    - FTS5 требует SQLite >= 3.9.0 (macOS 10.12+)
    - Не потокобезопасен (один writer за раз, WAL помогает с readers)

ENV/Файлы:
    ~/.claude/rolling_memory.db — основная БД
    ~/.claude/rolling_memory.db.bak — бэкап
    ~/.claude/logs/memory_hooks.log — логи хуков
"""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DB_PATH = Path.home() / ".claude" / "rolling_memory.db"
BACKUP_PATH = DB_PATH.with_suffix(".db.bak")
LOG_DIR = Path.home() / ".claude" / "logs"
LOG_PATH = LOG_DIR / "memory_hooks.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("rolling_memory")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    handler = RotatingFileHandler(
        str(LOG_PATH), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    )
    logger.addHandler(handler)

# ---------------------------------------------------------------------------
# Schema version & rolling limits
# ---------------------------------------------------------------------------
# v1 → v2 (2026-04-12): add idempotency_key column to support upsert for
# session_summary rows (session_end re-firing must not spawn duplicate rows).
# v2 → v3 (2026-04-12): add scope column to agent_memory_fts for schema parity
# with the base table and future-proofing. Note: scope filtering in search()
# and _find_similar() still runs on the joined agent_memory.scope column
# (NOT as an FTS5 column filter) because scope values are filesystem paths
# and the default unicode61 tokenizer shreds them on /_-. which would cause
# false positives across projects that share path components.
# v3 → v4 (2026-04-13, Phase 2c): add preserve INTEGER column to agent_memory.
# Rows with preserve=1 (sourced from markdown frontmatter `preserve: true`)
# are skipped by consolidate() so distinct consilium/audit reports cannot be
# fused into a single synthesized row. FTS is NOT touched — preserve is
# metadata, not searchable.
# v4 → v5 (2026-04-18, Q1): add 4 supersession-state columns to agent_memory:
#   status TEXT ('active'|'under_review'|'superseded') — replaces the prose
#     [UNDER REVIEW]/[SUPERSEDED] tags in institutional.md with queryable state.
#   verified_at TEXT — ISO timestamp of last explicit re-verification.
#   superseded_by_id INTEGER — FK to agent_memory.id of the superseder; NULL
#     when status != 'superseded'.
#   resolve_by_date TEXT — ISO date; when status='under_review', this is the
#     deadline surfaced by check_review_ages.py at /start.
# FTS is NOT touched — none of the four are text-searchable. Source:
# consilium_2026-04-18_memory_rearchitecture.md §Q1 verdict.
# v5 → v6 (2026-05-12, model_balancer day-1, partial): initial model_metrics
#   table created with incorrect schema (wrong column names/types). Superseded
#   by v7.
# v6 → v7 (2026-05-12, model_balancer day-1, corrected): drop and recreate
#   model_metrics with exact Artifact Contract schema — ts_utc, per_turn_ms,
#   project_root, nullable task_category/duration_ms. Two composite indexes:
#   (model, ts_utc DESC) and (provider, ts_utc DESC). FTS NOT touched.
# v7 → v8 (2026-05-13, weekly_max_pct live feed): add claude_max_usage table
#   for StopSession token tracking. session_id UNIQUE (upsert via INSERT OR
#   REPLACE). Indexes on session_id and ts_utc for 7-day rolling queries.
#   FTS NOT touched. claude_max_tracker.py (StopSession hook) writes here;
#   model_balancer._get_weekly_max_pct() reads here.
# v8 → v9 (2026-07-03, Fable 5 usage visibility): add immutable
#   fable_usage_events ledger for API-equivalent credit-rate estimates.
#   event_key UNIQUE dedupes repeated Stop hook runs and duplicate transcript
#   rows. Costs are stored as integer USD nanounits so presentation rounding is
#   the only rounding step. FTS NOT touched.
SCHEMA_VERSION = 9

ROLLING_LIMITS = {
    "directive": 50,
    "feedback": 100,
    "session_summary": 30,
    "error_lesson": 100,
    "decision": 200,
    "project_context": 50,  # per scope
}

DEFAULT_PRIORITY = {
    "directive": 100,
    "feedback": 90,
    "session_summary": 80,
    "incident": 95,
    "error_lesson": 70,
    "decision": 50,
    "project_context": 40,
}

INCIDENT_SEVERITY_RANK = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "unknown": 4,
}

# Merge thresholds for compounding pattern
# MERGE requires ALL three: subset >= 0.6, jaccard >= 0.4, shared >= 3
# LINK requires: subset >= 0.35 (any amount of meaningful overlap)
MERGE_SUBSET_THRESHOLD = 0.6
MERGE_JACCARD_THRESHOLD = 0.4
MERGE_MIN_SHARED = 3
MERGE_LINK_THRESHOLD = 0.35

# Cluster thresholds for periodic consolidation.
# Stricter than MERGE because BFS transitivity amplifies weak edges across
# many rows — a false-positive cluster destroys N originals, not 1.
CLUSTER_SUBSET_THRESHOLD = 0.5
CLUSTER_JACCARD_THRESHOLD = 0.3
CLUSTER_MIN_SHARED = 5

# Stopwords for keyword extraction (English + common CLI/code terms).
# Template tokens (commit, insights, decision, cat, eof, docs, feat, fix,
# handover, co-authored-by) are included to prevent boilerplate dominance
# on template-heavy types like session_summary and error_lesson.
_STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would shall should may might can could of in to for on with "
    "at by from as into through during before after above below between "
    "and or but not no nor so yet both either neither each every all "
    "any few more most other some such than too very this that these those "
    "it its he she they them their his her we our you your i me my "
    "session command failed error exit stderr "
    "commit commits insights decision cat eof docs feat fix handover co-authored-by".split()
)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT,
    priority INTEGER DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    scope TEXT DEFAULT 'global',
    category TEXT DEFAULT '',
    source TEXT DEFAULT '',
    related_files TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    expires_at TEXT,
    last_accessed_at TEXT,
    -- Activity counter: increments for retrieval touches and store merges.
    -- The historical name is retained for schema compatibility.
    access_count INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    idempotency_key TEXT,
    preserve INTEGER NOT NULL DEFAULT 0 CHECK(preserve IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','under_review','superseded')),
    verified_at TEXT,
    superseded_by_id INTEGER REFERENCES agent_memory(id) ON DELETE SET NULL,
    resolve_by_date TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_dedup
    ON agent_memory(memory_type, content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_type
    ON agent_memory(memory_type, active, priority DESC);
CREATE INDEX IF NOT EXISTS idx_memory_scope
    ON agent_memory(scope, active, priority DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_idempotency_key
    ON agent_memory(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_status
    ON agent_memory(status, active, priority DESC);
"""

_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts
    USING fts5(content, memory_type, category, scope, content='agent_memory', content_rowid='id');
"""

# Status-demote ORDER BY fragment (consilium Q1 §Retrieval spec).
# Lower = higher rank. Superseded always tied with overdue under_review.
# Source: reports/consilium_2026-04-18_memory_rearchitecture.md lines 77-90.
# Two variants: bare (SELECT FROM agent_memory) vs am.-prefixed (FTS JOIN).
# The ``resolve_by_date IS NOT NULL`` guard makes the intent explicit — a
# malformed under_review row with NULL deadline gets demote=1 (mid-rank),
# not NULL (which would sort as top-priority in SQLite). Correctness is
# identical to the previous implicit form; the guard is for future readers.
_STATUS_DEMOTE = (
    "CASE status "
    "WHEN 'superseded' THEN 2 "
    "WHEN 'under_review' THEN "
    "CASE WHEN resolve_by_date IS NOT NULL AND resolve_by_date < date('now') "
    "THEN 2 ELSE 1 END "
    "ELSE 0 END"
)
_STATUS_DEMOTE_AM = (
    "CASE am.status "
    "WHEN 'superseded' THEN 2 "
    "WHEN 'under_review' THEN "
    "CASE WHEN am.resolve_by_date IS NOT NULL AND am.resolve_by_date < date('now') "
    "THEN 2 ELSE 1 END "
    "ELSE 0 END"
)

_CREATE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS agent_memory_ai AFTER INSERT ON agent_memory BEGIN
    INSERT INTO agent_memory_fts(rowid, content, memory_type, category, scope)
        VALUES (new.id, new.content, new.memory_type, new.category, new.scope);
END;

CREATE TRIGGER IF NOT EXISTS agent_memory_ad AFTER DELETE ON agent_memory BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, content, memory_type, category, scope)
        VALUES('delete', old.id, old.content, old.memory_type, old.category, old.scope);
END;

CREATE TRIGGER IF NOT EXISTS agent_memory_au AFTER UPDATE ON agent_memory BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, content, memory_type, category, scope)
        VALUES('delete', old.id, old.content, old.memory_type, old.category, old.scope);
    INSERT INTO agent_memory_fts(rowid, content, memory_type, category, scope)
        VALUES (new.id, new.content, new.memory_type, new.category, new.scope);
END;
"""

_CREATE_MODEL_METRICS = """
CREATE TABLE IF NOT EXISTS model_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  task_category TEXT,
  duration_ms INTEGER,
  num_turns INTEGER,
  per_turn_ms INTEGER,
  tokens_in INTEGER,
  tokens_out INTEGER,
  success INTEGER NOT NULL DEFAULT 1,
  session_id TEXT,
  project_root TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_metrics_model_ts ON model_metrics(model, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_model_metrics_provider_ts ON model_metrics(provider, ts_utc DESC);
"""

_CREATE_CLAUDE_MAX_USAGE = """
CREATE TABLE IF NOT EXISTS claude_max_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    ts_utc      TEXT    NOT NULL,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    project_root TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claude_max_session
    ON claude_max_usage (session_id);
CREATE INDEX IF NOT EXISTS idx_claude_max_ts
    ON claude_max_usage (ts_utc);
"""

_CREATE_FABLE_USAGE_EVENTS = """
CREATE TABLE IF NOT EXISTS fable_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL,
    assistant_message_id TEXT,
    source_path TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    task_key TEXT NOT NULL DEFAULT '',
    project_root TEXT,
    ts_utc TEXT NOT NULL,
    month_utc TEXT NOT NULL,
    model TEXT NOT NULL,
    canonical_model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cache_creation_5m_tokens >= 0),
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cache_creation_1h_tokens >= 0),
    cache_read_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cache_read_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
    cost_usd_nanos INTEGER NOT NULL DEFAULT 0 CHECK(cost_usd_nanos >= 0),
    pricing_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fable_usage_event_key
    ON fable_usage_events(event_key);
CREATE INDEX IF NOT EXISTS idx_fable_usage_month
    ON fable_usage_events(month_utc, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_fable_usage_task
    ON fable_usage_events(task_key, ts_utc DESC);
CREATE TRIGGER IF NOT EXISTS fable_usage_events_no_update
BEFORE UPDATE ON fable_usage_events
BEGIN
    SELECT RAISE(ABORT, 'fable_usage_events is immutable');
END;
CREATE TRIGGER IF NOT EXISTS fable_usage_events_no_delete
BEFORE DELETE ON fable_usage_events
BEGIN
    SELECT RAISE(ABORT, 'fable_usage_events is immutable');
END;
"""

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    """Open SQLite connection with WAL mode, FK enforcement, Row factory.

    ``PRAGMA foreign_keys=ON`` is per-connection in SQLite (default OFF), so it
    must be set every time we open a connection — otherwise the
    ``superseded_by_id REFERENCES agent_memory(id) ON DELETE SET NULL`` FK
    added in v5 would be cosmetic.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_readonly_connection() -> sqlite3.Connection:
    """Open a strictly read-only SQLite connection via URI mode=ro.

    Unlike :func:`get_connection`, this does **not** create the DB file or
    parent directory when missing — a missing DB raises
    ``sqlite3.OperationalError: unable to open database file``. Used by
    :func:`build_start_context` so the /start lookup honours its "no DB
    writes" contract literally, not just at the SQL layer.
    """
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ---------------------------------------------------------------------------
# Init & migrations
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create tables if not exist, run migrations. Idempotent.

    The v2→v3 FTS migration (and any FTS self-repair) runs inside a single
    explicit transaction so concurrent readers never observe a dropped or
    empty FTS virtual table. ``executescript`` honours the BEGIN/COMMIT
    written into the script text.
    """
    conn = get_connection()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_memory'"
        ).fetchone() is not None
        fts_exists_before = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_memory_fts'"
        ).fetchone() is not None

        # v1 → v2 (idempotency_key column). Must run BEFORE _CREATE_TABLES
        # because _CREATE_TABLES contains `CREATE INDEX ... ON agent_memory
        # (idempotency_key)` which would fail on a pre-migration v1 table.
        if table_exists and version < 2:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_memory)").fetchall()}
            if "idempotency_key" not in cols:
                conn.execute("ALTER TABLE agent_memory ADD COLUMN idempotency_key TEXT")
            conn.commit()
            logger.info("migrated agent_memory to schema version 2 (idempotency_key column)")

        # v3 → v4 (Phase 2c): add preserve column. Runs BEFORE _CREATE_TABLES
        # and the v3 FTS rebuild because ALTER TABLE ADD COLUMN must succeed
        # on the base table first. SQLite ≥ 3.25 accepts a CHECK constraint
        # on ADD COLUMN; system sqlite is 3.51. No FTS rebuild needed —
        # preserve is not text-searchable.
        if table_exists and version < 4:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_memory)").fetchall()}
            if "preserve" not in cols:
                conn.execute(
                    "ALTER TABLE agent_memory ADD COLUMN "
                    "preserve INTEGER NOT NULL DEFAULT 0 CHECK(preserve IN (0, 1))"
                )
            conn.commit()
            logger.info("migrated agent_memory to schema version 4 (preserve column, Phase 2c)")

        # v4 → v5 (Q1, 2026-04-18): add 4 supersession-state columns. None are
        # FTS-searchable so no FTS rebuild needed. Each ALTER is guarded by
        # PRAGMA table_info so a partial prior run or manual seed is a no-op.
        # Adding a NOT NULL column with a constant DEFAULT is cheap in SQLite
        # (no row rewrite) per https://sqlite.org/lang_altertable.html#altertabaddcol.
        if table_exists and version < 5:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_memory)").fetchall()}
            if "status" not in cols:
                conn.execute(
                    "ALTER TABLE agent_memory ADD COLUMN "
                    "status TEXT NOT NULL DEFAULT 'active' "
                    "CHECK(status IN ('active','under_review','superseded'))"
                )
            if "verified_at" not in cols:
                conn.execute("ALTER TABLE agent_memory ADD COLUMN verified_at TEXT")
            if "superseded_by_id" not in cols:
                conn.execute(
                    "ALTER TABLE agent_memory ADD COLUMN superseded_by_id INTEGER "
                    "REFERENCES agent_memory(id) ON DELETE SET NULL"
                )
            if "resolve_by_date" not in cols:
                conn.execute("ALTER TABLE agent_memory ADD COLUMN resolve_by_date TEXT")
            conn.commit()
            logger.info("migrated agent_memory to schema version 5 (supersession state, Q1)")


        # v6 → v7: drop the incorrectly-schemed model_metrics table that was
        # created in v6 (wrong column names: ts, decision_date, duration_per_turn_ms,
        # notes; wrong nullability). Recreate with exact Artifact Contract schema.
        # The table was always empty at this point so DROP is safe.
        if version < 7:
            mm_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_metrics'"
            ).fetchone() is not None
            if mm_exists:
                conn.execute("DROP TABLE IF EXISTS model_metrics")
                conn.commit()
                logger.info("dropped incorrect model_metrics table (v6 schema) for v7 rebuild")

        need_v3_migration = table_exists and version < 3
        # Rebuild FTS from the base table when either:
        #  (a) we are running the v2→v3 migration (FTS schema changed), or
        #  (b) the base table already has data but agent_memory_fts is missing
        #      (backup restored without the FTS, or an external repair step).
        # For a fresh DB (table_exists=False), rebuild is a no-op anyway but we
        # skip it to keep the log clean.
        should_rebuild_fts = need_v3_migration or (table_exists and not fts_exists_before)

        script_parts: list[str] = ["BEGIN IMMEDIATE;\n"]
        if need_v3_migration:
            script_parts.append(
                "DROP TRIGGER IF EXISTS agent_memory_ai;\n"
                "DROP TRIGGER IF EXISTS agent_memory_ad;\n"
                "DROP TRIGGER IF EXISTS agent_memory_au;\n"
                "DROP TABLE IF EXISTS agent_memory_fts;\n"
            )
        script_parts.append(_CREATE_TABLES)
        script_parts.append(_CREATE_FTS)
        script_parts.append(_CREATE_TRIGGERS)
        script_parts.append(_CREATE_MODEL_METRICS)
        script_parts.append(_CREATE_CLAUDE_MAX_USAGE)
        script_parts.append(_CREATE_FABLE_USAGE_EVENTS)
        if should_rebuild_fts:
            script_parts.append(
                "INSERT INTO agent_memory_fts(agent_memory_fts) VALUES('rebuild');\n"
            )
        if version < SCHEMA_VERSION:
            script_parts.append(f"PRAGMA user_version = {SCHEMA_VERSION};\n")
        script_parts.append("COMMIT;\n")

        conn.executescript("".join(script_parts))

        if need_v3_migration:
            logger.info("migrated agent_memory_fts to schema version 3 (scope column, atomic)")
        elif should_rebuild_fts:
            logger.info("repaired missing agent_memory_fts")
        if version < SCHEMA_VERSION:
            if version < 7:
                logger.info("migrated to schema version 7 (model_metrics table, corrected schema)")
            elif version < 8:
                logger.info("migrated to schema version 8 (claude_max_usage table, weekly_max_pct live feed)")
            elif version < 9:
                logger.info("migrated to schema version 9 (fable_usage_events ledger)")
            else:
                logger.info("DB initialized at schema version %d", SCHEMA_VERSION)
    except Exception:
        logger.exception("init_db failed")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def _content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode()).hexdigest()


_VALID_STATUS = ("active", "under_review", "superseded")


def memorize(
    content: str,
    memory_type: str,
    priority: Optional[int] = None,
    scope: str = "global",
    category: str = "",
    source: str = "",
    related_files: str = "",
    metadata: Optional[dict] = None,
    session_id: str = "",
    expires_at: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    preserve: bool = False,
    status: str = "active",
    verified_at: Optional[str] = None,
    superseded_by_id: Optional[int] = None,
    resolve_by_date: Optional[str] = None,
) -> Optional[int]:
    """Store a memory. Returns row id or None if duplicate.

    When ``idempotency_key`` is provided, any existing row with the same key is
    deleted atomically before the insert — so re-firing the same source (e.g.
    session_end hook) replaces the prior row instead of accumulating duplicates.

    ``preserve=True`` marks the row as immune to ``consolidate()``. Used for
    rows indexed from markdown frontmatter with ``preserve: true`` (consilium,
    audit, handover). Phase 2c.

    Q1 (v5) supersession state — all four optional, preserve v4 caller
    behaviour via defaults:
        ``status``          — ``'active'`` | ``'under_review'`` | ``'superseded'``.
        ``verified_at``     — ISO timestamp of last re-verification (NULL until set).
        ``superseded_by_id``— FK to the superseder row's id (NULL until superseded).
        ``resolve_by_date`` — ISO date deadline for ``under_review`` rows.
    """
    if status not in _VALID_STATUS:
        raise ValueError(f"invalid status={status!r}, expected one of {_VALID_STATUS}")
    if status == "under_review":
        if not resolve_by_date:
            raise ValueError("status='under_review' requires a resolve_by_date (ISO YYYY-MM-DD)")
        try:
            datetime.fromisoformat(resolve_by_date).date()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"resolve_by_date must be ISO YYYY-MM-DD, got {resolve_by_date!r}") from exc
    if status != "superseded" and superseded_by_id is not None:
        raise ValueError("superseded_by_id is only valid when status='superseded'")

    if priority is None:
        priority = DEFAULT_PRIORITY.get(memory_type, 50)

    chash = _content_hash(content)
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    preserve_int = 1 if preserve else 0

    conn = get_connection()
    try:
        if idempotency_key is not None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM agent_memory WHERE idempotency_key = ?",
                (idempotency_key,),
            )

        cur = conn.execute(
            """INSERT INTO agent_memory
               (session_id, memory_type, content, content_hash, priority,
                scope, category, source, related_files, metadata_json, expires_at,
                idempotency_key, preserve,
                status, verified_at, superseded_by_id, resolve_by_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, memory_type, content.strip(), chash, priority,
             scope, category, source, related_files, meta_json, expires_at,
             idempotency_key, preserve_int,
             status, verified_at, superseded_by_id, resolve_by_date),
        )
        conn.commit()
        row_id = cur.lastrowid
        logger.info("memorize id=%d type=%s scope=%s", row_id, memory_type, scope)

        # Enforce rolling limits after insert
        trim_rolling(memory_type, scope if memory_type == "project_context" else "", conn=conn)

        return row_id
    except sqlite3.IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("duplicate skipped: type=%s hash=%s", memory_type, chash[:12])
        return None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("memorize failed")
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Compounding: Smart merge at ingestion
# ---------------------------------------------------------------------------

def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords from text for overlap scoring."""
    import re
    tokens = re.findall(r"[a-zA-Z0-9_./:-]{3,}", text.lower())
    return {t for t in tokens if t not in _STOPWORDS and not t.isdigit()}


def _token_overlap(a: set[str], b: set[str]) -> float:
    """Subset-biased overlap: intersection / min(len_a, len_b). For ranking only."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    return intersection / min(len(a), len(b))


def _similarity_metrics(a: set[str], b: set[str]) -> tuple[float, float, int]:
    """Return (subset_score, jaccard, shared_count).

    subset_score: intersection / min(|a|, |b|) — for ranking
    jaccard:      intersection / union — stricter gate for merge decision
    shared_count: absolute count of shared keywords — floor to prevent
                  merging on 1-2 generic tokens like "docker", "build"
    """
    if not a or not b:
        return 0.0, 0.0, 0
    inter = len(a & b)
    subset = inter / min(len(a), len(b))
    jaccard = inter / len(a | b)
    return subset, jaccard, inter


def _fts_quote(term: str) -> str:
    """Escape a term for FTS5 MATCH by double-quoting and doubling internal quotes."""
    return '"' + term.replace('"', '""') + '"'


def _find_similar(
    content: str,
    memory_type: str,
    scope: str,
    limit: int = 5,
) -> list[tuple[dict, float, float, int]]:
    """Find existing memories similar to content using FTS5 + keyword overlap.

    Returns list of (memory_dict, subset_score, jaccard, shared_count) tuples.
    Restricts candidates to same scope + global fallback (prevents cross-project
    contamination of project-specific memories).
    """
    keywords = _extract_keywords(content)
    if not keywords:
        return []

    # Build FTS5 query from top keywords (OR-joined), quoting ALL terms
    query_terms = sorted(keywords, key=len, reverse=True)[:8]
    fts_query = " OR ".join(_fts_quote(t) for t in query_terms)

    conn = get_connection()
    try:
        # Restrict to same scope OR global — never cross-project
        rows = conn.execute(
            """SELECT am.* FROM agent_memory_fts fts
               JOIN agent_memory am ON am.id = fts.rowid
               WHERE agent_memory_fts MATCH ?
                     AND am.active = 1
                     AND am.memory_type = ?
                     AND (am.scope = ? OR am.scope = 'global')
               ORDER BY
                   CASE WHEN am.scope = ? THEN 0 ELSE 1 END,
                   rank
               LIMIT ?""",
            (fts_query, memory_type, scope, scope, limit),
        ).fetchall()

        scored = []
        for r in rows:
            row_dict = dict(r)
            existing_kw = _extract_keywords(row_dict["content"])
            subset, jaccard, shared = _similarity_metrics(keywords, existing_kw)
            scored.append((row_dict, subset, jaccard, shared))

        # Sort by subset score DESC for ranking
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored
    except Exception:
        logger.exception("_find_similar failed")
        return []
    finally:
        conn.close()


MERGE_MAX_LENGTH = 2000  # cap merged content to prevent unbounded growth


def _merge_content(existing: str, new: str) -> str:
    """Merge new content into existing, avoiding pure duplication.

    Caps output at MERGE_MAX_LENGTH — if exceeded, flags for consolidation.
    """
    # Extract lines/facts from new that aren't in existing
    existing_kw = _extract_keywords(existing)
    new_lines = [l.strip() for l in new.split("\n") if l.strip()]
    novel_parts = []
    for line in new_lines:
        line_kw = _extract_keywords(line)
        if line_kw and _token_overlap(line_kw, existing_kw) < 0.8:
            novel_parts.append(line)

    if not novel_parts:
        return existing  # nothing new to add

    merged = existing.rstrip() + "\n[+] " + "; ".join(novel_parts)

    # Cap length — if exceeded, keep head + latest additions, flag for consolidation
    if len(merged) > MERGE_MAX_LENGTH:
        head = existing[:MERGE_MAX_LENGTH // 2].rstrip()
        tail = "; ".join(novel_parts)[:MERGE_MAX_LENGTH // 2 - 50]
        merged = f"{head}\n[...truncated, needs consolidation...]\n[+] {tail}"

    return merged


def memorize_with_merge(
    content: str,
    memory_type: str,
    priority: Optional[int] = None,
    scope: str = "global",
    category: str = "",
    source: str = "",
    related_files: str = "",
    metadata: Optional[dict] = None,
    session_id: str = "",
    expires_at: Optional[str] = None,
    preserve: bool = False,
) -> Optional[int]:
    """Store a memory with compounding: merge into similar if found, else create new.

    Returns row id (new or updated) or None on duplicate/error.

    ``preserve`` is monotonic across merges — once a row is preserved, merging
    a non-preserved payload into it keeps ``preserve=1``. See Phase 2c.
    """
    if priority is None:
        priority = DEFAULT_PRIORITY.get(memory_type, 50)

    preserve_int = 1 if preserve else 0

    # Skip merge for session_summary (too noisy, low value for merging)
    if memory_type == "session_summary":
        return memorize(
            content=content, memory_type=memory_type, priority=priority,
            scope=scope, category=category, source=source,
            related_files=related_files, metadata=metadata,
            session_id=session_id, expires_at=expires_at, preserve=preserve,
        )

    # Find similar existing memories
    try:
        similar = _find_similar(content, memory_type, scope)
    except Exception:
        logger.exception("merge search failed, falling back to plain memorize")
        similar = []

    def _try_merge_into(cand: dict, subset: float, jaccard: float, shared: int) -> Optional[int]:
        """Atomic CAS merge of `content` into `cand`. Returns cand id on success, None otherwise."""
        for _attempt in range(2):
            conn = get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    """SELECT content, content_hash, priority, access_count, preserve
                       FROM agent_memory WHERE id = ? AND active = 1""",
                    (cand["id"],),
                ).fetchone()

                if current is None:
                    conn.rollback()
                    return None  # row gone or deactivated

                merged = _merge_content(current["content"], content)
                new_hash = _content_hash(merged)
                new_priority = max(current["priority"], priority)
                new_access_count = (current["access_count"] or 0) + 1
                # preserve is monotonic: once set to 1, merges cannot clear it.
                new_preserve = max(current["preserve"] or 0, preserve_int)

                cur = conn.execute(
                    """UPDATE agent_memory
                       SET content = ?, content_hash = ?, priority = ?,
                           access_count = ?, source = ?, preserve = ?,
                           last_accessed_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                       WHERE id = ? AND content_hash = ?""",
                    (merged, new_hash, new_priority, new_access_count,
                     source, new_preserve, cand["id"], current["content_hash"]),
                )
                if cur.rowcount == 1:
                    conn.commit()
                    logger.info(
                        "merge (subset=%.2f j=%.2f shared=%d) into id=%d type=%s",
                        subset, jaccard, shared, cand["id"], memory_type,
                    )
                    return cand["id"]
                conn.rollback()  # concurrent update — retry
            except sqlite3.IntegrityError:
                conn.rollback()
                logger.debug("merge hash collision for id=%d, falling back", cand["id"])
                return None
            except Exception:
                logger.exception("merge update failed, falling back to plain insert")
                try:
                    conn.rollback()
                except Exception:
                    pass
                return None
            finally:
                conn.close()
        return None

    # Per-candidate evaluation: iterate all similar rows and pick the first
    # that passes the full 3-way merge gate. This prevents a short generic
    # top-subset candidate from shadowing a later row that would satisfy
    # all three gate conditions.
    link_target: Optional[int] = None
    for cand, subset, jaccard, shared in similar:
        full_gate = (
            subset >= MERGE_SUBSET_THRESHOLD
            and jaccard >= MERGE_JACCARD_THRESHOLD
            and shared >= MERGE_MIN_SHARED
        )
        if full_gate:
            merged_id = _try_merge_into(cand, subset, jaccard, shared)
            if merged_id is not None:
                return merged_id
            # CAS failed — try the next candidate
            continue
        if link_target is None and subset >= MERGE_LINK_THRESHOLD:
            link_target = cand["id"]

    if link_target is not None:
        # PARTIAL overlap — create new but link to first LINK-worthy candidate
        meta = dict(metadata or {})
        meta["related_to"] = link_target
        return memorize(
            content=content, memory_type=memory_type, priority=priority,
            scope=scope, category=category, source=source,
            related_files=related_files, metadata=meta,
            session_id=session_id, expires_at=expires_at, preserve=preserve,
        )

    # No overlap — plain insert
    return memorize(
        content=content, memory_type=memory_type, priority=priority,
        scope=scope, category=category, source=source,
        related_files=related_files, metadata=metadata,
        session_id=session_id, expires_at=expires_at, preserve=preserve,
    )


# ---------------------------------------------------------------------------
# Compounding: Periodic consolidation (requires Claude API)
# ---------------------------------------------------------------------------

def _cluster_gate(a: set[str], b: set[str]) -> bool:
    """Strict 3-way gate for cluster edges — matches the merge-path philosophy.

    An edge exists only if subset, jaccard, AND shared_count all clear their
    thresholds. This prevents template boilerplate (paths, commit markers,
    section headers) from linking genuinely distinct memories.
    """
    subset, jaccard, shared = _similarity_metrics(a, b)
    return (
        subset >= CLUSTER_SUBSET_THRESHOLD
        and jaccard >= CLUSTER_JACCARD_THRESHOLD
        and shared >= CLUSTER_MIN_SHARED
    )


def _cluster_memories(memories: list[dict]) -> list[list[dict]]:
    """Group memories by transitive keyword overlap (BFS over similarity graph).

    Uses the strict 3-way gate (subset + jaccard + shared_count) for edges, and
    re-verifies every non-seed member against the cluster seed after BFS to
    reject "bridge" rows that chain unrelated memories through a template-heavy
    intermediary.
    """
    if not memories:
        return []

    kw_cache = [_extract_keywords(m["content"]) for m in memories]
    n = len(memories)
    visited = [False] * n
    clusters: list[list[dict]] = []

    for start in range(n):
        if visited[start]:
            continue
        # BFS from start — builds the transitive reachability set
        cluster_indices: list[int] = []
        queue = [start]
        visited[start] = True
        while queue:
            i = queue.pop(0)
            cluster_indices.append(i)
            for j in range(n):
                if visited[j]:
                    continue
                if _cluster_gate(kw_cache[i], kw_cache[j]):
                    visited[j] = True
                    queue.append(j)

        # Post-BFS seed re-verification: reject transitive bridges. Rejected
        # rows must be returned to the unvisited pool so they can seed (or be
        # absorbed into) a different cluster on a later iteration of the outer
        # loop — otherwise `A ~ B ~ C` with `A !~ C` would silently lose `C`.
        seed_idx = cluster_indices[0]
        seed_kw = kw_cache[seed_idx]
        verified = [seed_idx]
        for idx in cluster_indices[1:]:
            if _cluster_gate(seed_kw, kw_cache[idx]):
                verified.append(idx)
            else:
                visited[idx] = False
        clusters.append([memories[idx] for idx in verified])

    return clusters


def _get_anthropic_client():
    """Create Anthropic client, trying multiple key sources."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Try reading from Claude Code's config
        config_path = Path.home() / ".claude" / ".credentials.json"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    creds = json.load(f)
                api_key = creds.get("apiKey") or creds.get("api_key")
            except Exception:
                pass
    if not api_key:
        raise RuntimeError(
            "No ANTHROPIC_API_KEY found. Run consolidation with:\n"
            "  ANTHROPIC_API_KEY=sk-... python3 rolling_memory.py consolidate\n"
            "Or from within Claude Code session:\n"
            "  python3 rolling_memory.py consolidate  (Claude injects the key)"
        )
    return anthropic.Anthropic(api_key=api_key)


def consolidate(
    scope: str = "global",
    memory_type: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Consolidate similar memories via Claude Haiku.

    Groups similar memories, synthesizes each cluster into one entry.
    Returns stats: {clusters_found, consolidated, conflicts, errors}.
    """
    # Refuse scope="all" — cross-scope clustering would silently merge memories
    # from different projects into whichever scope happens to win the synthesis.
    # Callers must iterate scopes explicitly.
    if scope == "all":
        raise ValueError(
            "consolidate(scope='all') is unsafe; iterate scopes explicitly"
        )

    # Phase 2c: preserved rows (preserve=1) are filtered out of the cluster
    # input below. consilium/audit rows all carry preserve=1 after
    # index_reports.py runs, so the prior Phase 2a R2-HIGH-1 guard on
    # memory_type is no longer needed — the contract is "consolidate respects
    # the flag, not the type". A user who explicitly inserts a preserve=0
    # audit row gets it considered (proves we're data-driven, not type-driven).

    types_to_consolidate = (
        [memory_type] if memory_type
        else ["error_lesson", "feedback", "decision", "directive"]
    )

    stats = {"clusters_found": 0, "consolidated": 0, "conflicts": [], "errors": 0}

    # For real runs, preflight the Anthropic client BEFORE any mutating recall
    # updates last_accessed_at. Otherwise a missing API key would fail after
    # side effects have already been committed. Dry-run and 0-cluster outcomes
    # of a real run never reach the synthesis path, so it's fine to pay one
    # client construction cost up front.
    client = None
    if not dry_run:
        client = _get_anthropic_client()

    for mtype in types_to_consolidate:
        memories = recall(
            types=[mtype],
            scope=scope,
            limit=200,
            active_only=True,
            touch_access=not dry_run,
        )
        # Phase 2c preserve filter. recall() does not surface preserve yet,
        # so we filter in-place via a direct SELECT of the row ids that
        # carry preserve=1, then drop them from the input list. This keeps
        # recall() semantics unchanged for all other callers.
        if memories:
            ids = tuple(m["id"] for m in memories)
            placeholders = ",".join("?" * len(ids))
            conn_ck = get_connection()
            try:
                preserved_ids = {
                    r[0] for r in conn_ck.execute(
                        f"SELECT id FROM agent_memory WHERE id IN ({placeholders}) AND preserve = 1",
                        ids,
                    ).fetchall()
                }
            finally:
                conn_ck.close()
            if preserved_ids:
                before = len(memories)
                memories = [m for m in memories if m["id"] not in preserved_ids]
                logger.info(
                    "consolidate(%s): filtered %d preserved rows (%d → %d)",
                    mtype, before - len(memories), before, len(memories),
                )
        if len(memories) < 2:
            continue

        clusters = _cluster_memories(memories)
        multi_clusters = [c for c in clusters if len(c) >= 2]
        stats["clusters_found"] += len(multi_clusters)

        for cluster in multi_clusters:
            # Format cluster for LLM
            entries = "\n---\n".join(
                f"[ID:{m['id']} | {m['created_at'][:10]} | p={m['priority']}]\n{m['content']}"
                for m in cluster
            )

            prompt = (
                f"You have {len(cluster)} related '{mtype}' memories from a developer's knowledge base.\n"
                f"Synthesize them into ONE concise memory that preserves ALL unique information.\n"
                f"If any memories CONTRADICT each other, start with CONFLICT: [fact A] vs [fact B]\n"
                f"Then give the synthesized memory anyway (using the most recent fact).\n"
                f"Output ONLY the synthesized memory text, nothing else.\n\n"
                f"{entries}"
            )

            if dry_run:
                print(f"\n[DRY RUN] Cluster ({mtype}, {len(cluster)} entries):")
                for m in cluster:
                    print(f"  #{m['id']}: {m['content'][:80]}")
                continue

            try:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=500,
                    messages=[{"role": "user", "content": prompt}],
                )
                synthesized = response.content[0].text.strip()

                if not synthesized:
                    logger.warning("empty synthesis for cluster %s, skipping",
                                   [m["id"] for m in cluster])
                    stats["errors"] += 1
                    continue

                has_conflict = synthesized.upper().startswith("CONFLICT:")
                if has_conflict:
                    stats["conflicts"].append({
                        "type": mtype,
                        "ids": [m["id"] for m in cluster],
                        "text": synthesized[:200],
                    })

                new_priority = min(100, max(m["priority"] for m in cluster) + 5)
                best_scope = cluster[0].get("scope") or scope

                # ATOMIC: insert synthesized FIRST, then deactivate originals.
                # If insert fails, originals stay active — no data loss.
                conn = get_connection()
                try:
                    conn.execute("BEGIN IMMEDIATE")

                    new_hash = _content_hash(synthesized)
                    meta_json = json.dumps(
                        {"consolidated_from": [m["id"] for m in cluster]},
                        ensure_ascii=False,
                    )

                    cur = conn.execute(
                        """INSERT INTO agent_memory
                           (session_id, memory_type, content, content_hash,
                            priority, scope, source, metadata_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("", mtype, synthesized, new_hash, new_priority,
                         best_scope, "consolidation", meta_json),
                    )
                    new_id = cur.lastrowid

                    conn.executemany(
                        "UPDATE agent_memory SET active = 0 WHERE id = ?",
                        [(m["id"],) for m in cluster],
                    )
                    conn.commit()

                    stats["consolidated"] += 1
                    logger.info(
                        "consolidated %d %s entries (ids=%s) into #%d",
                        len(cluster), mtype,
                        [m["id"] for m in cluster], new_id,
                    )
                except sqlite3.IntegrityError:
                    # Hash collision with existing synthesized — skip, keep originals
                    conn.rollback()
                    logger.info("consolidation hash collision, keeping originals")
                    stats["errors"] += 1
                except Exception:
                    conn.rollback()
                    logger.exception("consolidation DB write failed")
                    stats["errors"] += 1
                finally:
                    conn.close()

            except Exception as e:
                logger.exception("consolidation API call failed: %s", e)
                stats["errors"] += 1

    return stats


def recall(
    types: Optional[list] = None,
    scope: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 50,
    active_only: bool = True,
    touch_access: bool = True,
) -> list[dict]:
    """Retrieve memories matching filters, ordered by priority DESC, created_at DESC.

    Set ``touch_access=False`` to make the call physically read-only — dry-run
    consolidation uses this so the DB file stays byte-identical after a no-op.
    """
    conn = get_connection()
    try:
        clauses = []
        params: list[Any] = []

        if active_only:
            clauses.append("active = 1")
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"memory_type IN ({placeholders})")
            params.extend(types)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if category:
            clauses.append("category = ?")
            params.append(category)

        where = " AND ".join(clauses) if clauses else "1=1"
        sql = f"""SELECT * FROM agent_memory WHERE {where}
                  ORDER BY priority DESC, created_at DESC LIMIT ?"""
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()

        if touch_access:
            ids = [r["id"] for r in rows]
            if ids:
                id_list = ",".join(str(i) for i in ids)
                conn.execute(
                    f"""UPDATE agent_memory SET access_count = access_count + 1,
                        last_accessed_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                        WHERE id IN ({id_list})"""
                )
                conn.commit()

        return [dict(r) for r in rows]
    except Exception:
        logger.exception("recall failed")
        return []
    finally:
        conn.close()


def search(
    query: str,
    limit: int = 20,
    scope: Optional[str] = None,
    include_global: bool = True,
) -> list[dict]:
    """Full-text search via FTS5.

    ``scope`` restricts results to the given scope (and 'global' when
    ``include_global=True``). Default ``scope=None`` returns all scopes —
    same behaviour as before v3. The scope filter is applied on the joined
    ``agent_memory.scope`` column rather than via an FTS5 column filter,
    because scope values are filesystem paths and the default FTS5
    tokenizer splits them on /, _, -, and . — a column filter would match
    on path-component tokens and leak cross-project rows.
    """
    conn = get_connection()
    try:
        sql = (
            """SELECT am.* FROM agent_memory_fts fts
               JOIN agent_memory am ON am.id = fts.rowid
               WHERE agent_memory_fts MATCH ? AND am.active = 1"""
        )
        params: list[Any] = [query]
        exact_scope_first = False
        if scope is not None:
            if include_global and scope != "global":
                sql += " AND (am.scope = ? OR am.scope = 'global')"
                params.append(scope)
                exact_scope_first = True
            else:
                sql += " AND am.scope = ?"
                params.append(scope)
        if exact_scope_first:
            # Prefer exact-scope matches over global fallback, then FTS rank.
            # Mirrors the ordering used by _find_similar so a highly-ranked
            # global row cannot crowd out relevant project-local rows.
            # v5: status demote prepended — superseded and overdue under_review
            # rows are pushed to the tail regardless of scope match (Q1).
            sql += (
                f" ORDER BY {_STATUS_DEMOTE_AM},"
                " CASE WHEN am.scope = ? THEN 0 ELSE 1 END, rank LIMIT ?"
            )
            params.extend([scope, limit])
        else:
            sql += f" ORDER BY {_STATUS_DEMOTE_AM}, rank LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("search failed for query=%s scope=%s", query, scope)
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# D3 — Temporal-causal recon: topic timeline + stuck-loop detector
# ---------------------------------------------------------------------------

# Import STOPWORDS and make_stuck_loop_key from stuck_loop_key — single source of truth.
# No fallback inline copy: stuck_loop_key.py is deployed alongside rolling_memory.py.
# If the import fails here, raising is correct — a silent drift fallback is worse.
import sys as _sys
_scripts_dir = str(Path.home() / ".claude" / "scripts")
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
from stuck_loop_key import STOPWORDS as _STUCK_STOPWORDS, make_stuck_loop_key, extract_first_step_body as _extract_first_step_body_from_key


def _extract_first_step(content: str) -> str:
    """Extract the body of the First-step / Первый шаг section from a handover.

    Delegates to extract_first_step_body() from stuck_loop_key — single source
    of truth for this regex (P1-7: no duplicate implementations).
    Returns empty string when section is absent.
    """
    result = _extract_first_step_body_from_key(content)
    return result if result is not None else ""


def _handover_first_step(path: Path) -> str:
    """Read a handover file and return its First-step body (empty on error)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return _extract_first_step(text)
    except OSError:
        return ""


def _stuck_loop_signal(
    handover_dir: Path,
    project_basename: str,
    window: int = 5,
) -> dict:
    """Analyse last `window` handover files for stuck-loop candidates.

    Returns:
        {
            "warn": bool,
            "hash": str | None,
            "count": int,
            "tokens": list[str],
            "oldest_match_date": str | None,   # date of oldest matching handover
            "since": str | None,               # same as oldest_match_date (alias)
            "no_gate_pass": bool,              # True when no verify_gate=pass found
        }
    """
    import re

    # Detect handover files in the reports/ subdirectory, sorted by mtime DESC.
    reports_dir = handover_dir / "reports"
    if not reports_dir.is_dir():
        reports_dir = handover_dir  # fallback: scope root
    try:
        candidates = sorted(
            (f for f in reports_dir.glob("handover_*.md") if f.is_file()),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )[:window]
    except OSError:
        return {"warn": False, "hash": None, "count": 0, "tokens": [],
                "oldest_match_date": None, "since": None, "no_gate_pass": True}

    if not candidates:
        return {"warn": False, "hash": None, "count": 0, "tokens": [],
                "oldest_match_date": None, "since": None, "no_gate_pass": True}

    # Compute hash for each handover that has a first-step body.
    hash_entries: list[tuple[str, dict, Path]] = []  # (hash, key_result, path)
    for f in candidates:
        body = _handover_first_step(f)
        if not body:
            continue
        key = make_stuck_loop_key(body, context_anchors=(project_basename,))
        hash_entries.append((key["hash"], key, f))

    if not hash_entries:
        return {"warn": False, "hash": None, "count": 0, "tokens": [],
                "oldest_match_date": None, "since": None, "no_gate_pass": True}

    # Find the most-frequent hash.
    from collections import Counter
    counts = Counter(h for h, _, _ in hash_entries)
    most_common_hash, most_common_count = counts.most_common(1)[0]

    if most_common_count < 3:
        return {"warn": False, "hash": most_common_hash, "count": most_common_count,
                "tokens": [], "oldest_match_date": None, "since": None, "no_gate_pass": True}

    # Gather tokens and oldest match date.
    matching = [(h, key, f) for h, key, f in hash_entries if h == most_common_hash]
    tokens: list[str] = []
    for _, key, _ in matching:
        for t in key["tokens"]:
            if t not in tokens:
                tokens.append(t)
    tokens = tokens[:20]

    # Oldest match = last in the list (they are already mtime-sorted newest-first).
    oldest_path = matching[-1][2]
    # Parse date from filename: handover_YYYY-MM-DD_HHMMSS.md
    date_match = re.search(r"handover_(\d{4}-\d{2}-\d{2})", oldest_path.name)
    oldest_date = date_match.group(1) if date_match else oldest_path.name[:10]

    # Check verify_gate=pass references for this hash across the FULL window.
    # P0-2: require both "status":"pass" AND "stuck_hash":"<hash>" in the SAME
    # JSON-ish block — cross-block matches don't count.
    # C1: scan ALL candidates (not only `matching`). A handover whose own
    # First-step hashed to a different value can still carry the verify_gate
    # clearance for an earlier hash — telemetry already scans the full window
    # text, and rolling_memory must do the same to stay consistent.
    no_gate_pass = True
    try:
        from _gate_common import has_pass_for_hash as _has_pass_for_hash_rm
    except ImportError:
        _has_pass_for_hash_rm = None

    for f in candidates:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            if _has_pass_for_hash_rm is not None:
                if _has_pass_for_hash_rm(text, most_common_hash):
                    no_gate_pass = False
                    break
            else:
                # _gate_common unavailable — conservative: leave no_gate_pass=True.
                pass
        except OSError:
            pass

    warn = most_common_count >= 3 and no_gate_pass
    return {
        "warn": warn,
        "hash": most_common_hash,
        "count": most_common_count,
        "tokens": tokens,
        "oldest_match_date": oldest_date,
        "since": oldest_date,
        "no_gate_pass": no_gate_pass,
    }


def _render_stuck_loop_block(signal: dict) -> str:
    """Render the STUCK LOOP CANDIDATE prose block from a signal dict.

    Returns empty string if ``signal['warn']`` is False (no candidate).
    """
    if not signal.get("warn"):
        return ""
    h = signal["hash"] or "?"
    raw_tokens = signal.get("tokens") or []
    tokens_str = ", ".join(raw_tokens) if raw_tokens else "(code-only / empty canonical; low semantic confidence)"
    count = signal.get("count", 0)
    since = signal.get("since") or "unknown"
    lines = [
        "=== STUCK LOOP CANDIDATE ===",
        f"Hash: {h}",
        f"Tokens: {tokens_str}",
        f"Appearances: {count}/5 last handovers (since {since})",
        "No verify_gate=pass evidence references this hash.",
        "",
        "Reframe questions Claude MUST answer in plan:",
        "  Q1: Is this still the right problem? Symptom?",
        "  Q2: Is the root upstream? What did each fix attempt assume about layer N-1?",
        "  Q3: What did the original framing miss? Read the FIRST handover that",
        "      surfaced this hash; name the assumption now invalidated.",
        "  Q4: Is the acceptance criterion still meaningful?",
        "",
        "If answers don't justify continuing — tag [SUPERSEDED by <id>] in rolling_memory.",
    ]
    return "\n".join(lines)


def build_topic_timeline(
    conn: sqlite3.Connection,
    seed_text: str,
    git_paths: list,
    limit: int = 6,
) -> tuple:
    """Build a chronological topic timeline from consilium/audit rows.

    Contract:
        Вход:
            conn       — read-only SQLite connection (agent_memory schema).
            seed_text  — text seed for topic-keyword extraction (e.g. first-step body).
            git_paths  — list of changed file paths (git status --porcelain basenames).
            limit      — max rows to return (default 6).
        Выход:
            (timeline_rows, stale_msg | None, topic_match_count)
            timeline_rows:     list of dicts with keys from agent_memory schema.
            stale_msg:         string warning if topic is stale, else None.
            topic_match_count: int — number of rows returned by the FTS5 topic query.
                               When 0, the caller MUST suppress the TOPIC TIMELINE block
                               (recency floor alone does not justify showing the block).

    Algorithm (spec §D3b):
        1. Extract top-5 keywords from seed_text + path basenames.
        2. FTS5 query with hyphen-fixed keywords (spec §6 R3: '-' → ' ' before MATCH).
        3. Recency floor: union with last 4 consilium/audit rows from last 21 days,
           BUT ONLY when topic_match_count ≥ 1 (provides recency anchor, not sole content).
        4. Dedupe, sort ASC by created_at, prune to limit (oldest 2 + newest limit-2).
        5. Stale flag: len ≥ 5 AND span_days ≥ 14 AND ≥ 3 open (status != 'superseded').
    """
    import re
    from datetime import datetime, timezone

    # Step 1 — extract topic keywords (shared helper: P1-6 lift).
    top_keywords = _top_keywords(seed_text, git_paths, n=5)

    # Step 2 — FTS5 query. Fix hyphen tokens: 'newest-block-wins' → 'newest block wins'.
    def _fts_fix_kw(kw: str) -> str:
        """Normalize keyword for FTS5: replace hyphens with spaces, strip punctuation."""
        kw = kw.replace("-", " ").replace("_", " ").strip(" .,;:")
        return kw.strip()

    topic_rows: list[dict] = []
    if top_keywords:
        # Build MATCH expression: quote all tokens (single-word too, for FTS5 safety).
        parts = []
        for kw in top_keywords:
            fixed = _fts_fix_kw(kw)
            if not fixed:
                continue
            parts.append(f'"{fixed}"')
        fts_match = " OR ".join(parts) if parts else None

        if fts_match:
            try:
                sql = """
                    SELECT am.id, am.content, am.created_at, am.status,
                           am.memory_type, am.source, am.category
                    FROM agent_memory_fts fts
                    JOIN agent_memory am ON am.id = fts.rowid
                    WHERE agent_memory_fts MATCH ?
                      AND am.active = 1
                      AND am.memory_type IN ('consilium','audit')
                    ORDER BY
                      CASE am.status WHEN 'superseded' THEN 2 WHEN 'under_review' THEN 1 ELSE 0 END,
                      am.created_at ASC
                    LIMIT 12
                """
                topic_rows = [dict(r) for r in conn.execute(sql, (fts_match,)).fetchall()]
            except sqlite3.OperationalError as exc:
                logger.warning("build_topic_timeline FTS5 query failed: %s | match=%s", exc, fts_match)
                topic_rows = []

    topic_match_count = len(topic_rows)

    # P0-3: When topic_match_count == 0, suppress the timeline entirely.
    # Recency floor alone does not justify showing causal labels (Premise/Tried/etc.)
    # because those labels imply topic continuity that FTS5 has not confirmed.
    if topic_match_count == 0:
        return [], None, 0

    # Step 3 — recency floor: last 4 consilium/audit rows from last 21 days.
    # Only included when ≥1 topic match exists (provides recency anchor).
    recency_rows: list[dict] = []
    try:
        recency_sql = """
            SELECT id, content, created_at, status, memory_type, source, category
            FROM agent_memory
            WHERE active = 1
              AND memory_type IN ('consilium','audit')
              AND created_at >= datetime('now', '-21 days')
            ORDER BY created_at DESC
            LIMIT 4
        """
        recency_rows = [dict(r) for r in conn.execute(recency_sql).fetchall()]
    except sqlite3.OperationalError as exc:
        logger.warning("build_topic_timeline recency query failed: %s", exc)

    # Step 4 — dedupe by id, sort ASC, prune to limit (oldest 2 + newest limit-2).
    seen_ids: set = set()
    combined_rows: list[dict] = []
    for r in topic_rows + recency_rows:
        rid = r.get("id")
        if rid not in seen_ids:
            seen_ids.add(rid)
            combined_rows.append(r)

    combined_rows.sort(key=lambda r: r.get("created_at") or "")

    if len(combined_rows) > limit:
        oldest_2 = combined_rows[:2]
        newest_rest = combined_rows[-(limit - 2):]
        # Merge avoiding duplicates.
        ids_in_oldest = {r["id"] for r in oldest_2}
        pruned = oldest_2 + [r for r in newest_rest if r["id"] not in ids_in_oldest]
        combined_rows = pruned

    # Step 5 — stale flag.
    stale_msg: Optional[str] = None
    if len(combined_rows) >= 5:
        dates = [r.get("created_at") or "" for r in combined_rows if r.get("created_at")]
        if len(dates) >= 2:
            try:
                def _parse_dt(s: str) -> datetime:
                    # Handle both 'YYYY-MM-DDTHH:MM:SSZ' and 'YYYY-MM-DD' forms.
                    s = s.rstrip("Z").replace("T", " ")
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                        try:
                            return datetime.strptime(s, fmt)
                        except ValueError:
                            pass
                    return datetime.min

                min_dt = _parse_dt(min(dates))
                max_dt = _parse_dt(max(dates))
                span_days = (max_dt - min_dt).days
                # P1-5: use open_count (not len(combined_rows)) in stale_msg.
                open_count = sum(
                    1 for r in combined_rows if (r.get("status") or "active") != "superseded"
                )
                if span_days >= 14 and open_count >= 3:
                    stale_msg = f"{span_days} days span, {open_count} entries unresolved"
            except Exception:
                pass

    return combined_rows, stale_msg, topic_match_count


def _row_title(row: dict, max_len: int = 80) -> str:
    """Extract display title from a consilium/audit row's content.

    Skips YAML frontmatter (--- blocks), returns first non-empty content line,
    truncated to max_len.
    """
    content = row.get("content") or ""
    lines = content.split("\n")
    in_frontmatter = False
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if stripped.startswith("# "):
            return stripped[2:].strip()[:max_len]
        if stripped:
            return stripped[:max_len]
    src = row.get("source") or ""
    return Path(src).name[:max_len] if src else "(untitled)"[:max_len]


def _render_topic_timeline(rows: list, keywords: list, stale_msg: Optional[str]) -> str:
    """Render the TOPIC TIMELINE prose block.

    Label assignment (spec §D3c):
        2 rows:  Premise + Current
        3 rows:  Premise + Tried + Current
        4 rows:  Premise + Tried + Result + Current
        5 rows:  Premise + Tried + Result + Tried + Current
        6 rows:  Premise + Tried + Result + Tried + Tried + Current

    Row labels are assigned by index, not by status.
    OPEN line appears when any row has status != 'superseded'.
    """
    if len(rows) < 2:
        return ""

    n = len(rows)
    # Label sequences per spec.
    if n == 2:
        labels = ["Premise", "Current"]
    elif n == 3:
        labels = ["Premise", "Tried  ", "Current"]
    elif n == 4:
        labels = ["Premise", "Tried  ", "Result ", "Current"]
    elif n == 5:
        labels = ["Premise", "Tried  ", "Result ", "Tried  ", "Current"]
    else:  # 6+
        labels = ["Premise", "Tried  ", "Result ", "Tried  ", "Tried  ", "Current"]
        rows = rows[:6]  # cap at 6

    kw_str = ", ".join(keywords[:3]) if keywords else "?"
    header = f'=== TOPIC TIMELINE: "{kw_str}" ==='

    lines = [header]
    for label, row in zip(labels, rows):
        date = (row.get("created_at") or "")[:10]
        title = _row_title(row)
        lines.append(f"{label} ({date}): {title}")

    # OPEN line: surface under_review rows or open (non-superseded) rows.
    under_review = [r for r in rows if (r.get("status") or "active") == "under_review"]
    open_rows = [r for r in rows if (r.get("status") or "active") != "superseded"]
    if under_review:
        open_titles = "; ".join(_row_title(r, 40) for r in under_review[:2])
        lines.append(f"OPEN: {open_titles} [under_review]")
    elif len(open_rows) == len(rows):
        lines.append(f"OPEN: all {len(open_rows)} entries unresolved")
    elif open_rows:
        lines.append(f"OPEN: {len(open_rows)} of {len(rows)} unresolved")

    if stale_msg:
        lines.append(f"⚠ STALE-TOPIC ({stale_msg}) — recommend /consilium pivot")

    return "\n".join(lines)


def _top_keywords(seed_text: str, git_paths: list, n: int = 5) -> list:
    """Extract top-N topic keywords from seed_text + path basenames.

    P1-6: Lifted from build_topic_timeline() Step 1 and
    _extract_topic_keywords_for_display() to eliminate duplication.
    Both callers now use this single implementation.
    """
    import re as _re
    combined = seed_text + " " + " ".join(Path(p).name for p in git_paths[:10])
    combined = combined.lower()
    raw = _re.findall(r"[a-z][a-z0-9_./-]{3,}", combined)
    all_stops = _STUCK_STOPWORDS | _STOPWORDS
    filtered = [t for t in raw if t not in all_stops and not t.isdigit()]
    freq: dict = {}
    for t in filtered:
        freq[t] = freq.get(t, 0) + 1
    return sorted(freq, key=lambda k: -freq[k])[:n]


def _extract_topic_keywords_for_display(seed_text: str, git_paths: list) -> list:
    """Extract top-5 topic keywords from seed_text + paths (for display in timeline header).

    Delegates to _top_keywords — P1-6: no duplicate implementation.
    """
    return _top_keywords(seed_text, git_paths, n=5)


def _category_from_scope(scope: Optional[str]) -> Optional[str]:
    """Derive an `index_reports.py` category from a scope path.

    Indexed reports store ``scope='global'`` and the project directory name in
    ``category`` (see ``index_reports._project_category``). To bias /start
    lookups toward the current project we mirror that mapping by walking
    upward from ``scope`` to the first ancestor that owns a ``reports/`` or
    ``audits/`` directory — that ancestor is the indexed project root, and
    its basename is the category. This means a subdirectory scope like
    ``~/Projects/horizon/src`` still resolves to ``horizon``.

    Returns ``None`` for ``None``/``"global"``/empty scopes, anything outside
    ``~/Projects``, or scopes whose ancestry contains no indexed project
    (graceful no-bias fallthrough).
    """
    if not scope or scope == "global":
        return None
    try:
        p = Path(scope).expanduser().resolve()
        projects_root = (Path.home() / "Projects").resolve()
        p.relative_to(projects_root)
    except (OSError, ValueError):
        return None
    cur = p
    while True:
        if cur == projects_root:
            return None
        try:
            if (cur / "reports").is_dir() or (cur / "audits").is_dir():
                return cur.name
        except OSError:
            pass
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def _fetch_start_context(
    scope: Optional[str],
    query: Optional[str],
    limit: int,
) -> tuple[Optional[list[dict]], Optional[str], Optional[str]]:
    """Shared data fetch for build_start_context (prose) + JSON CLI.

    Returns ``(rows, category, err_msg)``.
    ``rows``     : list of dicts, or ``None`` on unrecoverable error.
    ``category`` : derived project category (may be ``None``).
    ``err_msg``  : prose-friendly error when the DB/query failed (or ``None``).

    When ``err_msg`` is set, callers should surface it to the user instead of
    treating ``rows=None`` as "empty result". This mirrors the explicit
    error-message behaviour the prose formatter had before the extraction.
    """
    category = _category_from_scope(scope)
    try:
        conn = get_readonly_connection()
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "unable to open" in msg or "no such file" in msg:
            logger.warning("_fetch_start_context: DB file missing — %s", exc)
            return (
                None,
                category,
                "DB not initialized. Run `python ~/.claude/scripts/index_reports.py` once to bootstrap.",
            )
        logger.exception("_fetch_start_context: unexpected connect error")
        return None, category, str(exc)
    try:
        if query:
            sql = (
                """SELECT am.* FROM agent_memory_fts fts
                   JOIN agent_memory am ON am.id = fts.rowid
                   WHERE agent_memory_fts MATCH ? AND am.active = 1
                     AND am.memory_type IN ('consilium','audit')"""
            )
            params: list[Any] = [query]
            if category:
                sql += (
                    f" ORDER BY {_STATUS_DEMOTE_AM},"
                    " CASE WHEN am.category = ? THEN 0 ELSE 1 END, rank LIMIT ?"
                )
                params.extend([category, limit])
            else:
                sql += f" ORDER BY {_STATUS_DEMOTE_AM}, rank LIMIT ?"
                params.append(limit)
        else:
            sql = (
                """SELECT * FROM agent_memory
                   WHERE active = 1 AND memory_type IN ('consilium','audit')"""
            )
            params = []
            if category:
                sql += (
                    f" ORDER BY {_STATUS_DEMOTE},"
                    " CASE WHEN category = ? THEN 0 ELSE 1 END,"
                    " priority DESC, created_at DESC LIMIT ?"
                )
                params.extend([category, limit])
            else:
                sql += (
                    f" ORDER BY {_STATUS_DEMOTE},"
                    " priority DESC, created_at DESC LIMIT ?"
                )
                params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError as exc:
        # Two distinct failure modes need distinct messages so a /start
        # invocation isn't silently confused with an empty result set:
        #   1. FTS5 syntax errors on user-supplied --query (round 2 lesson:
        #      malformed input must surface, not be swallowed).
        #   2. Missing tables — happens when start-context is run against an
        #      uninitialized DB (we exempt the CLI from init_db() now, so this
        #      path is reachable on a fresh install or restored backup).
        msg = str(exc)
        msg_lower = msg.lower()
        if query and "fts5" in msg_lower:
            logger.warning("_fetch_start_context FTS5 query rejected: %s", msg)
            return None, category, f"invalid FTS5 query {query!r}: {msg}"
        if "no such table" in msg_lower:
            logger.warning("_fetch_start_context: DB not initialized — %s", msg)
            return (
                None,
                category,
                "DB not initialized. Run `python ~/.claude/scripts/index_reports.py` once to bootstrap.",
            )
        logger.exception("_fetch_start_context failed scope=%s query=%s", scope, query)
        return None, category, msg
    except Exception as exc:
        logger.exception("_fetch_start_context failed scope=%s query=%s", scope, query)
        return None, category, str(exc)
    finally:
        conn.close()

    return rows, category, None


def _row_metadata(row: dict) -> dict:
    raw = row.get("metadata_json") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _incident_severity(row: dict) -> str:
    severity = str(_row_metadata(row).get("severity") or "").strip().lower()
    return severity if severity in INCIDENT_SEVERITY_RANK else "unknown"


def _incident_sort_key(row: dict) -> tuple[int, str, int]:
    created_at = str(row.get("created_at") or "")
    row_id = int(row.get("id") or 0)
    # Negative codepoints give deterministic descending timestamp order while
    # keeping the overall sort ascending by severity rank.
    inverted_created_at = "".join(chr(0x10FFFF - ord(ch)) for ch in created_at)
    return (INCIDENT_SEVERITY_RANK[_incident_severity(row)], inverted_created_at, row_id)


def _incident_warning_bucket(
    row: dict,
    category: Optional[str],
    query_match_ids: set[int],
) -> int:
    """Rank incident relevance buckets before severity.

    Same-bucket ordering still comes from ``_incident_sort_key`` so
    critical/high/medium/low remains stable within current-project, query-hit,
    and unrelated buckets.
    """
    is_category_match = bool(category and row.get("category") == category)
    try:
        row_id = int(row.get("id") or 0)
    except (TypeError, ValueError):
        row_id = 0
    is_query_match = bool(query_match_ids and row_id in query_match_ids)
    if is_category_match and is_query_match:
        return 0
    if is_category_match:
        return 1
    if is_query_match:
        return 2
    return 3


def _fetch_incident_warnings(
    scope: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 5,
) -> tuple[Optional[list[dict]], Optional[str]]:
    """Fetch category-biased/query-aware incident rows without mutating the DB."""
    category = _category_from_scope(scope)
    try:
        conn = get_readonly_connection()
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "unable to open" in msg or "no such file" in msg:
            return [], None
        logger.exception("_fetch_incident_warnings: unexpected connect error")
        return None, str(exc)
    try:
        query_match_ids: set[int] = set()
        if query:
            try:
                query_match_ids = {
                    int(r["id"])
                    for r in conn.execute(
                        """SELECT am.id FROM agent_memory_fts fts
                           JOIN agent_memory am ON am.id = fts.rowid
                           WHERE agent_memory_fts MATCH ?
                             AND am.active = 1
                             AND am.memory_type = 'incident'""",
                        (query,),
                    ).fetchall()
                }
            except sqlite3.OperationalError as exc:
                msg = str(exc)
                if "fts5" in msg.lower():
                    logger.warning("_fetch_incident_warnings FTS5 query rejected: %s", msg)
                    return None, f"invalid FTS5 query {query!r}: {msg}"
                raise

        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM agent_memory
                   WHERE active = 1 AND memory_type = 'incident'
                   ORDER BY created_at DESC, id ASC"""
            ).fetchall()
        ]
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "no such table" in msg:
            return [], None
        logger.exception("_fetch_incident_warnings failed")
        return None, str(exc)
    finally:
        conn.close()

    rows.sort(
        key=lambda r: (
            _incident_warning_bucket(r, category, query_match_ids),
            *_incident_sort_key(r),
        )
    )
    return rows[:limit], None


def _render_incident_warnings(rows: list[dict], header: str = "=== INCIDENT WARNINGS ===") -> str:
    if not rows:
        return ""
    out_lines = [header]
    for r in rows:
        date = (r.get("created_at") or "")[:10]
        severity = _incident_severity(r).upper()
        cat = r.get("category") or "-"
        src = r.get("source") or ""
        title = _start_context_title(r)
        out_lines.append(f"  ! [{severity}] [{date}] incident/{cat} — {title}")
        if src:
            out_lines.append(f"    {src}")
    return "\n".join(out_lines)


def _emit_injection_telemetry(
    source: str,
    memory_ids: list[int],
    memory_types: dict[str, int],
    text: str,
    session_id: Optional[str] = None,
    project_root: Optional[str] = None,
) -> None:
    """Best-effort telemetry for memory strings surfaced to the agent."""
    try:
        from memory_telemetry import emit_injection
    except Exception:
        return

    try:
        emit_injection(
            log_path=LOG_DIR / "memory_injection.jsonl",
            project_root=project_root,
            source=source,
            memory_ids=memory_ids,
            memory_types=memory_types,
            char_count=len(text),
            token_estimate=_estimate_tokens(text),
            session_id=session_id,
        )
    except Exception:
        return


def _telemetry_rows(rows: list[dict], memory_type: Optional[str] = None) -> tuple[list[int], dict[str, int]]:
    ids: list[int] = []
    types: dict[str, int] = {}
    for row in rows:
        try:
            row_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        row_type = str(memory_type or row.get("memory_type") or "unknown")
        ids.append(row_id)
        types[row_type] = types.get(row_type, 0) + 1
    return ids, types


def build_start_context(
    scope: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 10,
    session_id: Optional[str] = None,
) -> str:
    """Format incident warnings plus relevant consilium/audit rows for /start.

    Replaces the legacy ``Glob("reports/consilium_*.md")+Read`` step in the
    /start workflow. Pulls from the `agent_memory` rows ingested by
    ``index_reports.py`` so cross-project knowledge (e.g., a horizon IBKR audit
    consulted from inside Claude_Booster) becomes discoverable.

    Ordering:
        1. Rows whose ``category`` matches the project derived from ``scope``.
        2. All other consilium/audit rows.
        3. Within each bucket: FTS rank (when ``query`` is set) else
           ``priority DESC, created_at DESC``.

    No DB writes — safe to call from rule prose, hooks, or interactive CLI.
    """
    incident_rows, incident_err = _fetch_incident_warnings(scope=scope, query=query, limit=5)
    rows, category, err_msg = _fetch_start_context(scope, query, limit)
    parts: list[str] = []
    if incident_rows:
        parts.append(_render_incident_warnings(incident_rows, header="=== INCIDENT REGISTER ==="))
    elif incident_err:
        parts.append(f"=== INCIDENT REGISTER — {incident_err} ===")

    rendered_rows: list[dict] = []
    if incident_rows:
        rendered_rows.extend(incident_rows)

    if err_msg is not None:
        parts.append(f"=== KNOWLEDGE BASE — {err_msg} ===")
        result = "\n\n".join(parts)
        ids, types = _telemetry_rows(rendered_rows)
        _emit_injection_telemetry("build_start_context", ids, types, result, session_id, scope)
        return result
    if not rows:
        result = "\n\n".join(parts)
        ids, types = _telemetry_rows(rendered_rows)
        _emit_injection_telemetry("build_start_context", ids, types, result, session_id, scope)
        return result

    if query:
        header = f"=== KNOWLEDGE BASE — query={query!r}"
    else:
        header = "=== KNOWLEDGE BASE"
    if category:
        header += f" — project={category}"
    header += " ==="

    out_lines: list[str] = [header]
    for r in rows:
        rendered_rows.append(r)
        date = (r.get("created_at") or "")[:10]
        mt = r.get("memory_type") or "?"
        cat = r.get("category") or "-"
        src = r.get("source") or ""
        content = r.get("content") or ""
        # First line of indexed reports is `# <name>` from build_row().
        title = ""
        first_line = content.split("\n", 1)[0] if content else ""
        if first_line.startswith("# "):
            title = first_line[2:].strip()
        else:
            title = first_line[:100].strip()
        if not title:
            title = Path(src).name if src else "(untitled report)"
        marker = "*" if category and cat == category else "-"
        out_lines.append(f"  {marker} [{date}] {mt}/{cat} — {title}")
        if src:
            out_lines.append(f"    {src}")
    parts.append("\n".join(out_lines))
    result = "\n\n".join(parts)
    ids, types = _telemetry_rows(rendered_rows)
    _emit_injection_telemetry("build_start_context", ids, types, result, session_id, scope)
    return result


def build_context(scope: str = "global", token_budget: int = 4000, session_id: Optional[str] = None) -> str:
    """Build formatted context without mutating the memory database."""
    if not isinstance(scope, str) or not scope:
        raise ValueError("scope must be a non-empty string")
    if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget < 0:
        raise ValueError("token_budget must be a non-negative integer")
    sections = [
        ("DIRECTIVES", "directive", None, 10),
        ("FEEDBACK", "feedback", None, 10),
        ("RECENT SESSIONS", "session_summary", None, 5),
        ("INCIDENT WARNINGS", "incident", None, 5),
        ("ERROR LESSONS", "error_lesson", None, 10),
        ("DECISIONS", "decision", None, 10),
    ]

    parts: list[str] = []
    tokens_used = 0
    rendered_ids: list[int] = []
    rendered_types: dict[str, int] = {}

    try:
        conn = get_readonly_connection()
    except sqlite3.Error:
        logger.exception("build_context database unavailable")
        return ""
    try:
        for title, mtype, _, limit in sections:
            # For project_context, match scope; others get both global and scope
            if mtype == "project_context":
                scope_clause = "scope = ?"
                params: list[Any] = [mtype, scope]
            else:
                scope_clause = "(scope = 'global' OR scope = ?)"
                params = [mtype, scope]

            if mtype == "incident":
                fetched = [
                    dict(r)
                    for r in conn.execute(
                        f"""SELECT id, content, created_at, priority, category,
                                   source, metadata_json
                            FROM agent_memory
                            WHERE memory_type = ? AND active = 1 AND {scope_clause}
                            ORDER BY created_at DESC, id ASC""",
                        tuple(params),
                    ).fetchall()
                ]
                fetched.sort(key=_incident_sort_key)
                rows = fetched[:limit]
            else:
                rows = conn.execute(
                    f"""SELECT id, content, created_at, priority, category FROM agent_memory
                        WHERE memory_type = ? AND active = 1 AND {scope_clause}
                        ORDER BY priority DESC, created_at DESC LIMIT ?""",
                    (*params, limit),
                ).fetchall()

            if not rows:
                continue

            section_lines = [f"=== {title} ==="]
            for r in rows:
                date_prefix = r["created_at"][:10] if r["created_at"] else ""
                if mtype == "directive":
                    line = f"  * {r['content']}"
                elif mtype == "error_lesson":
                    # Phase 2b: surface the taxonomy slug so /start readers
                    # can see at a glance which category an error belongs to
                    # and spot `unclassified` rows that need manual triage.
                    cat = (r["category"] or "unclassified").strip() or "unclassified"
                    line = f"  * [{date_prefix}] [{cat}] {r['content']}"
                elif mtype == "incident":
                    severity = _incident_severity(dict(r)).upper()
                    cat = (r.get("category") if isinstance(r, dict) else r["category"]) or "-"
                    title_text = _start_context_title(dict(r))
                    line = f"  ! [{severity}] [{date_prefix}] incident/{cat} — {title_text}"
                else:
                    line = f"  * [{date_prefix}] {r['content']}"

                line_tokens = _estimate_tokens(line)
                if tokens_used + line_tokens > token_budget:
                    break
                section_lines.append(line)
                tokens_used += line_tokens
                try:
                    rendered_ids.append(int(r["id"]))
                    rendered_types[mtype] = rendered_types.get(mtype, 0) + 1
                except (KeyError, TypeError, ValueError):
                    pass

            if len(section_lines) > 1:
                parts.append("\n".join(section_lines))

            if tokens_used >= token_budget:
                break

        # Also include project_context if scope is not global
        if scope != "global":
            rows = conn.execute(
                """SELECT id, content, created_at FROM agent_memory
                   WHERE memory_type = 'project_context' AND active = 1 AND scope = ?
                   ORDER BY priority DESC, created_at DESC LIMIT 10""",
                (scope,),
            ).fetchall()
            if rows:
                section_lines = ["=== PROJECT CONTEXT ==="]
                for r in rows:
                    date_prefix = r["created_at"][:10] if r["created_at"] else ""
                    line = f"  * [{date_prefix}] {r['content']}"
                    line_tokens = _estimate_tokens(line)
                    if tokens_used + line_tokens > token_budget:
                        break
                    section_lines.append(line)
                    tokens_used += line_tokens
                    try:
                        rendered_ids.append(int(r["id"]))
                        rendered_types["project_context"] = rendered_types.get("project_context", 0) + 1
                    except (KeyError, TypeError, ValueError):
                        pass
                if len(section_lines) > 1:
                    parts.append("\n".join(section_lines))
    except sqlite3.Error:
        logger.exception("build_context failed")
    finally:
        conn.close()

    result = "\n\n".join(parts)
    _emit_injection_telemetry("build_context", rendered_ids, rendered_types, result, session_id, scope)
    return result


def forget(memory_id: int) -> bool:
    """Soft-delete a memory (active=0)."""
    conn = get_connection()
    try:
        conn.execute("UPDATE agent_memory SET active = 0 WHERE id = ?", (memory_id,))
        conn.commit()
        logger.info("forget id=%d", memory_id)
        return True
    except Exception:
        logger.exception("forget failed id=%d", memory_id)
        return False
    finally:
        conn.close()


def forget_expired() -> int:
    """Delete entries past expires_at. Returns count deleted."""
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = conn.execute(
            "DELETE FROM agent_memory WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        conn.commit()
        count = cur.rowcount
        if count:
            logger.info("forget_expired: deleted %d", count)
        return count
    except Exception:
        logger.exception("forget_expired failed")
        return 0
    finally:
        conn.close()


def trim_rolling(memory_type: str, scope: str = "", conn: Optional[sqlite3.Connection] = None) -> int:
    """Enforce rolling limits for a memory_type. Returns count trimmed."""
    limit = ROLLING_LIMITS.get(memory_type)
    if limit is None:
        return 0

    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    try:
        # preserve=1 rows are immune to eviction. They were seeded as canonical
        # source-of-truth (institutional rules, consilium/audit imports) and
        # must not be auto-deleted when the rolling limit is exceeded — that
        # would silently re-open questions that Dmitry has already closed.
        # Counting them in the total is fine; excluding them from the DELETE
        # candidate pool is what matters.
        if memory_type == "project_context" and scope:
            count_sql = "SELECT COUNT(*) FROM agent_memory WHERE memory_type = ? AND scope = ? AND active = 1"
            count = conn.execute(count_sql, (memory_type, scope)).fetchone()[0]
            if count <= limit:
                return 0
            excess = count - limit
            conn.execute(
                """DELETE FROM agent_memory WHERE id IN (
                       SELECT id FROM agent_memory
                       WHERE memory_type = ? AND scope = ? AND active = 1 AND preserve = 0
                       ORDER BY priority ASC, created_at ASC LIMIT ?
                   )""",
                (memory_type, scope, excess),
            )
        else:
            count_sql = "SELECT COUNT(*) FROM agent_memory WHERE memory_type = ? AND active = 1"
            count = conn.execute(count_sql, (memory_type,)).fetchone()[0]
            if count <= limit:
                return 0
            excess = count - limit
            conn.execute(
                """DELETE FROM agent_memory WHERE id IN (
                       SELECT id FROM agent_memory
                       WHERE memory_type = ? AND active = 1 AND preserve = 0
                       ORDER BY priority ASC, created_at ASC LIMIT ?
                   )""",
                (memory_type, excess),
            )

        conn.commit()
        if excess > 0:
            logger.info("trim_rolling: type=%s scope=%s trimmed=%d", memory_type, scope, excess)
        return excess
    except Exception:
        logger.exception("trim_rolling failed")
        return 0
    finally:
        if own_conn:
            conn.close()


def backup_db() -> bool:
    """Copy DB to .bak file."""
    try:
        if DB_PATH.exists():
            shutil.copy2(str(DB_PATH), str(BACKUP_PATH))
            logger.info("backup_db: %s -> %s", DB_PATH, BACKUP_PATH)
            return True
        return False
    except Exception:
        logger.exception("backup_db failed")
        return False


def get_stats() -> dict:
    """Return counts per type, total size, etc."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT memory_type, active, COUNT(*) as cnt FROM agent_memory GROUP BY memory_type, active"
        ).fetchall()

        stats: dict[str, Any] = {"by_type": {}, "total_active": 0, "total_inactive": 0}
        for r in rows:
            key = r["memory_type"]
            if key not in stats["by_type"]:
                stats["by_type"][key] = {"active": 0, "inactive": 0}
            if r["active"]:
                stats["by_type"][key]["active"] = r["cnt"]
                stats["total_active"] += r["cnt"]
            else:
                stats["by_type"][key]["inactive"] = r["cnt"]
                stats["total_inactive"] += r["cnt"]

        # DB file size
        if DB_PATH.exists():
            stats["db_size_bytes"] = DB_PATH.stat().st_size
            stats["db_size_human"] = _human_size(stats["db_size_bytes"])

        return stats
    except Exception:
        logger.exception("get_stats failed")
        return {}
    finally:
        conn.close()


def list_memories(memory_type: Optional[str] = None, scope: Optional[str] = None, limit: int = 50) -> str:
    """Return formatted string for CLI display."""
    types = [memory_type] if memory_type else None
    memories = recall(types=types, scope=scope, limit=limit)

    if not memories:
        return "No memories found."

    lines = []
    for m in memories:
        status = "" if m["active"] else " [inactive]"
        date = m["created_at"][:10] if m["created_at"] else "?"
        content_preview = m["content"][:120].replace("\n", " ")
        if len(m["content"]) > 120:
            content_preview += "..."
        lines.append(
            f"  #{m['id']:4d} | {m['memory_type']:17s} | p={m['priority']:3d} | {date} | {m['scope']:20s}{status}\n"
            f"         {content_preview}"
        )
    return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f} TB"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _start_context_title(row: dict) -> str:
    """Derive the human-readable title of an indexed report row.

    Mirrors the prose formatter in :func:`build_start_context`: first line of
    the content is ``# <title>`` when produced by ``index_reports.build_row``;
    fallback is the first 100 chars of content, or the file basename from
    ``source`` when neither is present.
    """
    content = row.get("content") or ""
    first_line = content.split("\n", 1)[0] if content else ""
    if first_line.startswith("# "):
        return first_line[2:].strip()
    if first_line:
        return first_line[:100].strip()
    src = row.get("source") or ""
    return Path(src).name if src else "(untitled)"


def _cli():
    import argparse

    parser = argparse.ArgumentParser(description="Rolling Memory CLI")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a single JSON object to stdout instead of prose. Every "
             "payload has a top-level \"cmd\" key naming the subcommand; "
             "remaining keys are subcommand-specific. `start-context`/`similar` "
             "return curated, user-friendly field subsets; `search`/`list` dump "
             "raw agent_memory rows for debug parity. Backward-compatible — "
             "omit the flag to keep the existing prose output that hooks and "
             "rule text rely on (consilium Q3 2026-04-18).",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("stats", help="Show statistics")
    sub.add_parser("backup", help="Backup DB")

    p_list = sub.add_parser("list", help="List memories")
    p_list.add_argument("--type", dest="memory_type", default=None)
    p_list.add_argument("--scope", default=None)
    p_list.add_argument("--limit", type=int, default=50)

    p_search = sub.add_parser("search", help="Full-text search")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--scope", default=None,
                          help="Restrict to this scope (plus 'global' unless --no-global)")
    p_search.add_argument("--no-global", action="store_true",
                          help="With --scope, exclude 'global' fallback")

    p_mem = sub.add_parser("memorize", help="Store a memory")
    p_mem.add_argument("--type", dest="memory_type", required=True)
    p_mem.add_argument("--content", required=True)
    p_mem.add_argument("--priority", type=int, default=None)
    p_mem.add_argument("--scope", default="global")
    p_mem.add_argument("--category", default="")
    p_mem.add_argument("--source", default="user:explicit")

    p_ctx = sub.add_parser("context", help="Build context string")
    p_ctx.add_argument("--scope", default="global")
    p_ctx.add_argument("--budget", type=int, default=4000)

    p_start = sub.add_parser(
        "start-context",
        help="List incident warnings plus relevant consilium/audit reports for /start",
    )
    p_start.add_argument("--scope", default=None,
                         help="Project absolute path (defaults to cwd if omitted)")
    p_start.add_argument("--query", default=None,
                         help="Optional FTS5 query to filter reports by topic")
    p_start.add_argument("--limit", type=int, default=10)
    p_start.add_argument(
        "--stuck-check",
        action="store_true",
        default=False,
        help=(
            "Detect stuck-loop candidates in last 5 handovers + emit TOPIC TIMELINE block "
            "before the flat consilium/audit list. Backwards-compatible: without this flag "
            "output is identical to pre-D3 behaviour."
        ),
    )

    p_forget = sub.add_parser("forget", help="Soft-delete a memory")
    p_forget.add_argument("id", type=int)

    p_cons = sub.add_parser("consolidate", help="Consolidate similar memories via Haiku")
    p_cons.add_argument("--scope", default="global")
    p_cons.add_argument("--type", dest="memory_type", default=None)
    p_cons.add_argument("--dry-run", action="store_true", help="Show clusters without consolidating")
    p_cons.add_argument(
        "--force",
        action="store_true",
        help="Required to consolidate session_summary (template dominance makes clustering unsafe)",
    )

    p_similar = sub.add_parser("similar", help="Find memories similar to text")
    p_similar.add_argument("text")
    p_similar.add_argument("--type", dest="memory_type", default="error_lesson")
    p_similar.add_argument("--scope", default="global")

    args = parser.parse_args()
    # `start-context` is read-only and called from /start rule prose. Skipping
    # init_db() preserves the read-only contract (mirrors the R2-MED-1 dry-run
    # lesson) — a missing/unmigrated DB surfaces as a clean error from
    # build_start_context() rather than triggering a silent schema migration.
    if args.cmd != "start-context":
        init_db()

    def _emit_json(payload: dict) -> None:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))

    if args.cmd == "stats":
        stats = get_stats()
        if args.json:
            _emit_json({"cmd": "stats", "data": stats})
        else:
            print(json.dumps(stats, indent=2, ensure_ascii=False))

    elif args.cmd == "backup":
        ok = backup_db()
        if args.json:
            _emit_json({"cmd": "backup", "ok": bool(ok)})
        else:
            print("Backup OK" if ok else "Backup failed or no DB")

    elif args.cmd == "list":
        memories = recall(
            types=[args.memory_type] if args.memory_type else None,
            scope=args.scope,
            limit=args.limit,
        )
        if args.json:
            _emit_json({
                "cmd": "list",
                "memory_type": args.memory_type,
                "scope": args.scope,
                "rows": memories,
            })
        else:
            print(list_memories(memory_type=args.memory_type, scope=args.scope, limit=args.limit))

    elif args.cmd == "search":
        results = search(
            args.query,
            limit=args.limit,
            scope=args.scope,
            include_global=not args.no_global,
        )
        if args.json:
            _emit_json({
                "cmd": "search",
                "query": args.query,
                "scope": args.scope,
                "rows": results,
            })
        else:
            if not results:
                print("No results.")
            for r in results:
                print(f"  #{r['id']} [{r['memory_type']}] {r['content'][:100]}")

    elif args.cmd == "memorize":
        row_id = memorize(
            content=args.content,
            memory_type=args.memory_type,
            priority=args.priority,
            scope=args.scope,
            category=args.category,
            source=args.source,
        )
        if args.json:
            _emit_json({"cmd": "memorize", "id": row_id, "ok": row_id is not None})
        else:
            if row_id:
                print(f"Stored: #{row_id}")
            else:
                print("Duplicate or error.")

    elif args.cmd == "context":
        ctx = build_context(scope=args.scope, token_budget=args.budget)
        if args.json:
            # `context` output is a formatted markdown doc; returning it verbatim
            # keeps 1:1 parity with prose while letting JSON consumers pick up
            # text boundaries by splitting on the `===` section headers.
            _emit_json({
                "cmd": "context",
                "scope": args.scope,
                "token_budget": args.budget,
                "body": ctx or "",
            })
        else:
            print(ctx if ctx else "(empty context)")

    elif args.cmd == "start-context":
        if args.scope is not None:
            scope = args.scope
        else:
            try:
                scope = os.getcwd()
            except OSError:
                # cwd was deleted out from under the shell. Fall back to no
                # scope so _category_from_scope returns None and we still
                # produce a useful (unbiased) result instead of crashing.
                scope = None

        stuck_check = getattr(args, "stuck_check", False)

        # --- D3a + D3b: compute stuck-loop signal and topic timeline when requested ---
        stuck_signal: dict = {}
        topic_rows_result: list = []
        topic_stale_msg: Optional[str] = None
        topic_keywords_display: list = []
        topic_match_count: int = 0

        if stuck_check and scope:
            project_basename = Path(scope).name if scope else "project"

            # D3a — stuck-loop signal (reads handover files from scope/reports/).
            stuck_signal = _stuck_loop_signal(Path(scope), project_basename)

            # D3b — topic timeline: seed from latest handover first-step body.
            try:
                reports_dir = Path(scope) / "reports"
                if not reports_dir.is_dir():
                    reports_dir = Path(scope)
                candidates = sorted(
                    (f for f in reports_dir.glob("handover_*.md") if f.is_file()),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
                seed_text = _handover_first_step(candidates[0]) if candidates else ""
            except (OSError, IndexError):
                seed_text = ""

            # git paths: use scope dir if available.
            git_paths_for_seed: list = []
            try:
                import subprocess
                gp = subprocess.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5,
                    cwd=scope,
                )
                if gp.returncode == 0:
                    git_paths_for_seed = [
                        line[3:].strip() for line in gp.stdout.splitlines() if len(line) > 3
                    ]
            except Exception:
                pass

            topic_keywords_display = _extract_topic_keywords_for_display(seed_text, git_paths_for_seed)

            # Build timeline using a fresh read-only connection.
            # P0-3: build_topic_timeline now returns 3-tuple including topic_match_count.
            try:
                tl_conn = get_readonly_connection()
                try:
                    topic_rows_result, topic_stale_msg, topic_match_count = build_topic_timeline(
                        tl_conn, seed_text, git_paths_for_seed, limit=6
                    )
                finally:
                    tl_conn.close()
            except Exception as exc:
                logger.warning("build_topic_timeline failed: %s", exc)
                topic_rows_result = []
                topic_stale_msg = None
                topic_match_count = 0

        if args.json:
            rows, category, err_msg = _fetch_start_context(scope, args.query, args.limit)
            incident_rows, incident_err = _fetch_incident_warnings(
                scope=scope,
                query=args.query,
                limit=5,
            )
            projected = []
            for r in rows or []:
                projected.append({
                    "id": r.get("id"),
                    "memory_type": r.get("memory_type"),
                    "category": r.get("category"),
                    "scope": r.get("scope"),
                    "title": _start_context_title(r),
                    "source": r.get("source"),
                    "status": r.get("status"),
                    "resolve_by_date": r.get("resolve_by_date"),
                    "verified_at": r.get("verified_at"),
                    "superseded_by_id": r.get("superseded_by_id"),
                    "priority": r.get("priority"),
                    "created_at": r.get("created_at"),
                })
            payload: dict = {
                "cmd": "start-context",
                "scope": scope,
                "query": args.query,
                "category": category,
                "error": err_msg,
                "incident_error": incident_err,
                "incident_warnings": [
                    {
                        "id": r.get("id"),
                        "severity": _incident_severity(r),
                        "category": r.get("category"),
                        "scope": r.get("scope"),
                        "title": _start_context_title(r),
                        "source": r.get("source"),
                        "priority": r.get("priority"),
                        "created_at": r.get("created_at"),
                    }
                    for r in (incident_rows or [])
                ],
                "rows": projected,
            }
            # D3 — JSON mode: add topic_timeline and stuck_loop_candidate keys.
            if stuck_check:
                payload["topic_timeline"] = {
                    "keywords": topic_keywords_display,
                    "topic_match_count": topic_match_count,
                    "rows": [
                        {
                            "id": r.get("id"),
                            "created_at": r.get("created_at"),
                            "status": r.get("status"),
                            "memory_type": r.get("memory_type"),
                            "title": _row_title(r),
                        }
                        for r in topic_rows_result
                    ],
                    "stale_msg": topic_stale_msg,
                }
                payload["stuck_loop_candidate"] = {
                    "warn": bool(stuck_signal.get("warn")),
                    "hash": stuck_signal.get("hash"),
                    "count": stuck_signal.get("count", 0),
                    "tokens": stuck_signal.get("tokens", []),
                    "since": stuck_signal.get("since"),
                }
            _emit_json(payload)
        else:
            # Prose mode — D3c: emit TOPIC TIMELINE before flat list (and before STUCK LOOP block).
            output_parts: list[str] = []

            if stuck_check:
                # TOPIC TIMELINE block (D3c).
                # P0-3: suppress entirely when topic_match_count == 0 (no FTS5 topic matches).
                # Recency floor alone does not justify causal Premise/Tried/Current labels.
                if topic_match_count > 0:
                    tl_block = _render_topic_timeline(
                        topic_rows_result, topic_keywords_display, topic_stale_msg
                    )
                    if tl_block:
                        output_parts.append(tl_block)

                # STUCK LOOP CANDIDATE block (D3a).
                sl_block = _render_stuck_loop_block(stuck_signal)
                if sl_block:
                    output_parts.append(sl_block)

            # Existing flat list (unchanged when no --stuck-check).
            out = build_start_context(scope=scope, query=args.query, limit=args.limit)
            flat_list = out if out else "(no consilium/audit reports indexed for this scope)"
            output_parts.append(flat_list)

            print("\n\n".join(output_parts))

    elif args.cmd == "forget":
        ok = forget(args.id)
        if args.json:
            _emit_json({"cmd": "forget", "id": args.id, "ok": bool(ok)})
        else:
            print(f"Forgot #{args.id}" if ok else "Failed")

    elif args.cmd == "consolidate":
        if args.memory_type == "session_summary" and not args.force:
            parser.error(
                "session_summary is excluded from consolidation due to template dominance. "
                "Pass --force to override."
            )
        result = consolidate(
            scope=args.scope,
            memory_type=args.memory_type,
            dry_run=args.dry_run,
        )
        if args.json:
            _emit_json({
                "cmd": "consolidate",
                "scope": args.scope,
                "memory_type": args.memory_type,
                "dry_run": bool(args.dry_run),
                "data": result,
            })
        else:
            print(f"Consolidating memories (scope={args.scope}, type={args.memory_type or 'all'})...")
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            if result["conflicts"]:
                print("\nConflicts detected:")
                for c in result["conflicts"]:
                    print(f"  [{c['type']}] ids={c['ids']}: {c['text']}")

    elif args.cmd == "similar":
        results = _find_similar(args.text, args.memory_type, args.scope, limit=5)
        if args.json:
            projected = []
            for mem, subset, jaccard, shared in results:
                would_merge = (
                    subset >= MERGE_SUBSET_THRESHOLD
                    and jaccard >= MERGE_JACCARD_THRESHOLD
                    and shared >= MERGE_MIN_SHARED
                )
                decision = "MERGE" if would_merge else ("LINK" if subset >= MERGE_LINK_THRESHOLD else "NEW")
                projected.append({
                    "id": mem["id"],
                    "memory_type": mem["memory_type"],
                    "scope": mem["scope"],
                    "subset": round(subset, 3),
                    "jaccard": round(jaccard, 3),
                    "shared": shared,
                    "decision": decision,
                    "content": mem["content"],
                })
            _emit_json({
                "cmd": "similar",
                "text": args.text,
                "memory_type": args.memory_type,
                "scope": args.scope,
                "rows": projected,
            })
        else:
            if not results:
                print("No similar memories found.")
            for mem, subset, jaccard, shared in results:
                would_merge = (
                    subset >= MERGE_SUBSET_THRESHOLD
                    and jaccard >= MERGE_JACCARD_THRESHOLD
                    and shared >= MERGE_MIN_SHARED
                )
                decision = "MERGE" if would_merge else ("LINK" if subset >= MERGE_LINK_THRESHOLD else "NEW")
                print(
                    f"  #{mem['id']} subset={subset:.2f} jaccard={jaccard:.2f} shared={shared} "
                    f"-> {decision} [{mem['memory_type']}/{mem['scope']}] {mem['content'][:80]}"
                )

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
