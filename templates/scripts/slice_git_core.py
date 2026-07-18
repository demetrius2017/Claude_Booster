"""Git snapshot and conservative attribution primitives for slice control.

Purpose: Capture stable Git/filesystem facts and classify changed paths against
an immutable slice baseline without inferring authorship.
Contract: Porcelain-v2 is parsed losslessly; anchors are sampled twice; content
reads are bounded and no-follow; missing evidence always lowers confidence.
CLI/Examples: Library module used by slice_git.py; it has no standalone CLI.
Limitations: Exact allowed paths only; no closure, commit authority, backlog,
telemetry, semantic classification, recursive filesystem traversal, or writes.
ENV/Files: No environment variables. Reads a supplied Git root and explicit
contract-relevant files; callers persist receipts under .claude/state.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 50 * 1024 * 1024
ZERO_OIDS = {"0" * 40, "0" * 64}
SECRET_NAMES = {".env", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


class GitFactError(Exception):
    """Typed git-fact failure with a stable CLI exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def canonical_path(raw: bytes) -> str:
    """Decode and validate one repository-relative porcelain path."""
    try:
        value = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise GitFactError("non-UTF8 git path is unsupported", 5) from exc
    if value.endswith("/"):
        value = value[:-1]
    path = PurePosixPath(value)
    if (
        not value or "\\" in value or "\x00" in value or path.is_absolute()
        or value != path.as_posix() or any(p in ("", ".", "..") for p in path.parts)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise GitFactError(f"non-canonical git path: {value!r}", 5)
    return value


def _entry(kind: str, path: str, fields: list[str], original: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": kind, "path": path, "original_path": original}
    if kind in {"1", "2"}:
        result.update(zip(("xy", "sub", "mode_head", "mode_index", "mode_worktree", "oid_head", "oid_index"), fields))
        if kind == "2":
            result["score"] = fields[7]
    elif kind == "u":
        result.update(zip(("xy", "sub", "mode_stage1", "mode_stage2", "mode_stage3", "mode_worktree", "oid_stage1", "oid_stage2", "oid_stage3"), fields))
    return result


def _ascii_fields(parts: list[bytes]) -> list[str]:
    try:
        return [part.decode("ascii", "strict") for part in parts]
    except UnicodeDecodeError as exc:
        raise GitFactError("non-ASCII porcelain metadata", 5) from exc


def parse_porcelain_v2(raw: bytes) -> list[dict[str, Any]]:
    """Parse `git status --porcelain=v2 -z` records without quoting loss."""
    if raw and not raw.endswith(b"\0"):
        raise GitFactError("truncated porcelain-v2 stream", 5)
    records = raw.split(b"\0")[:-1] if raw else []
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(records):
        record = records[index]
        if record.startswith(b"1 "):
            parts = record.split(b" ", 8)
            if len(parts) != 9:
                raise GitFactError("malformed ordinary porcelain record", 5)
            entries.append(_entry("1", canonical_path(parts[8]), _ascii_fields(parts[1:8])))
        elif record.startswith(b"2 "):
            parts = record.split(b" ", 9)
            if len(parts) != 10 or index + 1 >= len(records):
                raise GitFactError("malformed rename porcelain record", 5)
            index += 1
            entries.append(_entry("2", canonical_path(parts[9]), _ascii_fields(parts[1:9]), canonical_path(records[index])))
        elif record.startswith(b"u "):
            parts = record.split(b" ", 10)
            if len(parts) != 11:
                raise GitFactError("malformed unmerged porcelain record", 5)
            entries.append(_entry("u", canonical_path(parts[10]), _ascii_fields(parts[1:10])))
        elif record[:2] in {b"? ", b"! "}:
            entries.append(_entry(record[:1].decode(), canonical_path(record[2:]), []))
        elif record.startswith(b"# "):
            pass
        else:
            raise GitFactError("unknown porcelain-v2 record", 5)
        index += 1
    _validate_path_collisions(entries)
    return entries


def _validate_path_collisions(entries: list[dict[str, Any]]) -> None:
    identities: dict[str, str] = {}
    for entry in entries:
        for path in (entry["path"], entry.get("original_path")):
            if path is None:
                continue
            identity = unicodedata.normalize("NFC", path).casefold()
            previous = identities.get(identity)
            if previous is not None and previous != path:
                raise GitFactError(f"case/Unicode path collision: {previous!r}, {path!r}", 5)
            identities[identity] = path


def _git(root: Path, args: list[str], *, allow: tuple[int, ...] = (0,)) -> bytes:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitFactError(f"git unavailable: {exc}", 6) from exc
    if result.returncode not in allow:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GitFactError(f"git {' '.join(args)} failed: {detail}", 5)
    return result.stdout


def _anchors(root: Path) -> dict[str, str]:
    head = _git(root, ["rev-parse", "--verify", "HEAD"]).strip().decode("ascii")
    tree = _git(root, ["rev-parse", "--verify", "HEAD^{tree}"]).strip().decode("ascii")
    ref_raw = _git(root, ["symbolic-ref", "-q", "HEAD"], allow=(0, 1)).strip()
    index_raw = _git(root, ["ls-files", "--stage", "-z"])
    object_format = _git(root, ["rev-parse", "--show-object-format"]).strip().decode("ascii")
    expected = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
    if not expected or len(head) != expected or len(tree) != expected:
        raise GitFactError("unsupported or malformed git object format", 5)
    return {
        "head": head, "tree": tree, "ref": ref_raw.decode("utf-8", "strict") if ref_raw else "DETACHED",
        "index_sha256": hashlib.sha256(index_raw).hexdigest(), "object_format": object_format,
    }


def _status(root: Path) -> bytes:
    return _git(root, ["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching", "--ignore-submodules=none"])


def stable_git_state(root: Path) -> tuple[dict[str, str], bytes, list[dict[str, Any]]]:
    """Return anchors and repeated identical raw status, rejecting races."""
    before = _anchors(root)
    raw1 = _status(root)
    raw2 = _status(root)
    after = _anchors(root)
    if before != after or raw1 != raw2:
        raise GitFactError("concurrent git snapshot mismatch", 3)
    return before, raw1, parse_porcelain_v2(raw1)


def _secret(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name in SECRET_NAMES or name.startswith(".env.") or PurePosixPath(name).suffix in SECRET_SUFFIXES


def file_fact(root: Path, path: str, budget: list[int]) -> dict[str, Any]:
    """Read one exact path with component-wise openat/no-follow guards."""
    parts = PurePosixPath(path).parts
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError:
                return {"kind": "unknown", "size": None, "hash_status": "unsafe_ancestor", "sha256": None, "symlink_target_sha256": None}
            os.close(dir_fd)
            dir_fd = next_fd
        name = parts[-1]
        try:
            before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"kind": "absent", "size": 0, "hash_status": "absent", "sha256": None, "symlink_target_sha256": None}
        common = {"size": before.st_size, "sha256": None, "symlink_target_sha256": None}
        if stat.S_ISLNK(before.st_mode):
            target = os.readlink(name, dir_fd=dir_fd).encode("utf-8", "surrogateescape")
            return {**common, "kind": "symlink", "hash_status": "unsafe_symlink", "symlink_target_sha256": hashlib.sha256(target).hexdigest()}
        if not stat.S_ISREG(before.st_mode):
            return {**common, "kind": "special" if not stat.S_ISDIR(before.st_mode) else "directory", "hash_status": "unsupported_kind"}
        if _secret(path):
            return {**common, "kind": "regular", "hash_status": "sensitive_skipped"}
        if before.st_size > MAX_FILE or budget[0] + before.st_size > MAX_TOTAL:
            return {**common, "kind": "regular", "hash_status": "too_large"}
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError:
            return {**common, "kind": "regular", "hash_status": "permission_denied"}
        digest = hashlib.sha256()
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size) or opened.st_nlink != 1:
                return {**common, "kind": "regular", "hash_status": "changed_during_read"}
            while chunk := os.read(fd, 65536):
                digest.update(chunk)
            after = os.fstat(fd)
            path_after = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if (
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                or (path_after.st_dev, path_after.st_ino) != (after.st_dev, after.st_ino)
            ):
                return {**common, "kind": "regular", "hash_status": "changed_during_read"}
        finally:
            os.close(fd)
        budget[0] += before.st_size
        return {**common, "kind": "regular", "hash_status": "hashed", "sha256": digest.hexdigest()}
    finally:
        os.close(dir_fd)


def _object_fact(root: Path, path: str) -> dict[str, Any]:
    """Return HEAD and all index-stage object facts for one exact path."""
    head_raw = _git(root, ["rev-parse", "--verify", f"HEAD:{path}"], allow=(0, 128)).strip()
    head_blob = head_raw.decode("ascii") if head_raw else None
    index_raw = _git(root, ["ls-files", "--stage", "-z", "--", path])
    stages: list[dict[str, str]] = []
    for record in index_raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split(" ")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitFactError("malformed index stage record", 5) from exc
        if canonical_path(raw_path) != path:
            raise GitFactError("index path identity mismatch", 5)
        stages.append({"mode": mode, "oid": oid, "stage": stage})
    return {"head_blob": head_blob, "index_stages": stages}


def snapshot(root: Path, allowed: list[str]) -> dict[str, Any]:
    anchors, raw, entries = stable_git_state(root)
    _validate_path_collisions([{"path": path} for path in allowed] + entries)
    relevant = set(allowed)
    for entry in entries:
        if entry["path"] in relevant or entry.get("original_path") in relevant:
            relevant.add(entry["path"])
            if entry.get("original_path"):
                relevant.add(entry["original_path"])
    budget = [0]
    facts = {path: {**file_fact(root, path, budget), **_object_fact(root, path)} for path in sorted(relevant)}
    end = _anchors(root)
    if end != anchors or _status(root) != raw:
        raise GitFactError("filesystem changed during scoped hashing", 3)
    return {"anchors": anchors, "porcelain_v2_sha256": hashlib.sha256(raw).hexdigest(), "entries": entries, "scoped_facts": facts}


def _entry_map(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for path in (entry["path"], entry.get("original_path")):
            if path:
                result[path] = entry
    return result


def classify(baseline: dict[str, Any], current: dict[str, Any], allowed: list[str]) -> list[dict[str, Any]]:
    """Exhaustively classify baseline/current changed paths without authorship claims."""
    base_entries, now_entries = _entry_map(baseline["entries"]), _entry_map(current["entries"])
    paths = set(base_entries) | set(now_entries)
    for path in allowed:
        if baseline["scoped_facts"].get(path) != current["scoped_facts"].get(path):
            paths.add(path)
    anchor_mismatch = baseline["anchors"] != current["anchors"]
    output: list[dict[str, Any]] = []
    for path in sorted(paths):
        base_entry, now_entry = base_entries.get(path), now_entries.get(path)
        base_fact, now_fact = baseline["scoped_facts"].get(path), current["scoped_facts"].get(path)
        protected = path == ".git" or path.startswith(".git/") or path == ".claude" or path == ".claude/state" or path.startswith(".claude/state/")
        reasons: list[str] = []
        if protected:
            classification, reasons = "foreign", ["reserved_control_state"]
        elif path not in allowed:
            classification, reasons = "off-scope", ["outside_artifact_contract"]
        elif anchor_mismatch:
            classification, reasons = "ambiguous", ["head_tree_or_index_mismatch"]
        elif base_entry is not None:
            if base_entry == now_entry and base_fact == now_fact:
                classification, reasons = "foreign", ["baseline_dirty_unchanged"]
            else:
                classification, reasons = "ambiguous", ["baseline_dirty_changed"]
        elif now_entry is None and base_fact == now_fact:
            continue
        elif now_entry and (now_entry["kind"] in {"u", "!"} or now_entry.get("sub", "N...") != "N..."):
            classification, reasons = "ambiguous", ["unmerged_ignored_or_submodule"]
        elif not now_fact or now_fact["hash_status"] not in {"hashed", "absent"}:
            classification, reasons = "ambiguous", ["content_evidence_unknown"]
        else:
            classification, reasons = "candidate-owned", ["allowed_clean_baseline_change"]
        output.append({"path": path, "classification": classification, "reasons": reasons, "baseline": {"entry": base_entry, "fact": base_fact}, "current": {"entry": now_entry, "fact": now_fact}})
    # A rename is atomic: an off-scope endpoint makes the allowed endpoint ambiguous.
    by_path = {item["path"]: item for item in output}
    for entry in current["entries"]:
        if entry["kind"] == "2" and entry["original_path"]:
            pair = [entry["path"], entry["original_path"]]
            if not all(path in allowed for path in pair):
                for path in pair:
                    if path in allowed and path in by_path:
                        by_path[path]["classification"] = "ambiguous"
                        by_path[path]["reasons"] = ["rename_crosses_contract_boundary"]
    return output


def attribution_receipt(ledger: dict[str, Any], baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Build deterministic, disjoint changed-path facts and exact state hash."""
    classifications = classify(baseline["git"], current, ledger["allowed_paths"])
    paths = [item["path"] for item in classifications]
    if len(paths) != len(set(paths)) or any(item["classification"] not in {"candidate-owned", "foreign", "ambiguous", "off-scope"} for item in classifications):
        raise GitFactError("classifications are not exhaustive and disjoint", 5)
    state = {
        "schema_version": 1, "run_id": ledger["run_id"], "slice_id": ledger["slice_id"],
        "baseline_sha256": ledger["baseline_sha256"],
        "artifact_contract_sha256": hashlib.sha256(ledger["artifact_contract"].encode()).hexdigest(),
        "allowed_paths": ledger["allowed_paths"], "anchors": current["anchors"],
        "porcelain_v2_sha256": current["porcelain_v2_sha256"],
        "scoped_facts": current["scoped_facts"], "classifications": classifications,
    }
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    state_sha256 = hashlib.sha256(canonical(state)).hexdigest()
    receipt = {**state, "ledger_event_hash": ledger["last_event_hash"], "state_sha256": state_sha256}
    receipt["attribution_sha256"] = hashlib.sha256(canonical(receipt)).hexdigest()
    return receipt
