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
