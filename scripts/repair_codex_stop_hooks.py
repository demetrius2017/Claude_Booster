#!/usr/bin/env python3
"""Repair Codex Stop hooks imported from Claude Code.

Purpose:
  Remove legacy presentation hooks whose plain-text stdout violates Codex's
  Stop JSON contract, and route ``ask_gate.py`` through its Codex adapter.

Contract:
  input  — ``~/.codex/hooks.json`` (override with ``--hooks``).
  output — atomically rewritten JSON; Claude settings are never read or changed.
  stdout — one JSON summary suitable for automation.

Limitations:
  Only the Stop event is modified. Unknown hook entries are preserved.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import tempfile
from pathlib import Path


LEGACY_STOP_BASENAMES = frozenset({"on_stop.sh"})


def _command_target(command: object) -> tuple[list[str], int, str] | None:
    """Resolve only the executable/script position, never a trailing argument."""
    if not isinstance(command, str):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts:
        return None

    executable = Path(parts[0]).name
    if executable == "env":
        if len(parts) < 3 or not Path(parts[1]).name.startswith("python"):
            return None
        index = 2
    elif executable.startswith("python"):
        if len(parts) < 2:
            return None
        index = 1
    else:
        index = 0
    return parts, index, Path(parts[index]).name


def repair_hooks(data: dict) -> tuple[dict, dict]:
    """Return validated repaired config and a deterministic change summary."""
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        raise ValueError("hooks config must be an object containing a hooks object")

    stop_groups = data["hooks"].get("Stop", [])
    if not isinstance(stop_groups, list):
        raise ValueError("hooks.Stop must be an array")

    removed: list[str] = []
    adapted = 0
    repaired_groups: list[dict] = []
    for group in stop_groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise ValueError("each hooks.Stop entry must contain a hooks array")
        kept: list[dict] = []
        for hook in group["hooks"]:
            if not isinstance(hook, dict):
                raise ValueError("each Stop hook must be an object")
            target = _command_target(hook.get("command"))
            basename = target[2] if target else ""
            if basename in LEGACY_STOP_BASENAMES:
                removed.append(basename)
                continue
            new_hook = dict(hook)
            if basename == "ask_gate.py":
                parts, index, _ = target
                parts[index] = str(Path(parts[index]).with_name("codex_ask_gate.py"))
                new_hook["command"] = shlex.join(parts)
                adapted += 1
            kept.append(new_hook)
        if kept:
            new_group = dict(group)
            new_group["hooks"] = kept
            repaired_groups.append(new_group)

    repaired = dict(data)
    repaired_hooks = dict(data["hooks"])
    repaired_hooks["Stop"] = repaired_groups
    repaired["hooks"] = repaired_hooks
    summary = {
        "removed": sorted(removed),
        "adapted_ask_gate": adapted,
        "stop_hook_count": sum(len(group["hooks"]) for group in repaired_groups),
    }
    return repaired, summary


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hooks", type=Path, default=Path.home() / ".codex/hooks.json")
    args = parser.parse_args()

    raw = json.loads(args.hooks.read_text(encoding="utf-8"))
    repaired, summary = repair_hooks(raw)
    _atomic_write_json(args.hooks, repaired)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
