#!/usr/bin/env python3
"""Typed, fail-closed primitives for managed development/release test modes.

Purpose: validate contracts, identity, leases, manifests, receipts and ledger data.
Contract: all public functions accept explicit project paths and raise ModeError on
invalid input; canonical JSON is SHA-256 hashed and state writes are atomic 0600.
CLI/Examples: library used by test_dispatcher.py and phase.py.
Limitations: local receipts are evidence only and never trusted-CI authorization.
ENV/Files: reads Git and templates/test-contract; writes .claude/test-modes only.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import stat
import subprocess
import tempfile
import fcntl
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = 1
ZERO = {"0" * 40, "0" * 64}
PHASES = {"RECON", "PLAN", "IMPLEMENT", "AUDIT", "VERIFY", "MERGE"}
DEV_PHASES = {"PLAN", "IMPLEMENT", "AUDIT"}
RELEASE_PHASES = {"VERIFY", "MERGE"}


class ModeError(Exception):
    """A validation error which must cause managed execution to fail closed."""


def canonical(value: Any) -> bytes:
    """Return the sole serialized form used for all identity hashes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ModeError("timestamp must be UTC RFC3339 ending Z")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ModeError("invalid timestamp") from exc
    return parsed


def project_root(raw: str | Path) -> Path:
    root = Path(raw).resolve(strict=True)
    if not (root / ".git").exists():
        raise ModeError("project root must contain .git")
    return root


