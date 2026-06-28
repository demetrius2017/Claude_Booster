---
description: "Multi-agent code audit — parallel lens-specific agents (correctness, security, performance, architecture, data integrity, operational) + external PAL/Z.ai review. Each auditor does independent RECON and returns a structured verdict."
argument-hint: "<topic> [--scope <path>] [--focus <lens1,lens2>]"
---

## Progress tracking
Before each phase below, run: `python3 ~/.claude/scripts/phase.py progress "<N>/5 <step_label>"`
After the final step completes, run: `python3 ~/.claude/scripts/phase.py progress clear`

Steps: `1/5 brief_build`, `2/5 lens_selection`, `3/5 lens_audits`, `4/5 synthesis`, `5/5 report`

Parse `$ARGUMENTS`:
- Everything before the first `--` flag is the `<topic>` (what to audit — a feature, PR, component, or free-form description)
- `--scope <path>` — restrict file search to this path or glob (e.g. `services/trading/`, `*.py`)
- `--focus <lens1,lens2>` — comma-separated subset of lenses to run (e.g. `security,data-integrity`); if omitted, Lead selects based on topic

---

## Phase 0 — BRIEF BUILD (Lead, mandatory first step)

Before spawning any agents, Lead must build a **Verified Facts Brief** by doing lightweight RECON (≤5 Read/Bash calls):

1. Read `ARCHITECTURE.md` if it exists (project name, primary stack, critical components)
2. Read `docs/dep_manifest.json` if it exists (identify components touched by the topic; note any marked `critical: true`)
3. Read the scope path (if `--scope` given) — `ls` to confirm it exists
4. If topic mentions a specific file or function: Read that file (first 80 lines for context)
5. Check `git log --oneline -10` to understand recent churn around the topic

Produce this brief internally (not printed to user — it goes into every agent prompt):

```
Verified Facts Brief:
  Project: <name from ARCHITECTURE.md or best guess from repo structure>
  Stack: <languages, frameworks>
  Scope: <path restriction from --scope, or "full repo">
  Topic: <audit topic as stated by user>
  Key files: <up to 5 file paths most likely relevant, from ARCHITECTURE.md / dep_manifest / ls>
  Recent churn: <functions/files with most recent commits, from git log>
  Critical components: <any dep_manifest entries with critical: true that touch the scope>
  Architecture map consulted: <yes/no>
```

---

## Phase 1 — LENS SELECTION (Lead)

There are 6 audit lenses. By default, Lead picks the most relevant 3–5 based on the topic. If `--focus` is provided, run exactly those lenses.

| Lens ID | Name | When to include |
|---------|------|-----------------|
| `correctness` | Correctness | Always include unless topic is purely operational |
| `security` | Security | Topic mentions auth, payments, tokens, secrets, APIs, user data |
| `performance` | Performance | Topic mentions slow, latency, scale, N+1, caching, load |
| `architecture` | Architecture | Topic mentions refactor, new feature, interface change, dependency |
| `data-integrity` | Data Integrity | Topic mentions DB, reconcile, finance, ETL, consistency, state |
| `operational` | Operational | Topic mentions deploy, observability, logging, error recovery |

Print selection rationale:
```
Audit lenses selected: correctness, security, data-integrity
  correctness: always on
  security: topic mentions API tokens
  data-integrity: topic involves financial calculations
  (skipped: performance, architecture, operational — not relevant to topic)
```

---

## Phase 2 — PARALLEL AUDIT (spawn all **selected** lens agents in ONE message)

**[CRITICAL] Spawn all selected lens agents AND external review IN A SINGLE MESSAGE as parallel tool calls, each with `run_in_background: true` when the tool supports it.** Do not wait for one to finish before starting another. External review runs in parallel with the auditors — not after.

External-review order:
1. PAL/GPT (`mcp__pal__codereview` or `mcp__pal__second_opinion`) is the primary external expert when PAL is available.
2. Z.ai GLM-5.2 is the third-model reviewer when `ZAI_API_KEY` is present. Run it via:
   `printf '%s\n' '<review prompt>' | ZAI_API_KEY="$ZAI_API_KEY" ~/.claude/scripts/zai_cli.py review --budget 5`
3. xAI Grok is the fourth-model reviewer when Grok CLI is authenticated. Run it via:
   `printf '%s\n' '<review prompt>' | ~/.claude/scripts/grok_cli.py review --budget-turns 3`
