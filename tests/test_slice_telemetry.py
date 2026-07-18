"""Black-box acceptance tests for Slice 4A diagnostic telemetry."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "templates/scripts"
LEDGER, GIT = SCRIPTS / "slice_ledger.py", SCRIPTS / "slice_git.py"
TELEMETRY = SCRIPTS / "slice_telemetry.py"


def run(script: Path, repo: Path, *args: str) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(script), "--cwd", str(repo), *args], text=True, capture_output=True, check=False)
    output = result.stdout if result.returncode == 0 else result.stderr
    assert output, result
    return result.returncode, json.loads(output)


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "private-user-canary" / "repo"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    (root / "work.txt").write_text("baseline\n")
    (root / ".gitignore").write_text(".claude/state/\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)
    assert run(LEDGER, root, "acquire", "--slice-id", "s", "--artifact-contract", "implement", "--allowed-path", "work.txt", "--session-id", "sess", "--run-id", "run1")[0] == 0
    assert run(GIT, root, "capture", "--run-id", "run1", "--session-id", "sess", "--revision", "1")[0] == 0
    return root


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def codex_sources(tmp_path: Path, root: Path, *, terminal_b: bool = False) -> list[Path]:
    def meta(thread_id: str, parent: str | None, depth: int, cwd: Path) -> dict:
        payload = {"id": thread_id, "session_id": "sess", "parent_thread_id": parent, "thread_source": "user" if depth == 0 else "subagent", "source": "user" if depth == 0 else {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": depth, "agent_path": "/sanitized/worker", "agent_nickname": "SECRET_AGENT"}}}, "cwd": str(cwd), "cli_version": "0.145.0-alpha.13"}
        return {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": payload}

    root_rows = [
        meta("sess", None, 0, root),
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "response_item", "payload": {"type": "function_call", "name": "spawn_agent", "call_id": "spawn-a", "arguments": "SECRET_CANARY prompt AgentAlice"}},
        {"timestamp": "2026-01-01T00:00:02.1Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "spawn-a", "output": "SECRET_CANARY"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "response_item", "payload": {"type": "function_call", "name": "spawn_agent", "call_id": "spawn-b", "arguments": "{}"}},
        {"timestamp": "2026-01-01T00:00:03.1Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "spawn-b", "output": "ok"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "response_item", "payload": {"type": "function_call", "name": "wait_agent"}},
        {"timestamp": "2026-01-01T00:00:04.5Z", "type": "response_item", "payload": {"type": "function_call", "name": "SECRET_TOOL_CANARY", "call_id": "unknown-tool", "arguments": "SECRET_CANARY"}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 110, "input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 80}, "last_token_usage": {"total_tokens": 110}}}},
        {"timestamp": "2026-01-01T00:00:06Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 150, "input_tokens": 130, "output_tokens": 20, "cached_input_tokens": 110}, "last_token_usage": {"total_tokens": 40}}}},
        {"timestamp": "2026-01-01T00:00:07Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 7, "input_tokens": 5, "output_tokens": 2, "cached_input_tokens": 0}, "last_token_usage": {"total_tokens": 7}}}},
        {"timestamp": "2026-01-01T00:00:08Z", "type": "event_msg", "payload": {"type": "task_completed", "output": "SECRET_CANARY"}},
    ]
    child_a = [
        meta("child-a", "sess", 1, root / "subdir"),
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "response_item", "payload": {"type": "function_call", "name": "wait_agent"}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg", "payload": {"type": "task_completed"}},
    ]
    child_b = [
        meta("child-b", "sess", 1, root),
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg", "payload": {"type": "task_started"}},
    ]
    if terminal_b:
        child_b.append({"timestamp": "2026-01-01T00:00:09Z", "type": "event_msg", "payload": {"type": "task_completed"}})
    (root / "subdir").mkdir(exist_ok=True)
    return [write_jsonl(tmp_path / "root.jsonl", root_rows), write_jsonl(tmp_path / "a.jsonl", child_a), write_jsonl(tmp_path / "b.jsonl", child_b)]


def telemetry_args(sources: list[Path], command: str = "inspect") -> list[str]:
    args = [command, "--provider", "codex_rollout_v1", "--run-id", "run1", "--session-id", "sess"]
    for source in sources:
        args += ["--transcript", str(source)]
    return args


def test_nested_codex_dedup_wait_scopes_tokens_privacy_and_right_censor(tmp_path: Path) -> None:
    root = repo(tmp_path)
    sources = codex_sources(tmp_path, root)
    code, value = run(TELEMETRY, root, *telemetry_args(sources))
    observation = value["result"]["observation"]
    metrics = observation["metrics"]
    assert code == 0 and metrics["spawns"]["value"] == 2
    assert metrics["waits"]["value"] == {"root": 1, "child": 1, "all": 2}
    assert metrics["background_completion"]["coverage_status"] == "right_censored"
    # 110 -> 150 contributes 150, reset to 7 contributes another 7; cached is a
    # separate snapshot and is never added to the cumulative delta.
    assert metrics["tokens"]["value"]["segmented_lower_bound"] == 157
    assert metrics["tokens"]["coverage_status"] == "partial" and "counter_reset" in metrics["tokens"]["unknown_reasons"]
    assert metrics["baseline_dirty"]["value"] == 0
    assert metrics["activation_delay_seconds"]["coverage_status"] == "complete"
    assert metrics["first_verification_delay_seconds"]["value"] is None
    assert metrics["slice_closure"]["coverage_status"] == "right_censored"
    rendered = json.dumps(value)
    assert "SECRET_CANARY" not in rendered and "AgentAlice" not in rendered and "SECRET_AGENT" not in rendered and "SECRET_TOOL_CANARY" not in rendered
    assert str(root) not in rendered and "private-user-canary" not in rendered
    assert all(set(item) == {"thread_hash", "root_hash", "parent_hash", "depth"} for item in observation["thread_identities"])


def test_complete_background_and_source_generation_rotation(tmp_path: Path) -> None:
    root = repo(tmp_path)
    sources = codex_sources(tmp_path, root, terminal_b=True)
    first = run(TELEMETRY, root, *telemetry_args(sources))[1]["result"]["observation"]
    assert first["metrics"]["background_completion"]["coverage_status"] == "complete"
    before = first["source_generations"][0]["generation"]
    rows = [json.loads(line) for line in sources[0].read_text().splitlines()]
    write_jsonl(sources[0], rows)
    after = run(TELEMETRY, root, *telemetry_args(sources))[1]["result"]["observation"]["source_generations"][0]["generation"]
    assert before != after


def test_orphan_child_does_not_inflate_successful_spawn_count(tmp_path: Path) -> None:
    root = repo(tmp_path)
    sources = codex_sources(tmp_path, root, terminal_b=True)
    rows = [json.loads(line) for line in sources[0].read_text().splitlines()]
    rows = [row for row in rows if not (row.get("payload", {}).get("type") == "function_call_output" and row.get("payload", {}).get("call_id") == "spawn-b")]
    write_jsonl(sources[0], rows)
    observation = run(TELEMETRY, root, *telemetry_args(sources))[1]["result"]["observation"]
    assert observation["metrics"]["spawns"]["value"] == 1
    assert observation["metrics"]["spawns"]["coverage_status"] == "partial"
    assert "missing_parent_spawn" in observation["unknown_reasons"]


def test_wrapper_adapter_is_explicit_and_clock_skew_is_not_inferred(tmp_path: Path) -> None:
    root = repo(tmp_path)
    source = write_jsonl(tmp_path / "wrapper.jsonl", [
        {"type": "booster_wrapper_meta", "schema_version": 1, "provider": "booster_wrapper_v1", "wrapper_version": 1, "root_session_id": "sess", "cwd": str(root)},
        {"timestamp": "2030-01-01T00:00:02Z", "thread_id": "sess", "parent_thread_id": None, "depth": 0, "event": "start"},
        {"timestamp": "2030-01-01T00:00:01Z", "thread_id": "sess", "event": "progress"},
        {"timestamp": "2030-01-01T00:00:03Z", "thread_id": "sess", "event": "terminal"},
    ])
    args = ["inspect", "--provider", "booster_wrapper_v1", "--transcript", str(source), "--run-id", "run1", "--session-id", "sess"]
    code, value = run(TELEMETRY, root, *args)
    assert code == 0 and value["result"]["observation"]["limitations"][1].startswith("wall_and_provider")
    activation = value["result"]["observation"]["metrics"]["activation_delay_seconds"]
    assert activation["value"] is None and activation["unknown_reasons"] == ["clock_skew_negative_duration"]
    wrong = ["inspect", "--provider", "codex_rollout_v1", "--transcript", str(source), "--run-id", "run1", "--session-id", "sess"]
    assert run(TELEMETRY, root, *wrong)[0] == 4


@pytest.mark.parametrize("mutation", ["schema_drift", "truncated", "wrong_project", "missing_terminal"])
def test_unknown_and_invalid_sources_never_become_fictional_zero(tmp_path: Path, mutation: str) -> None:
    root = repo(tmp_path)
    sources = codex_sources(tmp_path, root, terminal_b=True)
    if mutation == "schema_drift":
        with sources[0].open("a") as stream:
            stream.write(json.dumps({"timestamp": "2026-01-01T00:01:00Z", "type": "future", "payload": {"type": "future_event"}}) + "\n")
        code, value = run(TELEMETRY, root, *telemetry_args(sources))
        metric = value["result"]["observation"]["metrics"]["parser_coverage"]
        assert code == 0 and metric["coverage_status"] == "partial" and metric["value"] < 1
    elif mutation == "truncated":
        with sources[0].open("a") as stream: stream.write("{")
        assert run(TELEMETRY, root, *telemetry_args(sources))[0] == 4
    elif mutation == "wrong_project":
        rows = [json.loads(line) for line in sources[0].read_text().splitlines()]
        rows[0]["payload"]["cwd"] = str(tmp_path / "other")
        write_jsonl(sources[0], rows)
        assert run(TELEMETRY, root, *telemetry_args(sources))[0] == 4
    else:
        rows = [json.loads(line) for line in sources[0].read_text().splitlines()]
        rows = [row for row in rows if row.get("payload", {}).get("type") != "task_completed"]
        write_jsonl(sources[0], rows)
        code, value = run(TELEMETRY, root, *telemetry_args(sources))
        assert code == 0 and value["result"]["observation"]["metrics"]["background_completion"]["value"] == 2


def test_record_status_idempotency_tamper_and_incidental_commit_exclusion(tmp_path: Path) -> None:
    root = repo(tmp_path)
    sources = codex_sources(tmp_path, root, terminal_b=True)
    (root / "incidental.txt").write_text("not slice closure\n")
    subprocess.run(["git", "-C", str(root), "add", "incidental.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "incidental"], check=True)
    args = telemetry_args(sources, "record")
    code, first = run(TELEMETRY, root, *args)
    code2, second = run(TELEMETRY, root, *args)
    assert code == code2 == 0 and first["result"]["receipt_sha256"] == second["result"]["receipt_sha256"]
    assert first["result"]["receipt"]["ledger"]["commit_oid"] is None
    assert run(TELEMETRY, root, "status", "--run-id", "run1", "--session-id", "sess")[0] == 0
    log = root / ".claude/state/slice_calibration.jsonl"
    item = json.loads(log.read_text()); item["ledger_event_hash"] = "0" * 64
    log.write_text(json.dumps(item) + "\n"); os.chmod(log, 0o600)
    assert run(TELEMETRY, root, "status", "--run-id", "run1", "--session-id", "sess")[0] == 4


def test_live_scale_evidence_is_bounded_and_round_trips_under_cap(tmp_path: Path) -> None:
    root = repo(tmp_path)
    sources = codex_sources(tmp_path, root, terminal_b=True)
    rows = [json.loads(line) for line in sources[0].read_text().splitlines()]
    bulk = [{"timestamp": "2026-01-01T00:00:10Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "SCALE_SECRET_CANARY"}} for _ in range(5805)]
    write_jsonl(sources[0], [rows[0], *bulk, *rows[1:]])
    args = telemetry_args(sources, "record")
    code, value = run(TELEMETRY, root, *args)
    receipt = value["result"]["receipt"]
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    assert code == 0 and len(encoded.encode()) < 256 * 1024
    assert "SCALE_SECRET_CANARY" not in encoded and "evidence_refs" not in encoded
    assert len(receipt["observation"]["evidence_index"]) <= len(sources) + 3
    source_set = receipt["observation"]["evidence_index"][0]
    assert source_set["recognized"]["count"] >= 5805 and len(source_set["recognized"]["ranges"]) <= 64
    for metric in receipt["observation"]["metrics"].values():
        assert "evidence_set_ids" in metric and "evidence_counts" in metric
    status_code, status = run(TELEMETRY, root, "status", "--run-id", "run1", "--session-id", "sess")
    assert status_code == 0 and status["result"]["receipt_sha256"] == value["result"]["receipt_sha256"]
    assert run(TELEMETRY, root, *args)[1]["result"]["receipt_sha256"] == value["result"]["receipt_sha256"]


def test_bounds_and_same_root_session_are_fail_closed(tmp_path: Path) -> None:
    root = repo(tmp_path)
    source = tmp_path / "oversized.jsonl"
    source.write_bytes(b"{" + b"x" * (256 * 1024) + b"}\n")
    assert run(TELEMETRY, root, *telemetry_args([source]))[0] == 5
    sources = codex_sources(tmp_path / "identity", root)
    rows = [json.loads(line) for line in sources[1].read_text().splitlines()]
    rows[0]["payload"]["session_id"] = "other-root"
    write_jsonl(sources[1], rows)
    assert run(TELEMETRY, root, *telemetry_args(sources))[0] == 4
