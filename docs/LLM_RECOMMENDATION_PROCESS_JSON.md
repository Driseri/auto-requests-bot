# Структура процесса рекомендательной LLM-проверки

## Назначение

Таблица SQLite `llm_recommendation_processes` хранит подробный аудит
рекомендательной проверки полноты поля «Суть изменений».

Одна строка таблицы соответствует одной заявке (`application_id`). Весь цикл —
исходный текст, до двух обращений к LLM, ответы модели, действия пользователя и
итог — хранится в колонке `process_json`.

Это основной источник подробных данных о проверке. Обезличенные записи
`llm_check_completed` в `application_events` предназначены для мониторинга и не
содержат полного процесса.

## Структура строки SQLite

| Колонка | Тип SQLite | Обязательность | Описание |
|---|---|---:|---|
| `application_id` | `TEXT` | да | Первичный ключ и идентификатор заявки. На заявку существует не более одной строки процесса. |
| `telegram_user_id` | `INTEGER` | да | Telegram ID пользователя, создавшего черновик. |
| `field_code` | `TEXT` | да | Код проверяемого поля. Сейчас всегда `change_description`. |
| `state` | `TEXT` | да | Текущее состояние процесса, вынесенное отдельно для поиска без разбора JSON. |
| `process_json` | `TEXT` | да | Сериализованный JSON полного процесса. |
| `created_at` | `TEXT` | да | Время создания процесса в UTC, ISO 8601. |
| `updated_at` | `TEXT` | да | Время последнего обновления процесса в UTC, ISO 8601. |

Значения `application_id`, `telegram_user_id`, `field_code` и `state`
дублируются внутри `process_json`. Это позволяет JSON оставаться
самодостаточным при отдельной выгрузке, а колонкам таблицы — обеспечивать
быстрый поиск.

## Верхний уровень `process_json`

Текущая версия схемы — `1`.

| Поле | Тип | Допускает `null` | Описание |
|---|---|---:|---|
| `schema_version` | `integer` | нет | Версия структуры JSON. Сейчас `1`. |
| `application_id` | `string` | нет | Идентификатор заявки. Совпадает с колонкой таблицы. |
| `telegram_user_id` | `integer` | нет | Telegram ID пользователя. |
| `field_code` | `string` | нет | Проверяемое поле. Сейчас `change_description`. |
| `state` | `string` | нет | Текущее или конечное состояние процесса. |
| `started_at` | `string` | нет | Время начала первой проверки в UTC, ISO 8601. |
| `completed_at` | `string` | да | Время закрытия процесса. До завершения равно `null`. |
| `initial_text` | `string` | нет | Полная исходная версия поля перед первой проверкой. |
| `current_text` | `string` | нет | Последняя сохранённая версия поля. После дополнения содержит исходную версию и новый фрагмент, присоединённый с новой строки. |
| `context` | `object` | нет | Снимок контекста заявки на момент запуска проверки. |
| `iterations` | `array` | нет | От одной до двух бизнес-итераций проверки. |
| `actions` | `array` | нет | Действия пользователя в процессе обработки рекомендации. |
| `final_outcome` | `string` | да | Итог процесса. До завершения равен `null`. |

## Состояния `state`

| Значение | Значение процесса |
|---|---|
| `checking` | Выполняется обращение к LLM. |
| `awaiting_action` | Первая итерация вернула рекомендацию; ожидается выбор «Дополнить» или «Пропустить». |
| `awaiting_revision` | Пользователь выбрал «Дополнить»; ожидается новый фрагмент для добавления к сохранённому полю. |
| `awaiting_skip_reason` | Пользователь выбрал «Пропустить»; ожидается причина пропуска. |
| `completed` | Проверка штатно завершена после `ok`, технического fallback или второй итерации. |
| `skipped` | Пользователь пропустил рекомендацию и выбрал причину. |
| `cancelled` | Черновик отменён либо перезапущен до штатного завершения процесса. |

Колонка `state` таблицы и поле `process_json.state` должны содержать одинаковое
значение.

## Итоги `final_outcome`

| Значение | Описание |
|---|---|
| `null` | Процесс ещё не завершён. |
| `ok` | LLM вернула `ok` на первой или второй итерации. |
| `technical_fallback` | Проверка завершилась технической ошибкой; заполнение заявки продолжено. |
| `recommendation_after_limit` | После второй и последней итерации рекомендация осталась. |
| `skipped_optional` | Пользователь считает рекомендацию необязательной. |
| `skipped_incorrect` | Пользователь считает рекомендацию неправильной. |
| `skipped_unclear` | Рекомендация непонятна пользователю. |
| `cancelled` | Пользователь отменил черновик. |
| `restarted` | Пользователь начал новую заявку вместо незавершённой. |

