# Техническое задание: Development/Release Test Modes

**Дата:** 2026-08-19
**Статус:** completed; focused tests, code review and exact-commit RELEASE gate passed
**Основание:** reports/consilium_2026-08-19_development_release_test_modes.md

## Контекст

Сейчас строгие Booster-пайплайны уже запрещают создавать и переписывать тесты во время implementation и откладывают durable regression tests и full suite до финального gate. Однако общий phase-контракт всё ещё говорит запускать тесты после каждого изменения, не ограничивая их объём. Исполняемого механизма выбора impacted tests, critical smoke, прошлых failures и Monte Carlo sample нет.

## North Star

Сократить время от изменения к полезному feedback без ослабления качества сдачи:

- development-проверка даёт быстрый advisory signal;
- только полный frozen-suite PASS, привязанный к точному promoted tree/SHA, разрешает release/push в protected branch.

## Цель текущей сессии

Спроектировать, реализовать, проверить и провести code review единого test dispatcher, который автоматически выбирает development или release режим, исполняет соответствующий набор тестов и создаёт проверяемый receipt.

## Scope

В scope:

- центральный test dispatcher;
- автоматическое определение режима;
- expiring phase lease вместо бессрочного доверия к простому phase marker;
- development test selection;
- deterministic stratified Monte Carlo sample 10%;
- release full-suite gate;
- pre-push resolver для push destination;
- exact-SHA/tree receipts;
- test, impact и critical-smoke registries;
- failure ledger;
- интеграция с /go, paired verification, phase contract и установщиком;
- тесты, документация, code review и финальная verification.

Вне scope:

- замена финального full suite выборочным тестированием;
- статистическое объявление 10% sample гарантией качества;
- live-генерация универсальной impact map моделью;
- изменение продуктовых тестов, не нужное для dispatcher;
- использование имени ветки как доверенной границы.

## Главный инвариант

Режим выбирает dispatcher, а не агент и не пользователь.

- Агент может повысить development до release.
- Агент не может понизить release до development.
- Неизвестное, повреждённое, просроченное или противоречивое состояние означает release.
- Локальная ветка main во время PLAN/IMPLEMENT сама по себе не означает release.
- Push destination main/protected branch или trusted CI merge-result всегда означает release.

## Функциональные требования

### FR-1. Central dispatcher

Единый CLI/module должен:

- принимать candidate identity и execution context;
- самостоятельно вычислять effective mode;
- до запуска тестов выдавать JSON decision receipt;
- запускать существующие тесты, не переписывая их;
- завершаться non-zero при invalid input, selector/registry error, required test failure или невозможности сохранить receipt;
- быть единственной поддерживаемой точкой managed test execution.

### FR-2. Mode resolution

Приоритет:

1. Trusted CI merge queue/protected push/merge-result, привязанный к candidate SHA: release.
2. Свежий валидный phase lease VERIFY или MERGE: release.
3. Явный запрос release: release.
4. Свежий валидный phase lease PLAN, IMPLEMENT или AUDIT: development.
5. Missing/stale/malformed/conflicting lease, unknown CI state, invalid manifest или selector failure: release.

Branch name сохраняется только как telemetry. Он не может понизить строгость.

### FR-3. Expiring phase lease

Phase state должен содержать:

- schema version;
- phase;
- created_at и expires_at;
- session/run identity;
- canonical project-root binding;
- issuer/version metadata;
- атомарную запись и validation.

Старый IMPLEMENT из прошлой сессии должен истекать. Невалидный lease не даёт development permission. Переходы фаз должны оставаться аудируемыми.

### FR-4. Development selection

Development manifest — объединение:

1. всех conservatively impacted tests;
2. всех critical-smoke tests;
3. всех unresolved prior failures;
4. deterministic stratified sample 10% из unaffected eligible tests.

Требования:

- critical tests запускаются всегда;
- strata задаются registry, минимум по component и test class/platform;
- sample воспроизводим из base SHA, candidate tree SHA, registry hash и sampling epoch;
- одинаковый input даёт одинаковые test IDs и порядок;
- unknown path, dynamic/generated dependency ambiguity, selector change или подозрительно пустой impacted set расширяют scope либо переключают в release;
- 10% — versioned и настраиваемый exploration budget, а не release evidence.

### FR-5. Release gate

Release mode должен:

- заново построить registry из точного candidate tree;
- до запуска заморозить manifest;
- выполнить полный зарегистрированный suite;
- отклонить skipped required jobs, continue-on-error, stale artifacts, изменение тестов после freeze и любой required non-zero exit;
- игнорировать development cache как release evidence.

Frozen manifest связывает commit SHA, tree SHA, test IDs, commands, test-file hashes, dispatcher/selector/runner versions, workflow/config/lockfile hashes и runtime identity.

### FR-6. Pre-push resolver

Перед git push resolver должен определить реальные destination ref и candidate SHA.

- Push в main/protected branch автоматически требует release.
- Push блокируется без валидного exact-candidate release receipt.
- Локальное имя branch main не включает release до анализа destination.
- Если protection/destination невозможно надёжно определить, используется release.
- Push в непродовую ветку может использовать development, но не создаёт promotion receipt.

Local pre-push не является единственной защитой: protected CI обязан повторно проверить exact merge-result SHA, поскольку git push --no-verify может обойти локальный hook.

### FR-7. Registries и ledger

Нужны versioned структуры:

- test registry: stable ID, command, file/content identity, components, platform, criticality, expected duration;
- impact registry: conservative source/component-to-test mapping и review metadata;
- critical-smoke registry: test ID, owner, rationale и review date;
- append-only failure ledger: test identity/content hash, candidate SHA/tree, failure signature, classification и valid closure.

