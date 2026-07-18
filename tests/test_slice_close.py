"""Black-box acceptance tests for Slice 3A verification transactions."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
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


def _repo(tmp_path: Path, allowed: list[str] | None = None) -> Path:
    allowed = allowed or ["work.txt"]
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    for path in allowed:
        (repo / path).write_text("baseline\n")
    (repo / ".gitignore").write_text(".claude/state/\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
    acquire = ["acquire", "--slice-id", "s", "--artifact-contract", "verify implementation", "--session-id", "sess", "--run-id", "run1"]
    for path in allowed: acquire += ["--allowed-path", path]
    assert _run(LEDGER, repo, *acquire)[0] == 0
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


def _retry(repo: Path, evidence: Path, attempt_id: str = "repair-2") -> tuple[int, dict]:
    return _run(CLOSE, repo, "verify", "--run-id", "run1", "--session-id", "sess", "--revision", "3", "--evidence-file", str(evidence), "--attempt-id", attempt_id, "--attempt-number", "2", "--repair-reason", "bounded command-only repair", "--provenance-actor", "codex:test", "--provenance-source", "verified_recon")


def test_fail_to_pass_retry_is_append_only_and_closure_eligible(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    failed_evidence = _evidence(tmp_path, [sys.executable, "-c", "raise SystemExit(7)"])
    code, failed = _verify(repo, failed_evidence)
    failed_path = _verification_path(repo); failed_bytes = failed_path.read_bytes()
    assert code == 0 and failed["result"]["status"] == "fail"
    passing = tmp_path / "passing.json"
    passing.write_text(json.dumps({"schema_version":1,"argv":[sys.executable,"-c","print('repaired')"],"timeout_seconds":10}))
    code, retried = _retry(repo, passing)
    assert code == 0 and retried["result"]["status"] == "pass"
    assert retried["result"]["attempt"]["first_pass"] is False and failed_path.read_bytes() == failed_bytes
    ledger = json.loads((repo/".claude/state/slice_ledger.json").read_text())
    events = [json.loads(line) for line in (repo/".claude/state/slice_events.jsonl").read_text().splitlines()]
    assert ledger["revision"] == 4 and events[-1]["type"] == "verification_retried"
    assert events[-1]["payload"]["previous_verification_sha256"] == hashlib.sha256(json.dumps(failed["result"],sort_keys=True,separators=(",",":")).encode()).hexdigest()
    assert _status(repo,"4")[1]["result"]["receipt"]["attempt"]["first_pass"] is False


def test_retry_duplicate_concurrency_and_manifest_tamper_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path); _verify(repo,_evidence(tmp_path,[sys.executable,"-c","raise SystemExit(1)"]))
    passing = tmp_path/"passing.json"; passing.write_text(json.dumps({"schema_version":1,"argv":[sys.executable,"-c","print('ok')"],"timeout_seconds":10}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _retry(repo,passing), range(2)))
    assert sorted(code for code,_ in results) == [0,3]
    manifest = _verification_path(repo).parent/"slice_verification_attempts.jsonl"
    assert len(manifest.read_text().splitlines()) == 1
    item=json.loads(manifest.read_text()); item["attempt_id"]="tampered"; manifest.write_text(json.dumps(item)+"\n"); os.chmod(manifest,0o600)
    assert _status(repo,"4")[0] == 0
    # A later retry must authenticate history before it can append.
    assert _run(CLOSE,repo,"verify","--run-id","run1","--session-id","sess","--revision","4","--evidence-file",str(passing),"--attempt-id","repair-3","--attempt-number","3","--repair-reason","next","--provenance-actor","codex:test","--provenance-source","verified_recon")[0] in {3,4}


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


def _prepare_candidate(tmp_path: Path, *, offscope: bool = False) -> tuple[Path, dict]:
    repo = _repo(tmp_path)
    (repo / "work.txt").write_text("implemented\n")
    if offscope:
        (repo / "outside.txt").write_text("finding\n")
    _, verified = _verify(repo, _evidence(tmp_path, [sys.executable, "-c", "print('ok')"]))
    return repo, verified["result"]


def _closure_args(receipt: dict, disposition: str, delivered: set[str] | None = None) -> list[str]:
    delivered = delivered or set()
    args = ["close", "--run-id", "run1", "--session-id", "sess", "--revision", "3", "--disposition", disposition]
    paths = {item["path"] for item in receipt["attribution"]["classifications"]}
    for path in sorted(delivered):
        args += ["--delivered-path", path]
    for path in sorted(paths - delivered):
        args += ["--exclude", f"{path}=not delivered"]
    return args


def test_delivered_uncommitted_requires_fresh_pass_and_exhaustive_exclusions(tmp_path: Path) -> None:
    repo, receipt = _prepare_candidate(tmp_path)
    candidates = {item["path"] for item in receipt["attribution"]["classifications"] if item["classification"] == "candidate-owned"}
    code, value = _run(CLOSE, repo, *_closure_args(receipt, "delivered_uncommitted", candidates))
    assert code == 0 and value["result"]["disposition"] == "delivered_uncommitted"
    assert value["result"]["paths"]["delivered"] == sorted(candidates)
    ledger = json.loads((repo / ".claude/state/slice_ledger.json").read_text())
    assert ledger["state"] == "closed" and ledger["terminal_disposition"] == "delivered_uncommitted"
    assert ledger["closure"]["commit_class"] is None
    assert _run(CLOSE, repo, "close", "--run-id", "run1", "--session-id", "sess", "--revision", "4", "--disposition", "blocked", "--blocked-category", "other", "--blocked-reason", "x", "--next-safe-action", "y")[0] == 3


def test_closed_retry_requires_exact_guarded_canonical_request(tmp_path: Path) -> None:
    repo, receipt = _prepare_candidate(tmp_path)
    candidates = {item["path"] for item in receipt["attribution"]["classifications"] if item["classification"] == "candidate-owned"}
    initial = _closure_args(receipt, "delivered_uncommitted", candidates)
    assert _run(CLOSE, repo, *initial)[0] == 0
    retry = ["4" if value == "3" and initial[index - 1] == "--revision" else value for index, value in enumerate(initial)]
    assert _run(CLOSE, repo, *retry)[0] == 0
    wrong_session = ["other" if value == "sess" else value for value in retry]
    assert _run(CLOSE, repo, *wrong_session)[0] == 3
    wrong_revision = ["5" if value == "4" and retry[index - 1] == "--revision" else value for index, value in enumerate(retry)]
    assert _run(CLOSE, repo, *wrong_revision)[0] == 3
    conflicting = [*retry, "--commit-oid", "0" * 40]
    assert _run(CLOSE, repo, *conflicting)[0] == 3


def test_stale_verification_and_incomplete_exclusions_refuse_delivery(tmp_path: Path) -> None:
    repo, receipt = _prepare_candidate(tmp_path)
    candidates = {item["path"] for item in receipt["attribution"]["classifications"] if item["classification"] == "candidate-owned"}
    assert _run(CLOSE, repo, "close", "--run-id", "run1", "--session-id", "sess", "--revision", "3", "--disposition", "delivered_uncommitted", "--delivered-path", next(iter(candidates)))[0] == 3
    (repo / "work.txt").write_text("stale\n")
    assert _run(CLOSE, repo, *_closure_args(receipt, "delivered_uncommitted", candidates))[0] == 3


def test_quarantine_routes_offscope_backlog_and_detects_tamper(tmp_path: Path) -> None:
    repo, receipt = _prepare_candidate(tmp_path, offscope=True)
    candidates = {item["path"] for item in receipt["attribution"]["classifications"] if item["classification"] == "candidate-owned"}
    args = _closure_args(receipt, "quarantined", candidates)
    code, value = _run(CLOSE, repo, *args)
    assert code == 0 and "outside.txt" in value["result"]["paths"]["off-scope"]
    ledger = json.loads((repo / ".claude/state/slice_ledger.json").read_text())
    backlog = repo / ledger["closure"]["backlog_path"]
    lines = backlog.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["path"] == "outside.txt"
    retry = ["4" if value == "3" and args[index - 1] == "--revision" else value for index, value in enumerate(args)]
    assert _run(CLOSE, repo, *retry)[0] == 0 and len(backlog.read_text().splitlines()) == 1
    item = json.loads(lines[0]); item["reason"] = "tampered"
    backlog.write_text(json.dumps(item) + "\n")
    assert _run(CLOSE, repo, *retry)[0] == 4


def test_blocked_is_typed_non_success_and_new_run_links_closed_hash(tmp_path: Path) -> None:
    repo, receipt = _prepare_candidate(tmp_path)
    args = ["close", "--run-id", "run1", "--session-id", "sess", "--revision", "3", "--disposition", "blocked", "--blocked-category", "external_blocker", "--blocked-reason", "dependency unavailable", "--next-safe-action", "retry after dependency"]
    code, value = _run(CLOSE, repo, *args)
    assert code == 0 and value["result"]["claims"]["blocked"]["category"] == "external_blocker"
    terminal = json.loads((repo / ".claude/state/slice_ledger.json").read_text())
    code, next_run = _run(LEDGER, repo, "new-run", "--previous-run-id", "run1", "--previous-revision", "4", "--run-id", "run2", "--session-id", "sess2", "--slice-id", "s2", "--artifact-contract", "next", "--allowed-path", "work.txt")
    assert code == 0 and next_run["ledger"]["run_id"] == "run2"
    event = json.loads((repo / ".claude/state/slice_events.jsonl").read_text().splitlines()[-1])
    assert event["payload"]["previous_terminal_hash"] == terminal["last_event_hash"]


def test_committed_proves_direct_parent_exact_paths_blobs_and_class(tmp_path: Path) -> None:
    repo, receipt = _prepare_candidate(tmp_path)
    candidates = {item["path"] for item in receipt["attribution"]["classifications"] if item["classification"] == "candidate-owned"}
    subprocess.run(["git", "-C", str(repo), "add", *sorted(candidates)], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "implement"], check=True)
    oid = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    args = _closure_args(receipt, "committed", candidates) + ["--commit-oid", oid]
    code, value = _run(CLOSE, repo, *args)
    assert code == 0 and value["result"]["facts"]["commit_class"] == "implementation"
    assert json.loads((repo / ".claude/state/slice_ledger.json").read_text())["closure"]["commit_oid"] == oid


def test_committed_rejects_short_oid(tmp_path: Path) -> None:
    repo, receipt = _prepare_candidate(tmp_path / "short")
    candidates = {item["path"] for item in receipt["attribution"]["classifications"] if item["classification"] == "candidate-owned"}
    assert _run(CLOSE, repo, *(_closure_args(receipt, "committed", candidates) + ["--commit-oid", "abc123"]))[0] == 2


def test_committed_rejects_empty_blob_resurrection_of_verified_deletion(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "work.txt").unlink()
    _, verified = _verify(repo, _evidence(tmp_path, [sys.executable, "-c", "print('ok')"]))
    receipt = verified["result"]
    candidates = {item["path"] for item in receipt["attribution"]["classifications"] if item["classification"] == "candidate-owned"}
    (repo / "work.txt").write_bytes(b"")
    subprocess.run(["git", "-C", str(repo), "add", "work.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "resurrect empty"], check=True)
    oid = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    assert _run(CLOSE, repo, *(_closure_args(receipt, "committed", candidates) + ["--commit-oid", oid]))[0] == 3


@pytest.mark.parametrize("delta", ["dirty_candidate", "untracked_offscope", "staged_allowed"])
def test_committed_rejects_every_post_verification_delta(tmp_path: Path, delta: str) -> None:
    repo = _repo(tmp_path, ["work.txt", "extra.txt"])
    (repo / "work.txt").write_text("implemented\n")
    _, verified = _verify(repo, _evidence(tmp_path, [sys.executable, "-c", "print('ok')"]))
    receipt = verified["result"]
    candidates = {item["path"] for item in receipt["attribution"]["classifications"] if item["classification"] == "candidate-owned"}
    subprocess.run(["git", "-C", str(repo), "add", "work.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "implement"], check=True)
    oid = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if delta == "dirty_candidate":
        (repo / "work.txt").write_text("dirty after commit\n")
    elif delta == "untracked_offscope":
        (repo / "impl.py").write_text("print('delta')\n")
    else:
        (repo / "extra.txt").write_text("staged delta\n")
        subprocess.run(["git", "-C", str(repo), "add", "extra.txt"], check=True)
    assert _run(CLOSE, repo, *(_closure_args(receipt, "committed", candidates) + ["--commit-oid", oid]))[0] == 3


def test_docs_only_commit_cannot_satisfy_implementation_contract(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "docs", ["work.txt", "guide.md"])
    (repo / "guide.md").write_text("docs only\n")
    evidence = _evidence(tmp_path / "docs", [sys.executable, "-c", "print('ok')"])
    _, verified = _verify(repo, evidence)
    receipt2 = verified["result"]
    candidates2 = {item["path"] for item in receipt2["attribution"]["classifications"] if item["classification"] == "candidate-owned"}
    subprocess.run(["git", "-C", str(repo), "add", "guide.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "docs only"], check=True)
    oid = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    args = ["close", "--run-id", "run1", "--session-id", "sess", "--revision", "3", "--disposition", "committed", "--commit-oid", oid]
    for path in sorted(candidates2): args += ["--delivered-path", path]
    for item in receipt2["attribution"]["classifications"]:
        if item["path"] not in candidates2: args += ["--exclude", f"{item['path']}=foreign"]
    assert _run(CLOSE, repo, *args)[0] == 3
