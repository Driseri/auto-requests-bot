from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from html import escape
from uuid import uuid4

from app.formatting import (
    TextFormattingSpan,
    deserialize_formatting_spans,
    render_html_with_formatting,
    serialize_formatting_spans,
)
from app.bulk import (
    BulkRegistrationResult,
    BulkReservationRegistrar,
    BulkReservationServiceProtocol,
)
from app.llm import LLM_ERROR_PREFIX, LlmClient
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BotResponse,
    BulkReservation,
    BulkReservationState,
    BulkTargetKind,
    ChangeType,
    ChipAfterTextAction,
    Direction,
    Draft,
    FieldName,
    KeyboardKind,
    LlmCheckResult,
    LlmCheckStatus,
    LlmContext,
    LinkedSubmissionResult,
    Priority,
    Step,
    SubmissionState,
    direction_display_label,
    direction_value_from_display_label,
)
from app.repository import DraftRepository
from app.submission import SubmissionServiceProtocol, dashboard_projection, dashboard_row

URGENT_EDITOR_NOTIFICATION_EVENT_TYPE = "urgent-editor-application-created"
LLM_CHECK_COMPLETED_EVENT_TYPE = "llm_check_completed"
LLM_CLARIFICATION_SUBMITTED_EVENT_TYPE = "llm_clarification_submitted"


FIELD_LABELS = {
    FieldName.DIRECTION: "Направление",
    FieldName.ANSWER_TYPE: "Тип ответа",
    FieldName.CHANGE_TYPE: "Тип изменения",
    FieldName.INTENT: "Интент",
    FieldName.SCRIPTWRITER: "Закрепленный сценарист",
    FieldName.REASON: "Кейс или сообщения клиента",
    FieldName.CHANGE_DESCRIPTION: "Суть изменений",
    FieldName.SOURCE_TEXT: "Исходный текст",
    FieldName.CHIP_TEXT_BEFORE: "Текст до чипса",
    FieldName.CHIP_TEXT: "Текст чипса",
    FieldName.CHIP_AFTER_TEXT_ACTION: "Действие с текстом после чипса",
    FieldName.CHIP_TEXT_AFTER: "Текст после чипса",
    FieldName.URGENCY: "Срочная",
    FieldName.PRIORITY: "Приоритет",
}

PENDING_SET_DEFAULT_INTENT = "set_default_intent"
PENDING_SET_DEFAULT_SCRIPTWRITER = "set_default_scriptwriter"
PENDING_SET_DEFAULT_DIRECTION = "set_default_direction"
PENDING_BULK_RESERVATION_COUNT = "bulk_reservation_count"
RETIRED_PENDING_CREATE_BULK_DIRECTION = "create_bulk_direction:"

STEP_PROMPTS = {
    Step.DIRECTION: "Выберите направление заявки.",
    Step.ANSWER_TYPE: "Выберите тип ответа.",
    Step.CHANGE_TYPE: "Выберите тип изменения.",
    Step.INTENT: "В каком интенте необходимо внести изменения?",
    Step.SCRIPTWRITER: "За каким сценаристом закреплен интент?",
    Step.REASON: (
        "Опишите кейс или сообщения клиента.\n\n"
        "Это контекст: что происходит в сценарии или с клиентом. Например: "
        "«Клиент нажимает на чипс после предложения услуги» или «Клиенты спрашивают, "
        "почему не приходит СМС».\n\n"
        "Не описывайте здесь, какой текст нужно изменить и как он будет звучать — "
        "это следующий шаг."
    ),
    Step.CHANGE_DESCRIPTION: (
        "В чем суть изменений?\n\n"
        "Опишите, что именно меняется в тексте и зачем: какую проблему или потребность "
        "это решает.\n\n"
        "Не подменяйте суть кейсом: «клиент нажимает на чипс» — это кейс; "
        "«после нажатия добавить пояснение, чтобы клиент понимал следующий шаг» — суть."
    ),
    Step.CHANGE_DESCRIPTION_CLARIFICATION: "Введите дополнение к сути изменений.",
    Step.CHANGE_DESCRIPTION_RECOMMENDATION: "Выберите, что сделать с рекомендацией.",
    Step.CHANGE_DESCRIPTION_REVISION: (
        "Пришлите только текст, который нужно дописать к сохранённой "
        "«Сути изменений». Бот добавит его с новой строки; уже сохранённый "
        "текст повторять не нужно."
    ),
    Step.CHANGE_DESCRIPTION_SKIP_REASON: "Почему вы решили пропустить рекомендацию?",
    Step.SOURCE_TEXT: "Пришлите предлагаемый текст.",
    Step.CHIP_TEXT_BEFORE: "Введите текст до чипса.",
    Step.CHIP_TEXT: "Введите текст чипса.",
    Step.CHIP_AFTER_TEXT_ACTION: "Что происходит с текстом ответа после чипса?",
    Step.CHIP_RESPONSE_CHANGE_DESCRIPTION: (
        "Кратко опишите суть изменения текста ответа после чипса. "
        "Связь с CHIPS-заявкой бот добавит автоматически."
    ),
    Step.CHIP_TEXT_AFTER: "Введите текст после чипса.",
    Step.URGENCY: "Заявка срочная?",
    Step.PRIORITY: "Выберите приоритет заявки.",
    Step.EDIT_INTENT: "Введите новый интент.",
    Step.EDIT_CHANGE_TYPE: "Выберите новый тип изменения.",
    Step.EDIT_SCRIPTWRITER: "Введите нового закрепленного сценариста.",
    Step.EDIT_REASON: (
        "Введите новый кейс или сообщения клиента. Это контекст ситуации, а не описание "
        "самого изменения текста."
    ),
    Step.EDIT_CHANGE_DESCRIPTION: (
        "Введите новую суть изменений: что именно меняется в тексте и зачем. "
        "Кейс описывает ситуацию, а суть — конкретное изменение и его цель."
    ),
    Step.EDIT_SOURCE_TEXT: "Введите новый предлагаемый текст.",
    Step.EDIT_CHIP_TEXT_BEFORE: "Введите новый текст до чипса.",
    Step.EDIT_CHIP_TEXT: "Введите новый текст чипса.",
    Step.EDIT_CHIP_AFTER_TEXT_ACTION: "Что происходит с текстом ответа после чипса?",
    Step.EDIT_CHIP_RESPONSE_CHANGE_DESCRIPTION: (
        "Введите новую суть изменений текста ответа после чипса. "
        "Связь с CHIPS-заявкой бот добавит автоматически."
    ),
    Step.EDIT_CHIP_TEXT_AFTER: "Введите новый текст после чипса.",
    Step.EDIT_URGENCY: "Выберите новый признак срочности.",
    Step.EDIT_PRIORITY: "Выберите новый приоритет заявки.",
}


