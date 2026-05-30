#!/usr/bin/env python3
"""Tiny viewer for delegate_gate / ask_gate decision logs.

Purpose:
    Reads the JSONL decision logs written by ``delegate_gate.py`` and
    ``ask_gate.py`` (plus the shared ``gate_bypass_attempts.jsonl``) and
    prints a plain-text table: invocation counts, allow/block rate,
    auto-skip share (sub-agent context), bypass honoured/refused counts,
    top callers (by cwd), top matched forbidden patterns (ask gate).

Contract:
    --gate {delegate,ask,all}   (default: all)
    --since <Nd|Nh|Nm>          (default: 7d) — relative time window
    --logdir PATH               (default: $CLAUDE_HOME/logs or
                                 ~/.claude/logs)

    Stdout only. No deps beyond stdlib.

CLI / Examples:
    python3 gate_stats.py
    python3 gate_stats.py --gate delegate --since 24h
    python3 gate_stats.py --gate ask --since 1h --logdir /tmp/fakelogs

Limitations:
    - One-shot. No caching. For dashboards with churn, build on top.
    - Time parsing accepts the three shortcuts (Nd/Nh/Nm). ISO durations
      are NOT supported.
    - Bypass attempts are read from ``gate_bypass_attempts.jsonl`` and
      filtered by the ``gate`` field; rows missing that field count as
      "delegate" (historical default for the first field rollout).

ENV/Files:
    - Reads  : <logdir>/delegate_gate_decisions.jsonl
               <logdir>/ask_gate_decisions.jsonl
               <logdir>/gate_bypass_attempts.jsonl
    - Writes : nothing
    - ENV    : CLAUDE_HOME (optional override for the default logdir)
"""
from __future__ import annotations

import argparse
import collections
import datetime as _dt
import json
import os
import pathlib
import sys


DELEGATE_LOG_NAME = "delegate_gate_decisions.jsonl"
ASK_LOG_NAME = "ask_gate_decisions.jsonl"
BYPASS_LOG_NAME = "gate_bypass_attempts.jsonl"


def _default_logdir() -> pathlib.Path:
    base = os.environ.get("CLAUDE_HOME")
    if base:
        return pathlib.Path(base) / "logs"
    return pathlib.Path.home() / ".claude" / "logs"


def _parse_since(spec: str) -> _dt.timedelta:
    """Parse Nd / Nh / Nm into a timedelta. Raises on bad input."""
    spec = (spec or "").strip().lower()
    if not spec:
        raise ValueError("empty --since")
    unit = spec[-1]
    try:
        n = int(spec[:-1])
    except ValueError as exc:
        raise ValueError(f"bad --since value: {spec!r}") from exc
    if unit == "d":
        return _dt.timedelta(days=n)
    if unit == "h":
        return _dt.timedelta(hours=n)
    if unit == "m":
        return _dt.timedelta(minutes=n)
    raise ValueError(f"unknown --since unit {unit!r} (use Nd/Nh/Nm)")


def _parse_ts(raw: str) -> _dt.datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        # Tolerate trailing 'Z'
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _load_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _filter_by_window(rows: list[dict], cutoff: _dt.datetime) -> list[dict]:
    """Keep rows whose ``ts`` parses and is >= cutoff.
    Rows without parseable ts are kept (assume current).
    """
    out = []
    for r in rows:
        ts = _parse_ts(r.get("ts", ""))
        if ts is None:
            out.append(r)
            continue
        # Normalise naive → UTC for comparison with cutoff (which is UTC-naive).
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        if ts >= cutoff:
            out.append(r)
    return out


def _pct(numer: int, denom: int) -> str:
    if denom <= 0:
        return "0%"
    return f"{(numer * 100) // denom}%"


def _count_subagent_bypass_attempts(rows: list[dict], gate: str) -> int:
    """Count decision-log rows for ``gate`` where a sub-agent tried to bypass.

    A sub-agent tried to bypass when the gate auto-skipped AND the
    decision record carries ``attempted_bypass=True`` — emitted by the
    gate when .delegate_mode/.ask_gate=off was present in a sub-agent
    context. Cross-file joins (bypass_refused in gate_bypass_attempts.jsonl
    + auto_skip in <gate>_decisions.jsonl) are now a one-field filter.
    """
    n = 0
    for r in rows:
        if r.get("decision") != "auto_skip":
            continue
        if r.get("attempted_bypass") is not True:
            continue
        if gate == "delegate" and (r.get("gate") or "delegate") != "delegate":
            continue
        if gate == "ask" and r.get("gate") != "ask":
            continue
        n += 1
    return n


