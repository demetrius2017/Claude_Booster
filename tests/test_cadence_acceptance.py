#!/usr/bin/env python3
"""Independent acceptance contract for the CADENCE classification of Fable autopilot.

Black-box test: feeds the hook `templates/scripts/fable_autopilot.py` Claude
PreToolUse(AskUserQuestion) and Stop JSON on stdin and asserts ONLY the
documented, observable decision + reason. It does not read the Worker's
implementation and does not assume how CADENCE is detected.

Observable-behavior contract (terminology):
  - PreToolUse decision  = `permissionDecision` in {deny, allow}
  - Stop decision        = `decision:block`, or exit-0-no-output ("allow")
  - Routes to Dmitry     = permissionDecision:allow / reason contains USER_REQUIRED
  - FABLE_DELEGATE       = deny/block whose reason contains FABLE_DELEGATE and
                           mentions the consult path
  - CADENCE (the NEW class) = deny/block whose reason tells the Lead to PROCEED
                           on the roadmap default; MUST contain proceed+roadmap
                           tokens and MUST NOT contain `consult-decision` or `fable`.

Run: python3 tests/test_cadence_acceptance.py   (exit 0 iff every case passes)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "templates/scripts/fable_autopilot.py"
EXISTING_SUITE = ROOT / "tests/test_fable_autopilot.py"

# Exact incident prose — used verbatim.
INCIDENT = (
    "Хочешь — стартуем следующую фазу прямо сейчас (pricing-reality тест) "
    "под autopilot? Или это на новую сессию, а пока закрываемся handover'ом?"
)

PROCEED_RE = re.compile(r"(proceed|continue|продолж)", re.IGNORECASE)
ROADMAP_RE = re.compile(r"(roadmap|plan|next step|запланир|следующ)", re.IGNORECASE)


# --- helpers copied in spirit from the existing suite -----------------------

def run_hook(payload: dict, home: Path) -> tuple[int, dict, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CLAUDE_HOME"] = str(home / ".claude")
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )
    try:
        body = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic only
        raise AssertionError(f"hook emitted invalid JSON: {proc.stdout!r}") from exc
    return proc.returncode, body, proc.stderr


def decision(rc: int, body: dict, stderr: str) -> tuple[str, str]:
    specific = body.get("hookSpecificOutput", body)
    explicit = str(specific.get("permissionDecision", specific.get("decision", ""))).lower()
    if explicit:
        return explicit, str(specific.get("permissionDecisionReason", specific.get("reason", "")))
    return ("deny", stderr) if rc == 2 else ("allow", stderr)


# --- classifiers over the observable reason string --------------------------

def is_cadence(reason: str) -> bool:
    low = reason.lower()
    return bool(
        PROCEED_RE.search(reason)
        and ROADMAP_RE.search(reason)
        and "consult-decision" not in low
        and "fable" not in low
    )


def is_fable_delegate(reason: str) -> bool:
    low = reason.lower()
    return "fable_delegate" in low and ("consult" in low or "fable" in low)


def is_user_required(reason: str) -> bool:
    return "user_required" in reason.lower()


# --- state + payload construction ------------------------------------------

def make_home() -> tuple[tempfile.TemporaryDirectory, Path, Path, Path]:
    tmp = tempfile.TemporaryDirectory(prefix="cadence-acceptance-")
    home = Path(tmp.name)
    project = home / "project"
    state_dir = project / ".claude"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "autopilot.json"
    return tmp, home, project, state_path


def write_state(state_path: Path, project: Path, max_calls: int = 20) -> None:
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "enabled": True,
                "scope": str(project.resolve()),
                "north_star": "Ship the accepted feature without local-scope drift",
                "provenance": [],
                "checkpoints": [],
                "reservations": {},
                "max_fable_calls": max_calls,
                "calls_used": 0,
                "degraded": False,
                "usage_percent": 0,
            }
        ),
        encoding="utf-8",
    )


def ask_payload(project: Path, question: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "cwd": str(project),
        "tool_input": {"questions": [{"question": question, "header": "Decision", "options": []}]},
    }


def stop_payload(project: Path, transcript_path: Path, text: str, *, stop_active: bool = False) -> dict:
    transcript_path.write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "hook_event_name": "Stop",
        "cwd": str(project),
        "transcript_path": str(transcript_path),
        "stop_hook_active": stop_active,
    }


# --- test driver ------------------------------------------------------------

class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def record(self, ok: bool, label: str, diag: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"[PASS] {label}")
        else:
            self.failed += 1
            print(f"[FAIL] {label}" + (f"  --  {diag}" if diag else ""))


def main() -> int:
    if not (HOOK.is_file() and HOOK.stat().st_size > 0):
        print(f"[FAIL] hook artifact missing: {HOOK}")
        print("Results: 0 passed, 1 failed")
        return 1

    r = Results()
    tmp, home, project, state_path = make_home()
    try:
        transcript = home / "session.jsonl"

        # Case 1 — incident prose (Stop) → CADENCE.
        write_state(state_path, project)
        rc, body, stderr = run_hook(stop_payload(project, transcript, INCIDENT), home)
        got1, reason1 = decision(rc, body, stderr)
        c1 = got1 in {"block", "deny"} and is_cadence(reason1) and not is_fable_delegate(reason1)
        r.record(c1, "1: incident prose Stop → CADENCE block (not FABLE_DELEGATE, not allow)",
                 f"got={got1!r} reason={reason1!r}")

        # Case 2 — CADENCE reason matches both proceed + roadmap regexes.
        r.record(bool(PROCEED_RE.search(reason1) and ROADMAP_RE.search(reason1)),
                 "2: CADENCE reason matches (proceed|continue|продолж) AND (roadmap|plan|next step|запланир|следующ)",
                 f"reason={reason1!r}")

        # Case 3 — CADENCE reason contains neither consult-decision nor fable.
        low1 = reason1.lower()
        r.record("consult-decision" not in low1 and "fable" not in low1,
                 "3: CADENCE reason contains neither consult-decision nor fable",
                 f"reason={reason1!r}")

        # Case 4 — pure cadence AskUserQuestion → deny CADENCE.
        write_state(state_path, project)
        rc, body, stderr = run_hook(
            ask_payload(project, "Start the next phase now, or defer to a new session?"), home)
        got4, reason4 = decision(rc, body, stderr)
        c4 = got4 == "deny" and is_cadence(reason4) and not is_fable_delegate(reason4)
        r.record(c4, "4: pure cadence AskUserQuestion → deny CADENCE (not FABLE_DELEGATE)",
                 f"got={got4!r} reason={reason4!r}")

        # Case 5 — USER_ONLY regression: secret → allow / USER_REQUIRED.
        write_state(state_path, project)
        rc, body, stderr = run_hook(
            ask_payload(project, "Should I paste the production API secret key value now?"), home)
        got5, reason5 = decision(rc, body, stderr)
        r.record(got5 == "allow" and is_user_required(reason5),
                 "5: secret question → allow + USER_REQUIRED (routes to Dmitry)",
                 f"got={got5!r} reason={reason5!r}")

        # Case 6 — overlap precedence: cadence-phrased + hard boundary (publish/release).
        # Hard boundary must win: result must NOT be CADENCE, and must route to Dmitry
        # (allow/USER_REQUIRED, or exit-0 pass-through). It must not be an autonomous proceed.
        write_state(state_path, project)
        rc, body, stderr = run_hook(
            stop_payload(project, transcript,
                         "Should I publish the production release now, or in a new session?"), home)
        got6, reason6 = decision(rc, body, stderr)
        routes_to_user6 = (got6 == "allow") or is_user_required(reason6)
        c6 = (not is_cadence(reason6)) and routes_to_user6
        r.record(c6, "6: cadence+hard-boundary (publish release) → hard boundary wins, routes to Dmitry, NOT CADENCE",
                 f"got={got6!r} reason={reason6!r}")

        # Case 7 — engineering fork → FABLE_DELEGATE (not CADENCE).
        write_state(state_path, project)
        rc, body, stderr = run_hook(
            ask_payload(project, "Which retry-backoff strategy should the Worker use?"), home)
        got7, reason7 = decision(rc, body, stderr)
        c7 = got7 == "deny" and is_fable_delegate(reason7) and not is_cadence(reason7)
        r.record(c7, "7: engineering fork (retry-backoff) → deny FABLE_DELEGATE (not CADENCE)",
                 f"got={got7!r} reason={reason7!r}")

        # Case 8 — mixed design fork (Stop) → FABLE_DELEGATE (not CADENCE).
        write_state(state_path, project)
        rc, body, stderr = run_hook(
            stop_payload(project, transcript,
                         "Start phase 2 now, or redesign the schema first?"), home)
        got8, reason8 = decision(rc, body, stderr)
        c8 = got8 in {"block", "deny"} and is_fable_delegate(reason8) and not is_cadence(reason8)
        r.record(c8, "8: mixed design fork (redesign schema) → FABLE_DELEGATE (not CADENCE)",
                 f"got={got8!r} reason={reason8!r}")

        # Case 9 — push/publish is NOT cadence (external visibility).
        write_state(state_path, project)
        rc, body, stderr = run_hook(
            ask_payload(project, "Push now or in a new session?"), home)
        got9, reason9 = decision(rc, body, stderr)
        r.record(not is_cadence(reason9),
                 "9: 'Push now or in a new session?' → NOT CADENCE (external visibility)",
                 f"got={got9!r} reason={reason9!r}")

        # Case 10 — reworded cadence follow-up (Stop) → CADENCE.
        write_state(state_path, project)
        rc, body, stderr = run_hook(
            stop_payload(project, transcript, "Ок, начинаем следующую фазу сейчас?"), home)
        got10, reason10 = decision(rc, body, stderr)
        c10 = got10 in {"block", "deny"} and is_cadence(reason10) and not is_fable_delegate(reason10)
        r.record(c10, "10: reworded RU cadence follow-up → CADENCE",
                 f"got={got10!r} reason={reason10!r}")

        # Case 11 — case/ё variant of incident prose → still CADENCE.
        write_state(state_path, project)
        variant = INCIDENT.lower().replace("ё", "е")
        rc, body, stderr = run_hook(stop_payload(project, transcript, variant), home)
        got11, reason11 = decision(rc, body, stderr)
        c11 = got11 in {"block", "deny"} and is_cadence(reason11) and not is_fable_delegate(reason11)
        r.record(c11, "11: lowercased ё→е incident-prose variant → CADENCE",
                 f"got={got11!r} reason={reason11!r}")

        # Case 12 — intended delegate expansion (Stop) → FABLE_DELEGATE.
        write_state(state_path, project)
        rc, body, stderr = run_hook(
            stop_payload(project, transcript, "Хочешь, я добавлю ретраи?"), home)
        got12, reason12 = decision(rc, body, stderr)
        c12 = got12 in {"block", "deny"} and is_fable_delegate(reason12) and not is_cadence(reason12)
        r.record(c12, "12: non-cadence delegate expansion ('добавлю ретраи?') → FABLE_DELEGATE",
                 f"got={got12!r} reason={reason12!r}")

        # Case 13 — stop_hook_active=true → exit 0, no output.
        write_state(state_path, project)
        rc, body, stderr = run_hook(
            stop_payload(project, transcript,
                         "Start the next phase now, or defer to a new session?", stop_active=True), home)
        r.record(rc == 0 and body == {} and stderr.strip() == "",
                 "13: stop_hook_active=true → exit 0, no block/deny output",
                 f"rc={rc} body={body!r} stderr={stderr!r}")

        # Case 14 — full existing suite still passes (no regression).
        proc = subprocess.run(
            [sys.executable, str(EXISTING_SUITE)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=180, check=False)
        r.record(proc.returncode == 0,
                 "14: pre-existing suite test_fable_autopilot.py exits 0 (no regression)",
                 f"rc={proc.returncode} tail={proc.stdout.strip()[-400:]!r} err={proc.stderr.strip()[-400:]!r}")

        # Case 15 — engineering-scope start (noun) → FABLE_DELEGATE, not CADENCE.
        # Guards against the over-broad _is_cadence hole (diff-review MED): a
        # "begin the <scope-noun> now, or next session?" phrasing embeds a real
        # engineering decision and must be delegated, not auto-proceeded.
        for scope_q in (
            "Should I begin the database migration now, or next session?",
            "Should I start implementing the new auth flow now or later?",
        ):
            write_state(state_path, project)
            rc, body, stderr = run_hook(ask_payload(project, scope_q), home)
            g, reason = decision(rc, body, stderr)
            r.record(g in {"block", "deny"} and is_fable_delegate(reason) and not is_cadence(reason),
                     f"15: engineering-scope start → FABLE_DELEGATE, not CADENCE :: {scope_q}",
                     f"got={g!r} reason={reason!r}")

    finally:
        tmp.cleanup()

    print(f"Results: {r.passed} passed, {r.failed} failed")
    return 0 if r.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