Состояние и итог описывают разные аспекты. Например, при `restarted`
состояние равно `cancelled`, а `final_outcome` равно `restarted`.

## Объект `context`

`context` — снимок полей заявки, полезных для последующего анализа.

| Поле | Тип | Описание |
|---|---|---|
| `direction` | `string` | Направление заявки. |
| `answer_type` | `string` | Тип ответа или заявки. |
| `change_type` | `string` | Тип изменения, например `ADD` или `EDIT`. |
| `intent` | `string` | Интент заявки. |
| `reason` | `string` | Значение поля «Кейс или сообщения клиента», передаваемое в V6.1+ как `client_case`. |

Если значение отсутствует в черновике, сохраняется пустая строка.

## Массив `actions`

Массив содержит только действия пользователя, относящиеся к рекомендации.
Пустой массив означает, что результат первой проверки не потребовал выбора
пользователя либо выбор ещё не сделан.

### Действие `add`

Создаётся после выбора «Дополнить».

| Поле | Тип | Обязательность | Описание |
|---|---|---:|---|
| `action` | `string` | да | Всегда `add`. |
| `selected_at` | `string` | да | Время выбора «Дополнить». |
| `submitted_at` | `string` | после ввода | Время отправки нового фрагмента. |
| `text_changed` | `boolean` | после ввода | Отличается ли новая версия от предыдущей. |
| `text_delta_chars` | `integer` | после ввода | `len(combined_text) - len(previous_text)`. Обычно включает перенос строки и длину дополнения. |

Если процесс отменён после выбора «Дополнить», но до отправки текста, поля
`submitted_at`, `text_changed` и `text_delta_chars` отсутствуют.

### Действие `skip`

Создаётся после выбора причины пропуска.

| Поле | Тип | Описание |
|---|---|---|
| `action` | `string` | Всегда `skip`. |
| `reason` | `string` | `optional`, `incorrect` или `unclear`. |
| `selected_at` | `string` | Время сохранения причины. |

### Действия `cancelled` и `restarted`

Создаются при закрытии незавершённого процесса.

| Поле | Тип | Описание |
|---|---|---|
| `action` | `string` | `cancelled` или `restarted`. |
| `selected_at` | `string` | Время закрытия процесса. |

## Массив `iterations`

Каждый элемент соответствует одной бизнес-проверке. Внутренние повторные
HTTP-запросы из-за невалидного ответа не создают новые элементы массива и
учитываются в `technical`.

Для проверяемой заявки допускается не более двух элементов:

1. первоначальная проверка;
2. проверка объединённого текста после выбора «Дополнить».

### Поля итерации

| Поле | Тип | Допускает `null` | Описание |
|---|---|---:|---|
| `check_id` | `string` | нет | Уникальный UUID конкретной бизнес-итерации. |
| `number` | `integer` | нет | Номер итерации: `1` или `2`. |
| `started_at` | `string` | нет | Время начала итерации. |
| `completed_at` | `string` | да | Время получения или формирования итогового результата. |
| `input` | `object` | нет | Снимок входных данных итерации. |
| `response` | `object` | да | Нормализованный результат. До завершения равен `null`. |
| `raw_response` | `string` | да | Ответ модели без преобразований. Может быть невалидным JSON. |
| `parse_status` | `string` | нет | `pending`, `valid` или `error`. |
| `technical` | `object` | да | Техническая телеметрия. До завершения равна `null`. |
| `llm` | `object` | да | Модель, идентификатор промптов и параметры генерации. До завершения равен `null`. |

## Объект `iterations[].input`

| Поле | Тип | Допускает `null` | Описание |
|---|---|---:|---|
| `initial_text` | `string` | нет | Исходная версия поля. Повторяется в обеих итерациях для аудита. |
| `current_text` | `string` | нет | Версия поля, проверяемая в этой итерации. |
| `previous_gap_code` | `string` | да | Код пробела первой итерации. На первой итерации `null`. |
| `previous_recommendation` | `string` | да | Legacy-рекомендация контрактов V1–V5. Для V6.1+ обычно `null`. |
| `previous_missing_detail` | `string` | да | Сырой `missing_detail` первой итерации. На первой итерации `null`. |

