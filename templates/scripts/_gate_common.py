#!/usr/bin/env python3
"""Shared helpers for delegate_gate.py and ask_gate.py.

Purpose:
    Both gates share byte-identical logging, timestamp, and path-walking
    primitives. Keeping them in two files lets silent drift happen: a typo
    in "bypass_honoured" on one side, a differently-cached mkdir on the
    other. This module is the single source of truth.

Contract:
    logs_dir() -> Path
        Returns the gate-log directory. Honours $CLAUDE_HOME (tests set
        this), falls back to ~/.claude. Computed per-call (not cached)
        because env overrides in subprocess tests must take effect.

    iso_now() -> str
        UTC timestamp as "YYYY-MM-DDTHH:MM:SSZ".

    append_jsonl(log_name: str, record: dict) -> None
        Appends one JSON line to logs_dir()/log_name. Fail-soft on OSError
        — gating must not fail because logging fails. Uses default=str so
        non-serialisable fields (e.g. Path) don't raise. The parent dir's
        mkdir is cached per-process (_LOG_DIR_READY) to eliminate the
        redundant syscall on the hot path.

    walk_up_to(start, predicate) -> Optional[Path]
        Walks [start, *start.parents], returns first path where
        predicate(p) is truthy. Catches OSError on the initial resolution.

    project_root_from(cwd_hint) -> Optional[Path]
        Thin wrapper: walks to the nearest ancestor with .git/ or .claude/.

    find_upward(cwd_hint, relpath) -> Optional[Path]
        Walks ancestors looking for p / relpath on disk.

Limitations:
    - Python 3.8+ compat: no `X | Y`, no `dict[str, int]`.
    - Log-dir mkdir cache is per-process. If a test deletes the dir and
      re-fires within the same process, the second call won't recreate
      the dir. Fine for our subprocess-based tests; worth knowing for
      future in-process callers.

ENV/Files:
    - Reads  : env $CLAUDE_HOME (optional)
    - Writes : <logs_dir>/<log_name> (append)
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from pathlib import Path
from typing import Callable, Optional, Set


# ---- Shared log-file names (single source of truth) --------------------

DELEGATE_LOG_NAME = "delegate_gate_decisions.jsonl"
ASK_LOG_NAME = "ask_gate_decisions.jsonl"
BYPASS_LOG_NAME = "gate_bypass_attempts.jsonl"


# ---- Decision constants (prevents silent typo drift) -------------------

DECISION_ALLOW = "allow"
DECISION_BLOCK = "block"
DECISION_AUTO_SKIP = "auto_skip"
DECISION_ADVISORY = "advisory"
DECISION_BYPASS_HONOURED = "bypass_honoured"
DECISION_BYPASS_REFUSED = "bypass_refused"


# ---- Logging primitives ------------------------------------------------

# Per-process cache of log dirs we've already mkdir'd. Hot-path gates can
# fire thousands of times per session; the mkdir syscall shows up on
# traces when we don't cache it.
_LOG_DIR_READY: Set[Path] = set()


def logs_dir() -> Path:
    base = os.environ.get("CLAUDE_HOME")
    if base:
        return Path(base) / "logs"
    return Path.home() / ".claude" / "logs"


def iso_now() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def append_jsonl(log_name: str, record: dict) -> None:
    """Append one JSON line to logs_dir()/log_name. Fail-soft."""
    try:
        d = logs_dir()
        if d not in _LOG_DIR_READY:
            d.mkdir(parents=True, exist_ok=True)
            _LOG_DIR_READY.add(d)
        path = d / log_name
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        # Gating must not fail because logging fails. Any non-OSError is
        # a programming bug (bad record shape) and is allowed to surface.
        pass


# ---- Path walking ------------------------------------------------------

def walk_up_to(start: Path, predicate: Callable[[Path], bool]) -> Optional[Path]:
    """Walk [start, *start.parents]; return first path where predicate is truthy."""
    try:
        resolved = start if isinstance(start, Path) else Path(start)
    except (TypeError, OSError):
        return None
    try:
        chain = [resolved, *resolved.parents]
    except (OSError, ValueError):
        return None
    for p in chain:
        try:
            if predicate(p):
                return p
        except OSError:
            continue
    return None


def _cwd_from_hint(cwd_hint: Optional[str]) -> Optional[Path]:
    try:
        if cwd_hint:
            return Path(cwd_hint)
        return Path.cwd()
    except (FileNotFoundError, OSError):
        return None


def project_root_from(cwd_hint: Optional[str]) -> Optional[Path]:
    """Nearest ancestor containing .git/ or .claude/. None if hint invalid."""
    cwd = _cwd_from_hint(cwd_hint)
    if cwd is None:
        return None
    return walk_up_to(
        cwd,
        lambda p: (p / ".git").exists() or (p / ".claude").is_dir(),
    )


def find_upward(cwd_hint: Optional[str], relpath: str) -> Optional[Path]:
    """Walk ancestors from cwd_hint looking for (ancestor / relpath) on disk."""
    cwd = _cwd_from_hint(cwd_hint)
    if cwd is None:
        return None
    hit = walk_up_to(cwd, lambda p: (p / relpath).exists())
    return (hit / relpath) if hit is not None else None


# ---- Sub-agent detection (multi-signal, defence-in-depth) --------------

def is_subagent_context(data: dict) -> bool:
    """Return True if hook stdin shows a sub-agent context.

    Claude Code v2.1.114+ passes BOTH ``agent_id`` and ``agent_type`` for
    sub-agent sessions. We check either one — if the harness ever renames
    one field, the other still carries the signal and sub-agents remain
    auto-skipped (the original delegate-budget incident is not reopened).
    """
    if not isinstance(data, dict):
        return False
    aid = data.get("agent_id")
    if isinstance(aid, str) and aid:
        return True
    atype = data.get("agent_type")
    if isinstance(atype, str) and atype:
        return True
    return False


# ---- Secret redaction (for log-record message excerpts) ----------------

# Matches common secret-bearing prefixes (api_key / token / secret /
# password / bearer), an optional separator (``=``, ``:``, whitespace or
# ``  ``), and the contiguous token that follows. Case-insensitive.
# JWT-ish ``eyJ...`` prefix is matched as a standalone token — JWTs rarely
# have a separator because they already carry one internally. Must run
# BEFORE ``[:200]`` truncation so we don't split a token mid-match.
_SECRET_RE = re.compile(
    r"(?i)(?:"
    r"(?:api[_-]?key|token|secret|password|bearer)[\s:=]*[\w\-\.]+"
    r"|eyJ[A-Za-z0-9_\-]+\.[\w\-\.]+"
    r")",
)


def redact_secrets(s: str) -> str:
    """Return ``s`` with runs matching the secret regex replaced by ``<redacted>``.

    Contract:
        - Non-str input → "" (defensive — we never propagate TypeError from
          a logging helper).
        - No match → input returned unchanged.
        - Match → the ENTIRE matched run (prefix + value) becomes
          ``<redacted>``. Callers truncate afterwards with ``[:200]``.
    """
    if not isinstance(s, str) or not s:
        return "" if not isinstance(s, str) else s
    return _SECRET_RE.sub("<redacted>", s)


# ---- Real context-window occupancy (for compact-advisor) ---------------

def real_context_tokens(transcript_path) -> Optional[int]:
    """Return the real context-window occupancy from the session JSONL.

    Streams the transcript line by line and tracks the LAST assistant message
    that carries a ``usage`` block. Returns the sum of
    ``input_tokens + cache_read_input_tokens + cache_creation_input_tokens``
    from that block — this is the actual number of tokens the model saw on
    its most recent turn, i.e. the true context occupancy.

    Returns None when:
        - the file can't be opened (OSError),
        - no assistant message with a usage dict is found,
        - a usage dict is present but the three fields sum to 0 (defensive:
          guards against renamed/missing fields — caller falls back).
    Malformed/truncated JSON lines (including a partially-written final line)
    are skipped, not fatal.
    """
    if not transcript_path:
        return None
    last_usage = None
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue  # malformed / truncated final line — skip
                if not isinstance(obj, dict):
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "assistant":
                    continue
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    last_usage = usage
    except OSError:
        return None
    except Exception:
        return None
    if not isinstance(last_usage, dict):
        return None
    # `or 0` coerces a present-but-null field (input_tokens: null) to 0 — the API
    # emits null on cached/interrupted turns; a bare .get(...,0) keeps the None and
    # the sum would raise TypeError, escaping this function uncaught.
    try:
        total = int(
            (last_usage.get("input_tokens") or 0)
            + (last_usage.get("cache_read_input_tokens") or 0)
            + (last_usage.get("cache_creation_input_tokens") or 0)
        )
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None  # fields renamed/zero — let caller fall back
    return total


def effective_compact_threshold(observed_tokens):
    """Compute (threshold, window) for the compact advisor.

    Returns a (threshold:int, window:int) tuple — the absolute token count at
    which the /compact reminder should fire, and the assumed context window.

    Logic:
        - window defaults to 1_000_000; env CLAUDE_BOOSTER_CONTEXT_WINDOW
          overrides (malformed → keep default).
        - If observed_tokens is already above the legacy 200k window, force
          window to at least 1_000_000 (we are clearly on a large window).
        - pct defaults to 0.6; env CLAUDE_BOOSTER_COMPACT_PCT overrides
          (malformed → 0.6), clamped to [0.01, 1.0].
        - If CLAUDE_BOOSTER_COMPACT_THRESHOLD is set AND parses as int, it is
          an absolute override that wins (back-compat). Otherwise
          threshold = int(window * pct).
        - threshold >= 1 and window >= 1 are guaranteed.
    """
    _DEFAULT_WINDOW = 1_000_000
    window = _DEFAULT_WINDOW
    explicit_window = False
    try:
        window = int(os.environ["CLAUDE_BOOSTER_CONTEXT_WINDOW"])
        explicit_window = True
    except (KeyError, ValueError):
        window = _DEFAULT_WINDOW
    if window < 1:
        # A non-positive window is meaningless — fall back to the default and
        # stop treating it as an explicit choice (else threshold would clamp to 1
        # and the advisory would fire on every call with a "0k window" message).
        window = _DEFAULT_WINDOW
        explicit_window = False

    # Auto-bump only when the window was NOT explicitly set: an observed occupancy
    # above the legacy 200k cap proves we are on a large (≥1M) window. An explicit
    # CLAUDE_BOOSTER_CONTEXT_WINDOW is the user's deliberate choice — respect it.
    if not explicit_window and observed_tokens is not None:
        try:
            if int(observed_tokens) > 200_000:
                window = max(window, _DEFAULT_WINDOW)
        except (TypeError, ValueError):
            pass

    pct = 0.6
    try:
        pct = float(os.environ["CLAUDE_BOOSTER_COMPACT_PCT"])
    except (KeyError, ValueError):
        pct = 0.6
    pct = max(min(pct, 1.0), 0.01)

    threshold = None
    if "CLAUDE_BOOSTER_COMPACT_THRESHOLD" in os.environ:
        try:
            threshold = int(os.environ["CLAUDE_BOOSTER_COMPACT_THRESHOLD"])
        except ValueError:
            threshold = None
    if threshold is None:
        threshold = int(window * pct)

    if window < 1:
        window = 1
    if threshold < 1:
        threshold = 1
    return threshold, window
