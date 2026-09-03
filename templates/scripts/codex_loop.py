#!/usr/bin/env python3
"""Schedule bounded, token-efficient wakes of one explicit Codex thread.

Purpose
-------
Keeps waiting outside the model: a detached helper sleeps until each monotonic
deadline and invokes ``codex queue`` only when a wake is due.

Contract
--------
Input  : ``start INTERVAL --thread UUID`` plus bounded optional settings.
Output : JSON lifecycle status on stdout; errors on stderr.
Exit   : zero only for a successful lifecycle operation.

CLI
---
    codex_loop.py start 30m --thread <uuid>
    codex_loop.py status
    codex_loop.py stop

Limitations
-----------
One scheduler is allowed per canonical project identity.  The helper uses
POSIX file locking and is intended for macOS/Linux where Codex is supported.
It never guesses a thread and never sends catch-up bursts after suspension.
A local queue timeout is terminal and is not retried automatically, but the
remote server may already have accepted that wake; delivery is at-least-once
ambiguous at that boundary.

ENV / Files
-----------
Writes ``$XDG_STATE_HOME`` (or ``~/.local/state``) under
``claude-booster/loop/<sha256(project)>``.  State is owner-only and retained
after a terminal result for inspection.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = 1
DEFAULT_MAX_WAKES = 8
DEFAULT_QUEUE_TIMEOUT = 120.0
DEFAULT_MESSAGE = "Loop wake: inspect the current task and continue only if useful."
TERMINAL = frozenset({"completed", "failed", "stopped"})
ACTIVE = frozenset({"active", "queueing"})
INTERVAL_RE = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>[smh])$")


class LoopError(RuntimeError):
    """Raised when trusted loop lifecycle invariants cannot be met."""


def _interval(value: str) -> float:
    """Parse a positive compact interval without accepting ambiguous forms."""
    match = INTERVAL_RE.fullmatch(value.strip())
    if not match:
        raise argparse.ArgumentTypeError("interval must be an integer followed by s, m, or h (for example 30m)")
    multiplier = {"s": 1, "m": 60, "h": 3600}[match.group("unit")]
    seconds = int(match.group("value")) * multiplier
    if seconds > 31_536_000:
        raise argparse.ArgumentTypeError("interval must be at most one year")
    return float(seconds)


def _positive(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _project(cwd: str | None) -> Path:
    path = Path(cwd or os.getcwd()).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise LoopError(f"cwd is not a directory: {path}")
    return path


def _paths(project: Path) -> tuple[Path, Path, Path]:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")).expanduser()
    digest = hashlib.sha256(str(project).encode("utf-8")).hexdigest()
    directory = state_home / "claude-booster" / "loop" / digest
    return directory, directory / "state.json", directory / "lock"


def _secure_dir(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise LoopError(f"insecure state directory: {directory}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise LoopError(f"state directory must have mode 0700: {directory}")


def _trusted_file(path: Path, *, required: bool) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise LoopError(f"missing state file: {path}")
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise LoopError(f"insecure state file: {path}")
    if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        raise LoopError(f"state file must be single-link mode 0600: {path}")
    return True


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    _trusted_file(lock_path, required=False)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield None
    finally:
        os.close(fd)


def _validate(data: Any, project: Path) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {
        "schema", "project", "status", "pid", "thread", "message", "interval_s",
        "max_wakes", "queue_timeout_s", "attempts", "stop_requested", "updated_at", "detail",
    }:
        raise LoopError("state schema is malformed or from an incompatible helper")
    checks = (
        data["schema"] == SCHEMA,
        data["project"] == str(project),
        data["status"] in ACTIVE | TERMINAL,
        isinstance(data["pid"], int) and data["pid"] > 0,
        isinstance(data["thread"], str) and bool(data["thread"].strip()),
        isinstance(data["message"], str) and bool(data["message"].strip()),
        isinstance(data["interval_s"], (int, float)) and data["interval_s"] > 0,
        isinstance(data["max_wakes"], int) and data["max_wakes"] > 0,
        isinstance(data["queue_timeout_s"], (int, float)) and math.isfinite(data["queue_timeout_s"]) and data["queue_timeout_s"] > 0,
        isinstance(data["attempts"], int) and 0 <= data["attempts"] <= data["max_wakes"],
        isinstance(data["stop_requested"], bool), isinstance(data["updated_at"], str), isinstance(data["detail"], str),
    )
    if not all(checks):
        raise LoopError("state contains invalid types, values, or a different project binding")
    return data


def _read(state_path: Path, project: Path, *, required: bool) -> dict[str, Any] | None:
    if not _trusted_file(state_path, required=required):
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError(f"cannot parse trusted state: {exc}") from exc
    return _validate(data, project)


def _write(state_path: Path, data: dict[str, Any]) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    encoded = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".state.", dir=state_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_path)
        directory_fd = os.open(state_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _state(project: Path, args: argparse.Namespace, *, status: str, detail: str = "") -> dict[str, Any]:
    return {"schema": SCHEMA, "project": str(project), "status": status, "pid": os.getpid(),
            "thread": args.thread, "message": args.message, "interval_s": args.interval,
            "max_wakes": args.max_wakes, "queue_timeout_s": args.queue_timeout, "attempts": 0,
            "stop_requested": False, "updated_at": "", "detail": detail}


def _emit(data: dict[str, Any]) -> None:
    print(json.dumps({key: data[key] for key in ("status", "pid", "attempts", "max_wakes", "stop_requested", "detail", "project")}, sort_keys=True))


def _set_terminal(state_path: Path, project: Path, status: str, detail: str) -> None:
    with _locked(state_path.parent / "lock"):
        current = _read(state_path, project, required=True)
        assert current is not None
        current.update(status=status, detail=detail)
        _write(state_path, current)


def _queue(codex: str, state_path: Path, project: Path) -> tuple[bool, str]:
    with _locked(state_path.parent / "lock"):
        data = _read(state_path, project, required=True)
        assert data is not None
        if data["stop_requested"]:
            _write(state_path, {**data, "status": "stopped", "detail": "stop requested before queue"})
            return False, "stopped"
        if data["attempts"] >= data["max_wakes"]:
            _write(state_path, {**data, "status": "completed", "detail": "maximum wake attempts reached"})
            return False, "completed"
        data["attempts"] += 1
        data["status"] = "queueing"
        _write(state_path, data)
    command = [codex, "queue", "--thread", data["thread"], "--message", data["message"]]
    try:
        child = subprocess.Popen(command, cwd=project, shell=False, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        _, stderr = child.communicate(timeout=data["queue_timeout_s"])
    except subprocess.TimeoutExpired:
        # The child has its own process group; this never targets an ambiguous daemon PID.
        os.killpg(child.pid, signal.SIGTERM)
        try:
            _, stderr = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            _, stderr = child.communicate()
        return False, "queue timeout"
    if child.returncode != 0:
        return False, f"queue failed exit={child.returncode}: {(stderr or '').strip()[:300]}"
    with _locked(state_path.parent / "lock"):
        latest = _read(state_path, project, required=True)
        assert latest is not None
        if latest["stop_requested"]:
            _write(state_path, {**latest, "status": "stopped", "detail": "stop requested while queueing"})
            return False, "stopped"
        if latest["attempts"] >= latest["max_wakes"]:
            _write(state_path, {**latest, "status": "completed", "detail": "maximum wake attempts reached"})
            return False, "completed"
        _write(state_path, {**latest, "status": "active", "detail": "queue delivered"})
    return True, ""


def _run(args: argparse.Namespace) -> int:
    project = _project(args.cwd)
    directory, state_path, lock_path = _paths(project)
    _secure_dir(directory)
    codex = args.codex
    if not os.path.isabs(codex) or not os.access(codex, os.X_OK):
        raise LoopError("resolved codex executable is not an executable absolute path")
    with _locked(lock_path):
        existing = _read(state_path, project, required=False)
        if existing is not None and existing["status"] in ACTIVE:
            raise LoopError("a scheduler is already active for this project")
        data = _state(project, args, status="active")
        _write(state_path, data)
    os.write(args.ready_fd, b"READY\n")
    os.close(args.ready_fd)
    deadline = time.monotonic() + args.interval
    while True:
        while time.monotonic() < deadline:
            with _locked(lock_path):
                current = _read(state_path, project, required=True)
                assert current is not None
                if current["stop_requested"]:
                    _write(state_path, {**current, "status": "stopped", "detail": "stop requested"})
                    return 0
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        ok, detail = _queue(codex, state_path, project)
        if not ok:
            if detail not in {"stopped", "completed"}:
                _set_terminal(state_path, project, "failed", detail)
                return 1
            return 0
        deadline = time.monotonic() + args.interval


def _start(args: argparse.Namespace) -> int:
    project = _project(args.cwd)
    directory, state_path, lock_path = _paths(project)
    _secure_dir(directory)
    with _locked(lock_path):
        current = _read(state_path, project, required=False)
        if current and current["status"] in ACTIVE:
            if _alive(current["pid"]):
                raise LoopError(
                    f"a live scheduler already owns this project (pid={current['pid']}, "
                    f"updated_at={current['updated_at']}, state={state_path})"
                )
            raise LoopError(
                f"stale active scheduler state; refusing unsafe replacement (pid={current['pid']}, "
                f"updated_at={current['updated_at']}, state={state_path})"
            )
    codex = shutil.which("codex")
    if not codex:
        raise LoopError("codex executable is not on PATH")
    read_fd, write_fd = os.pipe()
    command = [sys.executable, str(Path(__file__).resolve()), "_run", "--ready-fd", str(write_fd), "--codex", str(Path(codex).resolve()), "--cwd", str(project), "--thread", args.thread, "--message", args.message, "--max-wakes", str(args.max_wakes), "--queue-timeout", str(args.queue_timeout), str(int(args.interval)) + "s"]
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, pass_fds=(write_fd,))
    finally:
        os.close(write_fd)
    ready, _, _ = __import__("select").select([read_fd], [], [], 10)
    if not ready or os.read(read_fd, 16) != b"READY\n":
        os.close(read_fd)
        raise LoopError("scheduler did not complete its ownership handshake")
    os.close(read_fd)
    with _locked(lock_path):
        data = _read(state_path, project, required=True)
        assert data is not None
        if data["status"] != "active":
            raise LoopError("scheduler handshake completed without active state")
        _emit(data)
    return 0


def _status(args: argparse.Namespace) -> int:
    project = _project(args.cwd)
    directory, state_path, lock_path = _paths(project)
    _secure_dir(directory)
    with _locked(lock_path):
        data = _read(state_path, project, required=False)
        if data is None:
            print(json.dumps({"status": "absent", "project": str(project)}, sort_keys=True))
        else:
            _emit(data)
    return 0


def _stop(args: argparse.Namespace) -> int:
    project = _project(args.cwd)
    directory, state_path, lock_path = _paths(project)
    _secure_dir(directory)
    with _locked(lock_path):
        data = _read(state_path, project, required=False)
        if data is None:
            print(json.dumps({"status": "absent", "project": str(project)}, sort_keys=True))
            return 0
        if data["status"] in TERMINAL:
            _emit(data)
            return 0
        if not _alive(data["pid"]):
            data.update(
                status="stopped",
                stop_requested=True,
                detail=f"stop recovered: recorded scheduler pid {data['pid']} is not alive",
            )
            _write(state_path, data)
            _emit(data)
            return 0
        data["stop_requested"] = True
        data["detail"] = "stop requested" if data["status"] == "queueing" else "stop requested; scheduler will stop before next wake"
        _write(state_path, data)
        _emit(data)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded external scheduler for an explicit Codex thread.")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("interval", type=_interval)
    start.add_argument("--thread", required=True)
    start.add_argument("--message", default=DEFAULT_MESSAGE)
    start.add_argument("--max-wakes", type=_positive, default=DEFAULT_MAX_WAKES)
    start.add_argument("--queue-timeout", type=float, default=DEFAULT_QUEUE_TIMEOUT)
    start.add_argument("--cwd")
    start.set_defaults(handler=_start)
    for name, handler in (("status", _status), ("stop", _stop)):
        sub = commands.add_parser(name)
        sub.add_argument("--cwd")
        sub.set_defaults(handler=handler)
    hidden = commands.add_parser("_run", help=argparse.SUPPRESS)
    hidden.add_argument("interval", type=_interval)
    hidden.add_argument("--thread", required=True)
    hidden.add_argument("--message", required=True)
    hidden.add_argument("--max-wakes", type=_positive, required=True)
    hidden.add_argument("--queue-timeout", type=float, required=True)
    hidden.add_argument("--cwd", required=True)
    hidden.add_argument("--ready-fd", type=int, required=True)
    hidden.add_argument("--codex", required=True)
    hidden.set_defaults(handler=_run)
    return parser


def main() -> int:
    """Validate CLI input and dispatch one guarded lifecycle operation."""
    args = _parser().parse_args()
    if hasattr(args, "queue_timeout") and (
        not math.isfinite(args.queue_timeout) or args.queue_timeout <= 0 or args.queue_timeout > 86_400
    ):
        raise SystemExit("queue timeout must be in (0, 86400]")
    if args.command in {"start", "_run"} and args.thread.strip() == "":
        raise SystemExit("--thread must be non-empty")
    try:
        return args.handler(args)
    except LoopError as exc:
        print(f"codex_loop: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
