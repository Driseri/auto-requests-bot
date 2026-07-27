from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .admin_delete import AdminDeleteService
from .application_report import ApplicationReportCollector
from .collector import DashboardCollector
from .config import DEFAULT_CONFIG_PATH, load_config, validate_config
from .schemas import (
    AdminDeleteExecuteRequest,
    AdminDeleteRequest,
    AdminDeleteResponse,
    ApplicationReportResponse,
    CollectResponse,
)
from .storage import JsonStorage


def create_app(
    *,
    config_path: Path | str | None = None,
    collector: DashboardCollector | None = None,
    application_report_collector: ApplicationReportCollector | None = None,
    admin_delete_service: AdminDeleteService | None = None,
) -> FastAPI:
    """Create FastAPI app; tests can inject a collector to avoid real SSH."""

    resolved_config_path = Path(
        config_path or os.getenv("DASHBOARD_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    config = load_config(resolved_config_path)
    storage = JsonStorage(config.storage.data_dir)
    storage.ensure_ready()
    active_collector = collector or DashboardCollector(config=config, storage=storage)
    active_application_report_collector = application_report_collector or ApplicationReportCollector(
        config=config,
        storage=storage,
    )
    active_admin_delete_service = admin_delete_service or AdminDeleteService(
        config=config,
        storage=storage,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Run background collection only while the local API process is alive."""

        if not app.state.config_errors:
            await app.state.collector.start_background()
        try:
            yield
        finally:
            await app.state.collector.stop_background()

    app = FastAPI(
        title="Alfa Auto Bot Monitoring Dashboard Backend",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.config_errors = validate_config(config)
    app.state.storage = storage
    app.state.collector = active_collector
    app.state.application_report_collector = active_application_report_collector
    app.state.admin_delete_service = active_admin_delete_service
    static_dir = Path(__file__).resolve().parents[1] / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        """Serve the local read-only dashboard page."""

        index_path = static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="Dashboard UI is not installed")
        return FileResponse(index_path)

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        """Report local backend health without touching the VPS."""

        state = app.state.storage.load_state()
        return {
            "ok": not app.state.config_errors and app.state.storage.is_writable(),
            "config_errors": app.state.config_errors,
            "storage_writable": app.state.storage.is_writable(),
            "last_collection": state.model_dump(mode="json"),
        }

    @app.get("/api/config/safe")
    async def safe_config() -> dict[str, object]:
        """Expose non-secret configuration for UI diagnostics."""

        return app.state.config.safe_dict()

    @app.get("/api/snapshot/latest")
    async def latest_snapshot() -> dict[str, object]:
        """Return the latest stored snapshot without triggering SSH collection."""

        snapshot = app.state.storage.load_latest()
        if snapshot is None:
            raise HTTPException(status_code=404, detail="No snapshot collected yet")
        return snapshot.model_dump(mode="json")

    @app.post("/api/collect", response_model=CollectResponse)
    async def collect(force: bool = Query(default=True)) -> CollectResponse:
        """Trigger one manual collection cycle."""

        if app.state.config_errors:
            raise HTTPException(status_code=400, detail=app.state.config_errors)
        return await app.state.collector.collect(force=force)

    @app.get("/api/history")
    async def history(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, object]:
        """Return recent local JSONL history, newest first."""

        return {"items": app.state.storage.load_history(limit=limit)}

    @app.get("/api/applications/report/latest")
    async def latest_application_report() -> dict[str, object]:
        """Return the latest stored application report without SSH collection."""

        report = app.state.storage.load_latest_application_report()
        if report is None:
            raise HTTPException(status_code=404, detail="No application report collected yet")
        return report.model_dump(mode="json")

    @app.post("/api/applications/report", response_model=ApplicationReportResponse)
    async def collect_application_report() -> ApplicationReportResponse:
        """Trigger one manual read-only application report collection."""

        if app.state.config_errors:
            raise HTTPException(status_code=400, detail=app.state.config_errors)
        return await app.state.application_report_collector.collect()

    @app.post("/api/admin/delete/preview", response_model=AdminDeleteResponse)
    async def admin_delete_preview(request: AdminDeleteRequest) -> AdminDeleteResponse:
        """Preview application deletion without writing to production SQLite."""

        if app.state.config_errors:
            raise HTTPException(status_code=400, detail=app.state.config_errors)
        try:
            return await app.state.admin_delete_service.preview(request.application_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/admin/delete/execute", response_model=AdminDeleteResponse)
    async def admin_delete_execute(request: AdminDeleteExecuteRequest) -> AdminDeleteResponse:
        """Execute confirmed destructive application deletion."""

        if app.state.config_errors:
            raise HTTPException(status_code=400, detail=app.state.config_errors)
        try:
            result = await app.state.admin_delete_service.execute(
                application_ids=request.application_ids,
                confirmation=request.confirmation,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result.skipped:
            raise HTTPException(status_code=400, detail=result.reason)
        return result

    return app


app = create_app()
