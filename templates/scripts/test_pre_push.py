#!/usr/bin/env python3
"""Fail-closed Git pre-push resolver for managed test-mode receipts.

Purpose: parse Git's four-field input and require exact local release evidence.
Contract: stdin records are parsed losslessly; unknown/protected destinations fail.
CLI/Examples: invoked by a securely installed pre-push hook.
Limitations: it is bypassable with --no-verify and never grants CI authorization.
ENV/Files: reads stdin, Git config and .claude/test-modes/receipts.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, tempfile
from pathlib import Path
from test_modes_core import ModeError, ZERO, candidate_identity, contract_dir, digest, load_registry, project_root, read_json

class JSONArgumentParser(argparse.ArgumentParser):
    def error(self,message): print(json.dumps({"status":"fail","error":message},sort_keys=True),file=sys.stderr); raise SystemExit(2)

def remote_url(root: Path, remote: str) -> str:
    out=subprocess.run(["git","-C",str(root),"remote","get-url",remote],capture_output=True,text=True,check=False)
    if out.returncode: raise ModeError("remote URL unavailable")
    return out.stdout.strip()

def protected(root: Path, remote: str, ref: str) -> bool:
    policy=contract_dir(root) / "protected_refs.json"
    value=read_json(policy)
    if value.get("schema_version") != 1 or not isinstance(value.get("protected_refs"),list): raise ModeError("protected-ref policy invalid")
    # The versioned policy makes unmatched refs explicitly non-protected; a
    # missing or malformed policy is rejected above and therefore fails closed.
    if not all(isinstance(item,str) and item.startswith("refs/") for item in value["protected_refs"]): raise ModeError("protected-ref policy entries invalid")
    return ref in value["protected_refs"]

def valid_oid(value: str) -> bool:
    return value in ZERO or bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value))

def is_force(root: Path, remote_oid: str, local_oid: str) -> bool:
    if remote_oid in ZERO: return False
    result=subprocess.run(["git","-C",str(root),"merge-base","--is-ancestor",remote_oid,local_oid],capture_output=True,text=True,check=False)
    if result.returncode == 0: return False
    if result.returncode == 1: return True
    raise ModeError("cannot determine push ancestry")

def new_branch_base(root: Path, remote: str, local_oid: str) -> str:
    """Return the merge-base with a remote's symbolic default branch."""
    if not isinstance(remote, str) or not remote or not valid_oid(local_oid) or local_oid in ZERO:
        raise ModeError("invalid new-branch base inputs")
    symbolic=f"refs/remotes/{remote}/HEAD"
    resolved=subprocess.run(["git","-C",str(root),"symbolic-ref","--quiet",symbolic],capture_output=True,text=True,check=False)
    if resolved.returncode or not resolved.stdout.strip().startswith(f"refs/remotes/{remote}/"):
        raise ModeError("remote default base cannot be resolved")
    base_ref=resolved.stdout.strip()
    base_oid=subprocess.run(["git","-C",str(root),"rev-parse","--verify",f"{base_ref}^{{commit}}"],capture_output=True,text=True,check=False)
    if base_oid.returncode or not valid_oid(base_oid.stdout.strip()) or base_oid.stdout.strip() in ZERO:
        raise ModeError("remote default base cannot be resolved")
    merged=subprocess.run(["git","-C",str(root),"merge-base",local_oid,base_oid.stdout.strip()],capture_output=True,text=True,check=False)
    candidate=merged.stdout.strip()
    if merged.returncode or not valid_oid(candidate) or candidate in ZERO:
        raise ModeError("remote default merge-base cannot be resolved")
    return candidate

def release_receipt_head(root: Path, receipt: dict, registry: dict) -> str | None:
    required={"schema_version","kind","created_at","status","candidate","manifest_hash","registry_hash","test_ids","outcomes","promotion_authorized","receipt_hash"}
    if set(receipt) != required or receipt.get("schema_version") != 1 or receipt.get("kind") != "local-release-receipt" or receipt.get("status") != "pass" or receipt.get("promotion_authorized") is not False: return None
    if receipt.get("receipt_hash") != digest({k:v for k,v in receipt.items() if k != "receipt_hash"}) or receipt.get("registry_hash") != registry["registry_hash"] or not re.fullmatch(r"[0-9a-f]{64}",receipt.get("manifest_hash", "")): return None
    ids=[job["id"] for job in registry["jobs"]]
    if receipt.get("test_ids") != ids or not isinstance(receipt.get("outcomes"),list) or not all(isinstance(item,dict) for item in receipt["outcomes"]) or [item["id"] for item in receipt["outcomes"]] != ids: return None
    outcome_keys={"id","status","exit_code","stdout_sha256","stderr_sha256","command_identity"}
    if any(set(item) != outcome_keys or item["status"] != "pass" or item["exit_code"] != 0 or any(not re.fullmatch(r"[0-9a-f]{64}",item[key]) for key in ("stdout_sha256","stderr_sha256","command_identity")) for item in receipt["outcomes"]): return None
    candidate=receipt.get("candidate")
    candidate_keys={"schema_version","root","git_common_dir","head","tree","index_sha256","worktree_sha256","untracked_sha256","dirty"}
    if not isinstance(candidate,dict) or set(candidate) != candidate_keys or candidate["schema_version"] != 1 or candidate["dirty"] is not False or candidate["root"] != str(root): return None
    if candidate["head"] in ZERO or candidate["tree"] in ZERO or not valid_oid(candidate["head"]) or not valid_oid(candidate["tree"]) or any(not re.fullmatch(r"[0-9a-f]{64}",candidate[key]) for key in ("index_sha256","worktree_sha256","untracked_sha256")): return None
    tree=subprocess.run(["git","-C",str(root),"rev-parse","--verify",f"{candidate['head']}^{{tree}}"],capture_output=True,text=True,check=False)
    if tree.returncode or tree.stdout.strip() != candidate["tree"]: return None
    return candidate["head"]

