---
description: "Paired Worker+Verifier protocol for evidence-first delegated code work. Loads when planning Agent spawns, code edits, or paired verification."
paths:
  - "**/*.py"
  - "**/*.ts"
  - "**/*.tsx"
  - "**/*.jsx"
  - "**/*.go"
  - "**/*.rs"
  - "**/*.java"
  - "**/*.sql"
  - "**/Dockerfile"
---

# Paired Verification — Lead spawns Worker AND Verifier as a pair

Dmitry's protocol change: когда Lead делегирует содержательную работу, второй агент проверяет её **независимо** — параллельно или последовательно, но с собственным контекстом. До финального deploy gate он не строит синтетическую приёмку: он выполняет или задаёт прямые read-only probes против реального source of truth и возвращает evidence receipt. Lead не оценивает результат своим суждением; он читает выход прямых probes и их exit code. Durable regression tests создаются только в final deploy gate после стабильного PASS и не заменяют реальные evidence.

## Why this rule exists

Эмпирически и теоретически, **single-agent ≥ multi-agent при равных compute** (arxiv 2604.02460, Anthropic engineering, Cognition). Когда Lead всё-таки делегирует, проигрыш приходит из трёх источников:

1. **Information loss at handoff** — Data Processing Inequality: ответ Worker'а информационно ограничен брифом, summary Lead'а ограничен ответом Worker'а. Каждый hop через границу агента — лосси кодек.
2. **Self-evaluation bias** — Anthropic явно: *"agents tend to confidently praise even mediocre work"*. Lead, оценивающий Worker'а, — это та же модель, которая написала бриф; она склонна видеть результат как соответствующий собственному намерению.
3. **Lead's context decay during the wait** — пока Worker работает, окно Lead'а смещается, acceptance criteria вытесняются свежими tool-results. К моменту возврата Worker'а у Lead'а уже размыто «что значит done».

Контра-мера: **evidence receipt — независимый, воспроизводимый набор прямых запросов к source of truth**, заданный другим агентом на другом контексте и запущенный Lead машинно. Примеры: `curl --fail`/HTTP status + body, SQL `SELECT`, CLI query, DevTools network/state inspection, source counts/samples и boolean invariants. Синтетический test artifact не является заменой таких доказательств.

## When this rule applies

- Любой `Agent` spawn, который **производит** артефакт (код, конфиг, данные, миграция, патч).
- Любой `Agent` spawn, чей выход Lead иначе бы «прочитал и одобрил».
- НЕ применяется к чисто read-only recon (Explore listing файлов, grep, summarize) — там нет артефакта против контракта.
- НЕ применяется к тривиальным механическим правкам, которые Lead делает сам без агента (опечатка, переименование, единичная конфиг-строка) — Three Nos применяется через body guards, не парный спавн.

См. §"When you can skip the pair" ниже — skip определён **отрицательно** (через перечень impact-классов, при которых skip запрещён), а не через «маленькая задача».

## RECON — mandatory architecture reading

