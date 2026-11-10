---
description: "End-of-session handover: finish and clean up owned work, record remaining branches/worktrees/folders, and save the report with Goal+KPI, required reading and verification evidence."
---

Auto-collect: `git log --oneline --since="8 hours ago"` + `Read roadmap.html` if it exists (else `roadmap.md`, else skip — do NOT shell-glob it; see /start step 1 for the nomatch+cascade reason).

## Finish work and reconcile the workspace (Claude and Codex)

Before writing the final report, account for the session's branches, worktrees,
uncommitted changes and local working folders. Apply the same procedure in both
clients; delegating report writing does not transfer the main agent's cleanup
responsibility.

1. **Inventory.** Read `git status --short --branch`, `git branch -vv` and
   `git worktree list --porcelain`. Check each session-owned worktree's status,
   including untracked files, and the temporary folders/checkouts created during
   this work. Reconcile leftovers recorded in the previous handover. Identify
   ownership and active agents/processes before changing anything; unrelated or
   uncertain ownership means retain and report, not automatic deletion.
2. **Finish completed work.** If the agreed task is complete, run any still-needed
   scoped checks, commit its remaining intended changes using explicit paths, and
   complete the publication/integration already authorized by the task. Distinguish
   committed, pushed, PR open and merged; a push alone does not mean integrated.
   Do not merge around review/release gates or mix unrelated changes into a cleanup
   commit. If a required step cannot finish, record its exact state and next action.
3. **Clean up completed work.** Remove session-owned temporary worktrees and local
   branches once their useful commits are verified in the intended integration
   target and no unique work remains. Verify against current target refs (refresh
   the relevant remote ref when needed); upstream tracking or a stale local main
   is not proof. For a squash/rebase merge, check the actual PR/target result rather
   than treating failed ancestry as proof of integration. Move to a surviving
   checkout first and keep the handover/evidence outside the directory being
   removed. Check for active users/agents/processes and valuable ignored files;
   clean `git status` alone is insufficient. Use `git worktree remove` without
   `--force` and `git branch -d`; if safe removal refuses, retain and explain.
   Delete a remote task branch only when its integration, ownership and existing
   cleanup authorization are established; preserve default/protected/shared
   branches. Remove ordinary session-created folders only when their contents
   are confirmed disposable or durably preserved elsewhere. Never use blanket
   `git clean`, forced removal/reset, or a stash as a substitute for accounting
   for work. Unknown, dirty or still-active locations stay in the report.
4. **Preserve unfinished work.** Save useful progress as a scoped checkpoint when
   appropriate and within the task's authority. Otherwise identify exact dirty
   paths and unpushed commits so they can be resumed. Do not discard unfinished
   work merely to make the tree clean or call it complete. Retain its branches
   and folders, with the reason and a concrete next step.

Save `reports/handover_YYYY-MM-DD_HHMMSS.md` with summary, tools used, first step tomorrow (copy-paste command), problems/solutions, **plus the following sections:**

**`## Goal + KPI`** — three sub-items:
- *North Star*: one sentence — what the project track achieves long-term.
- *Current milestone*: what this sprint/session was targeting.
- *KPI*: a measurable success criterion. Copy from the prior handover unless the milestone changed; update if it did.

**`## Required reading`** — bulleted list of `path` + one-line reason why the next session MUST read it before touching any code. At minimum: this handover file itself. Add any file whose current state the next session cannot safely ignore (e.g. a config whose format changed, a module with a fresh invariant, a migration that altered schema).

**`## Session reference`** — obtain via this snippet and paste the result:
```bash
# CC encodes the project dir by replacing EVERY non-alphanumeric char with '-'
# (so /Users/.../Claude_Booster -> -Users-...-Claude-Booster; note '_' -> '-' too).
ls -t "$HOME/.claude/projects/$(git rev-parse --show-toplevel 2>/dev/null | sed 's/[^a-zA-Z0-9]/-/g')"/*.jsonl 2>/dev/null | head -1
```
Format as: `Session UUID: <uuid>  JSONL: <full-path>`. Add a one-line note that this JSONL can be grepped during RECON to understand what was tried and what failed in this session.

**`## Outstanding Debts`** — run `/debt review` (or manually scan the session for incomplete items). Include the formatted table in the handover. If no debts exist, write "No outstanding debts." Workspace cleanliness is recorded separately below. This section goes between `## Problems / Solutions` and `## Required reading` in the report.

**`## Workspace closure`** — record the inventory and actual cleanup outcome for
branches, worktrees and local folders, including inherited leftovers still
present. Use a compact table:

