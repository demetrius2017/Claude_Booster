---
description: Schedule bounded, external wake-ups for one explicit Codex thread
argument-hint: 'INTERVAL --thread UUID | status | stop'
---

# Loop

Use `$loop`, or legacy `/prompts:loop`; neither is a guaranteed native `/loop`
slash command. This is a bounded scheduler, not an invitation for the model to
wait or poll: Python sleeps outside the conversation and sends a queue message
to exactly the supplied existing thread after each full interval.

Route commands directly to the installed runner:

```sh
python3 ~/.claude/scripts/codex_loop.py start 30m --thread <uuid>
python3 ~/.claude/scripts/codex_loop.py status
python3 ~/.claude/scripts/codex_loop.py stop
```

`start` requires `INTERVAL` (`30s`, `30m`, or `2h`) and `--thread`; it never
infers the latest thread. Optional flags are `--message TEXT`, `--max-wakes N`
(default 8), `--queue-timeout SECONDS`, and `--cwd PATH`. The deterministic
default message asks the thread to inspect and continue only if useful. First
wake is after one complete interval. `status` never invokes Codex. `stop` is
idempotent; during an in-flight queue it requests a cooperative stop and lets
the bounded queue timeout settle the child.

A queue timeout is terminal locally and is never retried automatically. The
remote server may nevertheless have accepted the wake before the local timeout,
so that boundary has at-least-once delivery ambiguity.

State is project-bound outside the repository, owner-only, atomically written,
and retained after completion/failure/stop. A corrupt, insecure, stale-active,
or already-live state is a loud blocker rather than something to overwrite.
