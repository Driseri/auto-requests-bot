# Аудит legacy-архитектуры и план очистки

Дата аудита: 27.07.2026.

## Цель и проверенный контур

Документ описывает legacy-механизмы в коде, SQLite, Google Sheets,
конфигурации, тестах и документации. Это не команда на немедленное удаление
данных: очистку необходимо выполнять несколькими релизами.

- локальная кодовая база: commit `cf9ef05`;
- production-контейнер: image `20260715-419f54e`;
- production SQLite и Google Sheets проверялись в read-only режиме;
- локальный worktree до аудита был чистым.

Локальный HEAD и production image различаются. До миграции нужно выбрать
единый целевой commit. Нельзя одновременно переносить данные и удалять
compatibility-код из версии, которая ещё не работала с production-БД.

## Главный вывод

Главный legacy-слой — старая модель массовых заявок `bulk_batches`. Новый UI
её больше не создаёт, старые callbacks закрываются сообщением об отключённом
формате, а batch-specific polling в production выключен через
`legacy_bulk_enabled=False`. При этом в production остаются:

- 5 записей `bulk_batches`, все `REGISTERED`;
- 36 активных `submitted_applications` с непустым `batch_id`;
- 7 записей `bulk_creation_requests`;
- отдельный лист `Массовый ввод` со старой bulk-схемой.

Все 36 старых массовых строк исключаются из точечного polling, потому что
`read_statuses_for()` пропускает записи с `batch_id`, а batch scan выключен.
Они не получают `not_found`, но и не обновляют статус, редактора,
комментарии и уведомления. Это уже функциональный разрыв, который нужно
устранить до удаления legacy-кода.

Все 141 отслеживаемые одиночные заявки используют актуальные layouts:

- 107 ADD/EDIT строк — `WORKSHEET_HEADERS`;
- 34 CHIPS строки — `CHIPS_WORKSHEET_HEADERS`;
- отслеживаемых одиночных строк старых схем не обнаружено;
- расхождений сохранённых координат с фактическими строками не обнаружено.

После отдельной проверки это позволяет убрать старые layouts из runtime
polling, оставив их только в migration/admin-инструментах.

## 1. Старая модель массовых заявок

### Что осталось

В `src/app/bulk.py` одновременно находятся две архитектуры:

- актуальные `GoogleSheetsBulkReservationService` и
  `BulkReservationRegistrar`;
- старые `GoogleSheetsBulkBatchService`, `BulkApplicationRegistrar`,
  `InMemoryBulkBatchService`, headers, форматирование пачек, relocation и
  регистрация строк staging-листа.

Только два главных legacy-класса занимают более 900 строк без учёта helpers,
моделей, repository и polling.

В `src/app/models.py` остаются `BulkBatch`, `BulkCreationRequest`,
`BulkBatchStatus`, `BulkApplicationStatus`, `BulkRegistrationState`,
`BulkBatchLocationState`, `BulkCreationState` и `ApplicationType.BULK`.

В `src/app/repository.py` остаются таблицы `bulk_batches` и
`bulk_creation_requests`, их индексы, startup-migrations, converters и CRUD,
registration, relocation и dashboard-методы.

В `src/app/notifications.py` остаются batch status reader, чтение старых bulk
rows, relocation, архивный scan, batch notifications, batch dashboard
projections и запрет удаления строк с непустым `batch_id`.

В `src/app/submission.py` остаются `BATCH_DASHBOARD_HEADERS`,
`BATCH_DASHBOARD_SHEET_NAME` и batch projections.

### Что реально активно

- Новый flow использует только `bulk_reservations`.
- Метод `create_bulk_batch()` назван по-старому, но создаёт
  `BulkReservation`; его следует переименовать.
- Callback `app:bulk_ready:<batch_id>` вызывает только tombstone-ответ.
- `legacy_bulk_enabled=False`, поэтому batch polling не выполняется.
- 36 production-заявок старой модели остаются `ACTIVE`, но фактически не
  обрабатываются.

### Риски и действия