Unresolved failure остаётся обязательным до подтверждённого green closure. Rename/delete/skip/xfail или изменение content hash не может молча очистить failure.

### FR-8. Receipts

Каждый запуск создаёт decision receipt. Release дополнительно создаёт promotion receipt.

Promotion receipt валиден только если commit SHA и tree SHA полностью совпадают с pushed/promoted candidate. Изменение candidate, manifest, runner/config или runtime identity инвалидирует receipt.

## Предлагаемые deliverables

Точные пути должны быть подтверждены RECON и architecture mapping до реализации:

- dispatcher CLI/module в templates/scripts/;
- structured lease support вокруг templates/scripts/phase.py;
- versioned registries в отдельном test-contract каталоге;
- failure ledger и run receipts в project-local .claude state;
- pre-push template/adapter;
- trusted-CI adapter;
- обновления go.md, paired-verification.md и phase contracts;
- unit/integration/adversarial tests;
- operator documentation и installer wiring.

## Задачи текущей сессии

1. Провести file-scoped RECON всех phase consumers, test entry points, installer paths, hooks и CI integration boundaries.
2. Подготовить Regression Loop Guard для каждого изменяемого файла.
3. Утвердить schemas lease, registries, receipts, manifest и failure ledger.
4. Реализовать structured expiring phase lease и compatibility path.
5. Реализовать registry validation, conservative impact resolution и deterministic sampler.
6. Реализовать central dispatcher и decision receipt.
7. Реализовать frozen release manifest, full-suite execution и exact-SHA receipt validation.
8. Реализовать pre-push resolver и trusted-CI adapter.
9. Обновить /go, paired verification, phase contracts и installer wiring.
10. Добавить focused tests после стабилизации поведения.
11. Провести независимый code review, security/bypass review и diff review.
12. Выполнить direct acceptance probes и финальный full-suite release run.

## KPI

| KPI | Цель | Как получить baseline |
|---|---:|---|
| Development p50 wall-clock | не более 25% full suite | Shadow plans + полный прогон на representative changes |
| Development p90 wall-clock | не более 45% full suite | Та же выборка с runtime identity |
| Critical-smoke inclusion | 100% | Проверка каждого decision receipt |
| Unresolved-failure inclusion | 100% | Seeded ledger scenarios |
| Release full-suite compliance | 100% | Аудит всех release receipts |
| Receipt SHA/tree match | 100% | Сравнение с реально pushed/promoted candidate |
| Deterministic rerun equivalence | 100% | Повтор одинакового resolver input |
| Critical regressions omitted in development | 0 | Shadow comparison с full suite |
| Non-critical selector false negatives | менее 0.5% после калибровки | Измерить до enforcement |
| Mapping coverage | 80% до enforcement; 95% steady state | Доля mapped changed paths/components |
| Dispatcher planning latency | менее 1 секунды cold | Отдельная timing measurement |
| Duplicate unchanged development runs | менее 5% | Receipt telemetry по candidate identity |

KPI ускорения нельзя объявлять достигнутым без baseline. Первая стадия — shadow: development plan вычисляется, но full suite ещё выполняется для измерения misses.

## Acceptance criteria

- Fresh IMPLEMENT lease на локальном main без trusted release context выбирает development.
- Push destination protected main выбирает release даже при IMPLEMENT и запросе development.
- Trusted merge-result SHA X отклоняет receipt для SHA Y.
- Missing, expired, malformed или root-mismatched lease выбирает release.
- Одинаковые immutable sampler inputs дают byte-equivalent test ordering и manifest hash.
- Development plan включает все impacted, critical и unresolved-failure tests до sample.
- Unknown mapping или invalid registry не выдаёт development PASS.
- Release manifest замораживается до тестов; mutation после freeze блокирует promotion.
- Protected push без exact-tree PASS receipt блокируется.
- Unresolved failing test нельзя удалить из обязательного consideration через rename/skip/xfail.
- Любая failure path выдаёт диагностический non-PASS receipt и non-zero exit.

## Rollout

1. Shadow mode: selective plan + обязательный full suite, сбор timing и omission evidence.
2. Development enforcement после прохождения mapping и false-negative thresholds.
3. Protected release enforcement с exact merge-result receipt.

## Rollback

- Отключить selective development enforcement и вернуть промежуточные full-suite runs.
- Сохранить release enforcement, registries, ledger и receipts.
- Никогда не использовать selective evidence как release evidence.

## Обязательный code review

До финальной сдачи провести:

- correctness review mode precedence и fail-closed behavior;
- security review trust-source parsing, protected destination и bypass paths;
- review lease expiry/root/run binding;
- review deterministic sampling и registry validation;
- review exact-SHA/tree binding и frozen-manifest integrity;
- независимый diff review после исправлений.

Любой HIGH/MED finding должен быть исправлен либо явно заблокировать Definition of Done. Review не может переопределить failing executable evidence.

## Verification gate

Обязательно выполнить direct probes для:

- local main + PLAN/IMPLEMENT;
- protected push;
- stale/malformed lease;
- merge-result SHA mismatch;
- invalid registry и unknown mapping;
- deterministic sample;
- prior-failure replay;
- frozen-manifest mutation;
- попытка downgrade release;
- обход local hook и повторная CI защита.

После direct probes выполнить полный frozen-suite run через release path и связать evidence с candidate SHA/tree.

## Definition of Done

Сессия завершена только когда:

- dispatcher и оба режима реализованы;
- expiring phase lease работает;
- registries и failure ledger валидируются;
- development selection воспроизводим;
- protected push/CI требуют release;
- exact-tree receipts не принимают stale evidence;
- acceptance criteria проходят;
- code review и bypass/security review завершены без открытых HIGH/MED;
- финальный full suite проходит через release path;
- документация и installer/runtime mirrors синхронизированы.