На второй итерации `current_text` содержит сохранённую версию поля и новый
пользовательский фрагмент, присоединённый через один перенос строки.
`previous_gap_code` и `previous_missing_detail` фиксируют единственный пробел,
устранение которого должна проверить модель.

## Объект `iterations[].response`

| Поле | Тип | Допускает `null` | Описание |
|---|---|---:|---|
| `check_result` | `string` | нет | `ok`, `recommendation` или `error`. |
| `gap_code` | `string` | да | Аналитический код пробела. Для `ok` и `error` обычно `null`. |
| `missing_detail` | `string` | да | Недостающее сведение из контрактов V6.1+. Хранится без пользовательского префикса. |
| `recommendation` | `string` | да | Legacy-текст рекомендации контрактов V1–V5. В V6.1+ обычно `null`. |

Допустимые V6.1+ значения `gap_code`:

- `missing_change_action`;
- `missing_change_content`;
- `missing_new_entity_content`;
- `missing_application_context`;
- `missing_change_rationale`.

Поля `missing_detail` и `recommendation` не подменяют друг друга. Это позволяет
сохранить совместимость со старыми промптами и одновременно анализировать
новый контракт.

## Поле `iterations[].raw_response`

`raw_response` хранит точный текст, полученный от модели. При корректном ответе
это обычно JSON, сериализованный как строка:

```json
{
  "raw_response": "{\n  \"check_result\": \"ok\",\n  \"gap_code\": null,\n  \"missing_detail\": null\n}"
}
```

Нормализованная копия тех же данных находится в `response`. Такое дублирование
намеренно:

- `raw_response` нужен для аудита, диагностики парсинга и проверки поведения
  конкретной версии модели;
- `response` удобен для программной обработки и аналитики.

При технической ошибке `raw_response` может быть `null`, пустой строкой или
невалидным JSON.

## Объект `iterations[].llm`

| Поле | Тип | Допускает `null` | Описание |
|---|---|---:|---|
| `model` | `string` | да | Запрошенная модель GigaChat, например `GigaChat-2-Pro`. |
| `prompt_version` | `string` | нет | Версия, извлечённая из имени системного промпта, например `v6.2`. |
| `prompt_hash` | `string` | да | Первые 12 символов SHA-256 от system- и user-шаблонов промпта. |
| `generation_parameters` | `object` | нет | Зафиксированные параметры генерации. |

`generation_parameters`:

| Поле | Тип | Текущее значение |
|---|---|---|
| `temperature` | `number` | `0.01` |
| `response_format` | `string` | `json_schema` |

Полные тексты промптов и credentials в процессе не сохраняются.

## Объект `iterations[].technical`

| Поле | Тип | Допускает `null` | Описание |
|---|---|---:|---|
| `duration_ms` | `integer` | да | Полная длительность бизнес-итерации в миллисекундах. |
| `response_attempts` | `integer` | да | Количество обращений к модели внутри итерации. |
| `validation_retries` | `integer` | да | Количество внутренних повторов после ошибки JSON или схемы. |
| `error_kind` | `string` | да | Нормализованный вид технической ошибки. При успехе `null`. |

`parse_status` связан с техническим результатом:

- `pending` — итерация создана, ответ ещё не зафиксирован;
- `valid` — структурированный ответ прошёл проверку;
- `error` — применён технический fallback.

## Жизненный цикл одной строки

Типичный процесс без рекомендации:

```text
checking
  -> completed (final_outcome=ok)
```

Процесс с дополнением:

```text
checking
  -> awaiting_action
  -> awaiting_revision
  -> checking
  -> completed (final_outcome=ok или recommendation_after_limit)
```

Процесс с пропуском:

```text
checking
  -> awaiting_action
  -> awaiting_skip_reason
  -> skipped (final_outcome=skipped_optional,
              skipped_incorrect или skipped_unclear)
```

Отмена или перезапуск незавершённой заявки:

```text
checking / awaiting_action / awaiting_revision / awaiting_skip_reason
  -> cancelled (final_outcome=cancelled или restarted)
```

Строка не удаляется после штатного завершения, отмены или перезапуска. При
изменении процесса существующая строка обновляется по `application_id`.

## Атомарность

Результат LLM-проверки сохраняется в одной SQLite-транзакции вместе с:

- актуальным состоянием черновика;
- строкой `llm_recommendation_processes`;
- совместимым событием `llm_check_completed`.