1. Старые строки выглядят отслеживаемыми, хотя их изменения не читаются.
2. Статус `Удаление` для них не поддержан.
3. Документация обещает совместимость, которой нет в production polling.
4. Изменения общих polling/dashboard-методов могут задеть неактивную ветку.
5. Восемь legacy bulk flow-тестов помечены `skip`; это мёртвое покрытие.

Порядок устранения:

1. Выпустить read-only inventory CLI: `batch_id`, application IDs,
   фактические строки, статусы и координаты.
2. Согласовать судьбу 36 строк:
   - перенести в обычные секции как независимые `SINGLE` с `batch_id=NULL`;
   - явно архивировать и снять с tracking;
   - либо временно вернуть узкий legacy polling до их завершения.
3. Не ограничиваться очисткой `batch_id` в SQLite: строки останутся в bulk
   layout, который обычный point polling не понимает.
4. Проверить нулевое число активных `batch_id`, `ApplicationType.BULK`,
   batch dashboard rows и pending legacy outbox.
5. Оставить tombstone callback на один релиз в изолированном router без
   импорта старого bulk service.
6. Удалить старые services, models, repository methods, readers,
   formatters, projections, skipped tests и документацию.

## 2. Несколько поколений Google Sheets layouts

Runtime распознаёт четыре ADD/EDIT-схемы:

- `WORKSHEET_HEADERS` — актуальная;
- `PREVIOUS_WORKSHEET_HEADERS`;
- `CURRENT_WORKSHEET_HEADERS`;
- `LEGACY_WORKSHEET_HEADERS`.

Для CHIPS распознаются актуальная и `PREVIOUS_CHIPS_WORKSHEET_HEADERS`. Для
старого staging-листа распознаются три поколения bulk headers. Также остаются
marker `CHIPS V2`, upgrade пустой секции, создание новой V2-секции рядом с
заполненной старой, legacy row builders и fallback parser.

В production остаются архивные вкладки `LEGACY_*`, старые `ДД.ММ ср/чт` и
старые staging-листы. Старое имя вкладки само по себе не мешает polling:
tracking использует сохранённые координаты. Исторические вкладки без tracking
не требуют runtime write-support.

### Риски

1. Layout определяется строковым сравнением шапок; ручная правка одного
   заголовка приводит к `SheetConfigurationError`.
2. Point polling перебирает несколько maps; ошибочно совпавший ID в чужой
   колонке может дать неверную интерпретацию.
3. Поколения схем используют разные индексы статуса, редактора, интента и
   технических полей.
4. Возможность записывать в старую схему продлевает её жизнь.
5. `CHIPS V2` превратился из временного migration marker в runtime-контракт.

### Что сделать

1. Создать dry-run по всем вкладкам: layout, строки с ID, tracking и ручные
   отклонения заголовков.
2. Архивные `LEGACY_*` вкладки сделать read-only и исключить из discovery.
3. После нулевого отчёта по tracked legacy layouts оставить в runtime только
   актуальные ADD/EDIT и CHIPS headers.
4. Перенести старые parsers/maps в offline migration module.
5. Запретить новые записи в старые layouts.
6. Удалить `CHIPS V2` upgrade path после миграции соответствующих секций.
7. Старые недельные названия не переименовывать только ради cleanup:
   достаточно завершить tracking или перевести вкладки в архив.

## 3. Старая модель приоритетов

Остаются `Draft.priority`, `drafts.priority`, priority steps, keyboard,
callbacks, enum/field и настройки:

- `GOOGLE_SPREADSHEET_ID`;
- `GOOGLE_SHEET_NAME`;
- `GOOGLE_HIGH_PRIORITY_SHEET_NAME`;
- `GOOGLE_LOW_PRIORITY_SHEET_NAME`;
- `GOOGLE_BULK_SHEET_NAME`.

Из них runtime использует только `GOOGLE_SHEET_NAME` в startup-log.
Production-черновиков с priority и активных priority-step нет.

Это безопасный ранний кандидат:

1. удалить steps, enums, callbacks, keyboard и flow-ветки;
2. удалить неиспользуемые env/settings и misleading startup-log;
3. оставить колонку `drafts.priority` до отдельной SQLite migration;
4. после rollback-окна пересобрать `drafts` без этой колонки.

## 4. Legacy dashboard

Остаются:

