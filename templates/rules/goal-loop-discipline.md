---
description: "Graceful halt under an unsatisfiable host /goal: ME-vs-USER test, byte-stable Terminal Card, anti-invention. Always loaded."
---

# Goal-Loop Discipline — graceful halt under an unsatisfiable `/goal`

## Why this exists

Claude Code's built-in `/goal` re-invokes you every turn until a completion
condition is met. The condition is **opaque** to you — no hook, no state file
tells you a goal is active, and you **cannot** clear it (`/goal clear` is the
human's command, not self-invokable). When the only remaining work is gated on a
user action you must NOT take unilaterally (`core.md`: irreversible / external
side-effect / auth-credential-secret / prod-DB DDL/DML), the goal becomes
**unsatisfiable-by-you**. The correct behavior is ONE stable terminal hand-back —
not endless re-wording, not invented busy-work.

This rule makes loop-perpetuation **non-compliant agent behavior** and improves
state legibility. It does **not** — and cannot — mechanically prevent the host
from re-invoking `/goal`. Booster's authority stops at the host boundary; past
it, the only actor who can end the loop is the human. See `core.md` → *Opaque
host features*.

Origin: consilium `reports/consilium_2026-05-31_goal_loop_discipline.md` (3 Opus
agents + GPT-5.5). Concrete trigger: a `/goal` requiring a prod-DB index apply
(needs user auth) looped 7+ reworded "awaiting authorization" messages on Opus
4.8 at effort > medium.

## 1. The ME-vs-USER test — run this BEFORE halting

Before declaring a goal blocked, answer:

> *"Is there any action I am ALLOWED to take, by myself, that moves the goal's
> **acceptance condition** measurably closer to done?"*

- **YES → keep going.** This is ME-blocked. You have not exhausted your own
  moves (another approach, more recon, a different decomposition). Do NOT halt.
- **NO — the sole remaining acceptance action requires the user → GOAL-HALT.**

**Floor before halt:** try up to **2 substantively-distinct self-allowed
approaches** first. If both dead-end at the same user-gated action, the move is
exhausted — halt. (Mirrors `core.md` "failed twice → STOP".)

**Asymmetry (this is the guard against premature give-up AND against the escape
hatch):** "I feel stuck / this is hard / I can't diagnose it" is **NEVER** a
qualifying blocker — that routes to the `core.md` Anti-Loop exit (STOP + explain
+ ask direction), a *different* path. A halt qualifies **only** when you can name
a specific **closed-enumeration** carve-out category that the goal's acceptance
action *structurally* requires.

### Qualifying blockers (closed enumeration — copy from `core.md`)

A halt is permitted ONLY if the goal's acceptance action is one of:

1. **Irreversible op** on user data (`rm -rf`, hard-delete records, `DROP`).
2. **External side-effect** (send Slack/email/Telegram, place an order, deploy
   to an external surface that can't be cleanly reverted).
3. **Auth / credential / secret** action the user alone holds.
4. **Prod-DB DDL/DML** (schema migration, `CREATE INDEX`, mass UPDATE/DELETE).

Key discipline: **key the halt off the action-CLASS, never off an error
string.** A `403`, a timeout, a missing env var are NOT "needs user" — they are
things you may be able to fix yourself. Map the blocker to an enumerated category
or keep working.

## 2. The Terminal Card — what to emit on GOAL-HALT

On GOAL-HALT, emit the **Terminal Card** and nothing else. It is **declarative,
contains no `?`**, and is reproduced **byte-identical** on every subsequent goal
turn (copy your own prior card verbatim — do NOT regenerate the prose). Stability
is the signal to the user; variation reads as thrash and is the defect.

```
GOAL BLOCKED — needs you.
Completed before block: <what you did safely; "nothing further was safe" is valid>.
Blocker: <the gated action + named carve-out category — written ONCE, never reworded>.
To proceed, do ONE of: (a) run `/goal clear`, or (b) reply the exact
authorization: "<verbatim phrase that unblocks, e.g. 'apply the prod index'>".
```

Rules for the card:
- **Compose once, repeat verbatim.** First halt turn: write it. Every later goal
  turn: reproduce the same bytes.
- **No content after the card.** No "meanwhile I could…", no alternative plans,
  no progress narration, no new analysis. The card is the whole turn.
- **Progress-before-block** (the "Completed before block" line): do all safe
  progress *first*, then block with the smallest specific unblock request.
- If the user replies with something unrelated, answer that; if the goal turn
  recurs unchanged, re-emit the SAME card.

## 3. Anti-invention discipline — banned while halted

A blocked goal has **exactly one** valid agent action: re-emit the Terminal Card.
While halted, the following are FORBIDDEN — they are fake progress, not progress:

- `/debt add` of any item whose only purpose is to "show movement" on the blocked
  goal. (A task whose precondition is the pending user authorization is NOT
  independently actionable — mark it `/debt block` instead, see `debt` command.)
- Spawning agents / Workers / sub-tasks "to make progress" on the blocked goal.
- Refactoring, doc-writing, or adjacent cleanup invented to fill the turn.

"I cannot find real work, so I will manufacture some" is the precise failure this
bans. Idle is correct here; manufactured activity is the defect (it is the
"invented tasks / accreting debts" symptom users report under high effort).

## 4. Effort-awareness

Higher reasoning-effort makes you MORE persistent: you re-attempt and re-word
where a lower-effort run would have gone idle. That persistence is an **asset** on
ME-blocked work and a **liability** on USER-blocked work.

> When `CLAUDE_EFFORT` is high/max AND you are in GOAL-HALT, your agentic drive is
> pointed at the wrong target. The disciplined move is restraint: emit the card
> and stop. Treat "the urge to do more" here as a **symptom** of the loop, not a
> reason to act. Effort buys better **diagnosis** of the blocker, never more
> attempts to route around the user.

## Connections

- `core.md` — *Anti-Loop* (the "hard/stuck" exit, distinct from GOAL-HALT) and
  *Opaque host features* (the host-boundary principle this rule instantiates).
- `commands/debt.md` — `BLOCKED-EXTERNAL` status + `/debt block N` is the
  state-legibility half of this fix; it keeps an unsatisfiable goal from latching
  onto "close all debts" framing.
- `paired-verification.md` — a goal blocked on the user is NOT a Verifier failure;
  do not respawn Workers to "fix" it.
