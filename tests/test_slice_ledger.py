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
        "--artifact-contract", "replacement contract", "--allowed-path", "z.py", "--allowed-path", "a.py",
    )
    code, updated = _run(root, *command)
    assert code == 0
    ledger = updated["ledger"]
    assert ledger["revision"] == 2
    assert ledger["artifact_contract"] == "replacement contract"
    assert ledger["allowed_paths"] == ["a.py", "z.py"]
    assert ledger["owner"]["session_id"] == old_owner["session_id"]
    assert len(_paths(root)[1].read_text().splitlines()) == 2
    code, repeated = _run(root, *command)
    assert code == 0 and repeated["ledger"] == ledger
    assert len(_paths(root)[1].read_text().splitlines()) == 2
    assert _run(root, "update", "--run-id", "run-1", "--session-id", "s1", "--revision", "9", "--artifact-contract", "v3", "--allowed-path", "v3.py")[0] == 3
    assert _run(root, "update", "--run-id", "wrong", "--session-id", "s1", "--revision", "2", "--artifact-contract", "v3", "--allowed-path", "v3.py")[0] == 3
    assert _run(root, "update", "--run-id", "run-1", "--session-id", "other", "--revision", "2", "--artifact-contract", "v3", "--allowed-path", "v3.py")[0] == 3


def test_update_guards_terminal_immutability_and_crash_replay(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _acquire(root)
    ledger_path, events_path, _ = _paths(root)
    original = ledger_path.read_bytes()
    args = ("update", "--run-id", "run-1", "--session-id", "s1", "--revision", "1", "--artifact-contract", "v2", "--allowed-path", "v2.py")
    code, updated = _run(root, *args)
    assert code == 0
    ledger_path.write_bytes(original)
    assert _run(root, "status")[1]["ledger"] == updated["ledger"]
    assert len(events_path.read_text().splitlines()) == 2
    assert _run(root, "release", "--run-id", "run-1", "--session-id", "s1", "--revision", "2")[0] == 0
    assert _run(root, "update", "--run-id", "run-1", "--session-id", "s1", "--revision", "3", "--artifact-contract", "v3", "--allowed-path", "x.py")[0] == 3


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
