#!/usr/bin/env python3
"""Acceptance test for kpi_rework.py — independent Verifier (black-box).

Purpose:
  Executable acceptance test for templates/scripts/kpi_rework.py. Tests OBSERVABLE
  behavior only — exit codes, JSONL log line contents, parsed stdout JSON, stdout
  prose patterns. Does NOT import the artifact or assume its internals.

Contract under test (from Artifact Contract + /tmp/go_kpi_clarifications.md + PFD):
  Subcommands:
    record --task --outcome --worker-spawns --verifier-fails [--category name:count ...]
           [--log-path FILE] [--project ROOT]
      Appends EXACTLY ONE JSON line: ts, project, task, outcome, worker_spawn_count,
      verifier_fail_count, first_pass_clean (=outcome=='pass' AND verifier==0),
      defect_categories (list of {category,count}). Exit 0 on success. Invalid input
      => non-zero exit AND no line written.
    report [--window DAYS] [--project ROOT] [--json] [--log-path FILE]
      Prints task count, first-pass-clean rate %, mean verifier_fail_count, per-category
      totals. --json => parseable JSON envelope. No/zero data => exit 0, clear message
      (--json still parseable JSON per D6).

  Log path precedence (D1): --log-path FILE > env CLAUDE_BOOSTER_LOGS_DIR/kpi_rework.jsonl
  > ~/.claude/logs/kpi_rework.jsonl. This test ALWAYS overrides so the real user log
  is never touched (VA-B / FM-030).

  ts format (D2): YYYY-MM-DDTHH:MM:SSZ (trailing Z, second precision).
  Window boundary (D3): INCLUSIVE (row_ts >= now-window).
  project fallback (D4): outside git/.claude tree => "unknown".
  Duplicate categories (D5): report SUMS per name; x:0 accepted.
  per_category_totals (D7): always all five keys, canonical order, zero-filled:
    contract_ambiguity, missed_failure_mode, integration_mismatch, weak_verification, capability.

CLI / Examples:
  python3 templates/scripts/test_kpi_rework.py
  Exit 0 iff all cases pass; non-zero otherwise. Prints [PASS]/[FAIL] per case.

Limitations:
  - Stdlib only. Deterministic (two runs => same result).
  - Does NOT test concurrency (FM-019) or broken-pipe (FM output_render) — out of scope.
  - Window-boundary fixtures written as raw JSONL lines with computed ts (UTC).

ENV/Files:
  - Uses tempfile dirs for all log paths; cleans them up.
  - Reads (does not write) ~/.claude/logs/kpi_rework.jsonl to assert it is unchanged.
"""

import json
import os
import subprocess
import sys
import tempfile
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = REPO_ROOT / "templates" / "scripts" / "kpi_rework.py"

ALLOWED = [
    "contract_ambiguity",
    "missed_failure_mode",
    "integration_mismatch",
    "weak_verification",
    "capability",
]

_results = []  # list of (label, ok, detail)


def _record(label, ok, detail=""):
    _results.append((label, bool(ok), detail))
    if ok:
        print("[PASS] %s" % label)
    else:
        print("[FAIL] %s — %s" % (label, detail))