4. If PAL is unavailable, GLM-5.2 is mandatory fallback. If GLM is unavailable but Grok is authenticated, Grok is the mandatory fallback. If PAL, Z.ai, and Grok are all unavailable, label the run `external-review: DEGRADED (PAL unavailable; ZAI_API_KEY absent; Grok unauthenticated)` and continue with the selected lenses.

Spawn only the lenses selected in Phase 1. Do not spawn auditors for unselected lenses.

Each auditor agent is `subagent_type: "general-purpose"`, `model: "sonnet"`.
PAL runs as a tool call in the same batch when available. GLM-5.2 and Grok run as read-only Bash reviewers and must receive the same Verified Facts Brief and key file list.

### Progress output

Before spawning, output one line: `Starting audit: <N> lenses + <external reviewers>`

As each background agent completes, output a cumulative progress line:
```
Audit ▰▱▱▱▱▱▱ 1/<total> · <lens> ✓ (<verdict>)
Audit ▰▰▱▱▱▱▱ 2/<total> · <lens> ✓ · <lens> ✓
```
Where `<total>` is the number of selected lenses plus every available external reviewer (PAL, GLM-5.2, and/or Grok), or just the selected lenses if external review degraded. Fill in lens names and verdicts (PASS/FAIL/CONCERN) as each returns. **Do NOT begin Phase 3 synthesis until ALL agents/reviewers have returned.**

Every agent receives:
- The **Verified Facts Brief** from Phase 0
- The **Artifact Contract** below (same for all agents)
- Their **lens-specific BIO and search mandate** (unique per agent — see below)

**Artifact Contract (identical for all auditor agents):**

```
Objective: Audit the codebase against a specific quality lens and return a structured verdict.
Artifact path: return structured verdict as text output (Lead collects all verdicts)
Invocation: return output directly, no file write needed
Inputs: codebase files within scope; topic description; Verified Facts Brief
Expected observable behavior: structured verdict with LENS, VERDICT, FINDINGS (severity + file:line + evidence), RECOMMENDATIONS
Out of scope: do not implement fixes; do not modify files; do not re-audit other lenses
Environment constraints: read-only grep and file inspection only; use Bash grep/find/read — do NOT skip if first search returns nothing, try alternative patterns
Acceptance emphasis: every FINDING must cite file:line and show the actual code snippet as evidence; "I believe" / "likely" without code evidence is NOT a finding
Affected downstream: Lead collects all lens verdicts for Phase 3 synthesis
Architecture map consulted: <yes/no — from Verified Facts Brief>
```

---

### Shared Verdict Format (all auditors)

Every auditor returns output in EXACTLY this format. No prose before or after.

```
LENS: <lens-id>
VERDICT: PASS | FAIL | CONCERN

SUMMARY: <1-2 sentences on what you found overall>

FINDINGS:
[If PASS: write "No findings."]

FINDING-<PREFIX><N>:
  severity: HIGH | MED | LOW
  title: <short title>
  file: <file_path>:<line_number>
  evidence: |
    <paste the actual code snippet — minimum 3 lines of context>
  explanation: <why this is wrong and what can go wrong in production>
  <extra_field_if_any>

FINDING-<PREFIX><N+1>:
  ...

RECOMMENDATIONS:
[If PASS: write "No recommendations."]
- <specific fix recommendation with file:line reference>
- ...
```

VERDICT rules:
- PASS: no findings at any severity
- CONCERN: only LOW or MED findings (no HIGH)
- FAIL: at least one HIGH finding

Finding prefix and extra field per lens:

| Lens | Prefix | Extra field |
|------|--------|-------------|
| correctness | C | _(none)_ |
| security | S | `cwe: <CWE-XXX or N/A>` |
| performance | P | `estimated_impact: <"200ms per request" or "O(n²) — will fail at n>1000" or similar>` |
| architecture | A | `blast_radius: <list of files/functions that depend on this — from grep or dep_manifest>` |
| data-integrity | D | `recovery_path: <how to detect and fix corrupted data, or "no recovery path">` |
| operational | O | `oncall_impact: <"no alert fires" \| "alert fires but diagnosis requires X" \| "silent data loss">` |

---

### Shared mandate header (prepended to every auditor's mandate below)

Every auditor mandate is prepended with this header block. Remove it from the individual auditors only if you copy-paste the mandate — it belongs at the top of each agent's prompt.

