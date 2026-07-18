"""Hostile promotion-calibration trust-model tests."""

from __future__ import annotations
import hashlib, importlib.util, json, subprocess, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).parents[1]; SCRIPTS = ROOT / "templates/scripts"
sys.path.insert(0, str(SCRIPTS))
from slice_calibration_core import CalibrationError, evaluate, sha256, validate_labels, validate_telemetry
from slice_session_registry_core import RegistryError, canonical, session_views

H = lambda value: hashlib.sha256(value.encode()).hexdigest()
WINDOW = {"schema_version":1,"window_id":"w","started_at":"2026-01-01T00:00:00Z","ended_at":"2026-02-01T00:00:00Z"}

def registry(count=10, first_fail=False, exclude=False, open_control=False):
    events=[]; mono=1_000
    def add(kind,payload,ts):
        nonlocal mono
        unsigned={"schema_version":1,"sequence":len(events)+1,"timestamp":ts,"monotonic_ns":mono,"type":kind,"payload":payload,"previous_hash":events[-1]["hash"] if events else "0"*64}
        events.append({**unsigned,"hash":hashlib.sha256(canonical(unsigned)).hexdigest()}); mono += 10
    for i in range(count):
        common={"run_id_hash":H(f"r{i}"),"session_id_hash":H(f"s{i}")}
        add("activated",{**common,"provider":"codex_rollout_v1","artifact_domain":"code","expected_controls":["ledger"]},"2026-01-02T00:00:00Z")
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
def test_typed_control_na_still_blocks_promotion_claim():
    events=registry(); start=next(item for item in events if item["type"]=="control_started"); start["type"]="control_unavailable"; start["payload"]={**start["payload"],"reason":"provider did not expose timing"}
    events.remove(next(item for item in events if item["type"]=="control_ended" and item["payload"]["session_id_hash"]==start["payload"]["session_id_hash"]))
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
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q",str(repo)],check=True)
    cli=SCRIPTS/"slice_calibration.py"
    def call(*args): return subprocess.run([sys.executable,str(cli),"--cwd",str(repo),*args],text=True,capture_output=True,check=False)
    started=call("session-start","--run-id","r","--session-id","s","--provider","codex_rollout_v1","--artifact-domain","code","--expected-control","ledger")
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
