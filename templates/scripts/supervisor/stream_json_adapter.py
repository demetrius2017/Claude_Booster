#!/usr/bin/env python3
"""
StreamJsonRuntime — Path A WorkerRuntime implementation.

Purpose:
  Concrete WorkerRuntime (see runtime.py) that spawns a headless
  `claude` CLI subprocess in stream-json mode and normalises its
  output into WorkerEvent objects. This is the Path A topology
  specified by consilium §5/Q1 (2026-04-20 architecture).

Contract:
  See WorkerRuntime Protocol in runtime.py. This module MUST NOT
  import from policy.py, quota.py, or detector.py — events flow
  one-way from worker to supervisor.

CLI / Examples:
  runtime = StreamJsonRuntime(cli="claude", model="claude-opus-4-8")
  task_id = await runtime.submit_task("explain this repo", cwd="/tmp/booster-42")
  async for ev in runtime.events(task_id):
      ...
  await runtime.shutdown()

Schema:
  The adapter assumes stream-json v1 (the shape shipped with
  claude-agent-sdk 0.1.63 and `claude --output-format stream-json`
  since 2025-Q4). A version-sentinel check fires on the first
  'system/init' line. On mismatch SchemaMismatchError is raised and
  terminal_state becomes 'failed' — triggers migration per §5/Q1.2.

Limitations:
  - One-worker-per-adapter MVP (matches supervisor-per-session model).
  - Signal ladder for cancel(): SIGINT → 2s → SIGTERM → 2s → SIGKILL.
  - No stdin feed of user messages after submit_task; supervisor
    does not currently multi-turn the worker.
  - submit_task(cwd=...) accepts any working directory with NO
    path-escape/sandbox validation. Sandbox enforcement is the caller's
    responsibility (supervisor.py will validate against project_dir and
    /tmp/booster-* before invoking this adapter).

ENV / Files:
  CLAUDE_BOOSTER_CLI   — override default `claude` binary path
  CLAUDE_BOOSTER_MODEL — override model id passed via --model
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from .runtime import WorkerEvent

EXPECTED_STREAM_JSON_VERSION = 1
TERMINAL_STATES = {"completed", "failed", "cancelled", "blocked_by_quota"}


class SchemaMismatchError(RuntimeError):
    """Raised when worker emits a stream-json schema we do not support."""


class TaskNotFoundError(KeyError):
    """Raised when callers reference an unknown task_id."""


@dataclass
class _TaskState:
    task_id: str
    proc: asyncio.subprocess.Process | None = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    terminal: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    usage_snapshot: dict | None = None
    reader_task: asyncio.Task | None = None
    started_monotonic: float = 0.0
    stderr_path: object = None  # Path | None — per-task stderr log for diagnostics
    terminal_reason: dict | None = None  # {subtype, stop_reason, num_turns, duration_ms} from result event
    cli_session_id: str | None = None    # populated from system/init; used for --resume continuations


class StreamJsonRuntime:
    """WorkerRuntime via subprocess + stream-json parsing."""

    def __init__(
        self,
        cli: str | None = None,
        model: str | None = None,
        default_cwd: str | None = None,
    ) -> None:
        self.cli = cli or os.environ.get("CLAUDE_BOOSTER_CLI", "claude")
        self.model = model or os.environ.get("CLAUDE_BOOSTER_MODEL")
        self.default_cwd = default_cwd
        self._tasks: dict[str, _TaskState] = {}
        self._shutdown = False

    async def submit_task(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        resume_session: str | None = None,
        permission_mode: str | None = "auto",
    ) -> str:
        if self._shutdown:
            raise RuntimeError("runtime already shut down")
        task_id = uuid.uuid4().hex[:16]
        args = [self.cli, "-p", "--output-format", "stream-json", "--verbose"]
        # --permission-mode auto keeps the worker from stalling on permission
        # prompts (the supervisor is the gate, not the Claude CLI's own UI).
        if permission_mode:
            args += ["--permission-mode", permission_mode]
        if model or self.model:
            args += ["--model", model or self.model]
        if system_prompt:
            args += ["--append-system-prompt", system_prompt]
        # Continuation support: `--resume <session_id>` re-attaches to the
        # CLI's prior conversation so auto-chaining after max_turns works.
        if resume_session:
            args += ["--resume", resume_session]
        # Capture stderr into a per-task log (open file, not a PIPE — avoids
        # H-stderr pipe-buffer deadlock but preserves diagnostics when the
        # worker fails to spawn or emits non-stream-json errors).
        log_dir = Path.home() / ".claude" / "logs" / "supervisor"
        log_dir.mkdir(parents=True, exist_ok=True)
        state_stderr_path = log_dir / f"worker_{task_id}.stderr.log"
        stderr_fh = open(state_stderr_path, "wb")
        # Write prompt to a tempfile and pipe it as stdin to avoid CLI arg
        # length limits that cause "Separator is found, but chunk is longer
        # than limit" crashes on long prompts. claude -p reads the prompt
        # from stdin when no positional arg is present.
        prompt_fd, prompt_path = tempfile.mkstemp(prefix="supervisor_prompt_", suffix=".txt")
        try:
            os.write(prompt_fd, prompt.encode("utf-8"))
        finally:
            os.close(prompt_fd)
        prompt_fh = open(prompt_path, "rb")
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=prompt_fh,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_fh,
            cwd=cwd or self.default_cwd,
        )
        prompt_fh.close()
        try:
            os.unlink(prompt_path)
        except OSError:
            pass
        state = _TaskState(task_id=task_id, proc=proc, started_monotonic=time.monotonic())
        state.stderr_path = state_stderr_path
        state.reader_task = asyncio.create_task(self._reader(state))
        self._tasks[task_id] = state
        return task_id

    async def events(self, task_id: str) -> AsyncIterator[WorkerEvent]:
        state = self._state(task_id)
        while True:
            item = await state.queue.get()
            if item is None:
                return
            yield item

    def terminal_state(self, task_id: str) -> str | None:
        return self._state(task_id).terminal

    def tool_invocations(self, task_id: str) -> list[dict]:
        return list(self._state(task_id).tool_calls)

    def usage(self, task_id: str) -> dict | None:
        return self._state(task_id).usage_snapshot

    def terminal_reason(self, task_id: str) -> dict | None:
        """Diagnostic snapshot from the CLI's `result` event.
        Keys: subtype, stop_reason, terminal_reason, num_turns, duration_ms,
        is_error, api_error_status. None if CLI never emitted result."""
        return self._state(task_id).terminal_reason

    def cli_session_id(self, task_id: str) -> str | None:
        """The CLI's own session id (from system/init) — used for --resume
        continuations after a max_turns interruption."""
        return self._state(task_id).cli_session_id

    async def cancel(self, task_id: str) -> None:
        state = self._state(task_id)
        proc = state.proc
        if proc is None or proc.returncode is not None:
            return
        for sig, wait in ((signal.SIGINT, 2.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, None)):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                break
            if wait is None:
                break
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                continue
        if state.terminal is None:
            state.terminal = "cancelled"
        # Queue sentinel is emitted exclusively by _reader.finally — don't race it here.

    async def shutdown(self) -> None:
        self._shutdown = True
        for tid in list(self._tasks):
            await self.cancel(tid)
        for state in self._tasks.values():
            if state.reader_task is not None:
                try:
                    await asyncio.wait_for(state.reader_task, timeout=3.0)
                except asyncio.TimeoutError:
                    state.reader_task.cancel()
                except SchemaMismatchError:
                    pass  # already reflected in state.terminal

    def _state(self, task_id: str) -> _TaskState:
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        return self._tasks[task_id]

    async def _reader(self, state: _TaskState) -> None:
        """Drain stdout, schema-check, normalise into WorkerEvents.

        Real `claude -p --output-format stream-json` emits a preamble of
        `system/hook_started` + `system/hook_response` events (one per
        SessionStart hook) BEFORE the authoritative `system/init` line.
        The handshake must tolerate that preamble and only validate the
        first `system/init` it sees. Non-JSON before init still fails
        closed (audit-fix H2).
        """
        proc = state.proc
        assert proc is not None and proc.stdout is not None
        schema_checked = False
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    if not schema_checked:
                        raise SchemaMismatchError("first non-empty line is not valid JSON")
                    continue
                if not schema_checked:
                    mtype, mstype = msg.get("type"), msg.get("subtype")
                    if mtype == "system" and mstype != "init":
                        # Preamble hook events; wait for system/init.
                        continue
                    self._assert_schema(msg)
                    schema_checked = True
                for ev in self._to_events(state, msg):
                    await state.queue.put(ev)
            await proc.wait()
            if state.terminal is None:
                state.terminal = "completed" if proc.returncode == 0 else "failed"
        except SchemaMismatchError:
            state.terminal = "failed"
            raise
        finally:
            await state.queue.put(None)

    @staticmethod
    def _assert_schema(msg: dict) -> None:
        if msg.get("type") != "system" or msg.get("subtype") != "init":
            raise SchemaMismatchError(f"first event not system/init: {msg.get('type')!r}/{msg.get('subtype')!r}")
        version = msg.get("stream_json_version", 1)
        if version != EXPECTED_STREAM_JSON_VERSION:
            raise SchemaMismatchError(f"stream-json v{version} ≠ expected v{EXPECTED_STREAM_JSON_VERSION}")

    @staticmethod
    def _to_events(state: _TaskState, msg: dict) -> list[WorkerEvent]:
        """Normalise one stream-json message into zero-or-more WorkerEvents.

        Multiple content blocks in a single assistant message (e.g. text +
        tool_use bundled) must each become a distinct event — dropping any
        of them would let a tool_use slip past policy/quota enforcement.
        """
        ts = time.monotonic()
        mtype = msg.get("type")
        out: list[WorkerEvent] = []
        if mtype == "system" and msg.get("subtype") == "init":
            state.cli_session_id = msg.get("session_id")  # capture for --resume on max_turns continuation
            out.append(WorkerEvent("message_start", state.task_id, ts, {"session": msg.get("session_id"), "model": msg.get("model")}))
            return out
        if mtype == "assistant":
            message = msg.get("message") or {}
            for block in (message.get("content") or []):
                btype = block.get("type")
                if btype == "text":
                    out.append(WorkerEvent("text_delta", state.task_id, ts, {"text": block.get("text", "")}))
                elif btype == "tool_use":
                    state.tool_calls.append(block)
                    out.append(WorkerEvent("tool_use_start", state.task_id, ts, {"name": block.get("name"), "input": block.get("input", {}), "id": block.get("id")}))
                elif btype == "thinking":
                    out.append(WorkerEvent("thinking_start", state.task_id, ts, {"text": block.get("thinking", "")}))
            return out
        if mtype == "user":
            message = msg.get("message") or {}
            for block in (message.get("content") or []):
                if block.get("type") == "tool_result":
                    out.append(WorkerEvent("tool_use_stop", state.task_id, ts, {"tool_use_id": block.get("tool_use_id"), "is_error": bool(block.get("is_error"))}))
            return out
        if mtype == "result":
            usage = msg.get("usage") or {}
            state.usage_snapshot = {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}
            state.terminal = "completed" if msg.get("subtype") == "success" else "failed"
            # Capture everything the CLI tells us about WHY it stopped. Without
            # this the user sees `terminal=failed, exit=1` and no diagnostic —
            # field log 2026-04-21 had a session die after 108 tool-calls with
            # no visible reason (turned out to be the CLI's max-turns limit).
            state.terminal_reason = {
                "subtype": msg.get("subtype"),
                "stop_reason": msg.get("stop_reason"),
                "terminal_reason": msg.get("terminal_reason"),
                "num_turns": msg.get("num_turns"),
                "duration_ms": msg.get("duration_ms"),
                "is_error": msg.get("is_error"),
                "api_error_status": msg.get("api_error_status"),
            }
            out.append(WorkerEvent("message_stop", state.task_id, ts, {
                "subtype": msg.get("subtype"),
                "usage": state.usage_snapshot,
                **state.terminal_reason,
            }))
            return out
        return out