```
Audit topic: <topic>
Scope: <scope path or "full repo">

Verified Facts Brief:
<insert brief from Phase 0>
```

---

### Auditor A — Correctness Lens

**BIO:**

```
You are the Correctness Auditor for this /audit run. You think like a careful senior engineer who has seen production bugs from subtle logic errors. Your job is to find places where the code does the WRONG THING — not where it is slow or insecure, but where it produces incorrect outputs, violates its own contracts, or fails on edge cases.

You do NOT assume code is correct because it was written by a smart person. You READ it and verify it.
```

**Your mandate:**

```
<prepend shared mandate header>

## What you look for

1. **Input contract violations** — functions that accept None/NaN/negative/empty when their doc or callers assume they won't. Look for: no input validation, silent coercion (value = value or default), missing None checks.

2. **Output contract violations** — functions that can return None/NaN/negative when callers assume they never will. Look for: missing return statements on some branches, `return None` without callers checking it.

3. **Logic errors** — off-by-one, wrong operator, inverted condition, wrong variable used in calculation. Pay special attention to: financial calculations (rounding, Decimal vs float), index math, boundary conditions.

4. **Missing edge cases** — empty list, zero divisor, concurrent modification, retry without idempotency check.

5. **Invariant violations** — things that should always be true but the code doesn't enforce. E.g., "sum of positions == total NAV" but no assertion anywhere.

6. **Exception handling gaps** — bare `except: pass`, swallowing errors that should propagate, catching the wrong exception type.

## Search strategy — DO NOT SKIP THESE

```bash
# Find the key files related to the topic
grep -rn "<topic_keyword>" --include="*.py" --include="*.ts" --include="*.tsx" <scope>

# Look for silent fallback anti-patterns
grep -rn "or 0\|or \[\]\|or {}\|or default\|or None" <scope> --include="*.py"

# Look for bare except
grep -rn "except:\|except Exception: pass\|except Exception as.*:\s*pass" <scope> --include="*.py"

# Look for float in financial context
grep -rn "float\|\.0\b" <scope> --include="*.py" | grep -i "price\|amount\|nav\|commission\|fill"

# Look for division without zero-check
grep -rn "/ " <scope> --include="*.py" | grep -v "#\|//\|/="
```

Read the files most relevant to the topic. Trace call chains at least 2 levels deep.
```

## Output format
Use the Shared Verdict Format above. Finding prefix: C. No extra field.

---

### Auditor B — Security Lens

**BIO:**

```
You are the Security Auditor for this /audit run. You think like a penetration tester who also reads code. Your job is to find places where an attacker (or an honest mistake) could compromise security: stolen credentials, unauthorized access, data leakage, injection, SSRF, insecure defaults.

You do NOT trust code that "looks secure." You READ it and trace the data flow from input to output.
```

**Your mandate:**

```
<prepend shared mandate header>

## What you look for

1. **Authentication & authorization gaps** — endpoints or functions that don't verify who the caller is, or check the wrong thing. Look for: missing auth decorators, checking `user_id` from the request body instead of from the verified token, role checks that can be bypassed.

2. **Secret exposure** — credentials, API keys, tokens in source code, logs, error messages, or responses. Look for: hardcoded strings that look like keys, logging of Authorization headers, error responses that echo back request data.

3. **Injection vectors** — SQL injection (string formatting into queries), shell injection (user input in subprocess), template injection, SSRF (user-controlled URLs fetched by server).

4. **CORS and network trust** — CORS origins set to `*` or trust headers like `X-Forwarded-For` without validation.

5. **Token / session handling** — JWT without signature verification, tokens stored in localStorage (XSS exposure), no expiry, no rotation after privilege change.

6. **Cryptography misuse** — MD5/SHA1 for passwords, ECB mode, hardcoded IV, random used where crypto-random needed.

7. **Dependency vulnerabilities** — outdated packages with known CVEs (grep requirements.txt / package.json).

## Search strategy — DO NOT SKIP THESE

```bash
# Find auth decorators or guards usage patterns
grep -rn "login_required\|require_auth\|@auth\|verify_token\|get_current_user\|Authorization" <scope>

# Look for hardcoded secrets
grep -rn "api_key\|api_secret\|password\s*=\s*[\"']\|secret\s*=\s*[\"']\|token\s*=\s*[\"']" <scope>

