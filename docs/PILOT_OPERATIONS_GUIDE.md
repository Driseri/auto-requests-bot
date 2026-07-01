# Справочник эксплуатации пилотного VPS

Этот документ предназначен для пилота Telegram-бота на пяти пользователях.

Исходные условия:

- VPS: Ubuntu 24.04 LTS, 1 vCPU, 1 ГБ RAM, 15 ГБ диска;
- приложение находится в `/opt/alfa-auto-requests`;
- используется `docker-compose.prod.yml`;
- одновременно работает только один контейнер `bot`;
- Docker image собирается и тестируется на локальном компьютере;
- на VPS передается готовый image;
- мониторинг выполняется вручную через SSH;
- резервные копии создаются вручную.

В командах замените:

- `VPS_USER` на имя пользователя VPS;
- `VPS_HOST` на IP-адрес или домен VPS;
- `VERSION` на уникальный тег релиза, например `4f2c9ab`;
- `BACKUP_FILE` на фактическое имя резервной копии.

Локальные команды рассчитаны на Windows PowerShell и записаны одной строкой. Команды
для VPS рассчитаны на Bash в Ubuntu. Строки с символом `\` в конце являются одной
многострочной Bash-командой.

## Оглавление

- [Быстрая шпаргалка](#быстрая-шпаргалка)
- [Ежедневный контроль](#ежедневный-контроль-за-23-минуты)
- [Первичная подготовка VPS](#первичная-подготовка-vps)
- [Подробный мониторинг](#подробный-мониторинг)
- [Управление контейнером](#управление-контейнером)
- [Ручное обновление](#ручное-обновление)
- [Выкладка текущих локальных изменений](#выкладка-текущих-локальных-изменений-на-vps)
- [Нагрузочное тестирование](#нагрузочное-тестирование)
- [Rollback](#rollback)
- [Резервные копии](#резервные-копии)
- [Удаление заявок из SQLite](#удаление-заявок-из-sqlite)
- [Выбор действия при сбое](#когда-ждать-когда-перезапускать-когда-делать-rollback)
- [Критические ситуации](#критические-ситуации)
- [Таблица команд и рисков](#таблица-команд-и-рисков)
- [Журнал пилота](#журнал-пилота)

## Быстрая шпаргалка

Подключиться к VPS:

```bash
ssh VPS_USER@VPS_HOST
```

Перейти в каталог приложения:

```bash
cd /opt/alfa-auto-requests
```

Проверить контейнер:

```bash
docker compose -f docker-compose.prod.yml ps
```

Проверить приложение изнутри контейнера:

```bash
docker compose -f docker-compose.prod.yml exec bot python -m app.health
```

Нормальный результат:

```text
ok
```

Последние логи:

```bash
docker compose -f docker-compose.prod.yml logs --tail=100 bot
```

Следить за логами:

```bash
docker compose -f docker-compose.prod.yml logs -f --tail=100 bot
```

Остановить просмотр логов: `Ctrl+C`. Это не останавливает контейнер.

Проверить ресурсы:

```bash
uptime
free -h
swapon --show
df -h /
docker stats --no-stream
docker system df
```

Перезапустить ту же версию:

```bash
docker compose -f docker-compose.prod.yml restart bot
```

Применить новый `.env`, Compose или image:

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate bot
```

Создать backup:

```bash
docker compose -f docker-compose.prod.yml --profile maintenance run --rm backup
```

Посмотреть backups:

```bash
ls -lh backups/
```

> Никогда не выполняйте `docker compose down -v`. Параметр `-v` удаляет named volume
> с SQLite, heartbeat и очередями уведомлений.

## Что где находится

| Объект | Расположение |
|---|---|
| Каталог приложения | `/opt/alfa-auto-requests` |
| Production Compose | `/opt/alfa-auto-requests/docker-compose.prod.yml` |
| Настройки приложения | `/opt/alfa-auto-requests/.env` |
| Google credentials | `/opt/alfa-auto-requests/credentials.json` |
| Ручные backups | `/opt/alfa-auto-requests/backups/` |
| SQLite внутри контейнера | `/data/app.db` |
| Heartbeat polling | `/data/status-polling-heartbeat.json` |
| Кэш внешнего healthcheck | `/data/external-health.json` |
| Постоянные данные Docker | named volume `alfa-auto-requests_bot-data` |

Файлы `/data/*` находятся не в каталоге проекта, а в Docker named volume. Поэтому
обычное пересоздание контейнера не удаляет SQLite.

## Как читать статусы Docker

| Статус | Значение | Действие |
|---|---|---|
| `Up ... (healthy)` | Контейнер работает, healthcheck успешен | Ничего не делать |
| `Up ... (health: starting)` | Контейнер недавно запущен | Подождать до 2–3 минут |
| `Up ... (unhealthy)` | Процесс работает, но healthcheck обнаружил проблему | Выполнить диагностику |
| `Restarting` | Главный процесс завершается и Docker запускает его снова | Срочно смотреть логи |
| `Exited` | Контейнер остановлен | Смотреть код выхода и логи |
| Контейнера нет | Он не создан или удален | Проверить Compose и запустить |

`unhealthy` не означает, что Docker автоматически перезапустит контейнер. Политика
`restart: unless-stopped` перезапускает завершившийся процесс, но не перезапускает
процесс только из-за состояния `unhealthy`.

## Ежедневный контроль за 2–3 минуты

Во время пилота выполняйте проверку утром и вечером, а также после жалобы пользователя,
обновления или перезагрузки VPS.

```bash
cd /opt/alfa-auto-requests

docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml exec bot python -m app.health
docker compose -f docker-compose.prod.yml logs --since=12h bot \
  | grep -Ei "traceback|error|failed|unhealthy|polling iteration" \
  | tail -n 50

free -h
swapon --show
df -h /
docker stats --no-stream
```

Проверка считается нормальной, если:

- виден ровно один контейнер `bot`;
- контейнер находится в состоянии `healthy`;
- ручной healthcheck выводит `ok`;
- диск занят менее чем на 80% и свободно не менее 4 ГБ;
- память не держится постоянно выше 70–80%;
- swap не растет непрерывно;
- контейнер не перезапускается;
- в логах нет повторяющихся traceback или `Status polling iteration failed`.

Одиночная ошибка внешнего API не всегда требует вмешательства. Сначала проверьте,
повторяется ли она и восстановилась ли система автоматически.

## Первичная подготовка VPS

### 1. Подключение

Команда выполняется на локальном компьютере:

```bash
ssh VPS_USER@VPS_HOST
```

Все следующие команды до отдельной пометки выполняются на VPS.

### 2. Обновление пакетов

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y ca-certificates curl
```

После обновления ядра перезагрузите VPS:

```bash
sudo reboot
```

Повторно подключитесь по SSH.

### 3. Установка Docker Engine и Compose plugin

Используется официальный apt-репозиторий Docker:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
```

Проверка:

```bash
sudo systemctl status docker --no-pager
sudo docker version
sudo docker compose version
```

Чтобы выполнять Docker-команды без `sudo`:

```bash
sudo usermod -aG docker "$USER"
```

Завершите SSH-сессию и подключитесь снова:

```bash
exit
```

После повторного входа:

```bash
docker version
docker compose version
```

### 4. Создание swap 1 ГБ

Сначала проверьте, существует ли swap:

```bash
swapon --show
free -h
```

Если swap уже есть и его размер около 1 ГБ или больше, новый файл создавать не нужно.

Если swap отсутствует:

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
grep -q '^/swapfile ' /etc/fstab \
  || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-alfa-swap.conf
