---
name: "fable"
description: "Ask Fable 5 for a one-off read-only advisory opinion. Not consilium; no report; no routing change."
---

# Booster Fable Consult

Read the sibling skill `../booster-command/SKILL.md`, then run command `fable`
through that runner.

Treat the rest of the user message as the Fable question. This is a single
read-only advisory consult, not the multi-agent consilium protocol.

For the actual model call, pass the completed prompt through stdin to
`~/.claude/scripts/fable_consult.sh`. Never assemble a raw Claude CLI call;
`--tools` is variadic and can swallow a following positional prompt. A local
input/argument failure is not evidence that the Fable channel is unavailable.
The wrapper prepends the canonical `$fable-identity` block exactly once; do not
duplicate or paraphrase it in the completed prompt.

The consult may outlive the first shell-tool yield. If `exec_command` returns a
`session_id`, poll that shell session with `write_stdin` until it returns the
real exit code and stdout. Do not treat `functions.wait` on a yielded JavaScript
`exec` cell as shell-output capture: the cell can resume with the original
pending result (`exit_code` absent and empty output) while the child PTY output
remains unread. A Fable consult is complete only when the shell session itself
has terminated and its stdout/stderr has been captured.

After the Fable call completes, invoke
`python3 ~/.claude/scripts/fable_usage.py refresh-display` and include its two
spend estimate lines if it prints anything. This refreshes the current UTC
month from Claude/Codex transcript stores before printing. The lines are
API-equivalent / credit-rate estimates, not an actual billing ledger.
