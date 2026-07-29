#!/usr/bin/env python3
"""Tests for delegate_gate.py / ask_gate.py hardening (2026-04-22 incident).

Purpose:
    Prove that (a) sub-agent context auto-skips both gates, (b) the
    `.delegate_mode=off` / `.ask_gate=off` bypass is refused when the
    caller is a sub-agent, (c) the stderr block message no longer
    advertises the bypass recipe, (d) every invocation appends one
    JSON line to the appropriate decision log.

Contract:
    Runs each gate as a subprocess with crafted stdin JSON and a
    per-test tempdir set as $HOME / $CLAUDE_HOME. Asserts on exit
    code, stderr, and the log files the gate appends to.

Limitations:
    - Does NOT exercise the full transcript-tail parser of ask_gate
      (the `messages` array path in stdin is the easier contract).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DELEGATE_GATE = REPO_ROOT / "templates" / "scripts" / "delegate_gate.py"
ASK_GATE = REPO_ROOT / "templates" / "scripts" / "ask_gate.py"


def _run(gate_path: pathlib.Path, stdin_obj: dict, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(gate_path)],
        input=json.dumps(stdin_obj),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _mk_env(tempdir: pathlib.Path, extra: dict | None = None) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(tempdir)
    env["CLAUDE_HOME"] = str(tempdir / ".claude")
    # Avoid inherited bypass envs leaking into tests.
    env.pop("CLAUDE_BOOSTER_SKIP_DELEGATE_GATE", None)
    env.pop("CLAUDE_BOOSTER_SKIP_ASK_GATE", None)
    if extra:
        env.update(extra)
    return env


def _log_path(tempdir: pathlib.Path, name: str) -> pathlib.Path:
    return tempdir / ".claude" / "logs" / name


def _read_jsonl(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))  # must be valid JSON
    return rows


def _mk_project(tempdir: pathlib.Path) -> pathlib.Path:
    proj = tempdir / "proj"
    (proj / ".claude").mkdir(parents=True, exist_ok=True)
    (proj / ".git").mkdir(exist_ok=True)  # mark as project root
    return proj


class DelegateGateTests(unittest.TestCase):
    def test_delegate_gate_auto_skips_for_subagent(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "cwd": str(proj),
                "agent_id": "agent-001",
                "agent_type": "general-purpose",
                "session_id": "sess-a",
            }
            res = _run(DELEGATE_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertEqual(res.stderr, "")
            rows = _read_jsonl(_log_path(td_path, "delegate_gate_decisions.jsonl"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "auto_skip")
            self.assertEqual(rows[0]["agent_id"], "agent-001")

    def test_delegate_gate_blocks_lead_at_budget(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            # Pre-seed the counter so the *next* action exceeds budget=1.
            (proj / ".claude" / ".delegate_counter").write_text("1\n")
            env = _mk_env(td_path)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "cwd": str(proj),
                "session_id": "sess-lead-1",
            }
            res = _run(DELEGATE_GATE, payload, env)
            self.assertEqual(res.returncode, 2, msg=res.stderr)
            self.assertIn("delegate_gate", res.stderr)
            # No bypass recipe hints in stderr.
            self.assertNotRegex(res.stderr, r"(?i)echo\s+off")
            self.assertNotIn(".delegate_mode", res.stderr)
            self.assertNotIn("Disable per-repo", res.stderr)
            rows = _read_jsonl(_log_path(td_path, "delegate_gate_decisions.jsonl"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "block")

    def test_delegate_gate_bypass_honoured_for_lead(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            (proj / ".claude" / ".delegate_mode").write_text("off\n")
            env = _mk_env(td_path)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "cwd": str(proj),
                "session_id": "sess-lead-2",
            }
            res = _run(DELEGATE_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            bypass = _read_jsonl(_log_path(td_path, "gate_bypass_attempts.jsonl"))
            self.assertEqual(len(bypass), 1)
            self.assertEqual(bypass[0]["decision"], "bypass_honoured")
            self.assertEqual(bypass[0]["gate"], "delegate")
            self.assertEqual(bypass[0]["agent_id"], "")

    def test_delegate_gate_bypass_refused_for_subagent(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            (proj / ".claude" / ".delegate_mode").write_text("off\n")
            env = _mk_env(td_path)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "cwd": str(proj),
                "agent_id": "agent-sneaky",
                "agent_type": "general-purpose",
                "session_id": "sess-sub",
            }
            res = _run(DELEGATE_GATE, payload, env)
            # Auto-skip takes precedence — exit 0.
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            bypass = _read_jsonl(_log_path(td_path, "gate_bypass_attempts.jsonl"))
            # But the refused-attempt must be logged for surveillance.
            self.assertEqual(len(bypass), 1)
            self.assertEqual(bypass[0]["decision"], "bypass_refused")
            self.assertEqual(bypass[0]["agent_id"], "agent-sneaky")

    def test_delegate_gate_delegation_tool_allowed(self):
        # Sanity: TaskCreate resets the counter and is always allowed.
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "tool_name": "TaskCreate",
                "tool_input": {},
                "cwd": str(proj),
            }
            res = _run(DELEGATE_GATE, payload, env)
            self.assertEqual(res.returncode, 0)


class AskGateTests(unittest.TestCase):
    def test_ask_gate_auto_skips_for_subagent(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "agent_id": "agent-99",
                "agent_type": "Explore",
                "messages": [
                    {"role": "assistant", "content": "Apply patch?"},
                ],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertEqual(res.stderr, "")
            rows = _read_jsonl(_log_path(td_path, "ask_gate_decisions.jsonl"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "auto_skip")

    def test_ask_gate_blocks_on_forbidden_pattern(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "messages": [
                    {"role": "assistant", "content": "I found the issue. Apply patch now?"},
                ],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 2, msg=res.stderr)
            self.assertIn("ask_gate", res.stderr)
            # No bypass recipe.
            self.assertNotIn(".ask_gate", res.stderr)
            self.assertNotRegex(res.stderr, r"(?i)echo\s+off")
            rows = _read_jsonl(_log_path(td_path, "ask_gate_decisions.jsonl"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "block")
            self.assertTrue(rows[0]["matched_pattern"])

    def test_ask_gate_blocks_russian_immediate_action_commitment(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "messages": [{
                    "role": "assistant",
                    "content": "Справедливо — сказал «запускаю» и не запустил. Запускаю.",
                }],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 2, msg=res.stderr)
            self.assertIn("Запускаю", res.stderr)

    def test_ask_gate_blocks_english_immediate_action_commitment(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "messages": [{
                    "role": "assistant",
                    "content": "You're right. I'll run the prototype gate now.",
                }],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 2, msg=res.stderr)
            self.assertIn("I'll run", res.stderr)

    def test_ask_gate_blocks_exact_russian_recap_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "messages": [{
                    "role": "assistant",
                    "content": (
                        "Дальше запускаю прототип-ворота: измерю, на скольких из "
                        "1099 спорных регионов приор корпуса меняет топ-1 и не "
                        "уводит ли он от золота."
                    ),
                }],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 2, msg=res.stderr)
            self.assertIn("Дальше запускаю", res.stderr)

    def test_ask_gate_allows_ordinary_future_plan(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "messages": [{
                    "role": "assistant",
                    "content": (
                        "В дорожной карте на следующий релиз команда проверит "
                        "влияние приора на спорные регионы."
                    ),
                }],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)

    def test_ask_gate_allows_quoted_immediate_action_examples(self):
        examples = (
            'Антипаттерн: «Запускаю.»\n'
            'English example: "I\'ll run it now."\n'
            "> Сейчас проверю.\n"
            "`Запускаю.`"
        )
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "messages": [{"role": "assistant", "content": examples}],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)

    def test_ask_gate_allows_completed_action_status(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "messages": [{
                    "role": "assistant",
                    "content": "Запустил прототип-ворота и проверил 1099 регионов: тест завершён.",
                }],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)

    def test_ask_gate_bypass_honoured_for_lead(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            (proj / ".claude" / ".ask_gate").write_text("off\n")
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "messages": [
                    {"role": "assistant", "content": "Apply patch?"},
                ],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            bypass = _read_jsonl(_log_path(td_path, "gate_bypass_attempts.jsonl"))
            self.assertEqual(len(bypass), 1)
            self.assertEqual(bypass[0]["decision"], "bypass_honoured")
            self.assertEqual(bypass[0]["gate"], "ask")

    def test_ask_gate_bypass_refused_for_subagent(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            (proj / ".claude" / ".ask_gate").write_text("off\n")
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "agent_id": "agent-bad",
                "messages": [{"role": "assistant", "content": "Apply patch?"}],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 0)
            bypass = _read_jsonl(_log_path(td_path, "gate_bypass_attempts.jsonl"))
            self.assertEqual(len(bypass), 1)
            self.assertEqual(bypass[0]["decision"], "bypass_refused")


class StderrHygieneTests(unittest.TestCase):
    """Cross-gate: the stderr block message must not teach the bypass."""

    FORBIDDEN_HINTS = re.compile(r"(?i)(echo\s+off|\.delegate_mode|\.ask_gate)")

    def test_delegate_gate_stderr_has_no_bypass_hint(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            (proj / ".claude" / ".delegate_counter").write_text("1\n")
            env = _mk_env(td_path)
            payload = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(proj / "src" / "x.py")},
                "cwd": str(proj),
            }
            res = _run(DELEGATE_GATE, payload, env)
            self.assertEqual(res.returncode, 2)
            self.assertNotRegex(res.stderr, self.FORBIDDEN_HINTS)

    def test_ask_gate_stderr_has_no_bypass_hint(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "messages": [{"role": "assistant", "content": "Proceed with deploy?"}],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 2)
            self.assertNotRegex(res.stderr, self.FORBIDDEN_HINTS)


class JsonlValidityTests(unittest.TestCase):
    def test_all_log_rows_are_valid_json(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)

            # Fire delegate gate on several paths:
            _run(DELEGATE_GATE, {"tool_name": "Read", "tool_input": {}, "cwd": str(proj)}, env)
            _run(DELEGATE_GATE, {"tool_name": "TaskCreate", "tool_input": {}, "cwd": str(proj)}, env)
            _run(DELEGATE_GATE, {"tool_name": "Bash", "tool_input": {"command": "echo hi"},
                                 "cwd": str(proj), "agent_id": "a-1"}, env)
            # Fire ask gate on several paths:
            _run(ASK_GATE, {"cwd": str(proj)}, env)
            _run(ASK_GATE, {"cwd": str(proj), "agent_id": "a-2",
                            "messages": [{"role": "assistant", "content": "Apply?"}]}, env)
            _run(ASK_GATE, {"cwd": str(proj),
                            "messages": [{"role": "assistant", "content": "Apply patch?"}]}, env)

            for name in (
                "delegate_gate_decisions.jsonl",
                "ask_gate_decisions.jsonl",
                "gate_bypass_attempts.jsonl",
            ):
                p = _log_path(td_path, name)
                if not p.exists():
                    continue  # some logs may have 0 lines legitimately
                for line in p.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    json.loads(line)  # raises on bad JSON


class LogFailSoftTests(unittest.TestCase):
    """The gate must exit 0/2 based on policy even if log writes fail."""

    def test_log_write_fails_soft_when_dir_readonly(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            logs_dir = td_path / ".claude" / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(logs_dir, 0o500)  # r-x only, no write
            except OSError:
                raise unittest.SkipTest("cannot chmod tempdir on this filesystem")
            # Verify the chmod actually took effect: try to write a file.
            probe = logs_dir / ".probe"
            try:
                probe.write_text("x")
                probe.unlink()
                raise unittest.SkipTest("chmod 0o500 not enforced (likely running as root)")
            except (PermissionError, OSError):
                pass  # good — dir is effectively read-only

            env = _mk_env(td_path)
            payload = {
                "tool_name": "TaskCreate",  # delegation signal → allow
                "tool_input": {},
                "cwd": str(proj),
            }
            try:
                res = _run(DELEGATE_GATE, payload, env)
                # Exit 0 even though logging can't write — gate must not
                # fail just because the log dir is read-only.
                self.assertEqual(res.returncode, 0, msg=f"stderr={res.stderr!r}")
                # stderr should be clean (no traceback).
                self.assertNotIn("Traceback", res.stderr)
            finally:
                # Restore so the tempdir can be cleaned up.
                try:
                    os.chmod(logs_dir, stat.S_IRWXU)
                except OSError:
                    pass


class FailClosedStdinTests(unittest.TestCase):
    """Fix 1: malformed / non-dict stdin must exit 2 (fail-closed)."""

    def _run_raw(self, gate: pathlib.Path, raw: str, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(gate)],
            input=raw,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _assert_blocked(self, gate: pathlib.Path, raw: str) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            env = _mk_env(td_path)
            res = self._run_raw(gate, raw, env)
            self.assertEqual(res.returncode, 2, msg=f"stdin={raw!r} stderr={res.stderr!r}")
            self.assertIn("malformed hook payload", res.stderr)
            # Log must carry a block row with the right reason.
            log_name = (
                "delegate_gate_decisions.jsonl" if gate == DELEGATE_GATE
                else "ask_gate_decisions.jsonl"
            )
            rows = _read_jsonl(_log_path(td_path, log_name))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "block")
            self.assertIn("invalid hook payload", rows[0]["reason"])

    def test_delegate_gate_blocks_malformed_json(self):
        self._assert_blocked(DELEGATE_GATE, "abc{")

    def test_delegate_gate_blocks_json_array(self):
        self._assert_blocked(DELEGATE_GATE, "[]")

    def test_delegate_gate_blocks_json_string(self):
        self._assert_blocked(DELEGATE_GATE, '"hello"')

    def test_ask_gate_blocks_malformed_json(self):
        self._assert_blocked(ASK_GATE, "abc{")

    def test_ask_gate_blocks_json_array(self):
        self._assert_blocked(ASK_GATE, "[]")

    def test_ask_gate_blocks_json_string(self):
        self._assert_blocked(ASK_GATE, '"hello"')


class MultiSignalSubagentTests(unittest.TestCase):
    """Fix 2: either agent_id OR agent_type alone is enough to mark sub-agent."""

    def test_delegate_gate_auto_skips_on_agent_type_only(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "cwd": str(proj),
                # Intentionally NO agent_id, only agent_type.
                "agent_type": "Explore",
                "session_id": "sess-t-only",
            }
            res = _run(DELEGATE_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            rows = _read_jsonl(_log_path(td_path, "delegate_gate_decisions.jsonl"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "auto_skip")

    def test_ask_gate_auto_skips_on_agent_type_only(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                # Only agent_type, no agent_id.
                "agent_type": "Explore",
                "messages": [{"role": "assistant", "content": "Apply patch?"}],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            rows = _read_jsonl(_log_path(td_path, "ask_gate_decisions.jsonl"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "auto_skip")


class AskGateRedactionTests(unittest.TestCase):
    """Fix 3: allow-path drops excerpt; block-path redacts secrets first."""

    def test_allow_path_has_no_message_excerpt(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            # Non-forbidden content → allow path.
            payload = {
                "cwd": str(proj),
                "messages": [
                    {"role": "assistant", "content": "All done. Here is a key: sk-abc123xyz"},
                ],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            rows = _read_jsonl(_log_path(td_path, "ask_gate_decisions.jsonl"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "allow")
            # Excerpt field must be absent (or empty) AND must not carry
            # the raw secret if it is present at all.
            self.assertNotIn("message_excerpt", rows[0])

    def test_block_path_redacts_secrets_in_excerpt(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            env = _mk_env(td_path)
            # Forbidden pattern + a secret-like token → block path.
            payload = {
                "cwd": str(proj),
                "messages": [
                    {"role": "assistant",
                     "content": "Apply patch? Here's my api_key=sk-abc123xyz456"},
                ],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 2, msg=res.stderr)
            rows = _read_jsonl(_log_path(td_path, "ask_gate_decisions.jsonl"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "block")
            excerpt = rows[0]["message_excerpt"]
            self.assertNotIn("sk-abc123xyz456", excerpt)
            self.assertIn("<redacted>", excerpt)


class TelemetryClaudeHomeTests(unittest.TestCase):
    """Fix 4: telemetry must read bypass log from $CLAUDE_HOME/logs, not ~/.claude/logs."""

    def test_bypass_row_under_claude_home_is_seen(self):
        telemetry = REPO_ROOT / "templates" / "scripts" / "telemetry_agent_health.py"
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            (proj / "reports").mkdir(exist_ok=True)
            # One handover so the _load_handovers path has something to chew.
            (proj / "reports" / "handover_2026-04-22_120000.md").write_text(
                "## Summary\nok\n", encoding="utf-8",
            )
            env = _mk_env(td_path)
            # Write a bypass_refused row under $CLAUDE_HOME/logs — gate path.
            logs = td_path / ".claude" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            row = {
                "ts": "2026-04-22T12:00:00Z",
                "gate": "delegate",
                "decision": "bypass_refused",
                "reason": "sub-agent cannot disable gate",
            }
            (logs / "gate_bypass_attempts.jsonl").write_text(
                json.dumps(row) + "\n", encoding="utf-8",
            )
            res = subprocess.run(
                [sys.executable, str(telemetry),
                 "--project", str(proj), "--json",
                 "--today", "2026-04-22"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            payload = json.loads(res.stdout)
            by = payload["signals"]["gate_bypass"]
            self.assertEqual(by["recent"], 1)
            self.assertEqual(by["refused"], 1)
            self.assertFalse(by["ok"])


class AttemptedBypassCorrelationTests(unittest.TestCase):
    """Fix 5: auto_skip + bypass_refused across log files carries a correlation flag."""

    def test_delegate_gate_auto_skip_carries_attempted_bypass(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            (proj / ".claude" / ".delegate_mode").write_text("off\n")
            env = _mk_env(td_path)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "cwd": str(proj),
                "agent_id": "agent-sneaky",
                "agent_type": "general-purpose",
                "session_id": "sess-corr",
            }
            res = _run(DELEGATE_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)

            decisions = _read_jsonl(_log_path(td_path, "delegate_gate_decisions.jsonl"))
            bypass = _read_jsonl(_log_path(td_path, "gate_bypass_attempts.jsonl"))
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["decision"], "auto_skip")
            self.assertTrue(decisions[0].get("attempted_bypass"))
            self.assertEqual(len(bypass), 1)
            self.assertEqual(bypass[0]["decision"], "bypass_refused")

    def test_ask_gate_auto_skip_carries_attempted_bypass(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            proj = _mk_project(td_path)
            (proj / ".claude" / ".ask_gate").write_text("off\n")
            env = _mk_env(td_path)
            payload = {
                "cwd": str(proj),
                "agent_id": "agent-bad",
                "agent_type": "Explore",
                "messages": [{"role": "assistant", "content": "Apply patch?"}],
            }
            res = _run(ASK_GATE, payload, env)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            decisions = _read_jsonl(_log_path(td_path, "ask_gate_decisions.jsonl"))
            bypass = _read_jsonl(_log_path(td_path, "gate_bypass_attempts.jsonl"))
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["decision"], "auto_skip")
            self.assertTrue(decisions[0].get("attempted_bypass"))
            self.assertEqual(len(bypass), 1)
            self.assertEqual(bypass[0]["decision"], "bypass_refused")


class GateCommonTests(unittest.TestCase):
    """Pin behaviour of the shared helpers so refactors don't drift."""

    def setUp(self):
        # _gate_common lives next to the gates — import via the same
        # on-disk path to keep this test representative of how the gates
        # resolve it at runtime.
        gates_dir = REPO_ROOT / "templates" / "scripts"
        if str(gates_dir) not in sys.path:
            sys.path.insert(0, str(gates_dir))
        import importlib
        import _gate_common  # noqa: F401
        self.mod = importlib.reload(_gate_common)

    def test_walk_up_to_returns_none_on_missing_marker(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td)
            # No marker exists → must return None, not raise.
            result = self.mod.walk_up_to(p, lambda x: (x / "NEVER").exists())
            self.assertIsNone(result)

    def test_walk_up_to_finds_nearest_ancestor(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "marker").mkdir()
            deep = root / "a" / "b" / "c"
            deep.mkdir(parents=True)
            result = self.mod.walk_up_to(deep, lambda p: (p / "marker").is_dir())
            self.assertEqual(result, root)

    def test_append_jsonl_lazy_mkdir(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            os.environ["CLAUDE_HOME"] = str(td_path / ".claude")
            try:
                # First write creates the dir; subsequent writes must reuse.
                self.mod._LOG_DIR_READY.clear()
                self.mod.append_jsonl("t.jsonl", {"k": 1})
                self.assertTrue((td_path / ".claude" / "logs" / "t.jsonl").exists())
                # Second call: dir already in cache, no error.
                self.mod.append_jsonl("t.jsonl", {"k": 2})
                lines = (td_path / ".claude" / "logs" / "t.jsonl").read_text().splitlines()
                self.assertEqual(len(lines), 2)
            finally:
                os.environ.pop("CLAUDE_HOME", None)
                self.mod._LOG_DIR_READY.clear()

    def test_decision_constants_match_expected_strings(self):
        self.assertEqual(self.mod.DECISION_ALLOW, "allow")
        self.assertEqual(self.mod.DECISION_BLOCK, "block")
        self.assertEqual(self.mod.DECISION_AUTO_SKIP, "auto_skip")
        self.assertEqual(self.mod.DECISION_BYPASS_HONOURED, "bypass_honoured")
        self.assertEqual(self.mod.DECISION_BYPASS_REFUSED, "bypass_refused")

    def test_is_subagent_context_multi_signal(self):
        f = self.mod.is_subagent_context
        # Missing/empty — Lead.
        self.assertFalse(f({}))
        self.assertFalse(f({"agent_id": "", "agent_type": ""}))
        self.assertFalse(f(None))  # defensive
        self.assertFalse(f([]))    # non-dict
        # agent_id set only.
        self.assertTrue(f({"agent_id": "a-1"}))
        # agent_type set only.
        self.assertTrue(f({"agent_type": "Explore"}))
        # both set.
        self.assertTrue(f({"agent_id": "a", "agent_type": "Plan"}))
        # Non-string fields should be safely rejected.
        self.assertFalse(f({"agent_id": 0, "agent_type": None}))

    def test_redact_secrets_replaces_common_patterns(self):
        r = self.mod.redact_secrets
        self.assertEqual(r("nothing to hide"), "nothing to hide")
        self.assertIn("<redacted>", r("api_key=sk-abc123"))
        self.assertNotIn("sk-abc123", r("api_key=sk-abc123"))
        self.assertIn("<redacted>", r("Bearer eyJhbGciOi.foo"))
        self.assertIn("<redacted>", r("password=hunter2"))
        self.assertEqual(r(""), "")
        self.assertEqual(r(None), "")  # non-str tolerated


if __name__ == "__main__":
    unittest.main()