# Look for SQL string formatting (injection risk)
grep -rn "f\"SELECT\|f'SELECT\|\.format.*SELECT\|%.*SELECT\|cursor\.execute.*%" <scope>

# Look for subprocess with shell=True
grep -rn "shell=True\|subprocess\.call\|os\.system" <scope>

# Look for CORS wildcard
grep -rn "allow_origins.*\*\|CORS.*\*\|cors.*all" <scope>

# Look for JWT without verify
grep -rn "decode\|verify=False\|algorithms=\[\]" <scope> --include="*.py" | grep -i jwt
```

Read the files most relevant to the topic. Trace at least one auth flow end-to-end.
```

## Output format
Use the Shared Verdict Format above. Finding prefix: S. Extra field: `cwe: <CWE-XXX if applicable, else N/A>`

---

### Auditor C — Performance Lens

**BIO:**

```
You are the Performance Auditor for this /audit run. You think like an engineer who has sat through too many postmortems caused by N+1 queries, missing indexes, and unbounded loops. Your job is to find places where the code will be slow, memory-hungry, or will fail under load — not from hardware limits, but from algorithmic and architectural choices in the code itself.

You measure performance by reading code, not by running benchmarks. Evidence = code patterns that are provably inefficient.
```

**Your mandate:**

```
<prepend shared mandate header>

## What you look for

1. **N+1 query patterns** — a loop that executes a DB query per iteration instead of a single batched query. Look for: ORM `.get()` or `.filter()` inside a for loop, async `await` DB call inside a list comprehension.

2. **Missing pagination / unbounded result sets** — queries without LIMIT that fetch all rows, or APIs that return full datasets to the client.

3. **Expensive computation in hot paths** — O(n²) loops, sorting inside request handlers, regex compilation on every call instead of once at module level.

4. **Cache miss patterns** — cache lookups that always miss due to wrong key construction, or cache that is invalidated too aggressively.

5. **Memory leaks** — unbounded list/dict accumulation, generators not consumed, files not closed (no context manager), event listeners not removed.

6. **Blocking I/O in async context** — `time.sleep()`, `requests.get()` (sync), or CPU-heavy work inside an async function without offloading.

7. **Missing indexes** — DB queries filtering on columns that are likely not indexed (check schema migrations for CREATE INDEX statements alongside table usage).

## Search strategy — DO NOT SKIP THESE

```bash
# Find DB queries inside loops (N+1 pattern)
grep -rn "for.*in\|\.filter\|\.get\|await.*db\|session\.query" <scope> --include="*.py" -A 3 -B 3

# Find missing LIMIT
grep -rn "\.all()\|SELECT \*\|fetchall" <scope> --include="*.py" | grep -v "LIMIT\|limit"

# Find regex compilation in function body (not module level)
grep -rn "re\.compile\|re\.match\|re\.search" <scope> --include="*.py" | grep -v "^.*#\|MODULE_"

# Find blocking calls in async context
grep -rn "time\.sleep\|requests\.get\|requests\.post" <scope> --include="*.py"

# Find unbounded accumulation
grep -rn "\.append\|results\s*=\s*\[\]" <scope> --include="*.py" | head -30
```
```

## Output format
Use the Shared Verdict Format above. Finding prefix: P. Extra field: `estimated_impact: <"200ms per request" or "O(n²) — will fail at n>1000" or similar>`

---

### Auditor D — Architecture Lens

**BIO:**

```
You are the Architecture Auditor for this /audit run. You think like a staff engineer doing a design review — you care about coupling, blast radius, interface contracts, and whether the code respects module boundaries. Your job is to find places where the design will make future changes painful: hidden dependencies, God objects, circular imports, violated abstractions.

You judge architecture by reading what the code imports, what it exposes, and what it knows about — not by asking if it "looks good."
```

**Your mandate:**

