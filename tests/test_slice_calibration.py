"""Hostile promotion-calibration trust-model tests."""

from __future__ import annotations
import hashlib, importlib.util, json, os, subprocess, sys
from pathlib import Path
import pytest
from concurrent.futures import ProcessPoolExecutor

ROOT = Path(__file__).parents[1]; SCRIPTS = ROOT / "templates/scripts"
sys.path.insert(0, str(SCRIPTS))
from slice_calibration_core import CalibrationError, evaluate, sha256, validate_labels, validate_telemetry
from slice_session_registry_core import RegistryError, canonical, read_events, session_views
from slice_ledger_core import _append as ledger_append, _load as ledger_load
import slice_calibration as calibration_cli

H = lambda value: hashlib.sha256(value.encode()).hexdigest()
WINDOW = {"schema_version":1,"window_id":"w","started_at":"2026-01-01T00:00:00Z","ended_at":"2026-02-01T00:00:00Z"}

def _bootstrap_call(values):
    repo, transcript = values
    result=subprocess.run([sys.executable,str(SCRIPTS/"slice_calibration.py"),"--cwd",repo,"bootstrap","--transcript",transcript,"--session-id","session","--artifact-domain","code","--expected-control","ledger"],text=True,capture_output=True,check=False)
    return result.returncode

def root_meta(repo, session="s", thread=None, **changes):
    payload={"id":thread or session,"session_id":session,"parent_thread_id":None,"thread_source":"user","source":"user","cwd":str(repo),"cli_version":"0.145.0-alpha.13"}
    payload.update(changes)
    return {"timestamp":"2026-01-01T00:00:00Z","type":"session_meta","payload":payload}

def rehash(events):
    previous="0"*64
    for sequence,event in enumerate(events,1):
        event.update(sequence=sequence,previous_hash=previous)
        event["hash"]=hashlib.sha256(canonical({key:value for key,value in event.items() if key!="hash"})).hexdigest(); previous=event["hash"]
    return read_events([canonical(event) for event in events])

def registry(count=10, first_fail=False, exclude=False, open_control=False):
    events=[]; mono=1_000
    def add(kind,payload,ts):
        nonlocal mono
        unsigned={"schema_version":1,"sequence":len(events)+1,"timestamp":ts,"monotonic_ns":mono,"type":kind,"payload":payload,"previous_hash":events[-1]["hash"] if events else "0"*64}
        events.append({**unsigned,"hash":hashlib.sha256(canonical(unsigned)).hexdigest()}); mono += 10
    for i in range(count):
        common={"run_id_hash":H(f"r{i}"),"session_id_hash":H(f"s{i}")}
        add("activated",{**common,"provider":"codex_rollout_v1","artifact_domain":"code","expected_controls":["ledger"],"thread_id_hash":H(f"thread{i}"),"session_meta_sha256":H(f"meta{i}")},"2026-01-02T00:00:00Z")
        if not (open_control and i==0):
            add("control_started",{**common,"kind":"ledger"},"2026-01-02T00:00:01Z")
            add("control_ended",{**common,"kind":"ledger"},"2026-01-02T00:00:02Z")
        add("verification_attempt",{**common,"status":"fail" if first_fail and i==0 else "pass","receipt_sha256":H("v")},"2026-01-02T00:00:03Z")
        if first_fail and i==0: add("verification_attempt",{**common,"status":"pass","receipt_sha256":H("v2")},"2026-01-02T00:00:04Z")
        mono += 100
        add("terminal",{**common,"ledger_tail_hash":H("ledger_tail_hash"+str(i)),"handoff_sha256":H("handoff_sha256"+str(i)),"terminal_at":"2026-01-02T00:00:04Z"},"2026-01-02T00:00:04Z")
        add("domain_outcome",{**common,"next_domain":"verify"},"2026-01-02T00:00:05Z")
    if exclude:
        common={"run_id_hash":H("r10"),"session_id_hash":H("s10")}; add("activated",{**common,"provider":"codex_rollout_v1","artifact_domain":"code","expected_controls":["ledger"]},"2026-01-02T00:00:00Z"); add("excluded",{**common,"reason":"operator_cancelled","evidence_sha256":H("e")},"2026-01-02T00:00:01Z")
    return events

def rows(count=10):
    out=[]
    for i in range(count):
        out.append({"run_id_hash":H(f"r{i}"),"session_id_hash":H(f"s{i}"),
          "labels":{"schema_version":1,"path_reviews":[{"path":f"p{i}","classification":"candidate-owned","truth":"legitimate"}],"docs_only_dirty":"none"},
          "machine":{"terminal_at":"2026-01-02T00:00:04Z","paths":[{"path":f"p{i}","classification":"candidate-owned","delivered":True}],"foreign_managed_commit":False,"repair_required":False,"routing_detected":1,"routing_routed":1,"delivery_terminal":True},
          "telemetry":{"parser_observed":1,"parser_expected":1,"parser_unknown":0,"spawns":1,"waits":1,"provider":"codex","adapter":"v1"}})
    return out

def run(rs=None, ev=None):
    rs=rows() if rs is None else rs; ev=registry() if ev is None else ev
    manifest=[{"run_id_hash":r["run_id_hash"],"session_id_hash":r["session_id_hash"],**{name:H(name+str(i)) for name in ("label_sha256","telemetry_sha256","ledger_tail_hash","handoff_sha256","verification_sha256","baseline_sha256","backlog_sha256")}} for i,r in enumerate(rs)]
    return evaluate(rs,WINDOW,manifest,"0"*64,ev)