def main(argv):
    parser=JSONArgumentParser(); parser.add_argument("--root",default="."); parser.add_argument("remote",nargs="?"); parser.add_argument("url",nargs="?"); args=parser.parse_args(argv)
    try:
        root=project_root(args.root); identity=candidate_identity(root); lines=sys.stdin.read().splitlines()
        remote=args.remote or "origin"; configured_url=remote_url(root,remote)
        if args.url and args.url != configured_url: raise ModeError("remote URL provenance mismatch")
        if not lines: raise ModeError("pre-push received no ref records")
        records=[]
        for line in lines:
            fields=line.split()
            if len(fields) != 4: raise ModeError("malformed pre-push record")
            local_ref,local_oid,remote_ref,remote_oid=fields
            if not local_ref.startswith("refs/") or not remote_ref.startswith("refs/") or not valid_oid(local_oid) or not valid_oid(remote_oid): raise ModeError("invalid pre-push ref or object ID")
            namespace_known=remote_ref.startswith("refs/heads/") or remote_ref.startswith("refs/tags/")
            deletion=local_oid in ZERO
            record={"local_ref":local_ref,"remote_ref":remote_ref,"local_oid":local_oid,"remote_oid":remote_oid,"deletion":deletion,"new":not deletion and remote_oid in ZERO,"force":False if deletion else is_force(root,remote_oid,local_oid),"protected":protected(root,remote,remote_ref),"unknown_destination":not namespace_known,"tag":remote_ref.startswith("refs/tags/")}
            records.append(record)
        if any(record["deletion"] for record in records): raise ModeError("deletion push requires protected CI policy; local receipt cannot authorize it")
        if any(record.get("unknown_destination") for record in records): raise ModeError("unknown destination namespace rejected")
        elevated=any(record["protected"] or record["tag"] or record["force"] for record in records)
        if elevated:
            registry=load_registry(root)
            receipts=list((root / ".claude" / "test-modes" / "receipts").glob("*.json"))
            passing_oids=set()
            for path in receipts:
                try:
                    head=release_receipt_head(root,read_json(path),registry)
                    if head: passing_oids.add(head)
                except ModeError: continue
            required_oids={record["local_oid"] for record in records if record.get("protected") or record.get("tag") or record.get("force")}
            if not required_oids <= passing_oids: raise ModeError("protected/new/tag/force destination requires exact clean local release receipt")
        else:
            bases={new_branch_base(root,remote,record["local_oid"]) if record["new"] else record["remote_oid"] for record in records}
            if len(bases) != 1: raise ModeError("unprotected multi-ref push has ambiguous selective-test base")
            dispatcher=Path(__file__).with_name("test_dispatcher.py")
            with tempfile.TemporaryDirectory(prefix="booster-pre-push-") as scratch:
                manifest=Path(scratch) / "manifest.json"
                plan=subprocess.run([sys.executable,str(dispatcher),"--root",str(root),"plan","--mode","development","--base",next(iter(bases))],capture_output=True,text=True,check=False)
                if plan.returncode: raise ModeError("development selective-test planning failed: " + (plan.stderr.strip() or plan.stdout.strip()))
                try: planned=json.loads(plan.stdout)
                except json.JSONDecodeError as exc: raise ModeError("development selective-test plan was not JSON") from exc
                manifest.write_text(json.dumps(planned,sort_keys=True),encoding="utf-8")
                run=subprocess.run([sys.executable,str(dispatcher),"--root",str(root),"run","--manifest",str(manifest)],capture_output=True,text=True,check=False)
                if run.returncode: raise ModeError("development selective-test gate failed: " + (run.stderr.strip() or run.stdout.strip()))
                try: outcome=json.loads(run.stdout)
                except json.JSONDecodeError as exc: raise ModeError("development selective-test result was not JSON") from exc
                if outcome.get("status") != "pass" or outcome.get("kind") != "development-receipt": raise ModeError("development selective-test gate returned non-PASS evidence")
        print(json.dumps({"status":"pass","records":records,"remote":remote,"remote_url":configured_url,"promotion_authorized":False},sort_keys=True)); return 0
    except (ModeError,OSError,ValueError,TypeError,KeyError,json.JSONDecodeError,subprocess.SubprocessError) as exc:
        print(json.dumps({"status":"fail","error":str(exc)},sort_keys=True),file=sys.stderr); return 1
if __name__ == "__main__": sys.exit(main(sys.argv[1:]))
