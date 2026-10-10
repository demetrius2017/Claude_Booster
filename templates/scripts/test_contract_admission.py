#!/usr/bin/env python3
"""Explicit local first-trust assertions for projects without a base registry.

Purpose: bind a reviewed baseline registry to operator-pinned external bytes.
Contract: load_admission returns registry plus frozen identity or raises ModeError.
CLI/Examples: dispatcher plan/run --admission /private/admission.json
    --admission-sha256 <operator-provided SHA256>.
Limitations: assertions are NOT authenticated reviewer/owner identity; the operator
must independently establish authority and review. No creation or CI promotion.
ENV/Files: reads Git, external private admission and review files; writes nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from test_modes_core import ModeError, _git, digest, preserve_registry_jobs, project_root, resolve_git_oid, validate_registry


def _sha(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ModeError("admission requires exact SHA256 pin")
    return value


def _private_json(path: Path, pin: str, excluded: tuple[Path, ...]):
    """Read pinned bytes from a private non-link path outside candidate/Git state."""
    _sha(pin)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ModeError("admission evidence path must be absolute without links")
    if any(path.is_relative_to(parent) for parent in excluded):
        raise ModeError("admission evidence must be outside candidate and Git directories")
    preliminary = path.lstat()
    if not stat.S_ISREG(preliminary.st_mode): raise ModeError("admission evidence must be a regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (info.st_dev, info.st_ino) != (preliminary.st_dev, preliminary.st_ino):
            raise ModeError("admission evidence changed during open")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ModeError("admission evidence must be owned regular mode-0600 file")
        raw = handle.read()
    if hashlib.sha256(raw).hexdigest() != pin:
        raise ModeError("admission evidence SHA256 changed")
    value = json.loads(raw)
    if not isinstance(value, dict): raise ModeError("admission evidence must be an object")
    return value


def load_admission(root: Path, base: str, path: Path, pin: str):
    """Validate an explicit operator assertion, never infer first trust from HEAD."""
    root = project_root(root)
    common = Path(_git(root, ["rev-parse", "--git-common-dir"]))
    common = (common if common.is_absolute() else root / common).resolve(strict=True)
    baseline = resolve_git_oid(root, base)
    if _git(root, ["ls-tree", baseline, "--", "templates/test-contract/registry.json"]):
        raise ModeError("admission forbidden when baseline already contains registry")
    excluded = (root, common)
    value = _private_json(path, pin, excluded)
    keys = {"schema_version", "kind", "repository", "base_sha", "registry", "author", "review"}
    if set(value) != keys or value["schema_version"] != 1 or value["kind"] != "local-first-trust-assertion":
        raise ModeError("invalid admission schema")
    if value["repository"] != {"root": str(root), "git_common_dir": str(common)} or value["base_sha"] != baseline:
        raise ModeError("admission repository or original baseline mismatch")
    if not isinstance(value["author"], str) or not value["author"].strip():
        raise ModeError("admission author missing")
    review_ref = value["review"]
    if not isinstance(review_ref, dict) or set(review_ref) != {"path", "sha256"} or not isinstance(review_ref["path"], str):
        raise ModeError("admission review binding missing")
    review_path = Path(review_ref["path"])
    review = _private_json(review_path, review_ref["sha256"], excluded)
    subject = digest({k: v for k, v in value.items() if k != "review"})
    review_keys = {"schema_version", "subject_sha256", "reviewer", "verdict", "scope"}
    if set(review) != review_keys or review["schema_version"] != 1 or review["subject_sha256"] != subject:
        raise ModeError("review does not bind exact admission subject")
    if (not isinstance(review["reviewer"], str) or not review["reviewer"].strip()
            or review["reviewer"].strip() == value["author"].strip() or review["verdict"] != "accept"
            or review["scope"] != "complete-baseline-job-policy-inventory"):
        raise ModeError("independent inventory review assertion missing")
    registry = validate_registry(value["registry"])
    binding = {"path": str(path), "sha256": pin, "review": review_ref, "subject_sha256": subject,
               "repository": value["repository"], "base_sha": baseline, "registry_hash": registry["registry_hash"],
               "authority": "operator-asserted-local-only"}
    return registry, binding


def preserve_jobs(candidate: dict, admitted: dict):
    """Preserve admitted execution/policy and every declared source dependency."""
    try:
        preserve_registry_jobs(candidate, admitted)
    except ModeError as exc:
        raise ModeError("candidate must preserve every admitted job definition: " + str(exc)) from exc