- `LEGACY_DASHBOARD_HEADERS` без `Редактор`;
- repair path старой шапки;
- `BATCH_DASHBOARD_HEADERS` и лист `Пачки`;
- `DashboardEntityType.BULK_BATCH`;
- batch projection logic;
- SQL старых пачек в спецификации локального dashboard.

Перед удалением нужно проверить текущую шапку, удалить batch rows явными
dashboard delete events, затем убрать batch entity, headers, projections и
SQL. Старую шапку следует оставить только в migration-команде, а не sync.

## 5. Старые callbacks, keyboards и пользовательские состояния

Остаются:

- `app:bulk_ready:<batch_id>`;
- `KeyboardKind.BULK_CREATED`, `BULK_COMPLETED`,
  `NOTIFICATION_BULK_BACK`;
- cleanup `pending_action=create_bulk_direction:*`;
- старые keyboard builders и response branches.

Активных старых `pending_action` в production нет. Следует оставить один
tombstone-handler, считать его вызовы 30 дней и затем удалить старые keyboard
kinds. Ради одной старой кнопки не нужно сохранять весь registrar/service.

## 6. Старые статусы и aliases

Остаются aliases `NEEDS_SCRIPTWRITER_RESPONSE` и `RESPONSE_RECEIVED`,
отдельные bulk statuses и legacy notification navigation.

Нужно проверить SQLite и Sheets, нормализовать найденные старые значения и
удалить aliases после пустого dry-run. В production SQLite старые
alias-значения не обнаружены.

## 7. Legacy GigaChat

Остаются V1/V2 prompts, `LlmV2ResultSchema`, неиспользуемый
`_legacy_check_change_description()`, выбор schema по имени V2-файла,
`llm_score` и колонка `LLM оценка`.

Production настроен на V3. Рекомендуется:

1. зафиксировать V3 как единственный runtime contract;
2. использовать git tag/image для отката;
3. удалить legacy method, V1/V2 selection и тесты;
4. перенести старые prompts в архив либо удалить;
5. отдельно мигрировать постоянно пустой `llm_score`;
6. не считать автоматически дублями `raw_change_description` и
   `formatted_change_description`: после уточнений у них может быть разная
   семантика.

## 8. Startup-migrations SQLite

Repository при каждом старте создаёт retired bulk tables, проверяет колонки
через `PRAGMA table_info`, добавляет их через `_ensure_*` и выполняет data-fix
для `bulk_batches`. `PRAGMA user_version=1` применяется только к одной старой
LLM-миграции.

Риски:

1. удалённая legacy table будет создана снова, пока её `CREATE TABLE` в коде;
2. старый image после destructive migration может перестать быть rollback;
3. SQLite cleanup требует транзакции и пересборки таблиц;
4. bootstrap и исторические data migrations смешаны.

Нужно ввести последовательные schema versions, разделить создание новой БД,
schema migration и data migration. Destructive cleanup выполнять только
после verified backup, dry-run, сверки counts и завершения rollback-окна.
Исторические `application_events` удалять не нужно.

## 9. Миграционные и административные инструменты

Не следует одновременно удалять runtime compatibility и
`sheet_schema_migration.py`/legacy branches `sheet_indexer.py`. Сначала их
нужно вынести в `tools/migrations/legacy/`, запретить runtime imports,
зафиксировать входную schema version, сохранить dry-run по умолчанию,
JSON-report и `--execute`. Архивировать инструменты можно после production
migration и rollback-периода.

## 10. Тесты и документация

Устарели:

- восемь навсегда skipped bulk flow-тестов;
- fixtures старых schemas и statuses в runtime suite;
- `docs/Описание.md` с прежним названием поля, приоритетом и старым flow;
- dashboard spec, считающий retired bulk tables рабочими источниками;
- противоречивые утверждения о legacy polling в `PROJECT_OVERVIEW.md`;
- старые priority settings в `.env.example`.

Skipped tests следует удалить вместе с retired code, migration fixtures
вынести отдельно, а основные документы описывать только актуальный flow.

## Необычные сценарии

1. Нажатие старой Telegram-кнопки после удаления таблиц: tombstone должен
   отвечать без legacy repository.
