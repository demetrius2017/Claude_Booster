#!/usr/bin/env python3
"""Managed test dispatcher for fail-closed development and release execution.

Purpose: decide, freeze plans, run structured commands, validate exact receipts.
Contract: JSON stdout only; errors are JSON stderr/non-zero; no shell execution.
CLI/Examples: test_dispatcher.py decide|plan|run|validate-receipt|install-hook.
Limitations: local release receipts are not CI promotion authority.
ENV/Files: TEST_MODE_SESSION_ID is optional; writes .claude/test-modes private state.
"""
from __future__ import annotations
import argparse, json, os, platform, shlex, shutil, stat, subprocess, sys, tempfile
from pathlib import Path
from test_modes_core import ModeError, append_failure, atomic_json, candidate_identity, changed_paths, digest, lease_path, load_critical, load_impact, load_registry, load_registry_ref, now, parse_time, project_root, read_json, resolve_git_oid, resolve_mode, selection, unresolved_failures, unresolved_failure_records, validate_lease, validate_registry

ALLOW_ENV = {"PATH", "LANG", "LC_ALL", "PYTHONPATH", "HOME", "TMPDIR"}
MANIFEST_KEYS = {"schema_version","kind","created_at","mode","candidate","lease_valid","trusted_ci","protected_destination","local_advisory_only","base","base_sha","registry_hash","registry","test_ids","jobs","manifest_hash"}

class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"status":"fail","error":message},sort_keys=True,separators=(",",":")),file=sys.stderr)
        raise SystemExit(2)

def emit(value: dict, code: int = 0) -> int:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"))); return code

def lease(root: Path):
    try: return validate_lease(root, read_json(lease_path(root)), os.environ.get("TEST_MODE_SESSION_ID"))
    except ModeError: return None

def decision(root: Path, args: argparse.Namespace) -> dict:
    candidate = candidate_identity(root); active = lease(root)
    mode = resolve_mode(requested=args.mode, trusted_ci=bool(args.trusted_ci), protected_destination=bool(args.protected), lease=active)
    return {"schema_version": 1, "kind": "decision", "created_at": now(), "mode": mode, "candidate": candidate, "lease_valid": active is not None, "trusted_ci": bool(args.trusted_ci), "protected_destination": bool(args.protected), "local_advisory_only": not bool(args.trusted_ci)}

def cmd_decide(args): return emit(decision(args.root, args))

def cmd_plan(args):
    item = decision(args.root, args); registry, impact = load_registry(args.root), load_impact(args.root)
    paths = changed_paths(args.root, args.base)
    contract_changed = any(path.startswith("templates/test-contract/") or path.startswith("templates/scripts/test_") or path.endswith((".lock", ".yaml", ".yml")) for path in paths)
    trusted = load_registry_ref(args.root, args.base) if contract_changed else None
    if contract_changed: item["mode"] = "release"
    unresolved = set(args.unresolved) | unresolved_failures(args.root)
    critical = load_critical(args.root)
    try:
        ids = selection(args.root, registry, impact, args.base, unresolved, args.epoch, item["mode"] == "release", trusted, critical)
    except ModeError as exc:
        if str(exc) != "unknown changed path requires release": raise
        item["mode"] = "release"
        ids = selection(args.root, registry, impact, args.base, unresolved, args.epoch, True, trusted, critical)
    jobs = [x for x in registry["jobs"] if x["id"] in ids]
    item.update({"kind": "manifest", "base": args.base, "base_sha": resolve_git_oid(args.root,args.base), "registry_hash": registry["registry_hash"], "registry": registry, "test_ids": ids, "jobs": jobs})
    item["manifest_hash"] = digest({k:v for k,v in item.items() if k != "created_at"})
    return emit(item)

