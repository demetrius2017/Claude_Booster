#!/usr/bin/env python3
"""Executable contract tests for the Codex exact-route adapter."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "templates/scripts/codex_routed_worker.py"
SKILL = ROOT / "templates/codex/skills/booster-command/SKILL.md"
PROMPT = b"preserve\x00these\nbytes\n"


def _load_runner():
    spec = importlib.util.spec_from_file_location("codex_routed_worker_test", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_installer():
    spec = importlib.util.spec_from_file_location("codex_installer_test", ROOT / "install.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, source: str, executable: bool = False) -> None:
    path.write_text(source, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _environment(tmp: Path, route: str) -> tuple[dict[str, str], Path, Path]:
    balancer, worker, codex, log = (tmp / name for name in ("model_balancer.py", "codex_worker.py", "codex", "log.json"))
    _write(balancer, "import os, sys\nprint(os.environ['TEST_ROUTE'])\n")
    recorder = (
        "import json, os, sys\n"
        "row={'argv':sys.argv[1:],'stdin':sys.stdin.buffer.read().hex(),"
        "'env':{key:os.environ.get(key) for key in ('CLAUDE_BOOSTER_ROUTE_SOURCE','CLAUDE_BOOSTER_TASK_CATEGORY','CODEX_REASONING_EFFORT')}}\n"
        "open(os.environ['TEST_LOG'],'w',encoding='utf-8').write(json.dumps(row))\n"
        "print('child stdout')\nprint('child stderr', file=sys.stderr)\n"
    )
    _write(worker, recorder)
    _write(codex, "#!/usr/bin/env python3\n" + recorder, executable=True)
    env = os.environ.copy()
    env.update(
        {
            "CLAUDE_BOOSTER_ROUTED_BALANCER": str(balancer),
            "CLAUDE_BOOSTER_ROUTED_WORKER": str(worker),
            "CLAUDE_BOOSTER_ROUTED_CODEX_BIN": str(codex),
            "TEST_LOG": str(log),
            "TEST_ROUTE": route,
        }
    )
    return env, log, codex


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([sys.executable, str(RUNNER), *args], input=PROMPT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)


def _read_log(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_exact_worker_forwarding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env, log, _ = _environment(Path(directory), '{"provider":"codex-cli","model":"gpt-5.6-terra","reasoning_effort":"medium"}')
        result = _run(env, "recon", "--ephemeral", "--sandbox", "read-only")
        assert result.returncode == 0, result.stderr.decode()
        row = _read_log(log)
        assert row["argv"] == ["gpt-5.6-terra", "--ephemeral", "--sandbox", "read-only"]
        assert row["stdin"] == PROMPT.hex()
        assert row["env"] == {"CLAUDE_BOOSTER_ROUTE_SOURCE": "balancer", "CLAUDE_BOOSTER_TASK_CATEGORY": "recon", "CODEX_REASONING_EFFORT": "medium"}


def test_malformed_lookup_uses_unpinned_fallback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env, log, _ = _environment(Path(directory), '{"provider":"codex-cli","model":42,"reasoning_effort":"medium"}')
        result = _run(env, "recon", "--ephemeral", "--sandbox", "read-only")
        assert result.returncode == 0, result.stderr.decode()
        row = _read_log(log)
        assert row["argv"] == ["exec", "--ephemeral", "--sandbox", "read-only", "-"]
        assert row["stdin"] == PROMPT.hex()
        assert "degraded routing; unpinned Codex fallback" in result.stderr.decode()
        assert "-m" not in row["argv"] and not any(str(arg).startswith("--model") for arg in row["argv"])


def test_nonzero_lookup_uses_unpinned_fallback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        env, log, _ = _environment(tmp, '{"provider":"codex-cli","model":"gpt-5.6-terra","reasoning_effort":"medium"}')
        _write(tmp / "model_balancer.py", "raise SystemExit(9)\n")
        result = _run(env, "recon", "--ephemeral")
        assert result.returncode == 0, result.stderr.decode()
        row = _read_log(log)
        assert row["argv"] == ["exec", "--ephemeral", "-"]
        assert row["stdin"] == PROMPT.hex()
        assert "degraded routing; unpinned Codex fallback" in result.stderr.decode()


def test_lookup_launch_and_timeout_fail_open_without_waiting() -> None:
    runner = _load_runner()
    original_run = runner.subprocess.run
    calls: list[dict[str, object]] = []

    def failing_run(*args: object, **kwargs: object) -> object:
        calls.append(kwargs)
        raise OSError("simulated launch failure")

    runner.subprocess.run = failing_run
    try:
        assert runner._route("recon", Path("/safe/balancer.py")) is None
    finally:
        runner.subprocess.run = original_run
    assert calls == [{"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "timeout": runner._ROUTE_TIMEOUT_SECONDS, "check": False}]

    def timing_out(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    runner.subprocess.run = timing_out
    try:
        assert runner._route("recon", Path("/safe/balancer.py")) is None
    finally:
        runner.subprocess.run = original_run


def test_installer_enumerates_python_codex_routing_targets() -> None:
    installer = _load_installer()
    sources = {source.relative_to(ROOT).as_posix() for source, _ in installer.enumerate_template_files()}
    assert {
        "templates/scripts/codex_routed_worker.py",
        "templates/scripts/model_balancer.py",
        "templates/scripts/codex_worker.py",
    } <= sources


def test_malformed_lookup_uses_absolute_codex_bin_default() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env, log, codex = _environment(Path(directory), '{"provider":"codex-cli","model":42,"reasoning_effort":"medium"}')
        env.pop("CLAUDE_BOOSTER_ROUTED_CODEX_BIN")
        env["CODEX_BIN"] = str(codex)
        result = _run(env, "recon", "--ephemeral")
        assert result.returncode == 0, result.stderr.decode()
        row = _read_log(log)
        assert row["argv"] == ["exec", "--ephemeral", "-"]
        assert row["stdin"] == PROMPT.hex()
        assert "degraded routing; unpinned Codex fallback" in result.stderr.decode()
        assert "-m" not in row["argv"] and not any(str(arg).startswith("--model") for arg in row["argv"])


def test_non_codex_route_refuses_launch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env, log, _ = _environment(Path(directory), '{"provider":"anthropic","model":"claude-opus-5","reasoning_effort":"medium"}')
        result = _run(env, "coding")
        assert result.returncode == 65
        assert not log.exists()
        assert "refusing local Codex" in result.stderr.decode()


def test_caller_override_refuses_launch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env, log, _ = _environment(Path(directory), '{"provider":"codex-cli","model":"gpt-5.6-terra","reasoning_effort":"medium"}')
        for override in (
            ("-m", "gpt-5.6-sol"),
            ("-mgpt-5.6-sol",),
            ("--model", "gpt-5.6-sol"),
            ("--model=gpt-5.6-sol",),
            ("-c", "model=gpt-5.6-sol"),
            ("-cmodel=gpt-5.6-sol",),
            ("--config", "model=gpt-5.6-sol"),
            ("--config=model=gpt-5.6-sol",),
        ):
            result = _run(env, "recon", *override)
            assert result.returncode == 2
            assert not log.exists()
            assert "caller model override is forbidden" in result.stderr.decode()


def test_skill_keeps_no_bare_gpt_5_6_pin() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "`gpt-5.6` is a family alias" in text
    assert "codex_routed_worker.py CATEGORY --ephemeral --sandbox read-only" in text
    assert "gpt-5.6` --" not in text


def test_grok_route_refuses_and_launches_no_codex_child() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env, log, _ = _environment(Path(directory), '{"provider":"grok-cli","model":"grok-4.6"}')
        result = _run(env, "audit_tertiary")
        stderr = result.stderr.decode()
        assert result.returncode == 65
        assert not log.exists()
        assert "route provider is grok-cli (model grok-4.6)" in stderr
        assert "refusing local Codex" in stderr
        assert "grok_cli.py review" in stderr
        assert "unpinned Codex fallback" not in stderr


def test_zai_route_names_zai_runner() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env, log, _ = _environment(Path(directory), '{"provider":"zai-cli","model":"glm-5.1"}')
        result = _run(env, "audit_secondary")
        assert result.returncode == 65
        assert not log.exists()
        assert "zai_cli.py review" in result.stderr.decode()


def test_anthropic_route_names_agent_tool() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env, log, _ = _environment(Path(directory), '{"provider":"anthropic","model":"claude-opus-5"}')
        result = _run(env, "coding")
        assert result.returncode == 65
        assert not log.exists()
        assert "use the Agent tool / PAL" in result.stderr.decode()


def test_codex_route_without_reasoning_effort_falls_back_with_diagnostic() -> None:
    """An unpinned codex-cli route still runs, but says so on stderr."""
    with tempfile.TemporaryDirectory() as directory:
        env, log, _ = _environment(Path(directory), '{"provider":"codex-cli","model":"gpt-5.6-terra"}')
        result = _run(env, "recon", "--ephemeral")
        assert result.returncode == 0, result.stderr.decode()
        row = _read_log(log)
        assert row["argv"] == ["exec", "--ephemeral", "-"]
        assert "degraded routing; unpinned Codex fallback" in result.stderr.decode()


def test_unknown_category_diagnostic_names_the_category() -> None:
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        env, log, _ = _environment(tmp, '{"provider":"codex-cli","model":"gpt-5.6-terra","reasoning_effort":"medium"}')
        _write(
            tmp / "model_balancer.py",
            "import sys\nprint(\"error: unknown category 'nope'\", file=sys.stderr)\nraise SystemExit(1)\n",
        )
        result = _run(env, "nope", "--ephemeral")
        stderr = result.stderr.decode()
        assert result.returncode == 0, stderr
        assert _read_log(log)["argv"] == ["exec", "--ephemeral", "-"]
        assert "unknown category 'nope'; unpinned Codex fallback" in stderr


def main() -> int:
    for test in (
        test_exact_worker_forwarding,
        test_malformed_lookup_uses_unpinned_fallback,
        test_nonzero_lookup_uses_unpinned_fallback,
        test_lookup_launch_and_timeout_fail_open_without_waiting,
        test_malformed_lookup_uses_absolute_codex_bin_default,
        test_non_codex_route_refuses_launch,
        test_caller_override_refuses_launch,
        test_grok_route_refuses_and_launches_no_codex_child,
        test_zai_route_names_zai_runner,
        test_anthropic_route_names_agent_tool,
        test_codex_route_without_reasoning_effort_falls_back_with_diagnostic,
        test_unknown_category_diagnostic_names_the_category,
        test_skill_keeps_no_bare_gpt_5_6_pin,
        test_installer_enumerates_python_codex_routing_targets,
    ):
        test()
    print("PASS: Codex executable exact-route contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
