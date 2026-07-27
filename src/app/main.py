from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from app.bot import create_router
from app.bulk import (
    BulkReservationRegistrar,
    GoogleSheetsBulkReservationService,
)
from app.config import load_settings
from app.flow import ApplicationFlow
from app.llm import LlmClient
from app.notifications import (
    GoogleSheetsStatusReader,
    StatusNotificationService,
    run_status_polling_loop,
)
from app.repository import DraftRepository
from app.scheduling import cutoff_to_string
from app.submission import (
    DashboardSyncService,
    DirectionSpreadsheetConfig,
    GoogleSheetsSubmissionService,
)


async def main() -> None:
    """Собрать зависимости и запустить Telegram и status polling."""
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    logging.info(
        "Starting bot: sqlite_path=%s google_dashboard_configured=%s "
        "direction_sheets_configured=%s/%s/%s/%s legacy_google_sheet=%s gigachat_model=%s "
        "telegram_proxy_configured=%s telegram_request_timeout=%s "
        "gigachat_scope=%s gigachat_credentials_configured=%s "
        "gigachat_base_url=%s gigachat_auth_url=%s "
        "gigachat_verify_ssl=%s gigachat_ca_bundle_configured=%s "
        "gigachat_timeout=%s gigachat_max_retries=%s "
        "gigachat_retry_backoff_factor=%s gigachat_show_response_json=%s "
        "status_polling_enabled=%s status_polling_interval_seconds=%s "
        "dashboard_sync_interval_seconds=%s "
        "bulk_max_rows=%s bulk_reserved_rows=%s bulk_registration_stale_seconds=%s "
        "bot_timezone=%s rollout_wednesday_cutoff=%s rollout_thursday_cutoff=%s "
        "application_editors_count=%s daily_sheet_grouping_enabled=%s "
        "daily_sheet_maintenance_enabled=%s daily_sheet_maintenance_time=%s",
        settings.sqlite_path,
        bool(settings.google_dashboard_spreadsheet_id),
        bool(settings.google_fl_spreadsheet_id),
        bool(settings.google_sme_spreadsheet_id),
        bool(settings.google_ai_spreadsheet_id),
        bool(settings.google_voice_collection_spreadsheet_id),
        settings.google_sheet_name,
        settings.gigachat_model,
        bool(settings.telegram_proxy_url),
        settings.telegram_request_timeout,
        settings.gigachat_scope,
        bool(settings.gigachat_credentials),
        settings.gigachat_base_url,
        settings.gigachat_auth_url,
        settings.gigachat_verify_ssl_certs,
        bool(settings.gigachat_ca_bundle_file),
        settings.gigachat_timeout,
        settings.gigachat_max_retries,
        settings.gigachat_retry_backoff_factor,
        settings.gigachat_show_response_json,
        settings.status_polling_enabled,
        settings.status_polling_interval_seconds,
        settings.dashboard_sync_interval_seconds,
        settings.bulk_max_rows,
        settings.bulk_reserved_rows,
        settings.bulk_registration_stale_seconds,
        settings.rollout_schedule.timezone_name,
        cutoff_to_string(settings.rollout_schedule.wednesday_cutoff),
        cutoff_to_string(settings.rollout_schedule.thursday_cutoff),
        len(settings.application_editors),
        settings.daily_sheet_grouping_enabled,
        settings.daily_sheet_maintenance_enabled,
        settings.daily_sheet_maintenance_time,
    )

    repository = DraftRepository(settings.sqlite_path)
    await repository.init()
    direction_spreadsheets = DirectionSpreadsheetConfig(
        fl_spreadsheet_id=settings.google_fl_spreadsheet_id,
        sme_spreadsheet_id=settings.google_sme_spreadsheet_id,
        ai_spreadsheet_id=settings.google_ai_spreadsheet_id,
        voice_collection_spreadsheet_id=settings.google_voice_collection_spreadsheet_id,
    )
    dashboard_sync = (
        DashboardSyncService(
            spreadsheet_id=settings.google_dashboard_spreadsheet_id,
            credentials_path=settings.google_credentials_path,
            application_editors=settings.application_editors,
            timezone_name=settings.rollout_schedule.timezone_name,
        )
        if settings.google_dashboard_spreadsheet_id
        else None
    )
    submission_service = GoogleSheetsSubmissionService(
        direction_spreadsheets=direction_spreadsheets,
        dashboard_spreadsheet_id=settings.google_dashboard_spreadsheet_id,
        credentials_path=settings.google_credentials_path,
        rollout_schedule=settings.rollout_schedule,
        timezone_name=settings.rollout_schedule.timezone_name,
        application_editors=settings.application_editors,
        dashboard_sync=dashboard_sync,
        google_api_retry=settings.google_api_retry,
        repository=repository,
        daily_sheet_grouping_enabled=settings.daily_sheet_grouping_enabled,
    )
    bulk_reservation_service = GoogleSheetsBulkReservationService(
        submission_service=submission_service,
        repository=repository,
        google_api_retry=settings.google_api_retry,
        daily_sheet_grouping_enabled=settings.daily_sheet_grouping_enabled,
    )
    bulk_reservation_registrar = BulkReservationRegistrar(
        repository=repository,
        credentials_path=settings.google_credentials_path,
        application_editors=settings.application_editors,
        urgent_editor_notifications_enabled=(
            settings.urgent_editor_notifications_enabled
        ),
        editor_urgent_chat_id=settings.editor_urgent_chat_id,
        google_api_retry=settings.google_api_retry,
        timezone_name=settings.rollout_schedule.timezone_name,
    )
    flow = ApplicationFlow(
        repository=repository,
        llm_client=LlmClient(
            credentials=settings.gigachat_credentials,
            base_url=settings.gigachat_base_url,
            auth_url=settings.gigachat_auth_url,
            scope=settings.gigachat_scope,
            model=settings.gigachat_model,
            verify_ssl_certs=settings.gigachat_verify_ssl_certs,
            ca_bundle_file=settings.gigachat_ca_bundle_file,
            timeout=settings.gigachat_timeout,
            max_retries=settings.gigachat_max_retries,
            retry_backoff_factor=settings.gigachat_retry_backoff_factor,
            system_prompt_path=settings.gigachat_system_prompt_path,
            user_prompt_path=settings.gigachat_user_prompt_path,
        ),
        show_llm_response_json=settings.gigachat_show_response_json,
        submission_service=submission_service,
        bulk_reservation_service=bulk_reservation_service,
        bulk_reservation_registrar=bulk_reservation_registrar,
        bulk_max_rows=settings.bulk_max_rows,
        bulk_reserved_rows=settings.bulk_reserved_rows,
        bulk_creation_stale_seconds=settings.bulk_creation_stale_seconds,
        dashboard_enabled=bool(settings.google_dashboard_spreadsheet_id),
        urgent_editor_notifications_enabled=(
            settings.urgent_editor_notifications_enabled
        ),
        editor_urgent_chat_id=settings.editor_urgent_chat_id,
    )

    session = AiohttpSession(
        proxy=settings.telegram_proxy_url,
        timeout=settings.telegram_request_timeout,
    )
    bot = Bot(token=settings.telegram_bot_token, session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(flow))

    polling_task: asyncio.Task | None = None
    daily_maintenance_task: asyncio.Task | None = None
    if settings.status_polling_enabled:
        polling_task = asyncio.create_task(
            run_status_polling_loop(
                service=StatusNotificationService(
                    repository=repository,
                    status_reader=GoogleSheetsStatusReader(
                        direction_spreadsheets=direction_spreadsheets,
                        credentials_path=settings.google_credentials_path,
                        google_api_retry=settings.google_api_retry,
                    ),
                    dashboard_sync=dashboard_sync,
                    legacy_bulk_enabled=False,
                    dashboard_sync_interval_seconds=(
                        settings.dashboard_sync_interval_seconds
                    ),
                    status_not_found_threshold=settings.status_not_found_threshold,
                    status_not_found_recheck_seconds=(
                        settings.status_not_found_recheck_seconds
                    ),
                    completed_bulk_dashboard_scan_interval_seconds=(
                        settings.completed_bulk_dashboard_scan_interval_seconds
                    ),
                    bulk_relocation_search_interval_seconds=(
                        settings.bulk_relocation_search_interval_seconds
                    ),
                    dashboard_outbox_retry_base_seconds=(
                        settings.dashboard_outbox_retry_base_seconds
                    ),
                    dashboard_outbox_retry_max_seconds=(
                        settings.dashboard_outbox_retry_max_seconds
                    ),
                    dashboard_outbox_sending_stale_seconds=(
                        settings.dashboard_outbox_sending_stale_seconds
                    ),
                    google_api_retry=settings.google_api_retry,
                    notifier=bot,
                    fallback_spreadsheet_id=settings.google_dashboard_spreadsheet_id,
                    notification_max_attempts=settings.notification_max_attempts,
                    notification_retry_base_seconds=(
                        settings.notification_retry_base_seconds
                    ),
                    notification_sending_stale_seconds=(
                        settings.notification_sending_stale_seconds
                    ),
                    notification_message_max_chars=(
                        settings.notification_message_max_chars
                    ),
                    urgent_editor_notifications_enabled=(
                        settings.urgent_editor_notifications_enabled
                    ),
                    editor_urgent_chat_id=settings.editor_urgent_chat_id,
                ),
                interval_seconds=settings.status_polling_interval_seconds,
                heartbeat_path=settings.status_polling_heartbeat_path,
                memory_log_interval=settings.status_polling_memory_log_interval,
            )
        )
    if (
        settings.daily_sheet_grouping_enabled
        and settings.daily_sheet_maintenance_enabled
    ):
        daily_maintenance_task = asyncio.create_task(
            run_daily_sheet_maintenance_loop(
                submission_service=submission_service,
                time_hhmm=settings.daily_sheet_maintenance_time,
                timezone_name=settings.rollout_schedule.timezone_name,
            )
        )

    try:
        await dispatcher.start_polling(bot)
    finally:
        if polling_task is not None:
            polling_task.cancel()
        if daily_maintenance_task is not None:
            daily_maintenance_task.cancel()
        await asyncio.gather(
            *[
                task
                for task in (polling_task, daily_maintenance_task)
                if task is not None
            ],
            return_exceptions=True,
        )


async def run_daily_sheet_maintenance_loop(
    *,
    submission_service: GoogleSheetsSubmissionService,
    time_hhmm: str,
    timezone_name: str,
) -> None:
    while True:
        delay = _seconds_until_next_daily_run(time_hhmm, timezone_name)
        await asyncio.sleep(delay)
        try:
            await submission_service.prepare_daily_sheet_blocks_once()
        except Exception:
            logging.exception("Daily sheet maintenance failed")


def _seconds_until_next_daily_run(time_hhmm: str, timezone_name: str) -> float:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    hour, minute = (int(part) for part in time_hhmm.split(":", maxsplit=1))
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 1.0)


if __name__ == "__main__":
    asyncio.run(main())
