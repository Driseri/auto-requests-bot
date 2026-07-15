# Local Bot Monitoring Dashboard

FastAPI backend and static web UI for local, read-only monitoring of the production bot VPS.

## Safety Model

- Runs locally on the administrator machine.
- Does not run a web service on the VPS.
- Uses SSH only for short read-only commands.
- Reads production SQLite with `file:/data/app.db?mode=ro` and `PRAGMA query_only=ON`.
- Stores snapshots and application reports locally in `dashboard/data`.
- Does not expose SSH password through API.
- Restart, rollback, and Google Sheets writes are not implemented.
- SQLite delete actions are disabled by default and require `[admin].destructive_actions_enabled = true`.

## Setup

```powershell
cd dashboard
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.toml config.local.toml
```

Edit `config.local.toml` and set SSH host, username and password.

## Run

```powershell
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8080
```

Open the dashboard page:

- `http://127.0.0.1:8080/`

The frontend is a static page served by FastAPI from `dashboard/static`. It does not need
Node.js, npm, a separate build step, or a second web server.

Useful endpoints:

- `GET http://127.0.0.1:8080/`
- `GET http://127.0.0.1:8080/api/health`
- `POST http://127.0.0.1:8080/api/collect`
- `GET http://127.0.0.1:8080/api/snapshot/latest`
- `GET http://127.0.0.1:8080/api/history?limit=100`
- `GET http://127.0.0.1:8080/api/config/safe`
- `POST http://127.0.0.1:8080/api/applications/report`
- `GET http://127.0.0.1:8080/api/applications/report/latest`
- `POST http://127.0.0.1:8080/api/admin/delete/preview`
- `POST http://127.0.0.1:8080/api/admin/delete/execute`

## Frontend

The web page uses a compact operations-dashboard layout adapted for the current backend:

- fixed left navigation rail;
- active `Мониторинг` and `Заявки` tabs;
- future navigation items are visible but disabled;
- explicit `Только чтение` state;
- normal page refresh reads local `latest.json` through `/api/snapshot/latest`;
- the `Обновить` button calls `/api/collect`, which performs one read-only SSH collection.

The `Заявки` tab is intentionally manual:

- it first reads local `dashboard/data/application-reports/latest.json`;
- `Получить актуальные заявки` calls `/api/applications/report`;
- the backend runs one bounded SSH command and read-only SQLite queries;
- failed collections are saved to history but do not overwrite the last successful report;
- only metadata is shown: IDs, statuses, sheet/row links, counters, timestamps and workflow states.

The application report includes:

- lost applications: `polling_state != 'ACTIVE' OR not_found_count > 0`;
- open urgent applications without a result: ADD/EDIT needs an editor final answer, while CHIPS needs `Принята` (single) or `Принято` (bulk);
- open applications without an owner/editor;
- applications needing clarification;
- open applications without movement for more than 24 hours;
- problematic new bulk reservations: `FAILED` or active for more than 24 hours;
- unfinished user workflows.

## Metric Rules

- The dashboard reads only the current bulk workflow from `bulk_reservations`. Legacy
  `bulk_batches` and `bulk_creation_requests` are not collected or displayed.
- An ADD/EDIT application is complete when the final-answer field is populated or its
  status is `Итоговый ответ готов`.
- A CHIPS application is complete when its status is `Принята` or `Принято`; it is not
  treated as unfinished merely because its final-answer field is empty.
- `Отклонена`, `Отложена`, and `Удаление` are closed states and are excluded from open,
  no-owner, and no-movement alerts.
- `Итоговый ответ готов сегодня` counts exact final-answer events for ADD/EDIT only.

## Admin Delete

The `Удаление` tab is a local administrator tool for deleting SQLite metadata by
`application_id`. It does not delete rows from Google Sheets and does not stop the bot
container.

Destructive execution is disabled until this is set in `config.local.toml`:

```toml
[admin]
destructive_actions_enabled = true
max_delete_ids = 20
```

The delete flow is:

- preview target IDs through `/api/admin/delete/preview`;
- verify affected rows and exact confirmation phrase;
- execute through `/api/admin/delete/execute`;
- write a local audit/export row into `dashboard/data/admin-deletes/YYYY-MM-DD.jsonl`.

## Tests

```powershell
cd dashboard
python -m pytest --basetemp .pytest-tmp-dashboard -o cache_dir=.pytest-tmp-dashboard-cache
```

The explicit `basetemp` and `cache_dir` keep pytest temporary files inside the workspace on
restricted Windows environments.
