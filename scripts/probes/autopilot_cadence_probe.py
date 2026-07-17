#!/usr/bin/env python3
"""Read-only Prototype Gate probe for the CADENCE autopilot change.

Purpose:
  Empirically prove the current Stop leak and pin branch precedence BEFORE any
  Worker edit. No production state is mutated; the hook module is imported and
  its pure classifiers are called against fixed strings.

Contract:
  stdout — labeled probe results; exit 0 if all pre-change expectations hold.
Limitations:
  Imports the TEMPLATE copy (source of truth for tests), not the deployed copy.
"""
from __future__ import annotations
import importlib.util
import inspect
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "templates/scripts/fable_autopilot.py"
sys.path.insert(0, str(HOOK.parent))
spec = importlib.util.spec_from_file_location("autopilot_probe_mod", HOOK)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PROSE = ("Хочешь — стартуем следующую фазу прямо сейчас (pricing-reality тест) "
         "под autopilot? Или это на новую сессию, а пока закрываемся handover'ом?")
SECRET_CADENCE = "Should I paste the production API secret key value now or in a new session?"

fails = []
def rep(label, got, expect):
    ok = got == expect
    if not ok:
        fails.append(label)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expect={expect!r}")

# P2 — current _looks_like_question misses the cadence prose (the leak)
rep("P2 _looks_like_question(prose) == False (Stop leak proven)",
    mod._looks_like_question(PROSE), False)
# P3 — prose is not a hard-authority question
rep("P3 _requires_user(prose) == False (belongs below hard boundary)",
    mod._requires_user(PROSE), False)
# P4 — a cadence-shaped production-secret question DOES trip hard boundary
rep("P4 _requires_user(secret+cadence) == True (boundary intact)",
    mod._requires_user(SECRET_CADENCE), True)

# P2c (FBL-002) — does _question_text include options[] labels?
src = inspect.getsource(mod._question_text)
opt_included = ('options' in src and 'label' in src)
rep("P2c _question_text concatenates options[] labels (FBL-002)", opt_included, True)

# P1b (FBL-005) — what fields does _load_state actually require/read?
load_src = inspect.getsource(mod._load_state)
reads_north_star = 'north_star' in load_src
print(f"[INFO] _load_state references north_star field: {reads_north_star} "
      f"(FBL-005 -> if False, INV-11 north_star clause must be dropped)")

# order check
main_src = inspect.getsource(mod.main)
print(f"[INFO] main() references _requires_user: {'_requires_user' in main_src}; "
      f"_is_cadence present (pre-change should be False): {'_is_cadence' in main_src}")

print(f"\nProbe summary: {'PASS — all pre-change expectations hold' if not fails else 'FAIL: ' + ','.join(fails)}")
sys.exit(1 if fails else 0)
