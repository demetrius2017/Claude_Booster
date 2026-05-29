#!/usr/bin/env python3
"""
model_balancer.py — Daily routing decision engine for model_balancer.

Purpose:
    Maintains ~/.claude/model_balancer.json as the single source of routing
    truth for all Claude Booster projects. Day-1 scope: passive observation
    + seed defaults. Active path (day-N): reads model_metrics from
    rolling_memory.db, computes p50 latency per (provider, model) per
    task_category over last 14 days, and applies a Pareto objective function
    to select the empirically-best candidate per category.

Contract (inputs/outputs):
    Reads  : ~/.claude/model_balancer.json (schema_version=2)
             ~/.claude/rolling_memory.db (model_metrics table, read-only)
             ~/.claude/openai_models.json (intelligence_score lookup)
    Writes : ~/.claude/model_balancer.json (atomic via .tmp + os.replace)
    Backups: ~/.claude/model_balancer.json.bak.<YYYY-MM-DD> (max 7 kept)

CLI:
    model_balancer.py decide [--force]  Idempotent daily refresh.
    model_balancer.py get <category>    Print routing dict for one category.
    model_balancer.py show              Print full current decision JSON.
    model_balancer.py status            One-line freshness summary.

Library mode:
    from model_balancer import get_routing, current_decision
    routing = get_routing("coding")   # → {"provider": ..., "model": ...}
    decision = current_decision()     # → full dict

Limitations:
    - Active path requires MIN_SAMPLES per (provider, model, category) to
      override routing; falls back to prior routing if threshold not met.
    - Backup filename uses prior file's decision_date; if that field is
      missing, falls back to today's date with a "-unknown" suffix.
    - Backup rotation deletes only files matching the expected .bak.* pattern.

ENV:
    CLAUDE_MODEL_BALANCER_PATH      Override default ~/.claude/model_balancer.json
    CLAUDE_BALANCER_FORCE_ACTIVE    Re-evaluate even if decision_date == today
    CLAUDE_BALANCER_DISABLE_ACTIVE  Skip active path; behave like day-1
    CLAUDE_BALANCER_MIN_SAMPLES     Override MIN_SAMPLES threshold (default 5)

Files:
    ~/.claude/model_balancer.json           live routing file
    ~/.claude/model_balancer.json.tmp       transient atomic-write temp
    ~/.claude/model_balancer.json.bak.*     rolling backups (max 7)
    ~/.claude/model_balancer.json.preworker-bak  pre-session safety backup
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DEFAULT_PATH = Path.home() / ".claude" / "model_balancer.json"
_BALANCER_PATH: Path = Path(
    os.environ.get("CLAUDE_MODEL_BALANCER_PATH", str(_DEFAULT_PATH))
)
_DB_PATH: Path = Path.home() / ".claude" / "rolling_memory.db"
_OAI_MODELS_PATH: Path = Path.home() / ".claude" / "openai_models.json"
_MAX_BACKUPS = 7

# Provider name constants — single source of truth, prevents silent typos
# in routing entries / metric rows / log queries.
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_CODEX = "codex-cli"
PROVIDER_PAL = "pal"

# Neutral fallback for unknown models (e.g. gpt-5.3-codex variants not in
# openai_models.json yet). Sits between Sonnet (17) and Haiku (13).
_INTELLIGENCE_SCORE_UNKNOWN = 15

# ---------------------------------------------------------------------------
# Active-path constants
# ---------------------------------------------------------------------------

# Minimum samples per (provider, model) within a category to trust the data
try:
    MIN_SAMPLES: int = int(os.environ.get("CLAUDE_BALANCER_MIN_SAMPLES", "5"))
except (TypeError, ValueError):
    MIN_SAMPLES = 5

# Look-back window in days for metric queries
_LOOKBACK_DAYS: int = 14

# Pareto objective weights — must sum to 1.0
_WEIGHTS: dict[str, float] = {"q": 0.5, "l": 0.3, "b": 0.2}

# Hardcoded intelligence scores for Anthropic models (not in openai_models.json)
_QUALITY_SCORES_ANTHROPIC: dict[str, int] = {
    "claude-opus-4-7": 20,
    "claude-opus-4-8": 20,
    "claude-sonnet-4-6": 17,
    "claude-haiku-4-5": 13,
}

# Pinned categories — their routing is NEVER overwritten by active logic
_PINNED_CATEGORIES: frozenset[str] = frozenset({"lead", "high_blast_radius"})

# Transitions ring-buffer cap
_MAX_TRANSITIONS: int = 50

# ---------------------------------------------------------------------------
# Hardcoded bootstrap defaults (used only when no prior JSON exists)
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    "schema_version": 2,
    "weight_profile": "balanced",
    "rationale": "bootstrap — no prior decision",
    "weekly_tokens_cap": 0,
    "codex_pro_weekly_tokens_cap": 0,
    "routing": {
        "trivial":        {"provider": PROVIDER_ANTHROPIC, "model": "claude-haiku-4-5"},
        "recon":          {"provider": PROVIDER_ANTHROPIC, "model": "claude-haiku-4-5"},
        "medium":         {"provider": PROVIDER_ANTHROPIC, "model": "claude-sonnet-4-6"},
        "coding":         {"provider": PROVIDER_ANTHROPIC, "model": "claude-sonnet-4-6"},
        "hard":           {"provider": PROVIDER_ANTHROPIC, "model": "claude-opus-4-7"},
        "consilium_bio":  {"provider": PROVIDER_ANTHROPIC, "model": "claude-opus-4-7"},
        "audit_external": {"provider": PROVIDER_PAL,       "model": "gpt-5.5"},
        "lead":           {"provider": PROVIDER_ANTHROPIC, "model": "claude-opus-4-7"},
        "high_blast_radius": {
            "provider": PROVIDER_ANTHROPIC,
            "model": "claude-sonnet-4-6",
            "applies_to": [
                "auth", "security", "secrets",
                "db_migrations", "financial_dml", "infra_config",
            ],
        },
    },
}

# Known routing categories (for validation)
_KNOWN_CATEGORIES = set(DEFAULTS["routing"].keys())

# ---------------------------------------------------------------------------
# Module-level cache (populated lazily on first get_routing / current_decision)
# ---------------------------------------------------------------------------

_cached_decision: dict | None = None
_cached_intelligence_scores: dict[str, int] | None = None


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _today_utc() -> str:
    """Return today's date in UTC as YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso8601() -> str:
    """Return current UTC time as ISO8601 with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_until_str() -> str:
    """Return tomorrow midnight UTC as ISO8601 with Z suffix."""
    tomorrow = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    return tomorrow.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rationale_to_source(rationale: str) -> str:
    """Map rationale prefix to a one-word source label for CLI status output."""
    if rationale.startswith("bootstrap"):
        return "bootstrap"
    if rationale.startswith("active"):
        return "active"
    if rationale.startswith("passive"):
        return "passive"
    return "seed"


def _load_file() -> dict | None:
    """Load and parse the balancer JSON. Returns None on missing/parse error."""
    if not _BALANCER_PATH.exists():
        return None
    try:
        return json.loads(_BALANCER_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _backup_current(prior_date: str | None) -> None:
    """Copy current file to a dated backup; rotate to keep at most _MAX_BACKUPS."""
    if not _BALANCER_PATH.exists():
        return
    suffix = prior_date if prior_date else (_today_utc() + "-unknown")
    backup_path = _BALANCER_PATH.parent / f"model_balancer.json.bak.{suffix}"
    shutil.copy2(_BALANCER_PATH, backup_path)

    # Rotate — collect all .bak.* siblings, sort by name, delete oldest
    bak_dir = _BALANCER_PATH.parent
    bak_files = sorted(
        bak_dir.glob("model_balancer.json.bak.*"),
        key=lambda p: p.name,
    )
    while len(bak_files) > _MAX_BACKUPS:
        bak_files.pop(0).unlink(missing_ok=True)


def _write_atomic(data: dict) -> None:
    """Write *data* to the balancer path atomically via a temp file."""
    tmp = _BALANCER_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _BALANCER_PATH)


def _build_refreshed(prior: dict) -> dict:
    """
    Build a refreshed decision from *prior* file contents.
    Preserves routing shape; only updates decision_date and valid_until.
    """
    today = _today_utc()
    refreshed = dict(prior)
    refreshed["decision_date"] = today
    refreshed["valid_until"] = _valid_until_str()
    return refreshed


def _build_bootstrap() -> dict:
    """Build a brand-new decision from hardcoded DEFAULTS."""
    today = _today_utc()
    decision = dict(DEFAULTS)
    decision["decision_date"] = today
    decision["valid_until"] = _valid_until_str()
    return decision


# ---------------------------------------------------------------------------
# Active-path helpers
# ---------------------------------------------------------------------------

def _load_intelligence_scores() -> dict[str, int]:
    """
    Load intelligence_score for OpenAI/Codex models from openai_models.json.
    Merges with hardcoded Anthropic scores. Result is cached at module level.
    Returns dict: model_name_or_alias -> int score (0..20).
    """
    global _cached_intelligence_scores
    if _cached_intelligence_scores is not None:
        return _cached_intelligence_scores

    scores: dict[str, int] = dict(_QUALITY_SCORES_ANTHROPIC)

    try:
        if _OAI_MODELS_PATH.exists():
            raw = json.loads(_OAI_MODELS_PATH.read_text(encoding="utf-8"))
            models_list: list[dict] = raw.get("models", [])
            for entry in models_list:
                score = entry.get("intelligence_score")
                if score is None:
                    continue
                score = int(score)
                # Index by model_name + all aliases
                names: list[str] = [entry.get("model_name", "")] + entry.get("aliases", [])
                for name in names:
                    if name:
                        scores[name] = score
    except Exception:
        # If JSON is malformed or unreadable, fall back to Anthropic-only scores
        pass

    _cached_intelligence_scores = scores
    return scores


def _get_intelligence_score(provider: str, model: str) -> int:
    """
    Return intelligence score (0..20) for a (provider, model) pair.
    Falls back to 15 (neutral) if unknown.
    """
    scores = _load_intelligence_scores()
    if model in scores:
        return scores[model]
    return _INTELLIGENCE_SCORE_UNKNOWN


def _query_metrics(category: str, db_path: Path) -> list[dict[str, Any]]:
    """
    Query model_metrics for a given task_category over last _LOOKBACK_DAYS.
    Returns list of dicts with keys: provider, model, per_turn_ms, success.
    Rows with NULL per_turn_ms are excluded by SQL WHERE clause.
    Opens DB read-only; closes in finally.
    """
    rows: list[dict[str, Any]] = []
    conn = None
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT provider, model, per_turn_ms, success
            FROM model_metrics
            WHERE task_category = ?
              AND ts_utc >= datetime('now', ?)
              AND per_turn_ms IS NOT NULL
            """,
            (category, f"-{_LOOKBACK_DAYS} days"),
        )
        for row in cur.fetchall():
            provider, model, per_turn_ms, success = row
            # Defensive: skip on any unexpected type
            try:
                rows.append({
                    "provider": str(provider),
                    "model": str(model),
                    "per_turn_ms": int(per_turn_ms),
                    "success": int(success),
                })
            except (TypeError, ValueError):
                continue
    finally:
        if conn is not None:
            conn.close()
    return rows


