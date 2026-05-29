#!/usr/bin/env python3
"""
supervisor.py — main orchestration loop for the Claude Booster Supervisor Agent.

Purpose:
  Wires policy → quota → detector → persistence into a single event loop
  driven by a WorkerRuntime. One supervisor instance manages one worker
  session (consilium §5/Q1 Path A MVP). Tool invocations observed on the
  worker stream are gated through `policy.evaluate`; deny/escalate outcomes
  are recorded; approvals are passed through; quota is updated from the
  worker's own `usage` event; silence-based completion is decided by the
  detector's adaptive timer.

Contract:
  Supervisor(
    runtime: WorkerRuntime,
    ctx:     PolicyContext,
    tracker: QuotaTracker,
    store:   SupervisorPersistence,
    detector: WorkerStateDetector | None = None,
  ).supervise(prompt, system_prompt=None, cwd=None, estimated_tokens=10_000)
    -> SupervisorResult(terminal, decisions, tool_calls, usage, state)

CLI / Examples:
  # Run one-shot:
  python3 -m supervisor.supervisor run "explain this repo" --cwd /tmp/booster-42

  # Status snapshot of last session:
  python3 -m supervisor.supervisor status --session <id>

  # Inspect the last N decisions:
  python3 -m supervisor.supervisor decisions --session <id> --limit 20

Limitations:
  - One worker per supervisor, one supervisor per CLI invocation. Multiplex
    across sessions is Session 5+ scope.
  - Haiku escalation is stubbed behind `HaikuEscalator.decide` — the actual
    LLM call lives outside this module (prompt is in prompts/supervisor_v1.md)
    and is wired in by the caller that owns an API client.
  - No stdin feed of user messages after submit_task. Multi-turn support is
    deferred.

ENV / Files:
  CLAUDE_BOOSTER_SUPERVISOR_YAML — override path to per-project config
  CLAUDE_BOOSTER_DB              — override rolling_memory.db location
  CLAUDE_BOOSTER_CLI             — override `claude` binary path
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol

# Bootstrap so the file works both as `python3 -m supervisor.supervisor ...`
# AND as `python3 ~/.claude/scripts/supervisor/supervisor.py ...` (the form
# the /supervise slash command uses).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "supervisor"

from supervisor import policy as P  # noqa: E402
from supervisor import runtime as R  # noqa: E402
from supervisor.detector import State, WorkerStateDetector  # noqa: E402
from supervisor.persistence import SupervisorPersistence  # noqa: E402
from supervisor.policy import PolicyContext, args_digest, evaluate  # noqa: E402
from supervisor.quota import CircuitState, QuotaTracker  # noqa: E402
from supervisor.stream_json_adapter import StreamJsonRuntime  # noqa: E402

DEFAULT_ESTIMATED_TOKENS = 10_000
SILENCE_POLL_INTERVAL = 1.0
DEFAULT_MAX_CONTINUATIONS = 5  # on CLI max_turns / network transient, auto-respawn this many times
CONTINUATION_PROMPT = (
    "Continue the task from exactly where you stopped. You were interrupted mid-work "
    "(turn limit / transient issue). Do NOT restart or summarise the previous work — "
    "pick up on the next step. When the task is actually complete, finish with a short "
    "summary of what you did."
)

AUTONOMY_DIRECTIVE = (
    "You are running under Claude Booster supervisor. Work fully autonomously:\n"
    "- Do NOT ask the user clarifying questions or offer A/B choices. If you face a "
    "decision, pick the best path using reversibility + scope + risk (small, reversible "
    "fix > large refactor; ship > discuss) and proceed.\n"
    "- Do NOT narrate plans in prose asking for approval. State the chosen approach in "
    "one line and act.\n"
    "- Do NOT stop to confirm before editing, running tests, or committing. The "
    "supervisor policy engine gates unsafe actions; within that sandbox, act.\n"
    "- If blocked by missing info, make the 51% best-guess, state the assumption in one "
    "line in your output, and continue.\n"
    "- End the session by summarising what you actually changed + any residual risk, "
    "not by asking what to do next.\n"
    "The supervisor records every tool call and final decision in "
    "~/.claude/rolling_memory.db; the user can override afterwards."
)


class HaikuEscalator(Protocol):
    """Called only when `policy.evaluate` returns escalate. Implementations
    own the actual LLM API credentials; this module does not import any
    Anthropic SDK. Return value must be one of {"approve","deny"}.
    """

    async def decide(self, tool: str, tool_input: dict, rationale: str) -> tuple[str, str]:
        """Return (decision, rationale). decision ∈ {'approve','deny'}."""


@dataclass
class SupervisorResult:
    session_id: str
    terminal: str | None
    decisions: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict | None = None
    final_state: State | None = None
    terminal_reason: dict | None = None  # {subtype, stop_reason, num_turns, duration_ms, ...}
    continuations: int = 0  # how many times we auto-respawned after max_turns


@dataclass
class SupervisorConfig:
    tier1_tools: set[str] = field(default_factory=set)
    tier2_trusted_repo: bool = False
    estimated_tokens: int = DEFAULT_ESTIMATED_TOKENS
    paranoid_mode: bool = False  # True = old whitelist-default; False = permissive blacklist
    max_continuations: int = DEFAULT_MAX_CONTINUATIONS  # cap on auto-respawns after max_turns
    autonomy_directive: bool = True  # append AUTONOMY_DIRECTIVE as worker system-prompt

    @classmethod
    def from_yaml(cls, path: Path) -> "SupervisorConfig":
        """Parse .claude/supervisor.yaml. Intentionally does NOT require PyYAML.

        Accepts a minimal flat YAML subset (key: value, lists as '- item').
        For complex configs users should pre-materialise to JSON at
        .claude/supervisor.json (tried as a fallback).
        """
        if not path.exists():
            return cls()
        as_json = path.with_suffix(".json")
        if as_json.exists():
            data = json.loads(as_json.read_text(encoding="utf-8"))
        else:
            data = _parse_minimal_yaml(path.read_text(encoding="utf-8"))
        # Audit-fix M5: validate types explicitly — bool(<non-empty str>) silently becomes True.
        tier1 = data.get("tier1_tools", [])
        trusted = data.get("tier2_trusted_repo", False)
        estimated = data.get("estimated_tokens", DEFAULT_ESTIMATED_TOKENS)
        paranoid = data.get("paranoid_mode", False)
        max_cont = data.get("max_continuations", DEFAULT_MAX_CONTINUATIONS)
        autonomy = data.get("autonomy_directive", True)
        if not isinstance(tier1, list) or not all(isinstance(x, str) for x in tier1):
            raise ValueError(f"tier1_tools must be a list of strings in {path}")
        if not isinstance(trusted, bool):
            raise ValueError(f"tier2_trusted_repo must be true/false in {path}")
        if not isinstance(estimated, int):
            raise ValueError(f"estimated_tokens must be an integer in {path}")
        if not isinstance(paranoid, bool):
            raise ValueError(f"paranoid_mode must be true/false in {path}")
        if not isinstance(max_cont, int) or max_cont < 0:
            raise ValueError(f"max_continuations must be a non-negative integer in {path}")
        if not isinstance(autonomy, bool):
            raise ValueError(f"autonomy_directive must be true/false in {path}")
        return cls(
            tier1_tools=set(tier1), tier2_trusted_repo=trusted,
            estimated_tokens=estimated, paranoid_mode=paranoid,
            max_continuations=max_cont, autonomy_directive=autonomy,
        )


def _parse_minimal_yaml(text: str) -> dict:
    """Tiny flat-YAML: `key: value` and `key:\\n  - item` lists. No anchors, no nesting beyond lists."""
    out: dict = {}
    current_list_key: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if current_list_key and line.startswith(("  - ", "  -\t", "- ")):
            item = line.split("-", 1)[1].strip()
            out[current_list_key].append(item.strip("\"'"))
            continue
        current_list_key = None
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        key, val = k.strip(), v.strip()
        if not val:
            out[key] = []
            current_list_key = key
        elif val.lower() in ("true", "false"):
            out[key] = val.lower() == "true"
        elif val.isdigit():
            out[key] = int(val)
        else:
            out[key] = val.strip("\"'")
    return out


class Supervisor:
    """Single-worker orchestration loop."""

    def __init__(
        self,
        runtime: "R.WorkerRuntime | StreamJsonRuntime",
        ctx: PolicyContext,
        tracker: QuotaTracker,
        store: SupervisorPersistence,
        detector: WorkerStateDetector | None = None,
        escalator: HaikuEscalator | None = None,
        max_continuations: int = DEFAULT_MAX_CONTINUATIONS,
    ) -> None:
        self.runtime = runtime
        self.ctx = ctx
        self.tracker = tracker
        self.store = store
        self.detector = detector or WorkerStateDetector()
        self.escalator = escalator
        self.max_continuations = max_continuations
        self.session_id = tracker.session_id
        self.result = SupervisorResult(session_id=self.session_id, terminal=None)

    async def supervise(
        self,
        prompt: str,
        system_prompt: str | None = None,
        cwd: str | None = None,
        estimated_tokens: int = DEFAULT_ESTIMATED_TOKENS,
        autonomy_directive: bool = True,
    ) -> SupervisorResult:
        """Run a supervised task, auto-continuing across CLI max_turns.

        The user writes one prompt. If the CLI's internal turn limit (or a
        transient error) interrupts the worker mid-task, we respawn with
        `--resume <cli_session_id>` so the new worker picks up the same
        conversation history, and tell it to keep going. This is the
        supervisor's core value-add over running `claude -p` directly.
        """
        admitted, reason = self.tracker.admit(estimated_tokens)
        self._persist_quota()
        if not admitted:
            self.detector.force(State.BLOCKED_BY_QUOTA)
            self.result.terminal = "blocked_by_quota"
            self.result.final_state = State.BLOCKED_BY_QUOTA
            self._record_decision("_admit", {"estimate": estimated_tokens}, "deny", None, reason, "regex")
            return self.result

        # Inject the autonomy directive as part of the system-prompt so the
        # worker doesn't stall on "A or B?" administrative questions. Callers
        # that already pass a system_prompt get it appended.
        effective_system_prompt = system_prompt
        if autonomy_directive:
            effective_system_prompt = (
                AUTONOMY_DIRECTIVE if not system_prompt
                else f"{AUTONOMY_DIRECTIVE}\n\n{system_prompt}"
            )

        current_prompt = prompt
        resume_session: str | None = None
        accumulated_tool_calls: list[dict] = []
        accumulated_input_tokens = 0
        accumulated_output_tokens = 0

        for attempt in range(self.max_continuations + 1):
            # Fresh detector per attempt so silence timing resets; policy state
            # (loop-guard, quota tracker) is session-scoped, not attempt-scoped.
            self.detector = WorkerStateDetector()
            task_id = await self._run_one_attempt(
                current_prompt, effective_system_prompt, cwd, resume_session,
            )
            # Accumulate cross-attempt
            accumulated_tool_calls.extend(self.runtime.tool_invocations(task_id))
            attempt_usage = self.runtime.usage(task_id) or {}
            accumulated_input_tokens += int(attempt_usage.get("input_tokens", 0))
            accumulated_output_tokens += int(attempt_usage.get("output_tokens", 0))

            tr = None
            if hasattr(self.runtime, "terminal_reason"):
                tr = self.runtime.terminal_reason(task_id)
            self.result.terminal_reason = tr  # always reflect the latest

            # Decide: done, or continue?
            # Retry on ANY non-success interruption: error_max_turns,
            # aborted_streaming, error_network, error_rate_limit, etc.
            # Field log 2026-04-21 had `aborted_streaming` (worker cut off mid
            # tool_use at num_turns=26) and continuation didn't fire because
            # the gate only matched error_max_turns. Policy-kill / quota-open
            # gates below still short-circuit the retry.
            subtype = (tr or {}).get("subtype")
            retriable = subtype is not None and subtype != "success"
            should_continue = (
                retriable
                and attempt < self.max_continuations
                and self.detector.state is not State.CANCELLED  # respect policy-kills
                and self.tracker.state is not CircuitState.OPEN  # respect quota circuit
            )
            if not should_continue:
                break

            # Prepare next attempt using CLI session resume.
            if hasattr(self.runtime, "cli_session_id"):
                resume_session = self.runtime.cli_session_id(task_id)
            if not resume_session:
                break  # no session id → can't resume, give up
            current_prompt = CONTINUATION_PROMPT
            self.result.continuations += 1

        # Finalise from the LAST attempt's runtime state.
        last_task_id = task_id
        self.result.terminal = self.runtime.terminal_state(last_task_id) or self.detector.state.value
        self.result.usage = {
            "input_tokens": accumulated_input_tokens,
            "output_tokens": accumulated_output_tokens,
        }
        self.result.tool_calls = accumulated_tool_calls
        self.result.final_state = self.detector.state
        self.tracker.record(worker_tokens=accumulated_output_tokens)
        self._persist_quota()
        return self.result

    async def _run_one_attempt(
        self,
        prompt: str,
        system_prompt: str | None,
        cwd: str | None,
        resume_session: str | None,
    ) -> str:
        """Single worker-spawn lifecycle. Returns the task_id."""
        submit_kwargs = {"prompt": prompt, "system_prompt": system_prompt, "cwd": cwd}
        if resume_session is not None:
            submit_kwargs["resume_session"] = resume_session
        try:
            task_id = await self.runtime.submit_task(**submit_kwargs)
        except TypeError:
            # Test doubles / MCP adapter without resume_session kwarg — fall back.
            submit_kwargs.pop("resume_session", None)
            task_id = await self.runtime.submit_task(**submit_kwargs)
        poll_task = asyncio.create_task(self._silence_poller(task_id))
        try:
            async for ev in self.runtime.events(task_id):
                self.detector.on_event(ev)
                await self._handle_event(ev, task_id)
                if self.detector.state in (State.COMPLETED, State.FAILED, State.CANCELLED):
                    break
        finally:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
        return task_id

    async def _silence_poller(self, task_id: str) -> None:
        """Audit-fix H2: act on POSSIBLY_COMPLETE by cancelling the worker.

        A hung or silently-deadlocked worker would otherwise block the
        event loop forever. The detector's adaptive threshold already
        has a 60s grace + 3-gap median seed, so firing here means we
        genuinely crossed `clamp(3×median, 20, 180)s` of silence.
        """
        while True:
            await asyncio.sleep(SILENCE_POLL_INTERVAL)
            if self.detector.tick(time.monotonic()) is State.POSSIBLY_COMPLETE:
                await self.runtime.cancel(task_id)
                self.detector.force(State.CANCELLED)
                return

    async def _handle_event(self, ev: R.WorkerEvent, task_id: str) -> None:
        """Audit-fix C1: resolve every tool event to an authoritative action.

        Worker cannot be pre-blocked from a sidecar process (claude -p
        stream-json is observation-only), so the supervisor enforces via
        `runtime.cancel()` the moment a non-approve verdict is reached.
        No escalator configured + escalate policy verdict → default-deny.
        """
        if ev.kind != "tool_use_start":
            return
        tool = (ev.payload or {}).get("name") or "Unknown"
        tool_input = (ev.payload or {}).get("input") or {}
        digest = args_digest(tool, tool_input)
        loop_hits = self.store.recent_by_args(digest, window_seconds=300)
        # Audit-fix M4: loop-guard only counts prior APPROVED calls, not denies/escalates.
        prior_approvals = [h for h in loop_hits if h.get("decision") == "approve"]
        decision = evaluate(tool, tool_input, self.ctx)
        if len(prior_approvals) >= 3 and decision.action == "approve":
            decision = P.Decision(
                "escalate", decision.tier,
                f"loop-guard: {len(prior_approvals)} prior approvals in 5min", None,
            )

        final_action = decision.action
        final_rationale = decision.rationale
        approved_by: str | None = "regex" if decision.action in ("approve", "deny") else None
        if decision.action == "escalate":
            if self.escalator is None:
                final_action = "deny"
                final_rationale = "escalation required but no escalator configured (default-deny)"
                approved_by = "regex"
            else:
                final_action, final_rationale = await self.escalator.decide(
                    tool, tool_input, decision.rationale,
                )
                approved_by = "haiku"

        self._record_decision(
            tool, tool_input, final_action, decision.tier, final_rationale, approved_by, digest=digest,
        )
        if final_action != "approve":
            await self.runtime.cancel(task_id)
            self.detector.force(State.CANCELLED)

    def _record_decision(
        self,
        tool: str,
        tool_input: dict,
        action: str,
        tier: int | None,
        rationale: str,
        approved_by: str | None,
        digest: str | None = None,
    ) -> None:
        digest = digest or args_digest(tool, tool_input)
        self.store.record_decision(
            session_id=self.session_id, tool=tool, args_digest=digest,
            decision=action, tier=tier, rationale=rationale, approved_by=approved_by,
        )
        self.result.decisions.append({"tool": tool, "decision": action, "tier": tier, "rationale": rationale})

    def _persist_quota(self) -> None:
        self.store.upsert_quota(self.tracker.snapshot())


# -------------------------- CLI --------------------------

def _cmd_run(args: argparse.Namespace) -> int:
    # REMAINDER gives us a list of tokens — join back into a single prompt.
    prompt = " ".join(args.prompt).strip() if isinstance(args.prompt, list) else (args.prompt or "").strip()
    if not prompt:
        print("supervisor: empty prompt — nothing to run. Usage: /lead <your task>", file=sys.stderr)
        return 2
    # Audit-fix M3: when --cwd is given, default config lookup relative to that project root,
    # not the shell's cwd. Prevents repo-A-configs bleeding into repo-B runs.
    base_dir = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    cfg_path = Path(args.config) if args.config else base_dir / ".claude" / "supervisor.yaml"
    cfg = SupervisorConfig.from_yaml(cfg_path)
    session_id = args.session or uuid.uuid4().hex[:16]
    ctx = PolicyContext(
        project_dir=base_dir,
        tier1_enabled=cfg.tier1_tools,
        tier2_trusted_repo=cfg.tier2_trusted_repo,
        paranoid_mode=cfg.paranoid_mode,
    )
    tracker = QuotaTracker(session_id=session_id)
    store = SupervisorPersistence()
    runtime = StreamJsonRuntime(model=getattr(args, 'model', None))
    sup = Supervisor(runtime=runtime, ctx=ctx, tracker=tracker, store=store,
                     max_continuations=cfg.max_continuations)
    # Stash for use inside supervise()
    sup._autonomy_directive_enabled = cfg.autonomy_directive

    # Audit-fix M2: single asyncio.run() wraps supervise + shutdown so tasks stay on one loop.
    async def _run_once() -> SupervisorResult:
        try:
            return await sup.supervise(
                prompt=prompt, cwd=args.cwd, estimated_tokens=cfg.estimated_tokens,
                autonomy_directive=cfg.autonomy_directive,
            )
        finally:
            await runtime.shutdown()

    try:
        result = asyncio.run(_run_once())
    except Exception as exc:
        print(f"supervisor: {exc}", file=sys.stderr)
        return 2
    payload = {
        "session_id": result.session_id, "terminal": result.terminal,
        "final_state": result.final_state.value if result.final_state else None,
        "decisions": len(result.decisions), "tool_calls": len(result.tool_calls),
        "usage": result.usage,
        "terminal_reason": result.terminal_reason,
        "continuations": result.continuations,
    }
    print(json.dumps(payload, indent=2))
    return 0 if result.terminal == "completed" else 1


def _cmd_status(args: argparse.Namespace) -> int:
    store = SupervisorPersistence()
    quota = store.load_quota(args.session)
    print(json.dumps({"session_id": args.session, "quota": quota}, indent=2))
    return 0 if quota else 2


def _cmd_decisions(args: argparse.Namespace) -> int:
    # Audit-fix L1: public list_decisions API instead of reaching into store._connect().
    store = SupervisorPersistence()
    for row in store.list_decisions(args.session, limit=args.limit):
        print(json.dumps(row))
    return 0


def _cmd_sessions(args: argparse.Namespace) -> int:
    """Summary across sessions — Claudes used to query sqlite3 ad-hoc with
    wrong column names (observed in field logs 2026-04-20). Shipping a
    first-class command avoids improvisation.
    """
    store = SupervisorPersistence()
    rows = store.list_sessions(limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No supervisor sessions on record.")
        return 0
    # Fixed-width columns; tolerate long session_ids by truncating.
    print(f"{'SESSION_ID':<20} {'STARTED':<22} {'WORKER_TOK':>10}  {'CIRCUIT':<10} {'DECISIONS':>9}")
    print("-" * 76)
    for r in rows:
        sid = (r["session_id"] or "")[:18]
        started = (r["started_at"] or "")[:19]
        print(f"{sid:<20} {started:<22} {r['worker_tokens']:>10}  {r['circuit_state']:<10} {r['n_decisions']:>9}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="supervisor", description="Claude Booster Supervisor Agent v1.2.0")
    # cmd no longer `required=True` — bare invocation drops into _cmd_sessions
    # so users and other Claudes get a useful summary, not argparse exit=2.
    sub = ap.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="Run a supervised worker session")
    run.add_argument("--cwd", help="Working directory for worker + policy scope (default: current directory)")
    run.add_argument("--session", help="Session id (auto if omitted)")
    run.add_argument("--config", help="Path to supervisor.yaml (default: <cwd>/.claude/supervisor.yaml)")
    run.add_argument("--model", help="Model override for the worker (e.g. claude-opus-4-8, claude-sonnet-4-6)")
    # REMAINDER so users can type `/lead run fix the bug in foo.py` without
    # needing to wrap the prompt in quotes. Flags MUST come before the prompt.
    run.add_argument("prompt", nargs=argparse.REMAINDER, help="Prompt text — quotes optional, spaces fine")
    run.set_defaults(func=_cmd_run)

    status = sub.add_parser("status", help="Show quota snapshot for a session")
    status.add_argument("--session", required=True)
    status.set_defaults(func=_cmd_status)

    dec = sub.add_parser("decisions", help="List recent decisions for a session")
    dec.add_argument("--session", required=True)
    dec.add_argument("--limit", type=int, default=20)
    dec.set_defaults(func=_cmd_decisions)

    sess = sub.add_parser("sessions", help="Summary across all supervisor sessions")
    sess.add_argument("--limit", type=int, default=20)
    sess.add_argument("--json", action="store_true", help="Emit JSON instead of table")
    sess.set_defaults(func=_cmd_sessions)

    return ap


_SUBCOMMANDS = {"run", "status", "decisions", "sessions"}


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # UX shortcut: `supervisor.py fix the bug in foo.py` → implicit `run`.
    # If the first arg is neither a known subcommand nor a flag, treat the
    # whole argv as a free-form prompt for `run`. Quotes become optional.
    if argv and argv[0] not in _SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["run", *argv]
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        # Bare invocation — friendly default instead of argparse exit=2.
        parser.print_help()
        print()
        return _cmd_sessions(argparse.Namespace(limit=10, json=False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
