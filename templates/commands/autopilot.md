---
description: "Enable, inspect, or disable Fable autopilot for reversible engineering decisions"
argument-hint: "on <North Star> | status | off"
---

# /autopilot — Fable Decision Autopilot

Autopilot lets Fable answer engineering questions that would otherwise pause
the Lead. It does not make Fable the Lead: the Lead owns execution and records
every delegated decision as `decision_source=fable_autopilot`.

## State

Store project-local state in `.claude/autopilot.json` using an atomic temporary
file plus rename. Validate the written JSON before reporting success.

For `on <North Star>`, require a nonblank North Star and write:

```json
{
  "version": 1,
  "enabled": true,
  "scope": "<resolved git/workspace root absolute path>",
  "north_star": "<verbatim North Star>",
  "calls_used": 0,
  "max_fable_calls": 3,
  "degraded": false,
  "decision_policy": "delegate_except_ui_and_hard_authority",
  "reservations": {},
  "checkpoints": [],
  "provenance": []
}
```

`status` reads and validates state without changing it. `off` atomically sets
`enabled=false`; do not delete history or counters.

## Routing contract

When `fable_autopilot.py` returns `FABLE_DELEGATE`:

1. Do not fabricate a synthetic user-response event.
2. Build a concise Verified Facts Brief containing the question, current North
   Star, observed code/runtime facts, options, reversibility, and constraints.
3. Pipe it to `~/.claude/scripts/fable_consult.sh` as a read-only decision call.
4. Run `fable_autopilot.py consult-decision --prompt-file <brief>`. This trusted
   runner alone reserves the nonce, invokes `fable_consult.sh`, captures and
   hashes its exact output/status, validates the typed verdict, and completes
   state in-process. There is no public caller-supplied receipt completion.
   The runner pins the sibling installed `fable_consult.sh`, removes
   caller-selected wrapper/model startup variables, and builds a minimal
   environment allowlist containing only HOME/auth, locale, TLS, terminal and
   temporary-directory inputs plus a canonical system PATH. Shell/Python
   startup injection variables are not inherited.
   Successful completion increments `calls_used` exactly once and records
   `decision_source=fable_autopilot`, preserve that provenance, summarize
   Fable's reasoning, and continue.
5. On failure, atomically set `degraded=true` and ask Dmitry the original
   question. Never substitute another model while claiming Fable provenance.

Everything may be delegated except Dmitry's personal acceptance/control of UI
actions and the hard authorization boundary: secrets; real/user/production
data; persistent project files at risk; destructive irreversible actions; external
messages, publication, payments, or orders; and expansion beyond authority
already granted. Those always route to Dmitry. Classify security questions by
blast radius, reversibility, and scope—not by the word `security`: deleting and
recreating a validated task-specific `/tmp` fixture or tightening a sandbox
permission is reversible/local and may be delegated to Fable.

### Routing precedence and cadence

`USER_ONLY` (hard authority) > `CADENCE` (pure timing/sequencing: proceed on
the roadmap default, no ask, no consult) > `FABLE_DELEGATE` (engineering
decisions). Under active autopilot with a North Star, ending a turn with a pure
timing/cadence question is forbidden: proceed now, stating the assumption in
one line. A non-blocking status line such as “starting phase X; say stop to
defer” is allowed, but a blocking question is not.

## Event-driven course correction

Use phase/event based checkpoints, not polling by time or tool-call count.

- plan/PFD completion;
- the first coherent implementation slice, when evidence suggests drift from
  the North Star;
- final diff review.

Reuse `/go fable`'s typed `fable_control.watchlist` (`OPEN`/`CLOSED`, target
phase, required evidence, closure evidence). A checkpoint returns one of
`ON_COURSE`, `REFOCUS`, `REPLAN`, or `ASK_USER`. `REFOCUS` must state what to
stop, what North-Star requirement was lost, and the next concrete step.

The ordinary budget is three successful calls per state activation: up to two
course checkpoints plus one delegated answer. Worker/verifier retries, polling,
and routine debugging never consume Fable calls. If the existing usage snapshot
is at least 80%, set `degraded=true`; continue locally except that a delegated
decision must fall back to Dmitry.

Executable checkpoints use:

```text
fable_autopilot.py checkpoint plan_complete --prompt-file <brief>
fable_autopilot.py checkpoint first_slice --prompt-file <brief>
fable_autopilot.py checkpoint final_diff --prompt-file <brief>
```

Each trusted command owns reserve → `fable_consult.sh` → exact-output hash →
typed validation → in-process completion. A caller cannot submit a receipt.
`VERDICT` is exactly `ON_COURSE`, `REFOCUS`, `REPLAN`, or `ASK_USER`.
Non-`ON_COURSE` verdicts require a directive. Closed watchlist items require
closure evidence. The state utility locks, validates scope and budget, then
atomically reconciles the typed checkpoint/provenance record. Feed the current
Fable usage percentage through `fable_autopilot_state.py usage --percent N`;
`N >= 80` persists a machine-readable degraded state.

Hooks trigger these independently of Lead prose: `ExitPlanMode` requests
`plan_complete` and `TaskCompleted` requests `final_diff`. `first_slice` is a
conditional/manual checkpoint only when coherent drift evidence exists. At
most two checkpoint calls are allowed, preserving one delegated-answer slot in
the ordinary three-call budget. Phases are ordered and unique. Completion is
idempotent only for the identical receipt. Reservations include `created_at`;
stale entries expire after the bounded TTL with an audited release reason.
`final_diff` cannot complete while any watchlist item remains `OPEN`.
Use `fable_autopilot_state.py recover --reason <reason>` to clear degradation
explicitly while preserving history and appending recovery provenance.
