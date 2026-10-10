"""Public CLI checks for project contracts and explicit local first trust.

Purpose: exercise failure boundaries using disposable Git repositories only.
Contract: no external services, production datasets, credentials, or installed writes.
CLI: python3 -m unittest tests.test_project_contract_admission
Limitations: synthetic infrastructure jobs assert mechanics, not Horizon acceptance.
ENV/Files: temporary repositories and private assertion files, automatically removed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_test_modes_contract as contracts
from tests.test_test_modes_contract import RepoFixture, canonical, digest, git


class ProjectAdmission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.repo = RepoFixture(self.directory / "repo")
        self.registry = self.repo.tiny_contract(2)
        self.repo.commit_contract()
        registry_path = self.repo.root / "templates/test-contract/registry.json"
        registry_path.unlink()
        git(self.repo.root, "add", ".")
        git(self.repo.root, "commit", "-qm", "project baseline without registry")
        self.base = git(self.repo.root, "rev-parse", "HEAD")
        self.repo.write_contract("registry.json", self.registry)
        self.repo.commit_contract()
        self.artifact = self.directory / "admission.json"
        self.review_path = self.directory / "review.json"
        self.subject = {"schema_version": 1, "kind": "local-first-trust-assertion",
                        "repository": {"root": str(self.repo.root), "git_common_dir": str(self.repo.root / ".git")},
                        "base_sha": self.base, "registry": self.registry, "author": "fixture-author"}
        self.make_admission()

    def tearDown(self):
        self.temp.cleanup()

    def write_private(self, path, value):
        raw = canonical(value)
        path.write_bytes(raw)
        path.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()

    def make_admission(self):
        review = {"schema_version": 1, "subject_sha256": digest(self.subject), "reviewer": "fixture-reviewer",
                  "verdict": "accept", "scope": "complete-baseline-job-policy-inventory"}
        self.review_hash = self.write_private(self.review_path, review)
        self.value = dict(self.subject, review={"path": str(self.review_path), "sha256": self.review_hash})
        self.pin = self.write_private(self.artifact, self.value)

    def plan(self, *extra):
        return self.repo.cli(self.repo.dispatcher, "plan", "--base", self.base,
                             "--admission", str(self.artifact), "--admission-sha256", self.pin, *extra)

    def freeze(self):
        result = self.plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        path = self.directory / "manifest.json"
        path.write_bytes(canonical(manifest))
        return path, manifest

    def execute(self, path, *extra):
        return self.repo.cli(self.repo.dispatcher, "run", "--manifest", str(path),
                             "--admission", str(self.artifact), "--admission-sha256", self.pin, *extra)

    def test_full_local_release_preserves_original_base_and_has_no_promotion(self):
        path, manifest = self.freeze()
        self.assertEqual(manifest["base_sha"], self.base)
        self.assertNotEqual(manifest["base_sha"], manifest["candidate"]["head"])
        self.assertEqual(manifest["test_ids"], ["job-00", "job-01"])
        self.assertEqual(manifest["mode"], "release")
        result = self.execute(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "pass")
        self.assertFalse(receipt["promotion_authorized"])
        self.assertEqual(receipt["admission"], manifest["admission"])

    def test_missing_pin_and_protected_or_ci_are_rejected(self):
        for flag in ("--trusted-ci", "--protected"):
            result = self.plan(flag)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("local-only", result.stderr)
        result = self.repo.cli(self.repo.dispatcher, "plan", "--base", self.base, "--admission", str(self.artifact))
        self.assertNotEqual(result.returncode, 0)

    def test_base_with_registry_cannot_bootstrap(self):
        self.base = git(self.repo.root, "rev-parse", "HEAD")
        self.subject["base_sha"] = self.base
        self.make_admission()
        result = self.plan()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already contains registry", result.stderr)

    def test_missing_review_and_wrong_subject_or_reviewer_are_rejected(self):
        original = json.loads(self.review_path.read_text())
        for patch in ({"subject_sha256": "0" * 64}, {"reviewer": "fixture-author"}, {"verdict": "reject"}):
            with self.subTest(patch=patch):
                review = dict(original, **patch)
                self.value["review"]["sha256"] = self.write_private(self.review_path, review)
                self.pin = self.write_private(self.artifact, self.value)
                self.assertNotEqual(self.plan().returncode, 0)
        self.review_path.unlink()
        self.assertNotEqual(self.plan().returncode, 0)

    def test_wrong_repository_or_base_are_rejected(self):
        for field, replacement in (("repository", {"root": "/wrong", "git_common_dir": "/wrong/.git"}),
                                   ("base_sha", "0" * 40)):
            previous = self.subject[field]
            self.subject[field] = replacement
            self.make_admission()
            self.assertNotEqual(self.plan().returncode, 0)
            self.subject[field] = previous

    def test_removed_or_same_id_weakened_jobs_are_rejected(self):
        variants = []
        removed = json.loads(json.dumps(self.registry)); removed["jobs"].pop(); variants.append(removed)
        for field, value in (("argv", [sys.executable, "-c", "print('weakened')"]),
                             ("required", False), ("critical", False), ("network", "required")):
            changed = json.loads(json.dumps(self.registry)); changed["jobs"][0][field] = value; variants.append(changed)
        for candidate in variants:
            with self.subTest(candidate=candidate):
                candidate.pop("registry_hash")
                self.repo.write_contract("registry.json", candidate)
                result = self.plan()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("preserve every admitted job", result.stderr)

    def test_run_requires_explicit_pin_and_rejects_changed_admission_or_review(self):
        path, _ = self.freeze()
        result = self.repo.cli(self.repo.dispatcher, "run", "--manifest", str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit admission", result.stderr)
        for target in (self.artifact, self.review_path):
            previous = target.read_bytes()
            target.write_bytes(previous + b" ")
            result = self.execute(path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA256 changed", result.stderr)
            target.write_bytes(previous)

    def test_run_rejects_repinning_and_changed_base_ref(self):
        path, _ = self.freeze()
        self.subject["author"] = "different-author"
        self.make_admission()
        result = self.execute(path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed after plan", result.stderr)
        git(self.repo.root, "tag", "baseline", self.base)
        self.base = "baseline"
        path, _ = self.freeze()
        git(self.repo.root, "tag", "-f", "baseline", "HEAD")
        result = self.execute(path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("base changed", result.stderr)

    def test_artifact_and_review_links_candidate_and_git_paths_are_rejected(self):
        for which in ("artifact", "review_path"):
            original = getattr(self, which)
            link = self.directory / f"{which}-link.json"
            link.symlink_to(original)
            setattr(self, which, link)
            if which == "review_path":
                self.value["review"]["path"] = str(link)
                self.pin = self.write_private(self.artifact, self.value)
            self.assertNotEqual(self.plan().returncode, 0)
            setattr(self, which, original)
            self.make_admission()
        original = self.artifact
        for directory in (self.repo.root, self.repo.root / ".git"):
            self.artifact = directory / "untrusted-admission.json"
            self.pin = self.write_private(self.artifact, self.value)
            result = self.plan()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("outside candidate", result.stderr)
        self.artifact = original

    def test_private_permissions_required(self):
        self.artifact.chmod(0o644)
        self.assertNotEqual(self.plan().returncode, 0)

    def test_fifo_evidence_is_rejected_without_blocking(self):
        self.artifact.unlink()
        os.mkfifo(self.artifact, 0o600)
        result = self.plan()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regular file", result.stderr)

    def test_stripping_admission_and_rehashing_cannot_bypass_base_requirement(self):
        path, manifest = self.freeze()
        manifest.pop("admission")
        manifest["manifest_hash"] = digest({k: v for k, v in manifest.items() if k not in {"created_at", "manifest_hash"}})
        path.write_bytes(canonical(manifest))
        result = self.repo.cli(self.repo.dispatcher, "run", "--manifest", str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trusted base registry unavailable", result.stderr)

    def test_additive_source_coverage_permitted_without_command_changes(self):
        candidate = json.loads(json.dumps(self.registry))
        candidate.pop("registry_hash")
        candidate["jobs"][0]["source_files"].append("additional_test.py")
        self.repo.write_contract("registry.json", candidate)
        result = self.plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        candidate["jobs"][0]["source_files"].remove("app.py")
        self.repo.write_contract("registry.json", candidate)
        self.assertNotEqual(self.plan().returncode, 0)

    def test_existing_baseline_allows_additive_coverage_and_rejects_command_change(self):
        base = git(self.repo.root, "rev-parse", "HEAD")
        candidate = json.loads(json.dumps(self.registry)); candidate.pop("registry_hash")
        candidate["jobs"][0]["source_files"].append("additional_test.py")
        self.repo.write_contract("registry.json", candidate)
        result = self.repo.cli(self.repo.dispatcher, "plan", "--mode", "release", "--base", base)
        self.assertEqual(result.returncode, 0, result.stderr)
        candidate["jobs"][0]["argv"] = [sys.executable, "-c", "print('weakened')"]
        self.repo.write_contract("registry.json", candidate)
        result = self.repo.cli(self.repo.dispatcher, "plan", "--mode", "release", "--base", base)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot change trusted-base", result.stderr)

    def test_review_change_during_execution_prevents_receipt(self):
        self.registry["jobs"][0]["argv"] = [sys.executable, "-c",
            "from pathlib import Path; p=Path(" + repr(str(self.review_path)) + "); p.write_bytes(p.read_bytes()+b' ')"]
        self.registry.pop("registry_hash")
        self.repo.write_contract("registry.json", self.registry)
        self.repo.commit_contract()
        self.make_admission()
        path, _ = self.freeze()
        result = self.execute(path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA256 changed", result.stderr)
        self.assertEqual(list((self.repo.root / ".claude/test-modes/receipts").glob("*.json")), [])

    def test_admission_receipt_cannot_authorize_protected_push(self):
        path, _ = self.freeze()
        result = self.execute(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        head = git(self.repo.root, "rev-parse", "HEAD")
        line = f"refs/heads/task {head} refs/heads/main {self.base}\n"
        result = self.repo.cli(self.repo.pre_push, "origin", "https://example.invalid/repo.git", stdin=line)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact clean local release receipt", result.stderr)

    def test_admission_manifest_cannot_be_used_by_ci_adapter(self):
        bundle = contracts.TestModesContract._ci_bundle(self)
        registry, context, manifest, receipt = bundle
        manifest["admission"] = {"authority": "operator-asserted-local-only"}
        manifest["manifest_hash"] = digest({k: v for k, v in manifest.items() if k not in {"created_at", "manifest_hash"}})
        receipt["manifest_hash"] = manifest["manifest_hash"]
        receipt["receipt_hash"] = digest({k: v for k, v in receipt.items() if k != "receipt_hash"})
        result = contracts.TestModesContract._ci_run(self, registry, context, manifest, receipt)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest", result.stderr)

    def test_project_precedence_and_no_installed_fallback(self):
        core = self.repo.module("test_modes_core")
        installed = self.directory / "installed"
        (installed / "test-contract").mkdir(parents=True)
        fake_module = installed / "scripts/test_modes_core.py"
        with mock.patch.object(core, "__file__", str(fake_module)):
            self.assertEqual(core.contract_dir(self.repo.root), self.repo.root / "templates/test-contract")
            external = self.directory / "external"
            external.mkdir()
            with self.assertRaises(core.ModeError): core.contract_dir(external)

    def test_source_impact_inventory_has_no_missing_job_ids(self):
        registry = json.loads((contracts.CONTRACT / "registry.json").read_text())
        ids = {job["id"] for job in registry["jobs"]}
        impact = json.loads((contracts.CONTRACT / "impact.json").read_text())
        for mapping in impact["mappings"]:
            self.assertLessEqual(set(mapping["test_ids"]), ids, mapping["path_prefix"])


if __name__ == "__main__":
    unittest.main()