2. Сбой между Google migration и SQLite commit: retry восстанавливает по
   `application_id`/developerMetadata и не создаёт дубль.
3. Старую bulk-строку перенесли вручную: migration ищет ID, а не доверяет
   координатам.
4. В пачке есть пустые строки: мигрируются только валидные application IDs.
5. Dashboard содержит строку пачки, но outbox пуст: нужен явный Sheets
   cleanup.
6. После destructive migration запускают старый image: такой rollback
   должен быть запрещён или проверен на копии БД.
7. Failed submission хранит старое `submission_sheet_name`: все активные
   drafts нужно проверить до отключения layouts.
8. Архивная вкладка совпадает с новым маршрутом: runtime не должен начать
   писать туда только из-за её существования.
9. Повторный запуск migration: второй dry-run возвращает ноль изменений.
10. В outbox остались legacy events: их нужно доставить, отменить или
    архивировать до удаления renderer.

## Рекомендуемый порядок

### Этап 0. Синхронизация и страховка

- [ ] Выбрать целевой commit и сравнять его с production.
- [x] Сделать и проверить backup SQLite.
- [x] Скачать проверенный backup за пределы VPS.
- [x] Экспортировать затрагиваемые Google Sheets диапазоны и metadata.
- [x] Добавить read-only inventory CLI и JSON-report.
- [x] Ввести следующую версию SQLite migration.
- [x] Проверить migration `v1 → v2` на копии production-БД.
- [ ] Развернуть подготовительный релиз после выбора целевого commit.

Результат inventory от 27.07.2026:

- SQLite `integrity_check=ok`, исходная production schema version `1`;
- 36 из 36 legacy bulk applications найдены в Google Sheets;
- все используют `bulk_new`;
- отсутствующих строк, ошибок источников и расхождений координат нет;
- migration `v1 → v2` не меняет количество заявок, пачек, reservations и
  событий;
- JSON-отчёт сохранён локально в
  `output/legacy-inventory-2026-07-27.json`;
- snapshot staging-листа с формулами, отображаемыми значениями, sheet
  metadata и developer metadata сохранён в
  `output/legacy-google-snapshot-2026-07-27.json`;
- проверенный backup сохранён локально в
  `backups/app-20260727-152110.db`.

### Этап 1. Старые массовые заявки

- [x] Получить бизнес-решение по 36 активным legacy rows.
- [x] Удалить завершенный tracking без изменения Google Sheets.
- [x] Добиться нулевого числа активных `submitted_applications.batch_id`.
- [x] Закрыть старые dashboard rows и pending outbox.
- [x] Проверить пустой повторный migration dry-run.

### Этап 2. Удаление legacy bulk runtime

- [x] Оставить изолированный tombstone callback.
- [x] Удалить старые bulk services, registrar и helpers.
- [x] Удалить batch readers, relocation, notifications и projections.
- [x] Удалить старые repository methods и models.
- [x] Удалить legacy bulk config и skipped tests.
- [x] Обновить dashboard и эксплуатационную документацию.

### Этап 3. Унификация Sheets

- [ ] Подтвердить отсутствие tracked rows старых layouts.
- [ ] Сделать архивные вкладки read-only.
- [ ] Убрать старые layouts из runtime parser и row builders.
- [ ] Убрать `CHIPS V2` upgrade path.
- [ ] Оставить legacy layouts только в offline tools.

### Этап 4. Простое мёртвое состояние

- [ ] Удалить priority flow, env и draft field.
- [ ] Удалить старые status aliases и keyboard kinds.
- [ ] Удалить V1/V2 LLM runtime и `llm_score` после migration.
- [ ] Переименовать `create_bulk_batch()` в `create_bulk_reservation()`.

### Этап 5. Физическая очистка SQLite

- [ ] Удалить `bulk_batches` и `bulk_creation_requests`.
- [ ] Удалить `submitted_applications.batch_id`, если история его не требует.
- [ ] Удалить `drafts.priority` и неиспользуемые LLM columns.
- [ ] Пересоздать индексы без legacy.
- [ ] Выполнить `integrity_check`, restore test и `VACUUM`.
- [ ] Снять несовместимые старые images с rollback после проверки.

## Критерии завершения

Cleanup завершён, когда:

