"""Black-box and parser acceptance tests for Slice 2 Git fact attribution."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
LEDGER = ROOT / "templates" / "scripts" / "slice_ledger.py"
GIT_CLI = ROOT / "templates" / "scripts" / "slice_git.py"
CLOSE = ROOT / "templates" / "scripts" / "slice_close.py"
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


def _invoke(script: Path, repo: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(script), "--cwd", str(repo), *args], text=True, capture_output=True, check=False, env=env)
    output = result.stdout if result.returncode == 0 else result.stderr
    assert output, result
    return result.returncode, json.loads(output)


def _invoke_env_i(script: Path, repo: Path, *args: str) -> tuple[int, dict]:
    path = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
    result = subprocess.run(
        ["/usr/bin/env", "-i", f"PATH={path}", sys.executable, str(script), "--cwd", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
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


def _baseline_path(repo: Path, run_id: str = "r1") -> Path:
    return repo / ".claude/state/runs" / hashlib.sha256(run_id.encode()).hexdigest() / "slice_baseline.json"


def _failed_expansion(repo: Path, tmp_path: Path, new_path: str = "new.txt") -> None:
    evidence=tmp_path/"fail.json"; evidence.write_text(json.dumps({"schema_version":1,"argv":[sys.executable,"-c","raise SystemExit(9)"],"timeout_seconds":10}))
    assert _invoke(CLOSE,repo,"verify","--run-id","r1","--session-id","s1","--revision","2","--evidence-file",str(evidence))[0]==0
    ledger=json.loads((repo/".claude/state/slice_ledger.json").read_text()); paths=[*ledger["allowed_paths"],new_path]
    args=["update","--run-id","r1","--session-id","s1","--revision","3","--artifact-contract","expanded repair","--reason","verified failed scope gap","--provenance-actor","test","--provenance-source","verified_recon","--provenance-evidence-sha256",ledger["verification_sha256"]]
    for path in paths: args += ["--allowed-path",path]
    assert _invoke(LEDGER,repo,*args)[0]==0


def test_refresh_v2_preserves_dirty_provenance_and_only_post_v2_clean_delta_is_candidate(tmp_path: Path) -> None:
    repo=_repo(tmp_path,["dirty.txt","clean.txt","owned.txt"]); (repo/"dirty.txt").write_text("dirty before v1\n"); _capture(repo)
    (repo/"owned.txt").write_text("legitimate after v1 before v2\n")
    (repo/"new.txt").write_text("dirty when admitted\n"); _failed_expansion(repo,tmp_path)
    code,value=_invoke(GIT_CLI,repo,"refresh","--run-id","r1","--session-id","s1","--revision","4")
    assert code==0 and value["result"]["generation"]==2 and value["result"]["lineage"]["root_baseline_sha256"]
    v1=_baseline_path(repo); v2=v1.with_name("slice_baseline_v2.json"); original=v1.read_bytes()
    assert _invoke(GIT_CLI,repo,"refresh","--run-id","r1","--session-id","s1","--revision","4")[0]==0 and v1.read_bytes()==original
    classes=_classes(_invoke(GIT_CLI,repo,"attribute","--run-id","r1","--session-id","s1","--revision","5")[1])
    assert classes["dirty.txt"]["classification"]=="foreign" and classes["new.txt"]["classification"]=="foreign"
    assert classes["owned.txt"]["classification"]=="candidate-owned"
    (repo/"clean.txt").write_text("post v2 delta\n"); classes=_classes(_invoke(GIT_CLI,repo,"attribute","--run-id","r1","--session-id","s1","--revision","5")[1])
    assert classes["clean.txt"]["classification"]=="candidate-owned" and v2.exists()


def test_late_admission_reuses_root_only_for_provably_tracked_clean_path(tmp_path: Path) -> None:
    repo=_repo(tmp_path,["work.txt"]); (repo/"tracked.txt").write_text("tracked base\n"); (repo/"root-dirty.txt").write_text("dirty base\n"); _git(repo,"add","tracked.txt","root-dirty.txt"); _git(repo,"commit","-qm","seed late paths"); (repo/"root-dirty.txt").write_text("dirty before v1\n"); _capture(repo)
    (repo/"tracked.txt").write_text("tracked repair\n"); (repo/"root-dirty.txt").write_text("dirty repair\n"); (repo/"untracked.txt").write_text("untracked repair\n")
    _failed_expansion(repo,tmp_path,"tracked.txt")
    ledger=json.loads((repo/".claude/state/slice_ledger.json").read_text()); paths=sorted(set([*ledger["allowed_paths"],"root-dirty.txt","untracked.txt"]))
    args=["update","--run-id","r1","--session-id","s1","--revision","4","--artifact-contract","admit hostile paths","--reason","hostile admission fixture","--provenance-actor","test","--provenance-source","verified_recon","--provenance-evidence-sha256",ledger["verification_sha256"]]
    for path in paths: args += ["--allowed-path",path]
    assert _invoke(LEDGER,repo,*args)[0]==0
    assert _invoke(GIT_CLI,repo,"refresh","--run-id","r1","--session-id","s1","--revision","5")[0]==0
    classes=_classes(_invoke(GIT_CLI,repo,"attribute","--run-id","r1","--session-id","s1","--revision","6")[1])
    assert classes["tracked.txt"]["classification"]=="candidate-owned"
    assert classes["root-dirty.txt"]["classification"] in {"foreign","ambiguous"}
    assert classes["untracked.txt"]["classification"] in {"foreign","ambiguous"}


def test_late_admission_anchor_change_never_reuses_root_ownership(tmp_path: Path) -> None:
    repo=_repo(tmp_path,["work.txt"]); (repo/"tracked.txt").write_text("base\n"); _git(repo,"add","tracked.txt"); _git(repo,"commit","-qm","tracked seed"); _capture(repo)
    (repo/"tracked.txt").write_text("repair\n"); (repo/"unrelated.txt").write_text("new head\n"); _git(repo,"add","unrelated.txt"); _git(repo,"commit","-qm","move anchor")
    _failed_expansion(repo,tmp_path,"tracked.txt"); assert _invoke(GIT_CLI,repo,"refresh","--run-id","r1","--session-id","s1","--revision","4")[0]==0
    classification=_classes(_invoke(GIT_CLI,repo,"attribute","--run-id","r1","--session-id","s1","--revision","5")[1])["tracked.txt"]["classification"]
    assert classification in {"foreign","ambiguous"}


def test_refresh_rejects_concurrency_and_tamper(tmp_path: Path) -> None:
    repo=_repo(tmp_path,["clean.txt"]); _capture(repo); (repo/"new.txt").write_text("new\n"); _failed_expansion(repo,tmp_path)
    args=("refresh","--run-id","r1","--session-id","s1","--revision","4")
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _: _invoke(GIT_CLI,repo,*args),range(2)))
    assert [code for code,_ in results]==[0,0]
    v2=_baseline_path(repo).with_name("slice_baseline_v2.json"); receipt=json.loads(v2.read_text()); receipt["lineage"]["root_baseline_sha256"]="0"*64; v2.write_text(json.dumps(receipt)+"\n"); os.chmod(v2,0o600)
    assert _invoke(GIT_CLI,repo,"attribute","--run-id","r1","--session-id","s1","--revision","5")[0]==4


def test_refresh_guards_wrong_revision_and_missing_fail_expansion(tmp_path: Path) -> None:
    plain=_repo(tmp_path/"plain",["clean.txt"]); _capture(plain)
    assert _invoke(GIT_CLI,plain,"refresh","--run-id","r1","--session-id","s1","--revision","2")[0]==3
    repo=_repo(tmp_path/"expanded",["clean.txt"]); _capture(repo); (repo/"new.txt").write_text("new\n"); _failed_expansion(repo,tmp_path/"expanded")
    assert _invoke(GIT_CLI,repo,"refresh","--run-id","r1","--session-id","s1","--revision","3")[0]==3


def test_refresh_rejects_preexisting_orphan_and_after_pass(tmp_path: Path) -> None:
    repo=_repo(tmp_path,["clean.txt"]); _capture(repo); _failed_expansion(repo,tmp_path)
    v2=_baseline_path(repo).with_name("slice_baseline_v2.json"); v2.write_text("{}\n"); os.chmod(v2,0o600)
    assert _invoke(GIT_CLI,repo,"refresh","--run-id","r1","--session-id","s1","--revision","4")[0]==3
    v2.unlink(); assert _invoke(GIT_CLI,repo,"refresh","--run-id","r1","--session-id","s1","--revision","4")[0]==0
    evidence=tmp_path/"pass.json"; evidence.write_text(json.dumps({"schema_version":1,"argv":[sys.executable,"-c","print('ok')"],"timeout_seconds":10}))
    retry=("verify","--run-id","r1","--session-id","s1","--revision","5","--evidence-file",str(evidence),"--attempt-id","a2","--attempt-number","2","--repair-reason","fixed","--provenance-actor","test","--provenance-source","verified_recon")
    assert _invoke(CLOSE,repo,*retry)[0]==0
    assert _invoke(GIT_CLI,repo,"refresh","--run-id","r1","--session-id","s1","--revision","6")[0]==3


def test_retry_fail_can_expand_and_refresh_generation_three(tmp_path: Path) -> None:
    repo=_repo(tmp_path,["clean.txt"]); _capture(repo); _failed_expansion(repo,tmp_path)
    assert _invoke(GIT_CLI,repo,"refresh","--run-id","r1","--session-id","s1","--revision","4")[0]==0
    evidence=tmp_path/"fail2.json"; evidence.write_text(json.dumps({"schema_version":1,"argv":[sys.executable,"-c","raise SystemExit(8)"],"timeout_seconds":10}))
    retry=("verify","--run-id","r1","--session-id","s1","--revision","5","--evidence-file",str(evidence),"--attempt-id","a2","--attempt-number","2","--repair-reason","still failing","--provenance-actor","test","--provenance-source","verified_recon")
    assert _invoke(CLOSE,repo,*retry)[0]==0
    ledger=json.loads((repo/".claude/state/slice_ledger.json").read_text()); paths=[*ledger["allowed_paths"],"third.txt"]
    args=["update","--run-id","r1","--session-id","s1","--revision","6","--artifact-contract","third repair","--reason","second failed gap","--provenance-actor","test","--provenance-source","verified_recon","--provenance-evidence-sha256",ledger["verification_sha256"]]
    for path in paths: args += ["--allowed-path",path]
    assert _invoke(LEDGER,repo,*args)[0]==0
    code,value=_invoke(GIT_CLI,repo,"refresh","--run-id","r1","--session-id","s1","--revision","7")
    assert code==0 and value["result"]["generation"]==3


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
    result = _attribute(repo)[1]
    classes = _classes(result)
    assert classes["outside.txt"]["classification"] == "off-scope"
    reserved = [
        item for item in result["result"]["classifications"]
        if item["path"] == ".claude" or item["path"].startswith(".claude/")
    ]
    assert reserved
    assert all(
        item["classification"] == "foreign"
        and item["reasons"] == ["reserved_control_state"]
        for item in reserved
    )


def test_git_facts_match_under_polluted_host_environment_and_env_i(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["tracked.txt"])
    assert _capture(repo)[0] == 0
    global_excludes = tmp_path / "global-excludes"
    global_excludes.write_text("host-only.tmp\n")
    global_config = tmp_path / "global-gitconfig"
    global_config.write_text(f"[core]\n\texcludesFile = {global_excludes}\n")
    info_exclude = repo / ".git" / "info" / "exclude"
    info_exclude.write_text("repo-local.tmp\n")
    (repo / "tracked.txt").write_text("changed\n")
    (repo / "host-only.tmp").write_text("must remain visible\n")
    (repo / "repo-local.tmp").write_text("must remain ignored\n")

    polluted = dict(os.environ)
    polluted["GIT_CONFIG_GLOBAL"] = str(global_config)
    polluted["HOME"] = str(tmp_path / "fake-home")
    direct_code, direct = _invoke(
        GIT_CLI, repo, "attribute", "--run-id", "r1", "--session-id", "s1", "--revision", "2", env=polluted
    )
    clean_code, clean = _invoke_env_i(
        GIT_CLI, repo, "attribute", "--run-id", "r1", "--session-id", "s1", "--revision", "2"
    )

    assert direct_code == clean_code == 0
    assert direct["result"]["state_sha256"] == clean["result"]["state_sha256"]
    assert direct["result"]["attribution_sha256"] == clean["result"]["attribution_sha256"]
    assert direct["result"]["classifications"] == clean["result"]["classifications"]
    classes = _classes(direct)
    assert classes["host-only.tmp"]["classification"] == "off-scope"
    assert classes["repo-local.tmp"]["classification"] == "off-scope"


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
    path = _baseline_path(repo)
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
    path = _baseline_path(repo)
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
    receipt_path = _baseline_path(repo)
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