def _git(root: Path, args: list[str], allow: tuple[int, ...] = (0,)) -> str:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "LANG": "C", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ModeError(f"git execution failed: {exc}") from exc
    if result.returncode not in allow:
        raise ModeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def candidate_identity(root: Path) -> dict[str, Any]:
    """Capture a stable identity including tracked and untracked worktree state."""
    root = project_root(root)
    head = _git(root, ["rev-parse", "--verify", "HEAD"])
    tree = _git(root, ["rev-parse", "--verify", "HEAD^{tree}"])
    index = _git(root, ["ls-files", "--stage", "-z"])
    status = _git(root, ["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=none"])
    common_raw = Path(_git(root, ["rev-parse", "--git-common-dir"]))
    common = (common_raw if common_raw.is_absolute() else root / common_raw).resolve()
    second = _git(root, ["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=none"])
    if status != second:
        raise ModeError("worktree changed while candidate identity was sampled")
    dirty = bool(status)
    return {"schema_version": SCHEMA, "root": str(root), "git_common_dir": str(common), "head": head, "tree": tree,
            "index_sha256": hashlib.sha256(index.encode()).hexdigest(), "worktree_sha256": hashlib.sha256(status.encode()).hexdigest(),
            "untracked_sha256": hashlib.sha256("\n".join(sorted(x[2:] for x in status.split("\0") if x.startswith("? "))).encode()).hexdigest(), "dirty": dirty}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically write private state, rejecting links and group-readable files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)):
        raise ModeError(f"unsafe state path: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical(value)); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    except Exception:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise


def read_json(path: Path) -> dict[str, Any]:
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise ModeError("unsafe state file")
        with path.open("rb") as handle: value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc: raise ModeError(f"invalid JSON state {path}") from exc
    if not isinstance(value, dict): raise ModeError("state must be object")
    return value


def lease_path(root: Path) -> Path: return root / ".claude" / "phase_lease.json"


def create_lease(root: Path, phase: str, session_id: str | None = None, ttl_seconds: int = 7200) -> dict[str, Any]:
    root = project_root(root); identity = candidate_identity(root)
    if not isinstance(phase, str): raise ModeError("invalid lease phase")
    phase = phase.upper()
    if phase not in PHASES or not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 86400: raise ModeError("invalid lease phase or TTL")
    start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    lease = {"schema_version": SCHEMA, "phase": phase, "created_at": start.isoformat().replace("+00:00", "Z"),
             "expires_at": (start + dt.timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z"),
             "session_id": session_id or os.environ.get("CLAUDE_SESSION_ID") or secrets.token_urlsafe(18), "run_id": secrets.token_urlsafe(18),
             "root": identity["root"], "git_common_dir": identity["git_common_dir"], "issuer": "claude-booster-test-modes", "issuer_version": SCHEMA}
    atomic_json(lease_path(root), lease); return lease


def validate_lease(root: Path, value: dict[str, Any], session_id: str | None = None, future_skew_seconds: int = 300) -> dict[str, Any]:
    required = {"schema_version", "phase", "created_at", "expires_at", "session_id", "run_id", "root", "git_common_dir", "issuer", "issuer_version"}
    if set(value) != required or value["schema_version"] != SCHEMA or value["phase"] not in PHASES: raise ModeError("lease schema invalid")
    created, expires, present = parse_time(value["created_at"]), parse_time(value["expires_at"]), dt.datetime.now(dt.timezone.utc)
    identity = candidate_identity(root)
    if created > present + dt.timedelta(seconds=future_skew_seconds) or expires <= present or expires <= created: raise ModeError("lease is expired or time-invalid")
    if value["root"] != identity["root"] or value["git_common_dir"] != identity["git_common_dir"]: raise ModeError("lease root binding mismatch")
    if session_id and value["session_id"] != session_id: raise ModeError("lease session binding mismatch")
    if not all(isinstance(value[x], str) and value[x] for x in ("session_id", "run_id", "issuer")): raise ModeError("lease identity invalid")
    return value


def resolve_mode(*, requested: str | None, trusted_ci: bool, protected_destination: bool, lease: dict[str, Any] | None) -> str:
    """Pure precedence resolver; invalid/missing context always resolves release."""
    if requested not in (None, "development", "release"): raise ModeError("invalid requested mode")
    if trusted_ci or protected_destination or requested == "release": return "release"
    if lease is None: return "release"
    return "release" if lease["phase"] in RELEASE_PHASES else "development" if lease["phase"] in DEV_PHASES else "release"


def _rel(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value: raise ModeError("invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(x in (".", "..", "") for x in path.parts): raise ModeError("invalid relative path")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ModeError(f"invalid {field}")
    return value


def _identity(value: Any, field: str = "identity") -> str:
    value = _text(value, field)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ModeError(f"invalid {field}")
    return value


def validate_registry(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"schema_version", "jobs", "registry_hash"}: raise ModeError("invalid test registry object")
    supplied_hash = value.get("registry_hash")
    if value.get("schema_version") != SCHEMA or not isinstance(value.get("jobs"), list) or not value["jobs"]: raise ModeError("invalid test registry")
    seen: set[str] = set()
    for job in value["jobs"]:
        required = {"id", "argv", "cwd", "env", "components", "test_class", "platform", "network", "state_scope", "timeout_seconds", "critical", "required", "source_files"}
        optional = {"expected_duration_seconds", "stratum"}
        if not isinstance(job, dict) or not required <= set(job) or set(job) - required - optional: raise ModeError("invalid registry job schema")
        if not isinstance(job["id"], str) or job["id"] in seen or not isinstance(job["argv"], list) or not job["argv"] or not all(isinstance(x, str) and x and "\x00" not in x for x in job["argv"]): raise ModeError("invalid registry job identity")
        seen.add(job["id"]); _rel(job["cwd"])
        if not isinstance(job["env"], dict) or not all(isinstance(k, str) and k and "\x00" not in k and "=" not in k and isinstance(v, str) and "\x00" not in v for k, v in job["env"].items()): raise ModeError("invalid registry environment")
        if not all(isinstance(job[k], list) and all(isinstance(x, str) and x for x in job[k]) for k in ("components", "source_files")): raise ModeError("invalid registry list field")
        if not isinstance(job["test_class"], str) or not job["test_class"] or not isinstance(job["state_scope"], str) or not job["state_scope"]: raise ModeError("invalid registry classification")
        if "stratum" in job and (not isinstance(job["stratum"], str) or not job["stratum"] or "\x00" in job["stratum"]): raise ModeError("invalid registry stratum")
        for source in job["source_files"]: _rel(source)
        if not isinstance(job["critical"], bool) or not isinstance(job["required"], bool): raise ModeError("invalid registry boolean")
        if not isinstance(job["timeout_seconds"], int) or not 1 <= job["timeout_seconds"] <= 3600 or job["platform"] not in {"any", "darwin", "linux"} or job["network"] not in {"forbidden", "isolated", "required"}: raise ModeError("invalid registry execution policy")
        if "expected_duration_seconds" in job and (not isinstance(job["expected_duration_seconds"], int) or job["expected_duration_seconds"] < 1): raise ModeError("invalid expected duration")
        job.setdefault("stratum", job["test_class"])
    body = {k: v for k, v in value.items() if k != "registry_hash"}
    computed = digest(body)
    if supplied_hash is not None and supplied_hash != computed: raise ModeError("registry hash mismatch")
    body["registry_hash"] = computed
    return body


def contract_dir(root: Path) -> Path:
    """Locate installed contracts first, with repository-template fallback."""
    installed = Path(__file__).resolve().parent.parent / "test-contract"
    if installed.is_dir(): return installed
    source = root / "templates" / "test-contract"
    if source.is_dir(): return source
    raise ModeError("versioned test-contract directory unavailable")


def load_registry(root: Path, contract: Path | None = None) -> dict[str, Any]:
    """Load and validate the candidate registry from a non-link JSON file."""
    path = contract or contract_dir(root) / "registry.json"
    return validate_registry(read_json(path))


def load_registry_ref(root: Path, ref: str) -> dict[str, Any]:
    """Load a trusted base registry directly from Git, never candidate disk."""
    try: value = json.loads(_git(root, ["show", f"{ref}:templates/test-contract/registry.json"]))
    except (json.JSONDecodeError, ModeError) as exc: raise ModeError("trusted base registry unavailable") from exc
    if not isinstance(value, dict): raise ModeError("trusted base registry invalid")
    return validate_registry(value)


def load_impact(root: Path, contract: Path | None = None) -> dict[str, Any]:
    value = read_json(contract or contract_dir(root) / "impact.json")
    if set(value) != {"schema_version", "mappings"} or value.get("schema_version") != SCHEMA or not isinstance(value.get("mappings"), list): raise ModeError("invalid impact registry")
    for item in value["mappings"]:
        if not isinstance(item, dict) or set(item) != {"path_prefix", "test_ids", "reviewed_at"} or not isinstance(item["test_ids"], list) or not item["test_ids"] or len(item["test_ids"]) != len(set(item["test_ids"])) or not all(isinstance(x, str) and x for x in item["test_ids"]): raise ModeError("invalid impact mapping")
        _rel(item["path_prefix"])
        _text(item["reviewed_at"], "impact review date")
    return value


def load_critical(root: Path) -> set[str]:
    """Load the separately reviewed critical-smoke registry."""
    value = read_json(contract_dir(root) / "critical_smoke.json")
    if set(value) != {"schema_version", "tests"} or value.get("schema_version") != SCHEMA or not isinstance(value.get("tests"), list): raise ModeError("invalid critical-smoke registry")
    ids: set[str] = set()
    for item in value["tests"]:
        if not isinstance(item, dict) or set(item) != {"id", "owner", "rationale", "reviewed_at"} or not all(isinstance(item[x], str) and item[x] for x in item): raise ModeError("invalid critical-smoke entry")
        ids.add(item["id"])
    return ids


def changed_paths(root: Path, base: str) -> set[str]:
    raw = _git(root, ["diff", "--name-only", "--no-renames", "-z", base])
    untracked = _git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    return {_rel(path) for path in (raw + untracked).split("\0") if path}


def resolve_git_oid(root: Path, ref: str) -> str:
    """Resolve a revision to an immutable commit object ID."""
    if not isinstance(ref, str) or not ref or ref.startswith("-"): raise ModeError("invalid Git revision")
    oid = _git(project_root(root), ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    if len(oid) not in (40, 64) or any(char not in "0123456789abcdef" for char in oid): raise ModeError("invalid resolved Git object ID")
    return oid


def deterministic_sample(jobs: list[dict[str, Any]], seed: str, percent: int = 10) -> list[str]:
    if not isinstance(seed, str) or not seed or not isinstance(percent, int) or not 0 <= percent <= 100: raise ModeError("invalid sampling parameters")
    strata: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        stratum = job.get("stratum")
        if not isinstance(stratum, str) or not stratum: raise ModeError("job lacks declared stratum")
        strata.setdefault(stratum, []).append(job)
    total = len(jobs)
    quota = 0 if total == 0 or percent == 0 else max(1, (total * percent + 99) // 100)
    if quota >= total:
        return sorted(job["id"] for job in jobs)

    # One global quota prevents many tiny strata from rounding a 10% sample
    # into the full suite.  A deterministic round-robin preserves breadth for
    # as many strata as the quota can represent, then adds depth evenly.
    ordered: dict[str, list[dict[str, Any]]] = {}
    for key, population in strata.items():
        ordered[key] = sorted(
            population,
            key=lambda item: (hashlib.sha256(f"{seed}:{item['id']}".encode()).hexdigest(), item["id"]),
        )
    stratum_order = sorted(
        ordered,
        key=lambda key: (hashlib.sha256(f"{seed}:stratum:{key}".encode()).hexdigest(), key),
    )
    chosen: list[str] = []
    depth = 0
    while len(chosen) < quota:
        added = False
        for key in stratum_order:
            if depth < len(ordered[key]):
                chosen.append(ordered[key][depth]["id"])
                added = True
                if len(chosen) == quota:
                    break
        if not added:
            raise ModeError("sampling quota exceeds eligible population")
        depth += 1
    if len(chosen) != quota or len(set(chosen)) != quota:
        raise ModeError("sampling output invariant violated")
    return sorted(chosen)


def selection(root: Path, registry: dict[str, Any], impact: dict[str, Any], base: str, unresolved: set[str], epoch: str, full: bool, trusted_registry: dict[str, Any] | None = None, critical_smoke: set[str] | None = None) -> list[str]:
    ids = {x["id"] for x in registry["jobs"]}; changed = changed_paths(root, base)
    affected: set[str] = set(); matched: set[str] = set()
    for path in changed:
        for mapping in impact["mappings"]:
            if path == mapping["path_prefix"] or path.startswith(mapping["path_prefix"].rstrip("/") + "/"):
                matched.add(path); affected.update(mapping["test_ids"])
    if not full and changed and matched != changed: raise ModeError("unknown changed path requires release")
    if not (affected | unresolved) <= ids: raise ModeError("selection refers to missing test ID")
    smoke = critical_smoke or set()
    if not smoke <= ids: raise ModeError("critical-smoke registry refers to missing test ID")
    mandatory = affected | unresolved | smoke | {x["id"] for x in registry["jobs"] if x["critical"]}
    contract_changed = any(path.startswith("templates/test-contract/") or path in {"templates/scripts/test_modes_core.py", "templates/scripts/test_dispatcher.py"} or path.endswith((".lock", ".yaml", ".yml")) for path in changed)
    if contract_changed:
        if trusted_registry is None: raise ModeError("contract change requires trusted-base registry union")
        base_ids = {x["id"] for x in trusted_registry["jobs"]}
        if not base_ids <= ids: raise ModeError("candidate registry cannot remove trusted-base required jobs")
        return sorted(ids)  # mode is already release; candidate full registry is required.
    if full: return sorted(ids)
    candidate = candidate_identity(root)
    seed = digest({"base": base, "candidate": digest(candidate), "registry": registry["registry_hash"], "epoch": epoch})
    eligible = [x for x in registry["jobs"] if x["id"] not in mandatory]
    return sorted(mandatory | set(deterministic_sample(eligible, seed)))


def _ledger_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists(): return []
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600: raise ModeError("failure ledger permissions or type invalid")
    records: list[dict[str, Any]] = []
    previous = "0" * 64
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            required = {"schema_version", "timestamp", "previous_hash", "type", "test_id", "command_identity", "contract_identity", "hash"}
            if not isinstance(item, dict) or not required <= set(item) or item.get("schema_version") != SCHEMA: raise ModeError("failure ledger record invalid")
            if item["type"] not in {"failure", "closure"}: raise ModeError("failure ledger event type invalid")
            _text(item["test_id"], "ledger test id"); _identity(item["command_identity"], "command identity"); _identity(item["contract_identity"], "contract identity"); parse_time(item["timestamp"])
            if item["type"] == "failure" and set(item) != required | {"candidate", "signature"}: raise ModeError("failure ledger failure event invalid")
            if item["type"] == "closure" and (set(item) - required != {"failure_hash", "status"} or item.get("status") != "green"): raise ModeError("failure ledger closure event invalid")
            if item["type"] == "failure": _identity(item["signature"], "failure signature")
            else: _identity(item["failure_hash"], "failure hash")
            expected = digest({k: v for k, v in item.items() if k != "hash"})
            if item.get("previous_hash") != previous or item.get("hash") != expected: raise ModeError("failure ledger chain corrupt")
            previous = item["hash"]; records.append(item)
    except (OSError, json.JSONDecodeError) as exc: raise ModeError("failure ledger unreadable") from exc
    return records


def append_failure(root: Path, event: dict[str, Any]) -> str:
    """Serialize and append one validated hash-chained private ledger event."""
    root = project_root(root)
    required = {"type", "test_id", "command_identity", "contract_identity"}
    if not isinstance(event, dict) or not required <= set(event): raise ModeError("invalid failure ledger event")
    _text(event["test_id"], "ledger test id"); _identity(event["command_identity"], "command identity"); _identity(event["contract_identity"], "contract identity")
    if event["type"] == "failure":
        if set(event) != required | {"candidate", "signature"}: raise ModeError("invalid failure event schema")
        _identity(event["signature"], "failure signature")
    elif event["type"] == "closure":
        if set(event) != required | {"failure_hash", "status"} or event["status"] != "green": raise ModeError("invalid closure event schema")
        _identity(event["failure_hash"], "failure hash")
    else: raise ModeError("invalid failure event type")
    path = root / ".claude" / "test-modes" / "failure-ledger.jsonl"; path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(lock_fd, 0o600); fcntl.flock(lock_fd, fcntl.LOCK_EX)
        records = _ledger_records(path); previous = records[-1]["hash"] if records else "0" * 64
        record = {"schema_version": SCHEMA, "timestamp": now(), "previous_hash": previous, **event}; record["hash"] = digest(record)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try: os.fchmod(fd, 0o600); os.write(fd, canonical(record) + b"\n"); os.fsync(fd)
        finally: os.close(fd)
        return record["hash"]
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)


def unresolved_failures(root: Path) -> set[str]:
    """Return active ledger failures; only a current-green closure can remove one."""
    path = root / ".claude" / "test-modes" / "failure-ledger.jsonl"
    return {key[0] for key in unresolved_failure_records(root)}


def unresolved_failure_records(root: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Return validated active failures for success closure recording."""
    path = root / ".claude" / "test-modes" / "failure-ledger.jsonl"
    active: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in _ledger_records(path):
        key = (item["test_id"], item["command_identity"], item["contract_identity"])
        if item["type"] == "failure": active[key] = item
        elif key in active and item["failure_hash"] == active[key]["hash"]: del active[key]
    return active
