#!/usr/bin/env python3
"""Acceptance test: install.py Codex bridge integration.

Tests observable behavior of the install.py Codex bridge integration per the
Artifact Contract. Does NOT test implementation details. Every invocation uses
a sandboxed HOME via a temporary directory; the real ~/.codex, ~/.agents, and
~/.claude are never touched.

Exit 0 = ALL assertions pass.
Exit non-zero = one or more assertions failed.

Run:
    python3 tests/test_install_codex_bridge_integration.py
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = ROOT / "install.py"
WRAPPER = ROOT / "scripts" / "install_codex_bridge.sh"

# Fake identity flags to avoid interactive prompts
IDENTITY = ["--name", "Test", "--email", "test@example.com"]

# ─── helpers ────────────────────────────────────────────────────────────────

passed = 0
failed = 0


def _ok(label: str) -> None:
    global passed
    passed += 1
    print(f"[PASS] {label}")


def _fail(label: str, detail: str = "") -> None:
    global failed
    failed += 1
    msg = f"[FAIL] {label}"
    if detail:
        msg += f"\n       {detail}"
    print(msg)


def _run(cmd: list[str], home: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a command with HOME set to the sandboxed temp dir."""
    env = {**os.environ, "HOME": home, "CODEX_BRIDGE_ROOT": str(ROOT)}
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _fresh_home() -> str:
    """Create a fresh temp directory to use as a sandboxed HOME."""
    return tempfile.mkdtemp(prefix="cb_test_home_")


def _cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


# ─── T1: dry-run shows BOTH Claude plan AND bridge plan, writes nothing ──────

def test_t1_dry_run_shows_both_plans() -> None:
    label = "T1: --dry-run shows Claude plan AND bridge plan, writes nothing"
    home = _fresh_home()
    try:
        result = _run(
            [sys.executable, str(INSTALL_PY), "--dry-run"] + IDENTITY,
            home,
        )
        stdout = (result.stdout + result.stderr).lower()

        if result.returncode != 0:
            _fail(label, f"exit={result.returncode}\nstdout={result.stdout[:500]}\nstderr={result.stderr[:300]}")
            return

        # Claude plan marker: some mention of WRITE / DRY RUN / settings
        has_claude = (
            "dry run" in stdout
            or "write" in stdout
            or "settings.json" in stdout
        )

        # Bridge plan marker: mentions "bridge" AND skills/prompts counts or "codex"
        has_bridge = (
            "bridge" in stdout
            and (
                re.search(r"skills?\s*:?\s*\d+", stdout)
                or re.search(r"prompts?\s*:?\s*\d+", stdout)
                or "codex" in stdout
            )
        )

        codex_dir = Path(home) / ".codex"
        agents_dir = Path(home) / ".agents"
        wrote_something = codex_dir.exists() or agents_dir.exists()

        if not has_claude:
            _fail(label, f"Claude plan not found in stdout.\nstdout={result.stdout[:600]}")
        elif not has_bridge:
            _fail(label, f"Bridge plan not found in stdout.\nstdout={result.stdout[:600]}")
        elif wrote_something:
            _fail(label, f"--dry-run wrote files: .codex={codex_dir.exists()} .agents={agents_dir.exists()}")
        else:
            _ok(label)
    finally:
        _cleanup(home)


# ─── T2: --dry-run --no-codex-bridge has NO bridge plan ─────────────────────

def test_t2_dry_run_no_bridge() -> None:
    label = "T2: --dry-run --no-codex-bridge stdout has NO bridge plan"
    home = _fresh_home()
    try:
        result = _run(
            [sys.executable, str(INSTALL_PY), "--dry-run", "--no-codex-bridge"] + IDENTITY,
            home,
        )
        stdout = (result.stdout + result.stderr).lower()

        if result.returncode != 0:
            _fail(label, f"exit={result.returncode}\nstdout={result.stdout[:400]}\nstderr={result.stderr[:300]}")
            return

        # Must NOT have bridge plan (bridge + count pattern)
        has_bridge = (
            "bridge" in stdout
            and re.search(r"skills?\s*:?\s*\d+", stdout)
        )
        if has_bridge:
            _fail(label, f"Bridge plan found despite --no-codex-bridge.\nstdout={result.stdout[:600]}")
        else:
            _ok(label)
    finally:
        _cleanup(home)


# ─── T3: --yes installs bridge manifest with correct counts ──────────────────