def test_green_authoritative_population_passes(): assert run()["verdict"] == "PASS"
def test_no_registry_or_2099_window_never_passes():
    assert run([],[])["verdict"] == "INSUFFICIENT_SAMPLE"
    future={**WINDOW,"started_at":"2099-01-01T00:00:00Z","ended_at":"2099-02-01T00:00:00Z"}
    assert evaluate([],future,[],"0"*64,registry())["verdict"] == "INSUFFICIENT_SAMPLE"
def test_ten_runs_one_session_rejected():
    rs=rows(); [r.update(session_id_hash=H("same")) for r in rs]
    with pytest.raises(CalibrationError): run(rs)
def test_omitted_bad_registry_session_rejected():
    with pytest.raises(CalibrationError): run(rows(),registry(11))
def test_arbitrary_exclusion_cannot_pass(): assert run(rows(),registry(exclude=True))["verdict"] == "INSUFFICIENT_SAMPLE"
@pytest.mark.parametrize("reviews", [[],[{"path":"x","classification":"candidate-owned","truth":"legitimate"}],[{"path":"p","classification":"candidate-owned","truth":"legitimate"}]*2])
def test_path_reviews_reject_missing_extra_duplicate(reviews):
    with pytest.raises(CalibrationError): validate_labels({"schema_version":1,"path_reviews":reviews,"docs_only_dirty":"none"},[{"path":"p","classification":"candidate-owned"}])
def test_aggregate_inflation_rejected():
    with pytest.raises(CalibrationError): validate_labels({"schema_version":1,"path_reviews":[],"docs_only_dirty":"none","reviewed":1_000_000})
def test_unknown_must_equal_expected_minus_observed_and_blocks():
    with pytest.raises(CalibrationError): validate_telemetry({"parser_observed":1,"parser_expected":2,"parser_unknown":0,"spawns":0,"waits":0,"provider":"p","adapter":"a"})
    rs=rows(); rs[0]["telemetry"].update(parser_expected=2,parser_unknown=1)
    assert run(rs)["verdict"] == "INSUFFICIENT_SAMPLE"
def test_zero_parser_expectation_is_unavailable_and_blocks():
    rs=rows(); rs[0]["telemetry"].update(parser_observed=0,parser_expected=0,parser_unknown=0)
    assert run(rs)["verdict"] == "INSUFFICIENT_SAMPLE"
def test_first_pass_comes_from_first_registry_attempt(): assert run(ev=registry(first_fail=True))["metrics"]["exact_state_first_pass"]["numerator"] == 9
def test_open_control_blocks_overhead(): assert run(ev=registry(open_control=True))["verdict"] == "INSUFFICIENT_SAMPLE"
def test_nested_same_kind_controls_close_lifo_without_corrupting_registry():
    events=registry(1); terminal=next(i for i,e in enumerate(events) if e["type"]=="terminal")
    common={"run_id_hash":H("r0"),"session_id_hash":H("s0")}
    original=events[:terminal]; tail=events[terminal:]
    # Replace the single pair with two nested starts and two ends.
    original=[e for e in original if e["type"] not in {"control_started","control_ended"}]
    def row(kind): return {"schema_version":1,"sequence":0,"timestamp":"2026-01-02T00:00:01Z","monotonic_ns":0,"type":kind,"payload":{**common,"kind":"ledger"},"previous_hash":"","hash":""}
    combined=[original[0],row("control_started"),row("control_started"),row("control_ended"),row("control_ended"),*tail]
    for n,event in enumerate(combined,1):
        event.update(sequence=n,monotonic_ns=n*10,previous_hash=combined[n-2]["hash"] if n>1 else "0"*64); unsigned={k:v for k,v in event.items() if k!="hash"}; event["hash"]=hashlib.sha256(canonical(unsigned)).hexdigest()
    view=session_views(combined)[H("s0")]
    assert not view["open"] and len(view["controls"])==2
def test_control_unavailable_terminates_latest_open_observation():
    events=registry(1); start=next(e for e in events if e["type"]=="control_started"); end=next(e for e in events if e["type"]=="control_ended")
    end_index=events.index(end); unavailable={**end,"type":"control_unavailable","payload":{**end["payload"],"reason":"operation_failed"}}
    events[end_index]=unavailable
    view=session_views(events)[H("s0")]
    assert not view["open"] and "ledger" in view["unavailable"]
def test_typed_control_na_still_blocks_promotion_claim():
    events=registry(); start=next(item for item in events if item["type"]=="control_started"); end=next(item for item in events if item["type"]=="control_ended" and item["payload"]["session_id_hash"]==start["payload"]["session_id_hash"]); end["type"]="control_unavailable"; end["payload"]={**end["payload"],"reason":"native_surface_unavailable"}
    assert run(ev=events)["verdict"] == "INSUFFICIENT_SAMPLE"
def test_foreign_commit_stop_precedence():
    rs=rows(); rs[0]["machine"]["foreign_managed_commit"]=True
    assert run(rs)["verdict"] == "STOP_FOREIGN_COMMIT"
def test_dataset_changes_when_authoritative_events_change(): assert sha256(run()) != sha256(run(ev=registry(first_fail=True)))
def test_registry_rejects_event_before_activation():
    events=registry(); events[0]["type"]="verification_attempt"; events[0]["payload"]={"run_id_hash":H("r0"),"session_id_hash":H("s0"),"status":"pass","receipt_sha256":H("v")}
    with pytest.raises(RegistryError): session_views(events)
