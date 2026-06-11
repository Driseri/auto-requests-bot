from __future__ import annotations

import asyncio
import json
from html import escape

from app.formatting import (
    TextFormattingSpan,
    deserialize_formatting_spans,
    render_html_with_formatting,
    serialize_formatting_spans,
)
from app.bulk import BulkApplicationRegistrar, BulkBatchServiceProtocol
from app.llm import LlmClient
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BotResponse,
    ChangeType,
    Direction,
    Draft,
    FieldName,
    KeyboardKind,
    LlmCheckStatus,
    LlmContext,
    Priority,
    Step,
    SubmissionState,
    direction_display_label,
    direction_value_from_display_label,
)
from app.repository import DraftRepository
from app.submission import SubmissionServiceProtocol


FIELD_LABELS = {
    FieldName.DIRECTION: "Направление",
    FieldName.ANSWER_TYPE: "Тип ответа",
    FieldName.CHANGE_TYPE: "Тип изменения",
    FieldName.INTENT: "Интент",
    FieldName.SCRIPTWRITER: "Закрепленный сценарист",
    FieldName.REASON: "Причина изменений",
    FieldName.CHANGE_DESCRIPTION: "Суть изменений",
    FieldName.SOURCE_TEXT: "Исходный текст",
    FieldName.URGENCY: "Срочная",
    FieldName.PRIORITY: "Приоритет",
}

PENDING_SET_DEFAULT_INTENT = "set_default_intent"
PENDING_SET_DEFAULT_SCRIPTWRITER = "set_default_scriptwriter"
PENDING_SET_DEFAULT_DIRECTION = "set_default_direction"
PENDING_CREATE_BULK_DIRECTION = "create_bulk_direction"

STEP_PROMPTS = {
    Step.DIRECTION: "Выберите направление заявки.",
    Step.ANSWER_TYPE: "Выберите тип ответа.",
    Step.CHANGE_TYPE: "Выберите тип изменения для раскатки.",
    Step.INTENT: "В каком интенте необходимо внести изменения?",
    Step.SCRIPTWRITER: "За каким сценаристом закреплен интент?",
    Step.REASON: "Опишите причину изменений.",
    Step.CHANGE_DESCRIPTION: "В чем суть изменений?",
    Step.CHANGE_DESCRIPTION_CLARIFICATION: "Введите дополнение к сути изменений.",
    Step.SOURCE_TEXT: "Пришлите исходный текст ответа чат-бота.",
    Step.URGENCY: "Заявка срочная?",
    Step.PRIORITY: "Выберите приоритет заявки.",
    Step.EDIT_INTENT: "Введите новый интент.",
    Step.EDIT_CHANGE_TYPE: "Выберите новый тип изменения для раскатки.",
    Step.EDIT_SCRIPTWRITER: "Введите нового закрепленного сценариста.",
    Step.EDIT_REASON: "Введите новую причину изменений.",
    Step.EDIT_CHANGE_DESCRIPTION: "Введите новую суть изменений.",
    Step.EDIT_SOURCE_TEXT: "Введите новый исходный текст.",
    Step.EDIT_URGENCY: "Выберите новый признак срочности.",
    Step.EDIT_PRIORITY: "Выберите новый приоритет заявки.",
}


