# Alfa Auto Requests Bot

Telegram-бот для регистрации и сопровождения заявок на изменение сценариев и ответов чат-ботов.

## Возможности

- регистрация одиночных и массовых заявок;
- маршрутизация заявок в Google Sheets по рабочим направлениям;
- проверка изменений через GigaChat;
- обработка сценариев ADD, EDIT и CHIPS;
- уведомления об изменении статусов и итоговых ответах редакторов;
- SQLite-хранилище черновиков, заявок и очередей доставки;
- read-only dashboard для контроля состояния заявок и интеграций;
- Docker-запуск для локальной и production-среды.

## Архитектура

- `src/app` — Telegram-бот и доменная логика;
- `dashboard` — backend и web-интерфейс мониторинга;
- `prompts` — версии системных и пользовательских prompt-файлов;
- `tests` и `dashboard/tests` — автоматические тесты;
- `ops/systemd` — systemd-таймер резервного копирования;
- `Dockerfile` и `docker-compose*.yml` — запуск приложения.

Бот использует polling Telegram API, Google Sheets для рабочих таблиц, GigaChat для проверки текстовых изменений и SQLite для локального состояния и очередей.

## Требования

- Docker и Docker Compose;
- Telegram bot token;
- Google service-account credentials и идентификаторы рабочих таблиц;
- GigaChat credentials для проверки ADD/EDIT-заявок.

## Настройка

1. Скопируйте `.env.example` в `.env`.
2. Заполните переменные окружения для Telegram, Google Sheets и GigaChat.
3. Положите файл `credentials.json` с Google credentials в корень проекта.

Секреты, локальные базы, архивы и deployment-артефакты не должны добавляться в Git. Файлы `.env`, `credentials.json` и локальные данные уже исключены через `.gitignore`.

## Локальный запуск

```bash
docker compose -f docker-compose.local.yml up --build
```

Для остановки:

```bash
docker compose -f docker-compose.local.yml down
```

### Полный лог запроса GigaChat

Для локальной проверки можно временно включить в `.env.local`:

```env
GIGACHAT_LOG_FULL_REQUEST=true
```

После перезапуска контейнера перед вызовом GigaChat в логах появятся полностью
отрендеренные `SYSTEM` и `USER` prompts. Лог содержит данные заявки, поэтому
режим нельзя включать в production и нужно выключить после диагностики:

```env
GIGACHAT_LOG_FULL_REQUEST=false
```

## Production-запуск

Production compose-файл использует заранее собранный образ:

```bash
APP_VERSION=latest docker compose -f docker-compose.prod.yml up -d
```

Резервное копирование SQLite запускается через maintenance-профиль:

```bash
APP_VERSION=latest docker compose -f docker-compose.prod.yml --profile maintenance run --rm backup
```

## Тесты

Основное приложение:

```bash
pytest
```

Dashboard:

```bash
python -m pytest dashboard/tests
```

## Лицензия и доступ

Проект предназначен для внутреннего использования. Репозиторий содержит код и техническую конфигурацию, но не содержит production-секретов, рабочих данных или отчётных материалов.