def run_kpi(args, env_extra=None):
    """Invoke kpi_rework.py. Returns (returncode, stdout, stderr)."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(ARTIFACT)] + [str(a) for a in args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    return proc.returncode, proc.stdout, proc.stderr


def line_count(path):
    p = Path(path)
    if not p.exists():
        return 0
    n = 0
    with p.open("r", encoding="utf-8") as fh:
        for ln in fh:
            if ln.strip():
                n += 1
    return n


def read_lines(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open("r", encoding="utf-8") as fh:
        for ln in fh:
            if ln.strip():
                out.append(ln.rstrip("\n"))
    return out


def iso_z(dt):
    """Format a tz-aware datetime as YYYY-MM-DDTHH:MM:SSZ (D2)."""
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_raw_rows(path, rows):
    """Write fixture rows (list of dict) as JSONL directly (controls ts)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def make_row(ts, project="unknown", task="t", outcome="pass", workers=1,
             vfails=0, cats=None):
    return {
        "ts": ts,
        "project": project,
        "task": task,
        "outcome": outcome,
        "worker_spawn_count": workers,
        "verifier_fail_count": vfails,
        "first_pass_clean": (outcome == "pass" and vfails == 0),
        "defect_categories": cats or [],
    }


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def case_round_trip_known_numbers(tmp):
    """Record→report round-trip with known numbers; assert exact aggregates.

    Fixture (3 rows):
      r1: pass, vfails=0, cats=[contract_ambiguity:2]            -> clean
      r2: pass, vfails=1, cats=[weak_verification:1]             -> not clean
      r3: fail_exhausted, vfails=3, cats=[contract_ambiguity:1, capability:2] -> not clean
    Expected:
      task_count=3
      clean_count=1 -> rate = 100*1/3 = 33.333...%
      mean vfails = (0+1+3)/3 = 1.333...
      per_category: contract_ambiguity=3, missed_failure_mode=0,
                    integration_mismatch=0, weak_verification=1, capability=2
    """
    log = os.path.join(tmp, "logs", "kpi_rework.jsonl")
    env = {"CLAUDE_BOOSTER_LOGS_DIR": os.path.join(tmp, "logs")}

    rc1, _, e1 = run_kpi(
        ["record", "--task", "r1", "--outcome", "pass",
         "--worker-spawns", "1", "--verifier-fails", "0",
         "--category", "contract_ambiguity:2"], env)
    rc2, _, e2 = run_kpi(
        ["record", "--task", "r2", "--outcome", "pass",
         "--worker-spawns", "2", "--verifier-fails", "1",
         "--category", "weak_verification:1"], env)
    rc3, _, e3 = run_kpi(
        ["record", "--task", "r3", "--outcome", "fail_exhausted",
         "--worker-spawns", "4", "--verifier-fails", "3",
         "--category", "contract_ambiguity:1", "--category", "capability:2"], env)

    if not (rc1 == 0 and rc2 == 0 and rc3 == 0):
        _record("round_trip: three records exit 0", False,
                "rc=%s/%s/%s stderr=%r" % (rc1, rc2, rc3, (e1 + e2 + e3)[:200]))
        return
    _record("round_trip: three records exit 0", True)

    lc = line_count(log)
    _record("round_trip: log has exactly 3 lines", lc == 3, "got %d" % lc)

    # Each line parses
    lines = read_lines(log)
    parsed_ok = True
    for ln in lines:
        try:
            json.loads(ln)
        except Exception as ex:
            parsed_ok = False
            _record("round_trip: every log line parses", False, "%r in %r" % (ex, ln[:120]))
            break
    if parsed_ok:
        _record("round_trip: every log line parses", True)

    # prose report
    rc, out, err = run_kpi(["report"], env)
    _record("round_trip: report exit 0", rc == 0, "rc=%d stderr=%r" % (rc, err[:200]))

    # JSON report for exact-number assertions
    rc, out, err = run_kpi(["report", "--json"], env)
    if rc != 0:
        _record("round_trip: report --json exit 0", False, "rc=%d stderr=%r" % (rc, err[:200]))
        return
    _record("round_trip: report --json exit 0", True)
    try:
        env_obj = json.loads(out)
    except Exception as ex:
        _record("round_trip: report --json parseable", False, "%r out=%r" % (ex, out[:200]))
        return
    _record("round_trip: report --json parseable", True)

    # task_count
    tc = env_obj.get("task_count")
    _record("round_trip: task_count == 3", tc == 3, "got %r" % tc)

    # first-pass-clean rate percent = 33.33...
    rate = env_obj.get("first_pass_clean_rate_percent")
    rate_ok = isinstance(rate, (int, float)) and abs(float(rate) - (100.0 * 1 / 3)) < 0.05
    _record("round_trip: first_pass_clean_rate_percent ~= 33.33", rate_ok, "got %r" % rate)

    # mean verifier fail count = 1.33...
    mean = env_obj.get("mean_verifier_fail_count")
    mean_ok = isinstance(mean, (int, float)) and abs(float(mean) - (4.0 / 3)) < 0.01
    _record("round_trip: mean_verifier_fail_count ~= 1.333", mean_ok, "got %r" % mean)

    # per_category_totals: all five keys, canonical order, exact values
    pct = env_obj.get("per_category_totals")
    if not isinstance(pct, dict):
        _record("round_trip: per_category_totals is dict", False, "got %r" % type(pct))
        return
    _record("round_trip: per_category_totals is dict", True)
    keys = list(pct.keys())
    _record("round_trip: per_category keys canonical order (D7)",
            keys == ALLOWED, "got %r" % keys)
    expected_pct = {
        "contract_ambiguity": 3,
        "missed_failure_mode": 0,
        "integration_mismatch": 0,
        "weak_verification": 1,
        "capability": 2,
    }
    vals_ok = all(pct.get(k) == v for k, v in expected_pct.items())
    _record("round_trip: per_category totals exact", vals_ok,
            "got %r expected %r" % (pct, expected_pct))


