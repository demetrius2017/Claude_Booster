#!/usr/bin/env python3
"""Black-box acceptance test for durable Lead epistemic anchors.

The test copies template hooks into a temporary HOME/repository and never
touches deployed ``~/.claude`` state.  Every assertion prints [PASS]/[FAIL].
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def run(script: Path, payload: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)], input=payload, text=True,
        capture_output=True, env=env, timeout=15, check=False,
    )


def output_context(cp: subprocess.CompletedProcess[str]) -> str:
    try:
        obj = json.loads(cp.stdout)
        return obj["hookSpecificOutput"]["additionalContext"]
    except Exception:
        return ""


def constitution_lines(context: str) -> list[str]:
    """Extract the five-line constitution without prescribing its heading."""
    lines = context.splitlines()
    starts = [i for i, line in enumerate(lines) if "CONSTITUTION" in line.upper() and "LEAD" in line.upper()]
    if len(starts) != 1:
        return []
    out: list[str] = []
    for line in lines[starts[0] + 1:]:
        if line.startswith("==="):
            break
        if line.strip():
            out.append(line.strip())
    return out


def copy_hook_tree(tmp: Path) -> Path:
    scripts = tmp / "scripts"
    shutil.copytree(TEMPLATES / "scripts", scripts)
    # Isolate SessionStart from the real DB and from unrelated optional helper
    # modules.  The hook contract, not rolling_memory's packaging, is under test.
    (scripts / "rolling_memory.py").write_text(
        "import os\n"
        "def init_db():\n"
        "    if os.environ.get('TEST_MEMORY_FAIL'): raise RuntimeError('injected memory failure')\n"
        "def backup_db(): pass\n"
        "def forget_expired(): pass\n"
        "def build_context(**kwargs): return 'stub memory context'\n"
    )
    return scripts


def test_session_start(tmp: Path, env: dict[str, str], scripts: Path) -> None:
    hook = scripts / "memory_session_start.py"
    project = tmp / "repo"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / ".delegate_counter").write_text("9\n")

    startup = run(hook, json.dumps({"source": "startup", "session_id": "s", "cwd": str(project)}), env)
    ctx = output_context(startup)
    lines = constitution_lines(ctx)
    check("SessionStart startup returns valid JSON/rc0", startup.returncode == 0 and bool(ctx))
    check("startup injects one Lead constitution of exactly five nonempty lines", len(lines) == 5)
    joined = " ".join(lines).lower()
    check("constitution carries epistemic semantics", all(x in joined for x in ("evidence", "assum", "fals")))
    check("legacy model-balancer and limits blocks survive", "MODEL BALANCER" in ctx and "LIMITS" in ctx)
    check("legacy delegation counter reset survives", (project / ".claude" / ".delegate_counter").read_text() == "0\n")

    for source in ("resume", "compact"):
        cp = run(hook, json.dumps({"source": source, "session_id": "s", "cwd": str(project)}), env)
        check(f"SessionStart source={source} is valid/nonblocking", cp.returncode == 0 and output_context(cp) != "")
        check(f"source={source} omits full constitution", not constitution_lines(output_context(cp)))

    for label, payload in (("empty", ""), ("malformed", "{"), ("wrong-shape", "[]")):
        cp = run(hook, payload, env)
        valid = False
        try:
            valid = isinstance(json.loads(cp.stdout), dict)
        except Exception:
            pass
        check(f"SessionStart {label} input stays valid JSON/nonblocking", cp.returncode == 0 and valid)

    failed = run(hook, json.dumps({"source": "startup", "cwd": str(project)}),
                 dict(env, TEST_MEMORY_FAIL="1"))
    failed_ctx = output_context(failed)
    check("SessionStart dependency failure stays valid and preserves startup/legacy context",
          failed.returncode == 0 and len(constitution_lines(failed_ctx)) == 5
          and "MODEL BALANCER" in failed_ctx and "LIMITS" in failed_ctx)

    source = hook.read_text()
    check("constitution helper failure has explicit nonblocking degradation path",
          "constitution" in source.lower() and ("degrad" in source.lower() or "except" in source.lower()))


def test_phase(tmp: Path, env: dict[str, str], scripts: Path) -> None:
    hook = scripts / "phase_prompt_inject.py"
    repo = tmp / "phase-repo"
    phase_dir = repo / ".claude"
    phase_dir.mkdir(parents=True)
    outputs: dict[str, str] = {}
    semantic = {
        "RECON": ("code", "runtime", "report", "memory"),
        "PLAN": ("assumption", "alternative", "fals"),
        "IMPLEMENT": ("contract", "downstream"),
        "AUDIT": ("reject", "fals"),
        "VERIFY": ("pass", "evidence", "exit"),
        "MERGE": ("residual", "downstream"),
    }
    for phase, words in semantic.items():
        (phase_dir / ".phase").write_text(phase)
        cp = run(hook, json.dumps({"cwd": str(repo)}), env)
        line = cp.stdout.strip()
        outputs[phase] = line
        low = line.lower()
        check(f"phase {phase} emits exactly one line/rc0", cp.returncode == 0 and len(line.splitlines()) == 1)
        check(f"phase {phase} preserves legacy phase/advance hint", f"[phase: {phase}]" in line and "phase.py set" in line)
        check(f"phase {phase} has required epistemic semantics", all(w in low for w in words), low)
    check("all six phase anchors are distinct", len(set(outputs.values())) == 6)

    (phase_dir / ".phase").unlink()
    for label, prep in (
        ("absent", lambda: None),
        ("empty", lambda: (phase_dir / ".phase").write_text("")),
        ("binary", lambda: (phase_dir / ".phase").write_bytes(b"\xff\xfe")),
    ):
        prep()
        cp = run(hook, json.dumps({"cwd": str(repo)}), env)
        check(f"phase file {label} safely falls back to RECON", cp.returncode == 0 and "[phase: RECON]" in cp.stdout)
        try:
            (phase_dir / ".phase").unlink()
        except FileNotFoundError:
            pass
    (phase_dir / ".phase").write_text("ALIEN")
    cp = run(hook, json.dumps({"cwd": str(repo)}), env)
    check("unknown phase is nonblocking", cp.returncode == 0 and "unknown phase" in cp.stdout.lower())


def test_compact(tmp: Path, env: dict[str, str], scripts: Path) -> None:
    hook = scripts / "compact_advisor_inject.py"
    sid = "12345678-1234-1234-1234-123456789abc"
    marker = Path(env["HOME"]) / ".claude" / f".compact_recommended_{sid}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"session_id": sid, "cwd": str(tmp)})
    marker.write_text("90000 200000")
    first = run(hook, payload, env)
    second = run(hook, payload, env)
    ctx = output_context(first)
    low = ctx.lower()
    check("compact valid marker emits valid advisory once", first.returncode == 0 and bool(ctx) and second.stdout == "")
    check("compact preserves advisory and adds short epistemic re-anchor", "/compact" in ctx and all(w in low for w in ("verified", "assum", "fals", "integration")))
    check("compact and phase hooks preserve independent output contracts",
          first.stdout.lstrip().startswith("{") and not run(scripts / "phase_prompt_inject.py", "{}", env).stdout.lstrip().startswith("{"))

    cases = (("empty", ""), ("malformed-json", "{"), ("non-object", "[]"),
             ("missing-session", "{}"), ("invalid-session", '{"session_id":"../x"}'))
    for label, raw in cases:
        cp = run(hook, raw, env)
        check(f"compact {label} input is silent/nonblocking", cp.returncode == 0 and cp.stdout == "")
    skip_env = dict(env, CLAUDE_BOOSTER_SKIP_COMPACT_ADVISOR="1")
    marker.write_text("90000 200000")
    cp = run(hook, payload, skip_env)
    check("compact env skip is silent", cp.returncode == 0 and cp.stdout == "")
    marker.unlink(missing_ok=True)
    marker.write_text("noise")
    one = run(hook, payload, env)
    two = run(hook, payload, env)
    check("malformed marker/noise is consumed silently once", one.returncode == 0 and one.stdout == "" and two.stdout == "")

    marker.write_text("90000 200000")
    old_mode = marker.parent.stat().st_mode
    try:
        marker.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        one = run(hook, payload, env)
        two = run(hook, payload, env)
    finally:
        marker.parent.chmod(old_mode)
    advisories = sum(bool(output_context(cp)) for cp in (one, two))
    check("FBL-002 unlink failure is non-crashing and emits at most once", one.returncode == 0 and two.returncode == 0 and advisories <= 1,
          f"advisories={advisories}")


def contains_all(path: Path, groups: tuple[tuple[str, ...], ...]) -> bool:
    text = path.read_text().lower()
    return all(any(term in text for term in group) for group in groups)


def test_documents() -> None:
    start = TEMPLATES / "commands" / "start.md"
    consilium = TEMPLATES / "commands" / "consilium.md"
    go = TEMPLATES / "commands" / "go.md"
    runner = TEMPLATES / "codex" / "skills" / "booster-command" / "SKILL.md"
    check("start Epistemic Receipt has all required fields", contains_all(start, (
        (("verified directly", "verified:")), (("inherited",)), (("unverified",)),
        (("disconfirm", "falsif")), (("integration",)),
    )))
    start_text = start.read_text().lower()
    check("start old hard stops remain", "hard stop" in start_text and "required reading" in start_text)
    check("delegation schema covers evidence classes/consumer/falsifier/shared premise", contains_all(runner, (
        (("evidence class", "direct evidence")), (("consumer", "downstream")), (("falsif", "disconfirm")), (("shared premise", "shared-premise", "unverified premise")),
    )))
    check("consilium has pre-spawn falsification and independent synthesis", contains_all(consilium, (
        (("pre-spawn", "before spawn")), (("falsif", "disconfirm")), (("independent",)), (("synthesis",)),
    )))
    go_text = go.read_text().lower()
    check("go has pre-worker premise multiplication check",
          "before spawning worker" in go_text and "multiplication check" in go_text and any(x in go_text for x in ("shared premise", "shared-premise")))
    verdict = go_text[go_text.find("## phase 4 — verdict"):]
    check("go pre-verdict checks evidence/falsifier/residual risk",
          bool(verdict) and all(x in verdict for x in ("before emitting the verdict", "evidence", "fals", "residual")))
    check("go retains seven stages, two-Fable cap, exit-code-only PASS", "1/7" in go_text and "7/7" in go_text and "at most 2 fable" in go_text and "exit code" in go_text)


def test_settings_and_manifest() -> None:
    settings = json.loads((TEMPLATES / "settings.json.template").read_text())
    ups = settings["hooks"]["UserPromptSubmit"]
    commands = [h["command"] for group in ups for h in group["hooks"]]
    check("UserPromptSubmit hook count/order unchanged", len(commands) == 2 and "phase_prompt_inject.py" in commands[0] and "compact_advisor_inject.py" in commands[1])
    pre = settings["hooks"]["PreToolUse"]
    pre_commands = [h["command"].lower() for group in pre for h in group["hooks"]]
    check("no universal catch-all PreToolUse/MCP epistemic prose hook",
          not any(any(word in command for word in ("epistemic", "constitution", "lead_anchor", "prompt_inject")) for command in pre_commands))
    manifest_path = ROOT / "docs" / "dep_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
        flat = json.dumps(manifest).lower()
        valid = True
    except Exception:
        flat, valid = "", False
    check("dependency manifest is valid JSON", valid)
    check("manifest records phase injector component relationships", "phase_prompt_inject" in flat and any(x in flat for x in ("userpromptsubmit", "settings.json.template", "called_by")))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lead-anchor-test-") as td:
        tmp = Path(td)
        home = tmp / "home"
        home.mkdir()
        env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
        scripts = copy_hook_tree(tmp)
        test_session_start(tmp, env, scripts)
        test_phase(tmp, env, scripts)
        test_compact(tmp, env, scripts)
    test_documents()
    test_settings_and_manifest()
    print(f"\nSummary: {len(FAILURES)} failure(s)")
    if FAILURES:
        print("Failed: " + "; ".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
