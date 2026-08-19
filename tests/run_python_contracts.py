#!/usr/bin/env python3
"""Run the repository's pytest and standalone Python contracts fail-fast."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
STANDALONE = TESTS_DIR / "test_lead_epistemic_anchors.py"


def inventory() -> list[Path]:
    """Return every Python test module in deterministic path order."""
    tests = sorted(TESTS_DIR.glob("test_*.py"), key=lambda path: path.name)
    if not tests:
        raise RuntimeError(f"no test_*.py files found in {TESTS_DIR}")
    if STANDALONE not in tests or not STANDALONE.is_file():
        raise RuntimeError(f"required standalone contract is unavailable: {STANDALONE}")
    return tests


def validate_runtime() -> None:
    """Reject a missing Python executable or unavailable pytest module."""
    executable = Path(sys.executable)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError(f"Python executable is unavailable: {sys.executable!r}")
    try:
        pytest_spec = importlib.util.find_spec("pytest")
    except (ImportError, ModuleNotFoundError, ValueError) as exc:
        raise RuntimeError(f"cannot inspect pytest module: {exc}") from exc
    if pytest_spec is None:
        raise RuntimeError(f"pytest module is unavailable to {sys.executable}")


def display(command: list[str]) -> str:
    """Render an argv without producing shell-reusable or ambiguous output."""
    return "argv=" + repr(command)


def run(command: list[str]) -> int:
    """Run one command without a shell and return its exact process status."""
    print(f"[python-contracts] RUN {display(command)}", file=sys.stderr, flush=True)
    try:
        completed = subprocess.run(command, cwd=ROOT, check=False)
    except OSError as exc:
        print(
            f"[python-contracts] ERROR unable to start command: {exc}; {display(command)}",
            file=sys.stderr,
            flush=True,
        )
        return 127
    if completed.returncode != 0:
        print(
            f"[python-contracts] FAIL status={completed.returncode}; {display(command)}",
            file=sys.stderr,
            flush=True,
        )
    return completed.returncode


def main() -> int:
    """Inventory contracts, optionally report them, then run both test modes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="validate prerequisites and print the inventory without running tests",
    )
    args = parser.parse_args()

    try:
        tests = inventory()
        validate_runtime()
    except RuntimeError as exc:
        print(f"[python-contracts] ERROR {exc}", file=sys.stderr)
        return 2

    print(f"[python-contracts] INVENTORY count={len(tests)}")
    for test in tests:
        print(test.relative_to(ROOT).as_posix())
    if args.inventory_only:
        return 0

    pytest_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests",
        "--ignore=tests/test_lead_epistemic_anchors.py",
    ]
    status = run(pytest_command)
    if status != 0:
        return status

    return run([sys.executable, "tests/test_lead_epistemic_anchors.py"])


if __name__ == "__main__":
    raise SystemExit(main())