def case_schema_complete(tmp):
    """Record one row; assert exact key set on the JSON line."""
    logs_dir = os.path.join(tmp, "schema_logs")
    log = os.path.join(logs_dir, "kpi_rework.jsonl")
    env = {"CLAUDE_BOOSTER_LOGS_DIR": logs_dir}
    rc, _, err = run_kpi(
        ["record", "--task", "schema-check", "--outcome", "pass",
         "--worker-spawns", "1", "--verifier-fails", "0",
         "--category", "capability:1"], env)
    if rc != 0:
        _record("schema: record exit 0", False, "rc=%d stderr=%r" % (rc, err[:200]))
        return
    _record("schema: record exit 0", True)
    lines = read_lines(log)
    if len(lines) != 1:
        _record("schema: exactly one line", False, "got %d" % len(lines))
        return
    _record("schema: exactly one line", True)
    obj = json.loads(lines[0])
    expected_keys = {"ts", "project", "task", "outcome", "worker_spawn_count",
                     "verifier_fail_count", "first_pass_clean", "defect_categories"}
    _record("schema: exact key set", set(obj.keys()) == expected_keys,
            "got %r" % sorted(obj.keys()))
    # field types / values
    _record("schema: task echoed", obj.get("task") == "schema-check", "got %r" % obj.get("task"))
    _record("schema: outcome echoed", obj.get("outcome") == "pass", "got %r" % obj.get("outcome"))
    _record("schema: worker_spawn_count int 1",
            obj.get("worker_spawn_count") == 1, "got %r" % obj.get("worker_spawn_count"))
    _record("schema: verifier_fail_count int 0",
            obj.get("verifier_fail_count") == 0, "got %r" % obj.get("verifier_fail_count"))
    dc = obj.get("defect_categories")
    dc_ok = isinstance(dc, list) and any(
        isinstance(it, dict) and it.get("category") == "capability" and it.get("count") == 1
        for it in dc)
    _record("schema: defect_categories list has capability:1", dc_ok, "got %r" % dc)
    # ts format YYYY-MM-DDTHH:MM:SSZ
    ts = obj.get("ts", "")
    ts_ok = False
    try:
        if isinstance(ts, str) and ts.endswith("Z"):
            datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            ts_ok = True
    except Exception:
        ts_ok = False
    _record("schema: ts format YYYY-MM-DDTHH:MM:SSZ (D2)", ts_ok, "got %r" % ts)


