"""Black-box contract tests for the project-local slice ledger CLI."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
SCRIPT = Path(__file__).parents[1] / "templates" / "scripts" / "slice_ledger.py"
UPDATE_PROVENANCE = ("--reason", "verified scope change", "--provenance-actor", "test-suite", "--provenance-source", "verified_recon", "--provenance-evidence-sha256", "a" * 64)
OLD_PATH_ARGS = ("--allowed-path", "tests/test_slice_ledger.py", "--allowed-path", "templates/scripts/slice_ledger.py")
def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root
def _run(root: Path, *args: str) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--cwd", str(root), *args],
        text=True, capture_output=True, check=False,
    )
    output = result.stdout if result.returncode == 0 else result.stderr
    assert output, result.stderr
    return result.returncode, json.loads(output)
def _acquire(root: Path, session: str = "s1", run: str = "run-1") -> tuple[int, dict]:
    return _run(
        root, "acquire", "--slice-id", "slice-1", "--artifact-contract", "implement ledger",
        "--allowed-path", "tests/test_slice_ledger.py", "--allowed-path", "templates/scripts/slice_ledger.py",
        "--session-id", session, "--run-id", run,
    )
def _worker(root: str, session: str) -> tuple[int, dict]:
    return _acquire(Path(root), session=session)
def _attempt_racer(values: tuple[str, str]) -> tuple[int, dict]:
    root, attempt = values
    return _run(Path(root), "attempt-start", "--run-id", "run-1", "--session-id", "s1", "--attempt-id", attempt, "--role", "worker", "--brief", "concurrent brief", "--parent-id", "parent", "--task-id", "task")
def _paths(root: Path) -> tuple[Path, Path, Path]:
    state = root / ".claude" / "state"
    return state / "slice_ledger.json", state / "slice_events.jsonl", state / "slice_ledger.lock"


def test_acquire_status_idempotency_schema_permissions_and_release(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    code, first = _acquire(root)
    assert code == 0
    ledger = first["ledger"]
    assert ledger["schema_version"] == 1
    assert ledger["revision"] == 1 and ledger["state"] == "active"
    assert ledger["terminal_disposition"] is None
    assert ledger["baseline_sha256"] is None
    assert ledger["allowed_paths"] == sorted(ledger["allowed_paths"])
    assert set(ledger["owner"]) == {"session_id", "pid", "hostname", "process_start"}

    code, second = _acquire(root)
    assert code == 0 and second["ledger"] == ledger
    assert _acquire(root, run="wrong-run")[0] == 3
    ledger_path, events_path, lock_path = _paths(root)
    assert len(events_path.read_text().splitlines()) == 1
    for path in (ledger_path, events_path, lock_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    code, status_value = _run(root, "status", "--run-id", "run-1")
    assert code == 0 and status_value["ledger"] == ledger
    code, released = _run(root, "release", "--run-id", "run-1", "--session-id", "s1", "--revision", "1")
    assert code == 0 and released["ledger"]["state"] == "released"
    assert released["ledger"]["revision"] == 2
    assert lock_path.exists()
    code, failure = _run(root, "release", "--run-id", "run-1", "--session-id", "s1", "--revision", "2")
    assert code == 3 and "immutable" in failure["error"]
    assert _acquire(root)[0] == 3


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../x", ".git/config", ".claude/state/x", "a\\b", "a//b", "."])
def test_allowed_paths_fail_closed(tmp_path: Path, bad: str) -> None:
    root = _repo(tmp_path)
    code, value = _run(root, "acquire", "--slice-id", "x", "--artifact-contract", "a", "--allowed-path", bad, "--session-id", "s")
    assert code == 2 and not value["ok"]
    assert not (root / ".claude" / "state" / "slice_events.jsonl").exists()


def test_non_git_and_symlink_state_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    assert _acquire(outside)[0] == 5
    root = _repo(tmp_path / "nested")
    target = tmp_path / "escape"
    target.mkdir()
    (root / ".claude").mkdir()
    (root / ".claude" / "state").symlink_to(target, target_is_directory=True)
    code, value = _acquire(root)
    assert code == 4 and "symlink" in value["error"]
    assert not any(target.iterdir())


def test_concurrent_collision_has_one_owner_and_no_overwrite(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    with ProcessPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_worker, [str(root), str(root)], ["alpha", "beta"]))
    assert sorted(code for code, _ in results) == [0, 3]
    ledger = json.loads(_paths(root)[0].read_text())
    assert ledger["owner"]["session_id"] in {"alpha", "beta"}
    assert len(_paths(root)[1].read_text().splitlines()) == 1


def test_optimistic_guards_and_live_owner_prevent_takeover(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _acquire(root)
    assert _run(root, "release", "--run-id", "wrong", "--session-id", "s1", "--revision", "1")[0] == 3
    assert _run(root, "release", "--run-id", "run-1", "--session-id", "wrong", "--revision", "1")[0] == 3
    assert _run(root, "release", "--run-id", "run-1", "--session-id", "s1", "--revision", "9")[0] == 3
    code, value = _run(root, "recover", "--run-id", "run-1", "--revision", "1", "--session-id", "s2", "--reason", "test")
    assert code == 3 and "not demonstrably stale" in value["error"]


def test_explicit_stale_recovery_records_provenance(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _acquire(root)
    ledger_path, events_path, _ = _paths(root)
    ledger = json.loads(ledger_path.read_text())
    ledger["owner"]["pid"] = 999_999_999
    # Construct a valid acquired history fixture with the dead owner.
    event = json.loads(events_path.read_text())
    event["payload"]["owner"] = ledger["owner"]
    unsigned = {key: value for key, value in event.items() if key != "hash"}
    event["hash"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    ledger["last_event_hash"] = event["hash"]
    events_path.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    ledger_path.write_text(json.dumps(ledger, sort_keys=True, separators=(",", ":")) + "\n")

    code, value = _run(root, "recover", "--run-id", "run-1", "--revision", "1", "--session-id", "s2", "--reason", "owner crashed")
    assert code == 0
    assert value["ledger"]["revision"] == 2 and value["ledger"]["owner"]["session_id"] == "s2"
    recovery = json.loads(events_path.read_text().splitlines()[-1])
    assert recovery["type"] == "recovered"
    assert recovery["payload"]["reason"] == "owner crashed"
    assert recovery["payload"]["previous_owner"]["pid"] == 999_999_999


def test_event_corruption_truncation_and_projection_mismatch_fail_closed(tmp_path: Path) -> None:
    for mutation in ("hash", "truncate", "projection"):
        root = _repo(tmp_path / mutation)
        _acquire(root)
        ledger_path, events_path, _ = _paths(root)
        if mutation == "hash":
            event = json.loads(events_path.read_text())
            event["hash"] = "f" * 64
            events_path.write_text(json.dumps(event) + "\n")
        elif mutation == "truncate":
            events_path.write_bytes(events_path.read_bytes()[:-1])
        else:
            ledger = json.loads(ledger_path.read_text())
            ledger["slice_id"] = "tampered"
            ledger_path.write_text(json.dumps(ledger) + "\n")
        code, value = _run(root, "status")
        assert code == 4 and not value["ok"]


def test_valid_log_one_event_ahead_replays_projection(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _acquire(root)
    ledger_path, events_path, _ = _paths(root)
    original = ledger_path.read_bytes()
    code, released = _run(root, "release", "--run-id", "run-1", "--session-id", "s1", "--revision", "1")
    assert code == 0
    ledger_path.write_bytes(original)  # injected crash: append+fsync happened, replace did not
    code, replayed = _run(root, "status")
    assert code == 0 and replayed["ledger"] == released["ledger"]
    assert json.loads(ledger_path.read_text()) == released["ledger"]
    assert len(events_path.read_text().splitlines()) == 2


def test_projection_missing_with_multi_event_history_is_not_silently_rebuilt(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _acquire(root)
    _run(root, "release", "--run-id", "run-1", "--session-id", "s1", "--revision", "1")
    _paths(root)[0].unlink()
    assert _run(root, "status")[0] == 4


def test_permissive_state_file_and_extra_schema_key_fail_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _acquire(root)
    ledger_path, events_path, _ = _paths(root)
    os.chmod(events_path, 0o644)
    assert _run(root, "status")[0] == 4
    os.chmod(events_path, 0o600)
    ledger = json.loads(ledger_path.read_text())
    ledger["unexpected"] = True
    ledger_path.write_text(json.dumps(ledger) + "\n")
    assert _run(root, "status")[0] == 4


@pytest.mark.parametrize("name", ["slice_ledger.json", "slice_events.jsonl", "slice_ledger.lock"])
def test_hardlinked_state_files_fail_before_external_inode_mutation(tmp_path: Path, name: str) -> None:
    root = _repo(tmp_path)
    _acquire(root)
    state_file = root / ".claude" / "state" / name
    external = tmp_path / f"external-{name}"
    os.link(state_file, external)
    before = external.read_bytes()
    code, value = _run(root, "status")
    assert code == 4 and "unlinked" in value["error"]
    assert external.read_bytes() == before


def test_boolean_schema_versions_fail_closed_in_projection_and_event(tmp_path: Path) -> None:
    root = _repo(tmp_path / "projection")
    _acquire(root)
    ledger_path, _, _ = _paths(root)
    ledger = json.loads(ledger_path.read_text())
    ledger["schema_version"] = True
    ledger_path.write_text(json.dumps(ledger) + "\n")
    assert _run(root, "status")[0] == 4

    root = _repo(tmp_path / "event")
    _acquire(root)
    _, events_path, _ = _paths(root)
    event = json.loads(events_path.read_text())
    event["schema_version"] = True
    unsigned = {key: value for key, value in event.items() if key != "hash"}
    event["hash"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    events_path.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    assert _run(root, "status")[0] == 4

    root = _repo(tmp_path / "payload")
    _acquire(root)
    _, events_path, _ = _paths(root)
    event = json.loads(events_path.read_text())
    event["payload"]["schema_version"] = True
    unsigned = {key: value for key, value in event.items() if key != "hash"}
    event["hash"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    events_path.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    assert _run(root, "status")[0] == 4


def test_usage_failures_are_typed_json_on_stderr(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--cwd", str(root), "release", "--revision", "not-an-int"],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2 and result.stdout == ""
    value = json.loads(result.stderr)
    assert value["ok"] is False and value["code"] == 2 and value["type"] == "error"


def test_existing_claude_directory_mode_is_not_mutated(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    claude = root / ".claude"
    claude.mkdir(mode=0o755)
    os.chmod(claude, 0o755)
    assert _acquire(root)[0] == 0
    assert stat.S_IMODE(claude.stat().st_mode) == 0o755


def test_update_replaces_full_contract_refreshes_owner_and_is_idempotent(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _, acquired = _acquire(root)
    old_owner = acquired["ledger"]["owner"]
    command = (
        "update", "--run-id", "run-1", "--session-id", "s1", "--revision", "1",
        "--artifact-contract", "replacement contract", *OLD_PATH_ARGS, "--allowed-path", "z.py", "--allowed-path", "a.py",
        *UPDATE_PROVENANCE,
    )
    code, updated = _run(root, *command)
    assert code == 0
    ledger = updated["ledger"]
    assert ledger["revision"] == 2
    assert ledger["artifact_contract"] == "replacement contract"
    assert ledger["allowed_paths"] == sorted(["a.py", "z.py", "tests/test_slice_ledger.py", "templates/scripts/slice_ledger.py"])
    assert ledger["owner"]["session_id"] == old_owner["session_id"]
    assert len(_paths(root)[1].read_text().splitlines()) == 2
    code, repeated = _run(root, *command)
    assert code == 0 and repeated["ledger"] == ledger
    assert len(_paths(root)[1].read_text().splitlines()) == 2
    assert _run(root, "update", "--run-id", "run-1", "--session-id", "s1", "--revision", "9", "--artifact-contract", "v3", "--allowed-path", "v3.py", *UPDATE_PROVENANCE)[0] == 3
    assert _run(root, "update", "--run-id", "wrong", "--session-id", "s1", "--revision", "2", "--artifact-contract", "v3", "--allowed-path", "v3.py", *UPDATE_PROVENANCE)[0] == 3
    assert _run(root, "update", "--run-id", "run-1", "--session-id", "other", "--revision", "2", "--artifact-contract", "v3", "--allowed-path", "v3.py", *UPDATE_PROVENANCE)[0] == 3


def test_update_guards_terminal_immutability_and_crash_replay(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _acquire(root)
    ledger_path, events_path, _ = _paths(root)
    original = ledger_path.read_bytes()
    args = ("update", "--run-id", "run-1", "--session-id", "s1", "--revision", "1", "--artifact-contract", "v2", *OLD_PATH_ARGS, "--allowed-path", "v2.py", *UPDATE_PROVENANCE)
    code, updated = _run(root, *args)
    assert code == 0
    ledger_path.write_bytes(original)
    assert _run(root, "status")[1]["ledger"] == updated["ledger"]
    assert len(events_path.read_text().splitlines()) == 2
    assert _run(root, "release", "--run-id", "run-1", "--session-id", "s1", "--revision", "2")[0] == 0
    assert _run(root, "update", "--run-id", "run-1", "--session-id", "s1", "--revision", "3", "--artifact-contract", "v3", "--allowed-path", "x.py", *UPDATE_PROVENANCE)[0] == 3


def test_contract_expansion_requires_reason_provenance_and_retains_both_states(tmp_path: Path) -> None:
    root = _repo(tmp_path); _acquire(root)
    base = ("update", "--run-id", "run-1", "--session-id", "s1", "--revision", "1", "--artifact-contract", "expanded", *OLD_PATH_ARGS, "--allowed-path", "new.py")
    before = tuple(path.read_bytes() for path in _paths(root)[:2])
    assert _run(root, *base)[0] == 2
    assert tuple(path.read_bytes() for path in _paths(root)[:2]) == before
    assert _run(root, *base, "--reason", "why", "--provenance-actor", "a", "--provenance-source", "poisoned", "--provenance-evidence-sha256", "a" * 64)[0] == 2
    assert tuple(path.read_bytes() for path in _paths(root)[:2]) == before
    assert _run(root, "update", "--run-id", "run-1", "--session-id", "s1", "--revision", "1", "--artifact-contract", "not expansion", "--allowed-path", "new.py", *UPDATE_PROVENANCE)[0] == 2
    assert tuple(path.read_bytes() for path in _paths(root)[:2]) == before
    assert _run(root, *base, "--reason", " ", "--provenance-actor", "a", "--provenance-source", "verified_recon", "--provenance-evidence-sha256", "a" * 64)[0] == 2
    assert _run(root, *base, "--reason", "why", "--provenance-actor", "a", "--provenance-source", "verified_recon", "--provenance-evidence-sha256", "bad")[0] == 2
    code, value = _run(root, *base, *UPDATE_PROVENANCE)
    assert code == 0 and value["ledger"]["revision"] == 2
    event = json.loads(_paths(root)[1].read_text().splitlines()[-1])
    assert event["type"] == "contract_expanded"
    assert event["payload"]["previous_contract"] == "implement ledger" and event["payload"]["artifact_contract"] == "expanded"
    assert event["payload"]["previous_paths"] != event["payload"]["allowed_paths"]
    assert event["payload"]["reason"] == "verified scope change" and event["payload"]["provenance"]["source"] == "verified_recon"


def test_contract_expansion_provenance_tamper_is_rejected_even_when_rehashed(tmp_path: Path) -> None:
    root = _repo(tmp_path); _acquire(root)
    _run(root, "update", "--run-id", "run-1", "--session-id", "s1", "--revision", "1", "--artifact-contract", "expanded", *OLD_PATH_ARGS, "--allowed-path", "new.py", *UPDATE_PROVENANCE)
    ledger_path, events_path, _ = _paths(root); rows = [json.loads(line) for line in events_path.read_text().splitlines()]
    rows[-1]["payload"]["provenance"]["source"] = "invented"
    unsigned = {key:value for key,value in rows[-1].items() if key != "hash"}; rows[-1]["hash"] = hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    events_path.write_text("".join(json.dumps(row,sort_keys=True,separators=(",",":")) + "\n" for row in rows)); ledger = json.loads(ledger_path.read_text()); ledger["last_event_hash"] = rows[-1]["hash"]; ledger_path.write_text(json.dumps(ledger,sort_keys=True,separators=(",",":")) + "\n")
    assert _run(root, "status")[0] == 4


def test_legacy_updated_event_replays_for_existing_installations(tmp_path: Path) -> None:
    root = _repo(tmp_path); _, acquired = _acquire(root); ledger_path, events_path, _ = _paths(root)
    first = json.loads(events_path.read_text()); payload = {"run_id":"run-1","revision":2,"updated_at":"2026-01-01T00:00:00Z","artifact_contract":"legacy expanded","allowed_paths":["legacy.py"],"owner":acquired["ledger"]["owner"]}
    unsigned = {"schema_version":1,"sequence":2,"timestamp":"2026-01-01T00:00:00Z","type":"updated","payload":payload,"previous_hash":first["hash"]}
    event = {**unsigned,"hash":hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()}
    with events_path.open("a") as stream: stream.write(json.dumps(event,sort_keys=True,separators=(",",":")) + "\n")
    ledger = acquired["ledger"]; ledger.update(artifact_contract="legacy expanded",allowed_paths=["legacy.py"],revision=2,updated_at=payload["updated_at"],last_event_hash=event["hash"]); ledger_path.write_text(json.dumps(ledger,sort_keys=True,separators=(",",":")) + "\n")
    code, status = _run(root, "status")
    assert code == 0 and status["ledger"]["artifact_contract"] == "legacy expanded"


def _attempt_start(root: Path, attempt: str, brief: str = "same   normalized brief", *retry: str) -> tuple[int, dict]:
    return _run(root, "attempt-start", "--run-id", "run-1", "--session-id", "s1", "--attempt-id", attempt, "--role", "worker", "--brief", brief, "--parent-id", "parent", "--task-id", "task", *retry)


def _attempt_delta(root: Path, command: str, attempt: str, digest: str, *extra: str) -> tuple[int, dict]:
    return _run(root, command, "--run-id", "run-1", "--session-id", "s1", "--attempt-id", attempt, "--evidence-delta-sha256", digest, *extra)


def test_worker_attempt_lifecycle_is_append_only_typed_and_observation_gated(tmp_path: Path) -> None:
    root = _repo(tmp_path); _acquire(root)
    assert _attempt_start(root, "a1")[0] == 0
    stable = tuple(path.read_bytes() for path in _paths(root)[:2])
    assert _attempt_start(root, "a1")[0] == 3
    assert tuple(path.read_bytes() for path in _paths(root)[:2]) == stable
    assert _attempt_delta(root, "attempt-complete", "a1", "b" * 64)[0] == 3
    assert _run(root, "attempt-observe", "--run-id", "wrong", "--session-id", "s1", "--attempt-id", "a1", "--evidence-delta-sha256", "b" * 64)[0] == 3
    assert _run(root, "attempt-observe", "--run-id", "run-1", "--session-id", "other", "--attempt-id", "a1", "--evidence-delta-sha256", "b" * 64)[0] == 3
    assert _attempt_delta(root, "attempt-observe", "a1", "b" * 64)[0] == 0
    assert _attempt_delta(root, "attempt-observe", "a1", "c" * 64)[0] == 3
    assert _attempt_delta(root, "attempt-complete", "a1", "b" * 64)[0] == 3
    assert _attempt_delta(root, "attempt-complete", "a1", "c" * 64)[0] == 0
    assert _attempt_delta(root, "attempt-fail", "a1", "d" * 64, "--failure-reason", "late failure")[0] == 3
    events = [json.loads(line) for line in _paths(root)[1].read_text().splitlines()]
    assert [event["type"] for event in events[-3:]] == ["worker_attempt_started", "worker_attempt_observed", "worker_attempt_completed"]
    assert events[-1]["payload"]["brief_sha256"] == hashlib.sha256(b"same normalized brief").hexdigest()
    assert _run(root, "status")[1]["ledger"]["revision"] == 1


def test_duplicate_brief_retry_requires_new_failed_provenance(tmp_path: Path) -> None:
    root = _repo(tmp_path); _acquire(root)
    assert _attempt_start(root, "a1")[0] == 0
    assert _attempt_delta(root, "attempt-fail", "a1", "d" * 64, "--failure-reason", "provider timeout")[0] == 0
    assert _attempt_start(root, "a2")[0] == 3
    assert _attempt_start(root, "a2", "same normalized brief", "--retry-of", "", "--retry-number", "1", "--retry-evidence-sha256", "e" * 64, "--retry-failure-reason", "fresh")[0] == 3
    assert _attempt_start(root, "a2", "same normalized brief", "--retry-of", "a1", "--retry-number", "1", "--retry-evidence-sha256", "e" * 64, "--retry-failure-reason", " ")[0] == 3
    same = ("--retry-of", "a1", "--retry-number", "1", "--retry-evidence-sha256", "d" * 64, "--retry-failure-reason", "provider timeout")
    assert _attempt_start(root, "a2", "same normalized brief", *same)[0] == 3
    unchanged_evidence = ("--retry-of", "a1", "--retry-number", "1", "--retry-evidence-sha256", "d" * 64, "--retry-failure-reason", "fresh reason")
    unchanged_reason = ("--retry-of", "a1", "--retry-number", "1", "--retry-evidence-sha256", "e" * 64, "--retry-failure-reason", "  PROVIDER   TIMEOUT ")
    stable = tuple(path.read_bytes() for path in _paths(root)[:2])
    assert _attempt_start(root, "a2", "same normalized brief", *unchanged_evidence)[0] == 3 and _attempt_start(root, "a2", "same normalized brief", *unchanged_reason)[0] == 3
    assert tuple(path.read_bytes() for path in _paths(root)[:2]) == stable
    new = ("--retry-of", "a1", "--retry-number", "1", "--retry-evidence-sha256", "e" * 64, "--retry-failure-reason", "provider timeout with fresh trace")
    assert _attempt_start(root, "a2", "same normalized brief", *new)[0] == 0
    assert _attempt_delta(root, "attempt-fail", "a2", "f" * 64, "--failure-reason", "second failure")[0] == 0
    stale_branch = ("--retry-of", "a1", "--retry-number", "2", "--retry-evidence-sha256", "9" * 64, "--retry-failure-reason", "third failure")
    assert _attempt_start(root, "a3", "same normalized brief", *stale_branch)[0] == 3


def test_concurrent_same_brief_allows_only_one_nonterminal_attempt(tmp_path: Path) -> None:
    root = _repo(tmp_path); _acquire(root)
    with ProcessPoolExecutor(max_workers=2) as pool: results = list(pool.map(_attempt_racer, [(str(root), "a1"), (str(root), "a2")]))
    assert sorted(code for code, _ in results) == [0, 3]
    attempts = [json.loads(line) for line in _paths(root)[1].read_text().splitlines() if "worker_attempt_started" in line]
    assert len(attempts) == 1 and _run(root, "status")[0] == 0


def test_failed_transition_also_requires_new_evidence_delta(tmp_path: Path) -> None:
    root = _repo(tmp_path); _acquire(root); _attempt_start(root, "a1", "unique brief"); _attempt_delta(root, "attempt-observe", "a1", "b" * 64)
    assert _attempt_delta(root, "attempt-fail", "a1", "b" * 64, "--failure-reason", "failed")[0] == 3
    assert _attempt_delta(root, "attempt-fail", "a1", "c" * 64, "--failure-reason", "failed")[0] == 0


def test_attempt_ids_and_briefs_are_reusable_after_new_run(tmp_path: Path) -> None:
    root = _repo(tmp_path); _acquire(root); assert _attempt_start(root, "a1")[0] == 0
    assert _run(root, "release", "--run-id", "run-1", "--session-id", "s1", "--revision", "1")[0] == 0
    assert _run(root, "new-run", "--previous-run-id", "run-1", "--previous-revision", "2", "--run-id", "run-2", "--session-id", "s2", "--slice-id", "next", "--artifact-contract", "next", "--allowed-path", "next.py")[0] == 0
    code, _ = _run(root, "attempt-start", "--run-id", "run-2", "--session-id", "s2", "--attempt-id", "a1", "--role", "worker", "--brief", "same normalized brief", "--parent-id", "parent", "--task-id", "task")
    assert code == 0 and _run(root, "status")[0] == 0


def test_rehashed_retry_number_tamper_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path); _acquire(root); _attempt_start(root, "a1"); _attempt_delta(root, "attempt-fail", "a1", "d" * 64, "--failure-reason", "timeout")
    _attempt_start(root, "a2", "same normalized brief", "--retry-of", "a1", "--retry-number", "1", "--retry-evidence-sha256", "e" * 64, "--retry-failure-reason", "fresh timeout")
    ledger_path, events_path, _ = _paths(root); rows = [json.loads(line) for line in events_path.read_text().splitlines()]; rows[-1]["payload"]["retry_number"] = 9
    unsigned = {key:value for key,value in rows[-1].items() if key != "hash"}; rows[-1]["hash"] = hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest(); events_path.write_text("".join(json.dumps(row,sort_keys=True,separators=(",",":")) + "\n" for row in rows)); ledger = json.loads(ledger_path.read_text()); ledger["last_event_hash"] = rows[-1]["hash"]; ledger_path.write_text(json.dumps(ledger,sort_keys=True,separators=(",",":")) + "\n")
    assert _run(root, "status")[0] == 4


def test_attempt_event_tamper_and_single_event_crash_replay_fail_safe(tmp_path: Path) -> None:
    root = _repo(tmp_path / "replay"); _, acquired = _acquire(root); ledger_path, events_path, _ = _paths(root); original = ledger_path.read_bytes()
    assert _attempt_start(root, "a1", "brief")[0] == 0
    ledger_path.write_bytes(original)
    code, replayed = _run(root, "status")
    assert code == 0 and replayed["ledger"]["last_event_hash"] == json.loads(events_path.read_text().splitlines()[-1])["hash"]
    root = _repo(tmp_path / "tamper"); _acquire(root); _attempt_start(root, "a1", "brief"); ledger_path, events_path, _ = _paths(root)
    rows = events_path.read_text().splitlines(); event = json.loads(rows[-1]); event["payload"]["task_id"] = "forged"; rows[-1] = json.dumps(event, sort_keys=True, separators=(",", ":")); events_path.write_text("\n".join(rows) + "\n")
    assert _run(root, "status")[0] == 4


def test_recovery_preserves_attempt_history_and_allows_later_transition(tmp_path: Path) -> None:
    root = _repo(tmp_path); _acquire(root); _attempt_start(root, "a1", "brief")
    ledger_path, events_path, _ = _paths(root); ledger = json.loads(ledger_path.read_text()); ledger["owner"]["pid"] = 999_999_999
    rows = [json.loads(line) for line in events_path.read_text().splitlines()]; rows[0]["payload"]["owner"] = ledger["owner"]
    previous = "0" * 64
    for row in rows:
        row["previous_hash"] = previous; unsigned = {key:value for key,value in row.items() if key != "hash"}; row["hash"] = hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest(); previous = row["hash"]
    ledger["last_event_hash"] = previous; events_path.write_text("".join(json.dumps(row,sort_keys=True,separators=(",",":")) + "\n" for row in rows)); ledger_path.write_text(json.dumps(ledger,sort_keys=True,separators=(",",":")) + "\n")
    code, recovered = _run(root, "recover", "--run-id", "run-1", "--revision", "1", "--session-id", "s2", "--reason", "owner crashed")
    assert code == 0 and recovered["ledger"]["revision"] == 2
    # Existing attempt identity is immutable, so the new owner cannot rewrite it.
    assert _run(root, "attempt-observe", "--run-id", "run-1", "--session-id", "s2", "--attempt-id", "a1", "--evidence-delta-sha256", "b" * 64)[0] == 3
    assert _run(root, "status")[0] == 0


def test_unverifiable_owner_requires_fingerprinted_explicit_override(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _acquire(root)
    ledger_path, events_path, _ = _paths(root)
    ledger = json.loads(ledger_path.read_text())
    ledger["owner"]["hostname"] = "remote.example.invalid"
    event = json.loads(events_path.read_text())
    event["payload"]["owner"] = ledger["owner"]
    unsigned = {key: value for key, value in event.items() if key != "hash"}
    event["hash"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    ledger["last_event_hash"] = event["hash"]
    events_path.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    ledger_path.write_text(json.dumps(ledger, sort_keys=True, separators=(",", ":")) + "\n")
    base = ("recover", "--run-id", "run-1", "--revision", "1", "--session-id", "s2", "--reason", "remote session abandoned")
    assert _run(root, *base)[0] == 3
    assert _run(root, *base, "--force-unverifiable-owner", "--prior-owner-fingerprint", "0" * 64)[0] == 3
    fingerprint = hashlib.sha256(json.dumps(ledger["owner"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    code, recovered = _run(root, *base, "--force-unverifiable-owner", "--prior-owner-fingerprint", fingerprint)
    assert code == 0 and recovered["ledger"]["owner"]["session_id"] == "s2"
    recovery = json.loads(events_path.read_text().splitlines()[-1])
    assert recovery["payload"]["override_unverifiable"] is True
    assert recovery["payload"]["prior_owner_fingerprint"] == fingerprint
    assert recovery["payload"]["previous_owner"] == ledger["owner"]
    assert recovery["payload"]["reason"] == "remote session abandoned"


def test_bind_subcommands_are_not_public_and_internal_baseline_bind_rejects_fakes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _acquire(root)
    for command in ("bind-baseline", "bind-verification"):
        code, value = _run(root, command)
        assert code == 2 and value["type"] == "error"
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import slice_ledger as module
        from argparse import Namespace
        ledger, events, _ = _paths(root)
        before = events.read_bytes()
        run_hash = hashlib.sha256(b"run-1").hexdigest()
        relative = f".claude/state/runs/{run_hash}/slice_baseline.json"
        args = Namespace(run_id="run-1", session_id="s1", revision=1, baseline_sha256="0" * 64, baseline_path=relative)
        with pytest.raises(module.LedgerError):
            module._bind_baseline(args, ledger, events)
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_text("{}\n")
        os.chmod(path, 0o600)
        with pytest.raises(module.LedgerError):
            module._bind_baseline(args, ledger, events)
        assert events.read_bytes() == before
    finally:
        sys.path.pop(0)