class ApplicationFlow:
    """Оркестрирует пользовательские сценарии независимо от aiogram handlers."""

    def __init__(
        self,
        repository: DraftRepository,
        llm_client: LlmClient,
        submission_service: SubmissionServiceProtocol,
        bulk_reservation_service: BulkReservationServiceProtocol | None = None,
        bulk_reservation_registrar: BulkReservationRegistrar | None = None,
        show_llm_response_json: bool = False,
        bulk_max_rows: int = 50,
        bulk_creation_stale_seconds: int = 600,
        dashboard_enabled: bool = False,
        urgent_editor_notifications_enabled: bool = False,
        editor_urgent_chat_id: int | None = None,
    ) -> None:
        self.repository = repository
        self.llm_client = llm_client
        self.submission_service = submission_service
        self.bulk_reservation_service = bulk_reservation_service
        self.bulk_reservation_registrar = bulk_reservation_registrar
        self.show_llm_response_json = show_llm_response_json
        self.bulk_max_rows = bulk_max_rows
        self.bulk_creation_stale_seconds = bulk_creation_stale_seconds
        self.dashboard_enabled = dashboard_enabled
        self.urgent_editor_notifications_enabled = urgent_editor_notifications_enabled
        self.editor_urgent_chat_id = editor_urgent_chat_id
        self._submission_locks: dict[str, asyncio.Lock] = {}

    async def show_start(self) -> BotResponse:
        return BotResponse(
            text=(
                "Это бот для сбора заявок на изменение сценариев.\n\n"
                "Выберите режим работы ниже или отправьте /new."
            ),
            keyboard=KeyboardKind.CREATE_MODE,
        )

    async def start_new(
        self,
        telegram_user_id: int,
        *,
        force: bool = False,
        author_name: str | None = None,
    ) -> BotResponse:
        return BotResponse(
            text="Выберите, как хотите создать заявку.",
            keyboard=KeyboardKind.CREATE_MODE,
        )

    async def resume_from_notification(
        self,
        telegram_user_id: int,
        *,
        author_name: str | None = None,
    ) -> BotResponse:
        """Восстановить незавершенный workflow перед показом главного меню."""
        settings = await self.repository.get_user_settings(telegram_user_id)
        if (settings.pending_action or "").startswith(RETIRED_PENDING_CREATE_BULK_DIRECTION):
            # Retired legacy actions must not alter navigation for a new workflow.
            await self.repository.save_user_setting(telegram_user_id, "pending_action", None)
        reservation = await self.repository.get_active_bulk_reservation(telegram_user_id)
        if reservation is not None:
            return self._bulk_reservation_response(reservation)
        draft = await self.repository.get_by_user_id(telegram_user_id)
        if draft is not None and draft.is_active:
            await self.remember_author(telegram_user_id, author_name)
            return await self.continue_existing(telegram_user_id)
        return await self.start_new(
            telegram_user_id,
            author_name=author_name,
        )

    async def start_single(
        self,
        telegram_user_id: int,
        *,
        force: bool = False,
        author_name: str | None = None,
    ) -> BotResponse:
        """Начать одиночную заявку или предложить продолжить активный черновик."""
        existing = await self.repository.get_by_user_id(telegram_user_id)
        if existing is not None and existing.is_active and not force:
            await self.remember_author(telegram_user_id, author_name)
            return BotResponse(
                text=(
                    "У вас уже есть незавершенная заявка.\n\n"
                    "Можно продолжить ее, начать заново или отменить."
                ),
                keyboard=KeyboardKind.ACTIVE_DRAFT,
                draft=existing,
            )

        if existing is not None:
            process = await self._closed_llm_process(existing, "restarted")
            await self.repository.delete_with_llm_recommendation_state(
                telegram_user_id,
                process=process,
            )

        draft = await self.repository.get_or_create(telegram_user_id)
        await self.repository.save_answer(
            telegram_user_id,
            "application_type",
            ApplicationType.SINGLE.value,
        )
        await self.remember_author(telegram_user_id, author_name)
        draft = await self.repository.get_by_user_id(telegram_user_id) or draft
        return await self._prompt_response_for_user(
            telegram_user_id,
            draft,
            prefix="Начинаем новую заявку.",
        )

    async def continue_existing(self, telegram_user_id: int) -> BotResponse:
        draft = await self.repository.get_by_user_id(telegram_user_id)
        if draft is None or not draft.is_active:
            return await self.start_single(telegram_user_id, force=True)
        if not draft.direction and draft.current_step != Step.DIRECTION:
            draft = await self.repository.set_step(telegram_user_id, Step.DIRECTION)
        elif (
            _answer_type_requires_change_type(draft.answer_type)
            and ChangeType.normalize(draft.change_type) is None
            and draft.current_step not in {Step.ANSWER_TYPE, Step.EDIT_ANSWER_TYPE}
        ):
            draft = await self.repository.set_step(telegram_user_id, Step.CHANGE_TYPE)
        draft = await self._normalize_change_type_step(telegram_user_id, draft)
        return await self._prompt_response_for_user(
            telegram_user_id,
            draft,
            prefix="Продолжаем незавершенную заявку.",
        )

    async def remember_author(
        self,
        telegram_user_id: int,
        author_name: str | None,
    ) -> None:
        value = (author_name or "").strip()
        if not value:
            return
        draft = await self.repository.get_by_user_id(telegram_user_id)
        if draft is None or not draft.is_active or draft.author_name:
            return
        await self.repository.save_answer(telegram_user_id, "author_name", value)

    async def cancel(self, telegram_user_id: int) -> BotResponse:
        draft = await self.repository.get_by_user_id(telegram_user_id)
        if draft is not None:
            process = await self._closed_llm_process(draft, "cancelled")
            await self.repository.delete_with_llm_recommendation_state(
                telegram_user_id,
                process=process,
            )
        return BotResponse(
            text="Заявка отменена. Чтобы создать новую, отправьте /new.",
            keyboard=KeyboardKind.CREATE_MODE,
        )

    async def bulk_upload_stub(self, telegram_user_id: int) -> BotResponse:
        return await self.open_bulk_menu(telegram_user_id)

    async def confirm_bulk_batch_filled(
        self,
        telegram_user_id: int,
        batch_id: str,
    ) -> BotResponse:
        """Safely close a callback from a legacy bulk-batch message."""
        return BotResponse(
            text="Старый формат массовых заявок отключен. Создайте новую массовую заявку.",
            keyboard=KeyboardKind.BULK_MENU,
        )

    async def open_bulk_menu(self, telegram_user_id: int) -> BotResponse:
        if self.bulk_reservation_service is None:
            return BotResponse(text="Массовые заявки не настроены.", keyboard=KeyboardKind.CREATE_MODE)
        return BotResponse(
            text=(
                "Массовая заявка создает обычные строки сразу в нужном рабочем листе.\n\n"
                "Выберите направление, место, тип и количество строк. После заполнения бот "
                "зарегистрирует заполненные строки как обычные заявки."
            ),
            keyboard=KeyboardKind.BULK_MENU,
        )

    async def create_bulk_batch(self, telegram_user_id: int) -> BotResponse:
        if self.bulk_reservation_service is None:
            return BotResponse(text="Массовые заявки не настроены.", keyboard=KeyboardKind.CREATE_MODE)
        active = await self.repository.get_active_bulk_reservation(telegram_user_id)
        if active is not None:
            return self._bulk_reservation_response(active)
        reservation_id = f"RES-{uuid4().hex[:8].upper()}"
        await self.repository.create_bulk_reservation(
            reservation_id=reservation_id,
            idempotency_key=uuid4().hex,
            telegram_user_id=telegram_user_id,
        )
        return BotResponse(
            text="Выберите направление массовой заявки.",
            keyboard=KeyboardKind.BULK_DIRECTION,
            keyboard_payload=reservation_id,
        )

    async def select_bulk_direction(
        self,
        telegram_user_id: int,
        reservation_id: str,
        direction: Direction,
    ) -> BotResponse:
        if self.bulk_reservation_service is None:
            return BotResponse(text="Массовые заявки не настроены.", keyboard=KeyboardKind.CREATE_MODE)
        reservation = await self.repository.get_bulk_reservation(reservation_id)
        if reservation is None or reservation.telegram_user_id != telegram_user_id:
            return BotResponse(text="Массовый резерв не найден.", keyboard=KeyboardKind.BULK_MENU)
        reservation = await self.repository.update_bulk_reservation_step(
            reservation_id,
            state=BulkReservationState.AWAITING_TARGET,
            direction=direction.value,
        )
        return self._bulk_reservation_response(reservation)

    async def select_bulk_target(
        self,
        telegram_user_id: int,
        reservation_id: str,
        target_kind: BulkTargetKind,
    ) -> BotResponse:
        reservation = await self.repository.get_bulk_reservation(reservation_id)
        if reservation is None or reservation.telegram_user_id != telegram_user_id:
            return BotResponse(text="Массовый резерв не найден.", keyboard=KeyboardKind.BULK_MENU)
        reservation = await self.repository.update_bulk_reservation_step(
            reservation_id,
            state=BulkReservationState.AWAITING_CHANGE_TYPE,
            target_kind=target_kind.value,
        )
        return self._bulk_reservation_response(reservation)

    async def select_bulk_change_type(
        self,
        telegram_user_id: int,
        reservation_id: str,
        change_type: ChangeType,
    ) -> BotResponse:
        reservation = await self.repository.get_bulk_reservation(reservation_id)
        if reservation is None or reservation.telegram_user_id != telegram_user_id:
            return BotResponse(text="Массовый резерв не найден.", keyboard=KeyboardKind.BULK_MENU)
        if reservation.target_kind == BulkTargetKind.INTEGRATION.value and change_type == ChangeType.CHIPS:
            return BotResponse(
                text="CHIPS для интеграций пока не поддерживается. Выберите ADD или EDIT.",
                keyboard=KeyboardKind.BULK_CHANGE_TYPE,
                keyboard_payload=reservation_id,
            )
        reservation = await self.repository.update_bulk_reservation_step(
            reservation_id,
            state=BulkReservationState.AWAITING_COUNT,
            change_type=change_type.value,
        )
        await self.repository.save_user_setting(
            telegram_user_id,
            "pending_action",
            f"{PENDING_BULK_RESERVATION_COUNT}:{reservation_id}",
        )
        return BotResponse(
            text=f"Введите количество строк для массовой заявки. Максимум: {self.bulk_max_rows}.",
            keyboard=KeyboardKind.STEP,
        )

    async def save_bulk_reservation_count(
        self,
        telegram_user_id: int,
        text: str,
        reservation_id: str,
    ) -> BotResponse:
        reservation = await self.repository.get_bulk_reservation(reservation_id)
        if reservation is None or reservation.telegram_user_id != telegram_user_id:
            return BotResponse(text="Массовый резерв не найден.", keyboard=KeyboardKind.BULK_MENU)
        try:
            count = int(text.strip())
        except ValueError:
            return BotResponse(
                text=f"Введите число от 1 до {self.bulk_max_rows}.",
                keyboard=KeyboardKind.STEP,
            )
        if count <= 0 or count > self.bulk_max_rows:
            return BotResponse(
                text=f"Введите число от 1 до {self.bulk_max_rows}.",
                keyboard=KeyboardKind.STEP,
            )
        await self.repository.save_user_setting(telegram_user_id, "pending_action", None)
        reservation = await self.repository.update_bulk_reservation_step(
            reservation_id,
            state=BulkReservationState.AWAITING_CONFIRMATION,
            requested_count=count,
        )
        return self._bulk_reservation_response(reservation)

    async def confirm_bulk_reservation_creation(
        self,
        telegram_user_id: int,
        reservation_id: str,
    ) -> BotResponse:
        if self.bulk_reservation_service is None:
            return BotResponse(text="Массовая заявка не настроена.", keyboard=KeyboardKind.BULK_MENU)
        existing = await self.repository.get_bulk_reservation(reservation_id)
        if (
            existing is not None
            and existing.telegram_user_id == telegram_user_id
            and existing.state == BulkReservationState.CREATING.value
            and _bulk_reservation_is_recent(
                existing,
                stale_after_seconds=self.bulk_creation_stale_seconds,
            )
        ):
            return self._bulk_reservation_response(existing)
        reservation = await self.repository.claim_bulk_reservation_creation(
            reservation_id,
            stale_after_seconds=self.bulk_creation_stale_seconds,
        )
        if reservation is None or reservation.telegram_user_id != telegram_user_id:
            return BotResponse(text="Массовый резерв не найден.", keyboard=KeyboardKind.BULK_MENU)
        if reservation.state == BulkReservationState.CREATED.value:
            return self._bulk_reservation_response(reservation)
        if reservation.state != BulkReservationState.CREATING.value:
            return self._bulk_reservation_response(reservation)
        result = await self.bulk_reservation_service.create_reservation_with_lock(reservation)
        if not result.success or result.reservation is None:
            if result.retry_allowed:
                return BotResponse(
                    text=result.message,
                    keyboard=KeyboardKind.BULK_COUNT_CONFIRM,
                    keyboard_payload=reservation_id,
                )
            await self.repository.fail_bulk_reservation(reservation_id, error=result.message)
            return BotResponse(text=result.message, keyboard=KeyboardKind.BULK_MENU)
        return self._bulk_reservation_response(result.reservation)

    async def confirm_bulk_reservation_filled(
        self,
        telegram_user_id: int,
        reservation_id: str,
    ) -> BotResponse:
        if self.bulk_reservation_registrar is None:
            return BotResponse(text="Регистрация массового резерва не настроена.", keyboard=KeyboardKind.BULK_MENU)
        result = await self.bulk_reservation_registrar.register_reservation(
            reservation_id,
            telegram_user_id,
        )
        if not result.success:
            link = (
                f'\n\n<a href="{escape(result.insert_url, quote=True)}">Открыть исходный диапазон</a>'
                if result.insert_url
                else ""
            )
            return BotResponse(
                text=f"{escape(result.message)}{link}",
                keyboard=KeyboardKind.BULK_RESERVATION_CREATED,
                keyboard_payload=reservation_id,
                parse_mode="HTML",
            )
        return BotResponse(
            text=_bulk_registration_confirmation(result),
            keyboard=KeyboardKind.BULK_RESERVATION_COMPLETED,
            parse_mode="HTML",
        )

    async def cancel_bulk_reservation(
        self,
        telegram_user_id: int,
        reservation_id: str,
    ) -> BotResponse:
        reservation = await self.repository.get_bulk_reservation(reservation_id)
        if reservation is not None and reservation.telegram_user_id == telegram_user_id:
            await self.repository.cancel_bulk_reservation(reservation_id)
        await self.repository.save_user_setting(telegram_user_id, "pending_action", None)
        return BotResponse(
            text=(
                "Массовая заявка отменена. Если строки уже были созданы в таблице, "
                "они останутся пустым резервом и не будут зарегистрированы ботом."
            ),
            keyboard=KeyboardKind.BULK_MENU,
        )

    def _bulk_reservation_response(self, reservation: BulkReservation | None) -> BotResponse:
        if reservation is None:
            return BotResponse(text="Массовый резерв не найден.", keyboard=KeyboardKind.BULK_MENU)
        state = reservation.state
        if state == BulkReservationState.AWAITING_DIRECTION.value:
            return BotResponse(
                text="Выберите направление массовой заявки.",
                keyboard=KeyboardKind.BULK_DIRECTION,
                keyboard_payload=reservation.reservation_id,
            )
        if state == BulkReservationState.AWAITING_TARGET.value:
            return BotResponse(
                text="Куда заносим массовую заявку?",
                keyboard=KeyboardKind.BULK_TARGET,
                keyboard_payload=reservation.reservation_id,
            )
        if state == BulkReservationState.AWAITING_CHANGE_TYPE.value:
            return BotResponse(
                text="Выберите тип изменения для строк.",
                keyboard=KeyboardKind.BULK_CHANGE_TYPE,
                keyboard_payload=_bulk_change_type_keyboard_payload(reservation),
            )
        if state == BulkReservationState.AWAITING_COUNT.value:
            return BotResponse(
                text=f"Введите количество строк для массовой заявки. Максимум: {self.bulk_max_rows}.",
                keyboard=KeyboardKind.STEP,
            )
        if state == BulkReservationState.AWAITING_CONFIRMATION.value:
            return BotResponse(
                text=(
                    "Проверьте параметры массового ввода:\n\n"
                    f"Направление: {direction_display_label(reservation.direction)}\n"
                    f"Куда заносим: {_bulk_target_label(reservation.target_kind)}\n"
                    f"Тип: {reservation.change_type}\n"
                    f"Количество строк: {reservation.requested_count}\n\n"
                    "Статус у строк останется пустым до регистрации."
                ),
                keyboard=KeyboardKind.BULK_COUNT_CONFIRM,
                keyboard_payload=reservation.reservation_id,
            )
        if state == BulkReservationState.CREATING.value:
            return BotResponse(
                text=(
                    "Строки для массовой заявки уже создаются.\n\n"
                    "Подождите несколько секунд. Если сообщение не обновится, нажмите кнопку создания ещё раз."
                ),
                keyboard=KeyboardKind.BULK_COUNT_CONFIRM,
                keyboard_payload=reservation.reservation_id,
            )
        if state == BulkReservationState.CREATED.value:
            link = (
                f'\n\n<a href="{escape(reservation.insert_url or "", quote=True)}">Открыть диапазон</a>'
                if reservation.insert_url
                else ""
            )
            return BotResponse(
                text=(
                    "Строки для массовой заявки созданы.\n\n"
                    "Заполните нужные строки в таблице. После заполнения вернитесь сюда "
                    "и нажмите «Заявка заполнена»."
                    f"{link}"
                ),
                keyboard=KeyboardKind.BULK_RESERVATION_CREATED,
                keyboard_payload=reservation.reservation_id,
                parse_mode="HTML",
            )
        if state == BulkReservationState.REGISTERED.value:
            return BotResponse(
                text=f"Массовый ввод уже зарегистрирован. Заявок: {reservation.registered_count}.",
                keyboard=KeyboardKind.BULK_RESERVATION_COMPLETED,
            )
        return BotResponse(
            text=reservation.last_error or "Массовый резерв находится в ошибочном состоянии.",
            keyboard=KeyboardKind.BULK_MENU,
        )

    async def open_defaults_menu(self, telegram_user_id: int) -> BotResponse:
        settings = await self.repository.get_user_settings(telegram_user_id)
        await self.repository.save_user_setting(telegram_user_id, "pending_action", None)
        return BotResponse(
            text=(
                "Дефолты пользователя.\n\n"
                f"Направление: {direction_display_label(settings.default_direction) or '-'}\n"
                f"Интент: {settings.default_intent or '-'}\n"
                f"Сценарист: {settings.default_scriptwriter or '-'}"
            ),
            keyboard=KeyboardKind.DEFAULTS_MENU,
        )

    async def start_set_default_direction(self, telegram_user_id: int) -> BotResponse:
        await self.repository.save_user_setting(
            telegram_user_id,
            "pending_action",
            PENDING_SET_DEFAULT_DIRECTION,
        )
        return BotResponse(
            text=(
                "Введите направление по умолчанию: ФЛ-chatbot, SME-chatbot, "
                "АИ-chatbot, VoiceBot-chatbot или Collection-chatbot."
            ),
            keyboard=KeyboardKind.DEFAULTS_BACK,
        )

    async def start_set_default_intent(self, telegram_user_id: int) -> BotResponse:
        await self.repository.save_user_setting(
            telegram_user_id,
            "pending_action",
            PENDING_SET_DEFAULT_INTENT,
        )
        return BotResponse(
            text="Введите интент, который нужно использовать по умолчанию.",
            keyboard=KeyboardKind.DEFAULTS_BACK,
        )

    async def start_set_default_scriptwriter(self, telegram_user_id: int) -> BotResponse:
        await self.repository.save_user_setting(
            telegram_user_id,
            "pending_action",
            PENDING_SET_DEFAULT_SCRIPTWRITER,
        )
        return BotResponse(
            text="Введите сценариста, которого нужно использовать по умолчанию.",
            keyboard=KeyboardKind.DEFAULTS_BACK,
        )

    async def clear_default_intent(self, telegram_user_id: int) -> BotResponse:
        await self.repository.clear_user_setting(telegram_user_id, "default_intent")
        return await self.open_defaults_menu(telegram_user_id)

    async def clear_default_direction(self, telegram_user_id: int) -> BotResponse:
        await self.repository.clear_user_setting(telegram_user_id, "default_direction")
        return await self.open_defaults_menu(telegram_user_id)

    async def clear_default_scriptwriter(self, telegram_user_id: int) -> BotResponse:
        await self.repository.clear_user_setting(telegram_user_id, "default_scriptwriter")
        return await self.open_defaults_menu(telegram_user_id)

    async def use_default_direction(self, telegram_user_id: int) -> BotResponse:
        settings = await self.repository.get_user_settings(telegram_user_id)
        if not settings.default_direction:
            draft = await self._get_active_or_start(telegram_user_id)
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Направление по умолчанию пока не задано.",
            )
        return await self.select_direction(
            telegram_user_id,
            Direction(settings.default_direction),
        )

    async def use_default_intent(self, telegram_user_id: int) -> BotResponse:
        settings = await self.repository.get_user_settings(telegram_user_id)
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step != Step.INTENT:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Сейчас подстановка интента не ожидается.",
            )
        if not settings.default_intent:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Интент по умолчанию пока не задан.",
            )

        await self.repository.save_answer(
            telegram_user_id,
            FieldName.INTENT.value,
            settings.default_intent,
        )
        draft = await self.repository.set_step(
            telegram_user_id,
            Step.REASON if _is_chips(draft) else Step.SCRIPTWRITER,
        )
        return await self._prompt_response_for_user(
            telegram_user_id,
            draft,
            prefix="Интент по умолчанию подставлен.",
        )

    async def use_default_scriptwriter(self, telegram_user_id: int) -> BotResponse:
        settings = await self.repository.get_user_settings(telegram_user_id)
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step != Step.SCRIPTWRITER:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Сейчас подстановка сценариста не ожидается.",
            )
        if not settings.default_scriptwriter:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Сценарист по умолчанию пока не задан.",
            )

        await self.repository.save_answer(
            telegram_user_id,
            FieldName.SCRIPTWRITER.value,
            settings.default_scriptwriter,
        )
        draft = await self.repository.set_step(
            telegram_user_id,
            Step.INTENT if _is_chips(draft) else Step.REASON,
        )
        return await self._prompt_response_for_user(
            telegram_user_id,
            draft,
            prefix="Сценарист по умолчанию подставлен.",
        )

    async def handle_text(
        self,
        telegram_user_id: int,
        text: str | None,
        formatting_spans: list[TextFormattingSpan] | None = None,
    ) -> BotResponse:
        """Обработать текст согласно текущему шагу черновика или настройки."""
        pending_response = await self._handle_pending_settings_text(telegram_user_id, text)
        if pending_response is not None:
            return pending_response

        draft = await self._get_active_or_start(telegram_user_id)
        draft = await self._normalize_change_type_step(telegram_user_id, draft)
        value = (text or "").strip()
        if not value:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Поле не должно быть пустым.",
            )

        match draft.current_step:
            case Step.DIRECTION | Step.EDIT_DIRECTION:
                return await self._prompt_response_for_user(
                    telegram_user_id,
                    draft,
                    prefix="Направление нужно выбрать кнопкой.",
                )
            case Step.ANSWER_TYPE | Step.EDIT_ANSWER_TYPE:
                return await self._prompt_response_for_user(
                    telegram_user_id,
                    draft,
                    prefix="Тип ответа нужно выбрать кнопкой.",
                )
            case Step.CHANGE_TYPE | Step.EDIT_CHANGE_TYPE:
                return await self._prompt_response_for_user(
                    telegram_user_id,
                    draft,
                    prefix="Тип изменения нужно выбрать кнопкой.",
                )
            case Step.INTENT:
                await self.repository.save_answer(telegram_user_id, FieldName.INTENT.value, value)
                draft = await self.repository.set_step(
                    telegram_user_id,
                    Step.REASON if _is_chips(draft) else Step.SCRIPTWRITER,
                )
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.SCRIPTWRITER:
                await self.repository.save_answer(
                    telegram_user_id,
                    FieldName.SCRIPTWRITER.value,
                    value,
                )
                draft = await self.repository.set_step(
                    telegram_user_id,
                    Step.INTENT if _is_chips(draft) else Step.REASON,
                )
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.REASON:
                await self.repository.save_answer(telegram_user_id, FieldName.REASON.value, value)
                draft = await self.repository.set_step(
                    telegram_user_id,
                    Step.CHIP_TEXT_BEFORE if _is_chips(draft) else Step.CHANGE_DESCRIPTION,
                )
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.CHIP_TEXT_BEFORE:
                await self._save_formatted_text(
                    telegram_user_id,
                    FieldName.CHIP_TEXT_BEFORE,
                    value,
                    formatting_spans,
                )
                draft = await self.repository.set_step(telegram_user_id, Step.CHIP_TEXT)
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.CHIP_TEXT:
                await self._save_formatted_text(
                    telegram_user_id,
                    FieldName.CHIP_TEXT,
                    value,
                    formatting_spans,
                )
                draft = await self.repository.set_step(
                    telegram_user_id,
                    Step.CHIP_AFTER_TEXT_ACTION,
                )
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.CHIP_TEXT_AFTER:
                await self._save_formatted_text(
                    telegram_user_id,
                    FieldName.CHIP_TEXT_AFTER,
                    value,
                    formatting_spans,
                )
                draft = await self.repository.set_step(telegram_user_id, Step.REVIEW)
                return self._review_response(draft)
            case Step.CHIP_RESPONSE_CHANGE_DESCRIPTION:
                await self._save_chip_response_change_description(telegram_user_id, value)
                draft = await self.repository.set_step(telegram_user_id, Step.CHIP_TEXT_AFTER)
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.CHANGE_DESCRIPTION:
                return await self._process_change_description(telegram_user_id, value)
            case Step.CHANGE_DESCRIPTION_CLARIFICATION:
                return await self._process_change_revision(telegram_user_id, value)
            case Step.CHANGE_DESCRIPTION_REVISION:
                return await self._process_change_revision(telegram_user_id, value)
            case Step.CHANGE_DESCRIPTION_RECOMMENDATION:
                return await self._prompt_response_for_user(
                    telegram_user_id,
                    draft,
                    prefix="Выберите действие кнопкой.",
                )
            case Step.CHANGE_DESCRIPTION_SKIP_REASON:
                return await self._prompt_response_for_user(
                    telegram_user_id,
                    draft,
                    prefix="Причину пропуска нужно выбрать кнопкой.",
                )
            case Step.SOURCE_TEXT:
                await self._save_source_text(telegram_user_id, value, formatting_spans)
                next_step = (
                    Step.REVIEW
                    if _direction_requires_answer_type(draft.direction)
                    else Step.URGENCY
                )
                draft = await self.repository.set_step(telegram_user_id, next_step)
                if next_step == Step.REVIEW:
                    return self._review_response(draft)
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.URGENCY | Step.EDIT_URGENCY:
                return await self._prompt_response_for_user(
                    telegram_user_id,
                    draft,
                    prefix="Признак срочности нужно выбрать кнопкой.",
                )
            case Step.PRIORITY | Step.EDIT_PRIORITY:
                return await self._prompt_response_for_user(
                    telegram_user_id,
                    draft,
                    prefix="Приоритет нужно выбрать кнопкой.",
                )
            case Step.REVIEW:
                return BotResponse(
                    text="Заявка заполнена. Выберите действие кнопками под итогом.",
                    keyboard=KeyboardKind.REVIEW,
                    draft=draft,
                )
            case Step.EDIT_INTENT:
                await self.repository.save_answer(telegram_user_id, FieldName.INTENT.value, value)
                return await self._return_to_review(telegram_user_id)
            case Step.EDIT_SCRIPTWRITER:
                await self.repository.save_answer(
                    telegram_user_id,
                    FieldName.SCRIPTWRITER.value,
                    value,
                )
                return await self._return_to_review(telegram_user_id)
            case Step.EDIT_REASON:
                await self.repository.save_answer(telegram_user_id, FieldName.REASON.value, value)
                return await self._return_to_review(telegram_user_id)
            case Step.EDIT_CHANGE_DESCRIPTION:
                await self.repository.save_llm_result(
                    telegram_user_id,
                    raw_change_description=value,
                    formatted_change_description=value,
                    llm_check_status=draft.llm_check_status,
                    llm_score=None,
                    clarification_count=draft.clarification_count,
                )
                return await self._return_to_review(
                    telegram_user_id,
                    prefix="Суть изменений обновлена без повторной проверки.",
                )
            case Step.EDIT_CHIP_RESPONSE_CHANGE_DESCRIPTION:
                await self._save_chip_response_change_description(telegram_user_id, value)
                return await self._return_to_review(telegram_user_id)
            case Step.EDIT_SOURCE_TEXT:
                await self._save_source_text(telegram_user_id, value, formatting_spans)
                return await self._return_to_review(telegram_user_id)
            case Step.EDIT_CHIP_TEXT_BEFORE | Step.EDIT_CHIP_TEXT | Step.EDIT_CHIP_TEXT_AFTER:
                field = {
                    Step.EDIT_CHIP_TEXT_BEFORE: FieldName.CHIP_TEXT_BEFORE,
                    Step.EDIT_CHIP_TEXT: FieldName.CHIP_TEXT,
                    Step.EDIT_CHIP_TEXT_AFTER: FieldName.CHIP_TEXT_AFTER,
                }[draft.current_step]
                await self._save_formatted_text(
                    telegram_user_id,
                    field,
                    value,
                    formatting_spans,
                )
                return await self._return_to_review(telegram_user_id)
            case Step.EDIT_DIRECTION:
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.EDIT_ANSWER_TYPE:
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.EDIT_URGENCY:
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.COMPLETED:
                return await self.start_new(telegram_user_id, force=True)

    async def should_show_llm_processing(self, telegram_user_id: int) -> bool:
        draft = await self.repository.get_by_user_id(telegram_user_id)
        return draft is not None and not _is_chips(draft) and draft.current_step in {
            Step.CHANGE_DESCRIPTION,
            Step.CHANGE_DESCRIPTION_CLARIFICATION,
            Step.CHANGE_DESCRIPTION_REVISION,
        }

    async def accept_llm_recommendation(self, telegram_user_id: int) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step != Step.CHANGE_DESCRIPTION_RECOMMENDATION:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Эта рекомендация уже обработана.",
            )
        process = await self._load_llm_process(draft)
        if process is None or len(process.get("iterations", [])) != 1:
            return await self._advance_after_llm(
                telegram_user_id,
                draft,
                prefix="Не удалось восстановить рекомендацию. Продолжаем заполнение заявки.",
            )
        process["state"] = "awaiting_revision"
        process.setdefault("actions", []).append(
            {
                "action": "add",
                "selected_at": _utc_now(),
            }
        )
        draft = await self.repository.save_llm_recommendation_state(
            telegram_user_id,
            process=process,
            draft_values={"current_step": Step.CHANGE_DESCRIPTION_REVISION.value},
        )
        current_text = draft.raw_change_description or ""
        return await self._prompt_response_for_user(
            telegram_user_id,
            draft,
            prefix=f"Сейчас сохранено:\n{escape(current_text)}",
        )

    async def skip_llm_recommendation(self, telegram_user_id: int) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step != Step.CHANGE_DESCRIPTION_RECOMMENDATION:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Эта рекомендация уже обработана.",
            )
        process = await self._load_llm_process(draft)
        if process is None:
            return await self._advance_after_llm(
                telegram_user_id,
                draft,
                prefix="Не удалось восстановить рекомендацию. Продолжаем заполнение заявки.",
            )
        process["state"] = "awaiting_skip_reason"
        draft = await self.repository.save_llm_recommendation_state(
            telegram_user_id,
            process=process,
            draft_values={"current_step": Step.CHANGE_DESCRIPTION_SKIP_REASON.value},
        )
        return await self._prompt_response_for_user(telegram_user_id, draft)

    async def select_llm_skip_reason(
        self,
        telegram_user_id: int,
        reason: str,
    ) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step != Step.CHANGE_DESCRIPTION_SKIP_REASON:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Причина пропуска уже сохранена.",
            )
        if reason not in {"optional", "incorrect", "unclear"}:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Неизвестная причина пропуска.",
            )
        process = await self._load_llm_process(draft)
        if process is None:
            return await self._advance_after_llm(
                telegram_user_id,
                draft,
                prefix="Не удалось восстановить рекомендацию. Продолжаем заполнение заявки.",
            )
        now = _utc_now()
        process["state"] = "skipped"
        process["completed_at"] = now
        process["final_outcome"] = f"skipped_{reason}"
        process.setdefault("actions", []).append(
            {
                "action": "skip",
                "reason": reason,
                "selected_at": now,
            }
        )
        next_step = Step.SOURCE_TEXT
        draft = await self.repository.save_llm_recommendation_state(
            telegram_user_id,
            process=process,
            draft_values={
                "current_step": next_step.value,
                "llm_check_status": LlmCheckStatus.NEEDS_ATTENTION.value,
            },
        )
        return await self._prompt_response_for_user(
            telegram_user_id,
            draft,
            prefix="Рекомендация пропущена.",
        )

    async def select_direction(self, telegram_user_id: int, direction: Direction) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step not in {Step.DIRECTION, Step.EDIT_DIRECTION}:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Сейчас выбор направления не ожидается.",
            )

        await self.repository.save_answer(telegram_user_id, FieldName.DIRECTION.value, direction.value)
        next_step = (
            Step.ANSWER_TYPE
            if direction in {Direction.FL, Direction.SME, Direction.AI}
            else Step.REVIEW
            if draft.current_step == Step.EDIT_DIRECTION
            else Step.INTENT
        )
        if direction in {Direction.VOICEBOT, Direction.COLLECTION}:
            await self.repository.save_answer(telegram_user_id, FieldName.ANSWER_TYPE.value, "")
            await self.repository.save_answer(telegram_user_id, FieldName.CHANGE_TYPE.value, "")
        draft = await self.repository.set_step(telegram_user_id, next_step)
        if next_step == Step.REVIEW:
            return self._review_response(draft, prefix="Направление обновлено.")
        return await self._prompt_response_for_user(telegram_user_id, draft)

    async def select_answer_type(
        self,
        telegram_user_id: int,
        answer_type: AnswerType,
    ) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step not in {Step.ANSWER_TYPE, Step.EDIT_ANSWER_TYPE}:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Сейчас выбор типа ответа не ожидается.",
            )
        if draft.direction not in {
            Direction.FL.value,
            Direction.SME.value,
            Direction.AI.value,
        }:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Для этого направления тип ответа не выбирается.",
            )

        await self.repository.save_answer(
            telegram_user_id,
            FieldName.ANSWER_TYPE.value,
            answer_type.value,
        )
        if not _answer_type_requires_change_type(answer_type.value):
            await self.repository.save_answer(
                telegram_user_id,
                FieldName.CHANGE_TYPE.value,
                "",
            )
        await self.repository.save_answer(
            telegram_user_id,
            FieldName.URGENCY.value,
            answer_type == AnswerType.URGENT,
        )
        if _answer_type_requires_change_type(answer_type.value):
            next_step = (
                Step.EDIT_CHANGE_TYPE
                if draft.current_step == Step.EDIT_ANSWER_TYPE
                else Step.CHANGE_TYPE
            )
        else:
            next_step = Step.REVIEW if draft.current_step == Step.EDIT_ANSWER_TYPE else Step.INTENT
        draft = await self.repository.set_step(telegram_user_id, next_step)
        if next_step == Step.REVIEW:
            return self._review_response(draft, prefix="Тип ответа обновлен.")
        return await self._prompt_response_for_user(telegram_user_id, draft)

    async def select_change_type(
        self,
        telegram_user_id: int,
        change_type: ChangeType,
    ) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step not in {Step.CHANGE_TYPE, Step.EDIT_CHANGE_TYPE}:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Сейчас выбор типа изменения не ожидается.",
            )
        if not _answer_type_requires_change_type(draft.answer_type):
            await self.repository.save_answer(
                telegram_user_id,
                FieldName.CHANGE_TYPE.value,
                "",
            )
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Тип изменения выбирается только для раскатки или срочной заявки.",
            )

        await self.repository.save_answer(
            telegram_user_id,
            FieldName.CHANGE_TYPE.value,
            change_type.value,
        )
        edit_mode = draft.current_step == Step.EDIT_CHANGE_TYPE
        if draft.change_type != change_type.value:
            await self._reset_change_type_specific_fields(telegram_user_id, change_type)
        updated = await self.repository.get_by_user_id(telegram_user_id)
        if updated is None:
            raise LookupError(f"Draft not found for user {telegram_user_id}")
        if edit_mode:
            next_step = _first_missing_step(updated) or Step.REVIEW
        else:
            next_step = Step.SCRIPTWRITER if change_type == ChangeType.CHIPS else Step.INTENT
        draft = await self.repository.set_step(telegram_user_id, next_step)
        if next_step == Step.REVIEW:
            return self._review_response(draft, prefix="Тип изменения обновлен.")
        return await self._prompt_response_for_user(telegram_user_id, draft)

    async def select_chip_after_text_action(
        self,
        telegram_user_id: int,
        action: ChipAfterTextAction,
    ) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step not in {
            Step.CHIP_AFTER_TEXT_ACTION,
            Step.EDIT_CHIP_AFTER_TEXT_ACTION,
        }:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Сейчас выбор действия с текстом после чипса не ожидается.",
            )
        if not _is_chips(draft):
            return self._review_response(
                draft,
                prefix="Этот выбор доступен только для CHIPS-заявки.",
            )
        edit_mode = draft.current_step == Step.EDIT_CHIP_AFTER_TEXT_ACTION
        await self.repository.save_answer(
            telegram_user_id,
            FieldName.CHIP_AFTER_TEXT_ACTION.value,
            action.value,
        )
        if action == ChipAfterTextAction.UNCHANGED:
            await self.repository.save_llm_result(
                telegram_user_id,
                formatted_change_description=None,
                raw_change_description=None,
                llm_check_status=LlmCheckStatus.SKIPPED.value,
                llm_score=None,
                clarification_count=0,
            )
        next_step = (
            Step.REVIEW
            if edit_mode and action == ChipAfterTextAction.UNCHANGED
            else Step.EDIT_CHIP_RESPONSE_CHANGE_DESCRIPTION
            if edit_mode
            else Step.CHIP_RESPONSE_CHANGE_DESCRIPTION
            if action != ChipAfterTextAction.UNCHANGED
            else Step.CHIP_TEXT_AFTER
        )
        draft = await self.repository.set_step(telegram_user_id, next_step)
        if next_step == Step.REVIEW:
            return self._review_response(draft, prefix="Действие после чипса обновлено.")
        return await self._prompt_response_for_user(telegram_user_id, draft)

    async def _save_chip_response_change_description(
        self,
        telegram_user_id: int,
        value: str,
    ) -> None:
        """Сохранить суть автоматически создаваемой ADD/EDIT без LLM-проверки."""
        await self.repository.save_llm_result(
            telegram_user_id,
            formatted_change_description=value,
            raw_change_description=value,
            llm_check_status=LlmCheckStatus.SKIPPED.value,
            llm_score=None,
            clarification_count=0,
        )

    async def _reset_change_type_specific_fields(
        self,
        telegram_user_id: int,
        change_type: ChangeType,
    ) -> None:
        if change_type == ChangeType.CHIPS:
            await self.repository.save_llm_result(
                telegram_user_id,
                formatted_change_description=None,
                raw_change_description=None,
                llm_check_status=LlmCheckStatus.SKIPPED.value,
                llm_score=None,
                clarification_count=0,
            )
            await self.repository.save_answer(telegram_user_id, FieldName.SOURCE_TEXT.value, "")
            await self.repository.save_answer(
                telegram_user_id,
                "source_text_formatting_json",
                "",
            )
            for field in (
                FieldName.CHIP_TEXT_BEFORE,
                FieldName.CHIP_TEXT,
                FieldName.CHIP_TEXT_AFTER,
            ):
                await self.repository.save_answer(telegram_user_id, field.value, "")
                await self.repository.save_answer(
                    telegram_user_id,
                    f"{field.value}_formatting_json",
                    "",
                )
            await self.repository.save_answer(
                telegram_user_id,
                FieldName.CHIP_AFTER_TEXT_ACTION.value,
                "",
            )
            return

        for field in (
            FieldName.CHIP_TEXT_BEFORE,
            FieldName.CHIP_TEXT,
            FieldName.CHIP_TEXT_AFTER,
        ):
            await self.repository.save_answer(telegram_user_id, field.value, "")
            await self.repository.save_answer(
                telegram_user_id,
                f"{field.value}_formatting_json",
                "",
            )
        await self.repository.save_answer(
            telegram_user_id,
            FieldName.CHIP_AFTER_TEXT_ACTION.value,
            "",
        )
        await self.repository.save_llm_result(
            telegram_user_id,
            formatted_change_description=None,
            raw_change_description=None,
            llm_check_status=LlmCheckStatus.NOT_CHECKED.value,
            llm_score=None,
            clarification_count=0,
        )
        await self.repository.save_answer(telegram_user_id, FieldName.SOURCE_TEXT.value, "")
        await self.repository.save_answer(
            telegram_user_id,
            "source_text_formatting_json",
            "",
        )

    async def select_urgency(self, telegram_user_id: int, is_urgent: bool) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step not in {Step.URGENCY, Step.EDIT_URGENCY}:
            if draft.current_step == Step.REVIEW:
                return self._review_response(
                    draft,
                    prefix="Срочность для выбранного направления уже определяется типом ответа.",
                )
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Сейчас выбор срочности не ожидается.",
            )

        await self.repository.save_answer(telegram_user_id, FieldName.URGENCY.value, is_urgent)
        draft = await self.repository.set_step(telegram_user_id, Step.REVIEW)
        return self._review_response(draft)

    async def select_priority(self, telegram_user_id: int, priority: Priority) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step not in {Step.PRIORITY, Step.EDIT_PRIORITY}:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Сейчас выбор приоритета не ожидается.",
            )

        await self.repository.save_answer(telegram_user_id, FieldName.PRIORITY.value, priority.value)
        draft = await self.repository.set_step(telegram_user_id, Step.REVIEW)
        return self._review_response(draft)

    async def back(self, telegram_user_id: int) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step in {
            Step.CHANGE_DESCRIPTION_REVISION,
            Step.CHANGE_DESCRIPTION_SKIP_REASON,
        }:
            process = await self._load_llm_process(draft)
            if process is not None:
                process["state"] = "awaiting_action"
                draft = await self.repository.save_llm_recommendation_state(
                    telegram_user_id,
                    process=process,
                    draft_values={
                        "current_step": Step.CHANGE_DESCRIPTION_RECOMMENDATION.value,
                    },
                )
                return self._recommendation_response(draft, process)
        if _is_chips(draft):
            previous_step = (
                Step.CHIP_RESPONSE_CHANGE_DESCRIPTION
                if draft.current_step == Step.CHIP_TEXT_AFTER
                and _chip_after_text_action(draft)
                in {ChipAfterTextAction.ADD, ChipAfterTextAction.EDIT}
                else {
                Step.SCRIPTWRITER: Step.CHANGE_TYPE,
                Step.INTENT: Step.SCRIPTWRITER,
                Step.REASON: Step.INTENT,
                Step.CHIP_TEXT_BEFORE: Step.REASON,
                Step.CHIP_TEXT: Step.CHIP_TEXT_BEFORE,
                Step.CHIP_AFTER_TEXT_ACTION: Step.CHIP_TEXT,
                Step.CHIP_TEXT_AFTER: Step.CHIP_AFTER_TEXT_ACTION,
                Step.CHIP_RESPONSE_CHANGE_DESCRIPTION: Step.CHIP_AFTER_TEXT_ACTION,
                Step.REVIEW: Step.CHIP_TEXT_AFTER,
                Step.EDIT_DIRECTION: Step.REVIEW,
                Step.EDIT_ANSWER_TYPE: Step.REVIEW,
                Step.EDIT_CHANGE_TYPE: Step.REVIEW,
                Step.EDIT_INTENT: Step.REVIEW,
                Step.EDIT_SCRIPTWRITER: Step.REVIEW,
                Step.EDIT_REASON: Step.REVIEW,
                Step.EDIT_CHIP_TEXT_BEFORE: Step.REVIEW,
                Step.EDIT_CHIP_TEXT: Step.REVIEW,
                Step.EDIT_CHIP_AFTER_TEXT_ACTION: Step.REVIEW,
                Step.EDIT_CHIP_RESPONSE_CHANGE_DESCRIPTION: Step.REVIEW,
                Step.EDIT_CHIP_TEXT_AFTER: Step.REVIEW,
                }.get(draft.current_step)
            )
        elif (
            draft.current_step == Step.INTENT
            and _answer_type_requires_change_type(draft.answer_type)
        ):
            previous_step = Step.CHANGE_TYPE
        elif draft.current_step == Step.INTENT and not _direction_requires_answer_type(draft.direction):
            previous_step = Step.DIRECTION
        else:
            previous_step = {
                Step.ANSWER_TYPE: Step.DIRECTION,
                Step.INTENT: Step.ANSWER_TYPE,
                Step.CHANGE_TYPE: Step.ANSWER_TYPE,
                Step.SCRIPTWRITER: Step.INTENT,
                Step.REASON: Step.SCRIPTWRITER,
                Step.CHANGE_DESCRIPTION: Step.REASON,
                Step.CHANGE_DESCRIPTION_CLARIFICATION: Step.CHANGE_DESCRIPTION,
                Step.CHANGE_DESCRIPTION_RECOMMENDATION: Step.CHANGE_DESCRIPTION,
                Step.CHANGE_DESCRIPTION_REVISION: Step.CHANGE_DESCRIPTION_RECOMMENDATION,
                Step.CHANGE_DESCRIPTION_SKIP_REASON: Step.CHANGE_DESCRIPTION_RECOMMENDATION,
                Step.SOURCE_TEXT: Step.CHANGE_DESCRIPTION,
                Step.URGENCY: Step.SOURCE_TEXT,
                Step.PRIORITY: Step.SOURCE_TEXT,
                Step.REVIEW: (
                    Step.SOURCE_TEXT
                    if _direction_requires_answer_type(draft.direction)
                    else Step.URGENCY
                ),
                Step.EDIT_DIRECTION: Step.REVIEW,
                Step.EDIT_ANSWER_TYPE: Step.REVIEW,
                Step.EDIT_CHANGE_TYPE: Step.REVIEW,
                Step.EDIT_INTENT: Step.REVIEW,
                Step.EDIT_SCRIPTWRITER: Step.REVIEW,
                Step.EDIT_REASON: Step.REVIEW,
                Step.EDIT_CHANGE_DESCRIPTION: Step.REVIEW,
                Step.EDIT_SOURCE_TEXT: Step.REVIEW,
                Step.EDIT_URGENCY: Step.REVIEW,
                Step.EDIT_PRIORITY: Step.REVIEW,
            }.get(draft.current_step)

        if previous_step is None:
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Это первый шаг заявки.",
            )

        draft = await self.repository.set_step(telegram_user_id, previous_step)
        if previous_step == Step.REVIEW:
            return self._review_response(draft)
        return await self._prompt_response_for_user(
            telegram_user_id,
            draft,
            prefix="Вернулись на предыдущий шаг.",
        )

    async def open_edit_menu(self, telegram_user_id: int) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step != Step.REVIEW:
            draft = await self.repository.set_step(telegram_user_id, Step.REVIEW)
        return BotResponse(
            text="Что нужно отредактировать?",
            keyboard=(
                KeyboardKind.EDIT_MENU_CHIPS
                if _is_chips(draft)
                else (
                    KeyboardKind.EDIT_MENU_ROLLOUT
                    if _answer_type_requires_change_type(draft.answer_type)
                    else KeyboardKind.EDIT_MENU
                )
            ),
            draft=draft,
        )

    async def select_edit_field(self, telegram_user_id: int, field: FieldName) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        chip_fields = {
            FieldName.CHIP_TEXT_BEFORE,
            FieldName.CHIP_TEXT,
            FieldName.CHIP_AFTER_TEXT_ACTION,
            FieldName.CHIP_TEXT_AFTER,
        }
        if (_is_chips(draft) and field == FieldName.SOURCE_TEXT) or (
            not _is_chips(draft) and field in chip_fields
        ):
            return self._review_response(draft, prefix="Эта кнопка не относится к выбранному типу изменения.")
        if field == FieldName.URGENCY and _direction_requires_answer_type(draft.direction):
            return self._review_response(
                draft,
                prefix="Срочность для этого направления определяется полем «Тип ответа». Чтобы изменить срочность, отредактируйте тип ответа.",
            )
        if (
            _is_chips(draft)
            and field == FieldName.CHANGE_DESCRIPTION
            and _chip_after_text_action(draft)
            not in {ChipAfterTextAction.ADD, ChipAfterTextAction.EDIT}
        ):
            return self._review_response(
                draft,
                prefix="Суть изменений текста после чипса нужна только для ADD или EDIT.",
            )
        step_by_field = {
            FieldName.DIRECTION: Step.EDIT_DIRECTION,
            FieldName.ANSWER_TYPE: Step.EDIT_ANSWER_TYPE,
            FieldName.CHANGE_TYPE: Step.EDIT_CHANGE_TYPE,
            FieldName.INTENT: Step.EDIT_INTENT,
            FieldName.SCRIPTWRITER: Step.EDIT_SCRIPTWRITER,
            FieldName.REASON: Step.EDIT_REASON,
            FieldName.CHANGE_DESCRIPTION: (
                Step.EDIT_CHIP_RESPONSE_CHANGE_DESCRIPTION
                if _is_chips(draft)
                else Step.EDIT_CHANGE_DESCRIPTION
            ),
            FieldName.SOURCE_TEXT: Step.EDIT_SOURCE_TEXT,
            FieldName.CHIP_TEXT_BEFORE: Step.EDIT_CHIP_TEXT_BEFORE,
            FieldName.CHIP_TEXT: Step.EDIT_CHIP_TEXT,
            FieldName.CHIP_AFTER_TEXT_ACTION: Step.EDIT_CHIP_AFTER_TEXT_ACTION,
            FieldName.CHIP_TEXT_AFTER: Step.EDIT_CHIP_TEXT_AFTER,
            FieldName.URGENCY: Step.EDIT_URGENCY,
            FieldName.PRIORITY: Step.EDIT_PRIORITY,
        }
        draft = await self.repository.set_step(telegram_user_id, step_by_field[field])
        return await self._prompt_response_for_user(telegram_user_id, draft)

    async def show_review(self, telegram_user_id: int) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if draft.current_step != Step.REVIEW:
            draft = await self.repository.set_step(telegram_user_id, Step.REVIEW)
        return self._review_response(draft)

    async def submit(self, telegram_user_id: int) -> BotResponse:
        """Идемпотентно отправить одиночную заявку и завершить ее tracking."""
        draft = await self.repository.get_by_user_id(telegram_user_id)
        if draft is None:
            draft = await self.repository.get_or_create(telegram_user_id)
        if draft.submission_state == SubmissionState.SENT.value:
            return BotResponse(
                text=(
                    "Заявка уже отправлена в таблицу.\n\n"
                    "Чтобы создать новую заявку, отправьте /new."
                ),
                keyboard=KeyboardKind.CREATE_MODE,
                draft=draft,
            )
        missing = self._missing_required_fields(draft)
        if missing:
            return BotResponse(
                text=(
                    "Заявку пока нельзя отправить. Не заполнены поля: "
                    + ", ".join(missing)
                    + "."
                ),
                keyboard=KeyboardKind.REVIEW,
                draft=draft,
            )

        draft = await self.repository.ensure_application_id(telegram_user_id)
        application_id = draft.application_id or ""
        lock = self._submission_locks.setdefault(application_id, asyncio.Lock())
        async with lock:
            draft = await self.repository.get_by_user_id(telegram_user_id) or draft
            if draft.submission_state == SubmissionState.SENT.value:
                return BotResponse(
                    text=(
                        "Заявка уже отправлена в таблицу.\n\n"
                        "Чтобы создать новую заявку, отправьте /new."
                    ),
                    keyboard=KeyboardKind.CREATE_MODE,
                    draft=draft,
                )

            resolve_target = getattr(self.submission_service, "resolve_target", None)
            if callable(resolve_target):
                spreadsheet_id, sheet_name = resolve_target(draft)
            else:
                spreadsheet_id, sheet_name = "", ""
            draft = await self.repository.begin_submission(
                telegram_user_id,
                spreadsheet_id=spreadsheet_id,
                sheet_name=sheet_name,
            )
            action = _chip_after_text_action(draft)
            if _is_chips(draft) and action in {
                ChipAfterTextAction.ADD,
                ChipAfterTextAction.EDIT,
            }:
                response_draft = _linked_response_draft(draft, action)

                async def complete_pair(linked: LinkedSubmissionResult) -> None:
                    nonlocal draft
                    chips_result = linked.chips_result
                    response_result = linked.response_result
                    if chips_result is None or response_result is None:
                        raise RuntimeError("Linked submission returned incomplete results")
                    applications = [
                        _tracking_item(draft, chips_result),
                        _tracking_item(response_draft, response_result),
                    ]
                    projections = []
                    if self.dashboard_enabled:
                        projections = [
                            {
                                "entity_id": item.application_id or "",
                                "snapshot": dashboard_projection(
                                    dashboard_row(
                                        item,
                                        ApplicationStatus.NEW.value,
                                        False,
                                        result_item.row_link or "",
                                        submitted_at=result_item.submitted_at,
                                    )
                                ),
                            }
                            for item, result_item in (
                                (draft, chips_result),
                                (response_draft, response_result),
                            )
                        ]
                    draft = await self.repository.complete_linked_submission(
                        telegram_user_id,
                        applications=applications,
                        row_shifts=linked.row_shifts,
                        dashboard_projections=projections,
                        notification_event=self._urgent_editor_linked_notification_event(
                            draft,
                            chips_link=chips_result.row_link,
                            response_id=response_draft.application_id or "",
                            response_link=response_result.row_link,
                        ),
                    )

                submit_pair = getattr(
                    self.submission_service,
                    "submit_chips_with_response",
                    None,
                )
                if not callable(submit_pair):
                    await self.repository.fail_submission(telegram_user_id)
                    return BotResponse(
                        text="Сервис отправки не поддерживает парные CHIPS-заявки.",
                        keyboard=KeyboardKind.REVIEW,
                        draft=draft,
                    )
                linked_result = await submit_pair(
                    draft,
                    response_draft,
                    on_success=complete_pair,
                )
                if not linked_result.success:
                    await self.repository.fail_submission(telegram_user_id)
                    return BotResponse(
                        text=linked_result.message,
                        keyboard=KeyboardKind.REVIEW,
                        draft=draft,
                    )
                return BotResponse(
                    text=(
                        "Заявки отправлены в таблицу.\n\n"
                        f"CHIPS ID: {application_id}\n"
                        f"{action.value} ID: {response_draft.application_id}\n\n"
                        "Чтобы создать новую заявку, отправьте /new."
                    ),
                    keyboard=KeyboardKind.CREATE_MODE,
                    draft=draft,
                )
            result = await self.submission_service.submit(draft)
            if not result.success:
                await self.repository.fail_submission(telegram_user_id)
                return BotResponse(
                    text=result.message,
                    keyboard=KeyboardKind.REVIEW,
                    draft=draft,
                )

            draft = await self.repository.complete_submission(
                telegram_user_id,
                application_id=application_id,
                spreadsheet_id=result.spreadsheet_id,
                sheet_id=result.sheet_id,
                sheet_name=result.sheet_name or sheet_name,
                row_number=result.row_number,
                last_known_status=ApplicationStatus.NEW.value,
                direction=draft.direction,
                answer_type=draft.answer_type,
                application_type=draft.application_type,
                change_type=draft.change_type,
                is_urgent=draft.is_urgent,
                submitted_at=result.submitted_at,
                dashboard_projection=(
                    dashboard_projection(
                        dashboard_row(
                            draft,
                            ApplicationStatus.NEW.value,
                            False,
                            result.row_link or "",
                            submitted_at=result.submitted_at,
                        )
                    )
                    if self.dashboard_enabled
                    else None
                ),
                notification_event=self._urgent_editor_notification_event(
                    draft,
                    row_link=result.row_link,
                ),
            )
        return BotResponse(
            text=(
                "Заявка отправлена в таблицу.\n\n"
                f"ID заявки: {application_id}\n\n"
                "Чтобы создать новую заявку, отправьте /new."
            ),
            keyboard=KeyboardKind.CREATE_MODE,
            draft=draft,
        )

    def _urgent_editor_notification_event(
        self,
        draft: Draft,
        *,
        row_link: str | None,
    ) -> dict[str, object] | None:
        if not self.urgent_editor_notifications_enabled:
            return None
        if self.editor_urgent_chat_id is None:
            return None
        if not draft.is_urgent:
            return None
        if not row_link:
            return None
        application_id = draft.application_id or ""
        if not application_id:
            return None
        snapshot = {
            "application_id": application_id,
            "direction": draft.direction or "",
            "scriptwriter": draft.scriptwriter or "",
            "intent": draft.intent or "",
            "row_link": row_link,
        }
        return {
            "telegram_user_id": self.editor_urgent_chat_id,
            "event_type": URGENT_EDITOR_NOTIFICATION_EVENT_TYPE,
            "dedupe_key": f"{URGENT_EDITOR_NOTIFICATION_EVENT_TYPE}:{application_id}",
            "snapshot_json": json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
            ),
            "chunks": [_render_urgent_editor_notification(snapshot)],
        }

    def _urgent_editor_linked_notification_event(
        self,
        draft: Draft,
        *,
        chips_link: str | None,
        response_id: str,
        response_link: str | None,
    ) -> dict[str, object] | None:
        if (
            not self.urgent_editor_notifications_enabled
            or self.editor_urgent_chat_id is None
            or not draft.is_urgent
            or not chips_link
            or not response_link
            or not draft.application_id
        ):
            return None
        snapshot = {
            "application_id": draft.application_id,
            "response_application_id": response_id,
            "direction": draft.direction or "",
            "scriptwriter": draft.scriptwriter or "",
            "intent": draft.intent or "",
            "chips_link": chips_link,
            "response_link": response_link,
        }
        return {
            "telegram_user_id": self.editor_urgent_chat_id,
            "event_type": URGENT_EDITOR_NOTIFICATION_EVENT_TYPE,
            "dedupe_key": (
                f"{URGENT_EDITOR_NOTIFICATION_EVENT_TYPE}:linked:{draft.application_id}"
            ),
            "snapshot_json": json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            "chunks": [_render_urgent_editor_linked_notification(snapshot)],
        }

    async def _process_change_description(
        self,
        telegram_user_id: int,
        value: str,
        *,
        edit_mode: bool = False,
    ) -> BotResponse:
        """Выполнить первую рекомендательную проверку поля."""
        existing = await self.repository.get_by_user_id(telegram_user_id)
        if existing is not None and _is_chips(existing):
            await self.repository.save_llm_result(
                telegram_user_id,
                formatted_change_description=None,
                raw_change_description=None,
                llm_check_status=LlmCheckStatus.SKIPPED.value,
                llm_score=None,
                clarification_count=0,
            )
            next_step = _first_missing_step(existing) or Step.REVIEW
            draft = await self.repository.set_step(telegram_user_id, next_step)
            if next_step == Step.REVIEW:
                return self._review_response(draft)
            return await self._prompt_response_for_user(telegram_user_id, draft)

        if edit_mode:
            if existing is None:
                return await self.start_new(telegram_user_id, force=True)
            await self.repository.save_llm_result(
                telegram_user_id,
                raw_change_description=value,
                formatted_change_description=value,
                llm_check_status=existing.llm_check_status,
                llm_score=None,
                clarification_count=existing.clarification_count,
            )
            return await self._return_to_review(
                telegram_user_id,
                prefix="Суть изменений обновлена без повторной проверки.",
            )

        draft = existing or await self._get_active_or_start(telegram_user_id)
        now = _utc_now()
        process = {
            "schema_version": 1,
            "application_id": draft.application_id,
            "telegram_user_id": telegram_user_id,
            "field_code": "change_description",
            "state": "checking",
            "started_at": now,
            "completed_at": None,
            "initial_text": value,
            "current_text": value,
            "context": {
                "direction": draft.direction or "",
                "answer_type": draft.answer_type or "",
                "change_type": draft.change_type or "",
                "intent": draft.intent or "",
                "reason": draft.reason or "",
            },
            "iterations": [
                _llm_iteration_started(
                    number=1,
                    current_text=value,
                    initial_text=value,
                )
            ],
            "actions": [],
            "final_outcome": None,
        }
        draft = await self.repository.save_llm_recommendation_state(
            telegram_user_id,
            process=process,
            draft_values={
                "raw_change_description": value,
                "formatted_change_description": value,
                "llm_check_status": LlmCheckStatus.NOT_CHECKED.value,
                "llm_score": None,
                "clarification_count": 0,
            },
        )
        llm_result = await self.llm_client.check_change_description(
            LlmContext(
                direction=draft.direction or "",
                answer_type=draft.answer_type or "",
                change_type=draft.change_type or "",
                intent=draft.intent or "",
                scriptwriter=draft.scriptwriter or "",
                reason=draft.reason or "",
                raw_change_description=value,
                initial_change_description=value,
                iteration_number=1,
            )
        )
        _complete_llm_iteration(process["iterations"][-1], llm_result)
        status = self._status_from_llm_result(llm_result)
        is_error = _is_llm_error_result(llm_result)
        if is_error or llm_result.is_complete:
            process["state"] = "completed"
            process["completed_at"] = _utc_now()
            process["final_outcome"] = "technical_fallback" if is_error else "ok"
            next_step = Step.SOURCE_TEXT
        else:
            process["state"] = "awaiting_action"
            process["final_outcome"] = None
            next_step = Step.CHANGE_DESCRIPTION_RECOMMENDATION
        draft = await self.repository.save_llm_recommendation_state(
            telegram_user_id,
            process=process,
            draft_values={
                "current_step": next_step.value,
                "formatted_change_description": value,
                "llm_check_status": status,
                "llm_score": None,
                "clarification_count": 0,
            },
            application_event=_llm_check_completed_event(
                draft,
                llm_result,
                stage="initial",
                trigger="edit" if edit_mode else "create",
                status=status,
            ),
        )

        if next_step == Step.SOURCE_TEXT:
            prefix = self._with_llm_json(
                self._llm_success_prefix(status),
                llm_result,
            )
            return await self._prompt_response_for_user(telegram_user_id, draft, prefix=prefix)
        return self._recommendation_response(draft, process)

    async def _process_change_revision(
        self,
        telegram_user_id: int,
        additional_text: str,
    ) -> BotResponse:
        draft = await self.repository.get_by_user_id(telegram_user_id)
        if draft is None:
            return await self.start_new(telegram_user_id, force=True)
        if _is_chips(draft):
            next_step = _first_missing_step(draft) or Step.REVIEW
            draft = await self.repository.set_step(telegram_user_id, next_step)
            if next_step == Step.REVIEW:
                return self._review_response(draft)
            return await self._prompt_response_for_user(telegram_user_id, draft)

        process = await self._load_llm_process(draft)
        if process is None:
            await self.repository.set_step(telegram_user_id, Step.CHANGE_DESCRIPTION)
            return await self._process_change_description(telegram_user_id, additional_text)
        iterations = process.setdefault("iterations", [])
        if len(iterations) >= 2 or process.get("state") != "awaiting_revision":
            return await self._advance_after_llm(
                telegram_user_id,
                draft,
                prefix="Лимит проверок уже исчерпан. Суть изменений сохранена.",
                revised_text=str(
                    process.get("current_text")
                    or draft.raw_change_description
                    or ""
                ),
            )

        trigger = "create"
        previous = iterations[-1]
        previous_response = previous.get("response") or {}
        initial_text = str(process.get("initial_text") or draft.raw_change_description or "")
        prior_text = str(process.get("current_text") or draft.raw_change_description or "")
        revised_text = _append_change_description(prior_text, additional_text)
        now = _utc_now()
        actions = process.setdefault("actions", [])
        if actions and actions[-1].get("action") == "add":
            actions[-1].update(
                {
                    "submitted_at": now,
                    "text_changed": revised_text != prior_text,
                    "text_delta_chars": len(revised_text) - len(prior_text),
                }
            )
        process["state"] = "checking"
        process["current_text"] = revised_text
        iterations.append(
            _llm_iteration_started(
                number=2,
                current_text=revised_text,
                initial_text=initial_text,
                previous_gap_code=previous_response.get("gap_code"),
                previous_recommendation=previous_response.get("recommendation"),
                previous_missing_detail=previous_response.get("missing_detail"),
            )
        )
        draft = await self.repository.save_llm_recommendation_state(
            telegram_user_id,
            process=process,
            draft_values={
                "raw_change_description": revised_text,
                "formatted_change_description": revised_text,
                "clarification_count": 1,
            },
        )
        await self.repository.record_application_event(
            event_type=LLM_CLARIFICATION_SUBMITTED_EVENT_TYPE,
            application_id=draft.application_id,
            telegram_user_id=telegram_user_id,
            metadata={
                "schema_version": 1,
                "clarification_number": 1,
                "trigger": trigger,
            },
        )
        llm_result = await self.llm_client.check_change_description(
            LlmContext(
                direction=draft.direction or "",
                answer_type=draft.answer_type or "",
                change_type=draft.change_type or "",
                intent=draft.intent or "",
                scriptwriter=draft.scriptwriter or "",
                reason=draft.reason or "",
                raw_change_description=revised_text,
                initial_change_description=initial_text,
                clarification_text=revised_text,
                previous_gap_code=str(previous_response.get("gap_code") or ""),
                previous_recommendation=str(
                    previous_response.get("recommendation") or ""
                ),
                previous_missing_detail=str(
                    previous_response.get("missing_detail") or ""
                ),
                iteration_number=2,
            )
        )
        _complete_llm_iteration(iterations[-1], llm_result)
        status = self._status_from_llm_result(llm_result, after_clarification=True)
        is_error = _is_llm_error_result(llm_result)
        process["state"] = "completed"
        process["completed_at"] = _utc_now()
        process["final_outcome"] = (
            "technical_fallback"
            if is_error
            else ("ok" if llm_result.is_complete else "recommendation_after_limit")
        )
        next_step = Step.SOURCE_TEXT
        draft = await self.repository.save_llm_recommendation_state(
            telegram_user_id,
            process=process,
            draft_values={
                "current_step": next_step.value,
                "raw_change_description": revised_text,
                "formatted_change_description": revised_text,
                "llm_check_status": status,
                "llm_score": None,
                "clarification_count": 1,
            },
            application_event=_llm_check_completed_event(
                draft,
                llm_result,
                stage="clarification",
                trigger=trigger,
                status=status,
            ),
        )
        if status == LlmCheckStatus.NEEDS_ATTENTION.value:
            prefix = _result_user_recommendation(llm_result) or (
                "Может быть, в описании всё ещё не хватает важной информации."
            )
        else:
            prefix = self._llm_success_prefix(status)
        prefix = self._with_llm_json(prefix, llm_result)
        return await self._prompt_response_for_user(telegram_user_id, draft, prefix=prefix)

    async def _load_llm_process(self, draft: Draft) -> dict[str, object] | None:
        if not draft.application_id:
            return None
        stored = await self.repository.get_llm_recommendation_process(draft.application_id)
        if stored is None:
            return None
        try:
            process = json.loads(stored.process_json)
        except (TypeError, json.JSONDecodeError):
            return None
        return process if isinstance(process, dict) else None

    async def _closed_llm_process(
        self,
        draft: Draft,
        outcome: str,
    ) -> dict[str, object] | None:
        process = await self._load_llm_process(draft)
        if process is None or process.get("state") in {"completed", "skipped", "cancelled"}:
            return None
        process["state"] = "cancelled"
        process["completed_at"] = _utc_now()
        process["final_outcome"] = outcome
        process.setdefault("actions", []).append(
            {
                "action": outcome,
                "selected_at": _utc_now(),
            }
        )
        return process

    def _recommendation_response(
        self,
        draft: Draft,
        process: dict[str, object],
    ) -> BotResponse:
        iterations = process.get("iterations")
        latest = iterations[-1] if isinstance(iterations, list) and iterations else {}
        response = latest.get("response") if isinstance(latest, dict) else {}
        text = _response_user_recommendation(response)
        return BotResponse(
            text=f"{escape(text)}\n\nХотите дополнить описание или пропустить рекомендацию?",
            keyboard=KeyboardKind.LLM_RECOMMENDATION,
            draft=draft,
        )

    async def _advance_after_llm(
        self,
        telegram_user_id: int,
        draft: Draft,
        *,
        prefix: str,
        revised_text: str | None = None,
    ) -> BotResponse:
        if revised_text is not None:
            await self.repository.save_llm_result(
                telegram_user_id,
                raw_change_description=revised_text,
                formatted_change_description=revised_text,
                llm_check_status=draft.llm_check_status,
                llm_score=None,
                clarification_count=draft.clarification_count,
            )
        draft = await self.repository.set_step(telegram_user_id, Step.SOURCE_TEXT)
        return await self._prompt_response_for_user(
            telegram_user_id,
            draft,
            prefix=prefix,
        )

    def _with_llm_json(self, prefix: str, llm_result) -> str:
        if not self.show_llm_response_json:
            return prefix
        return f"{prefix}\n\nОтвет GigaChat:\n{_llm_result_to_json(llm_result)}"

    @staticmethod
    def _status_from_llm_result(
        llm_result,
        *,
        after_clarification: bool = False,
    ) -> str:
        if _is_llm_error_result(llm_result):
            return LlmCheckStatus.ERROR.value
        if llm_result.is_complete:
            return LlmCheckStatus.COMPLETE.value
        if after_clarification:
            return LlmCheckStatus.NEEDS_ATTENTION.value
        return LlmCheckStatus.NEEDS_ATTENTION.value

    @staticmethod
    def _llm_success_prefix(status: str) -> str:
        if status == LlmCheckStatus.ERROR.value:
            return (
                "GigaChat временно не смог корректно проверить описание. "
                "Продолжаем заполнение заявки; редактор увидит пометку о проблеме проверки."
            )
        if status == LlmCheckStatus.NEEDS_ATTENTION.value:
            return (
                "Суть изменений сохранена, но заявка будет отмечена как требующая "
                "дополнительного внимания редактора."
            )
        return "Полнота описания проверена."

    @staticmethod
    def _clarification_prefix(llm_result) -> str:
        lines = ["В описании не хватает информации для выполнения заявки без догадок."]
        if llm_result.blocking_problem:
            lines.append(f"Главный блокер: {llm_result.blocking_problem}")
        if llm_result.clarification_instruction:
            lines.extend(["", llm_result.clarification_instruction])
        return "\n".join(lines)

    async def _return_to_review(
        self,
        telegram_user_id: int,
        *,
        prefix: str = "Поле обновлено.",
    ) -> BotResponse:
        draft = await self.repository.set_step(telegram_user_id, Step.REVIEW)
        return self._review_response(draft, prefix=prefix)

    async def _save_source_text(
        self,
        telegram_user_id: int,
        value: str,
        formatting_spans: list[TextFormattingSpan] | None,
    ) -> None:
        await self.repository.save_answer(telegram_user_id, FieldName.SOURCE_TEXT.value, value)
        await self.repository.save_answer(
            telegram_user_id,
            "source_text_formatting_json",
            serialize_formatting_spans(formatting_spans) or "",
        )

    async def _save_formatted_text(
        self,
        telegram_user_id: int,
        field: FieldName,
        value: str,
        formatting_spans: list[TextFormattingSpan] | None,
    ) -> None:
        await self.repository.save_answer(telegram_user_id, field.value, value)
        await self.repository.save_answer(
            telegram_user_id,
            f"{field.value}_formatting_json",
            serialize_formatting_spans(formatting_spans) or "",
        )

    async def _handle_pending_settings_text(
        self,
        telegram_user_id: int,
        text: str | None,
    ) -> BotResponse | None:
        pending_action = await self.repository.get_pending_settings_action(telegram_user_id)
        if pending_action is None:
            return None

        value = (text or "").strip()
        if pending_action.startswith(f"{PENDING_BULK_RESERVATION_COUNT}:"):
            reservation_id = pending_action.split(":", maxsplit=1)[1]
            return await self.save_bulk_reservation_count(
                telegram_user_id,
                value,
                reservation_id,
            )

        if not value:
            return BotResponse(
                text="Значение не должно быть пустым.",
                keyboard=KeyboardKind.DEFAULTS_BACK,
            )

        if pending_action == PENDING_SET_DEFAULT_INTENT:
            await self.repository.save_user_setting(
                telegram_user_id,
                "default_intent",
                value,
            )
        elif pending_action == PENDING_SET_DEFAULT_DIRECTION:
            value = direction_value_from_display_label(value)
            try:
                direction = Direction(value)
            except ValueError:
                return BotResponse(
                    text=(
                        "Неизвестное направление. Введите одно из значений: "
                        "ФЛ-chatbot, SME-chatbot, АИ-chatbot, VoiceBot-chatbot, Collection-chatbot."
                    ),
                    keyboard=KeyboardKind.DEFAULTS_BACK,
                )
            await self.repository.save_user_setting(
                telegram_user_id,
                "default_direction",
                direction.value,
            )
        elif pending_action == PENDING_SET_DEFAULT_SCRIPTWRITER:
            await self.repository.save_user_setting(
                telegram_user_id,
                "default_scriptwriter",
                value,
            )
        else:
            await self.repository.save_user_setting(telegram_user_id, "pending_action", None)
            return None

        await self.repository.save_user_setting(telegram_user_id, "pending_action", None)
        return await self.open_defaults_menu(telegram_user_id)

    async def _get_active_or_start(self, telegram_user_id: int) -> Draft:
        draft = await self.repository.get_by_user_id(telegram_user_id)
        if draft is None or not draft.is_active:
            draft = await self.repository.get_or_create(telegram_user_id)
            if not draft.is_active:
                await self.repository.delete(telegram_user_id)
                draft = await self.repository.get_or_create(telegram_user_id)
        return draft

    async def _prompt_response_for_user(
        self,
        telegram_user_id: int,
        draft: Draft,
        *,
        prefix: str | None = None,
    ) -> BotResponse:
        if draft.current_step == Step.CHANGE_DESCRIPTION_RECOMMENDATION:
            process = await self._load_llm_process(draft)
            if process is not None:
                return self._recommendation_response(draft, process)
        if draft.current_step == Step.CHANGE_DESCRIPTION_REVISION and prefix is None:
            process = await self._load_llm_process(draft)
            if process is not None:
                iterations = process.get("iterations")
                latest = iterations[-1] if isinstance(iterations, list) and iterations else {}
                result = latest.get("response") if isinstance(latest, dict) else {}
                prefix = (
                    f"Рекомендация:\n{escape(_response_user_recommendation(result))}\n\n"
                    f"Сейчас сохранено:\n{escape(draft.raw_change_description or '')}"
                )
        response = self._prompt_response(draft, prefix=prefix)
        if draft.current_step == Step.DIRECTION:
            settings = await self.repository.get_user_settings(telegram_user_id)
            if settings.default_direction:
                response.keyboard = KeyboardKind.DIRECTION_WITH_DEFAULT
        elif draft.current_step == Step.INTENT:
            settings = await self.repository.get_user_settings(telegram_user_id)
            if settings.default_intent:
                response.keyboard = KeyboardKind.INTENT_STEP_WITH_DEFAULT
        elif draft.current_step == Step.SCRIPTWRITER:
            settings = await self.repository.get_user_settings(telegram_user_id)
            if settings.default_scriptwriter:
                response.keyboard = KeyboardKind.SCRIPTWRITER_STEP_WITH_DEFAULT
        return response

    async def _normalize_change_type_step(
        self,
        telegram_user_id: int,
        draft: Draft,
    ) -> Draft:
        """Move pre-migration drafts away from steps incompatible with their type."""
        if not _is_chips(draft):
            return draft
        incompatible_steps = {
            Step.CHANGE_DESCRIPTION,
            Step.CHANGE_DESCRIPTION_CLARIFICATION,
            Step.CHANGE_DESCRIPTION_RECOMMENDATION,
            Step.CHANGE_DESCRIPTION_REVISION,
            Step.CHANGE_DESCRIPTION_SKIP_REASON,
            Step.SOURCE_TEXT,
            Step.EDIT_CHANGE_DESCRIPTION,
            Step.EDIT_SOURCE_TEXT,
        }
        next_step = _first_missing_step(draft)
        needs_review_repair = draft.current_step == Step.REVIEW and next_step is not None
        if draft.current_step not in incompatible_steps and not needs_review_repair:
            return draft
        return await self.repository.set_step(telegram_user_id, next_step or Step.REVIEW)

    def _prompt_response(self, draft: Draft, *, prefix: str | None = None) -> BotResponse:
        text = STEP_PROMPTS[draft.current_step]
        if draft.current_step == Step.CHIP_TEXT_AFTER:
            action = _chip_after_text_action(draft)
            text = {
                ChipAfterTextAction.ADD: (
                    "Пришлите новый текст ответа, который будет показан после нажатия на чипс."
                ),
                ChipAfterTextAction.EDIT: (
                    "Пришлите предлагаемую версию текста ответа после чипса."
                ),
                ChipAfterTextAction.UNCHANGED: (
                    "Пришлите текущий текст ответа после чипса. "
                    "Он будет указан только как контекст."
                ),
            }.get(action, text)
        if prefix:
            text = f"{prefix}\n\n{text}"
        return BotResponse(
            text=text,
            keyboard=self._keyboard_for_step(draft.current_step),
            draft=draft,
        )

    def _review_response(self, draft: Draft, *, prefix: str | None = None) -> BotResponse:
        text = self._render_review(draft)
        if prefix:
            text = f"{escape(prefix)}\n\n{text}"
        return BotResponse(
            text=text,
            keyboard=KeyboardKind.REVIEW,
            draft=draft,
            parse_mode="HTML",
        )

    @staticmethod
    def _keyboard_for_step(step: Step) -> KeyboardKind:
        if step in {Step.DIRECTION, Step.EDIT_DIRECTION}:
            return KeyboardKind.DIRECTION
        if step in {Step.ANSWER_TYPE, Step.EDIT_ANSWER_TYPE}:
            return KeyboardKind.ANSWER_TYPE
        if step in {Step.CHANGE_TYPE, Step.EDIT_CHANGE_TYPE}:
            return KeyboardKind.CHANGE_TYPE
        if step in {Step.CHIP_AFTER_TEXT_ACTION, Step.EDIT_CHIP_AFTER_TEXT_ACTION}:
            return KeyboardKind.CHIP_AFTER_TEXT_ACTION
        if step in {Step.URGENCY, Step.EDIT_URGENCY}:
            return KeyboardKind.URGENCY
        if step in {Step.PRIORITY, Step.EDIT_PRIORITY}:
            return KeyboardKind.PRIORITY
        if step == Step.CHANGE_DESCRIPTION_RECOMMENDATION:
            return KeyboardKind.LLM_RECOMMENDATION
        if step == Step.CHANGE_DESCRIPTION_SKIP_REASON:
            return KeyboardKind.LLM_SKIP_REASON
        if step == Step.REVIEW:
            return KeyboardKind.REVIEW
        if step == Step.COMPLETED:
            return KeyboardKind.START
        return KeyboardKind.STEP

    @staticmethod
    def _render_review(draft: Draft) -> str:
        if _is_chips(draft):
            response_change_description = (
                "📝 <b>Суть изменений текста после чипса:</b> "
                f"{_html_value(draft.formatted_change_description or draft.raw_change_description)}\n"
                if _chip_after_text_action(draft)
                in {ChipAfterTextAction.ADD, ChipAfterTextAction.EDIT}
                else ""
            )
            return (
                "Проверьте заявку перед отправкой.\n\n"
                f"🧭 <b>Направление:</b> {_html_value(direction_display_label(draft.direction))}\n"
                f"🏷 <b>Тип ответа:</b> {_html_value(draft.answer_type)}\n"
                f"🔀 <b>Тип изменения:</b> {ChangeType.CHIPS.value}\n"
                f"👤 <b>Закрепленный сценарист:</b> {_html_value(draft.scriptwriter)}\n"
                f"🎯 <b>Интент:</b> {_html_value(draft.intent)}\n"
                f"📝 <b>Причина:</b> {_html_value(draft.reason)}\n"
                "⬅️ <b>Текст до чипса:</b> "
                f"{_render_formatted_draft_value(draft.chip_text_before, draft.chip_text_before_formatting_json)}\n"
                "🔘 <b>Текст чипса:</b> "
                f"{_render_formatted_draft_value(draft.chip_text, draft.chip_text_formatting_json)}\n"
                "🔗 <b>Действие с текстом после чипса:</b> "
                f"{_html_value(_chip_after_text_action_review(draft))}\n"
                f"{response_change_description}"
                "➡️ <b>Текст после чипса:</b> "
                f"{_render_formatted_draft_value(draft.chip_text_after, draft.chip_text_after_formatting_json)}\n"
                f"⚡ <b>Срочная:</b> {_html_value('Да' if draft.is_urgent else 'Нет')}"
            )
        change_type_line = (
            f"🔀 <b>Тип изменения:</b> {_html_value(draft.change_type)}\n"
            if _answer_type_requires_change_type(draft.answer_type)
            else ""
        )
        return (
            "Проверьте заявку перед отправкой.\n\n"
            f"🧭 <b>Направление:</b> {_html_value(direction_display_label(draft.direction))}\n"
            f"🏷 <b>Тип ответа:</b> {_html_value(draft.answer_type or 'Не требуется')}\n"
            f"{change_type_line}"
            f"🎯 <b>Интент:</b> {_html_value(draft.intent)}\n"
            f"👤 <b>Закрепленный сценарист:</b> {_html_value(draft.scriptwriter)}\n"
            f"📝 <b>Кейс или сообщения клиента:</b> {_html_value(draft.reason)}\n"
            "🔧 <b>Суть изменений:</b> "
            f"{_html_value(draft.formatted_change_description or draft.raw_change_description)}\n"
            "📄 <b>Исходный текст:</b> "
            f"{render_html_with_formatting(draft.source_text, deserialize_formatting_spans(draft.source_text_formatting_json))}\n"
            f"⚡ <b>Срочная:</b> {_html_value('Да' if draft.is_urgent else 'Нет')}"
        )

    @staticmethod
    def _missing_required_fields(draft: Draft) -> list[str]:
        missing: list[str] = []
        if not draft.direction:
            missing.append(FIELD_LABELS[FieldName.DIRECTION])
        if _direction_requires_answer_type(draft.direction) and not draft.answer_type:
            missing.append(FIELD_LABELS[FieldName.ANSWER_TYPE])
        if (
            _answer_type_requires_change_type(draft.answer_type)
            and ChangeType.normalize(draft.change_type) is None
        ):
            missing.append(FIELD_LABELS[FieldName.CHANGE_TYPE])
        if not draft.intent:
            missing.append(FIELD_LABELS[FieldName.INTENT])
        if not draft.scriptwriter:
            missing.append(FIELD_LABELS[FieldName.SCRIPTWRITER])
        if not draft.reason:
            missing.append("Причина" if _is_chips(draft) else FIELD_LABELS[FieldName.REASON])
        if _is_chips(draft):
            if not draft.chip_text_before:
                missing.append(FIELD_LABELS[FieldName.CHIP_TEXT_BEFORE])
            if not draft.chip_text:
                missing.append(FIELD_LABELS[FieldName.CHIP_TEXT])
            if _chip_after_text_action(draft) is None:
                missing.append(FIELD_LABELS[FieldName.CHIP_AFTER_TEXT_ACTION])
            if (
                _chip_after_text_action(draft)
                in {ChipAfterTextAction.ADD, ChipAfterTextAction.EDIT}
                and not (draft.formatted_change_description or draft.raw_change_description)
            ):
                missing.append("Суть изменений текста после чипса")
            if not draft.chip_text_after:
                missing.append(FIELD_LABELS[FieldName.CHIP_TEXT_AFTER])
        else:
            if not draft.formatted_change_description and not draft.raw_change_description:
                missing.append(FIELD_LABELS[FieldName.CHANGE_DESCRIPTION])
            if not draft.source_text:
                missing.append(FIELD_LABELS[FieldName.SOURCE_TEXT])
        if draft.is_urgent is None:
            missing.append(FIELD_LABELS[FieldName.URGENCY])
        return missing