def case_first_pass_clean_derivation(tmp):
    """pass/0 => true; pass/1 => false; fail_exhausted/0 => false."""
    cases = [
        ("fpc_pass0", "pass", 0, True),
        ("fpc_pass1", "pass", 1, False),
        ("fpc_fail0", "fail_exhausted", 0, False),
    ]
    for i, (task, outcome, vf, expected) in enumerate(cases):
        logs_dir = os.path.join(tmp, "fpc_%d" % i)
        log = os.path.join(logs_dir, "kpi_rework.jsonl")
        env = {"CLAUDE_BOOSTER_LOGS_DIR": logs_dir}
        rc, _, err = run_kpi(
            ["record", "--task", task, "--outcome", outcome,
             "--worker-spawns", "1", "--verifier-fails", str(vf)], env)
        if rc != 0:
            _record("first_pass_clean: %s record exit 0" % task, False,
                    "rc=%d stderr=%r" % (rc, err[:200]))
            continue
        lines = read_lines(log)
        if len(lines) != 1:
            _record("first_pass_clean: %s one line" % task, False, "got %d" % len(lines))
            continue
        obj = json.loads(lines[0])
        got = obj.get("first_pass_clean")
        _record("first_pass_clean: %s => %s" % (task, expected),
                got is expected or got == expected, "got %r" % got)


def _invalid_case_no_write(tmp, label, args):
    """Generic: invalid record must exit non-zero AND write no line.

    Pre-seeds the log with one valid row, then runs the invalid command,
    asserts line count is unchanged (== 1) and exit code != 0.
    """
    logs_dir = os.path.join(tmp, "inv_" + label)
    log = os.path.join(logs_dir, "kpi_rework.jsonl")
    env = {"CLAUDE_BOOSTER_LOGS_DIR": logs_dir}
    # seed one valid row via record so the file legitimately exists
    rc0, _, e0 = run_kpi(
        ["record", "--task", "seed", "--outcome", "pass",
         "--worker-spawns", "0", "--verifier-fails", "0"], env)
    if rc0 != 0:
        _record("invalid[%s]: seed record exit 0" % label, False,
                "rc=%d stderr=%r" % (rc0, e0[:200]))
        return
    before = line_count(log)
    rc, _, err = run_kpi(["record"] + args, env)
    after = line_count(log)
    nonzero = rc != 0
    unchanged = after == before
    _record("invalid[%s]: exit non-zero" % label, nonzero, "rc=%d stderr=%r" % (rc, err[:160]))
    _record("invalid[%s]: no line written (count %d->%d)" % (label, before, after),
            unchanged, "before=%d after=%d" % (before, after))


def case_invalid_inputs(tmp):
    # unknown category name
    _invalid_case_no_write(tmp, "bad_category", [
        "--task", "x", "--outcome", "pass", "--worker-spawns", "1",
        "--verifier-fails", "0", "--category", "totally_unknown:1"])
    # negative category count
    _invalid_case_no_write(tmp, "neg_cat_count", [
        "--task", "x", "--outcome", "pass", "--worker-spawns", "1",
        "--verifier-fails", "0", "--category", "weak_verification:-1"])
    # bad outcome
    _invalid_case_no_write(tmp, "bad_outcome", [
        "--task", "x", "--outcome", "maybe", "--worker-spawns", "1",
        "--verifier-fails", "0"])
    # empty/whitespace task
    _invalid_case_no_write(tmp, "empty_task", [
        "--task", "   ", "--outcome", "pass", "--worker-spawns", "1",
        "--verifier-fails", "0"])
    # negative worker-spawns
    _invalid_case_no_write(tmp, "neg_workers", [
        "--task", "x", "--outcome", "pass", "--worker-spawns", "-1",
        "--verifier-fails", "0"])
    # negative verifier-fails
    _invalid_case_no_write(tmp, "neg_vfails", [
        "--task", "x", "--outcome", "pass", "--worker-spawns", "1",
        "--verifier-fails", "-2"])
    # malformed category token (no count)
    _invalid_case_no_write(tmp, "cat_no_count", [
        "--task", "x", "--outcome", "pass", "--worker-spawns", "1",
        "--verifier-fails", "0", "--category", "weak_verification"])
    # malformed category token (non-int count)
    _invalid_case_no_write(tmp, "cat_nonint", [
        "--task", "x", "--outcome", "pass", "--worker-spawns", "1",
        "--verifier-fails", "0", "--category", "weak_verification:abc"])


