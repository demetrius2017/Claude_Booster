#!/usr/bin/env python3
"""
PreToolUse hook: NUDGE "delegate, don't do" via a 1-action budget (ADVISORY).

Purpose:
  The Lead (main Claude session) is supposed to orchestrate agents, not do
  the substantive work itself. pipeline.md says so, but soft rules get
  ignored. This hook keeps the same 1-action budget but, on exhaustion,
  emits a NON-BLOCKING advisory (additionalContext + exit 0) instead of a
  hard block. It nudges the Lead toward delegation without ever cancelling
  the tool call — and therefore without cancelling sibling calls in a
  parallel tool-batch (the harness cancels every sibling in a batch when
  any one returns non-zero; a hard block here used to take out the very
  Agent-spawn siblings that would have reset the budget).

  Hard teeth live elsewhere: go_gate / phase_gate / dep_guard /
  financial_dml_guard still block (exit 2). This gate is advisory only.

Contract:
  stdin  — PreToolUse JSON {tool_name, tool_input, cwd, agent_id, agent_type,
           session_id}
  stdout — on budget exhaustion ONLY: a single JSON line
           {"additionalContext": "<nudge>"}. No other path writes stdout.
  stderr — unused on the budget path (kept for malformed-payload note).
  exit   — 0 in ALL normal cases (allow + advisory). Exit 2 is reserved
           for the malformed-payload fail-closed branch ONLY.

Sub-agent auto-skip:
  Claude Code v2.1.114+ passes ``agent_id`` and ``agent_type`` fields in the
  hook stdin JSON for sub-agent sessions. If ``agent_id`` is a non-empty
  string the gate's purpose (force the Lead to delegate) is already
  satisfied — delegation has happened. We auto-skip (exit 0) and log the
  event for post-hoc surveillance.

State:
  <project_root>/.claude/.delegate_counter — integer count of action calls
  since the last delegation signal. Plain-text, single line.

"Actions" (counted, budget = 1):
  Bash (non-recon), Edit, Write, NotebookEdit

"Recon Bash" (NOT counted — free, like Reads):
  Read-only / diagnostic Bash: git status/diff/log, ls/find/grep,
  curl/wget, ssh, docker ps/logs, gh pr/issue, .claude/scripts/*,
  pip/npm list-family.  Matched by RECON_BASH_PATTERNS.

"Reads" (NOT counted — free, unlimited):
  Read, Grep, Glob, WebSearch, WebFetch, ToolSearch, and any other tool
  not in the actions list.

"Delegation signals" (reset counter to 0, then allow):
  Agent       — main Claude spawned a sub-agent (Explore/Plan/general-purpose)
  TaskCreate  — TaskCreate is the orchestrator's planning primitive
  Bash invoking `python3 ~/.claude/scripts/supervisor/supervisor.py *`
    (or any path ending in /supervisor/supervisor.py) — /supervise worker spawn
  Bash invoking `codex_worker.sh <model>` or `codex exec -m <model>` —
    Codex CLI delegation (same budget-reset semantics as supervisor spawn).
  Bash invoking `mcp__pal__*` via shell is impossible — PAL is its own tool
    but since it runs a deep Claude-like analysis, it counts as delegation.
  Because the over-budget response is now advisory (exit 0), the Agent /
  TaskCreate sibling in a parallel batch always survives and resets the
  counter — the advisory is self-clearing.

Bypass (LEAD ONLY — sub-agents cannot self-disable):
  env CLAUDE_BOOSTER_SKIP_DELEGATE_GATE=1
  path allowlist match (reports/ audits/ *.md .claude/ etc.)
  (The legacy <project_root>/.claude/.delegate_mode file bypass has been
  RETIRED — the gate is advisory, so a bypass is no longer needed. Stale
  .delegate_mode files on disk are ignored.)

Phase-aware exemption:
  When <project_root>/.claude/.phase contains RECON or PLAN, the gate
  allows all tool calls without counting — these phases are inherently
  read-only (phase_gate.py blocks Edit/Write separately), so delegation
  enforcement is counterproductive.  If .phase is absent or contains any
  other value, the gate enforces normally.

Decision telemetry:
  Every invocation appends one JSON line to
  ~/.claude/logs/delegate_gate_decisions.jsonl with fields
  {ts, gate, decision, reason, agent_id, agent_type, tool_name, cwd,
  project, session_id, counter, budget}. Over-budget events log
  decision='advisory' (was 'block'). Fail-soft: log failures are swallowed
  — the gate's primary job is gating, not logging.

Limitations:
  - Per-project state in the repo, so parallel sessions on the same repo
    share the same counter (race-prone but state is idempotent).
  - Agent-spawn subprocesses run in their own tool context — the inner
    Claude's tool calls hit THEIR own hooks, not this one.
  - Advisory only: an over-budget action is NOT blocked, just nudged. The
    block-rate in gate_stats.py reads ~0 because over-budget now logs
    'advisory', not 'block' (gate_stats does not yet count 'advisory').
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from pathlib import Path

try:
    from _gate_common import (
        DECISION_ADVISORY,
        DECISION_ALLOW,
        DECISION_AUTO_SKIP,
        DECISION_BLOCK,
        DELEGATE_LOG_NAME,
        append_jsonl,
        is_subagent_context,
        iso_now,
        project_root_from,
        redact_secrets,
    )
except ImportError:
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    from _gate_common import (  # type: ignore[no-redef]
        DECISION_ADVISORY,
        DECISION_ALLOW,
        DECISION_AUTO_SKIP,
        DECISION_BLOCK,
        DELEGATE_LOG_NAME,
        append_jsonl,
        is_subagent_context,
        iso_now,
        project_root_from,
        redact_secrets,
    )

BUDGET = int(os.environ.get("CLAUDE_BOOSTER_DELEGATE_BUDGET", "1"))
STATE_FILE_REL = ".claude/.delegate_counter"

# Phases exempt from the delegation budget — read-only by design;
# phase_gate.py separately blocks Edit/Write during RECON/PLAN.
EXEMPT_PHASES = {"RECON", "PLAN"}

# Tools that count against the budget when called directly by main Claude.
ACTIONS = {"Bash", "Edit", "Write", "NotebookEdit"}

# Tools that reset the counter (delegation happened).
DELEGATION_TOOLS = {"Agent", "TaskCreate"}

# Supervisor-spawn patterns for Bash — also counts as delegation.
SUPERVISOR_BASH_PATTERNS = [
    re.compile(r"python3?\s+[^\s]*\.claude/scripts/supervisor/supervisor\.py\b"),
    re.compile(r"python3?\s+-m\s+supervisor\.supervisor\b"),
]

# Codex worker spawn — treat as delegation signal, not direct action.
# Anchor and model charset must match model_metric_capture.py _RE_CODEX_* patterns.
CODEX_WORKER_PATTERNS = [
    re.compile(r'(?:^|[/;&|])\s*codex_worker\.sh\s+[a-zA-Z][a-zA-Z0-9._-]*'),
    re.compile(r'(?:^|[/;&|])\s*codex_sandbox_worker\.sh\s+[a-zA-Z][a-zA-Z0-9._-]*'),
    re.compile(r'(?:^|[/;&|])\s*codex\s+exec\s+(?:[^|;&\n]+?\s)?-m\s+[a-zA-Z][a-zA-Z0-9._-]*'),
]

# Recon Bash — read-only / diagnostic, exempt from budget like Read/Grep.
# Gate enforces workflow discipline, not safety (that's permissions.deny).
RECON_BASH_PATTERNS = [
    re.compile(r"python3?\s+\S+\.claude/scripts/(?!supervisor/)"),
    re.compile(r"\bgit\s+(-\w+\s+\S*\s+)*(status|diff|log|show|branch|tag|rev-parse|describe|ls-files|ls-tree|blame|shortlog|remote|fetch|stash\s+list|config|add|commit|push|worktree|cherry-pick|merge|rebase)\b"),
    re.compile(r"\bssh\b"),
    re.compile(r"(?:^|&&\s*|;\s*)(ls|find|grep|egrep|fgrep|rg|ag|cat|head|tail|wc|file|stat|du|df|diff|md5sum|shasum|sha256sum|which|type|command|echo|printf|date|whoami|hostname|uname|id|pwd|realpath|dirname|basename|env|printenv)\b"),
    re.compile(r"\b(curl|wget)\b"),
    re.compile(r"\bdocker\s+(ps|logs|inspect|images|stats|top|compose\s+(ps|logs))\b"),
    re.compile(r"\bgh\s+(pr|issue|api|auth|repo|run)\s"),
    re.compile(r"\b(pip3?|npm|yarn|bun|cargo|go)\s+(list|show|info|outdated|audit|why|ls)\b"),
    re.compile(r"\b(python3?|node|ruby|perl)\s+(-[ceEp]\b|-m\s)"),
    re.compile(r"\b(jq|yq|sqlite3|psql|mysql|redis-cli)\b"),
]

ALLOWLIST_PATHS = [
    r"/docs/", r"/doc/", r"/reports/", r"/audits/", r"/tests/", r"/test/",
    r"/\.claude/", r"\.md$", r"\.txt$", r"README", r"CLAUDE\.md$",
    r"/scratch/", r"/tmp/", r"\.log$",
]

# Pipe targets that are safe to receive recon output (read from stdin, no file edits).
# A pipe chain ending in one of these stays recon; piping to bash/sh/eval makes it non-recon.
_SAFE_PIPE_TARGETS: frozenset[str] = frozenset({
    "jq", "yq", "grep", "egrep", "fgrep", "rg", "ag",
    "head", "tail", "wc", "sort", "uniq", "tee", "cat",
    "less", "more", "column", "cut", "awk", "sed", "tr", "xargs",
})

# (destructive-ssh detection retired 2026-05-30: the gate is advisory and all ssh
# is recon — remote-command safety is permissions.deny, not the delegate budget.)

# Regex to find unquoted shell operators (&&, ||, ;) for compound-command splitting.
# We walk the string manually to skip content inside single or double quotes.
_COMPOUND_SPLIT_RE = re.compile(r"&&|\|\||;")

# Regex to detect unquoted output redirects to real files — these make a command
# non-recon even if it otherwise looks read-only (e.g. "echo hi > file.txt").
#
# Matches: [012]? >> or > followed by a non-/dev/null, non-/dev/stderr,
#          non-fd-dup (&), non-/dev/fd/ target.
# Does NOT match:
#   2>/dev/null   — suppress stderr, harmless
#   2>/dev/stderr — harmless
#   2>&1          — fd duplication, no file write (?!& lookahead)
#   >&2           — same
#   > /dev/fd/N   — fd via /dev/fd path
# The leading (?:^|[^'"]) skips > chars that are preceded by a quote character
# (i.e. the > sits inside a quoted string like grep ">" file).
_REDIRECT_TO_FILE_RE = re.compile(
    r"(?:^|[^'\"])\s*[012]?\s*>{1,2}\s*(?!/dev/(?:null|stderr)\b)(?!/dev/fd/)(?!&)\S"
)

# Trivially-safe command substitutions — $(pwd), $(git rev-parse ...), etc.
# These are read-only by definition and do not alter shell state.
_SAFE_SUBST_RE = re.compile(
    r"\$\(\s*(?:pwd|git\s+rev-parse|git\s+describe|date|which|command\s+-v|basename|dirname|realpath)\b[^)]*\)",
)

# SSH command detector — used to narrow ssh calls for destructive-payload check.
_SSH_CMD_RE = re.compile(r"\bssh\b")

# Git write-ops — source-control delivery commands (add/commit/push/tag/etc.).
# $(cat <<'EOF'...) in commit messages is legitimate, not a local mutation.
_GIT_WRITE_OPS_RE = re.compile(
    r"\bgit\s+(-\w+\s+\S*\s+)*(add|commit|push|worktree|cherry-pick|merge|rebase|tag)\b"
)

# gh CLI — all subcommands are integration/delivery, not inline coding.
_GH_CMD_RE = re.compile(r"\bgh\s+\w")

# Pipe targets that execute arbitrary code — piping to any of these makes a
# command non-recon regardless of how read-only the source side looks.
_DANGEROUS_PIPE_TARGETS: frozenset[str] = frozenset({
    "bash", "sh", "dash", "zsh", "exec", "eval",
    "python", "python3", "node", "ruby", "perl",
})


def _split_compound(cmd: str) -> list[str]:
    """Split a shell command on unquoted &&, ||, ; operators.

    Quoted sections (single or double quotes) are treated as opaque; operators
    inside them are ignored. Returns a list of individual command segments with
    leading/trailing whitespace stripped. Empty segments are dropped.

    This is intentionally simple — it handles the common cases without a full
    POSIX shell parser.  The gate is a workflow-discipline tool, not a security
    boundary; shlex is not used here because it recognises neither && nor ||.
    """
    if not _COMPOUND_SPLIT_RE.search(cmd):
        return [cmd.strip()] if cmd.strip() else []
    segments: list[str] = []
    current_chars: list[str] = []
    in_quote: str | None = None  # None, "'", or '"'
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if in_quote:
            current_chars.append(ch)
            if ch == in_quote:
                in_quote = None
            i += 1
        elif ch in ('"', "'"):
            in_quote = ch
            current_chars.append(ch)
            i += 1
        elif cmd[i : i + 2] in ("&&", "||"):
            segments.append("".join(current_chars).strip())
            current_chars = []
            i += 2
        elif ch == ";":
            segments.append("".join(current_chars).strip())
            current_chars = []
            i += 1
        else:
            current_chars.append(ch)
            i += 1
    segments.append("".join(current_chars).strip())
    return [s for s in segments if s]


def _segment_is_recon(segment: str) -> bool:
    """Return True if a single (non-compound) command segment is recon-safe.

    Handles:
    - SSH: ALL ssh is recon (ops/delivery; the gate is workflow-discipline,
      not a remote-command safety boundary — that is permissions.deny).
    - Pipe chains: the segment may contain pipes; every piped sub-command must
      be either a RECON_BASH_PATTERN match or a _SAFE_PIPE_TARGETS entry.
    - Command substitution ($(...) / backticks): treated conservatively as
      non-recon unless the substitution is a trivially-safe read-only form
      ($(pwd), $(git rev-parse ...), $(date ...), $(which ...)).
    """
    stripped = segment.strip()

    # Trivially safe shell builtins — read-only by definition.
    if (
        stripped in ("cd", "true", "false", ":")
        or stripped.startswith("cd ")
        or stripped.startswith("cd\t")
    ):
        return True

    # Git source-control early-exit: add/commit/push/tag/etc. deliver work.
    # Must fire BEFORE redirect/subst guards — commit messages contain > and $()
    # in Co-Authored-By emails and heredoc formatting, not actual shell ops.
    # Destructive ops (push --force, reset --hard) are in permissions.deny.
    if _GIT_WRITE_OPS_RE.search(stripped):
        return True

    # gh CLI early-exit: PR/issue/release/repo ops are integration, not coding.
    if _GH_CMD_RE.search(stripped):
        return True

    # SSH early-exit: ALL SSH is exempt from delegation budget.
    # Gate enforces "delegate code work" — SSH is operations/delivery, not coding.
    # dep_guard/financial_dml_guard can't observe remote commands anyway;
    # delegating SSH to an Agent adds zero safety value.
    if _SSH_CMD_RE.search(stripped):
        return True

    # Output-redirect guard: any unquoted > or >> to a real file makes this
    # segment non-recon, even if the command itself is read-only.
    # /dev/null, /dev/stderr, and fd-duplication (&) are exempted.
    if _REDIRECT_TO_FILE_RE.search(segment):
        return False

    # Command substitution guard — conservative approach.
    # Allow only trivially-safe $(…) forms; anything else is non-recon.
    if "$(" in segment or "`" in segment:
        # Strip out all safe substitutions; if any $(…) or ` remains → non-recon.
        subst_residual = _SAFE_SUBST_RE.sub("", segment)
        if "$(" in subst_residual or "`" in subst_residual:
            return False

    # /tmp file operations are prep-work (temp scripts, patches), not project
    # code editing. Covers: sed /tmp/..., cat > /tmp/..., cp X /tmp/..., etc.
    if re.search(r"(/tmp/|/private/tmp/|/var/tmp/)", stripped) and not re.search(r"(/home/|/opt/|/srv/|/etc/|/usr/)", stripped):
        return True

    # Split on pipes to inspect each piped segment individually.
    # Only the first part needs to match RECON_BASH_PATTERNS; subsequent parts
    # must be safe pipe targets.  We do a simple unquoted-pipe split here.
    if '|' not in segment:
        # No pipes — just check the full segment against patterns.
        pipe_parts = [segment]
    else:
        pipe_parts = re.split(r"(?<![|])\|(?![|])", segment)  # single | not part of ||

    first_part = pipe_parts[0].strip()

    # The first part must match at least one RECON_BASH_PATTERN.
    if not any(p.search(first_part) for p in RECON_BASH_PATTERNS):
        return False

    # Each subsequent piped command must be a known safe pipe target.
    for pipe_part in pipe_parts[1:]:
        # Extract the leading command name (first word, ignoring flags).
        pipe_cmd = pipe_part.strip().lstrip("| ").split()[0] if pipe_part.strip() else ""
        # Strip any path prefix (e.g. /usr/bin/grep → grep).
        pipe_cmd = pipe_cmd.rsplit("/", 1)[-1]
        # Piping to bash/sh/exec/eval/python/node → definitely not recon.
        if pipe_cmd in _DANGEROUS_PIPE_TARGETS:
            return False
        # Piping to something not in the safe set → conservative non-recon.
        if pipe_cmd and pipe_cmd not in _SAFE_PIPE_TARGETS:
            return False

    return True


def _project_root(cwd_hint: str) -> Path:
    found = project_root_from(cwd_hint)
    if found is not None:
        return found
    # Fallback: honour the hint path even if no marker was found, else $HOME.
    try:
        return Path(cwd_hint) if cwd_hint else Path.cwd()
    except (FileNotFoundError, OSError):
        return Path.home()


def _current_phase(root: Path) -> str | None:
    """Read the current workflow phase from <project_root>/.claude/.phase.

    Returns the phase name in UPPER CASE (e.g. "RECON", "PLAN"), or None if
    the file does not exist or cannot be read.  Callers compare against
    EXEMPT_PHASES to decide whether to skip budget enforcement.
    """
    phase_file = root / ".claude" / ".phase"
    if not phase_file.exists():
        return None
    try:
        return phase_file.read_text().strip().upper()
    except OSError:
        return None


def _read_counter(root: Path) -> int:
    """Read counter value without locking — for telemetry snapshots only, not decisions."""
    path = root / STATE_FILE_REL
    if not path.exists():
        return 0
    try:
        return max(0, int(path.read_text().strip()))
    except (ValueError, OSError):
        return 0


def _atomic_increment(root: Path) -> int:
    """Atomically read, increment, and write counter. Returns the NEW value.

    Uses fcntl.flock for mutual exclusion — two parallel calls on the same
    project will serialize, not race.
    """
    path = root / STATE_FILE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = os.read(fd, 64).decode("utf-8", errors="replace").strip()
            try:
                current = max(0, int(data)) if data else 0
            except ValueError:
                # Corrupt counter — repair to fail-closed value
                closed = BUDGET + 1
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, f"{closed}\n".encode())
                return closed
            new_val = current + 1
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{new_val}\n".encode())
            return new_val
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except OSError:
        return BUDGET + 1  # fail-closed: assume budget consumed


def _atomic_check_and_increment(root: Path, budget: int) -> tuple[int, bool]:
    """Atomically check budget, increment only if within budget.

    Returns (counter_value, incremented). If counter >= budget BEFORE this call,
    returns (current_value, False) — no state mutation. If counter < budget,
    increments and returns (new_value, True).
    """
    path = root / STATE_FILE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = os.read(fd, 64).decode("utf-8", errors="replace").strip()
            try:
                current = max(0, int(data)) if data else 0
            except ValueError:
                closed = budget + 1
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, f"{closed}\n".encode())
                return closed, False
            if current >= budget:
                return current, False
            new_val = current + 1
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{new_val}\n".encode())
            return new_val, True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except OSError:
        return budget + 1, False


def _atomic_reset(root: Path) -> None:
    """Atomically reset counter to 0. Used on delegation signals."""
    path = root / STATE_FILE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.ftruncate(fd, 0)
            os.write(fd, b"0\n")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except OSError:
        pass


def _path_allowlisted(tool_input: dict) -> bool:
    for key in ("file_path", "path", "notebook_path"):
        v = tool_input.get(key)
        if not v:
            continue
        s = str(v)
        for pat in ALLOWLIST_PATHS:
            if re.search(pat, s):
                return True
    return False


def _bash_is_supervisor_spawn(cmd: str) -> bool:
    return any(p.search(cmd) for p in SUPERVISOR_BASH_PATTERNS)


def _bash_is_codex_worker(cmd: str) -> bool:
    return any(p.search(cmd) for p in CODEX_WORKER_PATTERNS)


def _bash_is_recon(cmd: str) -> bool:
    """Return True only when ALL compound segments of cmd are individually recon-safe.

    A compound command like ``git status && rm -rf foo`` must NOT be classified
    as recon just because the first segment matches — every segment must pass.
    Simple (non-compound) commands preserve their previous behaviour exactly,
    since _split_compound() returns a single-element list for them.
    """
    segments = _split_compound(cmd)
    if not segments:
        return False
    return all(_segment_is_recon(seg) for seg in segments)


def _advisory_nudge(counter: int) -> str:
    """Build the non-blocking over-budget advisory text (additionalContext)."""
    return (
        f"ℹ delegate_gate: {counter}/{BUDGET} direct actions this window — "
        f"prefer delegating code work via /go (Worker+Verifier). "
        f"Advisory only, not blocking."
    )


def _build_base_record(data: dict, root: Path) -> dict:
    return {
        "ts": iso_now(),
        "gate": "delegate",
        "agent_id": data.get("agent_id") or "",
        "agent_type": data.get("agent_type") or "",
        "tool_name": data.get("tool_name") or "",
        "cwd": data.get("cwd") or "",
        "project": root.name if root else "",
        "session_id": data.get("session_id") or "",
        "budget": BUDGET,
        "command_excerpt": redact_secrets(data.get("tool_input", {}).get("command", ""))[:200],
    }


def _delegation_reset(root: Path, base: dict, reason: str) -> int:
    """Reset budget counter and log a delegation-allow event. Returns 0."""
    _atomic_reset(root)
    append_jsonl(DELEGATE_LOG_NAME, {**base, "decision": DECISION_ALLOW, "reason": reason})
    return 0


def main() -> int:
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        raw = ""
    # Fail-closed on malformed/non-dict hook payload. An empty dict would
    # make `tool_name=""` → "not in ACTIONS" → allow, letting corrupted or
    # adversarial stdin silently bypass the gate.
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
        partial = {
            "ts": iso_now(),
            "gate": "delegate",
            "decision": DECISION_BLOCK,
            "reason": "invalid hook payload (malformed or non-dict stdin)",
        }
        append_jsonl(DELEGATE_LOG_NAME, partial)
        sys.stderr.write("delegate_gate: malformed hook payload, blocking fail-closed\n")
        return 2

    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or ""
    is_subagent = is_subagent_context(data)

    root = _project_root(cwd)
    base = _build_base_record(data, root)
    base["counter"] = _read_counter(root)

    # Sub-agent: delegation has already happened, the gate's job is done.
    # Auto-skip (exit 0). dep_guard auto-skips sub-agents the same way.
    if is_subagent:
        append_jsonl(DELEGATE_LOG_NAME, {
            **base,
            "decision": DECISION_AUTO_SKIP,
            "reason": "sub-agent context (agent_id/agent_type set)",
        })
        return 0

    if os.environ.get("CLAUDE_BOOSTER_SKIP_DELEGATE_GATE") == "1":
        append_jsonl(DELEGATE_LOG_NAME, {
            **base,
            "decision": DECISION_ALLOW,
            "reason": "env CLAUDE_BOOSTER_SKIP_DELEGATE_GATE=1",
        })
        return 0

    phase = _current_phase(root)
    if phase in EXEMPT_PHASES:
        _atomic_reset(root)
        append_jsonl(DELEGATE_LOG_NAME, {
            **base,
            "decision": DECISION_ALLOW,
            "reason": f"phase {phase!r} exempt from delegation budget",
        })
        return 0

    if tool in DELEGATION_TOOLS:
        return _delegation_reset(root, base, f"delegation signal {tool!r} resets counter")
    if tool == "Bash":
        cmd = tool_input.get("command") or ""
        if _bash_is_supervisor_spawn(cmd):
            return _delegation_reset(root, base, "supervisor spawn resets counter")
        if _bash_is_codex_worker(cmd):
            return _delegation_reset(root, base, "codex_worker spawn resets counter")
        if _bash_is_recon(cmd):
            append_jsonl(DELEGATE_LOG_NAME, {
                **base,
                "decision": DECISION_ALLOW,
                "reason": "recon bash (read-only pattern match)",
            })
            return 0

    if tool not in ACTIONS:
        append_jsonl(DELEGATE_LOG_NAME, {
            **base,
            "decision": DECISION_ALLOW,
            "reason": f"tool {tool!r} not in ACTIONS (free)",
        })
        return 0

    if _path_allowlisted(tool_input):
        append_jsonl(DELEGATE_LOG_NAME, {
            **base,
            "decision": DECISION_ALLOW,
            "reason": "path allowlist match",
        })
        return 0

    counter_val, incremented = _atomic_check_and_increment(root, BUDGET)
    if not incremented:
        # Over budget — ADVISORY, not a block. Emit a non-blocking nudge via
        # additionalContext and exit 0. A hard block (exit 2) here would cause
        # the harness to cancel every sibling call in the same parallel batch,
        # including the Agent-spawn that would reset the counter. The advisory
        # print is the ONLY stdout write on any code path. The try/except is
        # MANDATORY: a BrokenPipe/OSError on the write must NOT degrade into a
        # non-zero exit — that would resurrect the sibling-cancellation cascade.
        try:
            print(json.dumps({"additionalContext": _advisory_nudge(counter_val)}))
        except OSError:
            pass
        append_jsonl(DELEGATE_LOG_NAME, {
            **base,
            "decision": DECISION_ADVISORY,
            "reason": f"budget exhausted ({counter_val}/{BUDGET}) — advisory nudge",
            "counter": counter_val,
        })
        return 0

    append_jsonl(DELEGATE_LOG_NAME, {
        **base,
        "decision": DECISION_ALLOW,
        "reason": f"within budget ({counter_val}/{BUDGET})",
        "counter": counter_val,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