def test_registry_rejects_vacuous_domain_transition():
    events=registry(); outcome=next(item for item in events if item["type"]=="domain_outcome"); outcome["payload"]["next_domain"]="code"
    with pytest.raises(RegistryError): session_views(events)
def test_legacy_activation_replays_but_blocks_promotion():
    events=registry()
    for event in events:
        if event["type"]=="activated": event["payload"].pop("thread_id_hash"); event["payload"].pop("session_meta_sha256")
    assert run(ev=events)["verdict"]=="INSUFFICIENT_SAMPLE"
def test_only_telemetry_calibration_controls_may_follow_outcome():
    events=registry(1); common={"run_id_hash":H("r0"),"session_id_hash":H("s0")}
    for kind,control in (("control_started","telemetry"),("control_ended","telemetry"),("control_started","calibration"),("control_unavailable","calibration")):
        payload={**common,"kind":control}; payload.update(reason="operation_failed") if kind=="control_unavailable" else None
        unsigned={"schema_version":1,"sequence":len(events)+1,"timestamp":"2026-01-02T00:00:06Z","monotonic_ns":events[-1]["monotonic_ns"]+1,"type":kind,"payload":payload,"previous_hash":events[-1]["hash"]}; events.append({**unsigned,"hash":hashlib.sha256(canonical(unsigned)).hexdigest()})
    assert session_views(events)[H("s0")]["open"]=={}
    events[-1]["type"]="verification_attempt"; events[-1]["payload"]={**common,"status":"pass","receipt_sha256":H("receipt")}
    with pytest.raises(RegistryError): session_views(rehash(events))
def test_rehashed_legacy_pre_outcome_naked_unavailable_replays_unknown_nonpass():
    events=registry(); index=next(i for i,item in enumerate(events) if item["type"]=="terminal")
    common={"run_id_hash":H("r0"),"session_id_hash":H("s0")}; previous=events[index-1]
    events.insert(index,{"schema_version":1,"sequence":0,"timestamp":"2026-01-02T00:00:03.5Z","monotonic_ns":previous["monotonic_ns"]+1,"type":"control_unavailable","payload":{**common,"kind":"ledger","reason":"operation_failed"},"previous_hash":"","hash":""})
    events=rehash(events); view=session_views(events)[H("s0")]
    assert view["ordering_unknown"] is True and "ledger" in view["unavailable"] and run(ev=events)["verdict"]!="PASS"
def test_rehashed_post_outcome_naked_control_unavailable_rejected():
    events=registry(1); common={"run_id_hash":H("r0"),"session_id_hash":H("s0")}; tail=events[-1]
    events.append({"schema_version":1,"sequence":0,"timestamp":"2026-01-02T00:00:07Z","monotonic_ns":tail["monotonic_ns"]+1,"type":"control_unavailable","payload":{**common,"kind":"calibration","reason":"operation_failed"},"previous_hash":"","hash":""})
    with pytest.raises(RegistryError,match="without start"): session_views(rehash(events))
@pytest.mark.parametrize("event_type,payload",[
    ("control_started",{"kind":"ledger"}),
    ("excluded",{"reason":"operator_cancelled","evidence_sha256":H("e")}),
    ("domain_outcome",{"next_domain":"other"}),
])
def test_rehashed_forbidden_post_outcome_events_rejected(event_type,payload):
    events=registry(1); common={"run_id_hash":H("r0"),"session_id_hash":H("s0")}
    tail=events[-1]; events.append({"schema_version":1,"sequence":0,"timestamp":"2026-01-02T00:00:07Z","monotonic_ns":tail["monotonic_ns"]+1,"type":event_type,"payload":{**common,**payload},"previous_hash":"","hash":""})
    with pytest.raises(RegistryError): session_views(rehash(events))
def test_terminal_binding_tamper_rejected():
    events=registry(); terminal=next(item for item in events if item["type"]=="terminal"); terminal["payload"]["handoff_sha256"]=H("tamper")
    with pytest.raises(CalibrationError): run(ev=events)
def test_delayed_terminal_observation_cannot_dilute_overhead():
    events=registry()
    for i in range(10):
        sid=H(f"s{i}"); start=next(item for item in events if item["type"]=="control_started" and item["payload"]["session_id_hash"]==sid); end=next(item for item in events if item["type"]=="control_ended" and item["payload"]["session_id_hash"]==sid)
        end["monotonic_ns"]=start["monotonic_ns"]+600_000_000
        terminal=next(item for item in events if item["type"]=="terminal" and item["payload"]["session_id_hash"]==sid); terminal["monotonic_ns"]=9_000_000_000_000+i; terminal["timestamp"]="2026-01-03T00:00:00Z"
    decision=run(ev=events)
    assert decision["verdict"]=="FAIL" and decision["metrics"]["overhead"]["value"]==pytest.approx(0.15)
