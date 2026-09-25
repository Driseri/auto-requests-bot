# Final Technical Release - 2026-09-25

## Release identity

- Git tag: `project-final-20260925`
- Docker image: `alfa-auto-requests-bot:20260925-final`
- Runtime: Python 3.12, aiogram 3, SQLite, Google Sheets API, GigaChat
- Production database is preserved in the existing Docker volume.

This release freezes the final project implementation before controlled shutdown.
Deploying it does not stop the bot and does not remove infrastructure or data.

## GigaChat configuration

- Model: `GigaChat-2-Pro`
- System prompt: `prompts/gigachat_system_v6.2_recommendation.md`
  - SHA-256: `fb9c59be128c562cd29fc6688ae0873164eb0c8908d8ec19c37daa47b834ee56`
- User prompt: `prompts/gigachat_user_v6.1_recommendation.md`
  - SHA-256: `65053e78be6bdd502db7c165419e0630f7dae46effa737853d1942b7e3dbbfd9`

## Required release checks

- `pytest -q`
- `ruff check src tests`
- `python -m compileall -q src tests`
- `docker compose config --quiet`
- `APP_VERSION=20260925-final docker compose -f docker-compose.prod.yml config --quiet`
- production health check after deployment
- `PRAGMA integrity_check` against production SQLite after deployment
- verification of the deployed image tag and prompt hashes

## Data preservation

The final data export is stored outside Git under
`reports/final-exports/final-export-20260925/`. It contains the verified SQLite
backup, table exports, Google Sheets CSV snapshots, prompt copies, checksums and
a complete Git bundle. Secrets and environment files are excluded.

Before deployment, create an additional server-side SQLite backup. Do not use
`docker compose down -v` during deployment or shutdown because it removes the
database volume.

## Shutdown boundary

Final shutdown is a separate operation. It requires confirming the handling of
active drafts, open applications, pending outbox entries and user communication
before stopping the container and revoking credentials.

