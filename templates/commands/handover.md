---
description: "End-of-session handover: auto-collect git log, save structured report with Goal+KPI, Required reading, Session reference, verify-gate evidence."
---

Auto-collect: `git log --oneline --since="8 hours ago"` + `Read roadmap.html` if it exists (else `roadmap.md`, else skip — do NOT shell-glob it; see /start step 1 for the nomatch+cascade reason).
Save `reports/handover_YYYY-MM-DD_HHMMSS.md` with the following required sections: summary, tools used, first step tomorrow (copy-paste command), problems/solutions, **plus these three mandatory sections:**

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

**`## Outstanding Debts`** — run `/debt review` (or manually scan the session for incomplete items). Include the formatted table in the handover. If no debts exist, write "No outstanding debts — session clean." This section goes between `## Problems / Solutions` and `## Required reading` in the report.

Update roadmap. Git add + commit + push.

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

**Model routing for handover delegation:** when spawning the handover-write agent, pass `model: "sonnet"` explicitly (Sonnet 4.6 is sufficient for report synthesis). For separate git-log/diff collection spawns, use `model: "haiku"`. Do NOT default to Opus — Lead's expensive model is for orchestration, not report writing.

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
