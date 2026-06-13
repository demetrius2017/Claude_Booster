---
description: "Track and resolve session debts. /debt list shows inventory, /debt auto clears all HIGH+MED automatically (LOW stays the user's call), /debt work picks the highest priority, /debt review formats for handover."
argument-hint: "[list|auto|add|work|resolve|block|review] [args]"
---

Parse `$ARGUMENTS` and route to the matching mode below. No argument or `list` → run **LIST** mode. Otherwise match the first word.

---

## MODE: list (default)

Produce a debt inventory for the current session. Do all four scans, then merge and deduplicate before printing.

**Scan 1 — Task tool:**
Run `TaskList`. For every task whose status is NOT `completed`, record: task id, title, status (`in_progress` / `blocked` / `not_started`).

**Scan 2 — Session transcript patterns:**
Run:
```bash
python3 ~/.claude/scripts/session_context.py --tools-only --grep "TODO\|FIXME\|HACK\|next session\|следующая сессия\|deferred\|Tier 2\|Tier 3" --no-thinking 2>/dev/null | tail -60
```
For each hit, extract the surrounding sentence and classify:
- `TODO` / `FIXME` / `HACK` in an Edit or Write call → likely open code debt.
- "next session" / "следующая сессия" / "deferred" in an assistant message → explicitly deferred work.
- "Tier 2" / "Tier 3" → backlog items that were consciously postponed.

Also run:
```bash
python3 ~/.claude/scripts/session_context.py --tools-only --grep "test.*fail\|exit code [^0]\|FAILED\|ERROR" --no-thinking 2>/dev/null | tail -40
```
Any failing test that has no subsequent passing run = HIGH-priority debt.

**Scan 3 — Git state:**
```bash
git -C "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" status --short 2>/dev/null
```
Uncommitted changes or untracked files in non-allowlisted paths (not `reports/`, `docs/`, `*.md`) → HIGH-priority debt.

**Scan 4 — Manual debts file:**
Read `.claude/.session_debts.json` (if it exists). Include every item with `status: "open"`. Items with `status: "BLOCKED-EXTERNAL"` go to the **separate blocked section** (see output format) — they are NOT open/closeable work.

**Merge and classify:**

| Priority | Condition |
|----------|-----------|
| HIGH | Failing tests not re-run; uncommitted non-doc changes; blocked tasks |
| MED | In-progress tasks without evidence of completion; deferred Tier 1 items; open `.session_debts.json` entries |
| LOW | Tier 2/3 items from transcript; nice-to-haves; documentation gaps |

**Output format:**
```
## Session Debt Inventory

[1] HIGH  Uncommitted changes in src/foo.py (git status)
[2] HIGH  Task "fix rebuilder edge case" is blocked (task-id: abc123)
[3] MED   "deferred to next session: dep_guard.py implementation" (transcript)
[4] LOW   Tier 3 — ADR adoption not yet started (transcript)

Total: 4 open items (2 HIGH, 1 MED, 1 LOW)

## Blocked — needs human  (NOT counted as open/closeable work)

[B1] apply prod index — needs: run `CREATE INDEX CONCURRENTLY` (prod-DB, user auth)
```

**[CRITICAL]** `BLOCKED-EXTERNAL` items are **excluded from the open count** and from any "close all debts" framing. They are not the agent's to close — only the user can clear them. Never re-classify a blocked item as open to "make progress," and never count them toward a `/goal`-style completion condition (see `goal-loop-discipline.md`). If there are blocked items but zero open items, the session is still **clean** for agent-actionable work — say so explicitly.

If both sections are empty: print `No open debts — session clean.` and stop.

---

## MODE: add

Parse: `add <description>`. If no description, print usage and exit.

Read `.claude/.session_debts.json` (return `[]` if file absent or unreadable).

Append a new entry:
```json
{"id": <next integer>, "description": "<description>", "priority": "MED", "added_at": "<ISO-8601 timestamp>", "status": "open"}
```