def _html_value(value: str | None) -> str:
    return escape(value or "-")


def _render_formatted_draft_value(value: str | None, formatting_json: str | None) -> str:
    return render_html_with_formatting(
        value,
        deserialize_formatting_spans(formatting_json),
    )


def _is_chips(draft: Draft) -> bool:
    return ChangeType.normalize(draft.change_type) == ChangeType.CHIPS


def _chip_after_text_action(draft: Draft) -> ChipAfterTextAction | None:
    try:
        return ChipAfterTextAction(draft.chip_after_text_action or "")
    except ValueError:
        return None


def _chip_after_text_action_label(draft: Draft) -> str:
    action = _chip_after_text_action(draft)
    if action is None:
        return "Не выбрано"
    return action.label


def _chip_after_text_action_review(draft: Draft) -> str:
    action = _chip_after_text_action(draft)
    return {
        ChipAfterTextAction.ADD: "Будут созданы CHIPS + ADD",
        ChipAfterTextAction.EDIT: "Будут созданы CHIPS + EDIT",
        ChipAfterTextAction.UNCHANGED: "Будет создана только CHIPS-заявка",
    }.get(action, "Не выбрано")


def _bulk_registration_confirmation(result: BulkRegistrationResult) -> str:
    if not result.application_ids:
        return escape(result.message)
    if len(result.application_links) != len(result.application_ids):
        raise ValueError("Bulk registration result has incomplete application links")
    id_lines = [
        f'• <a href="{escape(link, quote=True)}">{escape(application_id)}</a>'
        for application_id, link in zip(
            result.application_ids,
            result.application_links,
            strict=True,
        )
    ]
    rendered_ids = "\n".join(id_lines)
    if len(result.application_ids) > 5:
        rendered_ids = f"<blockquote expandable>{rendered_ids}</blockquote>"
    return "\n".join(
        [
            "✅ <b>Массовая заявка зарегистрирована</b>",
            "",
            f"<b>Статус:</b> {escape(ApplicationStatus.NEW.value)}",
            f"<b>Зарегистрировано заявок:</b> {result.registered_count}",
            f"<b>Пустых строк оставлено:</b> {result.empty_count}",
            "",
            "<b>ID заявок:</b>",
            rendered_ids,
        ]
    )