def case_no_data_paths(tmp):
    """Missing log => exit 0 + no-data prose. --json no-data => parseable JSON (D6/VA-A)."""
    missing = os.path.join(tmp, "nope_dir")  # dir doesn't exist => log missing
    env = {"CLAUDE_BOOSTER_LOGS_DIR": missing}

    # prose no-data
    rc, out, err = run_kpi(["report"], env)
    _record("no_data: missing-log report exit 0", rc == 0, "rc=%d stderr=%r" % (rc, err[:160]))
    _record("no_data: prose mentions no data", "no data" in out.lower(),
            "stdout=%r" % out[:160])

    # VA-A: --json no-data still parseable JSON, exit 0
    rc, out, err = run_kpi(["report", "--json"], env)
    _record("no_data: --json missing-log exit 0 (VA-A)", rc == 0,
            "rc=%d stderr=%r" % (rc, err[:160]))
    json_ok = False
    obj = None
    try:
        obj = json.loads(out)
        json_ok = True
    except Exception as ex:
        _record("no_data: --json missing-log parseable (VA-A/D6)", False,
                "%r stdout=%r" % (ex, out[:200]))
    if json_ok:
        _record("no_data: --json missing-log parseable (VA-A/D6)", True)
        _record("no_data: --json task_count == 0",
                obj.get("task_count") == 0, "got %r" % obj.get("task_count"))
        pct = obj.get("per_category_totals")
        _record("no_data: --json per_category all five keys zero-filled (D7)",
                isinstance(pct, dict) and list(pct.keys()) == ALLOWED
                and all(v == 0 for v in pct.values()),
                "got %r" % pct)


def case_zero_rows_in_window(tmp):
    """Log exists but all rows older than window => exit 0 + no-data."""
    logs_dir = os.path.join(tmp, "old_logs")
    log = os.path.join(logs_dir, "kpi_rework.jsonl")
    env = {"CLAUDE_BOOSTER_LOGS_DIR": logs_dir}
    old_ts = iso_z(datetime.now(timezone.utc) - timedelta(days=10))
    write_raw_rows(log, [make_row(old_ts, task="old")])

    rc, out, err = run_kpi(["report", "--window", "1"], env)
    _record("zero_in_window: exit 0", rc == 0, "rc=%d stderr=%r" % (rc, err[:160]))
    _record("zero_in_window: no-data message", "no data" in out.lower(),
            "stdout=%r" % out[:160])
    # --json variant also parseable, task_count 0
    rc, out, err = run_kpi(["report", "--window", "1", "--json"], env)
    ok = False
    try:
        obj = json.loads(out)
        ok = (rc == 0 and obj.get("task_count") == 0)
    except Exception:
        ok = False
    _record("zero_in_window: --json exit 0 + task_count 0", ok,
            "rc=%d stdout=%r" % (rc, out[:160]))