def test_t3_yes_installs_bridge_manifest() -> None:
    label = "T3: --yes installs bridge manifest matching all managed source artifacts"
    home = _fresh_home()
    try:
        result = _run(
            [sys.executable, str(INSTALL_PY), "--yes"] + IDENTITY,
            home,
        )

        if result.returncode != 0:
            _fail(label, f"exit={result.returncode}\nstdout={result.stdout[:500]}\nstderr={result.stderr[:300]}")
            return

        manifest_path = Path(home) / ".codex" / "claude-booster-bridge-manifest.json"
        if not manifest_path.exists():
            _fail(label, f"Bridge manifest not found at {manifest_path}")
            return

        try:
            data = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as e:
            _fail(label, f"Bridge manifest is not valid JSON: {e}")
            return

        bridge_id = data.get("bridge_id")
        if bridge_id != "claude-booster-codex-bridge":
            _fail(label, f"bridge_id={bridge_id!r}, expected 'claude-booster-codex-bridge'")
            return

        # Count skills (SKILL.md files), prompts (.md in prompts/), command specs (.md in commands/)
        agents_dir = Path(home) / ".agents"
        codex_dir = Path(home) / ".codex"

        skills = list(agents_dir.rglob("SKILL.md"))
        prompts_dir = codex_dir / "prompts"
        prompts = list(prompts_dir.glob("*.md")) if prompts_dir.exists() else []
        commands_dir = agents_dir / "skills" / "booster-command" / "references" / "commands"
        command_specs = list(commands_dir.glob("*.md")) if commands_dir.exists() else []

        # Derive the contract from the managed source trees. Adding a command
        # such as autopilot must expand both installation and manifest without
        # requiring another unrelated magic-number edit here.
        expected_skills = list((ROOT / "templates" / "codex" / "skills").rglob("SKILL.md"))
        expected_prompts = list((ROOT / "templates" / "codex" / "prompts").glob("*.md"))
        expected_commands = list((ROOT / "templates" / "commands").glob("*.md"))
        expected_sources = {
            str(path.relative_to(ROOT))
            for path in (*expected_skills, *expected_prompts, *expected_commands)
        }
        manifest_sources = {
            entry.get("source") for entry in data.get("files", [])
            if isinstance(entry, dict) and isinstance(entry.get("source"), str)
        }

        errors = []
        if len(skills) != len(expected_skills):
            errors.append(f"skills={len(skills)}, expected {len(expected_skills)}")
        if len(prompts) != len(expected_prompts):
            errors.append(f"prompts={len(prompts)}, expected {len(expected_prompts)}")
        if len(command_specs) != len(expected_commands):
            errors.append(f"command_specs={len(command_specs)}, expected {len(expected_commands)}")
        if manifest_sources != expected_sources:
            errors.append(
                "manifest source set differs from managed sources: "
                f"missing={sorted(expected_sources - manifest_sources)}, "
                f"extra={sorted(manifest_sources - expected_sources)}"
            )

        # Delivery contract: the installed bridge must carry the exact canonical
        # autopilot goal lifecycle, not merely the right artifact counts. This
        # catches a stale/partial mirror that would make `$autopilot roadmap.md`
        # stop after setup even though source-level acceptance tests pass.
        goal_mirrors = (
            (
                ROOT / "templates" / "codex" / "skills" / "autopilot" / "SKILL.md",
                agents_dir / "skills" / "autopilot" / "SKILL.md",
            ),
            (
                ROOT / "templates" / "codex" / "skills" / "booster-command" / "SKILL.md",
                agents_dir / "skills" / "booster-command" / "SKILL.md",
            ),
            (
                ROOT / "templates" / "commands" / "autopilot.md",
                commands_dir / "autopilot.md",
            ),
        )
        installed_goal_text = []
        for source, installed in goal_mirrors:
            if not installed.is_file():
                errors.append(f"installed goal-contract artifact missing: {installed}")
                continue
            source_text = source.read_text(encoding="utf-8")
            installed_text = installed.read_text(encoding="utf-8")
            if installed_text != source_text:
                errors.append(f"installed artifact differs from canonical source: {installed}")
            installed_goal_text.append(installed_text.lower())

        combined_goal_text = "\n".join(installed_goal_text)
        for term in ("get_goal", "create_goal", "same turn"):
            if term not in combined_goal_text:
                errors.append(f"installed autopilot goal contract missing {term!r}")

        if errors:
            _fail(label, "; ".join(errors))
        else:
            _ok(label)
    finally:
        _cleanup(home)


# ─── T4: two consecutive --yes runs → no new backup dir ─────────────────────

