#!/usr/bin/env python3
"""Immutable Git baseline and conservative slice attribution CLI.

Purpose: Bind stable Git facts to an active slice ledger and later classify
changed paths without treating compatibility as proof of authorship.
Contract: Requires exact run/session/revision/event-hash identity, one immutable
mode-0600 baseline receipt, and the shared stable slice lock.
CLI/Examples: ``slice_git.py --cwd ROOT capture|attribute --run-id ID
--session-id ID --revision N``; successful output is typed JSON.
Limitations: No staging, commit authority, closure, backlog, telemetry, semantic
scope inference, hooks, integration, or automatic remediation.
ENV/Files: No environment variables. Reads the slice ledger/event log and writes
only ``<git-root>/.claude/state/runs/<run-hash>/slice_baseline.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from slice_git_core import GitFactError, attribution_receipt, classify, snapshot
from slice_ledger import _bind_baseline, _git_root, _locked
from slice_ledger_core import LedgerError, _append, _atomic_projection, _canonical, _failed_verification_binding, _load, _now, _validate_relpath

OK, USAGE, CONFLICT, CORRUPT, UNSUPPORTED, IO_ERROR = 0, 2, 3, 4, 5, 6
RECEIPT_KEYS = {
    "schema_version", "run_id", "slice_id", "ledger_revision", "ledger_event_hash",
    "artifact_contract_sha256", "allowed_paths", "captured_at", "git",
}
REFRESH_KEYS = RECEIPT_KEYS | {"generation", "lineage", "state_sha256"}


class Parser(argparse.ArgumentParser):
    """Argument parser emitting the shared typed error contract."""

    def error(self, message: str) -> None:
        raise GitFactError(message, USAGE)


def _positive(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = Parser(description=__doc__)
    parser.add_argument("--cwd", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "attribute", "refresh"):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)
        command.add_argument("--session-id", required=True)
        command.add_argument("--revision", type=_positive, required=True)
    return parser


def _emit(ok: bool, kind: str, *, stream: Any = sys.stdout, **values: Any) -> None:
    print(json.dumps({"ok": ok, "type": kind, **values}, sort_keys=True, separators=(",", ":")), file=stream)


def _ledger(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    state_dir = root / ".claude" / "state"
    ledger, events = state_dir / "slice_ledger.json", state_dir / "slice_events.jsonl"
    state, _ = _load(ledger, events)
    if state is None or state["state"] != "active":
        raise GitFactError("active slice ledger required", CONFLICT)
    if (
        state["run_id"] != args.run_id or state["owner"]["session_id"] != args.session_id
        or state["revision"] != args.revision
    ):
        raise GitFactError("run/session/revision guard conflict", CONFLICT)
    return state


def _run_dir(root: Path, run_id: str) -> Path:
    directory = root / ".claude/state/runs" / hashlib.sha256(run_id.encode()).hexdigest()
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise GitFactError("run state directory is unsafe", CORRUPT)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _receipt_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "slice_baseline.json"


def _latest_receipt_path(root: Path, ledger: dict[str, Any]) -> Path:
    return root / ledger["baseline_path"] if ledger.get("baseline_path") else _receipt_path(root,ledger["run_id"])


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _validate_receipt_file(path: Path) -> None:
    if path.is_symlink():
        raise GitFactError("baseline receipt symlink forbidden", CORRUPT)
    if path.exists():
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
            raise GitFactError("baseline receipt must be regular, single-link, mode 0600", CORRUPT)


def _read_receipt(path: Path) -> dict[str, Any]:
    _validate_receipt_file(path)
    if not path.exists():
        raise GitFactError("slice baseline not captured", CONFLICT)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened, current = os.fstat(fd), os.stat(path, follow_symlinks=False)
        if opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise GitFactError("baseline receipt inode/path mismatch", CORRUPT)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        value = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitFactError("corrupt baseline receipt JSON", CORRUPT) from exc
    if (
        not isinstance(value, dict) or frozenset(value) not in {frozenset(RECEIPT_KEYS),frozenset(REFRESH_KEYS)}
        or not isinstance(value["schema_version"], int) or isinstance(value["schema_version"], bool)
        or value["schema_version"] != 1
    ):
        raise GitFactError("baseline receipt schema mismatch", CORRUPT)
    if not isinstance(value["ledger_revision"], int) or isinstance(value["ledger_revision"], bool) or value["ledger_revision"] < 1:
        raise GitFactError("invalid baseline revision", CORRUPT)
    for key in ("run_id", "slice_id", "ledger_event_hash", "artifact_contract_sha256", "captured_at"):
        if not isinstance(value[key], str) or not value[key]:
            raise GitFactError(f"invalid baseline {key}", CORRUPT)
    if (
        not isinstance(value["allowed_paths"], list)
        or [_validate_relpath(path) for path in value["allowed_paths"]] != value["allowed_paths"]
        or value["allowed_paths"] != sorted(set(value["allowed_paths"]))
    ):
        raise GitFactError("invalid baseline allowed_paths", CORRUPT)
    if not isinstance(value["git"], dict):
        raise GitFactError("invalid baseline git snapshot", CORRUPT)
    if set(value) == REFRESH_KEYS and (not isinstance(value["generation"],int) or isinstance(value["generation"],bool) or value["generation"] < 2 or not isinstance(value["lineage"],dict) or set(value["lineage"]) != {"root_baseline_sha256","previous_baseline_sha256","failed_verification_sha256","expansion_event_hash"} or any(not isinstance(value["lineage"][key],str) or len(value["lineage"][key]) != 64 for key in value["lineage"]) or not isinstance(value["state_sha256"],str) or len(value["state_sha256"]) != 64): raise GitFactError("invalid refreshed baseline lineage",CORRUPT)
    return value


def _binding(ledger: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": ledger["run_id"], "slice_id": ledger["slice_id"],
        "ledger_revision": ledger["revision"], "ledger_event_hash": ledger["last_event_hash"],
        "artifact_contract_sha256": hashlib.sha256(ledger["artifact_contract"].encode()).hexdigest(),
        "allowed_paths": ledger["allowed_paths"],
    }


def _assert_binding(receipt: dict[str, Any], ledger: dict[str, Any]) -> None:
    expected = _binding(ledger)
    stable_keys = ("run_id", "slice_id", "artifact_contract_sha256", "allowed_paths")
    if any(receipt[key] != expected[key] for key in stable_keys):
        raise GitFactError("ledger facts changed since baseline capture", CONFLICT)
    receipt_hash = hashlib.sha256(_canonical(receipt)).hexdigest()
    if ledger["baseline_sha256"] != receipt_hash:
        raise GitFactError("baseline receipt disagrees with authoritative ledger hash", CORRUPT)


def _capture(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    path = _receipt_path(root, args.run_id)
    _validate_receipt_file(path)
    # Exact retry after binding accepts the original expected revision.
    state_dir = root / ".claude" / "state"
    current, _ = _load(state_dir / "slice_ledger.json", state_dir / "slice_events.jsonl")
    if current and current["revision"] == args.revision + 1 and current["baseline_sha256"] and path.exists():
        existing = _read_receipt(path)
        if current["run_id"] == args.run_id and current["owner"]["session_id"] == args.session_id and hashlib.sha256(_canonical(existing)).hexdigest() == current["baseline_sha256"]:
            return existing
    ledger = _ledger(root, args)
    facts = snapshot(root, ledger["allowed_paths"])
    receipt = {"schema_version": 1, **_binding(ledger), "captured_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"), "git": facts}
    if path.exists():
        existing = _read_receipt(path)
        # Timestamp is not part of idempotency; all authoritative facts must match.
        if {k: v for k, v in existing.items() if k != "captured_at"} == {k: v for k, v in receipt.items() if k != "captured_at"}:
            baseline_sha256 = hashlib.sha256(_canonical(existing)).hexdigest()
            bind_args = argparse.Namespace(run_id=args.run_id, session_id=args.session_id, revision=args.revision, baseline_sha256=baseline_sha256, baseline_path=_relative(root, path))
            _bind_baseline(bind_args, root / ".claude/state/slice_ledger.json", root / ".claude/state/slice_events.jsonl")
            return existing
        raise GitFactError("immutable baseline already exists with different facts", CONFLICT)
    _atomic_projection(path, receipt)
    baseline_sha256 = hashlib.sha256(_canonical(receipt)).hexdigest()
    bind_args = argparse.Namespace(run_id=args.run_id, session_id=args.session_id, revision=args.revision, baseline_sha256=baseline_sha256, baseline_path=_relative(root, path))
    _bind_baseline(bind_args, root / ".claude/state/slice_ledger.json", root / ".claude/state/slice_events.jsonl")
    return receipt


def _refresh(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Append the next generation after a provenance-bound post-FAIL expansion."""
    state_dir=root/".claude/state"; ledger_path=state_dir/"slice_ledger.json"; events_path=state_dir/"slice_events.jsonl"
    state,events=_load(ledger_path,events_path)
    if state and events and events[-1]["type"]=="baseline_refreshed" and state["run_id"]==args.run_id and state["owner"]["session_id"]==args.session_id and state["revision"]==args.revision+1:
        receipt=_read_receipt(root/state["baseline_path"]); _assert_refresh_binding(receipt,state,events[-1]); return receipt
    state=_ledger(root,args)
    if not events or events[-1]["type"]!="contract_expanded" or not events[-1]["payload"]["post_fail_repair"]: raise GitFactError("refresh requires latest post-FAIL contract expansion",CONFLICT)
    failed=_failed_verification_binding(state,root)
    if not failed or events[-1]["payload"]["failed_verification_sha256"]!=failed: raise GitFactError("refresh FAIL binding mismatch",CONFLICT)
    previous=_read_receipt(_latest_receipt_path(root,state)); previous_sha=hashlib.sha256(_canonical(previous)).hexdigest()
    if previous_sha!=state["baseline_sha256"]: raise GitFactError("previous baseline binding mismatch",CORRUPT)
    generation=previous.get("generation",1)+1; path=_run_dir(root,args.run_id)/f"slice_baseline_v{generation}.json"
    if path.exists(): raise GitFactError("unbound preexisting refreshed baseline is untrusted",CONFLICT)
    before=snapshot(root,state["allowed_paths"])
    if snapshot(root,state["allowed_paths"])!=before: raise GitFactError("state changed during baseline refresh",CONFLICT)
    state_sha=hashlib.sha256(_canonical(before)).hexdigest(); root_sha=previous.get("lineage",{}).get("root_baseline_sha256",previous_sha)
    lineage={"root_baseline_sha256":root_sha,"previous_baseline_sha256":previous_sha,"failed_verification_sha256":failed,"expansion_event_hash":state["last_event_hash"]}
    receipt={"schema_version":1,**_binding(state),"captured_at":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z"),"git":before,"generation":generation,"lineage":lineage,"state_sha256":state_sha}
    _atomic_projection(path,receipt)
    sha=hashlib.sha256(_canonical(receipt)).hexdigest(); payload={"run_id":state["run_id"],"revision":state["revision"]+1,"updated_at":_now(),"baseline_sha256":sha,"baseline_path":_relative(root,path),"previous_baseline_sha256":previous_sha,"root_baseline_sha256":root_sha,"generation":generation,"failed_verification_sha256":failed,"expansion_event_hash":state["last_event_hash"],"state_sha256":state_sha}
    event=_append(events_path,"baseline_refreshed",payload,events); state.update(baseline_sha256=sha,baseline_path=payload["baseline_path"],revision=payload["revision"],updated_at=payload["updated_at"],last_event_hash=event["hash"]); _atomic_projection(ledger_path,state)
    return receipt


def _assert_refresh_binding(receipt: dict[str, Any], ledger: dict[str, Any], event: dict[str, Any]) -> None:
    """Cross-check a refreshed receipt against its notarizing ledger event."""
    _assert_binding(receipt,ledger); payload=event["payload"]; lineage=receipt["lineage"]
    expected={"generation":payload["generation"],"state_sha256":payload["state_sha256"]}
    if any(receipt[key]!=value for key,value in expected.items()) or any(lineage[key]!=payload[key] for key in lineage) or receipt["ledger_event_hash"]!=payload["expansion_event_hash"]: raise GitFactError("refreshed baseline receipt/event mismatch",CORRUPT)


def _lineage_attribution(root: Path, ledger: dict[str, Any], latest: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Preserve v1 ownership while admitting new paths conservatively at vN."""
    if "generation" not in latest: return attribution_receipt(ledger,latest,current)
    _,events=_load(root/".claude/state/slice_ledger.json",root/".claude/state/slice_events.jsonl")
    notarization=next((event for event in reversed(events) if event["type"]=="baseline_refreshed" and event["payload"]["baseline_sha256"]==ledger["baseline_sha256"]),None)
    if notarization is None: raise GitFactError("refreshed baseline event missing",CORRUPT)
    _assert_refresh_binding(latest,ledger,notarization)
    root_receipt=_read_receipt(_receipt_path(root,ledger["run_id"])); root_sha=hashlib.sha256(_canonical(root_receipt)).hexdigest()
    if root_sha!=latest["lineage"]["root_baseline_sha256"]: raise GitFactError("root baseline lineage unavailable",CORRUPT)
    old=set(root_receipt["allowed_paths"]); added=set(ledger["allowed_paths"])-old
    root_dirty={entry["path"] for entry in root_receipt["git"]["entries"]}|{entry["original_path"] for entry in root_receipt["git"]["entries"] if entry.get("original_path")}
    same_anchors=root_receipt["git"]["anchors"]==latest["git"]["anchors"]
    eligible=set()
    for path in added:
        fact=latest["git"]["scoped_facts"].get(path); stages=fact.get("index_stages",[]) if isinstance(fact,dict) else []
        if same_anchors and path not in root_dirty and fact and fact.get("head_blob") and len(stages)==1 and stages[0].get("stage")=="0" and stages[0].get("oid")==fact["head_blob"]: eligible.add(path)
    latest_items={item["path"]:item for item in classify(latest["git"],current,ledger["allowed_paths"])}
    root_items={item["path"]:item for item in classify(root_receipt["git"],current,sorted(old|eligible))}
    for path in old|eligible:
        if path in root_items: latest_items[path]=root_items[path]
        else: latest_items.pop(path,None)
    return attribution_receipt(ledger,latest,current,classifications=[latest_items[path] for path in sorted(latest_items)])


def _attribute(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    ledger = _ledger(root, args)
    baseline = _read_receipt(_latest_receipt_path(root,ledger))
    _assert_binding(baseline, ledger)
    current = snapshot(root, ledger["allowed_paths"])
    return {**_lineage_attribution(root,ledger,baseline,current), "candidate_owned_is_authorship": False, "current": current}


def current_attribution(root: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    """Return exact current attribution facts for verification consumers."""
    baseline = _read_receipt(_latest_receipt_path(root,ledger))
    _assert_binding(baseline, ledger)
    return _lineage_attribution(root,ledger,baseline,snapshot(root, ledger["allowed_paths"]))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        root = _git_root(args.cwd)
        with _locked(root):
            result = _capture(root,args) if args.command=="capture" else _refresh(root,args) if args.command=="refresh" else _attribute(root,args)
        _emit(True, args.command, result=result)
        return OK
    except (GitFactError, LedgerError) as exc:
        _emit(False, "error", stream=sys.stderr, code=exc.code, error=str(exc))
        return exc.code
    except OSError as exc:
        _emit(False, "error", stream=sys.stderr, code=IO_ERROR, error=f"filesystem error: {exc}")
        return IO_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
