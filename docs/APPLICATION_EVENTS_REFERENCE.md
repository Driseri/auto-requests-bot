# Технический справочник событий заявок

## 1. Назначение

Документ описывает фактический контракт событий, которые production-код записывает
в SQLite-таблицу `application_events`. События используются для продуктовых метрик,
воронок, расчёта длительности этапов и диагностики движения заявок.

`application_events` не является:

- полным аудитом всех действий пользователя;
- заменой рабочих Google Sheets;
- очередью доставки Telegram-уведомлений;
- журналом Docker-логов;
- источником полного состояния заявки.

Очереди `notification_outbox` и `dashboard_outbox` имеют собственные `event_type`
и не входят в каталог этого документа.

## 2. Физическая схема

Таблица создаётся автоматически при инициализации репозитория:

```sql
CREATE TABLE application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT,
    telegram_user_id INTEGER,
    event_type TEXT NOT NULL,
    event_at TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);
```

Индексы:

```sql
CREATE INDEX idx_application_events_application
ON application_events(application_id, event_type, event_at);

CREATE INDEX idx_application_events_type_time
ON application_events(event_type, event_at);

CREATE INDEX idx_application_events_user_time
ON application_events(telegram_user_id, event_at);
```

Внешних ключей и уникального ограничения на событие нет. Идемпотентность
обеспечивается бизнес-логикой конкретного producer, а не схемой таблицы.

## 3. Общие поля

| Поле | SQLite | Обязательность | Контракт |
|---|---|---|---|
| `id` | `INTEGER` | всегда | Автоинкрементный технический идентификатор строки. Не является временем события и не используется как ID заявки. |
| `application_id` | `TEXT` | зависит от события | Восьмисимвольный ID заявки/черновика. Схема допускает `NULL` для системных событий, хотя текущие события заявки обычно имеют ID. |
| `telegram_user_id` | `INTEGER` | зависит от события | Telegram ID сценариста, которому принадлежит заявка. Может быть `NULL`, если tracking уже удалён или событие создано системной операцией без пользователя. |
| `event_type` | `TEXT` | всегда | Стабильный машинный код события из каталога ниже. Для аналитики сравнивается точной строкой. |
| `event_at` | `TEXT` | всегда | Бизнес-время события в ISO 8601 UTC, например `2026-07-17T09:15:30.123456+00:00`. |
| `old_value` | `TEXT` | необязательно | Предыдущее значение. Смысл и формат зависят от `event_type`; иногда это JSON-строка. |
| `new_value` | `TEXT` | необязательно | Новое значение, результат операции или диагностический счётчик. Смысл зависит от `event_type`. |
| `metadata_json` | `TEXT` | необязательно | JSON object с техническим контекстом. Версионируемые события содержат `schema_version`. |
| `created_at` | `TEXT` | всегда | Время фактической вставки события в SQLite в ISO 8601 UTC. |

### `event_at` и `created_at`

- Для `application_submitted` в `event_at` используется сохранённое время отправки
  заявки (`submitted_at`), если оно известно.
- Для polling, индексирования, удаления и LLM-проверок `event_at` обычно равно
  времени обнаружения/обработки события ботом.
- `created_at` показывает время записи в SQLite. Для отложенных или восстановленных
  операций оно может отличаться от `event_at`.
- Временные KPI следует считать по `event_at`.

## 4. Каталог событий

### 4.1 `draft_started`

Создаётся при создании нового черновика. Повторный `get_or_create()` активного
черновика событие не создаёт. После удаления/завершения черновика новый черновик
получает новый `application_id` и новое событие.

| Поле | Значение |
|---|---|
| `application_id` | ID нового черновика |
| `telegram_user_id` | автор черновика |
| `old_value` | `NULL` |
| `new_value` | `NULL` |
| `event_at` | время создания черновика |

`metadata_json`:

| Ключ | Тип | Описание |
|---|---|---|
| `current_step` | `string` | Начальный FSM-шаг, сейчас `direction`. |
| `application_type` | `string` | Тип flow, например `SINGLE` или значение массового flow. |

### 4.2 `application_submitted`

Создаётся после успешной регистрации заявки в Google Sheets и tracking. Используется
для одиночной заявки, обеих независимых строк CHIPS + ADD/EDIT и каждой
зарегистрированной строки нового массового резерва.

| Поле | Значение |
|---|---|
| `application_id` | ID зарегистрированной строки |
| `telegram_user_id` | автор заявки/резерва |
| `old_value` | `NULL` |
| `new_value` | `NULL` |
| `event_at` | `submitted_at`, иначе время фиксации |

