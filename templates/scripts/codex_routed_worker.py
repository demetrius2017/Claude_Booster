#!/usr/bin/env python3
"""Execute a local Codex delegation from an exact balancer route.

Purpose:
    Make Claude Booster's local Codex routing contract executable. The runner
    obtains one category route from the sibling model balancer, rejects caller
    attempts to select a model, and forwards a valid Codex route unchanged to
    the sibling worker.

Contract (inputs/outputs):
    Input is ``CATEGORY [codex exec args...]`` plus stdin, which is forwarded
    byte-for-byte to the launched child. Every route requires non-empty string
    ``provider`` and ``model`` fields; ``reasoning_effort`` is required only for
    a ``codex-cli`` route, so a non-Codex route (grok-cli, zai-cli, anthropic,
    pal) is recognised and refused by name instead of silently degrading into an
    unpinned Codex fallback. Output and stderr from the selected child are
    preserved, along with at most one sanitized degraded-routing diagnostic when
    lookup cannot be trusted.

CLI:
    codex_routed_worker.py CATEGORY [codex exec args...]

Examples:
    printf '%s' 'inspect this module' | codex_routed_worker.py recon \\
      --ephemeral --sandbox read-only

Limitations:
    Non-Codex provider routes deliberately fail here with exit 65 and name the
    runner the caller must use instead: selecting an Anthropic, PAL, Z.ai, or
    Grok runner is the caller's responsibility. If route lookup itself fails
    (including an unknown category), the fallback is intentionally unpinned and
    does not infer a model.

ENV/Files:
    CODEX_BIN optionally overrides the Codex executable. The three
    CLAUDE_BOOSTER_ROUTED_{BALANCER,WORKER,CODEX_BIN} overrides exist for
    hermetic tests and controlled installations; each must name a regular,
    usable absolute path. Normal operation resolves sibling Python scripts from
    this file and runs them with ``sys.executable``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


_CATEGORY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MODEL_CONFIG = re.compile(r"^model\s*=", re.IGNORECASE)
_ROUTE_TIMEOUT_SECONDS = 5
_RUNNER_HINTS = {
    "grok-cli": "run ~/.claude/scripts/grok_cli.py review instead",
    "zai-cli": "run ~/.claude/scripts/zai_cli.py review instead",
    "anthropic": "use the Agent tool / PAL instead",
    "pal": "use the Agent tool / PAL instead",
}
_DEFAULT_RUNNER_HINT = "use that provider's own runner instead"


def _regular_path(path: Path, *, executable: bool) -> Path:
    """Return a resolved regular path, rejecting ambiguous child targets."""
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or (executable and not os.access(resolved, os.X_OK)):
        raise ValueError(f"unsafe child path: {path}")
    return resolved


def _child_path(env_name: str, sibling: str, *, executable: bool = False) -> Path:
    """Resolve an absolute override/default or a relative sibling beside this runner."""
    override = os.environ.get(env_name)
    candidate = Path(override) if override else Path(sibling)
    if override and not candidate.is_absolute():
        raise ValueError(f"{env_name} must be an absolute path")
    if not candidate.is_absolute():
        candidate = Path(__file__).resolve().with_name(sibling)
    return _regular_path(candidate, executable=executable)


def _has_model_override(extra: list[str]) -> bool:
    """Reject model pins before they can bypass the balancer-selected route."""
    for index, arg in enumerate(extra):
        if arg == "-m" or arg.startswith("-m") and len(arg) > 2:
            return True
        if arg == "--model" or arg.startswith("--model="):
            return True
        if arg.startswith("-c") and len(arg) > 2 and _MODEL_CONFIG.match(arg[2:]):
            return True
        if arg.startswith("--config=") and _MODEL_CONFIG.match(arg.split("=", 1)[1]):
            return True
        if arg in {"-c", "--config"} and index + 1 < len(extra) and _MODEL_CONFIG.match(extra[index + 1]):
            return True
    return False


def _route(category: str, balancer: Path | None) -> dict[str, str] | None:
    """Return a fully typed balancer route, or None when lookup is unusable."""
    if balancer is None:
        return None
    try:
        result = subprocess.run(
            [sys.executable, str(balancer), "get", category],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_ROUTE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        if b"unknown category" in result.stderr.lower():
            return {"__unknown_category__": category}
        return None
    try:
        payload: Any = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    def _typed(field: str) -> str | None:
        value = payload.get(field)
        if not isinstance(value, str) or not value or value != value.strip():
            return None
        return value

    provider, model = _typed("provider"), _typed("model")
    if provider is None or model is None:
        return None
    route = {"provider": provider, "model": model}
    if provider == "codex-cli":
        effort = _typed("reasoning_effort")
        if effort is None:
            return None
        route["reasoning_effort"] = effort
    return route


def _run(command: list[str], prompt: bytes, env: dict[str, str] | None = None) -> int:
    """Run one child and preserve its byte streams and exit status."""
    result = subprocess.run(
        command,
        input=prompt,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


def main(argv: list[str]) -> int:
    """Validate the routing boundary and execute the appropriate local child."""
    if not argv or not _CATEGORY.fullmatch(argv[0]):
        print("usage: codex_routed_worker.py CATEGORY [codex exec args...]", file=sys.stderr)
        return 2
    category, extra = argv[0], argv[1:]
    if _has_model_override(extra):
        print("codex_routed_worker.py: caller model override is forbidden", file=sys.stderr)
        return 2
    try:
        codex = _child_path("CLAUDE_BOOSTER_ROUTED_CODEX_BIN", os.environ.get("CODEX_BIN", "/opt/homebrew/bin/codex"), executable=True)
    except (OSError, ValueError) as exc:
        print(f"codex_routed_worker.py: {exc}", file=sys.stderr)
        return 127
    try:
        balancer = _child_path("CLAUDE_BOOSTER_ROUTED_BALANCER", "model_balancer.py")
    except (OSError, ValueError):
        balancer = None

    prompt = sys.stdin.buffer.read()
    route = _route(category, balancer)
    if route is None or "__unknown_category__" in route:
        if route is None:
            reason = "degraded routing"
        else:
            reason = f"unknown category {category!r}"
        print(f"codex_routed_worker.py: {reason}; unpinned Codex fallback", file=sys.stderr)
        return _run([str(codex), "exec", *extra, "-"], prompt)
    if route["provider"] != "codex-cli":
        print(
            f"codex_routed_worker.py: route provider is {route['provider']} "
            f"(model {route['model']}); refusing local Codex — "
            f"{_RUNNER_HINTS.get(route['provider'], _DEFAULT_RUNNER_HINT)}",
            file=sys.stderr,
        )
        return 65
    try:
        worker = _child_path("CLAUDE_BOOSTER_ROUTED_WORKER", "codex_worker.py")
    except (OSError, ValueError) as exc:
        print(f"codex_routed_worker.py: {exc}", file=sys.stderr)
        return 127

    child_env = os.environ.copy()
    child_env.update(
        {
            "CLAUDE_BOOSTER_ROUTE_SOURCE": "balancer",
            "CLAUDE_BOOSTER_TASK_CATEGORY": category,
            "CODEX_REASONING_EFFORT": route["reasoning_effort"],
        }
    )
    return _run([sys.executable, str(worker), route["model"], *extra], prompt, child_env)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