def _linked_response_application_id(chips_application_id: str) -> str:
    return hashlib.sha256(
        f"chip-response:{chips_application_id}".encode("utf-8")
    ).hexdigest()[:8].upper()


def _linked_response_draft(
    chips: Draft,
    action: ChipAfterTextAction,
) -> Draft:
    if action not in {ChipAfterTextAction.ADD, ChipAfterTextAction.EDIT}:
        raise ValueError("Only ADD and EDIT create a second application")
    chips_id = chips.application_id or ""
    generated_description = (
        "Добавление нового текста ответа после чипса."
        if action == ChipAfterTextAction.ADD
        else "Изменение текста ответа после чипса."
    )
    generated_description = f"{generated_description} Связано с CHIPS-заявкой {chips_id}."
    user_description = (
        chips.formatted_change_description or chips.raw_change_description or ""
    ).strip()
    description = (
        f"{user_description}\n\n{generated_description}"
        if user_description
        else generated_description
    )
    return replace(
        chips,
        application_id=_linked_response_application_id(chips_id),
        change_type=action.value,
        source_text=chips.chip_text_after,
        source_text_formatting_json=chips.chip_text_after_formatting_json,
        raw_change_description=description,
        formatted_change_description=description,
        llm_check_status=LlmCheckStatus.SKIPPED.value,
        llm_score=None,
        chip_text_before=None,
        chip_text_before_formatting_json=None,
        chip_text=None,
        chip_text_formatting_json=None,
        chip_after_text_action=None,
        chip_text_after=None,
        chip_text_after_formatting_json=None,
    )