`metadata_json`:

| Ключ | Тип | Nullable | Описание |
|---|---|---|---|
| `spreadsheet_id` | `string` | да | ID Google spreadsheet. |
| `sheet_id` | `integer` | да | Числовой `gid` листа. |
| `sheet_name` | `string` | да | Имя вкладки на момент отправки. |
| `row_number` | `integer` | да | Номер строки, начиная с 1. |
| `direction` | `string` | да | Направление заявки. |
| `answer_type` | `string` | да | Тип ответа/маршрут: раскатка, срочные, интеграции и т. п. |
| `application_type` | `string` | да | Обычно `SINGLE`; новые bulk reservation строки также регистрируются как одиночные. |
| `change_type` | `string` | да | `ADD`, `EDIT` или `CHIPS`. |
| `is_urgent` | `boolean` | да | Признак срочности. |

### 4.3 `application_indexed`

Создаётся, когда заявка впервые получает полные координаты Sheets либо её сохранённое
положение меняется. Повторное сохранение тех же координат событие не создаёт.

Текущие producer-пути: `save_submitted_application()` и административный
`index_submitted_application()`. Обычные `complete_submission()`, linked
CHIPS + ADD/EDIT и регистрация нового bulk reservation записывают координаты сразу
вместе с `application_submitted`, но отдельный `application_indexed` в этих путях
сейчас не создают. Поэтому наличие `application_indexed` нельзя считать обязательным
для каждой исторической или новой заявки до унификации producer-путей.

| Поле | Значение |
|---|---|
| `application_id` | ID индексируемой заявки |
| `telegram_user_id` | владелец tracking |
| `old_value` | `NULL` при первой индексации либо JSON array старых координат |
| `new_value` | JSON array новых координат |
| `event_at` | время подтверждения координат |

Формат координат в `old_value/new_value`:

```json
["spreadsheet_id", 123456789, "Срочные", 42]
```

Порядок элементов фиксирован: `spreadsheet_id`, `sheet_id`, `sheet_name`,
`row_number`.

`metadata_json`:

| Ключ | Тип | Nullable | Описание |
|---|---|---|---|
| `spreadsheet_id` | `string` | нет для созданного события | Новая таблица. |
| `sheet_id` | `integer` | нет | Новый физический лист. |
| `sheet_name` | `string` | нет | Новое имя листа. |
| `row_number` | `integer` | нет | Новая строка. |
| `direction` | `string` | да | Направление. |
| `answer_type` | `string` | да | Тип ответа. |
| `change_type` | `string` | да | Тип изменения. |
| `is_urgent` | `boolean` | да | Срочность. |

### 4.4 `status_changed`

Создаётся polling-циклом при изменении статуса строки.

| Поле | Значение |
|---|---|
| `old_value` | предыдущий статус |
| `new_value` | текущий статус из Sheets |
| `event_at` | время обнаружения изменения polling-циклом |

Общий `metadata_json` событий polling:

| Ключ | Тип | Nullable | Описание |
|---|---|---|---|
| `spreadsheet_id` | `string` | да | Таблица заявки. |
| `sheet_id` | `integer` | да | Физический лист. |
| `sheet_name` | `string` | да | Имя вкладки. |
| `row_number` | `integer` | да | Строка на момент чтения. |
| `direction` | `string` | да | Направление. |
| `answer_type` | `string` | да | Тип ответа. |
| `change_type` | `string` | да | `ADD`, `EDIT`, `CHIPS`. |
| `batch_id` | `string` | да | ID legacy-пачки; у новых bulk reservations пустой. |

### 4.5 `editor_changed`

Создаётся polling-циклом при назначении или изменении редактора.

| Поле | Значение |
|---|---|
| `old_value` | предыдущее имя/значение редактора |
| `new_value` | текущее имя/значение редактора |
| `metadata_json` | общий контекст polling из раздела 4.4 |

### 4.6 `editor_comment_added`

Создаётся после стабильного чтения поля `Вопросы/комментарии редактора` в трёх
polling-циклах. Изменённый текст создаёт новое событие.

| Поле | Значение |
|---|---|
| `old_value` | ранее зафиксированный полный комментарий |
| `new_value` | новый полный комментарий редактора |
| `metadata_json` | общий контекст polling из раздела 4.4 |

Важно: полный текст комментария хранится в событии. Это не относится к защищённой
телеметрии LLM и требует ограничения доступа к production SQLite и backup-файлам.

