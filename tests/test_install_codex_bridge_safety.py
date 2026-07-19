"""Installer wrapper, isolation, syntax, and runtime-HOME acceptance cases."""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from install_codex_bridge_test_support import IDENTITY, INSTALL_PY, ROOT, WRAPPER, _cleanup, _fail, _fresh_home, _ok, _run

def test_t6_wrapper_dry_run() -> None:
    label = "T6: scripts/install_codex_bridge.sh --dry-run exits 0 and prints bridge plan"; home = _fresh_home()
    try:
        if not WRAPPER.exists(): _fail(label, f"Wrapper not found: {WRAPPER}"); return
        result = _run([str(WRAPPER), "--dry-run"] + IDENTITY, home); stdout = (result.stdout + result.stderr).lower()
        if result.returncode != 0: _fail(label, f"exit={result.returncode}\nstdout={result.stdout[:500]}\nstderr={result.stderr[:300]}"); return
        has_bridge = "bridge" in stdout and (re.search(r"skills?\s*:?\s*\d+", stdout) or "codex" in stdout or "prompts" in stdout)
        _ok(label) if has_bridge else _fail(label, f"Bridge plan not found in wrapper stdout.\nstdout={result.stdout[:600]}")
    finally: _cleanup(home)

def test_t7_bridge_failure_isolation() -> None:
    label = "T7: bridge collision → exit 50, Claude manifest intact (bridge fails, Claude NOT rolled back)"; home = _fresh_home()
    try:
        aliases = sorted(p.parent.name for p in (ROOT / "templates/codex/skills").glob("*/SKILL.md") if p.parent.name != "booster-command")
        if not aliases: _fail(label, "No skill aliases found in templates/codex/skills/"); return
        collision = Path(home) / ".agents/skills" / aliases[0] / "SKILL.md"; collision.parent.mkdir(parents=True); collision.write_text("# NOT a bridge file\nThis file is user-owned and must block the bridge.\n")
        result = _run([sys.executable, str(INSTALL_PY), "--yes"] + IDENTITY, home); manifest = Path(home) / ".claude/.booster-manifest.json"; errors=[]
        if result.returncode != 50: errors.append(f"exit={result.returncode}, expected 50")
        if not manifest.exists(): errors.append(f"Claude manifest missing: {manifest}")
        else:
            try:
                if not isinstance(json.loads(manifest.read_text()), dict): errors.append("Claude manifest is not a JSON object")
            except json.JSONDecodeError as exc: errors.append(f"Claude manifest is not valid JSON: {exc}")
        _fail(label, "\n       ".join(errors)) if errors else _ok(label)
    finally: _cleanup(home)

def test_t8_no_shadowed_helpers() -> None:
    label = "T8: install.py has exactly one module-level def each of load_manifest/atomic_write/write_manifest"
    try: tree = ast.parse(INSTALL_PY.read_text())
    except SyntaxError as exc: _fail(label, f"SyntaxError in install.py: {exc}"); return
    counts={}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.col_offset == 0: counts[node.name]=counts.get(node.name,0)+1
    errors=[f"{name}: {counts.get(name,0)} module-level defs (expected exactly 1)" for name in ("load_manifest","atomic_write","write_manifest") if counts.get(name,0)!=1]
    _fail(label,"; ".join(errors)) if errors else _ok(label)

def test_t9_syntax_valid() -> None:
    label="T9: install.py passes ast.parse (syntax valid)"
    try: ast.parse(INSTALL_PY.read_text()); _ok(label)
    except SyntaxError as exc: _fail(label,f"SyntaxError: {exc}")

def test_t10_no_module_level_home_paths() -> None:
    label="T10: bridge dest paths respect runtime HOME (no module-level baked paths)"; home=_fresh_home()
    try:
        _run([sys.executable,str(INSTALL_PY),"--dry-run"]+IDENTITY,home); errors=[]
        for name in (".codex",".agents"):
            if (Path(home)/name).exists(): errors.append(f"--dry-run wrote {name} into sandbox HOME")
        _fail(label,"\n       ".join(errors)) if errors else _ok(label)
    finally: _cleanup(home)
