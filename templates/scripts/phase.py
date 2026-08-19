#!/usr/bin/env python3
"""Project-scoped workflow state machine with compatibility phase lease.

Purpose: retain legacy phase stdout while issuing structured expiring leases.
Contract: get/set/list output and .phase storage remain compatible; a failed
lease write never updates the legacy marker.
CLI/Examples: phase.py get; phase.py set IMPLEMENT; phase.py progress "4/7".
Limitations: lease is local advisory state, not a trusted CI authorization.
ENV/Files: CLAUDE_SESSION_ID optionally binds a lease; .claude/{.phase,phase_lease.json}.
phase.py — project-scoped phase state machine for Lead-Orchestrator workflow.

Contract:
  phase.py           → print current phase (alias of get)
  phase.py get       → print current phase; default RECON if unset
  phase.py set NAME  → set phase, issue expiring root/run-bound lease, print prev→new
  phase.py progress TEXT → append an auditable progress event
  phase.py progress clear → safely remove progress state
  phase.py list      → list valid phases with short rule

Storage:
  <project_root>/.claude/.phase                — one phase name + newline
  <project_root>/.claude/phase_transitions.log — append-only audit

Project root = first ancestor of CWD containing .git/ or .claude/, else CWD.

Exit codes: 0 OK, 2 bad usage / invalid phase.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

VALID = ["RECON", "PLAN", "IMPLEMENT", "AUDIT", "VERIFY", "MERGE"]
DEFAULT = "RECON"

RULES = {
    "RECON":     "read-only exploration (Read/Grep/Glob/WebSearch); no Edit/Write",
    "PLAN":      "design + TaskCreate + consilium if uncertainty >30%; no code edits",
    "IMPLEMENT": "Edit/Write allowed; run tests after each change",
    "AUDIT":     "code review + PAL second opinion; no new code",
    "VERIFY":    "real curl / pytest / Chrome DevTools — collect evidence",
    "MERGE":     "git push after user acceptance; post-merge curl/console check",
}


def _project_root() -> Path:
    try:
        cwd = Path(os.getcwd())
    except (FileNotFoundError, OSError):
        return Path.home()
    for p in [cwd, *cwd.parents]:
        if (p / ".git").exists() or (p / ".claude").exists():
            return p
    return cwd


def _phase_file(root: Path) -> Path:
    return root / ".claude" / ".phase"


def get_phase() -> str:
    f = _phase_file(_project_root())
    if not f.exists():
        return DEFAULT
    try:
        v = f.read_text(encoding="utf-8").strip().upper()
        return v if v in VALID else DEFAULT
    except OSError:
        return DEFAULT


def set_phase(name: str) -> int:
    name = name.strip().upper()
    if name not in VALID:
        print(f"error: invalid phase '{name}'. valid: {VALID}", file=sys.stderr)
        return 2
    root = _project_root()
    f = _phase_file(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    prev = get_phase()
    # The legacy marker remains for all existing consumers, but is never a
    # development authorization.  test_dispatcher validates phase_lease.json.
    try:
        from test_modes_core import create_lease
        create_lease(root, name)
    except Exception as exc:
        print(f"error: phase marker not promoted to a valid lease: {exc}", file=sys.stderr)
        return 2
    f.write_text(name + "\n", encoding="utf-8")
    log = f.parent / "phase_transitions.log"
    try:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.utcnow().isoformat()}Z  {prev} -> {name}  (root={root})\n")
    except OSError:
        pass
    print(f"{prev} -> {name}")
    return 0


def progress(message: str) -> int:
    """Append structured phase progress without altering get/set/list stdout."""
    if not isinstance(message, str) or not message.strip() or len(message) > 500:
        print("error: progress message must be 1..500 characters", file=sys.stderr)
        return 2
    root = _project_root(); path = root / ".claude" / "phase_progress.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"timestamp": datetime.utcnow().isoformat() + "Z", "phase": get_phase(), "message": message.strip()}
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            fh.flush(); os.fsync(fh.fileno())
    except OSError as exc:
        print(f"error: cannot write phase progress: {exc}", file=sys.stderr); return 2
    print(f"{event['phase']}: {event['message']}")
    return 0


def clear_progress() -> int:
    """Remove only the regular, project-scoped progress file."""
    path = _project_root() / ".claude" / "phase_progress.jsonl"
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            print("error: unsafe phase progress path", file=sys.stderr)
            return 2
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"error: cannot clear phase progress: {exc}", file=sys.stderr)
        return 2
    if path.exists():
        print("error: phase progress clear invariant failed", file=sys.stderr)
        return 2
    print("progress cleared")
    return 0


def list_phases() -> int:
    for name in VALID:
        print(f"  {name:<9} {RULES[name]}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] == "get":
        print(get_phase())
        return 0
    cmd = argv[1]
    if cmd == "set":
        if len(argv) < 3:
            print("usage: phase.py set <PHASE>", file=sys.stderr)
            return 2
        return set_phase(argv[2])
    if cmd == "list":
        return list_phases()
    if cmd == "progress":
        if len(argv) < 3:
            print("usage: phase.py progress <TEXT>", file=sys.stderr); return 2
        if len(argv) == 3 and argv[2] == "clear":
            return clear_progress()
        return progress(" ".join(argv[2:]))
    print("usage: phase.py [get|set <PHASE>|list|progress <TEXT>]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
