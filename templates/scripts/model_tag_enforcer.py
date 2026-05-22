#!/usr/bin/env python3
"""
PreToolUse hook: enforce [model] tag in Agent tool call descriptions AND
model_balancer routing decisions.

Purpose:
    Two enforcement layers in one hook:

    1. [model] TAG ENFORCEMENT
    Claude Code's UI renders the Agent tool's `description` field but does NOT
    display the `model` parameter separately. Without a visible [model] tag in
    the description, there is no way to know at a glance which model tier a
    spawned sub-agent runs on.  This hook checks every Agent tool call made by
    the Lead (top-level Claude session) and prints an advisory warning to stderr
    when the `description` field lacks a recognised [model] tag like [sonnet],
    [opus], or [haiku].  The call is NOT blocked — the warning tells Claude
    exactly what tag to add so the next spawn is correctly labelled.

    NOTE (v1.9.4+): Missing [model] tags were originally blocked (exit 2), but
    this caused a deadlock: the hook blocked the Agent call, and Claude could not
    self-correct the description without making another Agent call.  Advisory
    mode (exit 0 + stderr) resolves the deadlock.  This is intentional design,
    not a gap to close.

    2. MODEL_BALANCER ROUTING ENFORCEMENT
    Reads ~/.claude/model_balancer.json and classifies each Agent call into a
    routing category (trivial, recon, medium, coding, hard, high_blast_radius,
    etc.).  When the balancer routes a category to codex-cli, Agent is the
    wrong tool — Bash + codex_worker.sh should be used instead.  The hook
    emits an advisory warning (exit 0 + stderr) explaining which script to use,
    but does NOT block the call.

    When the balancer routes to anthropic and the Agent's `model` param
    disagrees with the recommended model, the hook DOES block (exit 2) with a
    message telling Claude which model to use.  This is the only hard-block path
    in routing enforcement: it prevents a weaker model from silently replacing a
    stronger one recommended by the balancer.

    Exemptions from routing enforcement:
    - Explore agents (subagent_type == "Explore") always need tool access and
      cannot run in Codex; they are exempt from the codex-cli advisory.
    - high_blast_radius categories are always routed to Agent (dep_guard and
      other PreToolUse hooks fire on Agent, not on Bash+codex subprocess).
    - All routing errors (missing JSON, parse error, unknown category) fail open.

    ENFORCEMENT MODEL SUMMARY (v1.9.4+):
    ┌──────────────────────────────────────────┬────────────────────────────┐
    │ Condition                                │ Action                     │
    ├──────────────────────────────────────────┼────────────────────────────┤
    │ Anthropic tier mismatch (weaker model    │ HARD BLOCK — exit 2        │
    │ than balancer recommends)                │                            │
    ├──────────────────────────────────────────┼────────────────────────────┤
    │ Codex routing — wrong tool (Agent used   │ Advisory — exit 0 + stderr │
    │ when balancer says codex-cli)            │                            │
    ├──────────────────────────────────────────┼────────────────────────────┤
    │ Missing [model] tag in description       │ Advisory — exit 0 + stderr │
    └──────────────────────────────────────────┴────────────────────────────┘

    WHY advisory for Codex routing and missing tags: CC bug #16598 prevents
    updatedInput (the hook cannot rewrite the description to add the tag).
    Blocking (exit 2) without the ability to self-correct creates a deadlock.
    Advisory mode surfaces the issue in stderr without stalling the workflow.
    Do NOT change Codex routing or missing-tag handling back to exit 2 — that
    re-introduces the deadlock that v1.9.4 specifically resolved.

Contract:
    stdin  — PreToolUse JSON from Claude Code harness:
               {tool_name, tool_input.{description, model, prompt, …},
                cwd, session_id, agent_id, agent_type, …}
    stdout — silent (no JSON output; CC bug #16598 prevents updatedInput)
    stderr — advisory warning on exit 0 (Codex routing / missing tag) OR
             human-readable block reason on exit 2 (Anthropic tier mismatch)
    exit   — 0 allow (includes advisory cases), 2 block (tier mismatch only)

    Sub-agent auto-skip:
        When `agent_id` is a non-empty string in the stdin JSON, this hook is
        firing inside a sub-agent context (the sub-agent itself is spawning a
        tool). The check is meaningless here — delegation has already happened.
        We exit 0 immediately.

    Fail-open policy:
        Any parsing error, unexpected JSON structure, or unexpected exception
        exits 0 so the hook NEVER breaks normal Claude Code operation.

CLI / Examples:
    # Allowed — tag present:
    echo '{"tool_name":"Agent","tool_input":{"description":"[sonnet] Explore files"},
           "session_id":"s1"}' | python3 model_tag_enforcer.py; echo "exit: $?"

    # Advisory (exit 0 + stderr warning) — tag absent:
    echo '{"tool_name":"Agent","tool_input":{"description":"Explore files"},
           "session_id":"s1"}' | python3 model_tag_enforcer.py; echo "exit: $?"

    # Auto-skip — sub-agent context:
    echo '{"tool_name":"Agent","tool_input":{"description":"Explore files"},
           "agent_id":"sub-42","session_id":"s1"}' | python3 model_tag_enforcer.py

    # Non-Agent tool — passthrough:
    echo '{"tool_name":"Bash","tool_input":{"command":"ls"},"session_id":"s1"}' \
         | python3 model_tag_enforcer.py


Limitations:
    - Tag matching is case-insensitive for the keyword part; bracket syntax
      [sonnet], [SONNET] etc. all recognised.  Custom sub-tier suffixes like
      [sonnet-3-7] are also accepted.
    - Mismatch between description tag and `model` param is logged to stderr as
      a WARNING but does NOT block — it's advisory only.  Enforcing strict
      parity would break callers that legitimately use [inherit] as a tag while
      specifying a concrete model param for the harness.
    - This hook only fires when registered in settings.json under PreToolUse
      with matcher "Agent".  If the matcher is omitted, the hook fires for ALL
      tools (harmless but wastes cycles on non-Agent calls).
    - Routing enforcement requires ~/.claude/model_balancer.json to be present
      and up-to-date (refreshed daily by model_balancer.py decide).  Missing or
      malformed JSON silently disables routing enforcement (fail-open).

ENV / Files:
    - Reads  : stdin (PreToolUse JSON),
               ~/.claude/model_balancer.json (routing table, optional)
    - Writes : nothing (no side effects, no log files)
    - ENV    : CLAUDE_BOOSTER_SKIP_MODEL_TAG_ENFORCER=1
                   -- bypass this hook entirely (useful for testing / one-off
                      sessions where tagging is intentionally relaxed);
                      bypasses BOTH tag enforcement AND routing enforcement
               CLAUDE_MODEL_TAG_AUTO_INJECT=1
                   -- currently a no-op (CC bug #16598 prevents updatedInput)
               CLAUDE_EFFORT
                   -- set by Claude Code runtime; when 'xhigh' or 'max',
                      the hook skips entirely (user chose maximum quality,
                      cost-saving advisories are counterproductive)
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, Optional

try:
    from _gate_common import DECISION_ALLOW, append_jsonl, is_subagent_context, iso_now
except ImportError:
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from _gate_common import DECISION_ALLOW, append_jsonl, is_subagent_context, iso_now
    except ImportError:
        DECISION_ALLOW = "allow"  # type: ignore[misc]

        def is_subagent_context(data):  # type: ignore[misc]
            aid = (data or {}).get("agent_id")
            return bool(aid and isinstance(aid, str))

        def iso_now() -> str:  # type: ignore[misc]
            import datetime as _dt
            return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        def append_jsonl(log_name: str, record: dict) -> None:  # type: ignore[misc]
            print("model_tag_enforcer: _gate_common unavailable, decision logging disabled", file=sys.stderr)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tags recognised as valid model indicators (case-insensitive, bracket syntax)
# Pattern: [<tier>] or [<tier>-<suffix>] e.g. [sonnet], [opus-3], [haiku-4-5]
_MODEL_TAG_RE = re.compile(
    r"\["
    r"(?:sonnet|opus|haiku|inherit|claude)"
    r"(?:[-\w.]*)"   # optional sub-tier / version suffix
    r"\]",
    re.IGNORECASE,
)

# Human-readable tier examples for the block message
_TIER_EXAMPLES = "[sonnet], [opus], [haiku]"

# Environment variables
_SKIP_ENV = "CLAUDE_BOOSTER_SKIP_MODEL_TAG_ENFORCER"

# Decision log name
ENFORCER_LOG_NAME = "model_tag_enforcer_decisions.jsonl"

# Decision constants (parallel to _gate_common.DECISION_* for delegate_gate)
DECISION_ADVISORY_CODEX = "advisory_codex"
DECISION_ALLOW_STRONGER = "allow_stronger_override"
DECISION_BLOCK_TIER = "block_tier_mismatch"

# ---------------------------------------------------------------------------
# model_balancer routing
# ---------------------------------------------------------------------------

_BALANCER_PATH = os.path.expanduser("~/.claude/model_balancer.json")

_VALID_CATEGORIES = frozenset({
    "coding", "high_blast_radius", "trivial", "recon", "medium",
    "hard", "consilium_bio", "audit_external",
})

_CATEGORY_TAG_RE = re.compile(
    r"\[(" + "|".join(sorted(_VALID_CATEGORIES)) + r")\]",
    re.IGNORECASE,
)

_CODING_KEYWORDS = frozenset({
    "worker", "verifier", "implement", "fix", "refactor", "write code",
    "apply", "edit", "modify", "add", "change", "update",
})

_HIGH_BLAST_KEYWORDS = frozenset({
    "auth", "security", "secret", "secrets", "migration", "db_migration",
    "financial", "financial_dml", "broker", "infra", "infra_config",
    "dml", "credential", "deploy", "permission",
    "high_blast_radius",
})


def _load_routing() -> Optional[Dict]:
    """Load routing table from model_balancer.json. Returns dict or None on any error."""
    try:
        with open(_BALANCER_PATH, "r") as f:
            data = json.load(f)
        return data.get("routing") or {}
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _infer_category(description: str, subagent_type: str) -> str:
    """
    Classify an Agent call into a model_balancer category.

    Priority: explicit [category] tag → high_blast_radius keywords → coding keywords
    → subagent_type/other keywords → default "medium".
    """
    cat_tag = _CATEGORY_TAG_RE.search(description)
    if cat_tag:
        return cat_tag.group(1).lower()

    desc = description.lower()
    # High blast radius — safety-critical, check FIRST
    if any(kw in desc for kw in _HIGH_BLAST_KEYWORDS):
        return "high_blast_radius"
    if any(kw in desc for kw in _CODING_KEYWORDS):
        return "coding"
    if subagent_type == "Explore" or "explore" in desc or "recon" in desc:
        return "recon"
    if "consilium" in desc:
        return "consilium_bio"
    if "audit" in desc and "audit-trace" not in desc:
        return "audit_external"
    if subagent_type == "Plan" or "plan" in desc or "architecture" in desc:
        return "hard"
    if (
        "trivial" in desc or "find" in desc or "grep" in desc
        or "lookup" in desc
    ):
        return "trivial"
    if (
        "simplify" in desc or "review" in desc or "judge" in desc
        or "research" in desc
    ):
        return "medium"
    return "medium"


def _build_routing_block_message(
    category: str, model: str, worker_script: str, description: str
) -> str:
    """Build a helpful stderr block message for codex-cli routing violations."""
    preview = description[:80] + ("..." if len(description) > 80 else "")
    return "\n".join([
        f"model_tag_enforcer: model_balancer routes '{category}' to codex-cli:{model}.",
        "",
        f"  Description: {preview!r}",
        f"  Category:    {category}",
        f"  Required:    codex-cli:{model} (not Agent)",
        "",
        "Use Bash with codex worker instead of Agent:",
        f"  printf '%s\\n' '<task>' | ~/.claude/scripts/{worker_script} {model}",
        "",
        "Agent tool is reserved for:",
        "  - high_blast_radius tasks (auth, security, migrations, financial, broker)",
        "  - Explore agents (subagent_type: Explore) that need tool access",
        "",
        "To bypass: CLAUDE_BOOSTER_SKIP_MODEL_TAG_ENFORCER=1",
    ])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_model_tag(description: str):
    """Return the regex match object if description contains a recognised [model] tag, else None."""
    return _MODEL_TAG_RE.search(description)


def _extract_tier(model_str: str) -> Optional[str]:
    """Extract tier keyword (opus/sonnet/haiku) from any model string format."""
    lower = model_str.lower()
    for tier in ("opus", "sonnet", "haiku"):
        if tier in lower:
            return tier
    return None


def _infer_tag_from_model_param(model_param: Optional[str]) -> str:
    """
    Derive a human-readable [tag] from the model param string.

    Claude Code accepts short aliases: "sonnet", "opus", "haiku" as well as
    full model IDs like "claude-sonnet-4-6".  We extract the first recognised
    tier keyword.  Falls back to "[inherit]" when model param is absent or
    unrecognised (the agent inherits the Lead's model).
    """
    if not model_param:
        return "[inherit]"

    lower = model_param.lower()
    for tier in ("opus", "sonnet", "haiku"):
        if tier in lower:
            return f"[{tier}]"

    # Unknown explicit model string -- use it verbatim with brackets
    return f"[{model_param}]"


def _check_mismatch(tag_match, model_param: Optional[str]) -> Optional[str]:
    """
    Return a warning string if description tag and model param appear to
    disagree, or None if they are consistent / cannot be determined.

    Parameters
    ----------
    tag_match : re.Match
        Match object returned by _find_model_tag (caller has already confirmed it is non-None).
    model_param : str or None
        The `model` parameter from tool_input.
    """
    if not model_param:
        return None  # inherit is always compatible

    tag_text = tag_match.group(0).lower()
    lower_model = model_param.lower()

    for tier in ("opus", "sonnet", "haiku"):
        tag_has_tier = tier in tag_text
        param_has_tier = tier in lower_model
        if tag_has_tier and not param_has_tier:
            return (
                f"WARNING: description says {tag_match.group(0)!r} but "
                f"model param is {model_param!r} -- consider aligning them."
            )
        if param_has_tier and not tag_has_tier:
            return (
                f"WARNING: model param is {model_param!r} but description tag "
                f"is {tag_match.group(0)!r} -- consider aligning them."
            )

    return None


def _build_block_message(
    description: str, model_param: Optional[str]
) -> str:
    """Build a helpful stderr block message."""
    suggested_tag = _infer_tag_from_model_param(model_param)
    preview = description[:80] + ("..." if len(description) > 80 else "")
    lines = [
        "model_tag_enforcer: Agent description is missing a [model] tag.",
        "",
        f"  Current description: {preview!r}",
        f"  Suggested fix:       {suggested_tag} {description}",
        "",
        f"Recognised tags: {_TIER_EXAMPLES} (and [inherit] for model-inherit).",
        "Add the tag at the START of the description so it is visible in the UI.",
    ]
    if model_param:
        lines.append(f"  (model param is {model_param!r} -- use {suggested_tag})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Entry point.

    Returns:
        0 -- allow (tool proceeds)
        2 -- block (stderr is fed back to Claude as error message)
    """
    # --- bypass env (check before reading stdin for speed) ---
    if os.environ.get(_SKIP_ENV) == "1":
        return 0

    # --- high effort = suppress cost advisories but keep quality hard-blocks ---
    _high_effort = os.environ.get("CLAUDE_EFFORT", "").lower() in ("xhigh", "max")

    # --- fail-open guard: parse stdin ---
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError, OSError):
        return 0  # malformed / empty -- fail open

    # --- only care about Agent tool calls ---
    tool_name = payload.get("tool_name", "")
    if tool_name != "Agent":
        return 0

    # --- sub-agent auto-skip: delegation already happened ---
    if is_subagent_context(payload):
        return 0

    # --- extract relevant fields from tool_input ---
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0  # unexpected type -- fail open

    description: str = tool_input.get("description") or ""
    model_param: Optional[str] = tool_input.get("model") or None
    subagent_type: str = tool_input.get("subagent_type") or ""

    # --- PHASE 1: routing enforcement (model_balancer) ---
    # Runs BEFORE tag check so auto-inject cannot bypass Codex routing.
    routing = _load_routing()
    category = _infer_category(description, subagent_type) if routing is not None else None
    if routing is not None:
        route = routing.get(category)

        if route:
            provider = route.get("provider", "")
            recommended_model = route.get("model", "")

            # Codex routing: advisory, not blocking. Using Agent instead
            # of codex costs more budget but doesn't break correctness.
            # Safety gates (dep_guard, verify_gate) are hard blocks; budget
            # optimization is a suggestion the Lead can override.
            if provider == "codex-cli" and subagent_type != "Explore" and not _high_effort:
                worker_script = (
                    "codex_sandbox_worker.sh" if category == "coding"
                    else "codex_worker.sh"
                )
                msg = _build_routing_block_message(
                    category, recommended_model, worker_script, description
                )
                print(f"model_tag_enforcer [advisory]: {msg}", file=sys.stderr)
                append_jsonl(ENFORCER_LOG_NAME, {
                    "ts": iso_now(),
                    "gate": "model_tag_enforcer",
                    "decision": DECISION_ADVISORY_CODEX,
                    "category": category,
                    "provider": provider,
                    "recommended_model": recommended_model,
                    "description_excerpt": description[:120],
                    "session_id": payload.get("session_id", ""),
                })

            elif provider == "anthropic" and recommended_model:
                param_tier = _extract_tier(model_param) if model_param else None
                rec_tier = _extract_tier(recommended_model)
                if param_tier and rec_tier and param_tier != rec_tier:
                    _TIER_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}
                    param_rank = _TIER_RANK.get(param_tier, -1)
                    rec_rank = _TIER_RANK.get(rec_tier, -1)
                    if param_rank > rec_rank:
                        append_jsonl(ENFORCER_LOG_NAME, {
                            "ts": iso_now(),
                            "gate": "model_tag_enforcer",
                            "decision": DECISION_ALLOW_STRONGER,
                            "category": category,
                            "recommended_model": recommended_model,
                            "actual_model": model_param or "",
                            "session_id": payload.get("session_id", ""),
                        })
                    else:
                        print(
                            f"model_tag_enforcer: model_balancer routes "
                            f"'{category}' to {recommended_model}, "
                            f"but model param is '{model_param}'.\n"
                            f"  Fix: change model=\"{rec_tier}\" "
                            f"in the Agent call.",
                            file=sys.stderr,
                        )
                        append_jsonl(ENFORCER_LOG_NAME, {
                            "ts": iso_now(),
                            "gate": "model_tag_enforcer",
                            "decision": DECISION_BLOCK_TIER,
                            "category": category,
                            "provider": provider,
                            "recommended_model": recommended_model,
                            "actual_model": model_param or "",
                            "description_excerpt": description[:120],
                            "session_id": payload.get("session_id", ""),
                        })
                        return 2

    # --- PHASE 2: tag enforcement (advisory — CC bug #16598 blocks auto-inject) ---
    if not _high_effort:
        tag_match = _find_model_tag(description)
        if tag_match:
            warning = _check_mismatch(tag_match, model_param)
            if warning:
                print(warning, file=sys.stderr)
        else:
            msg = _build_block_message(description, model_param)
            print(f"model_tag_enforcer [advisory]: {msg}", file=sys.stderr)

    append_jsonl(ENFORCER_LOG_NAME, {
        "ts": iso_now(),
        "gate": "model_tag_enforcer",
        "decision": DECISION_ALLOW,
        "category": category,
        "session_id": payload.get("session_id", ""),
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 -- last-resort fail-open
        sys.exit(0)