sudo sysctl --system
```

Проверка:

```bash
swapon --show
free -h
```

Swap не заменяет RAM. Он только уменьшает вероятность немедленного OOM при кратком
пике памяти.

### 5. Каталог приложения

```bash
sudo mkdir -p /opt/alfa-auto-requests/backups
sudo chown -R "$USER":"$USER" /opt/alfa-auto-requests
cd /opt/alfa-auto-requests
```

### 6. Передача конфигурации

Команды выполняются на локальном компьютере из каталога проекта:

```powershell
scp docker-compose.prod.yml VPS_USER@VPS_HOST:/opt/alfa-auto-requests/
scp .env VPS_USER@VPS_HOST:/opt/alfa-auto-requests/
scp credentials.json VPS_USER@VPS_HOST:/opt/alfa-auto-requests/
```

На VPS:

```bash
cd /opt/alfa-auto-requests
chmod 600 .env credentials.json
mkdir -p backups
```

Убедитесь, что в `.env` есть:

```env
APP_VERSION=VERSION
BACKUP_DIR=./backups
```

Пока image не загружен, `docker compose config` проверит структуру, но запускать сервис
еще рано:

```bash
docker compose -f docker-compose.prod.yml config --quiet
```

Отсутствие вывода означает успешную проверку.

### 7. Сборка и проверка image

Команды выполняются на локальном компьютере:

```powershell
docker build --pull -t alfa-auto-requests-bot:VERSION .
docker run --rm alfa-auto-requests-bot:VERSION python -m pytest -q
docker run --rm alfa-auto-requests-bot:VERSION python -m compileall -q src tests
```

Сохранить image:

```powershell
docker save -o alfa-auto-requests-bot-VERSION.tar alfa-auto-requests-bot:VERSION
```

Передать image:

```powershell
scp alfa-auto-requests-bot-VERSION.tar VPS_USER@VPS_HOST:/tmp/
```

На VPS:

```bash
docker load -i /tmp/alfa-auto-requests-bot-VERSION.tar
docker image inspect alfa-auto-requests-bot:VERSION \
  --format 'Image={{.RepoTags}} Size={{.Size}}'
rm /tmp/alfa-auto-requests-bot-VERSION.tar
```

Удаляйте переданный `.tar` только после успешного `docker load`.

### 8. Первый запуск

На VPS:

```bash
cd /opt/alfa-auto-requests
grep '^APP_VERSION=' .env
docker compose -f docker-compose.prod.yml up -d bot
docker compose -f docker-compose.prod.yml ps
```

Первые 60–180 секунд состояние может быть `starting`. Следить за запуском:

```bash
docker compose -f docker-compose.prod.yml logs -f --tail=100 bot
```

После запуска:

```bash
docker compose -f docker-compose.prod.yml exec bot python -m app.health
```

Затем выполните smoke-тест:

1. Откройте бота в Telegram.
2. Выполните `/start`.
3. Создайте тестовую одиночную заявку.
4. Убедитесь, что строка появилась в правильной Google-таблице.
5. Измените важный статус и дождитесь уведомления.
6. Проверьте логи еще раз.

## Подробный мониторинг

### Контейнер и healthcheck

```bash
cd /opt/alfa-auto-requests
docker compose -f docker-compose.prod.yml ps
```

Подробное состояние:

```bash
CID=$(docker compose -f docker-compose.prod.yml ps -q bot)
docker inspect --format \
  'Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} Restarts={{.RestartCount}} OOMKilled={{.State.OOMKilled}} Started={{.State.StartedAt}}' \
  "$CID"
```

Последние результаты healthcheck:

```bash
docker inspect --format \
  '{{range .State.Health.Log}}{{.End}} exit={{.ExitCode}} {{.Output}}{{println}}{{end}}' \
  "$CID"
```

Ручной healthcheck:

```bash
docker compose -f docker-compose.prod.yml exec bot python -m app.health
```

Возможные ответы:

- `ok` — локальные и внешние проверки успешны;
- `heartbeat unavailable` — polling еще не создал heartbeat или файл недоступен;
- `heartbeat is stale` — polling давно не завершал успешный цикл;
- `sqlite unavailable` или `sqlite quick_check failed` — проблема SQLite;
- `configuration invalid` — отсутствует обязательная настройка;
- `external checks failed` — Telegram или Google недоступны.

### Логи

Последние 100 строк:

```bash
docker compose -f docker-compose.prod.yml logs --tail=100 bot
```

Логи за последний час:

```bash
docker compose -f docker-compose.prod.yml logs --since=1h bot
```

Ошибки за сутки:

```bash
docker compose -f docker-compose.prod.yml logs --since=24h bot \
  | grep -Ei "traceback|error|failed|polling iteration|unhealthy" \
  | tail -n 100
```

Наблюдение в реальном времени:

```bash
docker compose -f docker-compose.prod.yml logs -f --tail=100 bot
```

Повторяющаяся одинаковая ошибка важнее одной отдельной записи.

### CPU, RAM, swap и load average

```bash
uptime
free -h
swapon --show
docker stats --no-stream
```

Как читать:

- `load average` около `1.0` для одного CPU означает полную загрузку CPU;
- краткий пик допустим, постоянное значение выше `1.5–2.0` требует диагностики;
- смотрите прежде всего на `available`, а не на `free`;
- постоянный рост swap и торможение указывают на нехватку RAM;
- единичное использование swap после пика не обязательно является проблемой.

### Диск

```bash
df -h /
du -sh /opt/alfa-auto-requests/backups
docker system df
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}'
```

Контроль логов текущего контейнера:

```bash
CID=$(docker compose -f docker-compose.prod.yml ps -q bot)
LOG_PATH=$(docker inspect --format '{{.LogPath}}' "$CID")
sudo du -h "$(dirname "$LOG_PATH")"
```

Безопасная очистка только неиспользуемых промежуточных images:

```bash
docker image prune
```

Перед удалением конкретного старого image убедитесь, что он не является последней
рабочей версией для rollback:

```bash
docker image rm alfa-auto-requests-bot:OLD_VERSION
```

> Не используйте `docker system prune -a --volumes`. Команда может удалить rollback
> images и неиспользуемые volumes с данными.

### Очередь Telegram-уведомлений

Команда только читает SQLite:

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

connection = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
for row in connection.execute(
    """
    SELECT state, COUNT(*), MAX(attempts)
    FROM notification_outbox
    GROUP BY state
    ORDER BY state
    """
):
    print(row)
connection.close()
PY
```

Нормально:

- большинство записей имеет состояние `SENT`;
- `PENDING` может кратковременно появляться перед retry;
- `SENDING` не должно зависать более пяти минут;
- `FAILED` требует разбора причины.

Последние проблемные записи:

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

connection = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
for row in connection.execute(
    """
    SELECT event_id, telegram_user_id, state, attempts,
           next_attempt_at, substr(last_error, 1, 200), updated_at
    FROM notification_outbox
    WHERE state IN ('PENDING', 'SENDING', 'FAILED')
    ORDER BY updated_at DESC
    LIMIT 30
    """
):
    print(row)
connection.close()
PY
```

Не меняйте состояния outbox вручную через SQL. Перезапуск не возвращает `FAILED`
в `PENDING`; сначала нужно установить причину и передать event ID разработчику.

### Уведомления редакторов о срочных заявках

Функция выключена по умолчанию. Чтобы включить уведомления в общий чат редакторов:

1. Добавьте бота в общий Telegram-чат редакторов.
2. Напишите любое сообщение в этот чат.
3. Получите `chat_id` из логов. Для supergroup он обычно начинается с `-100`:

```bash
docker compose -f docker-compose.prod.yml logs --since=10m bot \
  | grep 'Ignored non-private chat update'
```

Бот не должен отвечать в группе интерфейсом заведения заявки. Он только пишет
`chat_id` в логи и игнорирует входящее сообщение.

4. В `.env` укажите:

```env
URGENT_EDITOR_NOTIFICATIONS_ENABLED=true
EDITOR_URGENT_CHAT_ID=-1001234567890
```

5. Примените `.env` без удаления данных:

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate bot
docker compose -f docker-compose.prod.yml exec -T bot python -m app.health
```