Write back to `.claude/.session_debts.json`. Print:
```
Added debt #<id>: "<description>" [priority: MED]
To change priority: edit .claude/.session_debts.json directly.
```

**[CRITICAL] Do NOT `/debt add` speculative or invented items to "show progress"** — especially under an active `/goal` (see `goal-loop-discipline.md` §3). Debt is for real, independently-actionable work. If a task's only precondition is a pending user authorization, it is NOT independently actionable — use `/debt block` (below), not `add`.

---

## MODE: block N

Parse `block <N> "<unblock_action>"`. Marks an existing debt as **blocked on an external/user action** the agent must not take unilaterally.

**Validity checklist — ALL must be true (else this is NOT a valid block; keep working):**
- The missing item is **external to the agent** (user authorization, a credential/secret only the user holds, an irreversible/external action per `core.md`, a human decision).
- The agent **cannot safely infer, substitute, or work around** it.
- **Meaningful safe progress is no longer possible** without it.
- `<unblock_action>` is **specific and minimal** — the exact smallest thing the user must do.

"This is hard / I'm stuck / tests fail" does **NOT** qualify — that is `core.md` Anti-Loop, not a block. Difficulty is never an external block.

Read `.claude/.session_debts.json`. Find item `id == N` (create the entry first via `add` if it's a transcript-only debt). Set:
```json
{"status": "BLOCKED-EXTERNAL", "unblock_action": "<unblock_action>", "blocked_at": "<ISO-8601 timestamp>"}
```
Write back. Print:
```
Debt [<N>] → BLOCKED-EXTERNAL. Needs human: <unblock_action>.
Excluded from open/closeable count. Clear it by doing the action, then /debt resolve <N>.
```

---

## MODE: work (no number)

1. Run LIST mode internally (do not reprint the full output — just build the inventory).
2. Select the first HIGH-priority item. If no HIGH items, select the first MED-priority item. If only LOW, select the first LOW item. If none, print `No open debts to work on.` and stop.
3. Print: `Working on debt [<N>] <priority>: <description>`
4. Create a task via `TaskCreate` with title = `debt[<N>]: <description>`.
5. Begin implementation. For code changes (Edit/Write to ≥2 files, or any behavior change), follow the paired Worker+Verifier pattern from `paired-verification.md`. For trivial changes (<2 files, formatting only, typo), proceed directly.

---

## MODE: work N

Parse `work <N>` where N is an integer.

Run LIST mode internally to get the current inventory. Find item `[N]`. If not found, print `Debt item [N] not found. Run /debt list to see current inventory.` and stop.

Print: `Working on debt [<N>] <priority>: <description>`

Create a task via `TaskCreate` with title = `debt[<N>]: <description>`.

Begin implementation per the same Worker+Verifier rules as MODE: work above.

---

## MODE: auto

**Automatically resolve every HIGH and MEDIUM debt; leave LOW debts for the user's decision.** This is the "clear the board" mode: HIGH/MED are the agent's to take without asking; LOW is a judgement call that stays with the user; `BLOCKED-EXTERNAL` is never touched.

1. Run LIST mode internally to build the inventory.
2. **If there are zero HIGH and zero MED open items** → skip straight to step 4 (nothing to auto-work).
3. **Auto-work loop** — repeat until no HIGH or MED open items remain (hard cap: **12 iterations** to prevent runaway; if the cap is hit, stop and report what's left):
   a. Select the first HIGH item; if none, the first MED item.
   b. Print: `Auto-working debt [<N>] <priority>: <description>`
   c. Resolve it by its nature:
      - **Substantial code** (≥20 lines / any behaviour change / ≥2 files) → run it through **`/go`** (the Шестёрка) so it is designed, cross-provider verified, and KPI-recorded. Do NOT hand-write it inline.
      - **Trivial** (config/doc/<20 lines, formatting, typo) → Lead edits directly.
      - **Uncommitted-change debt** → commit it (with a real message). **Failing-test debt** → fix + re-run until green.
   d. On success → `/debt resolve <N>` and commit. Re-run LIST (a fix may surface a NEW HIGH/MED follow-up, or close several at once).
   e. **If a debt turns out to need the user** (a real external/auth/irreversible blocker surfaces per `core.md`) → `/debt block <N> "<unblock_action>"` and move on. **If a debt fails to resolve twice** → skip it, leave it open, report it, and move on (`core.md` Anti-Loop — never a third identical attempt).
4. **Hand the rest to the user — STOP, do NOT auto-work LOW or BLOCKED:**
   ```
   /debt auto — done.
   Auto-resolved <K> HIGH/MED debts:
     [N] <priority> <description> → <how: /go PASS / committed / fixed> (<commit SHA>)
     ...
   <if any were skipped/blocked: list them with the reason>

   Remaining LOW debts — YOUR call (I will not touch these without a go-ahead):
     [N] LOW  <description>
     ...
   Reply with numbers to work (e.g. "work 4 6"), or "skip" to leave them.
   ```
   If there are `BLOCKED-EXTERNAL` items, list them separately as needs-human (unchanged) — they are not part of the LOW decision.
   If after the loop there are zero LOW items too → print `Board clear — all HIGH/MED auto-resolved, no LOW remaining.`

**[CRITICAL] Guardrails (auto mode is powerful — these are non-negotiable):**
- NEVER auto-work a `LOW` item or a `BLOCKED-EXTERNAL` item — both require the user. LOW is explicitly the user's decision boundary.
- NEVER invent debts to keep the loop running (`goal-loop-discipline.md` §3). The loop ends when the real HIGH/MED inventory is empty, not when you run out of obvious work.
- Each auto-worked code debt MUST pass its own verification (the Шестёрка's exit-code test gate, or a real command for non-`/go` debts). "Looks done" is not resolved.
- Re-run LIST every iteration — operate on the live inventory, not a stale snapshot.

---

## MODE: resolve N

Parse `resolve <N>` where N is an integer.

**Check TaskList:** if there is a task matching `debt[N]:`, update it to `completed` via `TaskUpdate`.

**Check `.session_debts.json`:** read the file. If an item with matching `id` exists, set its `status` to `"resolved"` and add `"resolved_at": "<ISO-8601 timestamp>"`. Write back.

Print: `Resolved debt [<N>]. Item marked closed.`

If item N was not found in either source, print: `Debt item [N] not found. It may have been from a transcript scan (not stored). Run /debt list to verify current state.`

---

## MODE: review

Generate a formatted debt summary suitable for pasting into a handover report.

1. Run LIST mode internally to build the full inventory.
2. Format output as:

```markdown
## Outstanding Debts

| # | Priority | Description | Status |
|---|----------|-------------|--------|
| 1 | HIGH | Tests not passing for require_task.py edge case | open |
| 2 | MED | dep_guard.py not yet implemented (Tier 2) | deferred |
| 3 | LOW | ADR practice not yet adopted in any project | backlog |

### Blocked — needs human (not agent-actionable)

| # | Needs from you |
|---|----------------|
| B1 | apply prod index — run `CREATE INDEX CONCURRENTLY` (prod-DB, user auth) |

Carried from: <session JSONL path or "current session">
```

Keep the two sections separate. `BLOCKED-EXTERNAL` items go ONLY in the "Blocked — needs human" table, never in "Outstanding Debts" — the next session must see them as human-gated, not as unfinished agent work. Omit the blocked table entirely if there are no blocked items.

3. If the inventory is empty, output:
```markdown
## Outstanding Debts

No outstanding debts — session clean.
```

4. Print the block to stdout. Do NOT write it to a file — the caller (handover or user) will copy-paste it.

---

## Integration note for /handover

When running `/handover`, invoke `/debt review` before finalising the report and insert its output as the `## Outstanding Debts` section. Place it after `## Problems / Solutions` and before `## Required reading`. If the section is already present in a prior handover template, replace it with the fresh output.