### 4.7 `final_answer_added`

Создаётся при первом появлении или изменении поля `Итоговый ответ редактора`.

| Поле | Значение |
|---|---|
| `old_value` | предыдущий полный итоговый ответ |
| `new_value` | новый полный итоговый ответ |
| `metadata_json` | общий контекст polling из раздела 4.4 |

Для CHIPS итоговый ответ может отсутствовать по схеме, поэтому событие не создаётся.
Полный итоговый текст хранится в `old_value/new_value`.

### 4.8 `scriptwriter_response_added`

Создаётся после стабильного чтения поля `Ответ сценариста` в трёх polling-циклах.
Статус заявки не является условием события. Изменение ответа создаёт новое событие.

| Поле | Значение |
|---|---|
| `old_value` | предыдущий полный ответ сценариста |
| `new_value` | новый полный ответ сценариста |
| `metadata_json` | общий контекст polling из раздела 4.4 |

Полный ответ сценариста хранится в `old_value/new_value`.

### 4.9 `application_not_found`

Создаётся при подтверждённом цикле, в котором tracked-заявка не найдена. Событие
может повторяться для одной заявки и не означает, что строка физически удалена.

| Поле | Значение |
|---|---|
| `old_value` | `NULL` |
| `new_value` | строковое представление текущего `not_found_count`, например `"3"` |
| `event_at` | время подтверждённого промаха поиска |

`metadata_json`:

| Ключ | Тип | Nullable | Описание |
|---|---|---|---|
| `threshold` | `integer` | нет | Порог перевода в `NOT_FOUND`. |
| `recheck_seconds` | `integer` | нет | Интервал следующей полной проверки после достижения порога. |
| `next_status_check_at` | `string` | да | ISO 8601 UTC следующей проверки. |
| `polling_state` | `string` | нет | Итоговое состояние tracking, например `ACTIVE` или `NOT_FOUND`. |

### 4.10 `application_deletion_error`

Создаётся при ошибке или небезопасном состоянии управляемого удаления через статус
`Удаление`.

| Поле | Значение |
|---|---|
| `old_value` | `NULL` |
| `new_value` | техническое описание ошибки, обрезанное до 1000 символов |
| `metadata_json` | `NULL` |

Событие может повторяться при каждой неуспешной попытке. Текст ошибки не должен
использоваться как стабильный машинный код.

### 4.11 `application_deleted`

Создаётся в транзакции удаления tracking после успешного Google `deleteDimension`.
История прежних событий заявки не удаляется.

| Поле | Значение |
|---|---|
| `old_value` | `NULL` |
| `new_value` | `NULL` |
| `event_at` | время SQLite-фиксации удаления |

`metadata_json`:

| Ключ | Тип | Описание |
|---|---|---|
| `spreadsheet_id` | `string` | Таблица, из которой удалена строка. |
| `sheet_id` | `integer` | Физический лист. |
| `deleted_row_number` | `integer` | Номер удалённой строки до row-shift. |

### 4.12 `llm_check_completed`

Создаётся для каждой фактически выполненной логической проверки одиночной ADD/EDIT
заявки через GigaChat: первоначальной и повторной после выбора «Дополнить».
Редактирование поля из review не запускает новую проверку. Внутренние HTTP-повторы
не создают отдельные строки.

Не создаётся для CHIPS, массовых заявок и автоматически сформированной ADD/EDIT
строки после CHIPS.

| Поле | Значение |
|---|---|
| `old_value` | `NULL` |
| `new_value` | `passed`, `needs_clarification` или `technical_fallback` |
| `event_at` | время завершения логической проверки |

`metadata_json`, schema version 1:

| Ключ | Тип | Nullable | Описание |
|---|---|---|---|
| `schema_version` | `integer` | нет | Версия контракта metadata, сейчас `1`. |
| `stage` | `string` | нет | `initial` или `clarification`. |
| `trigger` | `string` | нет | Для V5 — `create`; `edit` встречается только в исторических событиях старого flow. |
| `prompt_version` | `string` | нет | Версия из имени system prompt, например `v5`; для совместимого тестового клиента `unknown`. |
| `prompt_hash` | `string` | да | Первые 12 hex-символов SHA-256 исходных system/user шаблонов. Пользовательские данные не входят. |
| `model` | `string` | да | Запрошенная модель GigaChat. |
| `blocking_rule` | `string` | да | `1.1`, `1.2`, `2.1`, `3.1` либо `null`. |
| `gap_code` | `string` | да | Код пробела V5 либо `null`. |
| `duration_ms` | `integer` | да | Полная длительность логической проверки в миллисекундах. |
| `response_attempts` | `integer` | да | Число внешних попыток получить/разобрать ответ; `0` при `not_configured`. |
| `validation_retries` | `integer` | да | Число повторов после невалидного ответа. |
| `error_kind` | `string` | да | Нормализованная ошибка; при успехе `null`. |

