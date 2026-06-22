# Local Bot Monitoring Dashboard

FastAPI backend and static web UI for local, read-only monitoring of the production bot VPS.

## Safety Model

- Runs locally on the administrator machine.
- Does not run a web service on the VPS.
- Uses SSH only for short read-only commands.
- Reads production SQLite with `file:/data/app.db?mode=ro` and `PRAGMA query_only=ON`.
- Stores snapshots locally in `dashboard/data`.
- Does not expose SSH password through API.

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

## Frontend

The web page is based on the MVP monitoring reference, adapted for the current backend:

- one screen, no sidebar navigation;
- explicit `read-only` state;
- no write actions, restart buttons, cleanup buttons, or rollback buttons;
- normal page refresh reads only local `latest.json` through `/api/snapshot/latest`;
- the `Обновить` button calls `/api/collect`, which performs one read-only SSH collection;
- unavailable historical metrics are shown as `нет истории` or `нет данных` instead of fake charts.

The page currently renders these MVP blocks:

- overall status;
- container;
- VPS;
- polling;
- Telegram;
- Google;
- GigaChat;
- dashboard outbox;
- applications;
- bulk batches;
- urgent applications;
- recent errors;
- notification outbox.

Historical charts, 24h counters, polling duration percentiles, RSS trend, and restart deltas
should be added only after enough local snapshots are accumulated or the backend exposes
those values explicitly.

## Tests

```powershell
cd dashboard
python -m pytest --basetemp .pytest-tmp-dashboard -o cache_dir=.pytest-tmp-dashboard-cache
```

The explicit `basetemp` and `cache_dir` keep pytest temporary files inside the workspace on
restricted Windows environments.