def _tracking_item(draft: Draft, result) -> dict[str, object]:
    return {
        "application_id": draft.application_id or "",
        "telegram_user_id": draft.telegram_user_id,
        "spreadsheet_id": result.spreadsheet_id,
        "sheet_id": result.sheet_id,
        "sheet_name": result.sheet_name or "",
        "last_known_status": ApplicationStatus.NEW.value,
        "direction": draft.direction,
        "answer_type": draft.answer_type,
        "application_type": draft.application_type,
        "change_type": draft.change_type,
        "is_urgent": draft.is_urgent,
        "batch_id": None,
        "last_seen_row_number": result.row_number,
        "last_seen_editor": None,
        "last_seen_editor_comment": None,
        "last_seen_final_answer": None,
        "submitted_at": result.submitted_at,
    }


def _first_missing_step(draft: Draft) -> Step | None:
    common = (
        (draft.scriptwriter, Step.SCRIPTWRITER),
        (draft.intent, Step.INTENT),
        (draft.reason, Step.REASON),
    )
    for value, step in common:
        if not value:
            return step
    if _is_chips(draft):
        for value, step in (
            (draft.chip_text_before, Step.CHIP_TEXT_BEFORE),
            (draft.chip_text, Step.CHIP_TEXT),
            (draft.chip_after_text_action, Step.CHIP_AFTER_TEXT_ACTION),
        ):
            if not value:
                return step
        if (
            _chip_after_text_action(draft)
            in {ChipAfterTextAction.ADD, ChipAfterTextAction.EDIT}
            and not (draft.formatted_change_description or draft.raw_change_description)
        ):
            return Step.CHIP_RESPONSE_CHANGE_DESCRIPTION
        if not draft.chip_text_after:
            return Step.CHIP_TEXT_AFTER
        return None
    if not draft.formatted_change_description and not draft.raw_change_description:
        return Step.CHANGE_DESCRIPTION
    if not draft.source_text:
        return Step.SOURCE_TEXT
    return None