Допустимые `error_kind`:

- `not_configured`;
- `timeout`;
- `network`;
- `auth`;
- `rate_limit`;
- `provider_error`;
- `empty_response`;
- `invalid_json`;
- `schema_validation`;
- `truncated_response`;
- `unknown`.

В событии не сохраняются тексты заявки, шаблонов, отрендеренных промптов, ответа
GigaChat, blocking problem или инструкции пользователю.

### 4.13 `llm_clarification_submitted`

Совместимое краткое событие создаётся после получения полной новой версии поля
«Суть изменений» и до второго вызова GigaChat.

| Поле | Значение |
|---|---|
| `old_value` | `NULL` |
| `new_value` | `NULL` |
| `event_at` | время принятия ответа ботом |

`metadata_json`, schema version 1:

| Ключ | Тип | Описание |
|---|---|---|
| `schema_version` | `integer` | `1`. |
| `clarification_number` | `integer` | Совместимый номер повторной проверки, для V5 всегда `1`. |
| `trigger` | `string` | Для V5 всегда `create`. |

Текст, preview и длина новой версии не сохраняются. Из-за порядка записи возможно
наличие `llm_clarification_submitted` без следующего `llm_check_completed`, если
процесс аварийно завершился перед фиксацией результата повторной проверки.

### 4.14 `llm_recommendation_processes`

Это не event stream, а подробное состояние рекомендательной проверки. На один
`application_id` существует ровно одна строка. Колонка `state` позволяет быстро
определить текущее состояние без разбора JSON. `process_json` содержит контекст,
исходную и актуальную версии поля, до двух элементов `iterations`, сырой и
структурированный ответы, prompt/model telemetry, технические ошибки и действия
`add`, `skip`, `cancelled` или `restarted`. Для `skip` причина принимает
`optional`, `incorrect` или `unclear`. Секреты и полные тексты prompt-шаблонов
не сохраняются. Сводные `application_events` остаются обезличенными.

## 5. Порядок событий жизненного цикла

Типичный ADD/EDIT без рекомендации:

```text
draft_started
llm_check_completed(new_value=passed, stage=initial, trigger=create)
application_submitted
application_indexed (только если сработал отдельный producer индексации)
status_changed / editor_changed / editor_comment_added
final_answer_added
```

ADD/EDIT с рекомендацией и полной новой версией:

```text
draft_started
llm_check_completed(new_value=needs_clarification, stage=initial)
llm_clarification_submitted
llm_check_completed(stage=clarification)
application_submitted
...
```

CHIPS:

```text
draft_started
application_submitted
application_indexed (не гарантирован для текущего submission path)
...
```

Между `application_submitted` и `application_indexed` нет гарантии одинакового
времени и вообще наличия второго события: индексация фиксирует отдельный producer-path
подтверждения координат.

## 6. Атомарность и повторяемость

| Группа | Атомарность |
|---|---|
| `draft_started` | Одна транзакция с созданием черновика. |
| `application_submitted` | Одна транзакция с tracking, завершением draft/reservation, dashboard projection и нужным notification outbox. |
| `application_indexed` | Одна транзакция с сохранением/обновлением координат. |
| polling-события | Одна транзакция с обновлением tracking и, если требуется, notification/dashboard outbox. |
| `llm_check_completed` | Одна транзакция с обновлением draft, единственной строки `llm_recommendation_processes` и совместимого события. |
| `llm_clarification_submitted` | Записывается отдельно перед повторной проверкой. |
| `application_deleted` | Одна транзакция с удалением tracking/outbox, dashboard delete projection и row-shift. |

Таблица не запрещает дубликаты. При аналитике необходимо учитывать семантику:

- `application_not_found` и `application_deletion_error` по определению повторяемы;
- изменённый комментарий, ответ или итоговый текст создаёт новое событие;
- `application_indexed` повторяется при изменении координат;
- `llm_check_completed` встречается не более двух раз для V5: первоначальная проверка и проверка полной новой версии; review-edit новую строку не создаёт;
- количество заявок считается по уникальному `application_id`, а не по числу всех
  событий.