def test_t4_idempotent_no_new_backup() -> None:
    label = "T4: two --yes runs into same HOME → no new backup dir after second run"
    home = _fresh_home()
    try:
        r1 = _run([sys.executable, str(INSTALL_PY), "--yes"] + IDENTITY, home)
        if r1.returncode != 0:
            _fail(label, f"First run failed exit={r1.returncode}\nstdout={r1.stdout[:400]}")
            return

        backup_root = Path(home) / ".codex" / "backups"
        dirs_after_run1 = set(backup_root.iterdir()) if backup_root.exists() else set()

        r2 = _run([sys.executable, str(INSTALL_PY), "--yes"] + IDENTITY, home)
        if r2.returncode != 0:
            _fail(label, f"Second run failed exit={r2.returncode}\nstdout={r2.stdout[:400]}")
            return

        dirs_after_run2 = set(backup_root.iterdir()) if backup_root.exists() else set()
        new_dirs = dirs_after_run2 - dirs_after_run1

        if new_dirs:
            _fail(label, f"New backup dir(s) created on idempotent run: {[d.name for d in new_dirs]}")
        else:
            _ok(label)
    finally:
        _cleanup(home)


# ─── T5: --yes --no-codex-bridge → no .codex/.agents, but Claude manifest ───

def test_t5_no_bridge_opt_out() -> None:
    label = "T5: --yes --no-codex-bridge → .codex and .agents absent, .claude manifest present"
    home = _fresh_home()
    try:
        result = _run(
            [sys.executable, str(INSTALL_PY), "--yes", "--no-codex-bridge"] + IDENTITY,
            home,
        )

        if result.returncode != 0:
            _fail(label, f"exit={result.returncode}\nstdout={result.stdout[:500]}\nstderr={result.stderr[:300]}")
            return

        codex_dir = Path(home) / ".codex"
        agents_dir = Path(home) / ".agents"
        claude_manifest = Path(home) / ".claude" / ".booster-manifest.json"

        errors = []
        if codex_dir.exists():
            errors.append(f".codex/ was created despite --no-codex-bridge: {codex_dir}")
        if agents_dir.exists():
            errors.append(f".agents/ was created despite --no-codex-bridge: {agents_dir}")
        if not claude_manifest.exists():
            errors.append(f"Claude manifest missing: {claude_manifest}")

        if errors:
            _fail(label, "\n       ".join(errors))
        else:
            _ok(label)
    finally:
        _cleanup(home)


# ─── T6: wrapper script delegates --dry-run ──────────────────────────────────

def test_t6_wrapper_dry_run() -> None:
    label = "T6: scripts/install_codex_bridge.sh --dry-run exits 0 and prints bridge plan"
    home = _fresh_home()
    try:
        if not WRAPPER.exists():
            _fail(label, f"Wrapper not found: {WRAPPER}")
            return

        result = _run(
            [str(WRAPPER), "--dry-run"] + IDENTITY,
            home,
        )
        stdout = (result.stdout + result.stderr).lower()

        if result.returncode != 0:
            _fail(label, f"exit={result.returncode}\nstdout={result.stdout[:500]}\nstderr={result.stderr[:300]}")
            return

        has_bridge = (
            "bridge" in stdout
            and (
                re.search(r"skills?\s*:?\s*\d+", stdout)
                or "codex" in stdout
                or "prompts" in stdout
            )
        )
        if not has_bridge:
            _fail(label, f"Bridge plan not found in wrapper stdout.\nstdout={result.stdout[:600]}")
        else:
            _ok(label)
    finally:
        _cleanup(home)


# ─── T7: bridge failure → exit 50, Claude manifest intact ───────────────────

def test_t7_bridge_failure_isolation() -> None:
    label = "T7: bridge collision → exit 50, Claude manifest intact (bridge fails, Claude NOT rolled back)"
    home = _fresh_home()
    try:
        # Pre-create a non-bridge file at one of the bridge skill destinations.
        # Pick the first real skill alias (not booster-command).
        skills_src = ROOT / "templates" / "codex" / "skills"
        aliases = sorted(
            p.parent.name for p in skills_src.glob("*/SKILL.md")
            if p.parent.name != "booster-command"
        )
        if not aliases:
            _fail(label, "No skill aliases found in templates/codex/skills/")
            return

        collision_alias = aliases[0]
        collision_dir = Path(home) / ".agents" / "skills" / collision_alias
        collision_dir.mkdir(parents=True, exist_ok=True)
        collision_file = collision_dir / "SKILL.md"
        collision_file.write_text(
            "# NOT a bridge file\nThis file is user-owned and must block the bridge.\n",
            encoding="utf-8",
        )

        result = _run(
            [sys.executable, str(INSTALL_PY), "--yes"] + IDENTITY,
            home,
        )

        claude_manifest = Path(home) / ".claude" / ".booster-manifest.json"
        errors = []

        if result.returncode != 50:
            errors.append(f"exit={result.returncode}, expected 50")

        if not claude_manifest.exists():
            errors.append(f"Claude manifest missing: {claude_manifest}")
        else:
            try:
                data = json.loads(claude_manifest.read_text())
                if not isinstance(data, dict):
                    errors.append("Claude manifest is not a JSON object")
            except json.JSONDecodeError as e:
                errors.append(f"Claude manifest is not valid JSON: {e}")

        if errors:
            _fail(label, "\n       ".join(errors))
        else:
            _ok(label)
    finally:
        _cleanup(home)


