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

## Activation and continuation

`on <North Star>` is a work command, not a setup-only command. After the state
write has been validated, activate the host's persistent goal mechanism when it
is callable, then immediately begin the first autonomous step implied by the
North Star in the same turn. A response that only reports enabled/status and
stops is forbidden.

On Codex, call `get_goal` first. If no unfinished goal exists, call
`create_goal` with an objective derived from the North Star (for example,
`Execute <North Star> to completion`) and omit `token_budget`. If the active
goal already matches that objective, retain it and continue; do not create a
duplicate. If a different unfinished goal exists, surface the concrete conflict
and do not replace, complete, or block that goal merely to enable autopilot.
`status` and `off` never create a goal.

Claude Code's built-in `/goal` is an opaque host feature and is not callable by
this command, skill, or hooks. Do not claim that Booster invoked it and do not
emit a nested `/goal` as if it had executed. Continue the first autonomous
North-Star step in the activation turn anyway; a brief non-blocking advisory
may tell Dmitry that only the host/user can activate or clear Claude's `/goal`.

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

## Advisory implementation-slice integration

Directional autopilot state and implementation-slice state are separate
authorities. `.claude/autopilot.json` stores the North Star, Fable budget, and
decision provenance. `.claude/state/slice_ledger.json` stores one concrete
artifact contract and its Git evidence. Never merge their schemas or present
directional activation as proof that a slice was acquired, verified, or closed.

For `on <North Star>`, after the host goal has been created or retained and the
first coherent roadmap step has supplied an artifact contract plus explicit
allowed paths, call the fail-closed bootstrap below. It generates a fresh
`run_id`, but always binds the real leading
`session_meta.payload.session_id`; a wrapper UUID is never a substitute for a
Codex root session. Explicit transcript/session inputs are preferred. Ambient
discovery is allowed only through a unique `CODEX_THREAD_ID` match in the
documented Codex session store; zero or multiple matches are typed failures,
never "pick latest". A subagent thread is never accepted as the root. Bootstrap
stdout returns hashes, run ID, and a project-relative mode-0600 binding
reference; it does not disclose the raw session ID or absolute transcript
path. The trusted runner reads that JSON directly, never by shell evaluation.
There is no backfill: immediately record the activation,
then call the installed tools in this order, using the
revision returned by each command:

```text
python3 ~/.claude/scripts/slice_calibration.py --cwd <root> bootstrap [--transcript <jsonl> --session-id <root-session>] --artifact-domain <domain> --expected-control ledger --expected-control git --expected-control verification --expected-control closure
python3 ~/.claude/scripts/slice_ledger.py --cwd <root> acquire --slice-id <slice> --artifact-contract <contract> --allowed-path <path> --session-id <session> --run-id <run>
python3 ~/.claude/scripts/slice_git.py --cwd <root> capture --run-id <run> --session-id <session> --revision 1
```

`session-start` is a durable prerequisite, not best-effort telemetry. If it
returns nonzero, abort slice activation and the instrumented work step: do not
run acquire, capture, or claim a measured session. The directional autopilot
and matching host goal may remain active, but no managed slice begins. Print
the typed failure and retry prospectively; never backfill work performed while
the registry was unavailable.

Enclose every Booster-owned operation in matching `slice_calibration.py
control-start|control-end --kind <kind>` events; emit the end only after the
owned command succeeds. On nonzero/failure emit `control-na --reason
operation_failed` and stop that managed step, never a successful end. When a
wrapper cannot observe the operation, including
an unsupported native Codex surface, record `control-na --kind <kind> --reason
native_surface_unavailable`; use `capability_missing` only for an absent owned
capability. These are the complete reason enum. UNKNOWN blocks promotion. Never synthesize paired
events from prose or after the fact.

At every Booster-owned verification invocation, append `verification-attempt`
with its actual receipt and status. When `slice_close.py close` creates the
terminal handoff, append `session-terminal` using the exact closed ledger tail,
handoff SHA-256, and handoff `terminal_at`; only then append `domain-outcome`
with the actual next domain. A failed wrapper call leaves missing/UNKNOWN
evidence and cannot produce PASS.

This is advisory instrumentation. A failure must be printed as a typed
diagnostic with the failed command and exit status. It must not be described as
native enforcement and must not silently end the activation turn: continue the
first safe work step while making the missing baseline/coverage explicit.

`status` reads cached project-local state only. Read directional status, then
`slice_ledger.py status`; when the exact run/session/revision permits it, read
`slice_git.py attribute`, `slice_close.py status`, and finally:

```text
python3 ~/.claude/scripts/slice_telemetry.py --cwd <root> status --run-id <run> --session-id <session>
```

Do not discover or scan transcripts during status. Print one compact advisory
block containing exactly this capability statement: `Claude hooks/wrappers
advisory; native Codex observational/no enforcement`, plus provider, parser
coverage, unknown count/reasons, terminal disposition, and receipt hash when
available. If cached telemetry is absent, resolve the absolute project root,
one explicitly supported adapter (`codex_rollout_v1` or
`booster_wrapper_v1`), one explicit existing transcript JSONL, and the actual
ledger run/session identifiers before emitting anything. Construct the
executable argv in this exact order: `python3`, the installed
`slice_telemetry.py`, global `--cwd`, resolved root, `record`, `--provider`,
adapter, `--transcript`, JSONL, `--run-id`, run, `--session-id`, session. Print
the shell-quoted concrete command with no ellipsis, metavariables, angle-bracket
placeholders, or guessed transcript path. Codex `session-start` also receives
that explicit transcript: leading `session_meta.payload.session_id` is the root
session identity and must equal `--session-id`; `session_meta.payload.id` is the
thread identity and may intentionally differ. Persist only their hashes and the
metadata hash, never raw transcript metadata.

`off` disables directional autopilot without deleting slice ledger, event,
backlog, handoff, or telemetry history. If a slice is active, surface its typed
state and the appropriate `slice_close.py close` disposition requirement. Do
not invent a terminal disposition or close it merely because autopilot stopped.
Neither `status` nor `off` appends activation, controls, attempts, terminal, or
domain events: cached reads are not lifecycle facts.