| Path / repository | Branch / HEAD | Owner / purpose | Git state and publication | Disposition / reason | Next action |
|---|---|---|---|---|---|

Give exact paths, relevant commit IDs, dirty/untracked paths and unpushed commits;
state whether work is merged, awaiting a PR, local only, or unverified. Mark each
item as removed, retained intentionally, or unresolved. If nothing remains beyond
the intended checkout, say so and name the checks that established it. Never
claim "session clean" while unaccounted changes or workspace leftovers remain.
Summarize unresolved/retained work in `## Summary`, and put the actionable resume
command and this section's reference in `## First step`. These short sections are
what the next `/start` reads; do not bury the only warning in the table. Carry
unfinished cleanup into `## Outstanding Debts` too, with its blocker. Intentional
retention is not a cleanup failure, but must remain visible to the next session.

Update roadmap. Stage only the intended paths, then commit and push the handover
under the existing verify gate and publication authority. After the final commit
and any cleanup, recheck status, branches and worktrees; report the actual result
and any failed commit/push/removal. A report commit alone does not close unfinished
implementation or erase local-only work. Keep the final report in a surviving
checkout; if new leftovers are discovered, correct it before declaring completion.

**`## Slice telemetry`** — snapshot cached advisory evidence only. Read the
current `slice_ledger.py status`, then call `slice_telemetry.py status` with its
exact run/session. Do not scan transcript stores. Record capability `Claude
hooks/wrappers advisory; native Codex observational/no enforcement`, receipt
path/hash, provider/adapter, parser coverage, unknown count/reasons, and typed
terminal disposition. If no receipt exists, first resolve the absolute project
root, explicit supported adapter, explicit existing transcript JSONL, and
actual ledger run/session IDs. Emit a fully concrete shell-quoted command whose
argv order is `python3`, installed `slice_telemetry.py`, global `--cwd`, root,
`record`, `--provider`, adapter, `--transcript`, JSONL, `--run-id`, run,
`--session-id`, session. Ellipses and placeholders are forbidden. This snapshot
does not close an active slice and does not merge `.claude/autopilot.json` with
the slice ledger. The handover remains docs-only for verify-gate purposes when
the staged diff contains only allowlisted documentation/report/test paths;
merely reading or citing telemetry does not turn it into runtime verification.

**Model routing for handover delegation:** when spawning the handover-write agent, pass `model: "sonnet"` explicitly (Sonnet 5 is sufficient for report synthesis). For separate git-log/diff collection spawns, use `model: "haiku"`. Do NOT default to Opus — Lead's expensive model is for orchestration, not report writing.

**[CRITICAL] Verify-gate JSON block — required before `git add`/`git commit` of the handover file.**
Before running `git add reports/handover_*.md` or `git commit … reports/handover_*.md`, emit one of these as an assistant text block (the PreToolUse hook `verify_gate.py` scans the last 200 transcript lines for it):

```json
{"verified": {"status": "pass", "evidence": ["<strong-evidence-1>", "<strong-evidence-2>"], "reason_na": null}}
```

or, for docs-only sessions:

```json
{"verified": {"status": "na", "evidence": [], "reason_na": "<why no runtime change to verify>"}}
```

Strong evidence must include a recognised marker (`curl`, `wget`, `psql`, `sqlite3`, `SELECT`, `PRAGMA`, `HTTP/`, `docker`, `kubectl`, `DevTools`, `pytest`, `exit=<N>`) AND:
- for HTTP/curl/wget: a 1xx-5xx status code in the same entry;
- for SQL/DB: a rowcount or `N rows` marker.

Automatically rejected (fake-evidence patterns):
- `localhost` / `127.0.0.1` as target — must be a real staging/prod URL;
- `|| true` — swallows failures;
- `curl -s` without `--fail` / `-o` / `| tee` — suppresses both exit code and body.

`status='na'` is allowed only when `git diff --cached --name-only` touches exclusively allowlisted paths: `docs/`, `reports/`, `audits/`, `.claude/`, `tests/`, `*.md`, `*.txt`, `README*`. Any Python/TypeScript/Dockerfile/YAML change requires `status='pass'` with evidence.

Per-project control via `.claude/CLAUDE.md` YAML frontmatter key `verify_gate: enforcing|warn|off`:
- `enforcing` — hook blocks the commit (exit 2);
- `warn` — hook logs to stderr and `~/.claude/logs/verify_gate_decisions.jsonl` but does not block;
- `off` — hook is a no-op (default for projects without `deploy/` or `.github/workflows/`).

Decisions log (every fire): `~/.claude/logs/verify_gate_decisions.jsonl`. Review weekly — projects silently stuck on `off` are visible there.
