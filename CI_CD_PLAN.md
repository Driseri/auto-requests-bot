# CI/CD для деплоя Telegram-бота на VPS

## Цель

Сделать деплой предсказуемым: изменения попадают на VPS только после тестов, сборки Docker-образа и контролируемого перезапуска контейнера. Секреты не хранятся в репозитории и не попадают в Docker image.

## Рекомендуемая схема

Лучший вариант для проекта: GitHub Actions + Docker image registry + SSH deploy на VPS.

Пайплайн:

1. Разработчик пушит изменения в GitHub.
2. CI запускает проверки:
   - `python -m compileall src tests`;
   - `pytest`;
   - `docker compose build`;
   - `docker compose run --rm bot pytest`.
3. Если проверки прошли, собирается production Docker image.
4. Image публикуется в GitHub Container Registry или Docker Hub.
5. CD по SSH подключается к VPS.
6. VPS подтягивает новый image.
7. `docker compose up -d --force-recreate bot`.
8. CD проверяет, что контейнер жив и polling стартовал.

## Что изменить в проекте

### 1. Разделить локальный и production compose

Текущий `docker-compose.yml` удобен для локальной сборки, потому что использует:

```yaml
build: .
```

Для VPS лучше использовать готовый image:

```yaml
services:
  bot:
    image: ghcr.io/<owner>/alfa-auto-requests-bot:${APP_VERSION:-latest}
    env_file:
      - path: .env
        required: true
    environment:
      SQLITE_PATH: /data/app.db
      GOOGLE_CREDENTIALS_PATH: /run/secrets/google_credentials.json
    volumes:
      - bot-data:/data
      - ./credentials.json:/run/secrets/google_credentials.json:ro
    restart: unless-stopped

volumes:
  bot-data:
```

Файл можно назвать:

```text
docker-compose.prod.yml
```

На VPS запускать:

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate bot
```

### 2. Секреты хранить вне репозитория

На VPS должны оставаться:

```text
/opt/alfa-auto-requests/.env
/opt/alfa-auto-requests/credentials.json
```

В GitHub Secrets хранить только доступы для деплоя:

```text
VPS_HOST
VPS_USER
VPS_SSH_KEY
VPS_APP_DIR=/opt/alfa-auto-requests
GHCR_TOKEN или GITHUB_TOKEN
```

Не хранить в GitHub Secrets без необходимости:

```text
TELEGRAM_BOT_TOKEN
GIGACHAT_CREDENTIALS
GOOGLE credentials.json
```

Эти секреты уже лежат на VPS и не должны перетираться при деплое.

## GitHub Actions

### CI workflow

Файл:

```text
.github/workflows/ci.yml
```

Логика:

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Compile
        run: python -m compileall src tests

      - name: Unit tests
        run: pytest

      - name: Docker build
        run: docker compose build

      - name: Docker tests
        run: docker compose run --rm bot pytest
```

### CD workflow

Файл:

```text
.github/workflows/deploy.yml
```

Запускать только после push в `main` и успешного CI.

Логика:

```yaml
name: Deploy

on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]
    branches: [main]

jobs:
  deploy:
    if: ${{ github.event.workflow_run.conclusion == 'success' }}
    runs-on: ubuntu-latest

    permissions:
      contents: read
      packages: write

    steps:
      - uses: actions/checkout@v4

      - name: Log in to GHCR
        run: echo "${{ secrets.GITHUB_TOKEN }}" | docker login ghcr.io -u ${{ github.actor }} --password-stdin

      - name: Build and push image
        run: |
          IMAGE=ghcr.io/${{ github.repository_owner }}/alfa-auto-requests-bot
          TAG=${{ github.sha }}
          docker build -t $IMAGE:$TAG -t $IMAGE:latest .
          docker push $IMAGE:$TAG
          docker push $IMAGE:latest

      - name: Deploy over SSH
        uses: appleboy/ssh-action@v1.2.0
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            set -e
            cd /opt/alfa-auto-requests
            docker compose -f docker-compose.prod.yml pull bot
            docker compose -f docker-compose.prod.yml up -d --force-recreate bot
            docker compose -f docker-compose.prod.yml ps
            docker compose -f docker-compose.prod.yml logs --tail=80 bot
```

## Подготовка VPS

На сервере один раз:

```bash
cd /opt/alfa-auto-requests
```

Положить:

```text
docker-compose.prod.yml
.env
credentials.json
```

Права:

```bash
chmod 600 .env credentials.json
```

Проверка:

```bash
docker compose -f docker-compose.prod.yml config --quiet
```

## Rollback

Если новый релиз сломался, нужен быстрый откат на предыдущий image tag.

В `.env` на VPS можно держать:

```env
APP_VERSION=latest
```

Для отката заменить `APP_VERSION` на предыдущий commit SHA:

```bash
cd /opt/alfa-auto-requests
nano .env
docker compose -f docker-compose.prod.yml up -d --force-recreate bot
docker compose -f docker-compose.prod.yml logs -f bot
```

Лучше хранить последние 5-10 успешных SHA в GitHub Actions summary или отдельном `releases.md`.

## Health-check после деплоя

Минимальная проверка:

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=80 bot
```

В логах должно быть:

```text
Start polling
Run polling for bot
```

Также стоит проверять отсутствие:

```text
Traceback
TelegramNetworkError
GigaChat check failed
Не удалось отправить заявку
```

## Что не делать

- Не копировать `.env` и `credentials.json` из CI на сервер при каждом деплое.
- Не собирать образ на VPS при каждом деплое, если есть registry.
- Не использовать `docker compose restart bot` для обновления env/image.
- Не хранить `credentials.json` в репозитории.
- Не деплоить с локальной машины архивом как основной процесс после появления CI/CD.

## Минимальный первый этап

Если нужно внедрять постепенно:

1. Добавить только CI: tests + docker build.
2. Добавить `docker-compose.prod.yml`.
3. На VPS один раз положить prod compose.
4. Добавить CD через SSH.
5. Перейти с ручного `deploy.tar.gz` на deploy из GitHub Actions.