Before writing any Artifact Contract, Lead **MUST** read `ARCHITECTURE.md` and `docs/dep_manifest.json` (if they exist in the project):
- Consult the dependency table to populate the `Affected downstream:` field
- If the function being changed is listed as `critical: true` in dep_manifest.json, include its `feeds` array in the Artifact Contract
- If the project has no architecture docs: note it in the handover as a gap; do not block work
- Populate `Architecture constraints:` in the Artifact Contract from `feeds` arrays of touched components (components whose `file` matches the planned edit target)
- Populate `Downstream consumers:` from `called_by` arrays of the same components — these are the functions/endpoints the Verifier MUST probe directly
- If `critical: true` OR `feeds` array has ≥3 entries for any touched component → mark for conditional Architecture Auditor (see §Architecture-aware verification below)
- **[CRITICAL] Code-over-docs**: dep_manifest.json reflects state at last update, not necessarily NOW. Before populating `Architecture constraints:` and `Downstream consumers:`, Lead MUST cross-check manifest entries against actual code (grep for function, check if it's still called, verify writer is still active). When manifest says "X writes to Y" but code shows X is disabled → manifest is stale, not code is wrong. Update manifest first, then populate Artifact Contract from corrected manifest. Finding type for divergence: `architecture-docs-stale`.

## Pattern A — Семёрка: Flow Designer → Prototype Gate → Worker + Verifier

Четыре стадии, две из которых параллельны:

**Стадия 1 — Flow Designer (sequential, до Worker+Verifier):**
Lead спавнит Flow Designer agent с Verified Facts Brief из RECON. Flow Designer анализирует задачу и производит Process Flow Document (PFD): temporal topology, branching scenarios, state dependencies, failure modes, worker directives, verifier assertions, `prototype_plan`, and `role_handoff_contract`.

**Стадия 2 — Prototype Gate (sequential, после Flow Designer/Challenge, до Worker):**
Lead спавнит Prototyper agent для read-only executable proof. Prototyper не пишет production code и не мутирует prod/state-owned данные. For every non-trivial behavioral, data, runtime, external-system, incident-driven, or `critical: true` Prototype Gate, it MUST create a durable notebook only in `notebooks/` or `reports/prototypes/` (never a tempdir). Each evidence-bearing cell records the authorized operation class and allowlist decision, direct command/query, source identity and environment, ISO timestamp or observation window, baseline/source snapshot binding (exact query SHA-256, result SHA-256, and raw-output SHA-256), filters/parameters, counts/samples, expected versus actual, invariant result, and bounded raw output. Large raw output is a durable repo-relative artifact referenced by SHA-256. A paired probe script is not required. Prototyper запускает read-only сравнение source-of-truth входов с текущим кодом/DB поведением, находит first divergence, counts/samples, and boolean invariants. Notebook N/A is allowed only when the entire Prototype Gate is explicitly `N/A` for a pure docs, format, or static-config task with no executable data/runtime hypothesis; it MUST state the concrete reason.

**Стадия 3 — Worker + Verifier (parallel, после Prototype Gate):**
В одном сообщении — два `Agent` tool-call'а:
1. **Worker agent** получает: goal + Verified Facts Brief + PFD + Prototype Handoff + scope + Artifact Contract (enriched with PFD worker_directives, failure_modes, and proven prototype facts). Делает работу, кладёт артефакт в указанный путь, возвращает ссылку на него.
2. **Verifier agent** получает: тот же goal + ту же Verified Facts Brief + PFD (direct-probe assertions, invariants, branching_scenarios) + Prototype Handoff probe requirements + тот же scope + тот же Artifact Contract. **Не видит prompt'а Worker'а.** Выполняет или специфицирует прямые read-only probes и производит evidence receipt.

Оба Worker и Verifier бегут конкурентно. Lead дожидается обоих, затем запускает согласованные прямые probes.

For critical components (see §Architecture-aware verification): Lead MAY spawn a third parallel agent — the **Architecture Auditor** — in the same message as Worker+Verifier. All three run concurrently. Lead requires independent direct evidence from both verification lanes before the final deploy gate.

## Pattern B — последовательная пара

Используется только когда контракт нельзя выразить, не увидев форму артефакта (редко). Worker → Verifier → Lead запускает прямые probes. Verifier всё равно работает в свежем контексте и не видит prompt'а Worker'а; он производит evidence receipt, не суждение.

## Artifact Contract — общий и обязательный

Каждый бриф (Worker-у И Verifier-у) включает один и тот же Artifact Contract:

```
Objective: <одно предложение, что система должна делать>
Verified Facts Brief: <что существует сейчас, с file paths и evidence>
Artifact path: <куда Worker положит результат>
Invocation: <как запускается / импортируется>
Inputs: <формы и типы входных данных>
Expected observable behavior: <что внешний наблюдатель должен увидеть>
Out of scope: <что НЕ менять, какие интерфейсы НЕ ломать>
Environment constraints: <зависимости, версии, доступные ресурсы>
Acceptance emphasis: <что обязательно проверить; что не предполагать>
Affected downstream: <functions/APIs/screens that consume this artifact's output — consult dep_manifest.json>
Architecture map consulted: <yes/no — was ARCHITECTURE.md or dep_manifest.json read before writing this contract?>
Architecture constraints: <interfaces Worker MUST NOT break — populated from dep_manifest.json `feeds` arrays of touched components; "(no dep_manifest.json)" if manifest absent>
Downstream consumers: <specific functions/endpoints Verifier MUST probe directly — from dep_manifest.json `called_by` arrays; "(none)" if manifest absent or called_by empty>
Process Flow Document: <path to PFD YAML or inline PFD — MANDATORY for all delegations>
Prototype Gate: <PASS with Prototype Handoff path and mandatory notebook | N/A with concrete reason; N/A only for a pure docs/format/static-config task with no executable data/runtime hypothesis>
Prototype Handoff: <path or inline summary: baseline/source snapshot binding, source-of-truth inputs, current-system comparison, first divergence, counts/samples, invariants proven, Worker facts, Verifier direct-probe requirements>
Session context: <OPTIONAL — see §Session context injection below>
```

Без Artifact Contract пара не спавнится — это предусловие. Если Lead не может его сформулировать, задача недостаточно понята для делегирования (Adzic «definition of ready»).

## Verifier — что видит / не видит (явно)

### Verifier МОЖЕТ видеть
- Objective и Verified Facts Brief.
- Artifact Contract (полностью).
- Существующий публичный интерфейс: имена функций, CLI shape, endpoint paths, config keys, output paths, DB schema, expected artifact location.
- Файлы или сниппеты, нужные для понимания ожидаемого поведения.

### Verifier НЕ ДОЛЖЕН видеть
- Worker's prompt.
- Worker's plan.
- Worker's reasoning / draft / черновики.
- Lead-комментарии, намекающие на выбранную стратегию реализации.
- «Likely solution» заметки, кроме случаев когда это реальные продуктовые ограничения.

Принцип: **Verifier тестирует наблюдаемое поведение артефакта, а не выбранный Worker-ом метод реализации.**

## Role handoff standard — no lossy summaries between roles

Every role passes a concrete artifact, not a "looks like" conclusion. This is
the standard interface between roles:

| From | To | Required payload | Forbidden payload |
|---|---|---|---|
| Lead | Flow Designer | Artifact Contract + Context Receipt + Verified Facts Brief with code/data evidence | Unchecked memory/report claims |
| Flow Designer | Challenge | Full PFD including `prototype_plan` and `role_handoff_contract` | Implementation code |
| Challenge | Prototyper | Additive PFD changes and explicit prototype checks | Deleted/overridden PFD requirements |
| Prototyper | Worker | Prototype Handoff: verdict, baseline/source snapshot hashes, artifacts, source/current counts, first divergence, invariants, Worker facts | Guesswork, write queries, prod mutations |
| Prototyper | Verifier | Baseline-derived direct-probe requirements, source snapshot hashes, and invariants | Worker's implementation approach or a future candidate identity |
| Worker | Verifier | Artifact path only; Verifier still uses AC/PFD/prototype evidence | Worker's prompt, plan, reasoning, or draft code |
| Worker/Verifier | Diff Reviewer | Git diff + AC + PFD + Prototype Handoff + evidence receipt | Permission to edit code |

If a role cannot produce its required payload, the pipeline pauses before the
next role. The correct response is to refine the contract/probe, not to let the
Worker guess.

## Prototype Gate — executable truth before code

The Prototype Gate requires a durable notebook in `notebooks/` or
`reports/prototypes/` for every non-trivial behavioral, data, runtime,
external-system, incident-driven, or `critical: true` task. The notebook is the
investigation journal, not a synthetic test stand. Each evidence-bearing cell
records the operation class, allowlist decision, direct authorized read-only
command/query, source/environment identity, ISO timestamp or observation
window, baseline/source snapshot binding (exact query SHA-256, result SHA-256,
and raw-output SHA-256), filters/parameters, counts/samples, expected versus
actual, invariant result, and bounded raw output. Large raw output must be
stored as a durable repo-relative artifact and referenced with its SHA-256. The
handoff must include the underlying direct command output or query result;
notebook-only claims are too weak for review and replay. Before the Worker
exists, the notebook MUST NOT fabricate or name a future candidate identity,
artifact/tree-diff hash, or process/build/deployment/version identity.
Do **not** require a paired probe script merely to make the journal look
executable. Notebook N/A is permitted only when the entire Prototype Gate is
explicitly `N/A` for a pure docs, format, or static-config task with no
executable data/runtime hypothesis, and the handoff states the concrete reason.

Required Prototype Handoff fields:

```
Prototype verdict: PASS | FAIL | N/A
Notebook:
Artifacts:
Baseline/source snapshot binding: source/environment identity; exact query SHA-256; result SHA-256; raw-output SHA-256; ISO timestamp or observation window
Source-of-truth inputs:
Current-system comparison:
First divergence:
Counts and samples:
Invariants proven:
Worker handoff:
Verifier direct-probe requirements:
```

The Prototype Handoff MUST NOT fabricate or name a future candidate identity,
artifact/tree-diff hash, or process/build/deployment/version identity. Those
candidate-binding requirements begin only after the Worker artifact exists, in
the Verifier Evidence Receipt and Lead's candidate-verifying probes.

Prototype PASS means the probe has identified what the source of truth says,
what the current system does, where they first diverge, and what invariant the
Worker must preserve. Prototype FAIL means no Worker spawn; code written from an
unproven data hypothesis is a Three Nos violation.

## Verifier mandate (точная формулировка для prompt'а)

> «Выполни или специфицируй прямые read-only probes против реального source of truth и верни evidence receipt. Используй curl/HTTP, SQL SELECT, CLI commands, DevTools, counts/samples и boolean invariants там, где они соответствуют контракту. Каждый probe должен иметь команду/query, timestamp, source identity, ожидаемый и фактический результат, stdout/stderr или rows, и exit code. Тестируй **наблюдаемое поведение**, не приватные детали реализации. Не реализуй задачу и не создавай/не переписывай test files, fixtures, mocks, synthetic datasets, verification stands или harnesses во время implementation iterations. Если acceptance criteria неоднозначны — **fail closed**: верни отчёт об неоднозначности вместо изобретения продуктовых решений.»

When the Artifact Contract contains a non-empty `Downstream consumers:` field, the Verifier MUST directly probe at least one listed downstream consumer — verifying that it still receives correct input or produces correct output after the Worker's change. This is the architecture protection layer: dep_guard.py auto-skips for Worker subagents by design (the hook targets Lead only), so downstream integrity is enforced through direct evidence, not through a synthetic test.

## Evidence Receipt Standard

Evidence receipt должен соответствовать всем пунктам:
- Проверяет **наблюдаемое поведение** через реальный source of truth, не приватные детали реализации (если деталь не в Artifact Contract явно).
- Для каждого claim указывает command/query, operation class, allowlist decision, timestamp, source identity, candidate binding (artifact/tree-diff SHA-256 and applicable process/build/deployment/version identity), filters, expected/actual result, samples/counts, invariant result, bounded raw output or durable repo-relative raw-output artifact with SHA-256, где применимо.
- Минимизирует допущения, которых нет в Objective / Verified Facts Brief / Artifact Contract.
- **Детерминирован** или явно описывает изменчивость источника и допустимое окно.
- Печатает осмысленный diagnostic output при failure и сохраняет exit code каждого executable probe.
- Read-only: не меняет production state и не создаёт synthetic validation artifacts. Unknown operation or an operation whose read-only status cannot be proved is `UNAVAILABLE`, never PASS.

### Fail-closed authorized read-only operations

- HTTP: `GET` or `HEAD` only.
- SQL: only through a DB-enforced read-only role or transaction, and only a
  `SELECT`, non-mutating `WITH`, or `EXPLAIN`; reject `CALL`, DDL/DML, mutating
  CTEs, side-effecting functions, and `COPY PROGRAM`.
- CLI: only documented `get`, `list`, `show`, `status`, `describe`, `logs`,
  `diff`, or an explicitly known equivalent read verb.
- Filesystem: reads only. Shell redirection or a pipe into a mutator is forbidden.

The notebook and every Evidence Receipt row MUST name the operation class and
its allowlist decision. If authorization cannot be established, do not execute;
record the required observation as unavailable.

### Allowed forms
- `curl --fail` against a real endpoint with HTTP status/body evidence.
- `sqlite3` / `psql` **SELECT** with rowcount, samples, or a concrete value.
- Read-only CLI commands against the authoritative service or dataset.
- DevTools network, DOM, state, or performance inspection when the browser is the relevant observer.
- File/source queries with counts, samples, and explicit invariant evaluation.
- Existing tests may be run unchanged as supplementary baseline evidence; they
  do not prove an intermediate candidate unless the observation binds to that
  exact artifact/tree diff and applicable runtime identity.

### Forbidden forms during implementation iterations
- ❌ Прозаический checklist «проверьте что X, Y, Z» без commands/queries and evidence.
- ❌ «Look at the output and decide» — требует LLM-суждения.
- ❌ Тест, который вызывает Claude/LLM как judge.
- ❌ `curl -s` без `--fail` / `|| true` (см. verify_gate fake-evidence patterns).
- ❌ `localhost` / `127.0.0.1` как target в проде-сценарии.
- ❌ Создание или переписывание test files, fixtures, mocks, synthetic datasets, verification stands, or harnesses merely to validate the candidate.
- ❌ Изменение existing tests to make an intermediate candidate pass.
- ❌ Verifier изобретает конкретные продуктовые решения (точные exit codes, sort order, key names) когда Artifact Contract их не специфицирует — это создаёт скрытую спеку, которую Worker не видел.

## Lead's role after the pair returns

1. Worker возвращает → запомнить путь к артефакту.
2. Verifier возвращает → запомнить evidence receipt и direct-probe commands/queries.
3. Only after the Worker artifact exists, Lead запускает candidate-verifying direct probes. The probe must be newer than the last relevant edit and record artifact/tree-diff SHA-256 plus applicable process/build/deployment/version identity. Old/current production observations are baseline only unless their deployed build identity matches the candidate. Record command/query, operation class, allowlist decision, timestamp, source identity, samples/counts, invariant result, raw output, and exit code.
4. **PASS (all required probes exit 0):** идём к diff review и final deploy gate; durable tests ещё не создаются.
5. **FAIL (exit ≠ 0):** не правим inline. Сначала **классифицируем failure** (см. ниже), потом действуем.

**Lead не читает код Worker'а чтобы вынести вердикт.** Единственный вход для iteration PASS/FAIL — exit code прямого probe + его factual output. stdout/rows читаем для роутинга remediation, не для override'а вердикта.

## Final deploy gate — frozen durable regression tests only after evidence

Only after all required direct probes pass, the candidate remains stable through
diff review, and deployment is the next action, the Lead may create or update
durable regression tests. These tests encode the already-proven behavior and
then the Lead runs the dispatcher’s frozen full existing suite. Development selection is advisory only: final release coverage includes every required registry job/platform for the exact candidate. They are a regression memory, not a
replacement for source-of-truth evidence.

- Before this gate, neither Worker nor Verifier creates or rewrites tests,
  fixtures, mocks, synthetic datasets, verification stands, or harnesses merely
  to validate a candidate.
- Derive a final regression manifest from every proven evidence requirement and
  every required unavailable branch. Existing coverage satisfies an item only
  with its named test path and SHA-256. Any unavailable CRITICAL/HIGH required
  observation must map to durable regression coverage or deployment is blocked.
- Freeze this manifest and every named test-file SHA-256 before the first full
  suite run. Once the suite begins, tests cannot be edited in this `/go` or
  `/hackathon` run. A test-contract defect blocks and requires a separate scoped
  run; code/environment fixes may rerun the same frozen tests.
- A suite failure may reopen code/environment only with the tests frozen; any
  test-contract defect blocks and requires a separate scoped run. It never
  invalidates or silently substitutes the earlier evidence receipt.
- Final PASS requires both: direct-probe evidence receipt with all required
  probes passing, and the full existing suite (including durable tests) exiting
  0. The final verdict records both exit codes.

## Post-VERIFY architecture update

After PASS (exit 0) and before commit, Lead checks: did this change modify any interface listed in the ARCHITECTURE.md dependency table or dep_manifest.json?

- **If YES:** spawn a background agent (`run_in_background: true`, `model: "haiku"`) that:
  1. Reads current ARCHITECTURE.md and dep_manifest.json
  2. Reads `git diff` of changes made in this session
  3. Updates the dependency table rows and dep_manifest.json entries for affected components
  4. Adds a row to the Update Log in ARCHITECTURE.md with date + commit description
  5. Bumps the `updated` date in dep_manifest.json
  This agent runs in background — Lead proceeds with commit and next steps without waiting.

- **If NO:** skip (most bug fixes don't change interfaces; skip is logged in handover)

- This is NOT a Worker+Verifier pair — it's a mechanical doc update (skip per §"When you can skip the pair": zero behavior impact, deterministic content)

## Architecture-aware verification — distributed protection design

Architecture protection is **distributed across the pair**, not concentrated in a separate agent or hook:

| Phase | Actor | Architecture role |
|---|---|---|
| **RECON** | Lead | Reads dep_manifest.json → populates `Architecture constraints:` and `Downstream consumers:` in Artifact Contract |
| **During edit** | Worker | Sees `Architecture constraints:` → knows which interfaces to preserve |
| **During edit** | Verifier | Sees `Downstream consumers:` → MUST directly probe at least one downstream consumer |
| **Post-VERIFY** | Background Haiku | Updates ARCHITECTURE.md + dep_manifest.json to reflect what changed |

### Code = ground truth (code-over-docs principle)

dep_manifest.json and ARCHITECTURE.md are **navigation aids**, not specifications. They describe what existed when last updated — functions may have been disabled, new paths added, writers refactored to read-only, without updating the manifest. Real example: `auto_fix_discrepancies_j2t` listed as active writer in manifest, but actually disabled in dispatch; new `j2t_post_apply_reconcile` function exists in code but not in manifest.

**Rules:**
- When audit/verification finds divergence between dep_manifest.json and code → finding type = **"architecture-docs-stale"**, NOT "code-is-wrong"
- Worker and Auditor must NEVER "fix" code to match stale docs — the opposite direction: update docs to match code
- Lead's RECON cross-checks manifest entries against actual code before populating `Architecture constraints:` (see §RECON above)
- Architecture Auditor traces code paths, using dep_manifest as starting hints — if manifest says "A feeds B" but code shows A is dead, Auditor skips that edge and flags the manifest entry as stale

**Anti-pattern (запрещён):** audit reads stale manifest → proposes "fix" that re-enables a disabled writer → rolls back a real improvement. This is the imbalance loop: architecture docs lag behind code, and trusting docs over code creates regressive changes.

### dep_guard auto-skip for Workers (by design)

`dep_guard.py` auto-skips for subagent context (`is_subagent_context()` → allow). This is intentional: the Worker operates within an Artifact Contract that already carries architecture constraints. Blocking the Worker via dep_guard would prevent it from doing its job. The protection flows through:
1. Lead's RECON (reads dep_manifest.json, populates constraints)
2. Worker's brief (sees what not to break)
3. Verifier's direct probe (checks downstream consumers)
4. Post-verify update (updates docs to reflect new reality)

### Conditional Architecture Auditor (critical components)

When dep_manifest.json shows `critical: true` **OR** `feeds` array has **≥3 entries** for any component touched by the planned edit:

1. Lead spawns a **third parallel agent** alongside Worker+Verifier: the **Architecture Auditor**
2. Architecture Auditor receives: Artifact Contract + full dep_manifest.json + ARCHITECTURE.md (if exists)
3. Architecture Auditor produces an independent direct-probe plan/receipt that verifies downstream consumers listed in `feeds`/`called_by` still work correctly
4. Lead runs **both** Verifier and Auditor direct probes; all required probes must exit 0
5. Failure classification applies independently to each evidence lane (W/V/A/E)

For non-critical components (the 80–90% case): the enriched pair is sufficient. The conditional Auditor is a safety net for high-connectivity nodes in the dependency graph.

The Architecture Auditor is NOT a Verifier — it does not probe the Worker's artifact against the Artifact Contract. It checks that the **surrounding system** still works after the change. The Verifier probes the artifact; the Auditor probes the environment.

## Failure classification — обязательно перед реакцией на FAIL

Lead классифицирует non-zero exit ровно в одну из четырёх категорий:

| Категория | Признак | Реакция |
|---|---|---|
| **W. Artifact wrong** | Artifact существует, ведёт себя не так, как требует Artifact Contract | Спавнить нового Worker'а с narrowed scope, передать failed probe + factual output. Existing tests НЕ менять. |
| **V. Probe invalid / over-constrained** | Probe checks a private detail absent from the Contract, or its source/query cannot establish the stated claim | Спавнить нового Verifier'а (fresh context), require a revised **direct probe plan** against the original Contract. Worker и existing tests НЕ менять. Lead не отменяет verification — only replaces an invalid probe, not a test artifact. |
| **A. Contract ambiguous** | Verifier явно вернул «ambiguous», или оба (Worker+Verifier) интерпретировали по-разному | Lead уточняет Artifact Contract, обновляет Verified Facts Brief, перезапускает пару (Pattern A). |
| **E. Environment** | Probe cannot run because of dependencies, access, version, or network | Fix environment/access, then rerun the same direct probe. |

**Важно:** ни в одной из четырёх категорий Lead не выносит PASS «по чтению кода». Категория V — единственный путь признать probe невалидным, и она требует **revised direct probe**, не synthetic test rewrite и не override.

Hard cap на retries: 3 (см. pipeline.md §Failure recovery). После 3 неудач — возврат пользователю с aggregated failure + recommended next action.

## When you can skip the pair

Skip разрешён **только** когда Lead может назвать конкретную причину, почему executable acceptance property не существует помимо прямого осмотра, **И** задача не имеет ни одного из impact-классов ниже.

### Skip ЗАПРЕЩЁН для
- Любого изменения поведения.
- Любого bug fix'а.
- Любого изменения auth / security / permissions.
- Любой data migration / schema change.
- Любого изменения concurrency / caching / error-handling.
- Любого изменения test-infrastructure (может маскировать failures).
- Любого production config / deployment изменения.
- Любого edit'а, затрагивающего несколько файлов где взаимодействие важно.
- Любого изменения после prior verification failures.
- Любой задачи, мотивированной audit / incident / handover failure'ом.

### Skip разрешён для
- Read-only investigation без изменения артефакта.
- Formatting-only edit'ов (детерминированный formatter — `ruff format`, `prettier`).
- Rename / comment / doc typo с zero behavior impact.
- Mechanical search/replace при scope малом, замене однозначной, **семантика поведения не меняется**.
- Throwaway / debug / one-off скриптов в `/tmp`.

«Small» и «obvious» **сами по себе не достаточны** для skip'а. Нужна явная причина, почему нет executable property для проверки. Если skip применён — **факт пропуска фиксируется в handover'е** одной строкой («skipped paired-verification because <reason>»), чтобы при будущем поиске «куда делась проверка» след был.

## Session context injection

Agents have no memory of the Lead's conversation. When the task depends on session history — prior decisions, failed attempts, discussed approaches — Lead injects session context into the Artifact Contract via the `Session context:` field.

### Tool

`python3 ~/.claude/scripts/session_context.py` — extracts readable conversation from the current session JSONL. Preserves code edits (Edit/Write diffs), Bash commands + results, and all dialogue. Strips hook noise, permission modes, file-history snapshots.

### When to include (decision rule)

Include session context when **any** of these is true:

| Trigger | Whose context | Invocation to use |
|---|---|---|
| **Retry/fix** — re-spawning after Worker failed | **Failed agent's** | `--agent "<Worker desc>" --tail 20 --no-thinking` |
| **Debug chain** — 2+ prior attempts at same problem | **Failed agent's** | `--agent "<prev Worker>" --grep "<symptom>" --no-thinking` |
| **Back-reference** — "как обсуждали" / "continue" | **Lead's** | `--tail 15 --no-thinking` |
| **Decision context** — *why* a choice was made | **Lead's** | `--grep "<topic>" --no-thinking` |
| **Self-audit** — reviewing session's code changes | **Lead's** | `--tools-only --grep "Edit\|Write" --no-thinking` |
| **List who did what** — orientation before retry | **Lead's** | `--subagents` |

**The critical distinction:** on retry, the new Worker needs the **failed agent's** session, not Lead's. The failed agent saw stack traces, tried approaches, hit edge cases — Lead only saw the summary. Lead's session is for discussion context (decisions, back-references); agent sessions are for execution context (what was tried, what broke).

**When NOT to include:** task is fully described by Artifact Contract + files on disk. Most first-attempt Worker+Verifier pairs fall here — the Contract is self-contained by design.

### Subagent discovery

Each Lead session stores subagent JSONLs in `<session-id>/subagents/agent-*.jsonl` with `.meta.json` files containing `agentType` and `description`. The tool supports:

```bash
# List all agents of a session (who ran, when, how big)
python3 ~/.claude/scripts/session_context.py --subagents

# Read specific agent by description keyword (picks most recent match)
python3 ~/.claude/scripts/session_context.py --agent "Worker: fix rebuilder" --tail 20 --no-thinking

# Read specific agent by ID prefix
python3 ~/.claude/scripts/session_context.py --agent "a3a5d27d" --no-thinking
```

**Note:** the tool auto-detects the project dir from CWD. If the agent runs in a worktree or different directory, pass `--project-dir ~/.claude/projects/<project-hash>` explicitly.

### How to write the field

For Lead context:
```
Session context: before starting, run:
  python3 ~/.claude/scripts/session_context.py --tail 15 --no-thinking
  Focus on: <what the agent should look for>
```

For failed agent context (retry):
```
Session context: the previous Worker failed. Read its session:
  python3 ~/.claude/scripts/session_context.py --agent "<Worker description>" --no-thinking
  Focus on: what it tried, where it got stuck, and any error messages.
  Do NOT repeat the same approach — find a different path.
```

The `Focus on:` directive is mandatory when including session context — without it the agent reads N turns and doesn't know what matters. Exception: `--tools-only` mode for self-audit, where the focus is implicit (all edits).

### What the Verifier sees

Session context goes to **Worker only**. Verifier tests observable behavior per Artifact Contract; session history is implementation context, not acceptance criteria. If a session decision changes *what* the artifact should do (not *how*), promote that decision into the Artifact Contract's `Objective` or `Expected observable behavior` fields instead.

## Temporal & Process Evidence

Когда Artifact Contract ссылается на Process Flow Document (PFD), Verifier получает дополнительный источник direct-probe specification. PFD — это YAML-артефакт от Flow Designer, содержащий формализованные сценарии поведения системы во времени. Наличие PFD **расширяет** standard Verifier mandate, но не отменяет его: Evidence Receipt Standard и принцип «наблюдаемое поведение, не implementation details» по-прежнему действуют.

### Когда применяется

PFD-верификация активна когда Artifact Contract содержит:
- Поле `Process Flow Document:` с путём к YAML-файлу, ИЛИ
- Блок `verifier_assertions:` инлайн в контракте.

Если ни того, ни другого нет — этот раздел не применяется, работает обычный Verifier flow.

### Что Verifier видит из PFD

| PFD-секция | Доступна Verifier'у? | Как используется |
|---|---|---|
| `verifier_assertions` | ✅ Да | Прямой источник direct-probe requirements — каждый assertion = evidence row |
| `invariants` | ✅ Да | Postconditions, которые проверяются ПОСЛЕ каждого реального наблюдения |
| `branching_scenarios` | ✅ Да | Список условий и outcomes для source-of-truth probes; не повод строить mock harness |
| `timeline` | ✅ Да | Reference для ordering guarantees и expected state transitions |
| `state_variables` | ✅ Да | Dependency graph для cascade verification |
| `worker_directives` | ❌ Нет | Implementation guidance для Worker'а; Verifier не должен знать *как* реализовано |
| `failure_modes` | ✅ Да | Expected graceful degradation — каждый failure mode получает direct probe или explicit unavailable evidence |

### Temporal evidence patterns

Время — самый частый источник недетерминизма. PFD probes должны быть repeatable, поэтому фиксируют timestamp, source identity и допустимое окно freshness:

**Freshness / staleness:** query the authoritative source and the consuming
system at recorded timestamps; capture identifiers, observed timestamps, counts,
and whether the PFD freshness invariant holds.

**Ordering guarantees:** query event/order identifiers from the authoritative
log or API, then compare the current system's observed order and idempotency
state. Record the exact filters, representative rows, and boolean result.

### Branch evidence

Каждый `branching_scenarios` entry из PFD превращается в direct probe or an
explicitly recorded unavailable source-of-truth observation:

| Сценарий PFD | Direct probe | Evidence |
|---|---|---|
| Partial success | Query authoritative partial result and current processed state | Source/current counts, error record, consistency invariant |
| Timeout path | Inspect real timeout/deadline telemetry or a documented controlled environment | Cleanup/rollback state and timestamped output |
| Reject/deny path | Use an authorized real rejection response or immutable audit record | No side-effect count and pre/post invariant |
| Retry exhaustion | Query an actual exhausted retry record or state transition | Final state and non-hanging evidence |

Do not manufacture mocks, synthetic data, fixtures, or a harness merely to
cover a branch during implementation. If no lawful read-only observation exists,
record that limitation and route it to the final deploy test design instead.

### Cascade verification

Когда PFD `state_variables` описывает зависимости (derived variables):

```
state_variables:
  price: {source: market_feed}
  position_value: {derived_from: [price, quantity]}
  portfolio_pnl: {derived_from: [position_value, cost_basis]}
```

Verifier обязан проверить **каскад** прямыми pre/post observations, не только конечное состояние:

1. **Propagation:** compare a real source update with observed `position_value` and `portfolio_pnl` timestamps/values.
2. **Invalidation:** inspect an actual missing/invalid source record and whether consumers surface invalid/stale state.
3. **Partial cascade:** compare authoritative event time to downstream update time within the documented consistency window.

Evidence receipt records **the relation** between variables (cascade passed), not merely a plausible code-path claim.

### Invariant evidence patterns

PFD `invariants` — это свойства, которые должны выполняться **всегда**, независимо от пройденного branch:

```yaml
invariants:
  - "balance >= 0"
  - "sum(positions) == portfolio.total"
  - "updated_at <= now()"
```

Правила использования:
- Каждый invariant → одна named evidence row with query/command, source identity, observed values, and boolean result.
- Invariant is evaluated after each available observed branch — positive and negative alike.
- If an invariant cannot be observed read-only, the receipt states why; it is not fabricated through a mock.

### Quality criteria для temporal evidence

- **Repeatable:** no invented timing; every temporal observation has timestamp and allowed window.
- **Coverage:** at least one happy-path and one failure branch are directly observed where source access permits.
- **Invariant presence:** at least one PFD invariant has a source-backed boolean result.
- **No implementation leakage:** the receipt does not claim internal state absent from the PFD — the same Evidence Receipt Standard, with a temporal dimension.

## Anti-patterns (запрещено)

- ❌ Спавнить Worker'а, потом Lead читает код и говорит «выглядит ок» — это и есть self-evaluation bias.
- ❌ Verifier сообщает «проверю, что функция F работает», но не указывает source-of-truth command/query, factual output и invariant — где определение «работает»? Если требуется LLM-суждение — это не evidence.
- ❌ Worker и Verifier живут в одном thread'е (continuation_id, sub-prompt, etc.) — контекст пересекается, независимости нет.
- ❌ Скип пары «потому что задача маленькая» — именно на маленьких bias кусает сильнее всего, потому что Lead легко убеждает себя что «и так очевидно».
- ❌ Verifier видит Worker'ов prompt в брифе («чтобы знал что проверять») — нарушение независимости. Verifier строит контракт от Objective, не от прочтения чужого решения.
- ❌ Verifier изобретает специфику (exact error code, sort order, точное имя файла) когда Artifact Contract её не задаёт — implicit decision conflict; должен возвращать «ambiguous» или фиксировать требование недоступного direct probe; test artifact не создаётся.
- ❌ Lead override'ит FAIL вердикт по «чтению кода» — даже когда probe over-constrained, корректный путь — revised direct probe (категория V), не override.

## Origin

Toyota Jidoka / Jikotei Kanketsu — каждый узел self-certifies, не передаёт брак вниз. Anthropic engineering on multi-agent: *"separate generator from evaluator"*, *"grade what was produced, not the path"*, *"agents tend to confidently praise even mediocre work"*. Пара Worker+Verifier — это implementation plus independent source-of-truth evidence, not a synthetic acceptance harness.

External hardening (PAL/GPT-5.5 second-opinion 2026-04-30, continuation `27613123-a244-49a7-95ba-baeaab0dbf9a`): добавлены §"Verifier may see / may not see", §"Failure classification" (W/V/A/E), §"Test Legitimacy Standard", сужение skip carve-out, §"Artifact Contract" как обязательное предусловие.

Закрывает gap, документированный в:
- `reports/audit_2026-04-17_agent_context_dysfunction.md` — Lead's verification of Worker output is shallow; rules as prose, not blocking mechanics.
- `reports/audit_2026-04-18_startup_token_budget.md` — Framing 2 (attention saturation; rules buried at token 8000 lose to recent tool output).
- `reports/consilium_2026-04-29_temporal_causal_recon_pivot.md` — verify_gate v1.5 FP went 11 days unfixed across 6+ handovers (exact failure mode).
