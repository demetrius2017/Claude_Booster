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

import json
import os
import re
import sys
from pathlib import Path
import install_codex_bridge_test_support as _h
from install_codex_bridge_test_support import IDENTITY, INSTALL_PY, ROOT, WRAPPER, _cleanup, _fail, _fresh_home, _ok, _run
import test_install_codex_bridge_safety as _safety


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
            (
                ROOT / "templates" / "commands" / "start.md",
                commands_dir / "start.md",
            ),
            (
                ROOT / "templates" / "commands" / "handover.md",
                commands_dir / "handover.md",
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
        for term in ("slice_ledger.py", "slice_git.py", "slice_telemetry.py", "slice_calibration.py", "session-start", "durable prerequisite", "operation_failed", "control-na", "session-terminal", "no backfill", "claude hooks/wrappers advisory; native codex observational/no enforcement", "transcript discovery"):
            if term not in combined_goal_text:
                errors.append(f"installed advisory telemetry contract missing {term!r}")

        # Main installer delivery: the bridge command specs depend on these
        # project-agnostic CLIs, so a fresh temp HOME must receive exact bytes.
        for script_name in ("slice_telemetry.py", "slice_telemetry_core.py", "slice_calibration.py", "slice_calibration_core.py", "slice_bootstrap_core.py", "slice_session_registry_core.py"):
            source = ROOT / "templates" / "scripts" / script_name
            installed = Path(home) / ".claude" / "scripts" / script_name
            if not installed.is_file():
                errors.append(f"installed telemetry script missing: {installed}")
            elif installed.read_bytes() != source.read_bytes():
                errors.append(f"installed telemetry script differs from canonical source: {installed}")

        # Executable advisory chain in the installed temp HOME. This proves the
        # command prose names callable CLIs and that `off` mutates only
        # directional state, not slice history.
        project = Path(home) / "advisory-project"
        project.mkdir()
        for command in (
            ["git", "init", "-q", str(project)],
            ["git", "-C", str(project), "config", "user.name", "Test"],
            ["git", "-C", str(project), "config", "user.email", "test@example.invalid"],
        ):
            result = _run(command, home)
            if result.returncode != 0:
                errors.append(f"fixture git setup failed: {result.stderr.strip()}")
        (project / "work.txt").write_text("baseline\n", encoding="utf-8")
        (project / ".gitignore").write_text(".claude/state/\n", encoding="utf-8")
        _run(["git", "-C", str(project), "add", "."], home)
        _run(["git", "-C", str(project), "commit", "-qm", "seed"], home)
        directional = project / ".claude" / "autopilot.json"
        directional.parent.mkdir(exist_ok=True)
        directional_state = {"version": 1, "enabled": True, "scope": str(project), "north_star": "test", "calls_used": 0, "max_fable_calls": 3, "degraded": False, "decision_policy": "delegate_except_ui_and_hard_authority", "reservations": {}, "checkpoints": [], "provenance": []}
        directional.write_text(json.dumps(directional_state), encoding="utf-8")
        scripts = Path(home) / ".claude" / "scripts"
        ledger_cli, git_cli, telemetry_cli, calibration_cli = scripts / "slice_ledger.py", scripts / "slice_git.py", scripts / "slice_telemetry.py", scripts / "slice_calibration.py"

        failed_project = Path(home) / "failed-activation-project"
        failed_project.mkdir(); _run(["git", "init", "-q", str(failed_project)], home)
        failed_registry = failed_project / ".claude/state/slice_session_events.jsonl"
        failed_registry.parent.mkdir(parents=True); failed_registry.write_text("{\n", encoding="utf-8"); failed_registry.chmod(0o600)
        failed_activation = _run([sys.executable, str(calibration_cli), "--cwd", str(failed_project), "session-start", "--run-id", "failed-run", "--session-id", "failed-session", "--provider", "codex_rollout_v1", "--artifact-domain", "implementation", "--expected-control", "ledger"], home)
        # This branch is the executable fail-closed orchestration contract:
        # acquire is reachable only after durable activation succeeds.
        failed_acquire = None
        if failed_activation.returncode == 0:
            failed_acquire = _run([sys.executable, str(ledger_cli), "--cwd", str(failed_project), "acquire", "--slice-id", "forbidden", "--artifact-contract", "must not start", "--allowed-path", "work.txt", "--session-id", "failed-session", "--run-id", "failed-run"], home)
        if failed_activation.returncode == 0 or failed_acquire is not None or (failed_project / ".claude/state/slice_ledger.json").exists():
            errors.append("failed durable session-start did not gate acquire fail-closed")

        activation_transcript = Path(home) / "activation-rollout.jsonl"
        activation_payload={"id":"thread-test","session_id":"session-test","parent_thread_id":None,"thread_source":"user","source":"user","cwd":str(project),"cli_version":"0.145.0-alpha.13"}
        activation_transcript.write_text(json.dumps({"timestamp":"2026-01-01T00:00:00Z","type":"session_meta","payload":activation_payload})+"\n", encoding="utf-8")
        wrong_activation = Path(home) / "wrong-activation.jsonl"
        wrong_activation.write_text(json.dumps({"timestamp":"2026-01-01T00:00:00Z","type":"session_meta","payload":{**activation_payload,"session_id":"wrong-root"}})+"\n", encoding="utf-8")
        registry_before = (project / ".claude/state/slice_session_events.jsonl").read_bytes() if (project / ".claude/state/slice_session_events.jsonl").exists() else b""
        rejected_activation = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "session-start", "--run-id", "run-test", "--session-id", "session-test", "--provider", "codex_rollout_v1", "--artifact-domain", "implementation", "--expected-control", "ledger", "--transcript", str(wrong_activation)], home)
        registry_after = (project / ".claude/state/slice_session_events.jsonl").read_bytes() if (project / ".claude/state/slice_session_events.jsonl").exists() else b""
        if rejected_activation.returncode == 0 or registry_before != registry_after: errors.append("wrong root activation was not byte-stable rejected")
        activation = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "session-start", "--run-id", "run-test", "--session-id", "session-test", "--provider", "codex_rollout_v1", "--artifact-domain", "implementation", "--expected-control", "ledger", "--expected-control", "git", "--expected-control", "verification", "--expected-control", "closure", "--transcript", str(activation_transcript)], home)
        ledger_control_start = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "control-start", "--run-id", "run-test", "--session-id", "session-test", "--kind", "ledger"], home)
        if activation.returncode != 0 or ledger_control_start.returncode != 0:
            errors.append(f"installed calibration activation/control failed: activation={activation.stderr.strip()} control={ledger_control_start.stderr.strip()}")

        acquire = _run([sys.executable, str(ledger_cli), "--cwd", str(project), "acquire", "--slice-id", "slice-test", "--artifact-contract", "change work.txt", "--allowed-path", "work.txt", "--session-id", "session-test", "--run-id", "run-test"], home)
        if acquire.returncode == 0:
            ledger_control_end = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "control-end", "--run-id", "run-test", "--session-id", "session-test", "--kind", "ledger"], home)
        else:
            ledger_control_end = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "control-na", "--run-id", "run-test", "--session-id", "session-test", "--kind", "ledger", "--reason", "operation_failed"], home)
        if ledger_control_end.returncode != 0:
            errors.append(f"installed ledger control completion failed: {ledger_control_end.stderr.strip()}")
        if acquire.returncode != 0:
            errors.append(f"installed acquire failed: {acquire.stderr.strip()}")
        else:
            acquired = json.loads(acquire.stdout)
            if acquired.get("ledger", {}).get("revision") != 1 or acquired.get("ledger", {}).get("run_id") != "run-test":
                errors.append("installed acquire returned wrong revision/run identity")

        failed_capture = _run([sys.executable, str(git_cli), "--cwd", str(project), "capture", "--run-id", "run-test", "--session-id", "wrong-session", "--revision", "1"], home)
        ledger_after_failure = json.loads((project / ".claude/state/slice_ledger.json").read_text(encoding="utf-8"))
        if failed_capture.returncode == 0 or ledger_after_failure.get("state") != "active" or ledger_after_failure.get("terminal_disposition") is not None:
            errors.append("failed advisory capture changed slice terminal truth")
        if json.loads(directional.read_text(encoding="utf-8")).get("enabled") is not True:
            errors.append("failed advisory capture changed directional goal state")

        git_control_start = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "control-start", "--run-id", "run-test", "--session-id", "session-test", "--kind", "git"], home)
        capture = _run([sys.executable, str(git_cli), "--cwd", str(project), "capture", "--run-id", "run-test", "--session-id", "session-test", "--revision", "1"], home)
        git_control_end = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "control-end" if capture.returncode == 0 else "control-na", "--run-id", "run-test", "--session-id", "session-test", "--kind", "git", *([] if capture.returncode == 0 else ["--reason", "operation_failed"])], home)
        if capture.returncode != 0:
            errors.append(f"installed capture failed: {capture.stderr.strip()}")
        if git_control_start.returncode != 0 or git_control_end.returncode != 0:
            errors.append("installed git control pair failed")
        verification_start = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "control-start", "--run-id", "run-test", "--session-id", "session-test", "--kind", "verification"], home)
        unavailable = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "control-na", "--run-id", "run-test", "--session-id", "session-test", "--kind", "verification", "--reason", "native_surface_unavailable"], home)
        closure_start = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "control-start", "--run-id", "run-test", "--session-id", "session-test", "--kind", "closure"], home)
        closure_unavailable = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "control-na", "--run-id", "run-test", "--session-id", "session-test", "--kind", "closure", "--reason", "capability_missing"], home)
        registry_path = project / ".claude/state/slice_session_events.jsonl"
        if verification_start.returncode != 0 or unavailable.returncode != 0 or closure_start.returncode != 0 or closure_unavailable.returncode != 0 or not registry_path.is_file():
            errors.append("installed typed UNKNOWN/registry missing")
        else:
            registry_types = [json.loads(line)["type"] for line in registry_path.read_text(encoding="utf-8").splitlines()]
            if registry_types != ["activated", "control_started", "control_ended", "control_started", "control_ended", "control_started", "control_unavailable", "control_started", "control_unavailable"]:
                errors.append(f"installed prospective registry sequence wrong: {registry_types}")
        window = project / "window.json"
        window.write_text(json.dumps({"schema_version": 1, "window_id": "integration", "started_at": "2000-01-01T00:00:00Z", "ended_at": "2099-01-01T00:00:00Z"}), encoding="utf-8")
        promotion = _run([sys.executable, str(calibration_cli), "--cwd", str(project), "evaluate", "--window-file", str(window)], home)
        if promotion.returncode == 0:
            errors.append("missing terminal/calibration evidence unexpectedly produced promotion")
        transcript = Path(home) / "sanitized-rollout.jsonl"
        transcript_rows = [
            {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": "session-test", "session_id": "session-test", "parent_thread_id": None, "thread_source": "user", "source": "user", "cwd": str(project), "cli_version": "0.145.0-alpha.13"}},
            {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg", "payload": {"type": "task_started"}},
            {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg", "payload": {"type": "task_complete"}},
        ]
        transcript.write_text("".join(json.dumps(row) + "\n" for row in transcript_rows), encoding="utf-8")
        failed_record = _run([sys.executable, str(telemetry_cli), "--cwd", str(project), "record", "--provider", "codex_rollout_v1", "--transcript", str(transcript), "--run-id", "run-test", "--session-id", "wrong-session"], home)
        state_after_record_failure = json.loads((project / ".claude/state/slice_ledger.json").read_text(encoding="utf-8"))
        if failed_record.returncode == 0 or state_after_record_failure.get("state") != "active" or state_after_record_failure.get("terminal_disposition") is not None:
            errors.append("failed advisory record changed slice terminal truth")
        if "goal_status" in json.loads(directional.read_text(encoding="utf-8")):
            errors.append("failed advisory record fabricated directional goal completion")

        record_args = [sys.executable, str(telemetry_cli), "--cwd", str(project), "record", "--provider", "codex_rollout_v1", "--transcript", str(transcript), "--run-id", "run-test", "--session-id", "session-test"]
        record = _run(record_args, home)
        status = _run([sys.executable, str(telemetry_cli), "--cwd", str(project), "status", "--run-id", "run-test", "--session-id", "session-test"], home)
        if record.returncode != 0 or status.returncode != 0:
            errors.append(f"installed telemetry record/status failed: record={record.stderr.strip()} status={status.stderr.strip()}")
        else:
            record_json, status_json = json.loads(record.stdout), json.loads(status.stdout)
            if record_json.get("result", {}).get("receipt_sha256") != status_json.get("result", {}).get("receipt_sha256"):
                errors.append("installed telemetry cached receipt is not readable/stable")

        ledger_path, events_path = project / ".claude/state/slice_ledger.json", project / ".claude/state/slice_events.jsonl"
        before_off = (ledger_path.read_bytes(), events_path.read_bytes())
        directional_state["enabled"] = False
        directional.write_text(json.dumps(directional_state, sort_keys=True), encoding="utf-8")
        after_off = (ledger_path.read_bytes(), events_path.read_bytes())
        if before_off != after_off or b'"type":"closed"' in after_off[1]:
            errors.append("directional off mutated or falsely closed slice history")

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


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"=== test_install_codex_bridge_integration.py ===")
    print(f"ROOT:    {ROOT}")
    print(f"INSTALL: {INSTALL_PY}")
    print(f"WRAPPER: {WRAPPER}")
    print()

    _safety.test_t9_syntax_valid()          # cheapest check first
    _safety.test_t8_no_shadowed_helpers()
    test_t1_dry_run_shows_both_plans()
    test_t2_dry_run_no_bridge()
    _safety.test_t10_no_module_level_home_paths()
    test_t3_yes_installs_bridge_manifest()
    test_t4_idempotent_no_new_backup()
    test_t5_no_bridge_opt_out()
    _safety.test_t6_wrapper_dry_run()
    _safety.test_t7_bridge_failure_isolation()

    print()
    print(f"Results: {_h.passed} passed, {_h.failed} failed")
    return 0 if _h.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