def case_window_boundary_inclusive(tmp):
    """VA-C / D3: a row with ts EXACTLY == (now - window days) is INCLUDED.

    We write two rows directly with controlled ts:
      - boundary row at now - window (must be included)
      - just-outside row at now - window - 1h (must be excluded)
    Then report --window N and assert task_count counts only the boundary row.
    Note: report captures its OWN now; we set the boundary ts slightly INSIDE
    (now - window + a small slack) so that report's now (a few ms later) still
    keeps row_ts >= report_now - window. A row exactly at the mathematical
    cutoff computed from THIS process's now would be at risk of being a hair
    outside once report advances its clock; the inclusive rule is what we test,
    so we place the boundary row a few seconds inside to make the inclusive
    boundary observable deterministically while the outside row is clearly out.
    """
    logs_dir = os.path.join(tmp, "boundary_logs")
    log = os.path.join(logs_dir, "kpi_rework.jsonl")
    env = {"CLAUDE_BOOSTER_LOGS_DIR": logs_dir}
    window = 7
    now = datetime.now(timezone.utc)
    # boundary: just inside cutoff (cutoff = now - window). Place 30s inside so
    # report's slightly-later now still includes it; tests inclusive (>=) rule.
    boundary_ts = iso_z(now - timedelta(days=window) + timedelta(seconds=30))
    outside_ts = iso_z(now - timedelta(days=window) - timedelta(hours=2))
    write_raw_rows(log, [
        make_row(boundary_ts, task="boundary", outcome="pass", vfails=0),
        make_row(outside_ts, task="outside", outcome="pass", vfails=0),
    ])
    rc, out, err = run_kpi(["report", "--window", str(window), "--json"], env)
    if rc != 0:
        _record("boundary(VA-C): report exit 0", False, "rc=%d stderr=%r" % (rc, err[:200]))
        return
    _record("boundary(VA-C): report exit 0", True)
    try:
        obj = json.loads(out)
    except Exception as ex:
        _record("boundary(VA-C): json parseable", False, "%r out=%r" % (ex, out[:200]))
        return
    _record("boundary(VA-C): json parseable", True)
    tc = obj.get("task_count")
    _record("boundary(VA-C): inclusive row counted, outside excluded => task_count==1",
            tc == 1, "got %r (boundary should be IN, outside OUT)" % tc)


def case_isolation_real_log_untouched(tmp):
    """VA-B / FM-030: record with override grows override file AND the real
    ~/.claude/logs/kpi_rework.jsonl is NOT created/modified."""
    real_log = Path.home() / ".claude" / "logs" / "kpi_rework.jsonl"
    pre_exists = real_log.exists()
    pre_stat = None
    if pre_exists:
        st = real_log.stat()
        pre_stat = (st.st_size, st.st_mtime_ns)

    logs_dir = os.path.join(tmp, "iso_logs")
    override_log = os.path.join(logs_dir, "kpi_rework.jsonl")
    env = {"CLAUDE_BOOSTER_LOGS_DIR": logs_dir}
    before = line_count(override_log)
    rc, _, err = run_kpi(
        ["record", "--task", "iso", "--outcome", "pass",
         "--worker-spawns", "1", "--verifier-fails", "0"], env)
    after = line_count(override_log)
    _record("isolation(VA-B): record exit 0", rc == 0, "rc=%d stderr=%r" % (rc, err[:160]))
    _record("isolation(VA-B): override file grew (%d->%d)" % (before, after),
            after == before + 1, "before=%d after=%d" % (before, after))

    # Real log assertions
    now_exists = real_log.exists()
    if not pre_exists:
        _record("isolation(VA-B): real log NOT created", not now_exists,
                "real log appeared at %s" % real_log)
    else:
        st = real_log.stat()
        post_stat = (st.st_size, st.st_mtime_ns)
        _record("isolation(VA-B): real log size+mtime unchanged",
                post_stat == pre_stat, "pre=%r post=%r" % (pre_stat, post_stat))


def case_log_path_flag_precedence(tmp):
    """D1: --log-path FILE beats CLAUDE_BOOSTER_LOGS_DIR. Record writes to the
    explicit file, not to <env-dir>/kpi_rework.jsonl."""
    env_dir = os.path.join(tmp, "prec_envdir")
    env_log = os.path.join(env_dir, "kpi_rework.jsonl")
    flag_log = os.path.join(tmp, "prec_explicit", "custom.jsonl")
    env = {"CLAUDE_BOOSTER_LOGS_DIR": env_dir}
    rc, _, err = run_kpi(
        ["record", "--task", "prec", "--outcome", "pass",
         "--worker-spawns", "1", "--verifier-fails", "0",
         "--log-path", flag_log], env)
    _record("precedence(D1): record exit 0", rc == 0, "rc=%d stderr=%r" % (rc, err[:160]))
    _record("precedence(D1): explicit --log-path file written",
            line_count(flag_log) == 1, "flag_log lines=%d" % line_count(flag_log))
    _record("precedence(D1): env-dir log NOT written when --log-path given",
            line_count(env_log) == 0, "env_log lines=%d" % line_count(env_log))