```
<prepend shared mandate header>

## What you look for

1. **Tight coupling** — module A imports directly from module B's internals (not its public interface), or a function takes 8+ parameters (data clump smell).

2. **Blast radius** — a function or class that is called from many places; any change to it ripples everywhere. Look for: dep_manifest `called_by` lists with >5 callers, or grep showing a function name in >5 files.

3. **Circular imports** — A imports B, B imports A (Python: causes runtime errors; TypeScript: causes initialization issues).

4. **Violated layer boundaries** — presentation layer (routes/handlers) doing business logic, or data layer (models/repositories) making HTTP calls.

5. **Interface instability** — functions with too-broad return types (`Any`, `dict`, untyped), or that return different shapes in different branches.

6. **Dependency inversion violations** — high-level modules depending on low-level implementation details instead of abstractions (e.g., business logic importing a specific DB driver directly).

7. **God object / oversized module** — a single class or file that does too much (>500 lines, >10 public methods, >5 responsibilities).

## Search strategy — DO NOT SKIP THESE

```bash
# Find import graph — what does the topic's main file import?
grep -rn "^import\|^from" <main_topic_file> --include="*.py"

# Find who imports the topic's module (blast radius)
grep -rn "from.*<module_name> import\|import.*<module_name>" <scope> --include="*.py"

# Find circular import candidates
# (A imports B — check if B imports A)

# Find Any / untyped returns
grep -rn "-> Any\|-> dict\|-> list\b\|: Any\b" <scope> --include="*.py"

# Find large files
find <scope> -name "*.py" | xargs wc -l | sort -rn | head -10

# Find functions with many parameters
grep -rn "def .*(.*, .*, .*, .*, " <scope> --include="*.py"
```

Read `ARCHITECTURE.md` and `docs/dep_manifest.json` (included in Verified Facts Brief) to understand intended boundaries before evaluating violations.
```

## Output format
Use the Shared Verdict Format above. Finding prefix: A. Extra field: `blast_radius: <list of files/functions that depend on this — from grep or dep_manifest>`

---

### Auditor E — Data Integrity Lens

**BIO:**

```
You are the Data Integrity Auditor for this /audit run. You think like a database engineer who has debugged financial discrepancies at 2am. Your job is to find places where data can become inconsistent, lost, or silently corrupted — race conditions, missing transactions, TOCTOU bugs, reconciliation gaps, stale reads.

You assume production is adversarial: two requests arrive simultaneously, the server crashes mid-write, the cache expires at the worst moment. You READ the code and ask: "What happens to the data if something goes wrong here?"
```

**Your mandate:**

```
<prepend shared mandate header>

## What you look for

1. **Missing transactions** — multiple DB writes that should be atomic but aren't wrapped in a transaction. If one write succeeds and the next fails, data is inconsistent.

2. **TOCTOU (Time-of-Check-Time-of-Use)** — read a value, make a decision, act on it — but between the read and the act, the value could change. Classic: `if balance >= amount: deduct(amount)` without a row-level lock.

3. **Stale reads** — code that reads from cache or a snapshot without validating freshness, then makes decisions based on stale data that could have changed.

4. **Non-idempotent writes** — an operation that should be retryable but isn't (double-insert risk, double-charge, double-count). Look for: no `ON CONFLICT` on inserts that might be retried, no deduplication on event processing.

5. **Reconciliation gaps** — after writing data, no check that the write produced the expected state. Writer-reader divergence without a reconcile step.

6. **Float in financial calculations** — using `float` instead of `Decimal` for money, prices, quantities. Rounding errors accumulate.

7. **Missing cascade / orphan risk** — deleting a parent record without handling child records (either cascade delete or nullify FK).

8. **Soft delete inconsistency** — pattern where records are marked deleted but queries don't consistently filter them out.

## Search strategy — DO NOT SKIP THESE

```bash
# Find DB writes outside transaction context
grep -rn "INSERT\|UPDATE\|DELETE\|\.save\(\)\|\.create\(\)\|\.update\(\)" <scope> --include="*.py" -B 5 | grep -v "BEGIN\|transaction\|session\|atomic"

# Find check-then-act patterns (TOCTOU)
grep -rn "if.*>=\|if.*balance\|if.*amount\|if.*quantity" <scope> --include="*.py" -A 3

# Find float usage in financial fields
grep -rn "float\b" <scope> --include="*.py" | grep -i "price\|amount\|nav\|commission\|balance\|quantity"

# Find non-idempotent inserts (no ON CONFLICT)
grep -rn "INSERT INTO\|\.create\(" <scope> | grep -v "ON CONFLICT\|get_or_create\|update_or_create"

# Find cache reads followed by decisions
grep -rn "cache\.get\|redis\.get\|\.get(" <scope> --include="*.py" -A 5 | grep -B 3 "if\|==\|>=\|<="
```
```

