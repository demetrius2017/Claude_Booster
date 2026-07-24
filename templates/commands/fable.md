---
description: "Ask Fable 5 for a one-off read-only advisory opinion. Not consilium."
argument-hint: '<question>'
---

# /fable — One-Off Fable 5 Consult

Use this command when Dmitry explicitly asks to consult Fable 5, including
natural-language triggers such as:

- `посоветуйся с Fable 5`
- `спроси Fable`
- `Fable opinion`
- `/fable <question>` or `$fable <question>`

This is **not** `/consilium`. It is one expert opinion from Fable 5, used
because Dmitry explicitly asked for the strongest advisory model on this
specific question.

## Contract

- Read-only by default: Fable may inspect context and reason, but must not edit
  files, run deploys, push commits, or make irreversible changes.
- One call only unless Dmitry asks for a follow-up.
- No report file by default. Do not create `reports/consilium_*.md`.
- Do not change model routing defaults. This command does not modify
  `~/.claude/model_balancer.json`.
- The Lead owns the decision. Fable gives advice; the Lead synthesizes and
  decides what to do next.
- After the Fable call completes, run
  `python3 ~/.claude/scripts/fable_usage.py refresh-display` and append its
  output if it prints anything. This refreshes the current UTC month from
  Claude/Codex transcript stores before printing. These lines are
  API-equivalent / credit-rate estimates from the shared ledger/cache, not an
  actual billing ledger. If the command is quiet, omit the spend block.
- If Fable is unavailable, say it is unavailable and stop. Do not silently
  substitute Codex, PAL, Z.ai, Grok, Opus, or Sonnet.

## Procedure

1. Parse `$ARGUMENTS` as the question. If `$ARGUMENTS` is empty, use the active
   user request and current task context as the question.
2. Build a concise Verified Facts Brief before asking Fable:
   - User question or decision being made.
   - Current repo/worktree and branch if relevant.
   - Relevant files already inspected; if the issue is code-related, include
     `git diff --stat` and `git diff --name-only`.
   - Constraints: Fable is advisory only, not a routing/default change.
   - Known risks, failed attempts, or places where cheaper lanes appear to be
     looping.
3. Ask exactly one Fable 5 read-only worker/subagent using the strongest Fable
   channel available in the current runtime:
   - In Claude Code: spawn one read-only Agent with explicit Fable model if the
     runtime supports it. Disallow edit/write/deploy tools in the prompt.
   - In Codex: pipe the complete prompt to the deterministic wrapper:
     `printf '%s\n' "$prompt" | ~/.claude/scripts/fable_consult.sh`.
     Do not construct a raw `claude` command. In particular, never put a
     positional prompt after variadic `--tools <tools...>`: the option can
     consume the prompt and fail before contacting Fable.
     The wrapper may run past the first shell-tool yield. If `exec_command`
     returns a `session_id`, poll that shell session with `write_stdin` until
     the real exit code and stdout/stderr arrive. Do not substitute
     `functions.wait` on the surrounding JavaScript `exec` cell: that waits for
     the cell, not the child PTY, and can lose the completed Fable response.
     Missing `exit_code` or an empty pending output is not a completed consult.
   - Interpret wrapper exits `64` (empty input), `69` (missing local dependency),
     `70` (local wrapper/output-contract failure), and `74` (stdin read failure)
     as local failures. Report wrapper stderr accurately. Other nonzero exits
     are Claude CLI/model failures. Do not call Fable "unavailable" merely
     because a local failure occurred.
4. Return the result in this shape:

```text
Fable 5 consult
Question: <one sentence>
Verdict: <Fable's position>
Key reasoning: <short synthesis>
Risks/unknowns: <what Fable could not verify>

<output of: python3 ~/.claude/scripts/fable_usage.py refresh-display, if non-empty>

Lead synthesis
Decision: <Lead's recommendation>
What I will do: <next action, or "no action" if this was advisory only>
```

## Fable Prompt Template

Use this structure for the read-only Fable call:

```text
You are Fable 5 acting as a read-only expert advisor for Dmitry's engineering
lead.

Task: answer the question below using only the provided facts and any files you
are explicitly allowed to inspect. Do not edit files, do not deploy, do not run
destructive commands, and do not make routing/default model changes.

Question:
<question>

Verified Facts Brief:
<facts>

Return:
1. Verdict
2. Reasoning
3. Risks or unknowns
4. Recommended next action for the Lead
```