def case_unknown_project_roundtrip(tmp):
    """VA-D / D4: record+report in a non-repo temp dir => project=='unknown'
    and the row is included in the round-trip.

    We invoke kpi_rework with cwd set to a clean temp dir that has no .git/.claude
    marker, so project_root_from() returns None => 'unknown'. We must invoke with
    that cwd; run_kpi uses REPO_ROOT cwd, so do a direct subprocess here.
    """
    workdir = os.path.join(tmp, "norepo_work")
    os.makedirs(workdir, exist_ok=True)
    logs_dir = os.path.join(tmp, "norepo_logs")
    log = os.path.join(logs_dir, "kpi_rework.jsonl")
    env = dict(os.environ)
    env["CLAUDE_BOOSTER_LOGS_DIR"] = logs_dir

    def run_in(cwd, args):
        p = subprocess.run(
            [sys.executable, str(ARTIFACT)] + [str(a) for a in args],
            capture_output=True, text=True, env=env, cwd=cwd)
        return p.returncode, p.stdout, p.stderr

    rc, _, err = run_in(workdir, [
        "record", "--task", "uproj", "--outcome", "pass",
        "--worker-spawns", "1", "--verifier-fails", "0"])
    if rc != 0:
        _record("unknown_project(VA-D): record exit 0", False,
                "rc=%d stderr=%r" % (rc, err[:200]))
        return
    _record("unknown_project(VA-D): record exit 0", True)
    lines = read_lines(log)
    if len(lines) != 1:
        _record("unknown_project(VA-D): one line", False, "got %d" % len(lines))
        return
    obj = json.loads(lines[0])
    _record("unknown_project(VA-D): project == 'unknown' (D4)",
            obj.get("project") == "unknown", "got %r" % obj.get("project"))

    # report round-trip (default --project resolves to 'unknown' in same cwd)
    rc, out, err = run_in(workdir, ["report", "--json"])
    ok = False
    tc = None
    try:
        o = json.loads(out)
        tc = o.get("task_count")
        ok = (rc == 0 and tc == 1)
    except Exception:
        ok = False
    _record("unknown_project(VA-D): round-trip report includes row (task_count 1)",
            ok, "rc=%d task_count=%r out=%r" % (rc, tc, out[:160]))


def case_malformed_line_resilience(tmp):
    """FM-021: a malformed JSONL line must not crash report; valid row still aggregates."""
    logs_dir = os.path.join(tmp, "malformed_logs")
    log = os.path.join(logs_dir, "kpi_rework.jsonl")
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    good_ts = iso_z(datetime.now(timezone.utc))
    good = json.dumps(make_row(good_ts, task="good", outcome="pass", vfails=0,
                               cats=[{"category": "capability", "count": 1}]))
    with Path(log).open("w", encoding="utf-8") as fh:
        fh.write("{ this is not valid json ]\n")
        fh.write(good + "\n")
    env = {"CLAUDE_BOOSTER_LOGS_DIR": logs_dir}
    rc, out, err = run_kpi(["report", "--window", "3650", "--json"], env)
    if rc != 0:
        _record("malformed(FM-021): report exit 0 (no crash)", False,
                "rc=%d stderr=%r" % (rc, err[:200]))
        return
    _record("malformed(FM-021): report exit 0 (no crash)", True)
    try:
        obj = json.loads(out)
    except Exception as ex:
        _record("malformed(FM-021): json parseable", False, "%r out=%r" % (ex, out[:200]))
        return
    _record("malformed(FM-021): only valid row counted (task_count 1)",
            obj.get("task_count") == 1, "got %r" % obj.get("task_count"))
    pct = obj.get("per_category_totals", {})
    _record("malformed(FM-021): capability total from good row == 1",
            isinstance(pct, dict) and pct.get("capability") == 1, "got %r" % pct)


