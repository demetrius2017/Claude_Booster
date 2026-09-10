# Roadmap: локальный патч Codex для истории ожидания агентов

**Статус: реализация и локальная сборка выполнены; требуется ребейз перед
следующим использованием.** Выпущен opt-in патч на `rust-v0.145.0-alpha.13`,
но после обновления официального Codex до `0.146.1` launcher корректно
отвергает старый release и запускает `/opt/homebrew/bin/codex`. Старые уже
запущенные patched TUI-процессы не мигрируются автоматически и должны быть
закрыты отдельно.

Основание решения: [consilium по wait-history patch](reports/consilium_2026-08-05_codex_wait_history_patch.md), commit [`c46068f`](https://github.com/demetrius2017/Claude_Booster/commit/c46068f).

## Цель и границы

Сделать opt-in, version-pinned локальную сборку Codex, которая в v1 убирает из истории только шумные карточки `Wait` со статусом `InProgress` (`Waiting for agents`). Это UI-патч, а не изменение протокола или жизненного цикла subagents.

Не входит в v1: изменение `DEFAULT_WAIT_TIMEOUT_MS`, перехват/отмена ожиданий, скрытие ошибок или завершений, модификация Homebrew-бинарника и изменение upstream-исходников без отдельного pinned worktree.

Проверенная точка изменения: `codex-rs/tui/src/multi_agents.rs`. Runtime-ожидание и его default timeout в первой версии не меняются.

## Этапы

1. **Repro и pin.** Зафиксировать upstream tag/commit, воспроизвести повторяющиеся `Waiting for agents`, записать исходный UX и проверить, что `Wait`/`InProgress` различим от финальных результатов.
2. **Узкий patch.** В отдельном worktree применить display-only изменение ровно к `Wait` + `InProgress`; включение — opt-in.
3. **Тесты и smoke.** Проверить spawn → тихое ожидание → completion, а также error, interrupt, approval и follow-up.
4. **Установка.** Собрать отдельный бинарник и переключать через wrapper, который сверяет ожидаемые version/commit; системный Codex остаётся нетронутым.
5. **Обновления и rollback.** Для каждого обновления: `apply --check`, rebuild, smoke-test, затем атомарное переключение wrapper. При конфликте или провале — wrapper возвращается к `/opt/homebrew/bin/codex`.

## Неприкосновенные инварианты и acceptance gates

- В v1 скрывается только `Wait` в `InProgress`; все финальные статусы (`Completed`, `Errored`, `Interrupted`, `Shutdown`, `NotFound`) видимы.
- Видимы approvals, task handles/paths, итоговые сообщения; `/agent` продолжает отображать и открывать агентов.
- `list_agents`, `send_message` и `followup_task` сохраняют исходное поведение.
- Default timeout не меняется до отдельного решения и отдельной проверки.
- Gate перед переключением wrapper: pinned version/commit совпадает, patch применяется чисто, сборка успешна, smoke охватывает completion и ошибку/interrupt.

## Риски, фальсификаторы, done

Главный риск — upstream изменит структуру TUI, а патч начнёт скрывать не только шум или сломает наблюдаемость. Фальсификатор решения: smoke показывает пропавший финальный статус, approval/handle, либо `/agent` перестаёт открывать агента — патч не устанавливается или немедленно откатывается.

Готово, когда opt-in patched binary проходит все gates, одна активная строка ожидания не засоряет историю повторными карточками, финальные состояния и управление агентами сохранены, а rollback документирован и проверен на реальном wrapper.

## Завершено в сессии 2026-08-06 — Gantt observability

Добавлена команда `/gantt`: фактический компактный снимок дорожек `Done / Now /
Next / State` и слотов. Она использует только состояние задач, известные факты и
не более одного снимка агентов; не создаёт scheduler и не poll'ит runtime. Lead
публикует snapshot после запуска, reassignment или terminal-события worker.
В Codex используйте `$gantt` или `/prompts:gantt`; bare `/gantt` зависит от UI.

## Завершено в сессии 2026-08-09 — External model runtime health

Закрыт runtime-fix для GLM-5.2/Grok routing: retired Z.ai alias
`glm-5.2[1m]` больше не маскируется пустым выводом, валидный `glm-5.2` с
`HTTP 429 insufficient_balance` переводится в degraded provider health, а
`audit_secondary` и hackathon external routing могут честно уходить на
`grok-cli:grok-4.5`. Commit `a76e7c3` установлен локально через
`python3 install.py --yes` без `--force`; focused verification прошла:
127 assertions в 7 model-balancer suites, Z.ai resilience 15/15, audit smoke
12/12, `git diff --check` clean. Операционные долги остаются внешними:
пополнить Z.ai balance/package и дождаться восстановления PAL quota.

## Завершено в сессии 2026-08-09 — Balanced Codex delegated model routes

Закрыт `4e04cab fix(codex): enforce balanced delegated model routes`, уже
отправленный в `origin/main` и `public/main`. Новый
`templates/scripts/codex_routed_worker.py` получает exact category route,
типизированно валидирует provider/model/reasoning effort, запрещает caller
`-m`/`--model` и config override, fail-closed обрабатывает не-Codex routes и
использует пятисекундный lookup с unpinned degraded fallback. Проверки: exact
route и GPT-5.6 routing PASS, installer/capability suite `23 passed`, live
smoke вернул `START_ROUTED_OK` через `gpt-5.6-luna` c `low` effort и
`source=balancer`; override отвергнут с exit 2 до HTTP-запроса. Этот трек не
меняет статус отдельного wait-history patch выше: он всё ещё version-pinned к
`rust-v0.145.0-alpha.13` и требует rebase перед следующим использованием.

## Завершено в сессии 2026-08-19 — Development/Release Test Modes

В `origin/main` доставлен центральный test dispatcher с expiring phase lease,
детерминированной глобальной 10% DEVELOPMENT-выборкой, exact-tree RELEASE
receipt, failure ledger и fail-closed pre-push/CI границами. Финальный RELEASE
gate прошёл 44/44 frozen jobs на commit `e43a7bd`; следующий этап — shadow
калибровка p50/p90 и omission rate до широкого включения enforcement.

## Завершено в сессии 2026-09-02 — внешние рецензенты восстановлены

PAL/OpenAI и Z.ai падали по балансу, а не по коду; Z.ai пополнен (Lite
yearly), PAL ждёт кредитов OpenAI. Grok получил probe `grok_cli.py status`,
типизированные события отказов (`max_turns`, `binary_missing`, `auth_missing`)
и редакцию секретов; `codex_routed_worker.py` больше не подменяет Grok/GLM
самим Codex — non-Codex маршруты завершаются exit 65 с подсказкой runner'а.
Спеки audit/consilium/hackathon/go и Codex-skill ссылаются на probe и
требуют `--budget-turns 24` для аудитов с чтением репозитория. Commit
`ee8a1a3`, установлено через `install.py --yes`; 318 passed, smoke 14/14.

## Завершено в сессии 2026-09-03 — bounded Codex loop и GLM-5.3

Добавлена `$loop`: внешний scheduler спит без участия модели и делает bounded
`codex queue` wakes только для явно указанного thread. Безопасный default — не
более 8 пробуждений (при `30m` это 4 часа), но предел настраивается через
`--max-wakes`; доступны `$loop status` и `$loop stop`. Реализация валидирует
интервал, UUID и timeout, управляет singleton/stale PID состоянием и не повторяет
queue автоматически после неоднозначного локального timeout. Прямой probe поймал
и закрыл пропущенный `--message` в queue argv.

Z.ai route обновлён до `glm-5.3` и подтверждён живым ответом `GLM_5_3_OK`.
Проверки: focused pytest **47 passed**, Z.ai smoke **14/14**, model-balancer
**7/7 suites и 127 assertions**, installed/template parity и `git diff --check`
прошли. Отдельный wait-history patch из начала roadmap не заменён этой работой:
он всё ещё pinned к `rust-v0.145.0-alpha.13` и требует rebase для Codex `0.146.1`.

## Завершено в сессии 2026-09-10 — Codex flagship GPT-6 Astra

Commit `6b51ec4` перевёл только флагманский Codex CLI маршрут `consilium_bio` на
`codex-cli:gpt-6-astra` с `medium` reasoning effort. Luna и Terra не менялись,
а PAL `audit_external` сохранил `pal:gpt-5.6-sol`; historical и explicit Sol
совместимость остались на месте. Канонический трёхполевый Sol route мигрирует
на Astra, custom Sol route с дополнительными операторскими полями сохраняется,
а active scorer больше не может воскресить Sol для pinned `consilium_bio`.

Boundary adapter теперь fail-closed: malformed успешный ответ балансировщика
завершается exit `65` до запуска Codex, тогда как реальная недоступность lookup
сохраняет документированный unpinned fallback. Изменения установлены через
`python3 install.py --yes`; focused pytest прошёл `38 passed`, model-balancer
smoke — `7/7`, telemetry — `17/17`, template/runtime parity подтверждён, а
живой routed smoke вернул `ASTRA_ROUTED_OK` с `source=balancer` и Astra как
requested/effective model. Остались не-блокирующие предупреждения Astra:
omitted priority tier, локальные metadata fallback и Context7 `AuthRequired`;
после обновлений их нужно мониторить. Статус отдельного wait-history patch выше
эта работа не меняет.

## Завершено в сессии 2026-09-10 — business-first правила Lead

Commit `025a3de` скорректировал характер Lead в Claude Booster: проверка теперь
служит достижению проверенного бизнес-результата, а не доминирует над ним через
повторные микроаудиты. Правила требуют сначала определить acceptance и доменные
инварианты, переиспользовать актуальное evidence и открывать новый круг только
после изменения, сбоя или конкретного неустранённого существенного риска.
Защита данных, финансов, безопасности и production-контрактов осталась
обязательной. Изменение установлено для Claude и Codex; focused
`test_lead_epistemic_anchors`, hook/validator проверки, `git diff --check` и
независимый focused review прошли. Точный эффект на недельный token budget ещё
не измерен и остаётся operational follow-up.