Промежуточные выборы пользователя также обновляют ту же строку процесса.

## Компактный пример двух итераций

Ниже показана сокращённая по значениям, но структурно полная строка:

```json
{
  "application_id": "B6450B6F",
  "telegram_user_id": 927523610,
  "field_code": "change_description",
  "state": "completed",
  "process_json": {
    "schema_version": 1,
    "application_id": "B6450B6F",
    "telegram_user_id": 927523610,
    "field_code": "change_description",
    "state": "completed",
    "started_at": "2026-07-30T12:41:57+00:00",
    "completed_at": "2026-07-30T12:43:38+00:00",
    "initial_text": "Создать новый ответ",
    "current_text": "Создать новый ответ с информацией про код",
    "context": {
      "direction": "ФЛ",
      "answer_type": "Раскатка",
      "change_type": "EDIT",
      "intent": "intent.code",
      "reason": "Клиент спрашивает, где найти код"
    },
    "iterations": [
      {
        "check_id": "7b1c9c54-92ca-411b-9f33-b6a8d7b1e5b2",
        "number": 1,
        "started_at": "2026-07-30T12:41:57+00:00",
        "completed_at": "2026-07-30T12:42:00+00:00",
        "input": {
          "initial_text": "Создать новый ответ",
          "current_text": "Создать новый ответ",
          "previous_gap_code": null,
          "previous_recommendation": null,
          "previous_missing_detail": null
        },
        "response": {
          "check_result": "recommendation",
          "gap_code": "missing_new_entity_content",
          "missing_detail": "какую информацию должен содержать новый ответ",
          "recommendation": null
        },
        "raw_response": "{\"check_result\":\"recommendation\",\"gap_code\":\"missing_new_entity_content\",\"missing_detail\":\"какую информацию должен содержать новый ответ\"}",
        "parse_status": "valid",
        "technical": {
          "error_kind": null,
          "response_attempts": 1,
          "validation_retries": 0,
          "duration_ms": 3049
        },
        "llm": {
          "model": "GigaChat-2-Pro",
          "prompt_version": "v6.2",
          "prompt_hash": "698582cb8789",
          "generation_parameters": {
            "temperature": 0.01,
            "response_format": "json_schema"
          }
        }
      },
      {
        "check_id": "860e34d5-4735-4375-be71-83780847e6ef",
        "number": 2,
        "started_at": "2026-07-30T12:43:36+00:00",
        "completed_at": "2026-07-30T12:43:38+00:00",
        "input": {
          "initial_text": "Создать новый ответ",
          "current_text": "Создать новый ответ с информацией про код",
          "previous_gap_code": "missing_new_entity_content",
          "previous_recommendation": null,
          "previous_missing_detail": "какую информацию должен содержать новый ответ"
        },
        "response": {
          "check_result": "ok",
          "gap_code": null,
          "missing_detail": null,
          "recommendation": null
        },
        "raw_response": "{\"check_result\":\"ok\",\"gap_code\":null,\"missing_detail\":null}",
        "parse_status": "valid",
        "technical": {
          "error_kind": null,
          "response_attempts": 1,
          "validation_retries": 0,
          "duration_ms": 1717
        },
        "llm": {
          "model": "GigaChat-2-Pro",
          "prompt_version": "v6.2",
          "prompt_hash": "698582cb8789",
          "generation_parameters": {
            "temperature": 0.01,
            "response_format": "json_schema"
          }
        }
      }
    ],
    "actions": [
      {
        "action": "add",
        "selected_at": "2026-07-30T12:42:07+00:00",
        "submitted_at": "2026-07-30T12:43:36+00:00",
        "text_changed": true,
        "text_delta_chars": 22
      }
    ],
    "final_outcome": "ok"
  },
  "created_at": "2026-07-30T12:41:57+00:00",
  "updated_at": "2026-07-30T12:43:38+00:00"
}
```

## Ограничения доступа

`process_json` содержит:

- Telegram ID пользователя;
- исходный и актуальный тексты заявки;
- контекст заявки;
- сырой ответ модели.

Поэтому SQLite-файл, его резервные копии и выгрузки
`llm_recommendation_processes` являются внутренними рабочими данными. Их нельзя
публиковать или передавать во внешние системы без проверки доступа и
обезличивания.

Секреты GigaChat, Telegram-токен, Google credentials и полные тексты промптов в
эту таблицу не записываются.