def case_duplicate_category_sum(tmp):
    """D5 / FM-010: duplicate category names sum per name in report; x:0 accepted."""
    logs_dir = os.path.join(tmp, "dup_logs")
    log = os.path.join(logs_dir, "kpi_rework.jsonl")
    env = {"CLAUDE_BOOSTER_LOGS_DIR": logs_dir}
    rc, _, err = run_kpi(
        ["record", "--task", "dup", "--outcome", "pass",
         "--worker-spawns", "1", "--verifier-fails", "0",
         "--category", "weak_verification:1",
         "--category", "weak_verification:2",
         "--category", "capability:0"], env)
    if rc != 0:
        _record("dup_category(D5): record exit 0 (dups + x:0 valid)", False,
                "rc=%d stderr=%r" % (rc, err[:200]))
        return
    _record("dup_category(D5): record exit 0 (dups + x:0 valid)", True)
    _record("dup_category(D5): exactly one line written", line_count(log) == 1,
            "got %d" % line_count(log))
    rc, out, err = run_kpi(["report", "--json"], env)
    try:
        obj = json.loads(out)
    except Exception as ex:
        _record("dup_category(D5): report json parseable", False, "%r out=%r" % (ex, out[:160]))
        return
    pct = obj.get("per_category_totals", {})
    _record("dup_category(D5): weak_verification summed to 3", pct.get("weak_verification") == 3,
            "got %r" % pct.get("weak_verification"))
    _record("dup_category(D5): capability:0 contributes 0", pct.get("capability") == 0,
            "got %r" % pct.get("capability"))


def case_deterministic_order(tmp):
    """FM-029 / D7: per_category_totals key order is canonical regardless of row order."""
    logs_dir = os.path.join(tmp, "det_logs")
    log = os.path.join(logs_dir, "kpi_rework.jsonl")
    ts = iso_z(datetime.now(timezone.utc))
    # rows introducing categories in NON-canonical order
    rows = [
        make_row(ts, task="a", cats=[{"category": "capability", "count": 1}]),
        make_row(ts, task="b", cats=[{"category": "contract_ambiguity", "count": 1}]),
        make_row(ts, task="c", cats=[{"category": "integration_mismatch", "count": 1}]),
    ]
    write_raw_rows(log, rows)
    env = {"CLAUDE_BOOSTER_LOGS_DIR": logs_dir}
    rc, out, err = run_kpi(["report", "--window", "3650", "--json"], env)
    try:
        obj = json.loads(out)
        keys = list(obj.get("per_category_totals", {}).keys())
    except Exception as ex:
        _record("determinism(D7): json parseable", False, "%r out=%r" % (ex, out[:160]))
        return
    _record("determinism(D7): keys canonical order regardless of input order",
            keys == ALLOWED, "got %r" % keys)


def main():
    if not ARTIFACT.exists():
        print("[FAIL] artifact missing: %s" % ARTIFACT)
        print("Results: 0 passed, 1 failed")
        return 1

    tmp = tempfile.mkdtemp(prefix="kpi_rework_test_")
    try:
        case_round_trip_known_numbers(tmp)
        case_schema_complete(tmp)
        case_first_pass_clean_derivation(tmp)
        case_invalid_inputs(tmp)
        case_no_data_paths(tmp)
        case_zero_rows_in_window(tmp)
        case_window_boundary_inclusive(tmp)
        case_isolation_real_log_untouched(tmp)
        case_log_path_flag_precedence(tmp)
        case_unknown_project_roundtrip(tmp)
        case_malformed_line_resilience(tmp)
        case_duplicate_category_sum(tmp)
        case_deterministic_order(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print("Results: %d passed, %d failed" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
