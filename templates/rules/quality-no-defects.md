---
description: "Three Nos (Jikotei Kanketsu): no accept/make/pass-on defects. Loads on code, schema, or migration edits."
paths:
  - "**/*.py"
  - "**/*.ts"
  - "**/*.tsx"
  - "**/*.jsx"
  - "**/*.go"
  - "**/*.rs"
  - "**/*.java"
  - "**/*.sql"
  - "**/migrations/**"
  - "**/Dockerfile"
---

# Built-in Quality — Three Nos (Jikotei Kanketsu)

Dmitry's request (2026-04-29): закрепить как директиву верхнего уровня. Корни — Toyota Production System, принцип **Jikotei Kanketsu** (自工程完結) / Дзидока. Каноническая формулировка:

> **Do not accept defects · Do not make defects · Do not pass on defects.**
> **Не принимай брак · Не делай брак · Не передавай брак.**

Качество встраивается в источнике (built-in quality at the source), а не инспектируется на выходе. Любой узел в цепочке имеет право и обязанность остановить «конвейер» при обнаружении дефекта.

## [CRITICAL] Two layers — agent AND code

Принцип применяется на **двух уровнях одновременно**, и второй уровень важнее первого:

### Layer 1 — поведение агента (как Claude работает)
Не принимай противоречивый контекст, не строй на устаревшей памяти, не закрывай task без evidence, не передавай отчёт вниз без cross-check'а с кодом. Это процессный axis.

### Layer 2 — код, который Claude пишет (как функции работают на проде) — **главное применение**
**Каждая функция, которую агент создаёт или модифицирует в проектах пользователя, обязана физически реализовывать Three Nos в самом коде.** Не «агент проверил при ревью», а **код сам не пускает брак** в runtime, без участия человека или агента.

Конкретно это означает, что в тело функции должны быть вшиты:

**Input guards (Layer 2 / Не принимай):**
```python
def calculate_position_size(account_value: Decimal, weight: float, price: Decimal) -> int:
    # NOT: if not account_value: account_value = 0   ← молчаливый fallback = брак
    if account_value is None or account_value <= 0:
        raise ValueError(f"account_value must be positive Decimal, got {account_value!r}")
    if not 0 < weight <= 1:
        raise ValueError(f"weight must be in (0, 1], got {weight}")
    if price is None or price <= 0:
        raise ValueError(f"price must be positive Decimal, got {price!r}")
    ...
```
Pydantic-модели на API-границах, `CHECK` constraint'ы в DB-миграциях, type guards на TypeScript-границах — это всё формы Layer 2.

**Body invariants (Layer 2 / Не делай):**
```python
def reconcile_positions(broker_state: dict, db_state: dict) -> ReconcileResult:
    result = ReconcileResult(...)
    # инвариант: после reconcile sum(db_positions) == sum(broker_positions) ± epsilon
    assert abs(result.db_total - result.broker_total) < Decimal("0.01"), \
        f"Reconcile invariant broken: db={result.db_total} broker={result.broker_total}"
    return result
```
Идемпотентность через `ON CONFLICT DO NOTHING`/`UPSERT`, детерминизм через explicit `Decimal` rounding, инварианты через assert'ы или explicit checks.

**Output guards (Layer 2 / Не передавай):**
```python
def get_nav_snapshot(account_id: str) -> NAVSnapshot:
    snapshot = ...
    # перед возвратом — проверь, что собранное согласовано
    if snapshot.equity != snapshot.cash + snapshot.positions_value:
        raise InconsistentStateError(
            f"NAV components don't sum: equity={snapshot.equity}, "
            f"cash+pos={snapshot.cash + snapshot.positions_value}"
        )
    return snapshot
```
Schema-validation перед `return`, контрактные пост-условия, явный `raise` вместо silent corrupted return.

### Fix the producer, not the data (Layer 2 / Не маскируй брак)

Если данные в БД неверны — чини **функцию, которая их создала**, а не строку в таблице. Прямой `UPDATE` на derived-readonly колонке (колонке из `data_patches_forbidden` в dep_manifest.json) — это нарушение Three Nos: ты маскируешь баг в функции-производителе, а она на следующем вызове опять создаст неверные данные.

**Правило:** `UPDATE`/`DELETE` на protected table без починки producer-функции = передача брака вниз.

```python
# ЗАПРЕЩЕНО — маскирует баг в snapshot_nav():
# UPDATE nav_snapshots SET total_nav = 125000.00 WHERE snapshot_date = '2026-05-04'

# ПРАВИЛЬНО — чиним функцию-производитель:
def snapshot_nav(account_id: str) -> NAVSnapshot:
    result = calculate_nav(account_id)
    # Починили calculate_nav() → snapshot_nav() теперь пишет корректное значение
    ...
```