def _render_urgent_editor_notification(snapshot: dict[str, str]) -> str:
    return "\n".join(
        [
            "🚨 <b>Новая срочная заявка</b>",
            "",
            f"<b>Направление:</b> {_html_value(snapshot.get('direction'))}",
            "",
            f"<b>Сценарист:</b> {_html_value(snapshot.get('scriptwriter'))}",
            f"<b>Интент:</b> {_html_value(snapshot.get('intent'))}",
            "",
            (
                f'<a href="{escape(snapshot.get("row_link", ""), quote=True)}">'
                "Открыть заявку</a>"
            ),
        ]
    )


def _render_urgent_editor_linked_notification(snapshot: dict[str, str]) -> str:
    return "\n".join(
        [
            "🚨 <b>Новые срочные заявки CHIPS + текст ответа</b>",
            "",
            f"<b>Направление:</b> {_html_value(snapshot.get('direction'))}",
            f"<b>Сценарист:</b> {_html_value(snapshot.get('scriptwriter'))}",
            f"<b>Интент:</b> {_html_value(snapshot.get('intent'))}",
            "",
            (
                f'<a href="{escape(snapshot.get("chips_link", ""), quote=True)}">'
                f'Открыть CHIPS {escape(snapshot.get("application_id", ""))}</a>'
            ),
            (
                f'<a href="{escape(snapshot.get("response_link", ""), quote=True)}">'
                "Открыть заявку на текст ответа "
                f'{escape(snapshot.get("response_application_id", ""))}</a>'
            ),
        ]
    )


