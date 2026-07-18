"""Black-box acceptance tests for Slice 3A verification transactions."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "templates/scripts"
LEDGER, GIT, CLOSE = SCRIPTS / "slice_ledger.py", SCRIPTS / "slice_git.py", SCRIPTS / "slice_close.py"


def _run(script: Path, repo: Path, *args: str) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(script), "--cwd", str(repo), *args], text=True, capture_output=True, check=False)
    output = result.stdout if result.returncode == 0 else result.stderr
    assert output, result
    return result.returncode, json.loads(output)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    (repo / "work.txt").write_text("baseline\n")
    (repo / ".gitignore").write_text(".claude/state/\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
    assert _run(LEDGER, repo, "acquire", "--slice-id", "s", "--artifact-contract", "verify work", "--allowed-path", "work.txt", "--session-id", "sess", "--run-id", "run1")[0] == 0
    assert _run(GIT, repo, "capture", "--run-id", "run1", "--session-id", "sess", "--revision", "1")[0] == 0
    return repo


def _evidence(tmp_path: Path, argv: list[str]) -> Path:
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({"schema_version": 1, "argv": argv, "timeout_seconds": 10}))
    return path


def _verify(repo: Path, evidence: Path, revision: str = "2") -> tuple[int, dict]:
    return _run(CLOSE, repo, "verify", "--run-id", "run1", "--session-id", "sess", "--revision", revision, "--evidence-file", str(evidence))


def _status(repo: Path, revision: str = "3") -> tuple[int, dict]:
    return _run(CLOSE, repo, "status", "--run-id", "run1", "--session-id", "sess", "--revision", revision)


def _verification_path(repo: Path, run_id: str = "run1") -> Path:
    return repo / ".claude/state/runs" / hashlib.sha256(run_id.encode()).hexdigest() / "slice_verification.json"


def test_argv_runner_passes_without_shell_and_records_bounded_claims(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker = tmp_path / "must-not-exist"
    evidence = _evidence(tmp_path, [sys.executable, "-c", "print('verified')", f";touch {marker}"])
    code, value = _verify(repo, evidence)
    receipt = value["result"]
    assert code == 0 and receipt["status"] == "pass"
    assert receipt["facts"]["state_unchanged"] is True
    assert receipt["claim"]["argv"][-1].startswith(";touch") and not marker.exists()
    assert receipt["claim"]["stdout"]["sha256"] and "verified" in receipt["claim"]["stdout"]["content"]
    ledger = json.loads((repo / ".claude/state/slice_ledger.json").read_text())
    assert ledger["revision"] == 3 and ledger["verification_sha256"]
    assert _status(repo)[1]["result"]["stale"] is False


def test_state_mutating_verifier_is_recorded_fail_and_immediately_stale(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    code = "from pathlib import Path; Path('work.txt').write_text('mutated')"
    result_code, value = _verify(repo, _evidence(tmp_path, [sys.executable, "-c", code]))
    assert result_code == 0 and value["result"]["status"] == "fail"
    assert value["result"]["facts"]["state_unchanged"] is False
    assert _status(repo)[1]["result"]["stale"] is True


@pytest.mark.parametrize("mutation", ["worktree", "index", "head"])
def test_verification_stales_on_every_git_anchor_or_content_change(tmp_path: Path, mutation: str) -> None:
    repo = _repo(tmp_path)
    _verify(repo, _evidence(tmp_path, [sys.executable, "-c", "print('ok')"]))
    (repo / "work.txt").write_text(f"{mutation}\n")
    if mutation in {"index", "head"}:
        subprocess.run(["git", "-C", str(repo), "add", "work.txt"], check=True)
    if mutation == "head":
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
    assert _status(repo)[1]["result"]["stale"] is True


def test_semantic_receipt_rewrite_is_rejected_by_event_binding(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verify(repo, _evidence(tmp_path, [sys.executable, "-c", "print('ok')"]))
    path = _verification_path(repo)
    receipt = json.loads(path.read_text())
    receipt["claim"]["exit_code"] = 99
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
    assert _status(repo)[0] == 4


def test_verified_contract_and_receipt_permissions_are_immutable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _verify(repo, _evidence(tmp_path, [sys.executable, "-c", "print('ok')"]))
    assert _run(LEDGER, repo, "update", "--run-id", "run1", "--session-id", "sess", "--revision", "3", "--artifact-contract", "changed", "--allowed-path", "work.txt")[0] == 3
    receipt = _verification_path(repo)
    os.chmod(receipt, 0o644)
    assert _status(repo)[0] == 4


def test_receipt_before_event_and_event_before_projection_crash_replay(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_dir = repo / ".claude/state"
    ledger_before = (state_dir / "slice_ledger.json").read_bytes()
    events_before = (state_dir / "slice_events.jsonl").read_bytes()
    evidence = _evidence(tmp_path, [sys.executable, "-c", "print('ok')"])
    _verify(repo, evidence)
    receipt_path = _verification_path(repo)
    receipt = receipt_path.read_bytes()
    # Crash after receipt replace but before event append.
    (state_dir / "slice_ledger.json").write_bytes(ledger_before)
    (state_dir / "slice_events.jsonl").write_bytes(events_before)
    code, _ = _verify(repo, evidence)
    assert code == 3 and receipt_path.read_bytes() == receipt
    # Start the transaction cleanly again, then simulate projection lag.
    (state_dir / "slice_events.jsonl").write_bytes(events_before)
    receipt_path.unlink()
    _verify(repo, evidence)
    # Crash after event fsync but before projection replace.
    (state_dir / "slice_ledger.json").write_bytes(ledger_before)
    assert _status(repo)[0] == 0
    assert json.loads((state_dir / "slice_ledger.json").read_text())["verification_sha256"]


def test_chain_linked_new_run_after_release_preserves_history(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _run(LEDGER, repo, "release", "--run-id", "run1", "--session-id", "sess", "--revision", "2")[0] == 0
    terminal = json.loads((repo / ".claude/state/slice_ledger.json").read_text())
    count = len((repo / ".claude/state/slice_events.jsonl").read_text().splitlines())
    code, value = _run(LEDGER, repo, "new-run", "--previous-run-id", "run1", "--previous-revision", "3", "--run-id", "run2", "--session-id", "sess2", "--slice-id", "s2", "--artifact-contract", "next", "--allowed-path", "work.txt")
    assert code == 0 and value["ledger"]["run_id"] == "run2" and value["ledger"]["revision"] == 1
    events = [json.loads(line) for line in (repo / ".claude/state/slice_events.jsonl").read_text().splitlines()]
    assert len(events) == count + 1 and events[-1]["type"] == "new_run"
    assert events[-1]["payload"]["previous_terminal_hash"] == terminal["last_event_hash"]
    retry = _run(LEDGER, repo, "new-run", "--previous-run-id", "run1", "--previous-revision", "3", "--run-id", "run2", "--session-id", "sess2", "--slice-id", "s2", "--artifact-contract", "next", "--allowed-path", "work.txt")
    assert retry[0] == 0 and len((repo / ".claude/state/slice_events.jsonl").read_text().splitlines()) == count + 1
    assert _run(LEDGER, repo, "new-run", "--previous-run-id", "run1", "--previous-revision", "3", "--run-id", "run3", "--session-id", "x", "--slice-id", "x", "--artifact-contract", "x", "--allowed-path", "work.txt")[0] == 3


def test_forged_unbound_receipt_is_never_notarized(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = _verification_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    os.chmod(path, 0o600)
    assert _verify(repo, _evidence(tmp_path, [sys.executable, "-c", "print('ok')"]))[0] in {3, 4}
    ledger = json.loads((repo / ".claude/state/slice_ledger.json").read_text())
    assert ledger["verification_sha256"] is None


def test_internal_verification_bind_rejects_nonexistent_receipt_without_event(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sys.path.insert(0, str(SCRIPTS))
    try:
        import slice_ledger as module
        from argparse import Namespace
        state_dir = repo / ".claude/state"
        events = state_dir / "slice_events.jsonl"
        before = events.read_bytes()
        relative = f".claude/state/runs/{hashlib.sha256(b'run1').hexdigest()}/slice_verification.json"
        args = Namespace(run_id="run1", session_id="sess", revision=2, verification_sha256="0" * 64, state_sha256="1" * 64, verification_path=relative)
        with pytest.raises(module.LedgerError):
            module._bind_verification(args, state_dir / "slice_ledger.json", events)
        assert events.read_bytes() == before
    finally:
        sys.path.pop(0)


def test_new_run_uses_independent_artifact_namespace_and_resets_verification(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first_baseline = repo / ".claude/state/runs" / hashlib.sha256(b"run1").hexdigest() / "slice_baseline.json"
    assert _run(LEDGER, repo, "release", "--run-id", "run1", "--session-id", "sess", "--revision", "2")[0] == 0
    assert _run(LEDGER, repo, "new-run", "--previous-run-id", "run1", "--previous-revision", "3", "--run-id", "run2", "--session-id", "sess2", "--slice-id", "s2", "--artifact-contract", "next", "--allowed-path", "work.txt")[0] == 0
    assert _run(GIT, repo, "capture", "--run-id", "run2", "--session-id", "sess2", "--revision", "1")[0] == 0
    run2_dir = repo / ".claude/state/runs" / hashlib.sha256(b"run2").hexdigest()
    ledger = json.loads((repo / ".claude/state/slice_ledger.json").read_text())
    assert first_baseline.exists() and (run2_dir / "slice_baseline.json").exists()
    assert not (run2_dir / "slice_verification.json").exists()
    assert ledger["run_id"] == "run2" and ledger["verification_sha256"] is None and ledger["verification_path"] is None
    assert first_baseline != run2_dir / "slice_baseline.json"


def test_output_flood_is_streamed_hashed_bounded_and_fails_policy(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    code = "import sys; sys.stdout.write('x'*(5*1024*1024))"
    _, value = _verify(repo, _evidence(tmp_path, [sys.executable, "-c", code]))
    output = value["result"]["claim"]["stdout"]
    assert value["result"]["status"] == "fail" and output["bytes"] == 5 * 1024 * 1024
    assert output["limit_exceeded"] is True and len(output["content"]) <= 16 * 1024


def test_verifier_gets_minimal_environment_without_injected_pythonpath(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setenv("PYTHONPATH", "/tmp/attacker")
    code = "import os; print(os.getenv('PYTHONPATH')); print(sorted(os.environ))"
    _, value = _verify(repo, _evidence(tmp_path, [sys.executable, "-c", code]))
    content = value["result"]["claim"]["stdout"]["content"]
    assert value["result"]["status"] == "pass" and "/tmp/attacker" not in content
    assert value["result"]["claim"]["environment_keys"] == ["LANG", "LC_ALL", "PATH"]


def test_executable_replacement_is_observed_and_forces_fail(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    script = repo / "replace-me.sh"
    replacement = repo / "replacement"
    replacement.write_text("#!/bin/sh\nexit 0\n")
    script.write_text("#!/bin/sh\ncp replacement replace-me.sh\nchmod +x replace-me.sh\n")
    os.chmod(script, 0o700)
    _, value = _verify(repo, _evidence(tmp_path, ["./replace-me.sh"]))
    claim = value["result"]["claim"]
    assert value["result"]["status"] == "fail" and claim["executable_before"] != claim["executable_after"]


def test_background_descendant_is_terminated_before_post_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    child = "import time; from pathlib import Path; time.sleep(1); Path('work.txt').write_text('late')"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
    _, value = _verify(repo, _evidence(tmp_path, [sys.executable, "-c", parent]))
    assert value["result"]["status"] == "pass"
    import time
    time.sleep(1.2)
    assert (repo / "work.txt").read_text() == "baseline\n"


def test_mutate_then_exact_restore_is_documented_provenance_boundary(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    code = "from pathlib import Path; p=Path('work.txt'); old=p.read_bytes(); p.write_bytes(b'temporary'); p.write_bytes(old)"
    _, value = _verify(repo, _evidence(tmp_path, [sys.executable, "-c", code]))
    assert value["result"]["status"] == "pass"
    assert value["result"]["facts"]["state_unchanged"] is True
    assert value["result"]["claim"]["argv"][2] == code
