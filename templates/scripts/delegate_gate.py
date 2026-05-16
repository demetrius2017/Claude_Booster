#!/usr/bin/env python3
"""
PreToolUse hook: enforce "delegate, don't do" via a 1-action budget.

Purpose:
  The Lead (main Claude session) is supposed to orchestrate agents, not do
  the substantive work itself. pipeline.md says so, but soft rules get
  ignored. This hook enforces the same rule at the harness layer: main
  Claude may perform at most 1 "action" tool call per delegation window,
  after which it MUST delegate (Agent / TaskCreate / /supervise) before
  doing another direct action.

Contract:
  stdin  — PreToolUse JSON {tool_name, tool_input, cwd, agent_id, agent_type,
           session_id}
  stderr — feedback on block
  exit   — 0 allow, 2 block

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

Bypass (LEAD ONLY — sub-agents cannot self-disable):
  env CLAUDE_BOOSTER_SKIP_DELEGATE_GATE=1
  file <project_root>/.claude/.delegate_mode containing 'off:<session_id>'
       (session-scoped: only honoured when the session_id in the file matches
       the current session's session_id, so the bypass expires automatically
       at session end; bare 'off' without a session_id is treated as expired
       and ignored — this prevents permanent bypass from stale forgotten files;
       sub-agent self-bypass is refused and logged to
       ~/.claude/logs/gate_bypass_attempts.jsonl)
  path allowlist match (reports/ audits/ *.md .claude/ etc.)

Phase-aware exemption:
  When <project_root>/.claude/.phase contains RECON or PLAN, the gate
  allows all tool calls without counting — these phases are inherently
  read-only (phase_gate.py blocks Edit/Write separately), so delegation
  enforcement is counterproductive.  If .phase is absent or contains any
  other value, the gate enforces normally.

  The stderr block message deliberately does NOT mention the bypass file
  path — sub-agents read the same stderr and adopt it as a fix-recipe.
  README keeps the documentation for human Leads.

Decision telemetry:
  Every invocation appends one JSON line to
  ~/.claude/logs/delegate_gate_decisions.jsonl with fields
  {ts, gate, decision, reason, agent_id, agent_type, tool_name, cwd,
  project, session_id, counter, budget}. Fail-soft: log failures are
  swallowed — the gate's primary job is gating, not logging.

Limitations:
  - Per-project state in the repo, so parallel sessions on the same repo
    share the same counter (race-prone but state is idempotent).
  - Agent-spawn subprocesses run in their own tool context — the inner
    Claude's tool calls hit THEIR own hooks, not this one.
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
        BYPASS_LOG_NAME,
        DECISION_ALLOW,
        DECISION_AUTO_SKIP,
        DECISION_BLOCK,
        DECISION_BYPASS_HONOURED,
        DECISION_BYPASS_REFUSED,
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
        BYPASS_LOG_NAME,
        DECISION_ALLOW,
        DECISION_AUTO_SKIP,
        DECISION_BLOCK,
        DECISION_BYPASS_HONOURED,
        DECISION_BYPASS_REFUSED,
        DELEGATE_LOG_NAME,
        append_jsonl,
        is_subagent_context,
        iso_now,
        project_root_from,
        redact_secrets,
    )

BUDGET = int(os.environ.get("CLAUDE_BOOSTER_DELEGATE_BUDGET", "1"))
STATE_FILE_REL = ".claude/.delegate_counter"
MODE_FILE_REL = ".claude/.delegate_mode"

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

# Keywords in ssh argument strings that indicate a destructive remote command.
_DESTRUCTIVE_SSH_PATTERNS = [
    re.compile(r"\brm\b"),
    re.compile(r"\bdd\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bkill\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bdocker\s+(rm|stop|kill)\b"),
]

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
    - SSH narrowing: ssh with a destructive payload → NOT recon.
    - Pipe chains: the segment may contain pipes; every piped sub-command must
      be either a RECON_BASH_PATTERN match or a _SAFE_PIPE_TARGETS entry.
    - Command substitution ($(...) / backticks): treated conservatively as
      non-recon unless the substitution is a trivially-safe read-only form
      ($(pwd), $(git rev-parse ...), $(date ...), $(which ...)).
    """
    # Output-redirect guard: any unquoted > or >> to a real file makes this
    # segment non-recon, even if the command itself is read-only.
    # /dev/null, /dev/stderr, and fd-duplication (&) are exempted.
    if _REDIRECT_TO_FILE_RE.search(segment):
        return False

    # Trivially safe shell builtins — read-only by definition.
    # 'cd' changes the shell's CWD which is non-persistent between Bash calls in
    # Claude Code (each call gets a fresh shell), so it has no lasting side-effect.
    # 'true', 'false', ':' are no-ops used as separators in compound commands.
    stripped = segment.strip()
    if (
        stripped in ("cd", "true", "false", ":")
        or stripped.startswith("cd ")
        or stripped.startswith("cd\t")
    ):
        return True

    # SSH early-exit: non-destructive SSH is always recon — heredoc content
    # executes remotely, so $()/pipes inside it are not local mutations.
    if _SSH_CMD_RE.search(stripped):
        for pat in _DESTRUCTIVE_SSH_PATTERNS:
            if pat.search(stripped):
                return False
        return True

    # Command substitution guard — conservative approach.
    # Allow only trivially-safe $(…) forms; anything else is non-recon.
    if "$(" in segment or "`" in segment:
        # Strip out all safe substitutions; if any $(…) or ` remains → non-recon.
        subst_residual = _SAFE_SUBST_RE.sub("", segment)
        if "$(" in subst_residual or "`" in subst_residual:
            return False

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