Healthcheck не отправляет тестовое сообщение в чат, чтобы не спамить редакторов.
Проверка выполняется созданием одной тестовой одиночной срочной заявки. После успешной
записи строки в Google Sheets в `notification_outbox` появится событие
`urgent-editor-application-created`, а бот отправит в общий чат короткое сообщение со
ссылкой `Открыть заявку`.

Проверить последние editor-chat события:

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

connection = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
for row in connection.execute(
    """
    SELECT telegram_user_id, state, attempts, updated_at
    FROM notification_outbox
    WHERE event_type = 'urgent-editor-application-created'
    ORDER BY updated_at DESC
    LIMIT 20
    """
):
    print(row)
connection.close()
PY
```

### Очередь дашборда

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

connection = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
for row in connection.execute(
    """
    SELECT state, COUNT(*), MAX(attempts)
    FROM dashboard_outbox
    GROUP BY state
    ORDER BY state
    """
):
    print(row)
connection.close()
PY
```

Проблемные элементы:

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

connection = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
for row in connection.execute(
    """
    SELECT entity_type, entity_id, state, attempts,
           next_attempt_at, substr(last_error, 1, 200), updated_at
    FROM dashboard_outbox
    WHERE state IN ('PENDING', 'SENDING')
    ORDER BY updated_at DESC
    LIMIT 30
    """
):
    print(row)
connection.close()
PY
```

Dashboard outbox повторяется автоматически. Удалять записи вручную нельзя.

## Управление контейнером

### `restart`: перезапуск той же конфигурации

```bash
docker compose -f docker-compose.prod.yml restart bot
```

Используйте, если процесс завис, но image и `.env` не менялись.

`restart` не применяет изменения `.env` или Compose.

### `up -d --force-recreate`: применить изменения

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate bot
```

Используйте после:

- изменения `.env`;
- изменения `docker-compose.prod.yml`;
- загрузки и выбора нового `APP_VERSION`;
- rollback на предыдущую версию.

Named volume сохраняется.

### `stop` и `start`

Контролируемая остановка:

```bash
docker compose -f docker-compose.prod.yml stop bot
```

Запуск остановленного контейнера:

```bash
docker compose -f docker-compose.prod.yml start bot
```

Используйте остановку перед реальным восстановлением SQLite.

### Перезагрузка VPS

```bash
sudo reboot
```

Перезагружайте VPS только когда проблема относится к ОС, Docker daemon, зависшим
системным ресурсам или обновлению ядра. Не используйте reboot как первую реакцию на
ошибку приложения.

После перезагрузки:

```bash
cd /opt/alfa-auto-requests
systemctl status docker --no-pager
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml exec bot python -m app.health
```

Благодаря `restart: unless-stopped` контейнер должен запуститься автоматически, если
до reboot он не был остановлен вручную.

## Ручное обновление

### Чек-лист перед обновлением

- [ ] Пользователи предупреждены о коротком перерыве.
- [ ] Локальные тесты и compileall прошли.
- [ ] Image имеет уникальный тег.
- [ ] На VPS свободно не менее 4 ГБ.
- [ ] Текущий `APP_VERSION` записан как `PREVIOUS_VERSION`.
- [ ] Старый image присутствует на VPS.
- [ ] Создан и проверен backup.
- [ ] Нет активной массовой регистрации.

### 1. Подготовка image локально

```powershell
docker build --pull -t alfa-auto-requests-bot:VERSION .
docker run --rm alfa-auto-requests-bot:VERSION python -m pytest -q
docker run --rm alfa-auto-requests-bot:VERSION python -m compileall -q src tests
docker save -o alfa-auto-requests-bot-VERSION.tar alfa-auto-requests-bot:VERSION
```

### 2. Передача image

```powershell
scp alfa-auto-requests-bot-VERSION.tar VPS_USER@VPS_HOST:/tmp/
```

Если изменился Compose:

```powershell
scp docker-compose.prod.yml VPS_USER@VPS_HOST:/opt/alfa-auto-requests/
```

Не перезаписывайте `.env` без отдельной необходимости.

### 3. Подготовка VPS

```bash
ssh VPS_USER@VPS_HOST
cd /opt/alfa-auto-requests

df -h /
docker system df
grep '^APP_VERSION=' .env
docker compose -f docker-compose.prod.yml ps
```

Запишите предыдущую версию:

```bash
PREVIOUS_VERSION=$(grep '^APP_VERSION=' .env | cut -d= -f2-)
echo "$PREVIOUS_VERSION"
docker image inspect "alfa-auto-requests-bot:$PREVIOUS_VERSION" \
  --format '{{.RepoTags}}'
```

### 4. Backup

```bash
docker compose -f docker-compose.prod.yml \
  --profile maintenance run --rm backup
ls -lht backups/ | head
```

Команда backup сама выполняет `integrity_check`. Успехом считается вывод пути
`/backups/app-YYYYMMDD-HHMMSS.db` и код завершения `0`.

### 5. Загрузка и выбор новой версии

```bash
docker load -i /tmp/alfa-auto-requests-bot-VERSION.tar
docker image inspect alfa-auto-requests-bot:VERSION \
  --format '{{.RepoTags}}'
```

Проверить Compose с новой версией без запуска:

```bash
APP_VERSION=VERSION docker compose -f docker-compose.prod.yml config --quiet
```

Обновить только строку `APP_VERSION`:

```bash
sed -i 's/^APP_VERSION=.*/APP_VERSION=VERSION/' .env
grep '^APP_VERSION=' .env
```

### 6. Переключение

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate bot
docker compose -f docker-compose.prod.yml ps
```

Наблюдать:

```bash
docker compose -f docker-compose.prod.yml logs -f --tail=100 bot
```

После получения `healthy`:

```bash
docker compose -f docker-compose.prod.yml exec bot python -m app.health
```

### 7. Smoke-тест

1. Выполнить `/start`.
2. Открыть главное меню.
3. Создать тестовую заявку или пройти минимальный безопасный сценарий.
4. Проверить запись в Google Sheets.
5. Проверить изменение статуса и уведомление.
6. Проверить логи.

### 8. Завершение

```bash
rm /tmp/alfa-auto-requests-bot-VERSION.tar
docker images 'alfa-auto-requests-bot'
```

Не удаляйте `PREVIOUS_VERSION` до окончания пилотной проверки новой версии.

## Выкладка текущих локальных изменений на VPS

Этот сценарий используйте, когда изменения уже сделаны локально в рабочей папке и их
нужно аккуратно залить на пилотный VPS. Image собирается локально, на VPS передается
готовый `.tar`. На сервере сборку не выполняем, чтобы не тратить RAM и CPU VPS.

Команды ниже рассчитаны на текущий пилотный сервер:

- VPS user: `root`;
- VPS host: `206.223.244.31`;
- каталог приложения на VPS: `/opt/alfa-auto-requests`;
- production Compose: `docker-compose.prod.yml`.

### 1. Локально выбрать тег версии

PowerShell, на локальном компьютере:

```powershell
cd "C:\Users\Yasch\OneDrive\Документы\Работа\Альфа-автозаявки"
$env:APP_VERSION = "pilot-$(Get-Date -Format yyyyMMdd-HHmm)"
$env:APP_VERSION
```

Зачем: у каждого релиза должен быть уникальный тег. Тогда можно понять, какая версия
запущена, и быстро вернуться на предыдущий image при rollback.

### 2. Локально проверить код перед сборкой

```powershell
pytest -q
ruff check src tests
python -m compileall src tests
docker compose config
docker compose -f docker-compose.prod.yml config
```

Зачем: если тесты, линтер, импорт Python или Compose-конфигурация падают локально,
такой image нельзя выкатывать на VPS.

### 3. Локально собрать и сохранить image

```powershell
docker build -t alfa-auto-requests-bot:$env:APP_VERSION .
docker save alfa-auto-requests-bot:$env:APP_VERSION -o "alfa-auto-requests-bot-$env:APP_VERSION.tar"
```

Зачем: `docker build` выполняется на локальной машине, а VPS получает уже готовый
image. Это стабильнее для маленького сервера.

### 4. Передать image на VPS

```powershell
scp ".\alfa-auto-requests-bot-$env:APP_VERSION.tar" root@206.223.244.31:/opt/alfa-auto-requests/
```

Если менялся `docker-compose.prod.yml`, отдельно передайте его:

```powershell
scp ".\docker-compose.prod.yml" root@206.223.244.31:/opt/alfa-auto-requests/docker-compose.prod.yml
```

`.env` так не перезаписывайте. Его меняют вручную на VPS, чтобы случайно не затереть
боевые токены, ID таблиц и список редакторов.

### 5. На VPS проверить текущее состояние

SSH на VPS:

```bash
ssh root@206.223.244.31
cd /opt/alfa-auto-requests

docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml exec bot python -m app.health
df -h /
free -h
docker system df
grep '^APP_VERSION=' .env
```

Зачем: перед обновлением нужно убедиться, что сервер жив, места хватает, текущая
версия известна, а healthcheck не показывает уже существующую проблему.

Сохранить текущую версию в переменную для rollback:

```bash
PREVIOUS_VERSION=$(grep '^APP_VERSION=' .env | cut -d= -f2-)
echo "$PREVIOUS_VERSION"
docker image inspect "alfa-auto-requests-bot:$PREVIOUS_VERSION" \
  --format '{{.RepoTags}}'
```

Если `docker image inspect` не нашел старый image, rollback будет сложнее. В таком
случае не удаляйте старые `.tar` и не чистите Docker до завершения пилотной проверки.

### 6. Сделать backup SQLite перед обновлением

```bash
cd /opt/alfa-auto-requests
docker compose -f docker-compose.prod.yml \
  --profile maintenance run --rm backup
ls -lht backups/ | head
```

Зачем: backup нужен перед любым обновлением, потому что новая версия может выполнить
автоматические миграции SQLite. Image можно откатить, но данные нужно уметь восстановить
отдельно.

### 7. Загрузить image и переключить APP_VERSION

На VPS подставьте тот же тег, который был в PowerShell:

```bash
VERSION=pilot-YYYYMMDD-HHMM
docker load -i "alfa-auto-requests-bot-$VERSION.tar"
docker image inspect "alfa-auto-requests-bot:$VERSION" \
  --format '{{.RepoTags}}'
```

Проверить Compose с новым тегом без запуска:

```bash
APP_VERSION="$VERSION" docker compose -f docker-compose.prod.yml config --quiet
```

Обновить только `APP_VERSION` в `.env`:

```bash
grep -q '^APP_VERSION=' .env \
  && sed -i "s/^APP_VERSION=.*/APP_VERSION=$VERSION/" .env \
  || echo "APP_VERSION=$VERSION" >> .env

grep '^APP_VERSION=' .env
```

Зачем: Compose берет тег image из `.env`. Пока `APP_VERSION` не изменен, сервер
продолжит запускать старую версию.

### 8. Пересоздать контейнер

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate bot
docker compose -f docker-compose.prod.yml ps
```

`up -d --force-recreate` создает новый контейнер из нового image и нового `.env`.
Данные SQLite остаются в named volume `bot-data`.

Наблюдать старт:

```bash
docker compose -f docker-compose.prod.yml logs -f --tail=100 bot
```

Остановить просмотр логов: `Ctrl+C`. Это не останавливает контейнер.

### 9. Проверить после запуска

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml exec bot python -m app.health
docker stats --no-stream
docker compose -f docker-compose.prod.yml logs --since=10m bot \
  | grep -Ei "traceback|error|failed|unhealthy|polling iteration" \
  | tail -n 80
```

Нормально:

- контейнер один;
- статус `healthy`;
- `python -m app.health` выводит `ok`;
- в логах нет повторяющихся traceback;
- бот отвечает в Telegram;
- тестовая заявка доходит до Google Sheets.

### 10. Smoke-тест после выкладки

Минимальный безопасный сценарий:

1. В Telegram выполнить `/new`.
2. Пройти создание тестовой одиночной заявки.
3. Проверить, что строка появилась в нужной Google-таблице.
4. Изменить статус/редактора в таблице и дождаться уведомления.
5. Проверить общий дашборд, если он включен.
6. Проверить логи за последние 15 минут.

Команда для логов:

```bash
docker compose -f docker-compose.prod.yml logs --since=15m bot
```

### 11. Удалить переданный tar после успешной проверки

```bash
rm -f "/opt/alfa-auto-requests/alfa-auto-requests-bot-$VERSION.tar"
docker images 'alfa-auto-requests-bot'
```

Удаляется только `.tar`, загруженный Docker image остается. Старый image
`$PREVIOUS_VERSION` не удаляйте до окончания проверки новой версии.

### 12. Быстрый rollback, если новая версия плохая

```bash
cd /opt/alfa-auto-requests
sed -i "s/^APP_VERSION=.*/APP_VERSION=$PREVIOUS_VERSION/" .env
docker compose -f docker-compose.prod.yml up -d --force-recreate bot
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml exec bot python -m app.health
```

Rollback переключает код на старый image, но не откатывает SQLite. Если новая версия
уже изменила данные несовместимым способом, потребуется восстановление backup из
раздела [Резервные копии](#резервные-копии).

## Нагрузочное тестирование

Нагрузочный тест запускается как отдельный one-off process внутри production image.
Он использует production `.env`, SQLite volume и Google Sheets, но не отправляет
сообщения в Telegram и не вызывает GigaChat. Пользователи имитируются разными
`telegram_user_id` напрямую через бизнес-логику бота.

Важно:

- production bot на время теста нужно остановить;
- тестовые строки в Google Sheets останутся после теста;
- SQLite очищается автоматически только при `--cleanup-sqlite`;
- все тестовые данные помечаются `RUN_ID`, например `LOADTEST-20260616-153000`;
- удаление строк из Google Sheets выполняется вручную по `RUN_ID`.

### Профили

| Профиль | Что делает |
|---|---|
| `baseline` | 5 пользователей, по 1 одиночной заявке, 5 polling cycles |
| `pilot15` | 15 пользователей, по 10 одиночных заявок, 3 массовых резерва по 30 строк, 30 polling cycles |
| `stress` | 30 пользователей, по 5 одиночных заявок, 5 массовых резервов по 30 строк, 60 polling cycles |

По умолчанию loadtest проверяет новый workflow `bulk_reservations`: бот вставляет резерв строк в боевой лист, заполняет его тестовыми данными и регистрирует строки как обычные одиночные заявки.

Для старого workflow с `bulk_batches` оставлен совместимый режим:

```bash
--bulk-mode legacy
```

### Перед тестом

```bash
cd /opt/alfa-auto-requests
docker compose -f docker-compose.prod.yml --profile maintenance run --rm backup
docker compose -f docker-compose.prod.yml stop bot
mkdir -p loadtest-reports
```

Зачем:

- backup нужен перед записью тестовых данных;
- `stop bot` исключает второй активный процесс с той же SQLite и теми же таблицами;
- каталог `loadtest-reports` нужен для сохранения отчетов.

### Запуск baseline

```bash
RUN_ID="LOADTEST-$(date +%Y%m%d-%H%M%S)"

docker compose -f docker-compose.prod.yml run --rm \
  -v "$PWD/loadtest-reports:/reports" \
  --entrypoint python bot \
  -m app.loadtest \
  --profile baseline \
  --bulk-mode reservations \
  --run-id "$RUN_ID" \
  --cleanup-sqlite \
  --concurrency 5 \
  --google-throttle-seconds 0.5 \
  --report-path "/reports/loadtest-$RUN_ID.json"
