from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


StatusLevel = Literal["OK", "DEGRADED", "ACTION_REQUIRED", "CRITICAL", "UNKNOWN"]


class CollectionState(BaseModel):
    """Local timestamps that decide which remote sections are due."""

    last_fast_at: str | None = None
    last_logs_at: str | None = None
    last_heavy_at: str | None = None
    last_success_at: str | None = None
    last_error_at: str | None = None
    last_error: str | None = None


class Summary(BaseModel):
    """Operator-facing status summary."""

    status: StatusLevel = "UNKNOWN"
    recommendations: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class Snapshot(BaseModel):
    """Normalized dashboard snapshot stored as JSON and served to the frontend."""

    collected_at: str
    source_host: str
    collection_status: Literal["ok", "failed"]
    collection_errors: list[str] = Field(default_factory=list)
    sections_collected: dict[str, bool] = Field(default_factory=dict)
    summary: Summary = Field(default_factory=Summary)
    container: dict[str, Any] = Field(default_factory=dict)
    vps: dict[str, Any] = Field(default_factory=dict)
    polling: dict[str, Any] = Field(default_factory=dict)
    external_health: dict[str, Any] = Field(default_factory=dict)
    sqlite_metrics: dict[str, Any] = Field(default_factory=dict)
    queues: dict[str, Any] = Field(default_factory=dict)
    applications: dict[str, Any] = Field(default_factory=dict)
    bulk: dict[str, Any] = Field(default_factory=dict)
    urgent: dict[str, Any] = Field(default_factory=dict)
    business: dict[str, Any] = Field(default_factory=dict)
    pilot: dict[str, Any] = Field(default_factory=dict)
    drafts: dict[str, Any] = Field(default_factory=dict)
    log_events: dict[str, Any] = Field(default_factory=dict)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class CollectResponse(BaseModel):
    """Response returned by manual collection endpoint."""

    skipped: bool = False
    reason: str | None = None
    snapshot: Snapshot | None = None


class ApplicationReport(BaseModel):
    """Manager-facing read-only report with problematic application metadata."""

    collected_at: str
    source_host: str
    collection_status: Literal["ok", "failed"]
    collection_errors: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    lost: list[dict[str, Any]] = Field(default_factory=list)
    urgent_without_final_answer: list[dict[str, Any]] = Field(default_factory=list)
    without_owner: list[dict[str, Any]] = Field(default_factory=list)
    needs_clarification: list[dict[str, Any]] = Field(default_factory=list)
    stale_without_movement: list[dict[str, Any]] = Field(default_factory=list)
    problematic_bulk_reservations: list[dict[str, Any]] = Field(default_factory=list)
    unfinished_workflows: list[dict[str, Any]] = Field(default_factory=list)


class ApplicationReportResponse(BaseModel):
    """Response returned by the manual application report endpoint."""

    skipped: bool = False
    reason: str | None = None
    report: ApplicationReport | None = None


class AdminDeleteRequest(BaseModel):
    """Request for previewing application deletion by application IDs."""

    application_ids: list[str] = Field(default_factory=list)


class AdminDeleteExecuteRequest(AdminDeleteRequest):
    """Request for executing destructive deletion after exact confirmation."""

    confirmation: str = ""


class AdminDeleteResponse(BaseModel):
    """Preview or execution result for destructive SQLite cleanup."""

    skipped: bool = False
    reason: str | None = None
    audit_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