def _mode_disabled(root: Path, session_id: str) -> bool:
    """Check if delegate gate is disabled for this session.

    Format: 'off:<session_id>' — bypass is scoped to one session.
    Legacy bare 'off' (no session_id) is treated as EXPIRED and ignored,
    preventing permanent bypass from stale files.
    """
    path = root / MODE_FILE_REL
    if not path.exists():
        return False
    try:
        content = path.read_text().strip().lower()
    except OSError:
        return False
    if content == "off":
        # Legacy bare 'off' without session scope — treat as expired.
        # This prevents permanent bypass from forgotten files.
        return False
    if content.startswith("off:"):
        file_session = content[4:].strip()
        return file_session == session_id.lower()
    return False


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


def _feedback(root: Path, tool: str, counter: int) -> str:
    return f"delegate_gate: → Agent ({counter}/{BUDGET} on {tool!r})"


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
    session_id = data.get("session_id") or ""
    is_subagent = is_subagent_context(data)

    root = _project_root(cwd)
    base = _build_base_record(data, root)
    base["counter"] = _read_counter(root)

    # Sub-agent: delegation has already happened, the gate's job is done.
    # But if the sub-agent ALSO wrote .delegate_mode=off to self-bypass,
    # log the refused attempt for surveillance before returning.
    if is_subagent:
        attempted_bypass = _mode_disabled(root, session_id)
        if attempted_bypass:
            append_jsonl(BYPASS_LOG_NAME, {
                **base,
                "decision": DECISION_BYPASS_REFUSED,
                "reason": "sub-agent cannot disable gate",
                "bypass_file": str(root / MODE_FILE_REL),
            })
        auto_skip_rec = {
            **base,
            "decision": DECISION_AUTO_SKIP,
            "reason": "sub-agent context (agent_id/agent_type set)",
        }
        if attempted_bypass:
            auto_skip_rec["attempted_bypass"] = True
        append_jsonl(DELEGATE_LOG_NAME, auto_skip_rec)
        return 0

    if os.environ.get("CLAUDE_BOOSTER_SKIP_DELEGATE_GATE") == "1":
        append_jsonl(DELEGATE_LOG_NAME, {
            **base,
            "decision": DECISION_ALLOW,
            "reason": "env CLAUDE_BOOSTER_SKIP_DELEGATE_GATE=1",
        })
        return 0

    if _mode_disabled(root, session_id):
        append_jsonl(BYPASS_LOG_NAME, {
            **base,
            "decision": DECISION_BYPASS_HONOURED,
            "reason": "lead context honoured .delegate_mode=off",
            "bypass_file": str(root / MODE_FILE_REL),
        })
        append_jsonl(DELEGATE_LOG_NAME, {
            **base,
            "decision": DECISION_BYPASS_HONOURED,
            "reason": ".delegate_mode=off (lead)",
            "attempted_bypass": True,
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

    new_counter = _atomic_increment(root)
    if new_counter > BUDGET:
        sys.stderr.write(_feedback(root, tool, new_counter) + "\n")
        append_jsonl(DELEGATE_LOG_NAME, {
            **base,
            "decision": DECISION_BLOCK,
            "reason": f"budget exhausted ({new_counter}/{BUDGET})",
            "counter": new_counter,
        })
        return 2

    append_jsonl(DELEGATE_LOG_NAME, {
        **base,
        "decision": DECISION_ALLOW,
        "reason": f"within budget ({new_counter}/{BUDGET})",
        "counter": new_counter,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