- нет активных tracking-записей с `batch_id`;
- нет runtime imports старых bulk classes;
- нет batch scan, relocation и batch dashboard paths;
- runtime распознаёт только актуальные ADD/EDIT и CHIPS schemas;
- старые layouts доступны только offline либо удалены;
- нет priority flow и неиспользуемых priority env;
- production использует единственную LLM schema;
- нет skipped tests retired workflow;
- документация не обещает отключённую совместимость;
- destructive SQLite migration проверена восстановлением backup;
- rollback-план соответствует новой схеме БД.

## Результат этапа 1 в production

Этап выполнен 27.07.2026 на версии `20260727-8a608a8`.

- перед операцией созданы backups `app-20260727-155449.db` и
  `app-20260727-155551.db`;
- production dry-run подтвердил 36 legacy applications, 5 `bulk_batches`,
  7 `bulk_creation_requests`, отсутствие blockers и legacy delivery events;
- из SQLite удалены ровно эти 36 tracking-записей, 5 batches и 7 requests;
- Google Sheets не изменялись;
- повторный dry-run не находит legacy applications, batches или requests;
- сохранены 145 актуальных `submitted_applications`, 8 `bulk_reservations` и
  все 443 `application_events`;
- `PRAGMA integrity_check = ok`, schema version `2`;
- production healthcheck возвращает `ok`, контейнер healthy;
- результат execute сохранён на VPS в
  `/data/legacy-cleanup-result-20260727.json`, inventory после очистки — в
  `/opt/alfa-auto-requests/legacy-inventory-post-cleanup-20260727.json`.

## Подтверждённые решения 27.07.2026

- целевая кодовая база: локальный `HEAD` на момент начала очистки;
- 36 заявок старой массовой модели завершены и больше не должны отслеживаться;
- строки, листы, форматирование и metadata в Google Sheets не изменяются;
- из SQLite удаляются только legacy tracking, `bulk_batches`,
  `bulk_creation_requests` и недоставленные outbox-события, относящиеся
  исключительно к этим legacy-заявкам;
- `application_events` сохраняются как исторические данные;
- смешанное недоставленное уведомление с legacy и актуальными заявками блокирует
  выполнение, а не удаляется автоматически.

Для операции добавлен DB-only инструмент `python -m app.legacy_cleanup`.
По умолчанию он выполняет dry-run. Выполнение требует токен из свежего dry-run;
токен меняется при изменении состава очищаемых записей. Инструмент не импортирует
Google API и не создаёт dashboard delete events.

Проверка на копии production БД:

- план: 36 `submitted_applications`, 5 `bulk_batches`,
  7 `bulk_creation_requests`;
- блокирующих смешанных outbox-событий нет;
- после execute legacy tracking, batches и requests равны нулю;
- сохранены все 443 `application_events`;
- сохранены 145 актуальных `submitted_applications`;
- `PRAGMA integrity_check = ok`.

## Результат этапа 2

Этап выполнен 27.07.2026 в локальной кодовой базе после production-очистки
этапа 1.

- удалены старые сервис создания пачек и registrar;
- удалены runtime-чтение статусов пачек, relocation, архивный scan,
  уведомления и dashboard projection по `batch_id`;
- удалены старые repository API, модели, enum, config и skipped tests;
- текущие массовые заявки продолжают работать только через
  `bulk_reservations` и после регистрации отслеживаются как одиночные;
- callback `app:bulk_ready:*` оставлен изолированным tombstone: Google Sheets и
  SQLite он не изменяет;
- offline-инструменты инвентаризации и DB-only cleanup сохранены;
- физические таблицы `bulk_batches` и `bulk_creation_requests` временно
  остаются в SQLite до этапа 5 и runtime не используются;
- актуальные dashboard- и эксплуатационные документы больше не обещают
  поддержку старого workflow;
- полный набор тестов: `404 passed`, skipped tests отсутствуют;
- Ruff, `compileall` и обе Compose-проверки прошли;
- инициализация repository на копии очищенной production-БД сохранила
  145 заявок, 8 резервов и 443 события, подняла schema version с `1` до `2`;
  до и после инициализации `PRAGMA integrity_check = ok`.
