"""Shared state and process helpers for Codex bridge installer acceptance."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = ROOT / "install.py"
WRAPPER = ROOT / "scripts" / "install_codex_bridge.sh"
IDENTITY = ["--name", "Test", "--email", "test@example.com"]
passed = 0
failed = 0

def _ok(label: str) -> None:
    global passed
    passed += 1; print(f"[PASS] {label}")

def _fail(label: str, detail: str = "") -> None:
    global failed
    failed += 1; print(f"[FAIL] {label}" + (f"\n       {detail}" if detail else ""))

def _run(cmd: list[str], home: str, timeout: int = 120) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": home, "CODEX_BRIDGE_ROOT": str(ROOT)}
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)

def _fresh_home() -> str:
    return tempfile.mkdtemp(prefix="cb_test_home_")

def _cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