def _manifest(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != MANIFEST_KEYS: raise ModeError("manifest schema invalid")
    if value["schema_version"] != 1 or value["kind"] != "manifest" or value["mode"] not in {"development","release"}: raise ModeError("manifest invariant invalid")
    if not isinstance(value["test_ids"], list) or not value["test_ids"] or len(value["test_ids"]) != len(set(value["test_ids"])) or not all(isinstance(x,str) and x for x in value["test_ids"]): raise ModeError("manifest test_ids invalid")
    candidate_keys={"schema_version","root","git_common_dir","head","tree","index_sha256","worktree_sha256","untracked_sha256","dirty"}
    candidate=value["candidate"]
    if not isinstance(value["jobs"], list) or not isinstance(candidate, dict) or set(candidate) != candidate_keys: raise ModeError("manifest jobs or candidate invalid")
    if candidate["schema_version"] != 1 or not isinstance(candidate["dirty"],bool) or not all(isinstance(candidate[k],str) and candidate[k] for k in candidate_keys-{"schema_version","dirty"}): raise ModeError("manifest candidate types invalid")
    if not all(len(candidate[k]) == 64 and all(char in "0123456789abcdef" for char in candidate[k]) for k in ("index_sha256","worktree_sha256","untracked_sha256")): raise ModeError("manifest candidate digest invalid")
    if len(candidate["head"]) not in (40,64) or len(candidate["tree"]) not in (40,64): raise ModeError("manifest candidate Git identity invalid")
    if not all(isinstance(value[k], bool) for k in ("lease_valid","trusted_ci","protected_destination","local_advisory_only")): raise ModeError("manifest trust flags invalid")
    if value["local_advisory_only"] == value["trusted_ci"]: raise ModeError("manifest trust invariant invalid")
    if not isinstance(value["base"],str) or not value["base"] or not isinstance(value["base_sha"],str) or len(value["base_sha"]) not in (40,64): raise ModeError("manifest base invalid")
    parse_time(value["created_at"])
    if value["manifest_hash"] != digest({k:v for k,v in value.items() if k not in {"manifest_hash","created_at"}}): raise ModeError("manifest hash mismatch")
    return value

def _runtime(job: dict) -> None:
    current = platform.system().lower()
    if job["platform"] not in {"any", current}: raise ModeError(f"required job {job['id']} unsupported on host {current}")
    if job["network"] == "required" and os.environ.get("TEST_MODE_NETWORK_OK") != "1": raise ModeError(f"required network capability not explicitly granted for {job['id']}")

def cmd_run(args):
    manifest = _manifest(read_json(args.manifest)); root = args.root
    frozen = validate_registry(manifest.get("registry"))
    current = load_registry(root)
    if frozen["registry_hash"] != manifest.get("registry_hash") or current["registry_hash"] != frozen["registry_hash"]: raise ModeError("registry changed after plan")
    frozen_jobs = {job["id"]: job for job in frozen["jobs"]}
    if manifest.get("jobs") != [frozen_jobs.get(test_id) for test_id in manifest.get("test_ids", [])]: raise ModeError("manifest jobs do not match frozen registry")
    before = candidate_identity(root)
    if before != manifest.get("candidate"): raise ModeError("candidate changed before run")
    if manifest["base_sha"] != resolve_git_oid(root, manifest["base"]): raise ModeError("manifest base changed after plan")
    outcomes=[]; active = unresolved_failure_records(root)
    for job in manifest["jobs"]:
        _runtime(job)
        env={k: os.environ[k] for k in ALLOW_ENV if k in os.environ}; env.update(job["env"]); env.update({"PATH": env.get("PATH", "/usr/bin:/bin"), "TEST_MODE_MANAGED": "1"})
        command_identity = digest({k: job[k] for k in ("id", "argv", "cwd", "env", "platform", "network", "state_scope", "timeout_seconds")})
        contract_identity = digest({"registry_hash":frozen["registry_hash"],"job":job})
        try:
            result=subprocess.run(job["argv"], cwd=root / job["cwd"], env=env, capture_output=True, text=True, timeout=job["timeout_seconds"], check=False)
            outcome={"id":job["id"],"status":"pass" if result.returncode == 0 else "fail","exit_code":result.returncode,"stdout_sha256":digest(result.stdout),"stderr_sha256":digest(result.stderr),"command_identity":command_identity}
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            outcome={"id":job["id"],"status":"timeout","exit_code":None,"stdout_sha256":digest(stdout),"stderr_sha256":digest(stderr),"command_identity":command_identity}
        outcomes.append(outcome)
        if outcome["status"] != "pass":
            append_failure(root,{"type":"failure","test_id":job["id"],"candidate":before,"command_identity":command_identity,"contract_identity":contract_identity,"signature":digest(outcome)})
        else:
            key=(job["id"],command_identity,contract_identity)
            if key in active: append_failure(root,{"type":"closure","test_id":job["id"],"failure_hash":active[key]["hash"],"command_identity":command_identity,"contract_identity":contract_identity,"status":"green"})
    after=candidate_identity(root)
    passed=all(x["status"] == "pass" for x in outcomes) and after == before
    receipt={"schema_version":1,"kind":"local-release-receipt" if manifest["mode"] == "release" else "development-receipt","created_at":now(),"status":"pass" if passed else "fail","candidate":before,"manifest_hash":manifest["manifest_hash"],"registry_hash":frozen["registry_hash"],"test_ids":manifest["test_ids"],"outcomes":outcomes,"promotion_authorized":False}
    if manifest["mode"] == "release" and before["dirty"]: receipt["status"]="fail"; receipt["reason"]="release receipt requires clean committed candidate"
    receipt["receipt_hash"] = digest(receipt)
    atomic_json(root / ".claude" / "test-modes" / "receipts" / f"{before['head']}-{manifest['manifest_hash']}.json", receipt)
    return emit(receipt, 0 if receipt["status"] == "pass" else 1)

def cmd_validate(args):
    receipt=read_json(args.receipt); candidate=candidate_identity(args.root)
    if receipt.get("kind") == "trusted-ci-promotion":
        integrity = receipt.get("promotion_hash") == digest({k:v for k,v in receipt.items() if k != "promotion_hash"})
    else:
        integrity = receipt.get("receipt_hash") == digest({k:v for k,v in receipt.items() if k != "receipt_hash"})
    valid=integrity and receipt.get("status") == "pass" and receipt.get("candidate") == candidate and (not args.promotion or (receipt.get("kind") == "trusted-ci-promotion" and receipt.get("promotion_authorized") is True))
    return emit({"valid":valid,"candidate":candidate,"receipt_kind":receipt.get("kind")},0 if valid else 1)

def cmd_install_hook(args):
    root=args.root
    def safe_regular(path, executable=True):
        info=path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022 or (executable and not info.st_mode & stat.S_IXUSR): raise ModeError(f"unsafe hook file: {path}")
        return info
    def safe_directory(path):
        if any(part in {".",".."} for part in path.parts): raise ModeError("effective hooksPath contains unsafe traversal")
        absolute=path.absolute()
        current=Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current=current / part
            info=current.stat(follow_symlinks=False)
            if current.is_symlink() or not stat.S_ISDIR(info.st_mode): raise ModeError("effective hooksPath contains a symlink or non-directory")
        info=absolute.stat(follow_symlinks=False)
        if info.st_uid != os.getuid() or info.st_mode & 0o022: raise ModeError("effective hooksPath has unsafe ownership or mode")
        return absolute
    configured=subprocess.run(["git","-C",str(root),"config","--path","--get","core.hooksPath"],capture_output=True,text=True,check=False)
    if configured.returncode not in {0,1}: raise ModeError("cannot read effective core.hooksPath")
    hooks=configured.stdout.rstrip("\n")
    if "\x00" in hooks or "\n" in hooks or "\r" in hooks: raise ModeError("effective hooksPath contains unsafe characters")
    if hooks:
        raw_target=Path(hooks); raw_target=root / raw_target if not raw_target.is_absolute() else raw_target
    else:
        git_path=subprocess.run(["git","-C",str(root),"rev-parse","--git-path","hooks"],capture_output=True,text=True,check=False)
        if git_path.returncode or not git_path.stdout.strip(): raise ModeError("cannot resolve Git hooks directory")
        raw_target=Path(git_path.stdout.strip()); raw_target=root / raw_target if not raw_target.is_absolute() else raw_target
    target=safe_directory(raw_target)
    hook=target / "pre-push"
    managed = "# claude-booster-managed-pre-push-v1"
    user_hook = target / "pre-push.claude-booster-user"
    python = shutil.which("python3") or "python3"; checker = Path(__file__).with_name("test_pre_push.py")
    script=f'''#!/bin/sh
{managed}
set -u
input=$(mktemp "${{TMPDIR:-/tmp}}/booster-pre-push.XXXXXX") || exit 1
trap 'rm -f "$input"' EXIT HUP INT TERM
cat >"$input" || exit 1
user_status=0
if [ -x {shlex.quote(str(user_hook))} ]; then
  {shlex.quote(str(user_hook))} "$@" <"$input" || user_status=$?
fi
managed_status=0
{shlex.quote(python)} {shlex.quote(str(checker))} --root {shlex.quote(str(root))} "$@" <"$input" || managed_status=$?
if [ "$user_status" -ne 0 ]; then exit "$user_status"; fi
exit "$managed_status"
'''
    payload=script.encode()
    if hook.exists() or hook.is_symlink():
        safe_regular(hook)
    if hook.exists():
        existing=hook.read_bytes()
        if existing == payload:
            if user_hook.exists() or user_hook.is_symlink(): safe_regular(user_hook)
            return emit({"installed":str(hook),"core_hooks_path":str(target),"idempotent":True,"note":"local hook is not CI authorization"})
        if managed.encode() in existing: raise ModeError("managed hook differs from canonical wrapper")
    preserve=hook.exists()
    if user_hook.exists() or user_hook.is_symlink(): raise ModeError("user hook chain destination already exists")
    fd, temp = tempfile.mkstemp(prefix=".pre-push.", dir=target)
    try:
        os.fchmod(fd,0o700)
        offset=0
        while offset < len(payload):
            written=os.write(fd,payload[offset:])
            if written <= 0: raise OSError("short write while installing managed hook")
            offset += written
        os.fsync(fd); os.close(fd)
        if preserve: os.replace(hook,user_hook)
        try: os.replace(temp,hook)
        except Exception:
            if preserve and user_hook.exists() and not hook.exists(): os.replace(user_hook,hook)
            raise
    except Exception:
        try: os.close(fd)
        except OSError: pass
        try: os.unlink(temp)
        except FileNotFoundError: pass
        raise
    return emit({"installed":str(hook),"core_hooks_path":str(target),"note":"local hook is not CI authorization"})

def main(argv):
    parser=JSONArgumentParser(); parser.add_argument("--root",type=Path,default=Path.cwd()); subs=parser.add_subparsers(dest="command",required=True,parser_class=JSONArgumentParser)
    for name in ("decide","plan"):
        p=subs.add_parser(name); p.add_argument("--mode",choices=["development","release"]); p.add_argument("--trusted-ci",action="store_true"); p.add_argument("--protected",action="store_true")
        if name == "plan": p.add_argument("--base",required=True); p.add_argument("--epoch",default="1"); p.add_argument("--unresolved",action="append",default=[])
    subs.add_parser("run").add_argument("--manifest",type=Path,required=True)
    p=subs.add_parser("validate-receipt"); p.add_argument("--receipt",type=Path,required=True); p.add_argument("--promotion",action="store_true")
    subs.add_parser("install-hook")
    args=parser.parse_args(argv)
    try:
        args.root=project_root(args.root)
        return {"decide":cmd_decide,"plan":cmd_plan,"run":cmd_run,"validate-receipt":cmd_validate,"install-hook":cmd_install_hook}[args.command](args)
    except (ModeError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status":"fail","error":str(exc) or exc.__class__.__name__},sort_keys=True,separators=(",",":")),file=sys.stderr)
        return 1
if __name__ == "__main__": sys.exit(main(sys.argv[1:]))
