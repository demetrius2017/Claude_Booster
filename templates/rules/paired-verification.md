---
description: "Paired Worker+Verifier protocol for delegated code work. Loads when planning Agent spawns, code edits, or paired acceptance verification."
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

Dmitry's request (2026-04-30): когда Lead делегирует содержательную работу, второй агент создаёт приёмку **независимо** — параллельно или последовательно, но с собственным контекстом. Lead не оценивает результат своим суждением; он запускает тест, написанный Verifier'ом, и читает exit code.

## Why this rule exists

Эмпирически и теоретически, **single-agent ≥ multi-agent при равных compute** (arxiv 2604.02460, Anthropic engineering, Cognition). Когда Lead всё-таки делегирует, проигрыш приходит из трёх источников:

1. **Information loss at handoff** — Data Processing Inequality: ответ Worker'а информационно ограничен брифом, summary Lead'а ограничен ответом Worker'а. Каждый hop через границу агента — лосси кодек.
2. **Self-evaluation bias** — Anthropic явно: *"agents tend to confidently praise even mediocre work"*. Lead, оценивающий Worker'а, — это та же модель, которая написала бриф; она склонна видеть результат как соответствующий собственному намерению.
3. **Lead's context decay during the wait** — пока Worker работает, окно Lead'а смещается, acceptance criteria вытесняются свежими tool-results. К моменту возврата Worker'а у Lead'а уже размыто «что значит done».

