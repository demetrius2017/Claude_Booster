"""Black-box and parser acceptance tests for Slice 2 Git fact attribution."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
LEDGER = ROOT / "templates" / "scripts" / "slice_ledger.py"
GIT_CLI = ROOT / "templates" / "scripts" / "slice_git.py"
CORE_PATH = ROOT / "templates" / "scripts" / "slice_git_core.py"
spec = importlib.util.spec_from_file_location("slice_git_core_test", CORE_PATH)
assert spec and spec.loader
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path, allowed: list[str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    for path in allowed:
        if path.startswith(".env") or path == "ignored.txt":
            continue
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"baseline {path}\n")
    (repo / ".gitignore").write_text(".claude/state/\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed")
    _ledger(repo, allowed)
    return repo


def _invoke(script: Path, repo: Path, *args: str) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(script), "--cwd", str(repo), *args], text=True, capture_output=True, check=False)
    output = result.stdout if result.returncode == 0 else result.stderr
    assert output, result
    return result.returncode, json.loads(output)


def _ledger(repo: Path, allowed: list[str]) -> None:
    args = ["acquire", "--slice-id", "slice-2", "--artifact-contract", "git attribution", "--session-id", "s1", "--run-id", "r1"]
    for path in allowed:
        args += ["--allowed-path", path]
    assert _invoke(LEDGER, repo, *args)[0] == 0


def _capture(repo: Path) -> tuple[int, dict]:
    return _invoke(GIT_CLI, repo, "capture", "--run-id", "r1", "--session-id", "s1", "--revision", "1")


def _attribute(repo: Path) -> tuple[int, dict]:
    return _invoke(GIT_CLI, repo, "attribute", "--run-id", "r1", "--session-id", "s1", "--revision", "2")


def _classes(value: dict) -> dict[str, dict]:
    return {item["path"]: item for item in value["result"]["classifications"]}


def test_porcelain_parser_preserves_modes_oids_xy_submodule_and_rename() -> None:
    ordinary = b"1 .M N... 100644 100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" dir/a b.txt\0"
    rename = b"2 R. S.CU 100644 100755 100755 " + b"c" * 40 + b" " + b"d" * 40 + b" R100 new.txt\0old.txt\0"
    unmerged = b"u UU N... 100644 100644 100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" " + b"3" * 40 + b" conflict.txt\0"
    entries = core.parse_porcelain_v2(ordinary + rename + unmerged + b"? new.bin\0! ignored.txt\0")
    assert entries[0]["xy"] == ".M" and entries[0]["mode_worktree"] == "100644"
    assert entries[1]["original_path"] == "old.txt" and entries[1]["score"] == "R100"
    assert entries[1]["sub"] == "S.CU"
    assert entries[2]["kind"] == "u" and entries[2]["oid_stage3"] == "3" * 40
    assert [entry["kind"] for entry in entries[-2:]] == ["?", "!"]


@pytest.mark.parametrize("raw", [b"? bad\xff\0", b"? ../escape\0", b"1 broken\0", b"2 broken\0"])
def test_porcelain_parser_rejects_non_utf8_traversal_and_malformed(raw: bytes) -> None:
    with pytest.raises(core.GitFactError):
        core.parse_porcelain_v2(raw)


def test_parser_rejects_case_and_unicode_collisions() -> None:
    with pytest.raises(core.GitFactError):
        core.parse_porcelain_v2(b"? Name.txt\0? name.txt\0")
    decomposed = "e\u0301.txt".encode()
    with pytest.raises(core.GitFactError):
        core.parse_porcelain_v2(b"? " + decomposed + b"\0")


def test_clean_tracked_and_untracked_changes_are_candidate_not_authorship(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["tracked.txt", "new.txt"])
    code, captured = _capture(repo)
    assert code == 0 and captured["result"]["git"]["anchors"]["head"]
    (repo / "tracked.txt").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    code, value = _attribute(repo)
    classes = _classes(value)
    assert code == 0 and value["result"]["candidate_owned_is_authorship"] is False
    assert classes["tracked.txt"]["classification"] == "candidate-owned"
    assert classes["new.txt"]["classification"] == "candidate-owned"


def test_baseline_dirty_unchanged_is_foreign_then_changed_is_ambiguous(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["tracked.txt", "preexisting.txt"])
    (repo / "tracked.txt").write_text("dirty before capture\n")
    (repo / "preexisting.txt").unlink()
    (repo / "preexisting.txt").write_text("untracked replacement\n")
    _git(repo, "rm", "--cached", "-q", "preexisting.txt")
    assert _capture(repo)[0] == 0
    classes = _classes(_attribute(repo)[1])
    assert classes["tracked.txt"]["classification"] == "foreign"
    assert classes["preexisting.txt"]["classification"] == "foreign"
    (repo / "tracked.txt").write_text("changed again\n")
    (repo / "preexisting.txt").write_text("changed again\n")
    classes = _classes(_attribute(repo)[1])
    assert classes["tracked.txt"]["classification"] == "ambiguous"
    assert classes["preexisting.txt"]["classification"] == "ambiguous"


def test_offscope_and_reserved_state_are_never_owned(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["allowed.txt"])
    _capture(repo)
    (repo / "outside.txt").write_text("outside\n")
    # Remove ignore rule so the adapter observes its own protected receipt.
    (repo / ".gitignore").write_text("")
    classes = _classes(_attribute(repo)[1])
    assert classes["outside.txt"]["classification"] == "off-scope"
    assert classes[".claude"]["classification"] == "foreign"


def test_rename_is_atomic_and_cross_contract_rename_is_ambiguous(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "inside", ["old.txt", "new.txt"])
    _capture(repo)
    (repo / "new.txt").unlink()
    _git(repo, "mv", "old.txt", "new.txt")
    classes = _classes(_attribute(repo)[1])
    # `git mv` changes the index anchor; facts outrank the clean-path claim.
    assert classes["old.txt"]["classification"] == "ambiguous"
    assert classes["new.txt"]["classification"] == "ambiguous"

    repo = _repo(tmp_path / "cross", ["old.txt"])
    _capture(repo)
    _git(repo, "mv", "old.txt", "outside.txt")
    classes = _classes(_attribute(repo)[1])
    assert classes["old.txt"]["classification"] == "ambiguous"
    assert classes["outside.txt"]["classification"] == "off-scope"


def test_head_or_index_change_downgrades_candidate_to_ambiguous(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["tracked.txt"])
    _capture(repo)
    (repo / "tracked.txt").write_text("changed\n")
    _git(repo, "add", "tracked.txt")
    classes = _classes(_attribute(repo)[1])
    assert classes["tracked.txt"]["classification"] == "ambiguous"


def test_ignored_secret_large_symlink_and_fifo_are_ambiguous_without_content(tmp_path: Path) -> None:
    allowed = ["ignored.txt", ".env.secret", "large.bin", "link", "pipe"]
    repo = _repo(tmp_path, allowed)
    with (repo / ".gitignore").open("a") as stream:
        stream.write("ignored.txt\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore")
    _capture(repo)
    (repo / "ignored.txt").write_text("ignored\n")
    (repo / ".env.secret").write_text("do not hash\n")
    with (repo / "large.bin").open("wb") as stream:
        stream.truncate(core.MAX_FILE + 1)
    (repo / "link").unlink()
    (repo / "link").symlink_to(".env.secret")
    (repo / "pipe").unlink()
    os.mkfifo(repo / "pipe")
    classes = _classes(_attribute(repo)[1])
    for path in allowed:
        assert classes[path]["classification"] == "ambiguous"
    current = _attribute(repo)[1]["result"]["current"]["scoped_facts"]
    assert current[".env.secret"]["hash_status"] == "sensitive_skipped"
    assert current["large.bin"]["hash_status"] == "too_large"
    assert current["link"]["hash_status"] == "unsafe_symlink"
    assert current["pipe"]["hash_status"] == "unsupported_kind"


def test_baseline_is_immutable_idempotent_and_bound_to_ledger(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["tracked.txt"])
    code, first = _capture(repo)
    assert code == 0
    path = repo / ".claude" / "state" / "slice_baseline.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _capture(repo)[1]["result"] == first["result"]
    assert _invoke(GIT_CLI, repo, "capture", "--run-id", "wrong", "--session-id", "s1", "--revision", "1")[0] == 3
    ledger = json.loads((repo / ".claude/state/slice_ledger.json").read_text())
    assert first["result"]["ledger_event_hash"] != ledger["last_event_hash"]
    assert ledger["baseline_sha256"] == hashlib.sha256(json.dumps(first["result"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    last_event = json.loads((repo / ".claude/state/slice_events.jsonl").read_text().splitlines()[-1])
    assert last_event["type"] == "baseline_bound" and last_event["payload"]["baseline_sha256"] == ledger["baseline_sha256"]
    assert first["result"]["artifact_contract_sha256"] == hashlib.sha256(b"git attribution").hexdigest()


@pytest.mark.parametrize("attack", ["hardlink", "symlink", "permissive", "truncated"])
def test_receipt_link_permission_and_corruption_attacks_fail_closed(tmp_path: Path, attack: str) -> None:
    repo = _repo(tmp_path, ["tracked.txt"])
    _capture(repo)
    path = repo / ".claude/state/slice_baseline.json"
    if attack == "hardlink":
        os.link(path, tmp_path / "external")
    elif attack == "symlink":
        saved = tmp_path / "saved"
        path.rename(saved)
        path.symlink_to(saved)
    elif attack == "permissive":
        os.chmod(path, 0o644)
    else:
        path.write_bytes(path.read_bytes()[:-5])
    assert _attribute(repo)[0] == 4


def test_no_recursive_hashing_of_unrelated_untracked_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["allowed.txt"])
    _capture(repo)
    secret = repo / "unrelated" / "deep" / "secret.pem"
    secret.parent.mkdir(parents=True)
    secret.write_text("never read")
    os.chmod(secret, 0)
    try:
        code, value = _attribute(repo)
        assert code == 0
        assert "unrelated/deep/secret.pem" not in value["result"]["current"]["scoped_facts"]
        assert _classes(value)["unrelated/deep/secret.pem"]["classification"] == "off-scope"
    finally:
        os.chmod(secret, 0o600)


def test_rewritten_receipt_with_valid_json_is_rejected_by_authoritative_event_hash(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["tracked.txt"])
    _capture(repo)
    receipt_path = repo / ".claude/state/slice_baseline.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["git"]["porcelain_v2_sha256"] = "f" * 64
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
    code, value = _attribute(repo)
    assert code == 4 and "authoritative ledger hash" in value["error"]


def test_symlinked_ancestor_never_reads_outside_content(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["safe/file.txt"])
    _capture(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "file.txt"
    outside_file.write_text("outside secret")
    (repo / "safe/file.txt").unlink()
    (repo / "safe").rmdir()
    (repo / "safe").symlink_to(outside, target_is_directory=True)
    fact = core.file_fact(repo, "safe/file.txt", [0])
    assert fact["hash_status"] == "unsafe_ancestor"
    assert fact["sha256"] is None
    assert fact.get("sha256") != hashlib.sha256(outside_file.read_bytes()).hexdigest()
    code, value = _attribute(repo)
    assert code == 0
    assert _classes(value)["safe/file.txt"]["classification"] == "ambiguous"


def test_stable_snapshot_rejects_concurrent_anchor_or_status_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    anchors = iter([{"head": "a"}, {"head": "b"}])
    monkeypatch.setattr(core, "_anchors", lambda _root: next(anchors))
    monkeypatch.setattr(core, "_status", lambda _root: b"")
    with pytest.raises(core.GitFactError, match="concurrent"):
        core.stable_git_state(repo)