```

Сначала запускайте `baseline`. Если он прошел без ошибок, переходите к `pilot15`.

### Запуск pilot15

```bash
RUN_ID="LOADTEST-$(date +%Y%m%d-%H%M%S)"

docker compose -f docker-compose.prod.yml run --rm \
  -v "$PWD/loadtest-reports:/reports" \
  --entrypoint python bot \
  -m app.loadtest \
  --profile pilot15 \
  --bulk-mode reservations \
  --run-id "$RUN_ID" \
  --cleanup-sqlite \
  --concurrency 5 \
  --google-throttle-seconds 0.5 \
  --report-path "/reports/loadtest-$RUN_ID.json"
```

Этот профиль основной для проверки пилота на 15 человек.

### Запуск stress

```bash
RUN_ID="LOADTEST-$(date +%Y%m%d-%H%M%S)"

docker compose -f docker-compose.prod.yml run --rm \
  -v "$PWD/loadtest-reports:/reports" \
  --entrypoint python bot \
  -m app.loadtest \
  --profile stress \
  --bulk-mode reservations \
  --run-id "$RUN_ID" \
  --cleanup-sqlite \
  --concurrency 5 \
  --google-throttle-seconds 1.0 \
  --report-path "/reports/loadtest-$RUN_ID.json"
```

`stress` запускайте только после успешного `pilot15`.

### После теста

```bash
docker compose -f docker-compose.prod.yml start bot
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml exec bot python -m app.health
docker compose -f docker-compose.prod.yml logs --since=10m bot
```

Посмотреть отчеты:

```bash
ls -lh loadtest-reports/
```

Вывести конкретный отчет:

```bash
cat loadtest-reports/loadtest-LOADTEST-YYYYMMDD-HHMMSS.json
```

### Как читать результат

В отчете проверьте:

- `errors` должен быть пустым;
- `counts.single_created` соответствует профилю;
- `counts.bulk_reservations_created` соответствует профилю при `--bulk-mode reservations`;
- `counts.bulk_batches_created` используется только для `--bulk-mode legacy`;
- `counts.bulk_rows_registered` соответствует профилю;
- `sqlite.cleanup` показывает удаление тестовых записей;
- `manual_google_cleanup.ranges` содержит диапазоны, которые нужно удалить из Google Sheets вручную;
- `memory.before` и `memory.after` не должны показывать резкий необъяснимый рост RSS.

### Ручная очистка Google Sheets

Откройте отчет и найдите:

- `run_id`;
- `created.google_ranges`;
- `manual_google_cleanup.ranges`.

В Google Sheets найдите строки по `run_id`, например `LOADTEST-20260616-153000`, и
удалите созданные тестовые строки вручную. SQLite cleanup не удаляет строки из Google
Sheets намеренно, чтобы не рисковать production-таблицами автоматическим удалением.

### Чек-лист после обновления

- [ ] Контейнер один.
- [ ] Состояние `healthy`.
- [ ] Ручной healthcheck выводит `ok`.
- [ ] В логах нет повторяющейся ошибки.
- [ ] Telegram отвечает.
- [ ] Google Sheets доступны.
- [ ] Polling создает свежий heartbeat.
- [ ] Smoke-тест пройден.
- [ ] Предыдущий image сохранен.
- [ ] Результат записан в журнал пилота.

## Rollback

Rollback возвращает код предыдущей версии, но сохраняет текущий named volume SQLite.
Перед rollback нужно учитывать совместимость старого кода с текущей схемой базы.

```bash
cd /opt/alfa-auto-requests

docker images 'alfa-auto-requests-bot'
sed -i 's/^APP_VERSION=.*/APP_VERSION=PREVIOUS_VERSION/' .env
docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml up -d --force-recreate bot
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 bot
docker compose -f docker-compose.prod.yml exec bot python -m app.health
```

Rollback необходим, если после новой версии:

- контейнер не запускается;
- контейнер постоянно перезапускается;
- healthcheck не восстанавливается;
- сломан основной пользовательский workflow;
- появились массовые повторяемые ошибки, которых не было раньше.

Если новая версия уже изменила данные несовместимым способом, одного rollback image
может быть недостаточно. Тогда потребуется восстановление backup.

## Резервные копии

Создавайте backup:

- перед каждым обновлением;
- перед изменением `.env`;
- перед восстановлением;
- после важной пилотной операции;
- перед потенциально рискованной диагностикой.

### Создание

```bash
cd /opt/alfa-auto-requests
docker compose -f docker-compose.prod.yml \
  --profile maintenance run --rm backup
```

### Просмотр

```bash
ls -lht /opt/alfa-auto-requests/backups/
```

### Повторная проверка конкретного backup

```bash
cd /opt/alfa-auto-requests
VERSION=$(grep '^APP_VERSION=' .env | cut -d= -f2-)
BACKUP_FILE=app-YYYYMMDD-HHMMSS.db

docker run --rm \
  -v "$PWD/backups:/backups:ro" \
  "alfa-auto-requests-bot:$VERSION" \
  python -m app.maintenance verify "/backups/$BACKUP_FILE"
```

Нормальный результат:

```text
ok
```

### Тестовое восстановление без замены production

```bash
cd /opt/alfa-auto-requests
BACKUP_FILE=app-YYYYMMDD-HHMMSS.db

docker compose -f docker-compose.prod.yml run --rm \
  -v "$PWD/backups/$BACKUP_FILE:/backup.db:ro" \
  --entrypoint python bot \
  -m app.maintenance restore /backup.db /data/restore-test.db
```

Проверить:

```bash
docker compose -f docker-compose.prod.yml run --rm \
  --entrypoint python bot \
  -m app.maintenance verify /data/restore-test.db
```

Удалить только тестовый файл:

```bash
docker compose -f docker-compose.prod.yml run --rm \
  --entrypoint sh bot \
  -c 'rm -f /data/restore-test.db'
```

### Реальное восстановление SQLite

> Выполняйте только при подтвержденной необходимости. Бот должен быть остановлен.

1. Сохранить диагностическую копию текущей базы:

```bash
cd /opt/alfa-auto-requests
docker compose -f docker-compose.prod.yml \
  --profile maintenance run --rm backup
```

2. Остановить бота:

```bash
docker compose -f docker-compose.prod.yml stop bot
```

3. Восстановить backup во временный файл:

```bash
BACKUP_FILE=app-YYYYMMDD-HHMMSS.db

docker compose -f docker-compose.prod.yml run --rm \
  -v "$PWD/backups/$BACKUP_FILE:/backup.db:ro" \
  --entrypoint python bot \
  -m app.maintenance restore /backup.db /data/app-restored.db
```

4. Заменить базу:

```bash
docker compose -f docker-compose.prod.yml run --rm \
  --entrypoint sh bot \
  -c 'mv /data/app.db /data/app.db.before-restore && rm -f /data/app.db-wal /data/app.db-shm && mv /data/app-restored.db /data/app.db'
```

5. Запустить и проверить:

```bash
docker compose -f docker-compose.prod.yml start bot
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 bot
docker compose -f docker-compose.prod.yml exec bot python -m app.health
```

Файл `/data/app.db.before-restore` не удаляйте до подтверждения успешного восстановления.

### Ограничение ручного backup

Backup в `/opt/alfa-auto-requests/backups` находится на том же VPS. Он помогает при
ошибке обновления или повреждении рабочей базы, но не спасает при полной потере VPS
или диска.

Для защиты от потери VPS периодически скачивайте хотя бы последнюю копию:

```powershell
scp VPS_USER@VPS_HOST:/opt/alfa-auto-requests/backups/BACKUP_FILE .
```

Это ручное действие и не является автоматическим backup.

## Удаление заявок из SQLite

Используйте этот раздел только для локального tracking в SQLite. Эти команды не
удаляют строки из Google Sheets и не меняют уже отправленные Telegram-сообщения.

Типовые случаи:

- в SQLite осталась заявка, которой уже нет в таблице;
- нужно остановить ложный polling/not_found по конкретному `application_id`;
- нужно убрать локальный tracking тестовой массовой пачки.

Перед любым `DELETE` обязательно:

1. сделать backup;
2. выполнить read-only preview;
3. удалять только явно перечисленные ID;
4. проверить результат.

### 1. Backup перед удалением

```bash
cd /opt/alfa-auto-requests
docker compose -f docker-compose.prod.yml \
  --profile maintenance run --rm backup