## 7. Данные и доступ

Сводная LLM-телеметрия в `application_events` спроектирована без пользовательского
содержимого. Защищённая строка `llm_recommendation_processes` намеренно содержит
тексты пользователя и сырой ответ модели для аудита; доступ к БД и резервным копиям
должен быть ограничен. Текущие polling-события также содержат полные рабочие значения:

- `editor_comment_added` — комментарий редактора;
- `final_answer_added` — итоговый ответ;
- `scriptwriter_response_added` — ответ сценариста;
- `editor_changed` — имя/значение редактора.

Поэтому `app.db`, backup БД и выгрузки событий следует считать внутренними рабочими
данными. Их нельзя публиковать или передавать во внешние системы без отдельной
проверки и обезличивания.

Автоматического TTL/retention для `application_events` сейчас нет. Удаление заявки
из tracking не удаляет её событийную историю.

## 8. События, которых production-код сейчас не создаёт

Следующие названия могут встречаться в старых dashboard-заготовках или обсуждениях,
но не являются текущими producer-событиями `application_events`:

- `user_comment_added`;
- `clarification_requested`;
- `status_final_answer_ready`.

`application-status`, `urgent-editor-application-created`,
`urgent-editor-scriptwriter-response`, `urgent-editor-bulk-reservation-created` и
`bulk-batch-status` относятся к `notification_outbox`, а не к
`application_events`.

## 9. Контрольные SQL-запросы

### Объём и период по типам

```sql
SELECT event_type,
       COUNT(*) AS event_count,
       COUNT(DISTINCT application_id) AS application_count,
       MIN(event_at) AS first_event_at,
       MAX(event_at) AS last_event_at
FROM application_events
GROUP BY event_type
ORDER BY event_type;
```

### История заявки

```sql
SELECT id, application_id, telegram_user_id, event_type, event_at,
       old_value, new_value, metadata_json, created_at
FROM application_events
WHERE application_id = :application_id
ORDER BY event_at, id;
```

### Результаты LLM по версии промпта

```sql
SELECT json_extract(metadata_json, '$.prompt_version') AS prompt_version,
       new_value AS outcome,
       COUNT(*) AS checks
FROM application_events
WHERE event_type = 'llm_check_completed'
GROUP BY prompt_version, outcome
ORDER BY prompt_version, outcome;
```

### Технические ошибки LLM

```sql
SELECT json_extract(metadata_json, '$.error_kind') AS error_kind,
       COUNT(*) AS errors
FROM application_events
WHERE event_type = 'llm_check_completed'
  AND new_value = 'technical_fallback'
GROUP BY error_kind
ORDER BY errors DESC;
```

### Проверки, прошедшие с первого раза

```sql
WITH initial_create AS (
    SELECT application_id,
           new_value,
           ROW_NUMBER() OVER (
               PARTITION BY application_id
               ORDER BY event_at, id
           ) AS rn
    FROM application_events
    WHERE event_type = 'llm_check_completed'
      AND json_extract(metadata_json, '$.stage') = 'initial'
      AND json_extract(metadata_json, '$.trigger') = 'create'
)
SELECT SUM(CASE WHEN new_value = 'passed' THEN 1 ELSE 0 END) AS passed_first,
       COUNT(*) AS checked_applications,
       ROUND(
           100.0 * SUM(CASE WHEN new_value = 'passed' THEN 1 ELSE 0 END)
           / NULLIF(COUNT(*), 0),
           1
       ) AS first_pass_percent
FROM initial_create
WHERE rn = 1;
```

### Проверка качества полей события

```sql
SELECT id, application_id, event_type, event_at, metadata_json
FROM application_events
WHERE event_type = ''
   OR event_at = ''
   OR event_at IS NULL
   OR (
       application_id IS NOT NULL
       AND application_id NOT GLOB '[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]'
   )
ORDER BY id DESC;
```

Production-проверки следует выполнять через read-only соединение и
`PRAGMA query_only=ON`.

## 10. Изменение контракта

- Новый `event_type` добавляется только вместе с документацией producer, полей и
  правил повторяемости.
- При изменении структуры `metadata_json` необходимо увеличить `schema_version`.
- Значение существующего поля нельзя переопределять задним числом без миграции
  consumers.
- Dashboard должен считать неизвестный `event_type` допустимым и игнорировать его,
  пока не добавлена явная поддержка.
- Исторические события не достраиваются автоматически после появления нового
  producer.