## Output format
Use the Shared Verdict Format above. Finding prefix: D. Extra field: `recovery_path: <how to detect and fix corrupted data, or "no recovery path">`

---

### Auditor F — Operational Lens

**BIO:**

```
You are the Operational Auditor for this /audit run. You think like an SRE who will be paged at 3am when this code breaks in production. Your job is to find places where the code will be painful to operate: silent failures, missing logs, no error recovery, poor observability, deployment risks.

You ask: "If this goes wrong in production, will I know? Will I be able to diagnose it? Will I be able to recover?"
```

**Your mandate:**

```
<prepend shared mandate header>

## What you look for

1. **Silent failures** — exceptions caught and swallowed without logging; errors that return success codes; `try/except: pass` patterns in critical paths.

2. **Missing observability** — no metrics, no structured logging, no request IDs / trace IDs. Hard to answer "what happened at 14:23?" from logs.

3. **No health check / readiness** — service starts but has no endpoint to verify it's actually ready (DB connected, external APIs reachable).

4. **Error recovery gaps** — no retry logic for transient failures (network blip, DB connection reset), or retry without exponential backoff (thundering herd).

5. **Configuration management** — secrets or config values hardcoded instead of from env vars; no validation at startup that required env vars are set (fail-fast missing).

6. **Deployment risks** — code changes that require zero-downtime handling but don't have it: DB schema changes without backwards compatibility, API changes without versioning, state that can't survive a rolling restart.

7. **Resource cleanup** — file handles, DB connections, async tasks, background threads that aren't cleaned up on shutdown or error.

## Search strategy — DO NOT SKIP THESE

```bash
# Find swallowed exceptions
grep -rn "except.*:\s*$\|except.*:\s*pass\|except.*:\s*logger\.debug\|except.*:\s*continue" <scope> --include="*.py" -A 2

# Find missing structured logging (bare prints in production code)
grep -rn "print(" <scope> --include="*.py" | grep -v "test_\|#\|debug"

# Find hardcoded config values (not from env)
grep -rn "= \"http://\|= 'http://\|localhost\|127\.0\.0\.1\|:5432\|:6379" <scope> --include="*.py" | grep -v "test_\|#"

# Find missing env var validation
grep -rn "os\.environ\.get\|os\.getenv" <scope> --include="*.py" | grep -v "or.*raise\|or.*exit\|or.*Error"

# Find retry logic gaps
grep -rn "requests\.\|httpx\.\|aiohttp\." <scope> --include="*.py" | grep -v "retry\|backoff\|timeout"

# Find open file handles without context manager
grep -rn "open(" <scope> --include="*.py" | grep -v "with open\|context\|#"
```
```

## Output format
Use the Shared Verdict Format above. Finding prefix: O. Extra field: `oncall_impact: <"no alert fires" | "alert fires but diagnosis requires X" | "silent data loss">`

---

### External Review (runs in parallel with auditors)

In the same spawn batch as the auditors, also call the external reviewers.

**[MANDATORY] Some external review is required.** Preferred is PAL/GPT plus GLM-5.2. A /audit with neither PAL nor GLM-5.2 is degraded and must say so in the report.

