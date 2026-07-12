#!/usr/bin/env python3
"""Executable regression checks for Rolling Memory debt hardening."""
from __future__ import annotations

import importlib.util
import json
import multiprocessing
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def emit_worker(script: str, log: str, worker: int) -> None:
    telemetry = load(f"memory_telemetry_{worker}", Path(script))
    for index in range(100):
        telemetry.emit_injection(
            log_path=log, project_root="/tmp/project", source="test",
            memory_ids=[worker * 1000 + index], memory_types={"directive": 1},
            char_count=10, token_estimate=3, session_id=str(worker),
        )


def main() -> int:
    rm_path = ROOT / "templates/scripts/rolling_memory.py"
    telemetry_path = ROOT / "templates/scripts/memory_telemetry.py"
    rm = load("rolling_memory_debt_test", rm_path)
    telemetry = load("memory_telemetry_debt_test", telemetry_path)
    with tempfile.TemporaryDirectory() as raw:
        temp = Path(raw)
        rm.DB_PATH = temp / "memory.db"
        rm.init_db()
        conn = sqlite3.connect(rm.DB_PATH)
        conn.execute("INSERT INTO agent_memory(memory_type, content, scope) VALUES('directive','keep evidence','global')")
        conn.commit()
        before = conn.execute("SELECT access_count FROM agent_memory").fetchone()[0]
        conn.close()
        rm.get_connection = lambda: (_ for _ in ()).throw(AssertionError("writable connection used"))
        assert "keep evidence" in rm.build_context()
        conn = sqlite3.connect(rm.DB_PATH)
        assert conn.execute("SELECT access_count FROM agent_memory").fetchone()[0] == before
        conn.close()
        try:
            rm.build_context(scope="")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid scope accepted")

        log = temp / "telemetry.jsonl"
        processes = [multiprocessing.Process(target=emit_worker, args=(str(telemetry_path), str(log), i)) for i in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join()
            assert process.exitcode == 0
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        assert rows and all(row["row_count"] == 1 for row in rows)
        size_before = log.stat().st_size
        telemetry.emit_injection(log_path=log, project_root="x" * 9000, source="test",
                                 memory_ids=[1], memory_types={"directive": 1},
                                 char_count=1, token_estimate=1)
        assert log.stat().st_size == size_before
        report = telemetry.build_report(30, log, rm.DB_PATH)
        assert "token_estimate" in report and "token_estimate" not in report["by_type"]
    print(f"PASS: readonly render, activity semantics, {len(rows)} concurrent JSON rows, cap, report schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