```python
# ЗАПРЕЩЕНО — маскирует баг в apply_fill() VWAP логике:
# UPDATE orders SET filled_quantity = 500, avg_fill_price = 142.50 WHERE id = 'ord_123'

# ПРАВИЛЬНО — чиним VWAP агрегацию в apply_fill():
def apply_fill(fill: Fill) -> None:
    prior_fills = get_fills_for_order(fill.order_id)  # все partial fills
    total_qty = sum(f.quantity for f in prior_fills) + fill.quantity
    vwap = sum(f.quantity * f.price for f in prior_fills + [fill]) / total_qty
    ...
```

**Единственное исключение:** one-time data cleanup **ПОСЛЕ** починки producer-функции, с маркером `[dml-authorized]` в транскрипте и объяснением какая функция была починена.

**Anti-pattern:** «быстро поправим данные сейчас, код починим потом» — «потом» не наступает, а producer на следующем запуске создаёт те же кривые записи.

**Enforcement:** `financial_dml_guard.py` (PreToolUse hook) блокирует DML на protected tables. Bypass: `CLAUDE_BOOSTER_DML_ALLOWED=1` или `[dml-authorized]` в транскрипте.

### Что это значит на практике для каждого `Edit`/`Write`

Когда Claude пишет/правит функцию из §Scope, он обязан **физически добавить** в код:
1. **Validation block** в начале — проверка типов, диапазонов, инвариантов входа.
2. **Invariant assertions** в теле — то, что должно быть истинно после ключевых шагов.
3. **Output validation** перед `return` (или эквивалент на стыке) — то, что downstream имеет право ожидать.

Не «я при ревью проверю» — а **в коде**, чтобы при следующей сессии (или другим агентом, или человеком) брак сам себя ловил.

**Anti-pattern, который явно запрещён:** «функция работает, валидацию добавим потом / в тестах / на ревью». Валидация — часть функции, не отдельный артефакт. Без guards функция не считается завершённой, даже если основная логика правильна.

## Scope — где это правило кусается

Применяется ко **всем** функциям, путям данных и решениям, которые попадают хотя бы в одну категорию:

1. **Обработка информации** — парсинг, нормализация, дедупликация, агрегация, ETL/ELT.
2. **База данных** — схемы, миграции, INSERT/UPDATE/DELETE-пути, репликация, бэкапы, восстановление, индексы, view'ы, партиции.
3. **Ключевые ML-расчёты** — фичи, пайплайны обучения, инференс, метрики, A/B-сплит, training/serving-skew, scoring, бэк-тесты.
4. **Финансовая / транзакционная логика** — ордеры, комиссии, NAV, reconcile, billing, payments, ledger.
5. **Любая функция, чей выход — вход для другой системы**, особенно если downstream — пользователь, broker, ML-модель, отчётность регулятору, прод-БД.

Если функция не в этом списке (логирование, debug-print, локальный CLI helper) — правило ослаблено до здравого смысла; см. §"Когда можно мягче".

## Three Nos — что это значит на практике

### 1. Не принимай брак (Do not accept)

На входе функции/модуля — **жёсткая валидация контракта**. Нельзя пускать дальше:

- `None`/`NaN`/пустые строки в полях, где их не должно быть, с молчаливым fallback'ом на дефолт.
- DataFrame с неожиданным `dtype` (float вместо decimal в финансовых расчётах — это брак, не "warning").
- Ответ внешнего API без проверки `status_code` и схемы тела.
- DB-row, прочитанный без явного projection и валидации типов.
- Файл без проверки checksum/encoding/version там, где это критично.

**Минимум:** assert / pydantic-модель / `CHECK` constraint в DB / явный `raise ValueError` с описанием контракта. Не `try: ... except: pass`. Не `value or default` на критическом поле.

**Формула:** *«Что я гарантирую о входе? Если гарантия нарушена — fail loud, не fail silent.»*

### 2. Не делай брак (Do not make)

Внутри функции — **детерминизм, идемпотентность, инварианты**:

- Округление в финансах — явное (`Decimal`, `ROUND_HALF_EVEN`), а не неявное float.
- Партиальные fill'ы — агрегация по `order_id`, VWAP — никогда «1 ордер = 1 fill» (см. директивы IBKR).
- Знак комиссии нормализуется (`abs()`), потому что брокер может вернуть отрицательное.
- ML-фичи: train и serve считаются **одной и той же функцией** (никаких "почти одинаковых" реализаций — это training/serving skew, классический брак).
- DB-запись: транзакция + `ON CONFLICT` + reconcile, а не «надеемся, что не было гонки».
- Фракции акций — `floor` для BUY, `ceil` для SELL, иначе reject ордера.

