"""Durable public CLI/Git contracts for managed development and release test modes.

The fixtures copy the shipped scripts and contracts into disposable Git repositories;
the source checkout is never used as mutable state.  Run with:
    python3 -m unittest tests.test_test_modes_contract
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1]
SCRIPTS = SOURCE / "templates" / "scripts"
CONTRACT = SOURCE / "templates" / "test-contract"
ZERO = "0" * 40


def run(*argv: object, cwd: Path, stdin: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    effective = os.environ.copy()
    if env:
        effective.update(env)
    return subprocess.run([str(x) for x in argv], cwd=cwd, input=stdin, text=True,
                          capture_output=True, check=False, env=effective, timeout=20)


def git(repo: Path, *args: str) -> str:
    result = run("git", *args, cwd=repo)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class RepoFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "templates/scripts").mkdir(parents=True)
        (root / "templates/test-contract").mkdir(parents=True)
        for name in ("test_modes_core.py", "test_dispatcher.py", "test_pre_push.py", "test_ci_adapter.py"):
            (root / "templates/scripts" / name).write_bytes((SCRIPTS / name).read_bytes())
        for source in CONTRACT.iterdir():
            if source.is_file():
                (root / "templates/test-contract" / source.name).write_bytes(source.read_bytes())
        git(root, "init", "-q")
        git(root, "config", "user.name", "Test")
        git(root, "config", "user.email", "test@example.invalid")
        (root / "app.py").write_text("print('ok')\n")
        git(root, "add", ".")
        git(root, "commit", "-qm", "fixture")
        git(root, "remote", "add", "origin", "https://example.invalid/repo.git")

    @property
    def dispatcher(self) -> Path:
        return self.root / "templates/scripts/test_dispatcher.py"

    @property
    def pre_push(self) -> Path:
        return self.root / "templates/scripts/test_pre_push.py"

    @property
    def ci(self) -> Path:
        return self.root / "templates/scripts/test_ci_adapter.py"

    def cli(self, script: Path, *args: str, stdin: str | None = None, env: dict[str, str] | None = None):
        return run(sys.executable, script, "--root", self.root, *args, cwd=self.root, stdin=stdin, env=env)

    def module(self, name: str):
        scripts = str(self.root / "templates/scripts")
        sys.path.insert(0, scripts)
        try:
            spec = importlib.util.spec_from_file_location(f"fixture_{name}_{id(self)}", Path(scripts) / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            assert spec.loader
            with mock.patch.object(sys, "dont_write_bytecode", True):
                spec.loader.exec_module(module)
            return module
        finally:
            sys.path.remove(scripts)

    def tiny_contract(self, count: int = 20) -> dict:
        (self.root / ".gitignore").write_text(".claude/test-modes/\n.claude/phase_lease.json\n__pycache__/\n")
        jobs = []
        for index in range(count):
            jobs.append({"id": f"job-{index:02d}", "argv": [sys.executable, "-c", "pass"], "cwd": ".", "env": {},
                         "components": ["app"], "test_class": "unit", "stratum": "even" if index % 2 == 0 else "odd",
                         "platform": "any", "network": "forbidden", "state_scope": "temp-isolated",
                         "timeout_seconds": 5, "critical": index == 0, "required": True, "source_files": ["app.py"]})
        registry = {"schema_version": 1, "jobs": jobs}
        registry["registry_hash"] = digest(registry)
        self.write_contract("registry.json", registry)
        self.write_contract("impact.json", {"schema_version": 1, "mappings": [{"path_prefix": "app.py", "test_ids": ["job-01"], "reviewed_at": "2026-08-19"}]})
        self.write_contract("critical_smoke.json", {"schema_version": 1, "tests": [{"id": "job-00", "owner": "tests", "rationale": "critical", "reviewed_at": "2026-08-19"}]})
        self.write_contract("protected_refs.json", {"schema_version": 1, "protected_refs": ["refs/heads/main"]})
        return registry

    def write_contract(self, name: str, value: dict) -> None:
        (self.root / "templates/test-contract" / name).write_bytes(canonical(value))

    def commit_contract(self) -> str:
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "contract")
        return git(self.root, "rev-parse", "HEAD")

    def plan(self, mode: str = "release", base: str = "HEAD") -> dict:
        result = self.cli(self.dispatcher, "plan", "--mode", mode, "--base", base)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def release_receipt(self, registry: dict, head: str | None = None) -> dict:
        core = self.module("test_modes_core")
        candidate = core.candidate_identity(self.root)
        if head is not None:
            candidate["head"] = head
            candidate["tree"] = git(self.root, "rev-parse", f"{head}^{{tree}}")
        outcomes = []
        for job in registry["jobs"]:
            command = digest({key: job[key] for key in ("id", "argv", "cwd", "env", "platform", "network", "state_scope", "timeout_seconds")})
            outcomes.append({"id": job["id"], "status": "pass", "exit_code": 0, "stdout_sha256": digest(""), "stderr_sha256": digest(""), "command_identity": command})
        receipt = {"schema_version": 1, "kind": "local-release-receipt", "created_at": "2026-08-19T00:00:00Z", "status": "pass",
                   "candidate": candidate, "manifest_hash": "a" * 64, "registry_hash": registry["registry_hash"],
                   "test_ids": [x["id"] for x in registry["jobs"]], "outcomes": outcomes, "promotion_authorized": False}
        receipt["receipt_hash"] = digest(receipt)
        return receipt


class TestModesContract(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = RepoFixture(Path(self.temporary.name) / "repo")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_shipped_registry_has_one_job_per_test_file_and_release_has_no_omissions(self) -> None:
        core = self.repo.module("test_modes_core")
        registry = core.validate_registry(json.loads((CONTRACT / "registry.json").read_text()))
        jobs = registry["jobs"]
        argv_by_id = {job["id"]: job["argv"] for job in jobs}
        python_files = sorted(path.relative_to(SOURCE).as_posix() for path in (SOURCE / "tests").glob("test_*.py"))
        shell_files = git(SOURCE, "ls-files", "tests/test_*.sh").splitlines()
        represented_python = {arg for argv in argv_by_id.values() for arg in argv if arg in python_files}
        represented_shell = {argv[-1] for argv in argv_by_id.values() if len(argv) == 3 and argv[:2] == ["bash", "tests/run_shell_contracts.sh"]}
        self.assertEqual(represented_python, set(python_files))
        self.assertEqual(represented_shell, set(shell_files))
        self.assertEqual(len(jobs), len(python_files) + len(shell_files) + 1)
        self.assertTrue(all(job["required"] for job in jobs))

    def test_phase_progress_clear_is_compatible_safe_and_keeps_clean_repo_clean(self) -> None:
        root = Path(self.temporary.name) / "phase-repo"
        (root / "templates/scripts").mkdir(parents=True)
        for name in ("phase.py", "test_modes_core.py"):
            (root / "templates/scripts" / name).write_bytes((SCRIPTS / name).read_bytes())
        (root / ".gitignore").write_bytes((SOURCE / ".gitignore").read_bytes())
        git(root, "init", "-q"); git(root, "config", "user.name", "Test"); git(root, "config", "user.email", "test@example.invalid")
        git(root, "add", "."); git(root, "commit", "-qm", "clean")
        phase = root / "templates/scripts/phase.py"
        self.assertEqual(run(sys.executable, phase, "set", "IMPLEMENT", cwd=root).returncode, 0)
        self.assertEqual(run(sys.executable, phase, "progress", "1/2", cwd=root).returncode, 0)
        cleared = run(sys.executable, phase, "progress", "clear", cwd=root)
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertFalse((root / ".claude/phase_progress.jsonl").exists())
        self.assertEqual(run(sys.executable, phase, "get", cwd=root).stdout.strip(), "IMPLEMENT")
        self.assertEqual(git(root, "status", "--porcelain"), "")

    def test_resolver_precedence_and_invalid_stale_or_wrong_session_lease(self) -> None:
        core = self.repo.module("test_modes_core")
        lease = core.create_lease(self.repo.root, "IMPLEMENT", "session-ok")
        self.assertEqual(core.resolve_mode(requested="development", trusted_ci=False, protected_destination=False, lease=lease), "development")
        for kwargs in ({"trusted_ci": True, "protected_destination": False}, {"trusted_ci": False, "protected_destination": True}):
            self.assertEqual(core.resolve_mode(requested="development", lease=lease, **kwargs), "release")
        self.assertEqual(core.resolve_mode(requested="release", trusted_ci=False, protected_destination=False, lease=lease), "release")
        with self.assertRaises(core.ModeError):
            core.resolve_mode(requested="bogus", trusted_ci=False, protected_destination=False, lease=lease)
        lease["expires_at"] = "2000-01-01T00:00:00Z"
        with self.assertRaisesRegex(core.ModeError, "expired"):
            core.validate_lease(self.repo.root, lease, "session-ok")
        lease = core.create_lease(self.repo.root, "PLAN", "session-ok")
        with self.assertRaisesRegex(core.ModeError, "session"):
            core.validate_lease(self.repo.root, lease, "other-session")
        lease["root"] = str(self.repo.root / "wrong")
        with self.assertRaisesRegex(core.ModeError, "root binding"):
            core.validate_lease(self.repo.root, lease, "session-ok")

    def test_selection_is_deterministic_stratified_and_unions_impact_critical_unresolved(self) -> None:
        registry = self.repo.tiny_contract()
        base = self.repo.commit_contract()
        (self.repo.root / "app.py").write_text("print('changed')\n")
        core = self.repo.module("test_modes_core")
        impact = core.load_impact(self.repo.root)
        first = core.selection(self.repo.root, core.load_registry(self.repo.root), impact, base, {"job-02"}, "epoch", False, None, {"job-00"})
        second = core.selection(self.repo.root, core.load_registry(self.repo.root), impact, base, {"job-02"}, "epoch", False, None, {"job-00"})
        self.assertEqual(first, second)
        self.assertTrue({"job-00", "job-01", "job-02"} <= set(first))
        sampled = set(first) - {"job-00", "job-01", "job-02"}
        self.assertEqual(len(sampled), 2, "one global ceil(10%) quota is required")

    def test_global_sample_quota_does_not_expand_many_small_strata_to_full_suite(self) -> None:
        core = self.repo.module("test_modes_core")
        jobs = [
            {"id": f"sample-{index:02d}", "stratum": f"stratum-{index:02d}"}
            for index in range(40)
        ]
        sampled = core.deterministic_sample(jobs, "stable-seed", 10)
        self.assertEqual(len(sampled), 4)
        self.assertLess(len(sampled), len(jobs))
        self.assertEqual(sampled, core.deterministic_sample(jobs, "stable-seed", 10))

    def test_unknown_path_escalates_and_trusted_base_jobs_cannot_be_removed(self) -> None:
        registry = self.repo.tiny_contract(4)
        base = self.repo.commit_contract()
        (self.repo.root / "unknown.bin").write_bytes(b"x")
        core = self.repo.module("test_modes_core")
        with self.assertRaisesRegex(core.ModeError, "unknown changed path"):
            core.selection(self.repo.root, core.load_registry(self.repo.root), core.load_impact(self.repo.root), base, set(), "1", False)
        (self.repo.root / "unknown.bin").unlink()
        candidate = json.loads(json.dumps(registry)); candidate["jobs"] = candidate["jobs"][:-1]; candidate.pop("registry_hash"); candidate = core.validate_registry(candidate)
        (self.repo.root / "templates/test-contract/impact.json").write_text('{"changed":true}')
        mapped = {"schema_version": 1, "mappings": [{"path_prefix": "templates/test-contract/impact.json", "test_ids": ["job-00"], "reviewed_at": "2026-08-19"}]}
        with self.assertRaisesRegex(core.ModeError, "cannot remove"):
            core.selection(self.repo.root, candidate, mapped, base, set(), "1", False, registry)

    def test_manifest_hash_candidate_mutation_dirty_release_and_json_error_boundary(self) -> None:
        self.repo.tiny_contract(2); self.repo.commit_contract()
        manifest = self.repo.plan("release")
        self.assertEqual(manifest["manifest_hash"], digest({k: v for k, v in manifest.items() if k not in {"created_at", "manifest_hash"}}))
        path = self.repo.root / "manifest.json"; path.write_bytes(canonical(manifest))
        (self.repo.root / "mutation.txt").write_text("dirty")
        changed = self.repo.cli(self.repo.dispatcher, "run", "--manifest", str(path))
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("candidate changed", json.loads(changed.stderr)["error"])
        manifest = self.repo.plan("release"); path.write_bytes(canonical(manifest))
        dirty = self.repo.cli(self.repo.dispatcher, "run", "--manifest", str(path))
        self.assertEqual(json.loads(dirty.stdout)["status"], "fail")
        bad = self.repo.cli(self.repo.dispatcher, "run", "--manifest", str(self.repo.root / "missing.json"))
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(json.loads(bad.stderr)["status"], "fail")

    def test_failure_ledger_is_private_hash_chained_and_closes_only_equivalent_failure(self) -> None:
        core = self.repo.module("test_modes_core")
        common = {"test_id": "job", "command_identity": "a" * 64, "contract_identity": "b" * 64}
        failure_hash = core.append_failure(self.repo.root, {"type": "failure", **common, "candidate": {"head": "x"}, "signature": "c" * 64})
        ledger = self.repo.root / ".claude/test-modes/failure-ledger.jsonl"
        self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), 0o600)
        core.append_failure(self.repo.root, {"type": "closure", **common, "failure_hash": failure_hash, "status": "green"})
        self.assertEqual(core.unresolved_failures(self.repo.root), set())
        other = {**common, "command_identity": "d" * 64}
        core.append_failure(self.repo.root, {"type": "failure", **other, "candidate": {}, "signature": "e" * 64})
        core.append_failure(self.repo.root, {"type": "closure", **common, "failure_hash": failure_hash, "status": "green"})
        self.assertEqual(core.unresolved_failures(self.repo.root), {"job"})
        lines = ledger.read_text().splitlines(); record = json.loads(lines[1]); record["previous_hash"] = "f" * 64; lines[1] = json.dumps(record)
        ledger.write_text("\n".join(lines) + "\n"); ledger.chmod(0o600)
        with self.assertRaisesRegex(core.ModeError, "chain corrupt"):
            core.unresolved_failures(self.repo.root)

    def test_install_hook_custom_path_spaces_propagates_stdin_argv_exit_and_is_idempotent(self) -> None:
        hooks = (self.repo.root / "custom hooks").resolve()
        hooks.mkdir(mode=0o700)
        git(self.repo.root, "config", "core.hooksPath", str(hooks))
        user = hooks / "pre-push"
        user.write_text("#!/bin/sh\nprintf '%s|%s\\n' \"$1\" \"$2\" > user.args\ncat > user.stdin\nexit 7\n")
        user.chmod(0o700)
        installed = self.repo.cli(self.repo.dispatcher, "install-hook")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        wrapper = hooks / "pre-push"
        payload = "refs/heads/x " + ZERO + " refs/heads/y " + ZERO + "\n"
        invoked = run(wrapper, "origin", "https://example.invalid/repo.git", cwd=self.repo.root, stdin=payload)
        self.assertEqual(invoked.returncode, 7)
        self.assertEqual((self.repo.root / "user.args").read_text(), "origin|https://example.invalid/repo.git\n")
        self.assertEqual((self.repo.root / "user.stdin").read_text(), payload)
        again = self.repo.cli(self.repo.dispatcher, "install-hook")
        self.assertTrue(json.loads(again.stdout)["idempotent"])

    def test_install_hook_rejects_truncated_symlink_hardlink_and_unsafe_hook(self) -> None:
        cases = ("truncated", "symlink", "hardlink", "unsafe")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                fixture = RepoFixture(Path(temporary) / "repo")
                hooks = (fixture.root / "hooks").resolve(); hooks.mkdir(mode=0o700); git(fixture.root, "config", "core.hooksPath", str(hooks))
                hook = hooks / "pre-push"
                if case == "truncated": hook.write_text("#!/bin/sh\n# claude-booster-managed-pre-push-v1\n"); hook.chmod(0o700)
                elif case == "symlink": os.symlink("target", hook)
                elif case == "hardlink":
                    target = hooks / "target"; target.write_text("#!/bin/sh\n"); target.chmod(0o700); os.link(target, hook)
                else: hook.write_text("#!/bin/sh\n"); hook.chmod(0o722)
                result = fixture.cli(fixture.dispatcher, "install-hook")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stderr)["status"], "fail")

    def test_pre_push_normal_protected_new_tag_delete_force_unknown_and_multiref(self) -> None:
        registry = self.repo.tiny_contract(2); parent = self.repo.commit_contract()
        (self.repo.root / "app.py").write_text("print('next')\n"); git(self.repo.root, "add", "app.py"); git(self.repo.root, "commit", "-qm", "app change")
        head = git(self.repo.root, "rev-parse", "HEAD")
        git(self.repo.root, "update-ref", "refs/remotes/origin/main", parent)
        git(self.repo.root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        receipt = self.repo.release_receipt(registry)
        self.repo.module("test_modes_core").create_lease(self.repo.root, "IMPLEMENT", "pre-push-session")
        receipt_dir = self.repo.root / ".claude/test-modes/receipts"; receipt_dir.mkdir(parents=True)
        (receipt_dir / "exact.json").write_bytes(canonical(receipt)); (receipt_dir / "exact.json").chmod(0o600)
        def probe(local_ref: str, local_oid: str, remote_ref: str, remote_oid: str):
            line = f"{local_ref} {local_oid} {remote_ref} {remote_oid}\n"
            return self.repo.cli(self.repo.pre_push, "origin", "https://example.invalid/repo.git", stdin=line)
        normal = probe("refs/heads/topic", head, "refs/heads/topic", parent)
        self.assertEqual(normal.returncode, 0, normal.stderr)
        for remote_ref, remote_oid in (("refs/heads/main", parent), ("refs/heads/new", ZERO), ("refs/tags/v1", ZERO)):
            self.assertEqual(probe("refs/heads/topic", head, remote_ref, remote_oid).returncode, 0)
        self.assertNotEqual(probe("(delete)", ZERO, "refs/heads/topic", head).returncode, 0)
        self.assertNotEqual(probe("refs/heads/topic", parent, "refs/heads/topic", head).returncode, 0)
        self.assertNotEqual(probe("refs/heads/topic", head, "refs/changes/1", ZERO).returncode, 0)
        multi = "\n".join((f"refs/heads/a {head} refs/heads/a {parent}", f"refs/heads/b {head} refs/heads/b {ZERO}")) + "\n"
        result = self.repo.cli(self.repo.pre_push, "origin", "https://example.invalid/repo.git", stdin=multi)
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertEqual([item["remote_ref"] for item in body["records"]], ["refs/heads/a", "refs/heads/b"])
        self.assertFalse(body["promotion_authorized"])

    def test_pre_push_new_unprotected_branch_uses_default_remote_merge_base_and_fails_closed_without_it(self) -> None:
        self.repo.tiny_contract(2); parent = self.repo.commit_contract()
        (self.repo.root / "app.py").write_text("print('feature')\n")
        git(self.repo.root, "add", "app.py"); git(self.repo.root, "commit", "-qm", "feature")
        head = git(self.repo.root, "rev-parse", "HEAD")
        self.repo.module("test_modes_core").create_lease(self.repo.root, "IMPLEMENT", "new-branch")
        line = f"refs/heads/feature {head} refs/heads/feature {ZERO}\n"
        missing = self.repo.cli(self.repo.pre_push, "origin", "https://example.invalid/repo.git", stdin=line)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("remote default base cannot be resolved", json.loads(missing.stderr)["error"])
        git(self.repo.root, "update-ref", "refs/remotes/origin/main", parent)
        git(self.repo.root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        passed = self.repo.cli(self.repo.pre_push, "origin", "https://example.invalid/repo.git", stdin=line)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertTrue(json.loads(passed.stdout)["records"][0]["new"])

    def test_pre_push_requires_receipt_for_exact_elevated_oid(self) -> None:
        registry = self.repo.tiny_contract(1); head = self.repo.commit_contract(); parent = git(self.repo.root, "rev-parse", "HEAD^")
        wrong = self.repo.release_receipt(registry, parent)
        target = self.repo.root / ".claude/test-modes/receipts/wrong.json"; target.parent.mkdir(parents=True); target.write_bytes(canonical(wrong)); target.chmod(0o600)
        line = f"refs/heads/topic {head} refs/heads/main {parent}\n"
        result = self.repo.cli(self.repo.pre_push, "origin", "https://example.invalid/repo.git", stdin=line)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact clean local release receipt", json.loads(result.stderr)["error"])

    def _ci_bundle(self):
        registry = self.repo.tiny_contract(2)
        if git(self.repo.root, "status", "--porcelain"):
            self.repo.commit_contract()
        core = self.repo.module("test_modes_core"); candidate = core.candidate_identity(self.repo.root)
        context = {"repository": "owner/repo", "remote": "origin", "target_ref": "refs/heads/main", "base_sha": git(self.repo.root, "rev-parse", "HEAD^"),
                   "merge_sha": candidate["head"], "merge_tree": candidate["tree"], "workflow_digest": "a" * 64, "run_id": "123", "platform_digest": "b" * 64, "trust_source": "github-actions-protected"}
        jobs = registry["jobs"]
        manifest = {"schema_version": 1, "kind": "manifest", "created_at": "2026-08-19T00:00:00Z", "mode": "release", "candidate": candidate,
                    "lease_valid": False, "trusted_ci": True, "protected_destination": True, "local_advisory_only": False,
                    "base": "HEAD^", "base_sha": context["base_sha"], "registry_hash": registry["registry_hash"], "registry": registry,
                    "test_ids": [x["id"] for x in jobs], "jobs": jobs}
        manifest["manifest_hash"] = digest({k: v for k, v in manifest.items() if k != "created_at"})
        receipt = self.repo.release_receipt(registry); receipt["candidate"] = candidate; receipt["manifest_hash"] = manifest["manifest_hash"]; receipt["receipt_hash"] = digest({k: v for k, v in receipt.items() if k != "receipt_hash"})
        return registry, context, manifest, receipt

    def _ci_run(self, registry: dict, context: dict, manifest: dict, receipt: dict):
        with tempfile.TemporaryDirectory() as temporary:
            paths = []
            for name, value in (("registry", registry), ("context", context), ("manifest", manifest), ("receipt", receipt)):
                path = Path(temporary) / f"{name}.json"; path.write_bytes(canonical(value)); path.chmod(0o600); paths.append(path)
            return run(sys.executable, self.repo.ci, "validate", "--input", paths[1], "--receipt", paths[3], "--manifest", paths[2], "--registry", paths[0], cwd=self.repo.root)

    def test_ci_exact_full_authorization(self) -> None:
        bundle = self._ci_bundle(); result = self._ci_run(*bundle)
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertTrue(body["promotion_authorized"])
        self.assertEqual(body["promotion_hash"], digest({k: v for k, v in body.items() if k != "promotion_hash"}))

    def test_ci_rejects_tampering_partial_skipped_timeout_and_forged_command_identity(self) -> None:
        mutations = {
            "manifest-tamper": lambda c, m, r: m.update({"manifest_hash": "0" * 64}),
            "partial": lambda c, m, r: (r["test_ids"].pop(), r["outcomes"].pop()),
            "skipped": lambda c, m, r: r["outcomes"][0].update({"status": "skipped"}),
            "timeout": lambda c, m, r: r["outcomes"][0].update({"status": "timeout", "exit_code": None}),
            "forged-command": lambda c, m, r: r["outcomes"][0].update({"command_identity": "f" * 64}),
            "dirty": lambda c, m, r: (c.update({"merge_sha": r["candidate"]["head"]}), r["candidate"].update({"dirty": True}), m["candidate"].update({"dirty": True})),
            "untrusted": lambda c, m, r: c.update({"trust_source": "local"}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                registry, context, manifest, receipt = self._ci_bundle()
                mutate(context, manifest, receipt)
                receipt["receipt_hash"] = digest({k: v for k, v in receipt.items() if k != "receipt_hash"})
                result = self._ci_run(registry, context, manifest, receipt)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stderr)["status"], "fail")


if __name__ == "__main__":
    unittest.main()