Контра-мера, конвергентно рекомендуемая Adzic (Specification by Example), Toyota Jidoka (self-process completion), Anthropic eval guidance (*"separate generator from evaluator"*): **acceptance check — отдельный артефакт, executable, написанный другим агентом, на другом контексте, который Lead запускает машинно**.

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
- Populate `Downstream consumers:` from `called_by` arrays of the same components — these are the functions/endpoints the Verifier MUST test
- If `critical: true` OR `feeds` array has ≥3 entries for any touched component → mark for conditional Architecture Auditor (see §Architecture-aware verification below)
- **[CRITICAL] Code-over-docs**: dep_manifest.json reflects state at last update, not necessarily NOW. Before populating `Architecture constraints:` and `Downstream consumers:`, Lead MUST cross-check manifest entries against actual code (grep for function, check if it's still called, verify writer is still active). When manifest says "X writes to Y" but code shows X is disabled → manifest is stale, not code is wrong. Update manifest first, then populate Artifact Contract from corrected manifest. Finding type for divergence: `architecture-docs-stale`.

## Pattern A — тройка: Flow Designer → Worker + Verifier (по умолчанию)

Три стадии, две из которых параллельны:

**Стадия 1 — Flow Designer (sequential, до Worker+Verifier):**
Lead спавнит Flow Designer agent с Verified Facts Brief из RECON. Flow Designer анализирует задачу и производит Process Flow Document (PFD): temporal topology, branching scenarios, state dependencies, failure modes, worker directives, verifier assertions.

**Стадия 2 — Worker + Verifier (parallel, после Flow Designer):**
В одном сообщении — два `Agent` tool-call'а:
1. **Worker agent** получает: goal + Verified Facts Brief + PFD + scope + Artifact Contract (enriched with PFD worker_directives and failure_modes). Делает работу, кладёт артефакт в указанный путь, возвращает ссылку на него.
2. **Verifier agent** получает: тот же goal + ту же Verified Facts Brief + PFD (verifier_assertions, invariants, branching_scenarios) + тот же scope + тот же Artifact Contract. **Не видит prompt'а Worker'а.** Производит ОДИН executable acceptance test.

Оба Worker и Verifier бегут конкурентно. Lead дожидается обоих, потом запускает тест.

For critical components (see §Architecture-aware verification): Lead MAY spawn a third parallel agent — the **Architecture Auditor** — in the same message as Worker+Verifier. All three run concurrently. Lead runs both acceptance tests after all agents return.

## Pattern B — последовательная пара

Используется только когда контракт нельзя выразить, не увидев форму артефакта (редко). Worker → Verifier → Lead запускает тест. Verifier всё равно работает в свежем контексте; всё равно не видит prompt'а Worker'а; всё равно производит executable, не суждение.

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
Downstream consumers: <specific functions/endpoints Verifier MUST include in acceptance test — from dep_manifest.json `called_by` arrays; "(none)" if manifest absent or called_by empty>
Process Flow Document: <path to PFD YAML or inline PFD — MANDATORY for all delegations>
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

## Verifier mandate (точная формулировка для prompt'а)

> «Произведи один executable acceptance test, который вернёт exit 0 если артефакт удовлетворяет Artifact Contract, иначе non-zero. Тестируй **наблюдаемое поведение**, не приватные детали реализации. Не реализуй задачу. Не предполагай, как Worker её решит. Если acceptance criteria неоднозначны — **fail closed**: верни отчёт об неоднозначности вместо того чтобы изобретать продуктовые решения. Тест должен печатать осмысленный stdout/stderr при failure.»

When the Artifact Contract contains a non-empty `Downstream consumers:` field, the Verifier's test MUST include at least one assertion against a listed downstream consumer — verifying that the consumer still receives correct input or produces correct output after the Worker's change. This is the architecture protection layer: dep_guard.py auto-skips for Worker subagents by design (the hook targets Lead only), so downstream integrity is enforced through the Verifier's test, not through the hook.

## Test Legitimacy Standard

Verifier-тест должен соответствовать всем пунктам:
- Тестирует **наблюдаемое поведение**, не приватные детали реализации (если деталь не в Artifact Contract явно).
- Минимизирует допущения, которых нет в Objective / Verified Facts Brief / Artifact Contract.
- **Детерминирован** — два запуска подряд дают одинаковый результат.
- Печатает осмысленный diagnostic output при failure (assertion, что ожидалось, что получено).
- Не зависит от network / localhost / hidden state / timing / CWD, если это не явно в scope.
- Использует tempdir / tempfile где уместно, чистит за собой.
- Возвращает non-zero на любой failure path.

### Allowed forms
- Bash-скрипт с `set -e` и явными `[[ ... ]]` / exit codes.
- pytest test-file, запускаемый как `pytest <path>`.
- `curl --fail` против реального endpoint'а + `grep` по телу + assert на HTTP code.
- `sqlite3` / `psql` запрос с проверкой rowcount или конкретного значения.
- `grep -q` по файлу с ожидаемой строкой/паттерном.
- Любая комбинация выше с явным non-zero exit на failure.
- Один executable artifact МОЖЕТ содержать несколько assertion'ов (positive case + negative case + edge case в одном файле — это нормально и желательно).

### Forbidden forms
- ❌ Прозаический checklist «проверьте что X, Y, Z».
- ❌ «Look at the output and decide» — требует LLM-суждения.
- ❌ Тест, который вызывает Claude/LLM как judge.
- ❌ `curl -s` без `--fail` / `|| true` (см. verify_gate fake-evidence patterns).
- ❌ `localhost` / `127.0.0.1` как target в проде-сценарии.
- ❌ Verifier изобретает конкретные продуктовые решения (точные exit codes, sort order, key names) когда Artifact Contract их не специфицирует — это создаёт скрытую спеку, которую Worker не видел.

## Lead's role after the pair returns

1. Worker возвращает → запомнить путь к артефакту.
2. Verifier возвращает → запомнить путь к acceptance-тесту.
3. Lead запускает тест через `Bash`. Записывает exit code и stdout/stderr.
4. **PASS (exit 0):** идём дальше — commit / next step / TaskUpdate completed.
5. **FAIL (exit ≠ 0):** не правим inline. Сначала **классифицируем failure** (см. ниже), потом действуем.

**Lead не читает код Worker'а чтобы вынести вердикт.** Единственный вход для PASS/FAIL — exit code теста + его stdout. stdout читаем для роутинга remediation, не для override'а вердикта.

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
| **During edit** | Verifier | Sees `Downstream consumers:` → MUST test at least one downstream consumer |
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
3. Verifier's test (tests downstream consumers)
4. Post-verify update (updates docs to reflect new reality)

### Conditional Architecture Auditor (critical components)

When dep_manifest.json shows `critical: true` **OR** `feeds` array has **≥3 entries** for any component touched by the planned edit:

1. Lead spawns a **third parallel agent** alongside Worker+Verifier: the **Architecture Auditor**
2. Architecture Auditor receives: Artifact Contract + full dep_manifest.json + ARCHITECTURE.md (if exists)
3. Architecture Auditor produces: an executable test that verifies downstream consumers listed in `feeds`/`called_by` still work correctly
4. Lead runs **both** Verifier's test AND Auditor's test; both must exit 0
5. Failure classification applies independently to each test (W/V/A/E)

For non-critical components (the 80–90% case): the enriched pair is sufficient. The conditional Auditor is a safety net for high-connectivity nodes in the dependency graph.

The Architecture Auditor is NOT a Verifier — it does not test the Worker's artifact against the Artifact Contract. It tests that the **surrounding system** still works after the change. The Verifier tests the artifact; the Auditor tests the environment.

## Failure classification — обязательно перед реакцией на FAIL

Lead классифицирует non-zero exit ровно в одну из четырёх категорий:

| Категория | Признак | Реакция |
|---|---|---|
| **W. Artifact wrong** | Artifact существует, ведёт себя не так, как требует Artifact Contract | Спавнить нового Worker'а с narrowed scope, передать failing test + stdout. Verifier-тест НЕ менять. |
| **V. Test invalid / over-constrained** | Тест проверяет приватную деталь не из Contract; Worker реализовал контракт корректно но иначе | Спавнить нового Verifier'а (свежий контекст), требовать revise теста против оригинального Contract. Worker НЕ менять. Lead не отменяет verification — только перевыпускает тест. |
| **A. Contract ambiguous** | Verifier явно вернул «ambiguous», или оба (Worker+Verifier) интерпретировали по-разному | Lead уточняет Artifact Contract, обновляет Verified Facts Brief, перезапускает пару (Pattern A). |
| **E. Environment** | Тест не запустился из-за зависимостей, доступа, версии, network | Чинит окружение / harness, перезапускает тот же тест. |

**Важно:** ни в одной из четырёх категорий Lead не выносит PASS «по чтению кода». Категория V — единственный путь признать тест невалидным, и он требует **regen теста**, не override.

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

## Temporal & Process Verification

Когда Artifact Contract ссылается на Process Flow Document (PFD), Verifier получает дополнительный источник executable-спецификации. PFD — это YAML-артефакт от Flow Designer, содержащий формализованные сценарии поведения системы во времени. Наличие PFD **расширяет** стандартный Verifier mandate, но не отменяет его: Test Legitimacy Standard и принцип «наблюдаемое поведение, не implementation details» по-прежнему действуют.

### Когда применяется

PFD-верификация активна когда Artifact Contract содержит:
- Поле `Process Flow Document:` с путём к YAML-файлу, ИЛИ
- Блок `verifier_assertions:` инлайн в контракте.

Если ни того, ни другого нет — этот раздел не применяется, работает обычный Verifier flow.

### Что Verifier видит из PFD

| PFD-секция | Доступна Verifier'у? | Как используется |
|---|---|---|
| `verifier_assertions` | ✅ Да | Прямой источник тест-паттернов — каждый assertion = один или более test case |
| `invariants` | ✅ Да | Postconditions, которые проверяются ПОСЛЕ каждого сценария |
| `branching_scenarios` | ✅ Да | Список условий для injection — каждый сценарий = минимум один test case |
| `timeline` | ✅ Да | Reference для ordering guarantees и expected state transitions |
| `state_variables` | ✅ Да | Dependency graph для cascade verification |
| `worker_directives` | ❌ Нет | Implementation guidance для Worker'а; Verifier не должен знать *как* реализовано |
| `failure_modes` | ✅ Да | Expected graceful degradation — каждый failure mode = negative test case |

### Temporal test patterns

Время — самый частый источник недетерминизма. PFD-тесты обязаны быть deterministic, поэтому:

**Mock clock / controllable time:**
```python
# monkeypatch time.time() для предсказуемости
with unittest.mock.patch('time.time', side_effect=[1000.0, 1000.5, 1001.0]):
    result = cache.get_or_refresh("key")
    assert result.fetched_at == 1000.0
```

**Staleness detection** — если PFD описывает TTL/expiry:
```bash
# Inject T₀, advance clock past TTL, verify staleness detected
set -e
RESULT_T0=$(python3 -c "from module import get_value; print(get_value(clock=100))")
RESULT_STALE=$(python3 -c "from module import get_value; print(get_value(clock=100 + TTL + 1))")
[[ "$RESULT_STALE" == "STALE" || "$RESULT_STALE" == "" ]] || { echo "FAIL: stale value not detected"; exit 1; }
```

**Ordering guarantees** — когда PFD `timeline` задаёт «A before B»:
- Inject событие B без предшествующего A → assert rejection/queue/error
- Inject A then B → assert success
- Inject A, B, A (повтор) → assert idempotency если PFD указывает

### Branch injection testing

Каждый `branching_scenarios` entry из PFD превращается в test case:

| Сценарий PFD | Что инжектировать | Что assert'ить |
|---|---|---|
| Partial success | Mock data source возвращает 2/5 items, затем error | Processed count = 2, error logged, state consistent |
| Timeout path | Inject timeout (mock sleep / deadline) раньше production значения | Cleanup/rollback выполнен, ресурсы освобождены |
| Reject/deny path | Auth mock returns 403, validation returns error | Rollback выполнен, side effects откачены, состояние = pre-attempt |
| Retry exhaustion | Mock возвращает transient error N+1 раз (N = max retries) | Final state = failed gracefully, не зависло |

Минимум: **один test case на каждый `branching_scenarios` entry**. Если PFD описывает 4 сценария — в тесте минимум 4 отдельных assertion-блока.

### Cascade verification

Когда PFD `state_variables` описывает зависимости (derived variables):

```
state_variables:
  price: {source: market_feed}
  position_value: {derived_from: [price, quantity]}
  portfolio_pnl: {derived_from: [position_value, cost_basis]}
```

Тест обязан проверить **каскад**, не только конечное состояние:

1. **Propagation:** изменить `price` → assert `position_value` обновился → assert `portfolio_pnl` обновился
2. **Invalidation:** удалить/обнулить `price` → assert dependents либо (a) выдают ошибку, либо (b) помечены invalid — НЕ возвращают stale значение молча
3. **Partial cascade:** если PFD допускает async propagation — inject change, wait (mock), verify eventual consistency

Тест assert'ит **связь** между переменными (cascade прошёл), а не конкретные числа (которые зависят от implementation).

### Invariant assertion patterns

PFD `invariants` — это свойства, которые должны выполняться **всегда**, независимо от пройденного branch:

```yaml
invariants:
  - "balance >= 0"
  - "sum(positions) == portfolio.total"
  - "updated_at <= now()"
```

Правила использования:
- Каждый invariant → **одна assertion-функция** (helper), вызываемая многократно
- Эта assertion вызывается **ПОСЛЕ каждого branch scenario** — и positive, и negative
- Invariant-assertion не зависит от конкретного сценария — она проверяет universal property

```python
def assert_invariants(state):
    """PFD invariants — вызывать после КАЖДОГО test case."""
    assert state.balance >= 0, f"balance={state.balance} < 0"
    assert abs(sum(state.positions) - state.portfolio.total) < 0.01
    assert state.updated_at <= time.time()

# В каждом test case:
def test_successful_trade():
    state = execute_trade(...)
    assert_invariants(state)  # ← обязательно

def test_rejected_trade():
    state = attempt_and_fail(...)
    assert_invariants(state)  # ← тоже обязательно
```

### Quality criteria для temporal tests

- **Deterministic:** никаких `time.sleep()` > 100ms в реальном времени; все temporal-проверки через mock clock или controllable deadline.
- **Coverage:** минимум один happy-path + один failure-branch из PFD покрыты.
- **Invariant presence:** минимум один PFD `invariant` assert'ится как postcondition.
- **No implementation leakage:** тест не проверяет internal state, которого нет в PFD — тот же Test Legitimacy Standard, просто с temporal dimension.

## Anti-patterns (запрещено)

- ❌ Спавнить Worker'а, потом Lead читает код и говорит «выглядит ок» — это и есть self-evaluation bias.
- ❌ Verifier пишет «тест, который проверит, что вызвав функцию F, она работает» — где определение «работает»? Если требует LLM-суждения — не acceptance.
- ❌ Worker и Verifier живут в одном thread'е (continuation_id, sub-prompt, etc.) — контекст пересекается, независимости нет.
- ❌ Скип пары «потому что задача маленькая» — именно на маленьких bias кусает сильнее всего, потому что Lead легко убеждает себя что «и так очевидно».
- ❌ Verifier видит Worker'ов prompt в брифе («чтобы знал что проверять») — нарушение независимости. Verifier строит контракт от Objective, не от прочтения чужого решения.
- ❌ Verifier изобретает специфику (exact error code, sort order, точное имя файла) когда Artifact Contract её не задаёт — implicit decision conflict; должен возвращать «ambiguous» или писать property-style тест.
- ❌ Lead override'ит FAIL вердикт по «чтению кода» — даже когда тест over-constrained, корректный путь — regen теста (категория V), не override.

## Origin

Adzic «Specification by Example» — спецификация = executable примеры. Toyota Jidoka / Jikotei Kanketsu — каждый узел self-certifies, не передаёт брак вниз. Anthropic engineering on multi-agent: *"separate generator from evaluator"*, *"grade what was produced, not the path"*, *"agents tend to confidently praise even mediocre work"*. Cognition «Don't build multi-agents» — для write-heavy tasks single-agent выигрывает; пара Worker+Verifier — это не multi-agent в их смысле, это single-agent + independent acceptance harness.

External hardening (PAL/GPT-5.5 second-opinion 2026-04-30, continuation `27613123-a244-49a7-95ba-baeaab0dbf9a`): добавлены §"Verifier may see / may not see", §"Failure classification" (W/V/A/E), §"Test Legitimacy Standard", сужение skip carve-out, §"Artifact Contract" как обязательное предусловие.

Закрывает gap, документированный в:
- `reports/audit_2026-04-17_agent_context_dysfunction.md` — Lead's verification of Worker output is shallow; rules as prose, not blocking mechanics.
- `reports/audit_2026-04-18_startup_token_budget.md` — Framing 2 (attention saturation; rules buried at token 8000 lose to recent tool output).
- `reports/consilium_2026-04-29_temporal_causal_recon_pivot.md` — verify_gate v1.5 FP went 11 days unfixed across 6+ handovers (exact failure mode).
