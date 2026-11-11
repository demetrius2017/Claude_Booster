---
description: "Unattended sessions: questions are answered by BIO advisors, never by waiting for Dmitry."
---

# Unattended questions — never idle waiting for a human

In `bypassPermissions`, `dontAsk` and `auto` modes (Desktop or CLI) Dmitry is
usually away. `question_router.py` answers instead of him:

- **AskUserQuestion** is intercepted before any dialog. The hook returns
  `ADVISOR_ANSWER (…)` with a role-specific (BIO) advisor's decision. Treat it
  as the decision, act on it immediately, do not re-ask the same question,
  and mention the advisor decision in the final report.
- **Plan approval (ExitPlanMode)** is decided by the plan-reviewer advisor:
  approve → execute; `PLAN_REVISE` → fix the named defects and resubmit.
- **Ending a turn on a question** is answered at Stop and the turn continues.
  Do not end a turn on a question in these modes: decide (most reversible
  option that completes the task), state the assumption in one line, continue.
- **`DEFERRED_TO_OWNER`** marks a hard owner boundary (real broker orders,
  secrets, irreversible prod/user-data deletion, payments, external
  publication). Do not wait: prepare the action completely, skip only that
  step, finish everything else, and end with «Готово, нажмите X».

Owner review: `~/.claude/state/question_router/decisions.jsonl` (every advisor
decision) and `owner_queue.jsonl` (deferred items); `/start` prints a summary.
Configuration: `~/.claude/question_router.json` (`enabled`, `modes`,
`delivery`, `budget_s`). In Manual (default) and Plan-only sessions questions
still go to Dmitry.