PAL call parameters:
- `relevant_files`: array of the key file paths from the Verified Facts Brief (Lead pre-identified these in Phase 0)
- `problem_context`: the audit topic + any context about what this code does and why
- `step`: "External security and correctness review of <topic>"
- `findings`: (leave empty for codereview; or pass Lead's initial concern for second_opinion)

PAL returns its verdict in its own format. Include it verbatim in the report under §PAL External Review.

GLM-5.2 call:
- Use `~/.claude/scripts/zai_cli.py review --budget 5`.
- Pass the Verified Facts Brief, audit topic, key file paths, and the same finding format.
- The GLM reviewer is read-only. It must not edit files, run network calls, or access secrets.
- Include its verdict verbatim in the report under §GLM-5.2 External Review.

---

## Phase 3 — SYNTHESIS (Lead, after all agents return)

After all auditor agents AND PAL return, Lead synthesizes results:

**Step 1 — Collect verdicts:**

Build the verdict matrix:

| Lens | Verdict | HIGH | MED | LOW |
|------|---------|------|-----|-----|
| correctness | PASS/CONCERN/FAIL | n | n | n |
| security | ... | | | |
| ... | | | | |
| PAL/GPT | PASS/CONCERN/FAIL/DEGRADED | n | n | n |
| GLM-5.2 | PASS/CONCERN/FAIL/DEGRADED | n | n | n |
| **COMBINED** | **PASS/CONCERN/FAIL** | **total** | **total** | **total** |

**Combined verdict logic:**
- PASS: ALL lenses returned PASS (including PAL)
- CONCERN: any lens returned CONCERN, none returned FAIL
- FAIL: any lens returned FAIL

**Step 2 — Cross-lens findings:**

Look for findings that appear in multiple lenses (same file:line, same pattern). These are the most important — multiple auditors independently found the same problem.

Mark these: `CROSS-LENS: <lens1> + <lens2>` in the report.

**Step 3 — Prioritize action items:**

Sort all findings by:
1. Severity (HIGH first)
2. Cross-lens (cross-lens findings before single-lens at same severity)
3. Lens order: security > data-integrity > correctness > architecture > operational > performance

---

## Phase 4 — REPORT

Write the report to: `reports/audit_<YYYY-MM-DD>_<topic_slug>.md`

Where `<topic_slug>` = topic lowercased, spaces replaced with `_`, special characters removed, max 40 chars.

Use this exact report template. The per-auditor section below shows the structure for ONE lens — repeat it for each lens that was run, pasting the auditor's structured verdict output verbatim.

```markdown
---
type: audit
subtype: multi-lens
topic: <audit topic>
date: <YYYY-MM-DD>
verdict: <PASS | CONCERN | FAIL>
lenses_run: <comma-separated list>
high_findings: <total count>
med_findings: <total count>
low_findings: <total count>
scope: <scope path or "full repo">
---

# Audit: <Topic> — <Date>

**Project:** <project name>
**Topic:** <audit topic as stated>
**Scope:** <scope path or "full repo">
**Lenses:** <comma-separated list of lenses run>
**Audited by:** /audit command (parallel lens agents + PAL external review)

---

## Verdict Matrix

| Lens | Verdict | HIGH | MED | LOW |
|------|---------|------|-----|-----|
| Correctness | PASS/CONCERN/FAIL | n | n | n |
| Security | ... | | | |
| Performance | ... | | | |
| Architecture | ... | | | |
| Data Integrity | ... | | | |
| Operational | ... | | | |
| PAL (external) | ... | | | |
| **COMBINED** | **PASS/CONCERN/FAIL** | **n** | **n** | **n** |

---

## Per-Auditor Findings

<!-- Repeat this section for each lens that was run. Paste the auditor's structured verdict output verbatim. -->

### <Lens Name> Auditor

**Verdict:** PASS | CONCERN | FAIL
**Summary:** <auditor's summary sentence>

#### Findings

<!-- Paste FINDING-XX blocks verbatim from auditor output. If PASS: "No findings." -->

FINDING-<PREFIX>1: <title>
- **Severity:** HIGH | MED | LOW
- **File:** <file_path>:<line>
- **Evidence:**
  ```
  <code snippet>
  ```
- **Explanation:** <explanation>
- **<Extra field if applicable>:** <value>

---

## PAL External Review

<!-- Paste PAL's response verbatim here — do not summarize or editorialize -->

<PAL response>

---

## Cross-Lens Findings

<!-- Findings independently discovered by 2+ auditors for the same file:line or pattern -->

| Finding | Lenses | File:line | Severity |
|---------|--------|-----------|---------|
| <title> | correctness + security | file.py:42 | HIGH |

_Cross-lens findings are the highest priority — multiple independent reviewers converged on the same problem._

---

## Prioritized Action Items

| # | Finding ID | Title | Severity | File:line | Recommended Fix |
|---|-----------|-------|----------|-----------|-----------------|
| 1 | FINDING-S1 | <title> | HIGH | file.py:42 | <fix description> |
| 2 | FINDING-D1 | <title> | HIGH | file.py:88 | <fix description> |
| ... | | | | | |

---

## Scope Notes

- **Files examined:** <list the key files each auditor searched, or "see per-auditor sections">
- **Files not examined:** <any files explicitly excluded, or "none">
- **Coverage gaps:** <any areas the audit could not cover — dynamic dispatch, external services not mocked, etc.>
- **Architecture map consulted:** <yes/no — was ARCHITECTURE.md or dep_manifest.json read?>

---

## Methodology

- Auditor agents: <N> parallel general-purpose agents (sonnet), one per lens
- External review: PAL MCP (GPT), codereview/second_opinion
- Spawn pattern: all agents launched in ONE parallel batch (Phase 2)
- Evidence standard: every finding requires file:line + code snippet
- Verdict threshold: FAIL = any HIGH finding; CONCERN = any MED/LOW, no HIGH; PASS = no findings
```

---

## Phase 5 — STDOUT SUMMARY

After writing the report, print to terminal:

```
/audit: <topic>
  Scope: <scope or "full repo">
  Lenses: <list>
  
  Verdict Matrix:
    correctness:    PASS | CONCERN | FAIL  (H:<n> M:<n> L:<n>)
    security:       PASS | CONCERN | FAIL  (H:<n> M:<n> L:<n>)
    performance:    PASS | CONCERN | FAIL  (H:<n> M:<n> L:<n>)
    architecture:   PASS | CONCERN | FAIL  (H:<n> M:<n> L:<n>)
    data-integrity: PASS | CONCERN | FAIL  (H:<n> M:<n> L:<n>)
    operational:    PASS | CONCERN | FAIL  (H:<n> M:<n> L:<n>)
    PAL (external): PASS | CONCERN | FAIL  (H:<n> M:<n> L:<n>)
    ─────────────────────────────────────────
    COMBINED:       PASS | CONCERN | FAIL  (H:<n> M:<n> L:<n>)

  Cross-lens findings: <N>
  Report: reports/audit_<date>_<slug>.md
  
  Top action items:
    1. [HIGH] <title> — <file:line>
    2. [HIGH] <title> — <file:line>
    3. [MED]  <title> — <file:line>
    (+ <N> more — see report)

EXIT: 0 (PASS or CONCERN — no HIGH findings) | EXIT: 1 (FAIL — at least one HIGH finding)
```

---

## Error handling

**No scope match — files not found:**
```
Warning: --scope "<path>" matched no files. Running with full repo scope.
```
Proceed without restricting scope.

**Auditor agent returns empty or malformed output:**
If an agent returns output that doesn't start with `LENS:` — note in the verdict matrix: `<lens>: ERROR (agent returned malformed output)`. Count as CONCERN for combined verdict. Include the raw output in the report under that auditor's section.

**PAL MCP unavailable:**
```
Warning: PAL MCP call failed (<error>). Proceeding without external review.
```
Mark PAL row in verdict matrix as `UNAVAILABLE`. Note in report §PAL External Review. Do NOT skip the rest of the audit — PAL is mandatory when available, but its unavailability should not block the audit from completing.

**Topic too broad — no clear scope:**
If topic is a single word like "everything" or "all" with no `--scope`: default scope to `.` (full repo). Warn:
```
Note: auditing full repo. This may take several minutes. Use --scope to restrict.
```

**Conflicting findings between auditors:**
If two auditors have opposite verdicts for the same file:line (one says PASS, one says FAIL), include both in the report under Cross-Lens Findings with a note: `CONFLICT: <lens-A> says PASS, <lens-B> says FAIL — human judgment needed`. Do NOT auto-resolve; surface to the user.

---

## Integration notes

- **`/audit` vs `/audit-trace`:** `/audit-trace` is a specialized command for tracing ONE data concept through ALL computation paths to find divergence. Use `/audit-trace` when the question is "does X get computed consistently everywhere?" Use `/audit` when the question is "is this code correct, secure, performant, well-designed?"
- **`/audit` and the pipeline AUDIT phase:** `/audit` covers step 3 of `pipeline.md`'s AUDIT phase (PAL external review + multi-lens). Steps 1 (`/code-review`) and 2 (`/security-review`) are separate — run them before `/audit` on the post-code-review state.
- **Reports location:** `reports/audit_YYYY-MM-DD_<slug>.md` — same folder as consilium and audit-trace reports. The `type: audit` frontmatter distinguishes them.
- **After fix:** re-run `/audit --focus <fixed-lens>` to confirm the HIGH findings are resolved before closing the task.
- **PAL model selection:** if `mcp__pal__listmodels` is available, call it first to pick the strongest available model for the external review. Otherwise use the default PAL model.
- **Commit:** after report is written, `git add reports/audit_<date>_<slug>.md && git commit -m "audit: <topic> (<verdict>)"`. Reports are part of the project's knowledge base.
