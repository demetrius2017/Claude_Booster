# Roadmap: локальный патч Codex для истории ожидания агентов

**Статус: только планирование.** Патч, сборка и установка ещё не начинались.

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