class ApplicationFlow:
    def __init__(
        self,
        repository: DraftRepository,
        llm_client: LlmClient,
        submission_service: SubmissionServiceProtocol,
        bulk_service: BulkBatchServiceProtocol | None = None,
        bulk_registrar: BulkApplicationRegistrar | None = None,
        show_llm_response_json: bool = False,
    ) -> None:
        self.repository = repository
        self.llm_client = llm_client
        self.submission_service = submission_service
        self.bulk_service = bulk_service
        self.bulk_registrar = bulk_registrar
        self.show_llm_response_json = show_llm_response_json
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

    async def start_single(
        self,
        telegram_user_id: int,
        *,
        force: bool = False,
        author_name: str | None = None,
    ) -> BotResponse:
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
            await self.repository.delete(telegram_user_id)

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
            draft.answer_type == AnswerType.ROLLOUT.value
            and ChangeType.normalize(draft.change_type) is None
            and draft.current_step not in {Step.ANSWER_TYPE, Step.EDIT_ANSWER_TYPE}
        ):
            draft = await self.repository.set_step(telegram_user_id, Step.CHANGE_TYPE)
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
        await self.repository.delete(telegram_user_id)
        return BotResponse(
            text="Заявка отменена. Чтобы создать новую, отправьте /new.",
            keyboard=KeyboardKind.CREATE_MODE,
        )

    async def bulk_upload_stub(self, telegram_user_id: int) -> BotResponse:
        return await self.open_bulk_menu(telegram_user_id)

    async def open_bulk_menu(self, telegram_user_id: int) -> BotResponse:
        return BotResponse(
            text=(
                "Массовая загрузка работает через отдельный лист Google Sheets.\n\n"
                "1. Создайте массовую заявку.\n"
                "2. Заполните строки сразу в Google Sheets.\n"
                "3. Вернитесь в бот и нажмите «Заявка заполнена»."
            ),
            keyboard=KeyboardKind.BULK_MENU,
        )

    async def create_bulk_batch(self, telegram_user_id: int) -> BotResponse:
        if self.bulk_service is None:
            return BotResponse(
                text="Массовая загрузка не настроена.",
                keyboard=KeyboardKind.BULK_MENU,
            )
        await self.repository.save_user_setting(
            telegram_user_id,
            "pending_action",
            PENDING_CREATE_BULK_DIRECTION,
        )
        settings = await self.repository.get_user_settings(telegram_user_id)
        return BotResponse(
            text="Выберите направление массовой заявки.",
            keyboard=(
                KeyboardKind.DIRECTION_WITH_DEFAULT
                if settings.default_direction
                else KeyboardKind.DIRECTION
            ),
        )

    async def _create_bulk_batch_for_direction(
        self,
        telegram_user_id: int,
        direction: str,
    ) -> BotResponse:
        if self.bulk_service is None:
            return BotResponse(text="Массовая загрузка не настроена.", keyboard=KeyboardKind.BULK_MENU)
        await self.repository.save_user_setting(telegram_user_id, "pending_action", None)
        result = await self.bulk_service.create_batch(telegram_user_id, direction)
        if not result.success:
            return BotResponse(text=result.message, keyboard=KeyboardKind.BULK_MENU)
        batch_id = result.batch.batch_id if result.batch else "-"
        insert_url = result.insert_url or ""
        return BotResponse(
            text=(
                f"Создана массовая заявка <b>{escape(batch_id)}</b>.\n\n"
                "Заполните строки в колонках A:G, начиная с первой строки по ссылке. "
                "Можно вставить любое количество строк вниз.\n"
                "После заполнения вернитесь сюда и нажмите «Заявка заполнена», чтобы бот выдал ID заявок.\n\n"
                f'<a href="{escape(insert_url, quote=True)}">Открыть место для вставки</a>'
            ),
            keyboard=KeyboardKind.BULK_CREATED,
            keyboard_payload=batch_id if batch_id != "-" else None,
            parse_mode="HTML",
        )

    async def confirm_bulk_batch_filled(
        self,
        telegram_user_id: int,
        batch_id: str,
    ) -> BotResponse:
        if self.bulk_registrar is None:
            return BotResponse(
                text="Регистрация массовой заявки не настроена.",
                keyboard=KeyboardKind.BULK_CREATED,
                keyboard_payload=batch_id,
            )
        result = await self.bulk_registrar.register_batch(batch_id, telegram_user_id)
        if not result.success:
            if not result.retry_allowed:
                return BotResponse(
                    text=result.message,
                    keyboard=KeyboardKind.BULK_MENU,
                )
            return BotResponse(
                text=result.message,
                keyboard=KeyboardKind.BULK_CREATED,
                keyboard_payload=batch_id,
            )
        return BotResponse(
            text=result.message,
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
        if settings.pending_action == PENDING_CREATE_BULK_DIRECTION:
            return await self._create_bulk_batch_for_direction(
                telegram_user_id,
                settings.default_direction,
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
        draft = await self.repository.set_step(telegram_user_id, Step.SCRIPTWRITER)
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
        draft = await self.repository.set_step(telegram_user_id, Step.REASON)
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
        pending_response = await self._handle_pending_settings_text(telegram_user_id, text)
        if pending_response is not None:
            return pending_response

        draft = await self._get_active_or_start(telegram_user_id)
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
                draft = await self.repository.set_step(telegram_user_id, Step.SCRIPTWRITER)
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.SCRIPTWRITER:
                await self.repository.save_answer(
                    telegram_user_id,
                    FieldName.SCRIPTWRITER.value,
                    value,
                )
                draft = await self.repository.set_step(telegram_user_id, Step.REASON)
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.REASON:
                await self.repository.save_answer(telegram_user_id, FieldName.REASON.value, value)
                draft = await self.repository.set_step(telegram_user_id, Step.CHANGE_DESCRIPTION)
                return await self._prompt_response_for_user(telegram_user_id, draft)
            case Step.CHANGE_DESCRIPTION:
                return await self._process_change_description(telegram_user_id, value)
            case Step.CHANGE_DESCRIPTION_CLARIFICATION:
                return await self._process_change_clarification(telegram_user_id, value)
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
                response = await self._process_change_description(
                    telegram_user_id,
                    value,
                    edit_mode=True,
                )
                if response.draft and response.draft.current_step == Step.SOURCE_TEXT:
                    return await self._return_to_review(
                        telegram_user_id,
                        prefix="Суть изменений обновлена через GigaChat.",
                    )
                return response
            case Step.EDIT_SOURCE_TEXT:
                await self._save_source_text(telegram_user_id, value, formatting_spans)
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
        return draft is not None and draft.current_step in {
            Step.CHANGE_DESCRIPTION,
            Step.CHANGE_DESCRIPTION_CLARIFICATION,
            Step.EDIT_CHANGE_DESCRIPTION,
        }

    async def select_direction(self, telegram_user_id: int, direction: Direction) -> BotResponse:
        settings = await self.repository.get_user_settings(telegram_user_id)
        if settings.pending_action == PENDING_CREATE_BULK_DIRECTION:
            return await self._create_bulk_batch_for_direction(telegram_user_id, direction.value)

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
        if answer_type != AnswerType.ROLLOUT:
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
        if answer_type == AnswerType.ROLLOUT:
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
        if draft.answer_type != AnswerType.ROLLOUT.value:
            await self.repository.save_answer(
                telegram_user_id,
                FieldName.CHANGE_TYPE.value,
                "",
            )
            return await self._prompt_response_for_user(
                telegram_user_id,
                draft,
                prefix="Тип изменения выбирается только для раскатки.",
            )

        await self.repository.save_answer(
            telegram_user_id,
            FieldName.CHANGE_TYPE.value,
            change_type.value,
        )
        next_step = Step.REVIEW if draft.current_step == Step.EDIT_CHANGE_TYPE else Step.INTENT
        draft = await self.repository.set_step(telegram_user_id, next_step)
        if next_step == Step.REVIEW:
            return self._review_response(draft, prefix="Тип изменения обновлен.")
        return await self._prompt_response_for_user(telegram_user_id, draft)

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
        if (
            draft.current_step == Step.INTENT
            and draft.answer_type == AnswerType.ROLLOUT.value
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
                KeyboardKind.EDIT_MENU_ROLLOUT
                if draft.answer_type == AnswerType.ROLLOUT.value
                else KeyboardKind.EDIT_MENU
            ),
            draft=draft,
        )

    async def select_edit_field(self, telegram_user_id: int, field: FieldName) -> BotResponse:
        draft = await self._get_active_or_start(telegram_user_id)
        if field == FieldName.URGENCY and _direction_requires_answer_type(draft.direction):
            return self._review_response(
                draft,
                prefix="Срочность для этого направления определяется полем «Тип ответа». Чтобы изменить срочность, отредактируйте тип ответа.",
            )
        step_by_field = {
            FieldName.DIRECTION: Step.EDIT_DIRECTION,
            FieldName.ANSWER_TYPE: Step.EDIT_ANSWER_TYPE,
            FieldName.CHANGE_TYPE: Step.EDIT_CHANGE_TYPE,
            FieldName.INTENT: Step.EDIT_INTENT,
            FieldName.SCRIPTWRITER: Step.EDIT_SCRIPTWRITER,
            FieldName.REASON: Step.EDIT_REASON,
            FieldName.CHANGE_DESCRIPTION: Step.EDIT_CHANGE_DESCRIPTION,
            FieldName.SOURCE_TEXT: Step.EDIT_SOURCE_TEXT,
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
                is_urgent=draft.is_urgent,
            )
        return BotResponse(
            text=f"{result.message}\n\nЧтобы создать новую заявку, отправьте /new.",
            keyboard=KeyboardKind.CREATE_MODE,
            draft=draft,
        )

    async def _process_change_description(
        self,
        telegram_user_id: int,
        value: str,
        *,
        edit_mode: bool = False,
    ) -> BotResponse:
        draft = await self.repository.save_answer(
            telegram_user_id,
            "raw_change_description",
            value,
        )
        llm_result = await self.llm_client.check_change_description(
            LlmContext(
                intent=draft.intent or "",
                scriptwriter=draft.scriptwriter or "",
                reason=draft.reason or "",
                raw_change_description=value,
            )
        )
        status = self._status_from_llm_result(llm_result)
        await self.repository.save_llm_result(
            telegram_user_id,
            formatted_change_description=value,
            llm_check_status=status,
            llm_score=None,
            clarification_count=0,
        )

        if llm_result.is_complete or status == LlmCheckStatus.ERROR.value:
            next_step = Step.REVIEW if edit_mode else Step.SOURCE_TEXT
            draft = await self.repository.set_step(telegram_user_id, next_step)
            prefix = self._with_llm_json(
                self._llm_success_prefix(status),
                llm_result,
            )
            if edit_mode:
                return self._review_response(draft, prefix=prefix)
            return await self._prompt_response_for_user(telegram_user_id, draft, prefix=prefix)

        draft = await self.repository.set_step(
            telegram_user_id,
            Step.CHANGE_DESCRIPTION_CLARIFICATION,
        )
        return await self._prompt_response_for_user(
            telegram_user_id,
            draft,
            prefix=self._with_llm_json(
                self._clarification_prefix(llm_result),
                llm_result,
            ),
        )

    async def _process_change_clarification(
        self,
        telegram_user_id: int,
        clarification: str,
    ) -> BotResponse:
        draft = await self.repository.get_by_user_id(telegram_user_id)
        if draft is None:
            return await self.start_new(telegram_user_id, force=True)

        original_description = draft.raw_change_description or ""
        combined_description = (
            f"{original_description}\n\n"
            f"Уточнение сценариста: {clarification}"
        ).strip()
        llm_result = await self.llm_client.check_change_description(
            LlmContext(
                intent=draft.intent or "",
                scriptwriter=draft.scriptwriter or "",
                reason=draft.reason or "",
                raw_change_description=original_description,
                clarification_text=clarification,
            )
        )
        status = self._status_from_llm_result(llm_result, after_clarification=True)
        await self.repository.save_llm_result(
            telegram_user_id,
            formatted_change_description=combined_description,
            llm_check_status=status,
            llm_score=None,
            clarification_count=1,
        )
        next_step = Step.REVIEW if draft.source_text and draft.is_urgent is not None else Step.SOURCE_TEXT
        draft = await self.repository.set_step(telegram_user_id, next_step)
        prefix = self._llm_success_prefix(status)
        if status == LlmCheckStatus.NEEDS_ATTENTION.value:
            prefix = (
                "Описание все еще выглядит неполным. "
                "Заявка продолжит заполняться и уйдет редактору с пометкой, "
                "что нужно дополнительное внимание."
            )
        prefix = self._with_llm_json(prefix, llm_result)
        if next_step == Step.REVIEW:
            return self._review_response(draft, prefix=prefix)
        return await self._prompt_response_for_user(telegram_user_id, draft, prefix=prefix)

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
                "Не удалось проверить суть изменений через GigaChat. "
                "Продолжаем с исходным текстом и пометкой для редактора."
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

    async def _handle_pending_settings_text(
        self,
        telegram_user_id: int,
        text: str | None,
    ) -> BotResponse | None:
        pending_action = await self.repository.get_pending_settings_action(telegram_user_id)
        if pending_action is None:
            return None

        value = (text or "").strip()
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

    def _prompt_response(self, draft: Draft, *, prefix: str | None = None) -> BotResponse:
        text = STEP_PROMPTS[draft.current_step]
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
        if step in {Step.URGENCY, Step.EDIT_URGENCY}:
            return KeyboardKind.URGENCY
        if step in {Step.PRIORITY, Step.EDIT_PRIORITY}:
            return KeyboardKind.PRIORITY
        if step == Step.REVIEW:
            return KeyboardKind.REVIEW
        if step == Step.COMPLETED:
            return KeyboardKind.START
        return KeyboardKind.STEP

    @staticmethod
    def _render_review(draft: Draft) -> str:
        change_type_line = (
            f"🔀 <b>Тип изменения:</b> {_html_value(draft.change_type)}\n"
            if draft.answer_type == AnswerType.ROLLOUT.value
            else ""
        )
        return (
            "Проверьте заявку перед отправкой.\n\n"
            f"🧭 <b>Направление:</b> {_html_value(direction_display_label(draft.direction))}\n"
            f"🏷 <b>Тип ответа:</b> {_html_value(draft.answer_type or 'Не требуется')}\n"
            f"{change_type_line}"
            f"🎯 <b>Интент:</b> {_html_value(draft.intent)}\n"
            f"👤 <b>Закрепленный сценарист:</b> {_html_value(draft.scriptwriter)}\n"
            f"📝 <b>Причина изменений:</b> {_html_value(draft.reason)}\n"
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
            draft.answer_type == AnswerType.ROLLOUT.value
            and ChangeType.normalize(draft.change_type) is None
        ):
            missing.append(FIELD_LABELS[FieldName.CHANGE_TYPE])
        if not draft.intent:
            missing.append(FIELD_LABELS[FieldName.INTENT])
        if not draft.scriptwriter:
            missing.append(FIELD_LABELS[FieldName.SCRIPTWRITER])
        if not draft.reason:
            missing.append(FIELD_LABELS[FieldName.REASON])
        if not draft.formatted_change_description and not draft.raw_change_description:
            missing.append(FIELD_LABELS[FieldName.CHANGE_DESCRIPTION])
        if not draft.source_text:
            missing.append(FIELD_LABELS[FieldName.SOURCE_TEXT])
        if draft.is_urgent is None:
            missing.append(FIELD_LABELS[FieldName.URGENCY])
        return missing


def _html_value(value: str | None) -> str:
    return escape(value or "-")


def _direction_requires_answer_type(direction: str | None) -> bool:
    return direction in {Direction.FL.value, Direction.SME.value, Direction.AI.value}


def _is_llm_error_result(llm_result) -> bool:
    problem = llm_result.blocking_problem or ""
    return (
        problem.startswith("Ошибка GigaChat:")
        or problem == "GIGACHAT_CREDENTIALS не задан."
    )


def _llm_result_to_json(llm_result) -> str:
    return json.dumps(
        {
            "is_complete": llm_result.is_complete,
            "blocking_problem": llm_result.blocking_problem,
            "clarification_instruction": llm_result.clarification_instruction,
        },
        ensure_ascii=False,
        indent=2,
    )