**Формула:** *«Если эту функцию вызвать дважды с одним входом — результат тот же? Если два инстанса параллельно — оба правильны? Если упало посередине — состояние согласовано?»*

### 3. Не передавай брак (Do not pass on)

На выходе функции/перед commit'ом/перед пушем — **контроль на стыке**:

- Reconcile после каждого `apply` — broker vs DB. **Stale snapshot — это передача брака вниз**: reconcile должен **invalidate + rebuild**, не просто append.
- Перед `git push` — `verify_gate` evidence-блок (curl/HTTP/SQL/rows). `localhost` или `|| true` не считаются — это театр.
- Перед `TaskUpdate(completed)` — реальная проверка (curl на прод, pytest, DevTools). Без evidence — `in progress — requires verification`, не `completed`.
- При delegation агенту — передавать **Verified Facts Brief** (код, не отчёт). Reports decay; code is truth.
- Frontend: после edit — `next build`, иначе deploy упадёт молча на Vercel.

**Формула:** *«То, что я отдаю наружу — может ли downstream-узел принять это как валидный вход?»* Если ответ "ну скорее всего" — это брак, его нельзя передавать.

## Andon — право и обязанность остановиться

Если в процессе работы видишь брак, который пришёл не от тебя (memory contradicts code, отчёт врёт, чужая функция возвращает inconsistent state) — **остановись и подними флаг**, не маскируй.

- Конкретные триггеры:
  - Память говорит X, код показывает Y → обнови память, не строй на ней дальше.
  - Тесты падают «иногда» (flaky) → flaky test = брак инфраструктуры, не "ну прогони ещё раз".
  - Миграция оставила inconsistent state → fix forward с reconcile, не «ну живём дальше».
- Anti-pattern: молча обернуть в `try/except` и идти дальше. Это и есть «принять и передать брак».

## Decision lens — короткий чек-лист перед commit'ом

Перед каждым `Edit`/`Write` на функции из §Scope, спроси:

1. **Вход:** что я гарантирую и как валидирую? *(Не принимай брак)*
2. **Тело:** идемпотентно? детерминированно? инварианты сохранены? *(Не делай брак)*
3. **Выход:** что downstream получит, и как я это проверил перед commit'ом? *(Не передавай брак)*

Если на любой вопрос ответ «не знаю» / «надеюсь, что да» — это сигнал спавнить агента-аудитора (`/code-review`, `/security-review`, или PAL `codereview`), а не пушить.

## Когда можно мягче

- Throwaway-скрипты, локальный debug, prototype в /tmp — full rigor избыточен; здравый смысл достаточен.
- Документация, README, отчёты — Three Nos не применяется буквально (там нет "брака" в инженерном смысле; есть качество текста, и это другой axis).
- Логирование/телеметрия (если её отказ не влияет на прод-логику) — best-effort приемлем.

Все остальные пути данных = full rigor.

## Связи с существующими правилами

- `core.md` §Pre-Edit Impact Analysis — Three Nos это **что именно проверять** в трёх вопросах "что зависит / что сломается / обратимо ли".
- `pipeline.md` §AUDIT — `/code-review` + `/security-review` + PAL `codereview` это и есть "не передавай брак" на стыке IMPLEMENT→VERIFY.
- `commands.md` §handover §Verify-gate — JSON evidence-блок это формальная реализация "не передавай брак" на стыке session→commit.

## Anti-patterns (запрещено)

- ❌ `value or default` на критическом поле без логирования причины.
- ❌ `try: ... except Exception: pass` в data-path функции.
- ❌ "Это работает на dev, на prod проверим если упадёт" — это план передачи брака.
- ❌ Принять отчёт/память как факт без cross-check с кодом, когда контекст важен.
- ❌ Закрыть task как `completed` без evidence — это передача брака следующей сессии.
- ❌ Молча обернуть упавший тест в `@pytest.mark.skip` без issue/follow-up.

## Origin

Toyota Production System — Jidoka (自働化) / Jikotei Kanketsu (自工程完結, "self-process completion"). Каждый узел отвечает за качество того, что отдаёт следующему; никто не передаёт брак вниз по конвейеру; любой имеет право и обязанность остановить линию (Andon cord) при обнаружении дефекта. В софт-инженерии переводится напрямую: каждая функция — узел; контракт на входе и выходе — стык; CI/тесты/audit — Andon.