def _score_candidate(
    provider: str,
    model: str,
    p50: float,
    max_p50: float,
    budget_pcts: dict[str, float],
) -> float:
    """
    Compute Pareto score for a (provider, model) candidate.

    score = 0.5 * quality - 0.3 * norm_lat * 20 - 0.2 * budget * 20

    All three terms are scaled to [0..20] range before weighting.

    budget_pcts maps provider name to its weekly-quota used fraction (0..1).
    Providers absent from the dict get budget pressure = 0.0.
    """
    quality = _get_intelligence_score(provider, model)          # 0..20
    norm_lat = (p50 / max_p50) if max_p50 > 0.0 else 0.0       # 0..1
    budget = budget_pcts.get(provider, 0.0)                     # 0..1

    score = (
        _WEIGHTS["q"] * quality
        - _WEIGHTS["l"] * norm_lat * 20.0
        - _WEIGHTS["b"] * budget * 20.0
    )
    return score


def _get_weekly_max_pct(prior: dict) -> float:
    """Return weekly_max_pct (0..1). Live from DB if cap configured; else snapshot fallback."""
    try:
        cap = prior.get("weekly_tokens_cap", 0)
        if cap and cap > 0:
            with sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True, timeout=1.0) as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(input_tokens + cache_creation_tokens + output_tokens), 0) "
                    "FROM claude_max_usage WHERE ts_utc >= datetime('now', '-7 days')"
                ).fetchone()
                total = row[0] if row else 0
                return min(1.0, total / cap)
    except Exception:
        pass
    # Fallback: stale snapshot
    try:
        return float(prior.get("inputs_snapshot", {}).get("claude_max_weekly_used_pct", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _get_codex_quota_pct(prior: dict) -> float:
    """Return codex_pro_weekly_used_pct (0..1). Reads ~/.codex/state_*.sqlite directly."""
    try:
        cap = prior.get("codex_pro_weekly_tokens_cap", 0)
        if cap and cap > 0:
            codex_dir = Path.home() / ".codex"
            candidates = list(codex_dir.glob("state_*.sqlite"))
            if not candidates:
                return 0.0
            codex_db = max(candidates, key=lambda p: p.stat().st_mtime)
            with sqlite3.connect(f"file:{codex_db}?mode=ro", uri=True, timeout=1.0) as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(tokens_used), 0) FROM threads "
                    "WHERE model_provider = 'openai' "
                    "AND updated_at >= strftime('%s', datetime('now', '-7 days'))"
                ).fetchone()
                total = int(row[0]) if row else 0
                return min(1.0, total / cap)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, FileNotFoundError, ValueError, OSError):
        pass
    # Fallback: stale snapshot
    try:
        return float(prior.get("inputs_snapshot", {}).get("codex_pro_weekly_used_pct", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _set_codex_snapshot(decision: dict, codex_pct: float) -> None:
    """Write codex_pro_weekly_used_pct into inputs_snapshot."""
    decision.setdefault("inputs_snapshot", {})["codex_pro_weekly_used_pct"] = codex_pct


def _active_decide(prior: dict) -> dict:
    """
    Active routing logic: reads model_metrics, applies Pareto scoring,
    updates routing per category when statistical confidence is met.

    Returns a new decision dict (never raises — caller wraps in try/except).
    """
    today = _today_utc()

    decision = copy.deepcopy(prior)
    decision["decision_date"] = today
    decision["valid_until"] = _valid_until_str()

    prior_routing: dict[str, dict] = prior.get("routing", {})
    new_routing: dict[str, dict] = copy.deepcopy(prior_routing)

    pinned: dict[str, dict] = {
        cat: copy.deepcopy(prior_routing[cat])
        for cat in _PINNED_CATEGORIES
        if cat in prior_routing
    }

    # Read weekly_max_pct — live from DB if cap configured, else stale snapshot
    weekly_max_pct: float = _get_weekly_max_pct(prior)
    codex_pct: float = _get_codex_quota_pct(prior)
    budget_pcts: dict[str, float] = {
        PROVIDER_ANTHROPIC: weekly_max_pct,
        PROVIDER_CODEX: codex_pct,
    }

    # Ensure DB exists before attempting reads
    db_path = _DB_PATH
    if not db_path.exists():
        # No DB at all — fall back to refreshed prior
        refreshed = _build_refreshed(prior)
        refreshed["rationale"] = "active — no samples in last 14d; preserved prior routing"
        return refreshed

    transitions: list[dict] = list(prior.get("transitions", []))

    categories = [c for c in prior_routing if c not in _PINNED_CATEGORIES]
    total_samples_seen = 0
    categories_updated = 0
    max_n_any_category = 0

    for category in categories:
        rows = _query_metrics(category, db_path)
        if not rows:
            continue

        # Group by (provider, model)
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            key = (row["provider"], row["model"])
            groups.setdefault(key, []).append(row)

        total_samples_seen += len(rows)

        # Compute stats per group
        candidates = []
        for (prov, mdl), group_rows in groups.items():
            n = len(group_rows)
            if n > max_n_any_category:
                max_n_any_category = n
            if n < MIN_SAMPLES:
                continue
            p50 = statistics.median(r["per_turn_ms"] for r in group_rows)
            success_rate = statistics.mean(r["success"] for r in group_rows)
            candidates.append({
                "provider": prov,
                "model": mdl,
                "n_samples": n,
                "p50": p50,
                "success_rate": success_rate,
            })

        if not candidates:
            continue

        # Normalize latency against max_p50 across all candidates in this category
        max_p50 = max(c["p50"] for c in candidates)

        # Score each candidate
        for c in candidates:
            c["score"] = _score_candidate(
                c["provider"], c["model"], c["p50"], max_p50, budget_pcts
            )

        # Pick winner: max score; tie → lower p50; tie again → higher success_rate
        winner = max(
            candidates,
            key=lambda c: (c["score"], -c["p50"], c["success_rate"]),
        )

        new_entry = {"provider": winner["provider"], "model": winner["model"]}

        # Detect change (compare core fields only)
        old_entry = prior_routing.get(category, {})
        old_core = {"provider": old_entry.get("provider"), "model": old_entry.get("model")}
        if new_entry != old_core:
            # Record transition
            transition = {
                "category": category,
                "old": old_core,
                "new": new_entry,
                "computed_at": _now_iso8601(),
                "n_samples_winner": winner["n_samples"],
                "p50_ms_winner": int(round(winner["p50"])),
            }
            transitions.append(transition)
            categories_updated += 1

        # Preserve any extra fields from old entry (e.g. applies_to)
        merged = dict(old_entry)
        merged.update(new_entry)
        new_routing[category] = merged

    # Restore pins unconditionally
    for cat, pinned_val in pinned.items():
        new_routing[cat] = pinned_val

    # Cap transitions ring buffer
    transitions = transitions[-_MAX_TRANSITIONS:]

    # Build rationale
    if categories_updated > 0:
        rationale = (
            f"active — {categories_updated}/{len(categories)} categories updated "
            f"based on {total_samples_seen} total samples (last 14d)"
        )
    elif total_samples_seen > 0:
        rationale = (
            f"active — insufficient samples (max n={max_n_any_category}, "
            f"threshold={MIN_SAMPLES}); preserved prior routing"
        )
    else:
        rationale = "active — no samples in last 14d; preserved prior routing"

    decision["routing"] = new_routing
    decision["rationale"] = rationale
    decision["transitions"] = transitions

    _set_codex_snapshot(decision, codex_pct)

    return decision


# ---------------------------------------------------------------------------
# Core decide logic
# ---------------------------------------------------------------------------

def decide(*, force: bool = False) -> dict:
    """
    Idempotent daily refresh.

    - If today's decision exists and force=False → return it unchanged.
    - If stale or missing → back up + regenerate + write atomically.

    Active path (day-N): reads model_metrics, computes p50 latency,
    selects empirically-best model per category when n >= MIN_SAMPLES.
    Falls back to _build_refreshed on any exception (hook safety).

    ENV overrides:
      CLAUDE_BALANCER_FORCE_ACTIVE=1  — re-evaluate even if date == today
      CLAUDE_BALANCER_DISABLE_ACTIVE=1 — skip active path entirely

    Returns the in-effect decision dict.
    """
    global _cached_decision
    today = _today_utc()
    prior = _load_file()

    # Check env-level force/disable flags
    force_active = os.environ.get("CLAUDE_BALANCER_FORCE_ACTIVE", "0") == "1" or force
    disable_active = os.environ.get("CLAUDE_BALANCER_DISABLE_ACTIVE", "0") == "1"

    if prior is not None and prior.get("decision_date") == today and not force_active:
        # Already fresh — no rewrite
        _cached_decision = prior
        return prior

    # Need to regenerate
    prior_date = prior.get("decision_date") if prior else None
    _backup_current(prior_date)

    if prior is None:
        new_decision = _build_bootstrap()
    elif disable_active:
        # Day-1 passive mode: only refresh date, mark rationale as passive bypass
        new_decision = _build_refreshed(prior)
        new_decision["rationale"] = "passive — CLAUDE_BALANCER_DISABLE_ACTIVE=1 bypass (day-1 refresh)"
        codex_pct = _get_codex_quota_pct(prior)
        _set_codex_snapshot(new_decision, codex_pct)
    else:
        # Active path — wrapped in try/except so hook never crashes
        try:
            new_decision = _active_decide(prior)
        except Exception:
            # Fall back gracefully to simple date refresh
            new_decision = _build_refreshed(prior)

    _write_atomic(new_decision)
    _cached_decision = new_decision
    return new_decision


# ---------------------------------------------------------------------------
# Public library API
# ---------------------------------------------------------------------------

def current_decision() -> dict:
    """Return in-effect decision dict (load once, cache)."""
    global _cached_decision
    if _cached_decision is None:
        _cached_decision = _load_file() or _build_bootstrap()
    return _cached_decision


def get_routing(category: str) -> dict:
    """
    Return routing dict for *category* from current decision.
    Raises KeyError if category unknown.
    """
    routing = current_decision().get("routing", {})
    if category not in routing:
        raise KeyError(f"Unknown routing category: {category!r}")
    return routing[category]


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

def _cmd_decide(args: argparse.Namespace) -> int:
    force_flag = getattr(args, "force", False)
    decision = decide(force=force_flag)
    date = decision.get("decision_date", "?")
    rationale = decision.get("rationale", "")
    source = _rationale_to_source(rationale)
    print(f"decision_date={date}  source={source}  routing_keys={list(decision.get('routing', {}).keys())}")
    print(f"rationale={rationale}")
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    cat = args.category
    decision = current_decision()
    routing = decision.get("routing", {})
    if cat not in routing:
        print(f"error: unknown category {cat!r}. known: {sorted(routing.keys())}", file=sys.stderr)
        return 1
    print(json.dumps(routing[cat], indent=2))
    return 0


def _cmd_show(_args: argparse.Namespace) -> int:
    decision = current_decision()
    # Compact summary: schema + date + routing table
    out = {
        "schema_version": decision.get("schema_version"),
        "decision_date": decision.get("decision_date"),
        "valid_until": decision.get("valid_until"),
        "weight_profile": decision.get("weight_profile"),
        "routing": decision.get("routing", {}),
        "rationale": decision.get("rationale", ""),
    }
    print(json.dumps(out, indent=2))
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    decision = _load_file()
    if decision is None:
        print("decision_date=none, age=n/a, source=none, stale (no file)")
        return 0

    date_str = decision.get("decision_date", "")
    today = _today_utc()

    try:
        decision_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        age_hours = int((now_utc - decision_dt).total_seconds() // 3600)
        freshness = "fresh" if date_str == today else "stale"
    except ValueError:
        age_hours = -1
        freshness = "stale"

    rationale = decision.get("rationale", "")
    source = _rationale_to_source(rationale)
    print(f"decision_date={date_str}, age={age_hours}h, source={source}, freshness={freshness}")
    snapshot = decision.get("inputs_snapshot", {})
    codex_pct_val = snapshot.get("codex_pro_weekly_used_pct")
    if codex_pct_val is not None:
        print(f"codex_pro_quota: {codex_pct_val * 100:.0f}%")
    else:
        print("codex_pro_quota: (no source — wire codex_pro_weekly_tokens_cap in model_balancer.json)")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="model_balancer",
        description="Daily routing decision engine for Claude Booster model_balancer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    decide_p = sub.add_parser("decide", help="Idempotent daily refresh of routing decision.")
    decide_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Force re-evaluation even if decision_date == today.",
    )

    get_p = sub.add_parser("get", help="Print routing for one category.")
    get_p.add_argument("category", help="Routing category name.")

    sub.add_parser("show", help="Print full current decision summary as JSON.")
    sub.add_parser("status", help="One-line freshness status.")

    args = parser.parse_args()

    dispatch = {
        "decide": _cmd_decide,
        "get": _cmd_get,
        "show": _cmd_show,
        "status": _cmd_status,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