ls -lht backups/ | head
```

Зачем: удаление из SQLite необратимо без backup. Даже если удаляется “только tracking”,
ошибка в SQL может затронуть больше записей, чем планировалось.

### 2. Найти заявку по application_id

Команда только читает SQLite:

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

ids = ["APPLICATION_ID_1", "APPLICATION_ID_2"]
conn = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

query = """
SELECT application_id, telegram_user_id, batch_id, sheet_name,
       last_known_status, last_seen_row_number, polling_state,
       not_found_count, created_at, updated_at
FROM submitted_applications
WHERE application_id IN ({})
ORDER BY created_at
""".format(",".join("?" for _ in ids))

for row in conn.execute(query, ids):
    print(dict(row))

conn.close()
PY
```

Замените `APPLICATION_ID_1`, `APPLICATION_ID_2` на реальные ID, например
`31096ED3`. Если ID один, оставьте список из одного элемента:

```python
ids = ["31096ED3"]
```

### 3. Посмотреть связанные dashboard-записи

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

ids = ["APPLICATION_ID_1", "APPLICATION_ID_2"]
conn = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

query = """
SELECT entity_type, entity_id, state, attempts,
       next_attempt_at, substr(last_error, 1, 200) AS last_error,
       updated_at
FROM dashboard_outbox
WHERE entity_type = 'APPLICATION'
  AND entity_id IN ({})
ORDER BY updated_at
""".format(",".join("?" for _ in ids))

for row in conn.execute(query, ids):
    print(dict(row))

conn.close()
PY
```

Зачем: если заявка стоит в `dashboard_outbox`, после удаления tracking нужно убрать и
ее pending-проекцию, иначе бот может продолжить пытаться обновить дашборд по старой
сущности.

### 4. Удалить tracking одиночных заявок по application_id

Выполняйте только после backup и preview.

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

ids = ["APPLICATION_ID_1", "APPLICATION_ID_2"]

conn = sqlite3.connect("/data/app.db")
conn.row_factory = sqlite3.Row
conn.execute("BEGIN IMMEDIATE")

query = """
SELECT application_id, telegram_user_id, batch_id, sheet_name,
       last_known_status, last_seen_row_number
FROM submitted_applications
WHERE application_id IN ({})
ORDER BY created_at
""".format(",".join("?" for _ in ids))
found = conn.execute(query, ids).fetchall()

print("will_delete_submitted_applications", [dict(row) for row in found])

delete_dashboard = """
DELETE FROM dashboard_outbox
WHERE entity_type = 'APPLICATION'
  AND entity_id IN ({})
""".format(",".join("?" for _ in ids))
conn.execute(delete_dashboard, ids)
after_dashboard = conn.total_changes

delete_submitted = """
DELETE FROM submitted_applications
WHERE application_id IN ({})
""".format(",".join("?" for _ in ids))
conn.execute(delete_submitted, ids)
after_all = conn.total_changes

conn.commit()
conn.close()

print("deleted_dashboard_outbox", after_dashboard)
print("deleted_submitted_applications", after_all - after_dashboard)
print("deleted_total_changes", after_all)
PY
```

Что удаляется:

- записи из `submitted_applications` по выбранным `application_id`;
- связанные pending/failed dashboard-проекции `APPLICATION` из `dashboard_outbox`.

Что не удаляется:

- строки в Google Sheets;
- история уже отправленных Telegram-уведомлений;
- черновики пользователей;
- массовая пачка, если заявка была частью пачки.

Если заявка относится к массовой пачке (`batch_id IS NOT NULL`), обычно лучше не
удалять отдельную строку без причины: она нужна для поздних изменений редактора,
итогового ответа и дашборда. Для ложных `not_found` чаще достаточно сбросить счетчики.

### 5. Сбросить not_found без удаления

Этот вариант безопаснее удаления, если заявка существует в Google Sheets, но бот
временно пометил ее как не найденную.

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

ids = ["APPLICATION_ID_1", "APPLICATION_ID_2"]
conn = sqlite3.connect("/data/app.db")
conn.execute("BEGIN IMMEDIATE")
query = """
UPDATE submitted_applications
SET polling_state = 'ACTIVE',
    not_found_count = 0,
    last_not_found_at = NULL,
    next_status_check_at = NULL,
    updated_at = datetime('now')
WHERE application_id IN ({})
""".format(",".join("?" for _ in ids))
conn.execute(query, ids)
print("updated", conn.total_changes)
conn.commit()
conn.close()
PY
```

Используйте это, когда строка есть в таблице, но tracking ушел в редкую проверку.

### 6. Preview массовой пачки по batch_id

Команда только читает SQLite:

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

batch_id = "BATCH_ID"
conn = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

print("bulk_batch")
for row in conn.execute(
    """
    SELECT batch_id, telegram_user_id, direction, sheet_name, start_row,
           data_start_row, data_end_row, reserved_rows, registration_state,
           last_known_batch_status, registered_count, created_at, updated_at
    FROM bulk_batches
    WHERE batch_id = ?
    """,
    (batch_id,),
):
    print(dict(row))

print("submitted_applications")
for row in conn.execute(
    """
    SELECT application_id, last_known_status, last_seen_row_number,
           polling_state, not_found_count, updated_at
    FROM submitted_applications
    WHERE batch_id = ?
    ORDER BY last_seen_row_number, application_id
    """,
    (batch_id,),
):
    print(dict(row))

conn.close()
PY
```

### 7. Удалить локальный tracking массовой пачки

Используйте только для тестовой или ошибочной пачки, которую точно больше не нужно
отслеживать. Строки в Google Sheets не удаляются.

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

batch_id = "BATCH_ID"

conn = sqlite3.connect("/data/app.db")
conn.row_factory = sqlite3.Row
conn.execute("BEGIN IMMEDIATE")

apps = conn.execute(
    """
    SELECT application_id, last_known_status, last_seen_row_number
    FROM submitted_applications
    WHERE batch_id = ?
    ORDER BY last_seen_row_number, application_id
    """,
    (batch_id,),
).fetchall()
batch = conn.execute(
    """
    SELECT batch_id, telegram_user_id, registration_state, last_known_batch_status
    FROM bulk_batches
    WHERE batch_id = ?
    """,
    (batch_id,),
).fetchone()

print("will_delete_batch", dict(batch) if batch else None)
print("will_delete_applications", [dict(row) for row in apps])

conn.execute(
    "DELETE FROM dashboard_outbox WHERE entity_type = 'BULK_BATCH' AND entity_id = ?",
    (batch_id,),
)
conn.execute(
    "DELETE FROM bulk_creation_requests WHERE batch_id = ?",
    (batch_id,),
)
conn.execute(
    "DELETE FROM submitted_applications WHERE batch_id = ?",
    (batch_id,),
)
conn.execute(
    "DELETE FROM bulk_batches WHERE batch_id = ?",
    (batch_id,),
)

print("deleted_total_changes", conn.total_changes)
conn.commit()
conn.close()
PY
```

Не используйте этот сценарий для обычной завершенной пачки: завершенные пачки
специально остаются в tracking и проверяются архивным scan раз в час.

### 8. Проверить результат удаления

Для одиночных заявок:

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

ids = ["APPLICATION_ID_1", "APPLICATION_ID_2"]
conn = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
query = "SELECT COUNT(*) FROM submitted_applications WHERE application_id IN ({})".format(
    ",".join("?" for _ in ids)
)
print(conn.execute(query, ids).fetchone()[0])
conn.close()
PY
```