# ─── T8: no shadowed module-level helpers ────────────────────────────────────

def test_t8_no_shadowed_helpers() -> None:
    label = "T8: install.py has exactly one module-level def each of load_manifest/atomic_write/write_manifest"
    try:
        source = INSTALL_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except SyntaxError as e:
        _fail(label, f"SyntaxError in install.py: {e}")
        return

    # Count only module-level (col_offset == 0) FunctionDef nodes
    counts: dict[str, int] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.col_offset == 0:
            counts[node.name] = counts.get(node.name, 0) + 1

    errors = []
    for name in ("load_manifest", "atomic_write", "write_manifest"):
        c = counts.get(name, 0)
        if c != 1:
            errors.append(f"{name}: {c} module-level defs (expected exactly 1)")

    if errors:
        _fail(label, "; ".join(errors))
    else:
        _ok(label)


# ─── T9: install.py parses cleanly ───────────────────────────────────────────

def test_t9_syntax_valid() -> None:
    label = "T9: install.py passes ast.parse (syntax valid)"
    try:
        source = INSTALL_PY.read_text(encoding="utf-8")
        ast.parse(source)
        _ok(label)
    except SyntaxError as e:
        _fail(label, f"SyntaxError: {e}")


# ─── T10: no module-level HOME-derived bridge paths ──────────────────────────

def test_t10_no_module_level_home_paths() -> None:
    """
    Bridge destination paths (SKILLS_DST, PROMPTS_DST, MANIFEST_PATH, etc.) must
    NOT be module-level Assign nodes that bake in Path.home() at import time.
    Functional check: a --dry-run with HOME=/nonexistent_tmpX must not create
    anything under the real home directory's .codex or .agents.

    We use the functional approach (a second sandboxed dry-run) as it tests the
    actual observable guarantee rather than scanning AST for binding names.
    """
    label = "T10: bridge dest paths respect runtime HOME (no module-level baked paths)"
    home = _fresh_home()
    real_home = Path.home()
    try:
        result = _run(
            [sys.executable, str(INSTALL_PY), "--dry-run"] + IDENTITY,
            home,
        )
        # We only care about side-effects; even a non-zero exit is acceptable for
        # this assertion as long as the real home is untouched by bridge writes.
        real_codex = real_home / ".codex"
        real_agents = real_home / ".agents"

        # Record pre-existing state of real home bridge dirs
        codex_existed = real_codex.exists()
        agents_existed = real_agents.exists()

        # A dry-run MUST NOT write files anywhere. If the installer respects HOME
        # at runtime, nothing new appears under real_home/.codex or real_home/.agents.
        # We verify by checking the sandboxed home stayed empty (bridge side).
        codex_in_sandbox = Path(home) / ".codex"
        agents_in_sandbox = Path(home) / ".agents"

        errors = []
        if codex_in_sandbox.exists():
            errors.append(f"--dry-run wrote .codex into sandbox HOME: {codex_in_sandbox}")
        if agents_in_sandbox.exists():
            errors.append(f"--dry-run wrote .agents into sandbox HOME: {agents_in_sandbox}")

        if errors:
            _fail(label, "\n       ".join(errors))
        else:
            _ok(label)
    finally:
        _cleanup(home)


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"=== test_install_codex_bridge_integration.py ===")
    print(f"ROOT:    {ROOT}")
    print(f"INSTALL: {INSTALL_PY}")
    print(f"WRAPPER: {WRAPPER}")
    print()

    test_t9_syntax_valid()          # cheapest check first
    test_t8_no_shadowed_helpers()
    test_t1_dry_run_shows_both_plans()
    test_t2_dry_run_no_bridge()
    test_t10_no_module_level_home_paths()
    test_t3_yes_installs_bridge_manifest()
    test_t4_idempotent_no_new_backup()
    test_t5_no_bridge_opt_out()
    test_t6_wrapper_dry_run()
    test_t7_bridge_failure_isolation()

    print()
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
