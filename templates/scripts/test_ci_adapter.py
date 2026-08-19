#!/usr/bin/env python3
"""Trusted-CI promotion receipt validator for immutable managed-test evidence.

Purpose: bind promotion authorization to hosted-CI provenance and exact identity.
Contract: rejects local/untrusted inputs and validates all supplied SHA/digest fields.
CLI/Examples: test_ci_adapter.py validate --input trusted.json --receipt receipt.json.
Limitations: verifies supplied trusted context; hosting attestation verification is external.
ENV/Files: no environment variables; explicit JSON files only.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from test_modes_core import ModeError, atomic_json, digest, read_json, validate_registry

REQUIRED={"repository","remote","target_ref","base_sha","merge_sha","merge_tree","workflow_digest","run_id","platform_digest","trust_source"}
def valid_sha(value): return isinstance(value,str) and len(value) in (40,64) and all(c in "0123456789abcdef" for c in value)
def valid_digest(value): return isinstance(value,str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
class JSONArgumentParser(argparse.ArgumentParser):
    def error(self,message): print(json.dumps({"status":"fail","error":message},sort_keys=True),file=sys.stderr); raise SystemExit(2)
def main(argv):
    parser=JSONArgumentParser(); parser.add_argument("validate"); parser.add_argument("--input",type=Path,required=True); parser.add_argument("--receipt",type=Path,required=True); parser.add_argument("--manifest",type=Path,required=True); parser.add_argument("--registry",type=Path); parser.add_argument("--out",type=Path); args=parser.parse_args(argv)
    try:
        if args.validate != "validate": raise ModeError("unknown CI adapter command")
        context,receipt,manifest=read_json(args.input),read_json(args.receipt),read_json(args.manifest)
        registry_path=args.registry or Path(__file__).resolve().parent.parent / "test-contract" / "registry.json"
        registry=validate_registry(read_json(registry_path))
        if set(context) != REQUIRED or context["trust_source"] not in {"github-actions-protected", "gitlab-protected", "buildkite-protected"}: raise ModeError("untrusted CI context")
        if not all(isinstance(context[x],str) and context[x] for x in REQUIRED): raise ModeError("invalid CI provenance field")
        if not re.fullmatch(r"[^/\s]+/[^/\s]+",context["repository"]) or not re.fullmatch(r"[^\s\x00]+",context["remote"]) or not re.fullmatch(r"[A-Za-z0-9._:-]+",context["run_id"]): raise ModeError("invalid repository, remote, or run provenance")
        if not all(valid_sha(context[x]) for x in ("base_sha","merge_sha","merge_tree")): raise ModeError("invalid CI SHA")
        if not valid_digest(context["workflow_digest"]) or not valid_digest(context["platform_digest"]): raise ModeError("invalid workflow or platform provenance digest")
        if not re.fullmatch(r"refs/heads/[^\s\x00]+",context["target_ref"]): raise ModeError("CI target ref is not a branch")
        manifest_keys={"schema_version","kind","created_at","mode","candidate","lease_valid","trusted_ci","protected_destination","local_advisory_only","base","base_sha","registry_hash","registry","test_ids","jobs","manifest_hash"}
        receipt_keys={"schema_version","kind","created_at","status","candidate","manifest_hash","registry_hash","test_ids","outcomes","promotion_authorized","receipt_hash"}
        if not isinstance(manifest,dict) or set(manifest) != manifest_keys or manifest.get("schema_version") != 1 or manifest.get("kind") != "manifest" or manifest.get("mode") != "release": raise ModeError("CI manifest is not a release manifest")
        if not isinstance(receipt,dict) or set(receipt) != receipt_keys or receipt.get("schema_version") != 1 or receipt.get("promotion_authorized") is not False: raise ModeError("local receipt schema invalid")
        if not isinstance(manifest.get("lease_valid"),bool) or not isinstance(manifest.get("base"),str) or not manifest["base"] or not isinstance(manifest.get("created_at"),str) or not isinstance(receipt.get("created_at"),str): raise ModeError("manifest or receipt provenance fields invalid")
        if manifest.get("trusted_ci") is not True or manifest.get("protected_destination") is not True or manifest.get("local_advisory_only") is not False: raise ModeError("local-only manifest cannot authorize promotion")
        candidate=receipt.get("candidate",{})
        candidate_keys={"schema_version","root","git_common_dir","head","tree","index_sha256","worktree_sha256","untracked_sha256","dirty"}
        if not isinstance(candidate,dict) or set(candidate) != candidate_keys or candidate.get("schema_version") != 1 or not all(isinstance(candidate[k],str) and candidate[k] for k in ("root","git_common_dir")) or not all(valid_digest(candidate[k]) for k in ("index_sha256","worktree_sha256","untracked_sha256")): raise ModeError("candidate identity schema invalid")
        if receipt.get("kind") != "local-release-receipt" or receipt.get("status") != "pass" or candidate.get("head") != context["merge_sha"] or candidate.get("tree") != context["merge_tree"] or candidate.get("dirty") is not False: raise ModeError("receipt does not bind exact clean merge result")
        receipt_body={k:v for k,v in receipt.items() if k != "receipt_hash"}
        if receipt.get("receipt_hash") != digest(receipt_body): raise ModeError("receipt integrity failure")
        if manifest.get("manifest_hash") != digest({k:v for k,v in manifest.items() if k not in {"manifest_hash","created_at"}}): raise ModeError("manifest integrity failure")
        if receipt.get("manifest_hash") != manifest["manifest_hash"] or receipt.get("registry_hash") != registry["registry_hash"] or manifest.get("registry_hash") != registry["registry_hash"]: raise ModeError("receipt/manifest/registry binding mismatch")
        if manifest.get("base_sha") != context["base_sha"]: raise ModeError("CI base provenance mismatch")
        if manifest.get("candidate") != candidate or manifest.get("registry") != registry: raise ModeError("manifest does not freeze exact candidate and full registry")
        required_ids={job["id"] for job in registry["jobs"]}
        frozen_jobs={job["id"]:job for job in registry["jobs"]}
        if manifest.get("test_ids") != [job["id"] for job in registry["jobs"]] or manifest.get("jobs") != [frozen_jobs[test_id] for test_id in manifest["test_ids"]]: raise ModeError("manifest is not an exact full-registry plan")
        outcome_ids=[item.get("id") for item in receipt.get("outcomes",[]) if isinstance(item,dict)]
        if outcome_ids != manifest["test_ids"] or set(outcome_ids) != required_ids or receipt.get("test_ids") != manifest["test_ids"]: raise ModeError("incomplete full-registry outcomes")
        for outcome in receipt["outcomes"]:
            if set(outcome) != {"id","status","exit_code","stdout_sha256","stderr_sha256","command_identity"}: raise ModeError("outcome schema invalid or continue-on-error metadata present")
            if outcome["status"] != "pass" or outcome["exit_code"] != 0 or not all(valid_digest(outcome[key]) for key in ("stdout_sha256","stderr_sha256","command_identity")): raise ModeError("non-passing, skipped, timeout, or continue-on-error outcome")
            job=frozen_jobs[outcome["id"]]
            command_identity=digest({key:job[key] for key in ("id","argv","cwd","env","platform","network","state_scope","timeout_seconds")})
            if outcome["command_identity"] != command_identity: raise ModeError("outcome command identity does not match frozen job")
        output={"schema_version":1,"kind":"trusted-ci-promotion","status":"pass","promotion_authorized":True,"candidate":candidate,"ci":context,"local_receipt_hash":digest(receipt),"manifest_hash":manifest["manifest_hash"],"registry_hash":registry["registry_hash"]}
        output["promotion_hash"]=digest(output)
        if args.out: atomic_json(args.out,output)
        print(json.dumps(output,sort_keys=True)); return 0
    except (ModeError,OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc: print(json.dumps({"status":"fail","error":str(exc) or exc.__class__.__name__},sort_keys=True),file=sys.stderr); return 1
if __name__ == "__main__": sys.exit(main(sys.argv[1:]))