Ожидаемый результат: `0`.

Для массовой пачки:

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

batch_id = "BATCH_ID"
conn = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
print("bulk_batches", conn.execute(
    "SELECT COUNT(*) FROM bulk_batches WHERE batch_id = ?",
    (batch_id,),
).fetchone()[0])
print("submitted_applications", conn.execute(
    "SELECT COUNT(*) FROM submitted_applications WHERE batch_id = ?",
    (batch_id,),
).fetchone()[0])
conn.close()
PY
```

Ожидаемый результат: оба значения `0`.

### 9. Чего не делать

- Не выполняйте `DELETE` без `WHERE`.
- Не удаляйте `/data/app.db`, `/data/app.db-wal`, `/data/app.db-shm` вручную.
- Не используйте `docker compose down -v`.
- Не чистите `notification_outbox` вручную без разбора причины.
- Не удаляйте tracking завершенных массовых пачек только из-за того, что они не
  проверяются каждые 30 секунд. Это нормальная оптимизация: они проверяются архивным
  проходом.

## Когда ждать, когда перезапускать, когда делать rollback

### Достаточно подождать

- единичный `429`, `5xx`, timeout или connection reset;
- одна неудачная отправка Telegram;
- кратковременный `PENDING` в outbox;
- healthcheck показывает `starting` сразу после запуска;
- система сама пишет `recovered after ... errors`.

Обычно достаточно наблюдать 5–10 минут.

### Перезапустить контейнер

- процесс работает, но перестал обрабатывать события;
- heartbeat остается stale после восстановления Google;
- зависла сетевая библиотека;
- нет признаков повреждения SQLite;
- проблема не появилась сразу после обновления кода.

```bash
docker compose -f docker-compose.prod.yml restart bot
```

### Пересоздать контейнер

- изменен `.env`;
- изменен Compose;
- выбран новый image;
- выполнен rollback.

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate bot
```

### Выполнить rollback

- новая версия не запускается;
- после обновления сломан основной workflow;
- новая версия дает повторяемые ошибки;
- restart и force-recreate не помогают;
- предыдущая версия известна и сохранена.

### Перезагрузить VPS

- Docker daemon не отвечает;
- система зависла;
- выполнено обновление ядра;
- системные ресурсы не освобождаются после остановки контейнера.

## Критические ситуации

### Бот не отвечает, но контейнер `healthy`

Диагностика:

```bash
docker compose -f docker-compose.prod.yml logs --since=30m bot
docker compose -f docker-compose.prod.yml exec bot python -m app.health
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
```

Ищите:

- ошибки Telegram;
- `Conflict` или сообщение о другом `getUpdates`;
- зависший пользовательский запрос;
- второй контейнер с тем же ботом.

Действие:

1. Убедиться, что работает только один экземпляр.
2. Проверить `/start` другим тестовым пользователем.
3. Если повторяющихся внешних ошибок нет, выполнить `restart bot`.
4. После restart повторить проверку.

Нельзя:

- запускать второй контейнер для проверки;
- удалять volume;
- очищать SQLite.

### Контейнер `unhealthy`

```bash
CID=$(docker compose -f docker-compose.prod.yml ps -q bot)
docker inspect --format \
  '{{range .State.Health.Log}}{{.End}} exit={{.ExitCode}} {{.Output}}{{println}}{{end}}' \
  "$CID"
docker compose -f docker-compose.prod.yml exec bot python -m app.health
docker compose -f docker-compose.prod.yml logs --since=30m bot
```

Действие зависит от текста:

- stale heartbeat — проверить polling и Google API;
- SQLite error — остановить запись и проверить backup;
- configuration invalid — исправить `.env`, затем `force-recreate`;
- external checks failed — проверить Telegram/Google и подождать retry.

Не перезапускайте контейнер многократно без понимания причины.

### Контейнер остановлен или постоянно перезапускается

```bash
docker compose -f docker-compose.prod.yml ps -a
docker compose -f docker-compose.prod.yml logs --tail=200 bot
CID=$(docker compose -f docker-compose.prod.yml ps --all --quiet bot)
docker inspect --format \
  'Exit={{.State.ExitCode}} Error={{.State.Error}} Restarts={{.RestartCount}} OOMKilled={{.State.OOMKilled}}' \
  "$CID"
```

Действие:

- `OOMKilled=true` — перейти к сценарию нехватки памяти;
- configuration error — исправить `.env`;
- ошибка новой версии — rollback;
- временная внешняя ошибка не должна завершать главный процесс, поэтому ищите traceback.

### Завис heartbeat polling

Признак:

```text
heartbeat is stale
```

Проверка:

```bash
docker compose -f docker-compose.prod.yml logs --since=30m bot \
  | grep -Ei "polling|google|retry|failed|recovered"
```

Логика:

- polling обновляет heartbeat только после полного успешного цикла;
- длительная ошибка Google делает heartbeat stale;
- уже сохраненные outbox-события могут продолжать доставляться независимо.

Действие:

1. Проверить Google и права.
2. Подождать 5–10 минут при временной ошибке.
3. Если Google доступен, а heartbeat не обновляется, выполнить один restart.
4. Если не помогло, сохранить логи и передать разработчику.

### Telegram временно недоступен

Признаки:

- `Telegram outbox delivery failed`;
- `PENDING` в `notification_outbox`;
- healthcheck сообщает внешнюю ошибку.

Действие:

1. Не очищать outbox.
2. Подождать автоматический retry.
3. Проверить состояние через 5–10 минут.
4. Если записи стали `FAILED`, зафиксировать event IDs и ошибки.

Редкий дубль уведомления возможен, если Telegram принял сообщение, а процесс завершился
до фиксации `SENT` в SQLite.

### Google временно недоступен

Признаки:

- timeout, `429`, `5xx`, reset connection;
- `Status polling iteration failed`;
- растет dashboard outbox.

Действие:

1. Подождать retry 5–10 минут.
2. Проверить healthcheck.
3. Не повторять пользовательскую операцию много раз.
4. При длительном сбое уведомить пользователей о задержке.

### Google сообщает ошибку прав или схемы

Признаки:

- `403`;
- `SheetConfigurationError`;
- отсутствующий spreadsheet ID;
- неожиданные заголовки или структура листа.

Это постоянная ошибка, retry ее не исправит.

Действие:

1. Проверить ID таблиц в `.env`.
2. Проверить доступ service account ко всем обязательным таблицам.
3. Сравнить заголовки листа с документацией проекта.
4. После исправления `.env` выполнить `force-recreate`.
5. Выполнить healthcheck и тестовую заявку.

Не меняйте вручную системные заголовки и ID уже созданных заявок без согласования.

### GigaChat недоступен

Признаки:

- ошибки авторизации, timeout или SSL в логах;
- одиночная заявка не проходит LLM-проверку.

При этом массовые заявки GigaChat не используют.

Действие:

1. Проверить, повторяется ли ошибка.
2. Проверить сетевую доступность и настройки GigaChat.
3. Не перезапускать весь VPS из-за одной LLM-ошибки.
4. Если ошибка постоянная, предупредить пользователей одиночного workflow.

### Заполнен диск

```bash
df -h /
docker system df
du -sh /opt/alfa-auto-requests/backups
docker images 'alfa-auto-requests-bot'
```

Безопасные действия:

1. Удалить переданные `/tmp/*.tar` после успешного `docker load`.
2. Удалить явно ненужные старые backups.
3. Выполнить `docker image prune`.
4. Удалить только известные старые images, оставив текущий и rollback.

Нельзя:

- удалять `/var/lib/docker` вручную;
- выполнять prune с `--volumes`;
- удалять текущий или единственный rollback image;
- удалять SQLite-файлы.

### Нехватка RAM или OOM

```bash
free -h
swapon --show
docker stats --no-stream
dmesg -T | grep -Ei "out of memory|killed process|oom" | tail -n 30
```

Действие:

1. Убедиться, что swap включен.
2. Не собирать Docker image на VPS.
3. Остановить посторонние тяжелые сервисы.
4. Перезапустить только bot после стабилизации памяти.
5. Если OOM повторяется, увеличить RAM VPS.

Постоянная работа в swap означает нехватку памяти, даже если процесс не падает.

### Повреждена SQLite

Признаки:

- `sqlite quick_check failed`;
- `database disk image is malformed`;
- healthcheck не проходит SQLite.

Действие:

1. Не выполнять новые пользовательские операции.
2. Сохранить логи.
3. Остановить bot.
4. Не удалять поврежденную базу.
5. Проверить последний backup.
6. Выполнить восстановление по инструкции выше.

### Накопились `FAILED`-уведомления

```bash
docker compose -f docker-compose.prod.yml exec -T bot python - <<'PY'
import sqlite3

connection = sqlite3.connect("file:/data/app.db?mode=ro", uri=True)
for row in connection.execute(
    """
    SELECT event_id, telegram_user_id, attempts,
           substr(last_error, 1, 300), updated_at
    FROM notification_outbox
    WHERE state = 'FAILED'
    ORDER BY updated_at DESC
    """
):
    print(row)
connection.close()
PY
```

Действие:

1. Исправить причину Telegram-доставки.
2. Сохранить event IDs и ошибки.
3. Не переводить записи вручную в `PENDING`.
4. Передать список разработчику для контролируемого повторного запуска или ручного
   уведомления пользователей.

### Не обновляется дашборд

Источник правды — рабочие таблицы направлений, а не дашборд.

Действие:

1. Проверить рабочие таблицы.
2. Проверить `dashboard_outbox`.
3. Проверить Google-права на дашборд.
4. Подождать автоматический retry.
5. Не повторять Telegram-уведомления из-за ошибки дашборда.

### Неудачное обновление

Если контейнер не стал healthy или сломан workflow:

1. Сохранить логи.
2. Выполнить rollback на `PREVIOUS_VERSION`.
3. Проверить healthcheck.
4. Выполнить smoke-тест.
5. При несовместимости данных восстановить backup.

Не пытайтесь несколько раз загружать тот же проблемный image под разными тегами.

### VPS перезагрузился

```bash
uptime
systemctl status docker --no-pager
cd /opt/alfa-auto-requests
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --since=30m bot
docker compose -f docker-compose.prod.yml exec bot python -m app.health
```

Если контейнер был остановлен вручную до reboot, `unless-stopped` может не запустить его.
Тогда:

```bash
docker compose -f docker-compose.prod.yml start bot
```

### Случайно запущены два экземпляра

```bash
docker ps --format 'table {{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}'
```

Признаки:

- несколько контейнеров приложения;
- Telegram `Conflict`;
- дублирующиеся действия;
- старый контейнер остался после ручного запуска.

Действие:

1. Определить контейнер из `/opt/alfa-auto-requests`.
2. Оставить production-контейнер текущего `APP_VERSION`.
3. Остановить подтвержденный лишний контейнер:

```bash
docker stop CONTAINER_NAME
```

4. Проверить основной контейнер и логи.

Не останавливайте контейнер только по похожему имени без проверки image и каталога
Compose.

### Потеря VPS без внешнего backup

Без копии SQLite вне VPS восстановить локальные черновики, tracking и outbox невозможно.
Рабочие заявки, уже записанные в Google Sheets, сохранятся.

Порядок действий:

1. Поднять новый VPS.
2. Установить Docker.
3. Развернуть приложение и настройки.
4. Создать новую пустую SQLite.
5. Проверить существующие Google Sheets.
6. Согласовать с разработчиком восстановление tracking по Google Sheets.
7. Предупредить пользователей, что часть незавершенных черновиков и уведомлений потеряна.

## Таблица команд и рисков

| Команда | Что делает | Когда применять | Риск |
|---|---|---|---|
| `docker compose ... ps` | Показывает состояние сервисов | Всегда для начала диагностики | Нет |
| `docker compose ... logs` | Читает логи | При ошибках и после обновления | Нет |
| `exec bot python -m app.health` | Проверяет heartbeat, SQLite и внешние API | Ежедневно и после изменений | Низкий, делает внешние probes по кэшу |
| `docker stats --no-stream` | Показывает ресурсы | Контроль RAM и CPU | Нет |
| `restart bot` | Перезапускает текущий контейнер | Зависание без изменения конфигурации | Короткий перерыв |
| `up -d --force-recreate bot` | Пересоздает контейнер | Новый image или `.env` | Короткий перерыв |
| `stop bot` | Останавливает бот | Перед restore | Пользователи не обслуживаются |
| `start bot` | Запускает остановленный контейнер | После обслуживания | Низкий |
| `--profile maintenance run --rm backup` | Создает и проверяет SQLite backup | Перед изменениями | Низкий |
| `docker image prune` | Удаляет dangling images | При нехватке диска | Низкий после просмотра |
| `docker image rm TAG` | Удаляет конкретный image | Только для старой ненужной версии | Можно потерять rollback |
| `sudo reboot` | Перезагружает VPS | Системная проблема или ядро | Полный краткий простой |
| `docker compose down -v` | Удаляет сервисы и volume | Не применять | Потеря данных |
| `docker system prune -a --volumes` | Массовая очистка Docker | Не применять в пилоте | Потеря images и volumes |

## Журнал пилота

Ведите таблицу в отдельном файле или рабочем документе:

| Дата и время | Версия | Причина | Действие | Backup | Результат | Примечание |
|---|---|---|---|---|---|---|
| 2026-06-15 10:00 | `4f2c9ab` | Первый запуск | Deploy | `app-...db` | Успех | Smoke-тест пройден |

Фиксируйте:

- обновления;
- rollback;
- restore;
- длительные внешние сбои;
- OOM и заполнение диска;
- ручные изменения `.env`;
- жалобы пользователей, подтвердившиеся в логах.

## Шаблон сообщения пользователям

Кратковременная проблема:

```text
Бот временно обрабатывает заявки с задержкой из-за технической проблемы.
Уже отправленные данные не нужно отправлять повторно. Сообщу после восстановления.
```

Плановое обновление:

```text
Выполняется плановое обновление бота. Ожидаемый перерыв — до 10 минут.
Во время обновления не нажимайте повторно кнопки отправки и регистрации.
```

Восстановление:

```text
Работа бота восстановлена. Можно продолжать создание заявок.
Если действие было начато до сбоя, сначала проверьте текущее сообщение и Google Sheets,
а затем повторяйте операцию.
```

## Контрольный список перед завершением пилота

- [ ] Все инциденты внесены в журнал.
- [ ] Сохранена последняя рабочая версия image.
- [ ] Создан и проверен финальный backup.
- [ ] При необходимости backup скачан с VPS.
- [ ] Проверены `FAILED`-уведомления.
- [ ] Проверена очередь дашборда.
- [ ] Зафиксировано использование CPU, RAM, swap и диска.
- [ ] Собраны предложения пользователей.
- [ ] Определено, какие эксплуатационные действия нужно автоматизировать.

## Официальные справочные материалы

- [Установка Docker Engine на Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Установка Docker Compose plugin](https://docs.docker.com/compose/install/linux/)
- [Политики автоматического перезапуска Docker](https://docs.docker.com/engine/containers/start-containers-automatically/)
- [Команда docker compose up](https://docs.docker.com/reference/cli/docker/compose/up/)
- [Команда docker compose restart](https://docs.docker.com/reference/cli/docker/compose/restart/)