def _direction_requires_answer_type(direction: str | None) -> bool:
    return direction in {Direction.FL.value, Direction.SME.value, Direction.AI.value}


def _answer_type_requires_change_type(answer_type: str | None) -> bool:
    return answer_type in {AnswerType.ROLLOUT.value, AnswerType.URGENT.value}


def _bulk_target_label(target_kind: str | None) -> str:
    labels = {
        BulkTargetKind.ROLLOUT.value: AnswerType.ROLLOUT.value,
        BulkTargetKind.URGENT.value: AnswerType.URGENT.value,
        BulkTargetKind.INTEGRATION.value: AnswerType.INTEGRATION.value,
    }
    return labels.get(target_kind or "", target_kind or "-")


def _bulk_change_type_keyboard_payload(reservation: BulkReservation) -> str:
    if reservation.target_kind == BulkTargetKind.INTEGRATION.value:
        return f"{reservation.reservation_id}:integration"
    return reservation.reservation_id


def _bulk_reservation_is_recent(
    reservation: BulkReservation,
    *,
    stale_after_seconds: int,
) -> bool:
    if not reservation.started_at:
        return False
    try:
        started = datetime.fromisoformat(reservation.started_at)
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started < timedelta(seconds=stale_after_seconds)


def _is_llm_error_result(llm_result) -> bool:
    if getattr(llm_result, "check_result", None) == LlmCheckResult.ERROR.value:
        return True
    problem = llm_result.blocking_problem or ""
    return (
        problem.startswith(LLM_ERROR_PREFIX)
        or problem.startswith("РћС€РёР±РєР° GigaChat:")
        or problem == "GIGACHAT_CREDENTIALS не задан."
        or problem == "GIGACHAT_CREDENTIALS РЅРµ Р·Р°РґР°РЅ."
    )