def _format_delegate_stats(rows: list[dict], bypass_rows: list[dict], since_label: str) -> str:
    total = len(rows)
    by_decision: collections.Counter = collections.Counter()
    cwd_counter: collections.Counter = collections.Counter()
    for r in rows:
        by_decision[r.get("decision", "unknown")] += 1
        cwd = r.get("cwd") or ""
        if cwd:
            cwd_counter[cwd] += 1

    allow = by_decision.get("allow", 0)
    block = by_decision.get("block", 0)
    advisory = by_decision.get("advisory", 0)
    auto_skip = by_decision.get("auto_skip", 0)

    by_bypass_decision: collections.Counter = collections.Counter()
    for r in bypass_rows:
        if (r.get("gate") or "delegate") != "delegate":
            continue
        by_bypass_decision[r.get("decision", "unknown")] += 1
    # The decision-log 'bypass_honoured' is a duplicate of bypass_rows for
    # telemetry redundancy. Prefer bypass_rows count for the bypass row.
    bypass_honoured = by_bypass_decision.get("bypass_honoured", 0)
    bypass_refused = by_bypass_decision.get("bypass_refused", 0)
    bypass_total = bypass_honoured + bypass_refused
    subagent_bypass = _count_subagent_bypass_attempts(rows, "delegate")

    # Over-budget signal is 'advisory' since 2026-05-30 (was 'block' before the
    # advisory conversion). Count both so the rate stays correct across the window;
    # post-conversion 'block' is only the malformed-payload fail-closed path.
    budget_hits = block + advisory
    budget_hit_denom = budget_hits + allow
    budget_hit_rate = _pct(budget_hits, budget_hit_denom)

    lines = [
        f"=== delegate_gate — last {since_label} ===",
        f"total invocations: {total}",
        f"  allow:          {allow} ({_pct(allow, total)})",
        f"  block:          {block} ({_pct(block, total)})",
        f"  advisory:       {advisory} ({_pct(advisory, total)})  [over-budget nudge, non-blocking]",
        f"  auto_skip:      {auto_skip} ({_pct(auto_skip, total)})  [sub-agent context]",
        f"  bypass:         {bypass_total} (honoured={bypass_honoured} refused={bypass_refused})",
        f"sub-agent bypass attempts: {subagent_bypass}",
        f"budget-hit rate:   {budget_hit_rate} ((block+advisory) / (block+advisory+allow))",
    ]
    top = cwd_counter.most_common(5)
    if top:
        cwd_text = ", ".join(f"{pathlib.Path(c).name or c} ({n})" for c, n in top)
        lines.append(f"top callers (cwd): {cwd_text}")
    else:
        lines.append("top callers (cwd): (none)")
    return "\n".join(lines)


def _format_ask_stats(rows: list[dict], bypass_rows: list[dict], since_label: str) -> str:
    total = len(rows)
    by_decision: collections.Counter = collections.Counter()
    cwd_counter: collections.Counter = collections.Counter()
    pattern_counter: collections.Counter = collections.Counter()
    for r in rows:
        by_decision[r.get("decision", "unknown")] += 1
        cwd = r.get("cwd") or ""
        if cwd:
            cwd_counter[cwd] += 1
        pat = r.get("matched_pattern") or ""
        if pat:
            pattern_counter[pat] += 1

    allow = by_decision.get("allow", 0)
    block = by_decision.get("block", 0)
    auto_skip = by_decision.get("auto_skip", 0)

    by_bypass_decision: collections.Counter = collections.Counter()
    for r in bypass_rows:
        if r.get("gate") != "ask":
            continue
        by_bypass_decision[r.get("decision", "unknown")] += 1
    bypass_honoured = by_bypass_decision.get("bypass_honoured", 0)
    bypass_refused = by_bypass_decision.get("bypass_refused", 0)
    bypass_total = bypass_honoured + bypass_refused
    subagent_bypass = _count_subagent_bypass_attempts(rows, "ask")

    block_rate_denom = block + allow
    block_rate = _pct(block, block_rate_denom)

    lines = [
        f"=== ask_gate — last {since_label} ===",
        f"total invocations: {total}",
        f"  allow:          {allow} ({_pct(allow, total)})",
        f"  block:          {block} ({_pct(block, total)})",
        f"  auto_skip:      {auto_skip} ({_pct(auto_skip, total)})  [sub-agent context]",
        f"  bypass:         {bypass_total} (honoured={bypass_honoured} refused={bypass_refused})",
        f"sub-agent bypass attempts: {subagent_bypass}",
        f"block rate:        {block_rate} (block / (block + allow))",
    ]
    top_cwds = cwd_counter.most_common(5)
    if top_cwds:
        cwd_text = ", ".join(f"{pathlib.Path(c).name or c} ({n})" for c, n in top_cwds)
        lines.append(f"top callers (cwd): {cwd_text}")
    top_pats = pattern_counter.most_common(5)
    if top_pats:
        pat_text = ", ".join(f"{p!r} ({n})" for p, n in top_pats)
        lines.append(f"top matched patterns: {pat_text}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate", choices=("delegate", "ask", "all"), default="all")
    ap.add_argument("--since", default="7d", help="Window: Nd / Nh / Nm (default 7d)")
    ap.add_argument("--logdir", default=None, help="Log dir (default ~/.claude/logs)")
    args = ap.parse_args()

    try:
        delta = _parse_since(args.since)
    except ValueError as exc:
        print(f"gate_stats: {exc}", file=sys.stderr)
        return 2

    logdir = pathlib.Path(args.logdir).expanduser() if args.logdir else _default_logdir()
    cutoff = _dt.datetime.utcnow() - delta

    delegate_rows = _filter_by_window(_load_jsonl(logdir / DELEGATE_LOG_NAME), cutoff)
    ask_rows = _filter_by_window(_load_jsonl(logdir / ASK_LOG_NAME), cutoff)
    bypass_rows = _filter_by_window(_load_jsonl(logdir / BYPASS_LOG_NAME), cutoff)

    outputs: list[str] = []
    if args.gate in ("delegate", "all"):
        outputs.append(_format_delegate_stats(delegate_rows, bypass_rows, args.since))
    if args.gate in ("ask", "all"):
        outputs.append(_format_ask_stats(ask_rows, bypass_rows, args.since))

    print("\n\n".join(outputs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