def test_cli_real_registry_record_evaluate_fail_closed_and_tamper(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(parents=True); subprocess.run(["git","init","-q",str(repo)],check=True)
    cli=SCRIPTS/"slice_calibration.py"
    def call(*args): return subprocess.run([sys.executable,str(cli),"--cwd",str(repo),*args],text=True,capture_output=True,check=False)
    transcript=tmp_path/"session.jsonl"; transcript.write_text(json.dumps(root_meta(repo))+"\n")
    started=call("session-start","--run-id","r","--session-id","s","--provider","codex_rollout_v1","--artifact-domain","code","--expected-control","ledger","--transcript",str(transcript))
    assert started.returncode==0 and json.loads(started.stdout)["ok"] is True
    labels=tmp_path/"labels.json"; labels.write_text(json.dumps({"schema_version":1,"path_reviews":[],"docs_only_dirty":"none"}))
    record=call("record","--run-id","r","--session-id","s","--labels-file",str(labels))
    assert record.returncode!=0 and json.loads(record.stderr)["type"]=="error"
    window=tmp_path/"window.json"; window.write_text("{")
    evaluated=call("evaluate","--window-file",str(window))
    assert evaluated.returncode!=0 and json.loads(evaluated.stderr)["type"]=="error"
    registry_path=repo/".claude/state/slice_session_events.jsonl"; registry_path.write_text(registry_path.read_text()+"{\n"); registry_path.chmod(0o600)
    tampered=call("control-start","--run-id","r","--session-id","s","--kind","ledger")
    assert tampered.returncode!=0 and json.loads(tampered.stderr)["type"]=="error"
def test_control_na_rejects_arbitrary_reason(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True); cli=SCRIPTS/"slice_calibration.py"
    def call(*args): return subprocess.run([sys.executable,str(cli),"--cwd",str(repo),*args],text=True,capture_output=True,check=False)
    transcript=tmp_path/"session.jsonl"; transcript.write_text(json.dumps(root_meta(repo))+"\n")
    assert call("session-start","--run-id","r","--session-id","s","--provider","codex_rollout_v1","--artifact-domain","code","--expected-control","ledger","--transcript",str(transcript)).returncode==0
    bad=call("control-na","--run-id","r","--session-id","s","--kind","ledger","--reason","made up prose")
    assert bad.returncode==2 and json.loads(bad.stderr)["type"]=="error"

@pytest.mark.parametrize("mutation",["evil_version","unsupported_version","parent","subagent","source","nonleading","missing","wrong_root","wrong_cwd","symlink"])
def test_activation_hostiles_are_typed_byte_stable_and_private(tmp_path,mutation):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    cli=SCRIPTS/"slice_calibration.py"; transcript=tmp_path/"session.jsonl"; meta=root_meta(repo)
    if mutation=="evil_version": meta["payload"]["cli_version"]="0.145.0;SECRET_RAW_ID"
    elif mutation=="unsupported_version": meta["payload"]["cli_version"]="0.146.0"
    elif mutation=="parent": meta["payload"]["parent_thread_id"]="SECRET_RAW_PARENT"
    elif mutation=="subagent": meta["payload"].update(thread_source="subagent",source={"subagent":{"thread_spawn":{"parent_thread_id":"p","depth":1}}},parent_thread_id="p")
    elif mutation=="source": meta["payload"]["source"]="system"
    elif mutation=="nonleading": transcript.write_text(json.dumps({"type":"event_msg","payload":{}})+"\n")
    elif mutation=="missing": meta["payload"].pop("cli_version")
    elif mutation=="wrong_root": meta["payload"]["session_id"]="SECRET_WRONG_ROOT"
    elif mutation=="wrong_cwd": meta["payload"]["cwd"]=str(tmp_path)
    if mutation!="nonleading": transcript.write_text(json.dumps(meta)+"\n")
    if mutation=="symlink":
        target=tmp_path/"target.jsonl"; transcript.replace(target); transcript.symlink_to(target)
    registry_path=repo/".claude/state/slice_session_events.jsonl"; before=registry_path.read_bytes() if registry_path.exists() else b""
    result=subprocess.run([sys.executable,str(cli),"--cwd",str(repo),"session-start","--run-id","r","--session-id","s","--provider","codex_rollout_v1","--artifact-domain","code","--expected-control","ledger","--transcript",str(transcript)],text=True,capture_output=True,check=False)
    after=registry_path.read_bytes() if registry_path.exists() else b""; rendered=result.stdout+result.stderr+after.decode(errors="replace")
    assert result.returncode in {4,5} and before==after and "SECRET_RAW" not in rendered and "SECRET_WRONG_ROOT" not in rendered

def test_successful_activation_persists_hashes_not_raw_ids(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    secret="ROOT-IDENTITY-SECRET-CANARY"; thread="THREAD-IDENTITY-SECRET-CANARY"; transcript=tmp_path/"session.jsonl"; transcript.write_text(json.dumps(root_meta(repo,secret,thread))+"\n")
    argv=[sys.executable,str(SCRIPTS/"slice_calibration.py"),"--cwd",str(repo),"session-start","--run-id","r","--session-id",secret,"--provider","codex_rollout_v1","--artifact-domain","code","--expected-control","ledger","--transcript",str(transcript)]
    result=subprocess.run(argv,text=True,capture_output=True,check=False)
    persisted=(repo/".claude/state/slice_session_events.jsonl").read_text()
    assert result.returncode==0 and secret not in persisted and thread not in persisted and H(secret) in persisted and H(thread) in persisted and H(secret)!=H(thread)
    before=persisted.encode(); wrong=subprocess.run([*argv[:8],thread,*argv[9:]],text=True,capture_output=True,check=False)
    assert wrong.returncode==4 and (repo/".claude/state/slice_session_events.jsonl").read_bytes()==before


def test_bootstrap_discovers_unique_root_and_generates_run(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    home=tmp_path/"codex"; transcript=home/"sessions/2026/01/01/root.jsonl"; transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps(root_meta(repo,"root-session","thread-root"))+"\n")
    env={**os.environ,"CODEX_HOME":str(home),"CODEX_THREAD_ID":"thread-root"}
    result=subprocess.run([sys.executable,str(SCRIPTS/"slice_calibration.py"),"--cwd",str(repo),"bootstrap","--artifact-domain","implementation","--expected-control","ledger"],text=True,capture_output=True,env=env,check=False)
    body=json.loads(result.stdout); payload=body["result"]
    assert result.returncode==0 and payload["session_id_hash"]==H("root-session") and payload["resolution"]=="codex_thread_id"
    assert len(payload["run_id"])==36 and payload["binding_path"].endswith("slice_session_binding.json")
    assert "root-session" not in result.stdout and str(transcript) not in result.stdout


@pytest.mark.parametrize("count",[0,2])
def test_bootstrap_discovery_zero_or_multiple_fails_without_registry(tmp_path,count):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    home=tmp_path/"codex"
    for index in range(count):
        transcript=home/f"sessions/2026/01/0{index+1}/root.jsonl"; transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps(root_meta(repo,f"session-{index}","same-thread"))+"\n")
    env={**os.environ,"CODEX_HOME":str(home),"CODEX_THREAD_ID":"same-thread"}
    result=subprocess.run([sys.executable,str(SCRIPTS/"slice_calibration.py"),"--cwd",str(repo),"bootstrap","--artifact-domain","implementation","--expected-control","ledger"],text=True,capture_output=True,env=env,check=False)
    assert result.returncode==3 and "count=" in json.loads(result.stderr)["error"]
    assert not (repo/".claude/state/slice_session_events.jsonl").exists()


def test_bootstrap_rejects_explicit_root_mismatch_and_subagent(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    transcript=tmp_path/"root.jsonl"; transcript.write_text(json.dumps(root_meta(repo,"actual"))+"\n")
    base=[sys.executable,str(SCRIPTS/"slice_calibration.py"),"--cwd",str(repo),"bootstrap","--artifact-domain","implementation","--expected-control","ledger","--transcript",str(transcript)]
    mismatch=subprocess.run([*base,"--session-id","wrong"],text=True,capture_output=True,check=False)
    sub=root_meta(repo,"actual","child",thread_source="subagent",source={"subagent":{"thread_spawn":{"parent_thread_id":"p","depth":1}}},parent_thread_id="p")
    transcript.write_text(json.dumps(sub)+"\n")
    rejected=subprocess.run(base,text=True,capture_output=True,check=False)
    assert mismatch.returncode==3 and rejected.returncode==4 and not (repo/".claude/state/slice_session_events.jsonl").exists()


def test_concurrent_duplicate_root_bootstrap_has_one_append(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    transcript=tmp_path/"root.jsonl"; transcript.write_text(json.dumps(root_meta(repo,"session","thread"))+"\n")
    with ProcessPoolExecutor(max_workers=2) as pool:
        codes=list(pool.map(_bootstrap_call,[(str(repo),str(transcript))]*2))
    registry=repo/".claude/state/slice_session_events.jsonl"; before=registry.read_bytes()
    assert sorted(codes)==[0,3] and len(before.splitlines())==1
    assert _bootstrap_call((str(repo),str(transcript)))==3 and registry.read_bytes()==before


@pytest.mark.parametrize("hostile_component", ["state", "runs", "run"])
def test_bootstrap_rejects_hostile_state_symlink_without_outside_write(tmp_path, hostile_component):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    transcript=tmp_path/"root.jsonl"; transcript.write_text(json.dumps(root_meta(repo,"session","thread"))+"\n")
    outside=tmp_path/"outside"; outside.mkdir()
    claude=repo/".claude"; claude.mkdir()
    if hostile_component == "state": (claude/"state").symlink_to(outside, target_is_directory=True)
    else:
        state=claude/"state"; state.mkdir()
        if hostile_component == "runs": (state/"runs").symlink_to(outside, target_is_directory=True)
        else:
            runs=state/"runs"; runs.mkdir()
            # UUID is generated internally, so replace mkdir with an attacker-like hook
            # by rejecting every pre-existing link through a deterministic UUID.
            import slice_calibration as calibration_cli
            fixed="11111111-1111-4111-8111-111111111111"
            (runs/H(fixed)).symlink_to(outside, target_is_directory=True)
            original=calibration_cli.uuid.uuid4; calibration_cli.uuid.uuid4=lambda: __import__("uuid").UUID(fixed)
            try:
                args=type("Args",(),{"transcript":str(transcript),"session_id":"session","artifact_domain":"code","expected_control":["ledger"]})()
                with pytest.raises(Exception): calibration_cli._bootstrap(repo,args)
            finally: calibration_cli.uuid.uuid4=original
            assert list(outside.iterdir()) == [] and not (state/"slice_session_events.jsonl").exists()
            return
    result=subprocess.run([sys.executable,str(SCRIPTS/"slice_calibration.py"),"--cwd",str(repo),"bootstrap","--transcript",str(transcript),"--session-id","session","--artifact-domain","code","--expected-control","ledger"],text=True,capture_output=True,check=False)
    assert result.returncode != 0 and list(outside.iterdir()) == []
    assert not (outside/"slice_session_events.jsonl").exists()


@pytest.mark.parametrize("mutation", ["mode", "hardlink", "symlink"])
def test_protected_binding_metadata_is_enforced_on_read(tmp_path, mutation):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    transcript=tmp_path/"root.jsonl"; transcript.write_text(json.dumps(root_meta(repo,"session","thread"))+"\n")
    call=lambda *args: subprocess.run([sys.executable,str(SCRIPTS/"slice_calibration.py"),"--cwd",str(repo),*args],text=True,capture_output=True,check=False)
    assert call("window-create").returncode == 0
    boot=call("bootstrap","--transcript",str(transcript),"--session-id","session","--artifact-domain","code","--expected-control","ledger")
    assert boot.returncode == 0
    payload=json.loads(boot.stdout)["result"]; binding=repo/payload["binding_path"]
    if mutation == "mode": binding.chmod(0o644)
    elif mutation == "hardlink": os.link(binding, tmp_path/"binding-hardlink.json")
    else:
        saved=tmp_path/"saved-binding.json"; binding.replace(saved); binding.symlink_to(saved)
    result=call("window-status")
    assert result.returncode == 4


@pytest.mark.parametrize("mode", [0o770, 0o777])
def test_bootstrap_rejects_group_or_world_writable_managed_directory(tmp_path, mode):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    state=repo/".claude/state"; state.mkdir(parents=True); runs=state/"runs"; runs.mkdir(); runs.chmod(mode)
    transcript=tmp_path/"root.jsonl"; transcript.write_text(json.dumps(root_meta(repo,"session","thread"))+"\n")
    result=subprocess.run([sys.executable,str(SCRIPTS/"slice_calibration.py"),"--cwd",str(repo),"bootstrap","--transcript",str(transcript),"--session-id","session","--artifact-domain","code","--expected-control","ledger"],text=True,capture_output=True,check=False)
    assert result.returncode != 0 and not (state/"slice_session_events.jsonl").exists()


def test_bootstrap_rejects_hardlinked_registry_before_binding_or_outside_mutation(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    state=repo/".claude/state"; state.mkdir(parents=True)
    outside=tmp_path/"outside-registry"; outside.write_bytes(b""); outside.chmod(0o600)
    os.link(outside,state/"slice_session_events.jsonl"); before=outside.read_bytes()
    transcript=tmp_path/"root.jsonl"; transcript.write_text(json.dumps(root_meta(repo,"session","thread"))+"\n")
    result=subprocess.run([sys.executable,str(SCRIPTS/"slice_calibration.py"),"--cwd",str(repo),"bootstrap","--transcript",str(transcript),"--session-id","session","--artifact-domain","code","--expected-control","ledger"],text=True,capture_output=True,check=False)
    assert result.returncode == 4 and outside.read_bytes() == before
    assert not (state/"runs").exists()


def test_record_rejects_hardlinked_label_log_before_receipt_write(tmp_path, monkeypatch):
    root=tmp_path/"repo"; state_dir=root/".claude/state"; state_dir.mkdir(parents=True)
    outside=tmp_path/"outside-labels"; outside.write_bytes(b""); outside.chmod(0o600)
    os.link(outside,state_dir/"slice_calibration_labels.jsonl"); before=outside.read_bytes()
    run_id="run"; run_dir=state_dir/"runs"/H(run_id); run_dir.mkdir(parents=True)
    labels_file=tmp_path/"labels.json"; labels_file.write_text(json.dumps({"schema_version":1,"path_reviews":[],"docs_only_dirty":"none"}))
    ledger={"run_id":run_id,"last_event_hash":"a"*64}
    monkeypatch.setattr(calibration_cli,"_binding",lambda *_:(ledger,[],{},tmp_path/"telemetry","b"*64))
    monkeypatch.setattr(calibration_cli,"_machine_facts",lambda *_:({"paths":[]},{},{}))
    args=type("Args",(),{"run_id":run_id,"session_id":"session","labels_file":str(labels_file)})()
    with pytest.raises(CalibrationError) as raised: calibration_cli._record(root,args)
    assert raised.value.code == 4
    assert outside.read_bytes() == before and not (run_dir/"slice_calibration.json").exists()


def test_bound_transcript_allows_append_but_rejects_leading_mutation(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    transcript=tmp_path/"root.jsonl"; meta=root_meta(repo,"session","thread"); transcript.write_text(json.dumps(meta)+"\n")
    cli=SCRIPTS/"slice_calibration.py"
    def call(*args): return subprocess.run([sys.executable,str(cli),"--cwd",str(repo),*args],text=True,capture_output=True,check=False)
    assert call("window-create").returncode==0 and call("bootstrap","--transcript",str(transcript),"--session-id","session","--artifact-domain","code","--expected-control","ledger").returncode==0
    with transcript.open("a") as stream: stream.write(json.dumps({"timestamp":"2026-01-01T00:00:01Z","type":"event_msg","payload":{}})+"\n")
    assert call("window-status").returncode==0
    meta["timestamp"]="2026-01-01T00:00:00.1Z"; transcript.write_text(json.dumps(meta)+"\n")
    assert call("window-status").returncode==4


@pytest.mark.parametrize("mutation",["replacement","symlink"])
def test_bound_transcript_rejects_path_replacement_or_symlink(tmp_path,mutation):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    transcript=tmp_path/"root.jsonl"; content=json.dumps(root_meta(repo,"session","thread"))+"\n"; transcript.write_text(content)
    cli=SCRIPTS/"slice_calibration.py"
    def call(*args): return subprocess.run([sys.executable,str(cli),"--cwd",str(repo),*args],text=True,capture_output=True,check=False)
    assert call("window-create").returncode==0 and call("bootstrap","--transcript",str(transcript),"--session-id","session","--artifact-domain","code","--expected-control","ledger").returncode==0
    original=tmp_path/"original"; transcript.replace(original)
    if mutation=="replacement": transcript.write_text(content)
    else: transcript.symlink_to(original)
    assert call("window-status").returncode==4


def test_window_create_is_prospective_unique_and_legacy_activation_excluded(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    cli=SCRIPTS/"slice_calibration.py"
    transcript=tmp_path/"legacy.jsonl"; transcript.write_text(json.dumps(root_meta(repo,"legacy"))+"\n")
    def call(*args): return subprocess.run([sys.executable,str(cli),"--cwd",str(repo),*args],text=True,capture_output=True,check=False)
    assert call("session-start","--run-id","legacy-run","--session-id","legacy","--provider","codex_rollout_v1","--artifact-domain","code","--expected-control","ledger","--transcript",str(transcript)).returncode==0
    created=call("window-create"); duplicate=call("window-create"); status=call("window-status")
    assert created.returncode==0 and duplicate.returncode==3
    assert json.loads(status.stdout)["result"]["counter"]=="0/10" and json.loads(status.stdout)["result"]["activated"]==0
    assert call("window-close").returncode==0
    sealed=call("window-status"); evaluated=call("evaluate")
    assert json.loads(sealed.stdout)["result"]["counter"]=="0/10"
    assert evaluated.returncode==0 and json.loads(evaluated.stdout)["result"]["decision"]["verdict"]=="INSUFFICIENT_SAMPLE"


def test_external_window_is_diagnostic_only_and_rejected_with_canonical(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    cli=SCRIPTS/"slice_calibration.py"; window=tmp_path/"window.json"; window.write_text(json.dumps(WINDOW))
    def call(*args): return subprocess.run([sys.executable,str(cli),"--cwd",str(repo),*args],text=True,capture_output=True,check=False)
    legacy=call("evaluate","--window-file",str(window)); decision=json.loads(legacy.stdout)["result"]["decision"]
    assert legacy.returncode==0 and decision["verdict"]=="LEGACY_NON_PROMOTABLE" and decision["authority"]=="legacy_non_promotable"
    assert call("window-create").returncode==0 and call("evaluate","--window-file",str(window)).returncode==3


def test_labels_template_is_exhaustive_unknown_and_refuses_overwrite(tmp_path,monkeypatch):
    root=tmp_path/"repo"; (root/".claude/state").mkdir(parents=True)
    machine={"paths":[{"path":"a.py","classification":"candidate-owned"},{"path":"docs/x.md","classification":"foreign"}]}
    monkeypatch.setattr(calibration_cli,"_binding",lambda *_: ({},[],{},Path("telemetry"),"a"*64))
    monkeypatch.setattr(calibration_cli,"_machine_facts",lambda *_: (machine,{},{}))
    output=tmp_path/"labels.json"; args=type("Args",(),{"run_id":"r","session_id":"s","output":str(output)})()
    result=calibration_cli._labels_template(root,args)
    assert result["human_edit_required"] is True and [item["truth"] for item in result["labels"]["path_reviews"]]==["unknown","unknown"]
    with pytest.raises(CalibrationError): calibration_cli._labels_template(root,args)


def test_open_window_count_increments_for_bound_eligible_receipt(tmp_path,monkeypatch):
    root=tmp_path/"repo"; root.mkdir(); subprocess.run(["git","init","-q",str(root)],check=True)
    cli=SCRIPTS/"slice_calibration.py"; transcript=tmp_path/"root.jsonl"; transcript.write_text(json.dumps(root_meta(root,"session","thread"))+"\n")
    def call(*args): return subprocess.run([sys.executable,str(cli),"--cwd",str(root),*args],text=True,capture_output=True,check=False)
    assert call("window-create").returncode==0
    boot=call("bootstrap","--transcript",str(transcript),"--session-id","session","--artifact-domain","code","--expected-control","ledger")
    result=json.loads(boot.stdout)["result"]; identity={"run_id_hash":H(result["run_id"]),"session_id_hash":H("session")}
    monkeypatch.setattr(calibration_cli,"_verified_rows",lambda *_:[identity])
    state=calibration_cli._read_window_state(root); members,eligible=calibration_cli._window_population(root,state)
    assert members==[identity] and eligible==1


def test_closed_window_zero_label_tail_blocks_post_close_receipt(tmp_path,monkeypatch):
    root=tmp_path/"repo"; root.mkdir(); subprocess.run(["git","init","-q",str(root)],check=True)
    cli=SCRIPTS/"slice_calibration.py"; transcript=tmp_path/"root.jsonl"; transcript.write_text(json.dumps(root_meta(root,"session","thread"))+"\n")
    def call(*args): return subprocess.run([sys.executable,str(cli),"--cwd",str(root),*args],text=True,capture_output=True,check=False)
    assert call("window-create").returncode==0
    boot=call("bootstrap","--transcript",str(transcript),"--session-id","session","--artifact-domain","code","--expected-control","ledger")
    result=json.loads(boot.stdout)["result"]; identity={"run_id_hash":H(result["run_id"]),"session_id_hash":H("session")}
    assert call("window-close").returncode==0
    monkeypatch.setattr(calibration_cli,"_verified_rows",lambda *_:[identity])
    state=calibration_cli._read_window_state(root); members,eligible=calibration_cli._window_population(root,state,closed=True)
    assert members==[identity] and eligible==0


def test_sealed_window_excludes_typed_blocked_legacy(tmp_path,monkeypatch):
    root=tmp_path/"repo"; state_dir=root/".claude/state"; state_dir.mkdir(parents=True)
    events=registry(0,exclude=True)
    registry_path=state_dir/"slice_session_events.jsonl"
    registry_path.write_bytes(b"".join(canonical(item)+b"\n" for item in events)); registry_path.chmod(0o600)
    state={"schema_version":1,"window_id":"w","status":"closed","started_at":"2026-01-01T00:00:00Z","ended_at":"2026-02-01T00:00:00Z","created_at":"2026-01-01T00:00:00Z","registry_tail_hash":events[-1]["hash"],"label_log_tail_hash":"0"*64,"members":[]}
    monkeypatch.setattr(calibration_cli,"_verified_rows",lambda *_:[])
    members,eligible=calibration_cli._window_population(root,state,closed=True)
    assert members==[] and eligible==0

def _ledger_call(repo, *args):
    return subprocess.run([sys.executable,str(SCRIPTS/"slice_ledger.py"),"--cwd",str(repo),*args],text=True,capture_output=True,check=False)

def _bound_verification_repo(tmp_path, status):
    repo=tmp_path/"repo"; repo.mkdir(parents=True); subprocess.run(["git","init","-q",str(repo)],check=True)
    assert _ledger_call(repo,"acquire","--slice-id","s","--artifact-contract","base","--allowed-path","a.py","--session-id","sess","--run-id","run").returncode==0
    ledger=repo/".claude/state/slice_ledger.json"; events=repo/".claude/state/slice_events.jsonl"; state,history=ledger_load(ledger,events)
    ledger_append(events,"baseline_bound",{"run_id":"run","revision":2,"updated_at":"2026-01-01T00:00:00Z","baseline_sha256":"b"*64,"baseline_path":".claude/state/runs/base/slice_baseline.json"},history); state,history=ledger_load(ledger,events)
    receipt={"status":status}; run_hash=H("run"); path=repo/f".claude/state/runs/{run_hash}/slice_verification.json"; path.parent.mkdir(parents=True); path.write_bytes(canonical(receipt)+b"\n"); path.chmod(0o600); digest=hashlib.sha256(canonical(receipt)).hexdigest()
    ledger_append(events,"verification_bound",{"run_id":"run","revision":3,"updated_at":"2026-01-01T00:00:01Z","verification_sha256":digest,"state_sha256":"c"*64,"verification_path":f".claude/state/runs/{run_hash}/slice_verification.json"},history); ledger_load(ledger,events)
    return repo,digest

def _repair_update(values):
    repo,contract,path=values
    return _ledger_call(Path(repo),"update","--run-id","run","--session-id","sess","--revision","3","--artifact-contract",contract,"--allowed-path","a.py","--allowed-path",path,"--reason","repair immutable fail","--provenance-actor","test","--provenance-source","verified_recon","--provenance-evidence-sha256","d"*64).returncode

def test_post_fail_repair_expansion_is_bound_concurrent_and_pass_immutable(tmp_path):
    repo,failed_sha=_bound_verification_repo(tmp_path/"fail","fail")
    with ProcessPoolExecutor(max_workers=2) as pool: codes=list(pool.map(_repair_update,[(str(repo),"repair one","b.py"),(str(repo),"repair two","c.py")]))
    assert sorted(codes)==[0,3]
    event=json.loads((repo/".claude/state/slice_events.jsonl").read_text().splitlines()[-1]); assert event["type"]=="contract_expanded" and event["payload"]["post_fail_repair"] is True and event["payload"]["failed_verification_sha256"]==failed_sha
    state=json.loads((repo/".claude/state/slice_ledger.json").read_text()); assert state["verification_sha256"]==failed_sha and set(state["allowed_paths"]) >= {"a.py"}
    passed,_=_bound_verification_repo(tmp_path/"pass","pass"); assert _repair_update((str(passed),"forbidden","b.py"))==3

def test_post_fail_repair_event_rehash_tamper_is_rejected(tmp_path):
    repo,_=_bound_verification_repo(tmp_path,"fail"); assert _repair_update((str(repo),"repair","b.py"))==0
    ledger=repo/".claude/state/slice_ledger.json"; events=repo/".claude/state/slice_events.jsonl"; rows=[json.loads(line) for line in events.read_text().splitlines()]; rows[-1]["payload"]["post_fail_repair"]=False
    unsigned={key:value for key,value in rows[-1].items() if key!="hash"}; rows[-1]["hash"]=hashlib.sha256(canonical(unsigned)).hexdigest(); events.write_text("".join(json.dumps(row,sort_keys=True,separators=(",",":"))+"\n" for row in rows)); state=json.loads(ledger.read_text()); state["last_event_hash"]=rows[-1]["hash"]; ledger.write_text(json.dumps(state,sort_keys=True,separators=(",",":"))+"\n")
    assert _ledger_call(repo,"status").returncode==4