def _llm_check_completed_event(
    draft: Draft,
    llm_result,
    *,
    stage: str,
    trigger: str,
    status: str,
) -> dict[str, object]:
    telemetry = getattr(llm_result, "telemetry", None)
    if status == LlmCheckStatus.ERROR.value:
        outcome = "technical_fallback"
    elif llm_result.is_complete:
        outcome = "passed"
    else:
        outcome = "needs_clarification"
    return {
        "event_type": LLM_CHECK_COMPLETED_EVENT_TYPE,
        "application_id": draft.application_id,
        "telegram_user_id": draft.telegram_user_id,
        "new_value": outcome,
        "metadata": {
            "schema_version": 1,
            "stage": stage,
            "trigger": trigger,
            "prompt_version": getattr(telemetry, "prompt_version", "unknown"),
            "prompt_hash": getattr(telemetry, "prompt_hash", None),
            "model": getattr(telemetry, "model", None),
            "blocking_rule": _llm_blocking_rule(llm_result.blocking_problem),
            "gap_code": _result_gap_code(llm_result),
            "duration_ms": getattr(telemetry, "duration_ms", None),
            "response_attempts": getattr(telemetry, "response_attempts", None),
            "validation_retries": getattr(telemetry, "validation_retries", None),
            "error_kind": getattr(telemetry, "error_kind", None),
        },
    }


def _llm_blocking_rule(blocking_problem: str | None) -> str | None:
    gap_rules = {
        "missing_new_entity_content": "1.1",
        "missing_change_content": "1.2",
        "missing_application_context": "2.1",
        "missing_change_rationale": "3.1",
    }
    if blocking_problem in gap_rules:
        return gap_rules[blocking_problem]
    match = re.search(r"правило\s+(1\.[12]|[23]\.1)", blocking_problem or "", re.IGNORECASE)
    if match and match.group(1) in {"1.1", "1.2", "2.1", "3.1"}:
        return match.group(1)
    return None


def _llm_result_to_json(llm_result) -> str:
    telemetry = getattr(llm_result, "telemetry", None)
    uses_missing_detail = getattr(telemetry, "prompt_version", None) in {
        "v6.1",
        "v6.2",
    }
    detail = _result_missing_detail(llm_result)
    result_field = (
        {"missing_detail": detail}
        if uses_missing_detail or detail is not None
        else {"recommendation": _result_recommendation(llm_result)}
    )
    return json.dumps(
        {
            "check_result": _result_check_result(llm_result),
            "gap_code": _result_gap_code(llm_result),
            **result_field,
        },
        ensure_ascii=False,
        indent=2,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_change_description(current_text: str, additional_text: str) -> str:
    saved = current_text.strip()
    addition = additional_text.strip()
    if not saved:
        return addition
    if not addition or addition == saved:
        return saved
    return f"{saved}\n{addition}"


def _llm_iteration_started(
    *,
    number: int,
    current_text: str,
    initial_text: str,
    previous_gap_code: object = None,
    previous_recommendation: object = None,
    previous_missing_detail: object = None,
) -> dict[str, object]:
    return {
        "check_id": str(uuid4()),
        "number": number,
        "started_at": _utc_now(),
        "completed_at": None,
        "input": {
            "initial_text": initial_text,
            "current_text": current_text,
            "previous_gap_code": previous_gap_code,
            "previous_recommendation": previous_recommendation,
            "previous_missing_detail": previous_missing_detail,
        },
        "response": None,
        "raw_response": None,
        "parse_status": "pending",
        "technical": None,
        "llm": None,
    }


def _complete_llm_iteration(iteration: dict[str, object], llm_result) -> None:
    telemetry = getattr(llm_result, "telemetry", None)
    is_error = _is_llm_error_result(llm_result)
    iteration.update(
        {
            "completed_at": _utc_now(),
            "response": {
                "check_result": _result_check_result(llm_result),
                "gap_code": _result_gap_code(llm_result),
                "recommendation": _result_recommendation(llm_result),
                "missing_detail": _result_missing_detail(llm_result),
            },
            "raw_response": getattr(llm_result, "raw_response", None),
            "parse_status": "error" if is_error else "valid",
            "technical": {
                "error_kind": getattr(telemetry, "error_kind", None),
                "response_attempts": getattr(telemetry, "response_attempts", None),
                "validation_retries": getattr(telemetry, "validation_retries", None),
                "duration_ms": getattr(telemetry, "duration_ms", None),
            },
            "llm": {
                "model": getattr(telemetry, "model", None),
                "prompt_version": getattr(telemetry, "prompt_version", "unknown"),
                "prompt_hash": getattr(telemetry, "prompt_hash", None),
                "generation_parameters": {
                    "temperature": 0.01,
                    "response_format": "json_schema",
                },
            },
        }
    )


def _result_check_result(llm_result) -> str:
    if _is_llm_error_result(llm_result):
        return LlmCheckResult.ERROR.value
    if llm_result.is_complete:
        return LlmCheckResult.OK.value
    return LlmCheckResult.RECOMMENDATION.value


def _result_gap_code(llm_result) -> str | None:
    value = getattr(llm_result, "gap_code", None)
    return str(value) if value else None


def _result_recommendation(llm_result) -> str | None:
    value = getattr(llm_result, "recommendation", None)
    if value:
        return str(value)
    if getattr(llm_result, "missing_detail", None):
        return None
    legacy = getattr(llm_result, "clarification_instruction", None)
    return str(legacy) if legacy else None


def _result_missing_detail(llm_result) -> str | None:
    value = getattr(llm_result, "missing_detail", None)
    return str(value) if value else None


def _result_user_recommendation(llm_result) -> str | None:
    missing_detail = _result_missing_detail(llm_result)
    if missing_detail:
        return _format_missing_detail(missing_detail)
    return _result_recommendation(llm_result)


def _response_user_recommendation(response: object) -> str:
    if isinstance(response, dict):
        missing_detail = response.get("missing_detail")
        if missing_detail:
            return _format_missing_detail(str(missing_detail))
        recommendation = response.get("recommendation")
        if recommendation:
            return str(recommendation)
    return "Может быть, тут не хватает важной информации."


def _format_missing_detail(missing_detail: str) -> str:
    return f"Может быть, тут не хватает информации о том, {missing_detail.strip()}"
