from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.formatting import extract_formatting_spans
from app.flow import ApplicationFlow
from app.keyboards import CallbackData, build_keyboard
from app.models import (
    AnswerType,
    BotResponse,
    BulkTargetKind,
    ChangeType,
    ChipAfterTextAction,
    Direction,
    FieldName,
    KeyboardKind,
    Priority,
)
from app.repository import DraftRepository

LOGGER = logging.getLogger(__name__)


STALE_CALLBACK_TEXT = "Эта кнопка устарела. Используйте последнее сообщение или /new."


class CallbackQueryAckMiddleware(BaseMiddleware):
    """Подтверждает callback и отсекает неактуальные управляющие сообщения."""

    def __init__(self, repository: DraftRepository | None = None) -> None:
        self.repository = repository

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        if self.repository is not None and not _callback_bypasses_active_message(event):
            if not await self._is_active(event):
                await self._answer(event, STALE_CALLBACK_TEXT)
                if event.message is not None:
                    try:
                        await event.message.edit_reply_markup(reply_markup=None)
                    except Exception:
                        LOGGER.warning(
                            "Could not disable stale callback keyboard: callback_id=%s data=%s",
                            event.id,
                            event.data,
                            exc_info=True,
                        )
                return None
        await self._answer(event)
        return await handler(event, data)

    async def _is_active(self, event: CallbackQuery) -> bool:
        if event.from_user is None or event.message is None or not hasattr(event.message, "chat"):
            return False
        settings = await self.repository.get_user_settings(event.from_user.id)
        return (
            settings.active_chat_id is not None
            and settings.active_message_id is not None
            and settings.active_chat_id == event.message.chat.id
            and settings.active_message_id == event.message.message_id
        )

    @staticmethod
    async def _answer(event: CallbackQuery, text: str | None = None) -> None:
        try:
            if text is None:
                await event.answer()
            else:
                await event.answer(text=text)
        except TelegramBadRequest as exc:
            if not _is_expired_callback_error(exc):
                raise
            LOGGER.warning(
                "Callback query acknowledgement expired: callback_id=%s data=%s",
                event.id,
                event.data,
            )


class ActiveMessageManager:
    """Поддерживает одну активную навигационную клавиатуру пользователя."""

    def __init__(self, repository: DraftRepository) -> None:
        self.repository = repository

    async def send(self, message: Message, response: BotResponse) -> None:
        user_id = _user_id(message)
        markup = build_keyboard(response.keyboard, response.keyboard_payload)
        await self._disable_previous(message.bot, user_id)
        sent = await message.answer(
            response.text,
            reply_markup=markup,
            parse_mode=response.parse_mode,
        )
        await self._save_or_disable(user_id, sent, markup)

    async def answer_callback(self, callback: CallbackQuery, response: BotResponse) -> None:
        if callback.message is None:
            return
        user_id = _callback_user_id(callback)
        markup = build_keyboard(response.keyboard, response.keyboard_payload)
        current_coordinates = _message_coordinates(callback.message)
        await self._disable_previous(
            callback.message.bot,
            user_id,
            except_coordinates=current_coordinates,
        )
        try:
            await callback.message.edit_text(
                response.text,
                reply_markup=markup,
                parse_mode=response.parse_mode,
            )
            await self._save_or_disable(user_id, callback.message, markup)
            return
        except Exception:
            LOGGER.warning(
                "Could not edit active callback message; sending a replacement: "
                "callback_id=%s data=%s",
                callback.id,
                callback.data,
                exc_info=True,
            )

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            LOGGER.warning(
                "Could not disable replaced callback keyboard: callback_id=%s data=%s",
                callback.id,
                callback.data,
                exc_info=True,
            )
        sent = await callback.message.answer(
            response.text,
            reply_markup=markup,
            parse_mode=response.parse_mode,
        )
        await self._save_or_disable(user_id, sent, markup)

    async def answer_notification_callback(
        self,
        callback: CallbackQuery,
        response: BotResponse,
    ) -> None:
        """Сохранить текст уведомления и отправить навигацию новым сообщением."""
        if callback.message is None:
            return
        user_id = _callback_user_id(callback)
        markup = build_keyboard(response.keyboard, response.keyboard_payload)
        await self._disable_previous(callback.message.bot, user_id)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            LOGGER.warning(
                "Could not disable notification keyboard: callback_id=%s data=%s",
                callback.id,
                callback.data,
                exc_info=True,
            )
        sent = await callback.message.answer(
            response.text,
            reply_markup=markup,
            parse_mode=response.parse_mode,
        )
        await self._save_or_disable(user_id, sent, markup)

    async def track_response_message(
        self,
        telegram_user_id: int,
        message: Any,
        markup: Any | None,
    ) -> None:
        """Сохранить уже отправленное управляющее сообщение как активное."""
        await self._save_or_disable(telegram_user_id, message, markup)

    async def _disable_previous(
        self,
        bot: Any,
        telegram_user_id: int,
        *,
        except_coordinates: tuple[int, int] | None = None,
    ) -> None:
        settings = await self.repository.get_user_settings(telegram_user_id)
        coordinates = (settings.active_chat_id, settings.active_message_id)
        if None in coordinates or coordinates == except_coordinates:
            return
        try:
            await bot.edit_message_reply_markup(
                chat_id=settings.active_chat_id,
                message_id=settings.active_message_id,
                reply_markup=None,
            )
        except Exception:
            LOGGER.warning(
                "Could not disable previous active keyboard: telegram_user_id=%s "
                "chat_id=%s message_id=%s",
                telegram_user_id,
                settings.active_chat_id,
                settings.active_message_id,
                exc_info=True,
            )

    async def _save_or_disable(
        self,
        telegram_user_id: int,
        message: Any,
        markup: Any | None,
    ) -> None:
        if markup is None:
            try:
                await self.repository.clear_active_message(telegram_user_id)
            except Exception:
                LOGGER.exception(
                    "Could not clear active message: telegram_user_id=%s",
                    telegram_user_id,
                )
            return
        coordinates = _message_coordinates(message)
        if coordinates is None:
            await self._disable_untracked_keyboard(message, telegram_user_id)
            return
        try:
            await self.repository.set_active_message(
                telegram_user_id,
                chat_id=coordinates[0],
                message_id=coordinates[1],
            )
        except Exception:
            LOGGER.exception(
                "Could not persist active message: telegram_user_id=%s chat_id=%s "
                "message_id=%s",
                telegram_user_id,
                coordinates[0],
                coordinates[1],
            )
            await self._disable_untracked_keyboard(message, telegram_user_id)

    @staticmethod
    async def _disable_untracked_keyboard(message: Any, telegram_user_id: int) -> None:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            LOGGER.warning(
                "Could not disable untracked keyboard: telegram_user_id=%s",
                telegram_user_id,
                exc_info=True,
            )


class UserActionLockMiddleware(BaseMiddleware):
    """Сериализует все пользовательские действия для одного Telegram user."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    async def __call__(
        self,
        handler: Callable[[Message | CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        if user is None:
            return await handler(event, data)
        lock = self._locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            return await handler(event, data)


class PrivateChatOnlyMiddleware(BaseMiddleware):
    """Пропускает пользовательский workflow только в личных чатах."""

    async def __call__(
        self,
        handler: Callable[[Message | CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        is_callback = hasattr(event, "message") and hasattr(event, "answer")
        message = event.message if is_callback else event
        if _is_private_chat_message(message):
            return await handler(event, data)

        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        chat_type = _chat_type_value(getattr(chat, "type", None))
        title = getattr(chat, "title", None)
        LOGGER.info(
            "Ignored non-private chat update: chat_id=%s chat_type=%s title=%s",
            chat_id,
            chat_type,
            title,
        )
        if is_callback:
            await CallbackQueryAckMiddleware._answer(
                event,
                "Бот принимает заявки только в личном чате.",
            )
        return None


def create_router(flow: ApplicationFlow) -> Router:
    """Собрать Telegram handlers и связать их с ApplicationFlow."""
    router = Router()
    ui = ActiveMessageManager(flow.repository)
    user_action_lock = UserActionLockMiddleware()
    private_chat_only = PrivateChatOnlyMiddleware()
    router.message.outer_middleware(private_chat_only)
    router.callback_query.outer_middleware(private_chat_only)
    router.callback_query.outer_middleware(CallbackQueryAckMiddleware(flow.repository))
    router.callback_query.outer_middleware(user_action_lock)
    router.message.outer_middleware(user_action_lock)

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await ui.send(message, await flow.show_start())

    @router.message(Command("new"))
    async def new_application(message: Message) -> None:
        await ui.send(
            message,
            await flow.start_new(_user_id(message), author_name=_message_author_name(message)),
        )

    @router.message(Command("cancel"))
    async def cancel_application(message: Message) -> None:
        await ui.send(message, await flow.cancel(_user_id(message)))

    @router.callback_query(F.data == CallbackData.NEW)
    async def callback_new(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.start_new(
                _callback_user_id(callback),
                author_name=_callback_author_name(callback),
            ),
        )

    @router.callback_query(F.data == CallbackData.NOTIFICATION_NEW)
    async def callback_notification_new(callback: CallbackQuery) -> None:
        await ui.answer_notification_callback(
            callback,
            await flow.resume_from_notification(
                _callback_user_id(callback),
                author_name=_callback_author_name(callback),
            ),
        )

    @router.callback_query(F.data == CallbackData.SINGLE)
    async def callback_single(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.start_single(
                _callback_user_id(callback),
                author_name=_callback_author_name(callback),
            ),
        )

    @router.callback_query(F.data == CallbackData.BULK_UPLOAD)
    async def callback_bulk_upload(callback: CallbackQuery) -> None:
        await ui.answer_callback(callback, await flow.open_bulk_menu(_callback_user_id(callback)))

    @router.callback_query(F.data == CallbackData.BULK_TEMPLATE)
    async def callback_bulk_template(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            BotResponse(
                text=(
                    "XLSX-шаблон больше не используется. "
                    "Заполняйте массовую заявку сразу в Google Sheets."
                ),
                keyboard=KeyboardKind.BULK_MENU,
            ),
        )

    @router.callback_query(F.data == CallbackData.BULK_CREATE)
    async def callback_bulk_create(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.create_bulk_batch(_callback_user_id(callback)),
        )

    @router.callback_query(F.data.startswith("app:bulk_direction:"))
    async def callback_bulk_direction(callback: CallbackQuery) -> None:
        _, _, idempotency_key, direction = (callback.data or "").split(":", maxsplit=3)
        response = await flow.select_bulk_direction(
            _callback_user_id(callback),
            idempotency_key,
            Direction(direction),
        )
        await ui.answer_callback(callback, response)

    @router.callback_query(F.data.startswith("app:bulk_target:"))
    async def callback_bulk_target(callback: CallbackQuery) -> None:
        _, _, reservation_id, target_kind = (callback.data or "").split(":", maxsplit=3)
        await ui.answer_callback(
            callback,
            await flow.select_bulk_target(
                _callback_user_id(callback),
                reservation_id,
                BulkTargetKind(target_kind),
            ),
        )

    @router.callback_query(F.data.startswith("app:bulk_change_type:"))
    async def callback_bulk_change_type(callback: CallbackQuery) -> None:
        _, _, reservation_id, change_type = (callback.data or "").split(":", maxsplit=3)
        await ui.answer_callback(
            callback,
            await flow.select_bulk_change_type(
                _callback_user_id(callback),
                reservation_id,
                ChangeType(change_type),
            ),
        )

    @router.callback_query(F.data.startswith("app:bulk_confirm:"))
    async def callback_bulk_confirm(callback: CallbackQuery) -> None:
        reservation_id = (callback.data or "").split(":", maxsplit=2)[2]
        await _show_callback_processing(
            callback,
            "Готовлю строки для массовой заявки в Google Sheets. Подождите...",
        )
        await ui.answer_callback(
            callback,
            await flow.confirm_bulk_reservation_creation(
                _callback_user_id(callback),
                reservation_id,
            ),
        )

    @router.callback_query(F.data.startswith("app:bulk_reservation_ready:"))
    async def callback_bulk_reservation_ready(callback: CallbackQuery) -> None:
        reservation_id = (callback.data or "").split(":", maxsplit=2)[2]
        await _answer_bulk_reservation_registration(
            callback,
            flow,
            ui,
            reservation_id,
        )

    @router.callback_query(F.data.startswith("app:bulk_cancel:"))
    async def callback_bulk_cancel(callback: CallbackQuery) -> None:
        reservation_id = (callback.data or "").split(":", maxsplit=2)[2]
        await ui.answer_callback(
            callback,
            await flow.cancel_bulk_reservation(_callback_user_id(callback), reservation_id),
        )

    @router.callback_query(F.data.startswith("app:bulk_ready:"))
    async def callback_bulk_ready(callback: CallbackQuery) -> None:
        batch_id = (callback.data or "").split(":", maxsplit=2)[2]
        await ui.answer_callback(
            callback,
            await flow.confirm_bulk_batch_filled(
                _callback_user_id(callback),
                batch_id,
            ),
        )

    @router.callback_query(F.data == CallbackData.DEFAULTS)
    async def callback_defaults(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.open_defaults_menu(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.SET_DEFAULT_DIRECTION)
    async def callback_set_default_direction(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.start_set_default_direction(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.SET_DEFAULT_INTENT)
    async def callback_set_default_intent(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.start_set_default_intent(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.SET_DEFAULT_SCRIPTWRITER)
    async def callback_set_default_scriptwriter(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.start_set_default_scriptwriter(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.CLEAR_DEFAULT_DIRECTION)
    async def callback_clear_default_direction(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.clear_default_direction(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.CLEAR_DEFAULT_INTENT)
    async def callback_clear_default_intent(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.clear_default_intent(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.CLEAR_DEFAULT_SCRIPTWRITER)
    async def callback_clear_default_scriptwriter(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.clear_default_scriptwriter(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.USE_DEFAULT_DIRECTION)
    async def callback_use_default_direction(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.use_default_direction(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.USE_DEFAULT_INTENT)
    async def callback_use_default_intent(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.use_default_intent(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.USE_DEFAULT_SCRIPTWRITER)
    async def callback_use_default_scriptwriter(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.use_default_scriptwriter(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.CONTINUE)
    async def callback_continue(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.continue_existing(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.RESTART)
    async def callback_restart(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.start_single(
                _callback_user_id(callback),
                force=True,
                author_name=_callback_author_name(callback),
            ),
        )

    @router.callback_query(F.data == CallbackData.CANCEL)
    async def callback_cancel(callback: CallbackQuery) -> None:
        await ui.answer_callback(callback, await flow.cancel(_callback_user_id(callback)))

    @router.callback_query(F.data == CallbackData.BACK)
    async def callback_back(callback: CallbackQuery) -> None:
        await ui.answer_callback(callback, await flow.back(_callback_user_id(callback)))

    @router.callback_query(F.data == CallbackData.LLM_ADD)
    async def callback_llm_add(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.accept_llm_recommendation(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.LLM_SKIP)
    async def callback_llm_skip(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.skip_llm_recommendation(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.LLM_SKIP_OPTIONAL)
    async def callback_llm_skip_optional(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.select_llm_skip_reason(_callback_user_id(callback), "optional"),
        )

    @router.callback_query(F.data == CallbackData.LLM_SKIP_INCORRECT)
    async def callback_llm_skip_incorrect(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.select_llm_skip_reason(_callback_user_id(callback), "incorrect"),
        )

    @router.callback_query(F.data == CallbackData.LLM_SKIP_UNCLEAR)
    async def callback_llm_skip_unclear(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.select_llm_skip_reason(_callback_user_id(callback), "unclear"),
        )

    @router.callback_query(F.data.startswith("app:direction:"))
    async def callback_direction(callback: CallbackQuery) -> None:
        direction_value = (callback.data or "").split(":", maxsplit=2)[2]
        await ui.answer_callback(
            callback,
            await flow.select_direction(_callback_user_id(callback), Direction(direction_value)),
        )

    @router.callback_query(F.data.startswith("app:answer_type:"))
    async def callback_answer_type(callback: CallbackQuery) -> None:
        answer_type_value = (callback.data or "").split(":", maxsplit=2)[2]
        await ui.answer_callback(
            callback,
            await flow.select_answer_type(
                _callback_user_id(callback),
                AnswerType(answer_type_value),
            ),
        )

    @router.callback_query(F.data.startswith("app:change_type:"))
    async def callback_change_type(callback: CallbackQuery) -> None:
        change_type_value = (callback.data or "").split(":", maxsplit=2)[2]
        await ui.answer_callback(
            callback,
            await flow.select_change_type(
                _callback_user_id(callback),
                ChangeType(change_type_value),
            ),
        )

    @router.callback_query(F.data.startswith("app:chip_after_action:"))
    async def callback_chip_after_text_action(callback: CallbackQuery) -> None:
        action_value = (callback.data or "").split(":", maxsplit=2)[2]
        await ui.answer_callback(
            callback,
            await flow.select_chip_after_text_action(
                _callback_user_id(callback),
                ChipAfterTextAction(action_value),
            ),
        )

    @router.callback_query(F.data == CallbackData.URGENCY_YES)
    async def callback_urgency_yes(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.select_urgency(_callback_user_id(callback), True),
        )

    @router.callback_query(F.data == CallbackData.URGENCY_NO)
    async def callback_urgency_no(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.select_urgency(_callback_user_id(callback), False),
        )

    @router.callback_query(F.data == CallbackData.PRIORITY_HIGH)
    async def callback_priority_high(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.select_priority(_callback_user_id(callback), Priority.HIGH),
        )

    @router.callback_query(F.data == CallbackData.PRIORITY_LOW)
    async def callback_priority_low(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.select_priority(_callback_user_id(callback), Priority.LOW),
        )

    @router.callback_query(F.data == CallbackData.SUBMIT)
    async def callback_submit(callback: CallbackQuery) -> None:
        await _show_callback_processing(
            callback,
            "Заявка отправляется в Google Sheets. Подождите...",
        )
        try:
            response = await flow.submit(_callback_user_id(callback))
        except Exception:
            LOGGER.exception(
                "Unexpected application submission failure: telegram_user_id=%s",
                _callback_user_id(callback),
            )
            response = BotResponse(
                text=(
                    "Не удалось завершить отправку заявки. "
                    "Проверьте данные и повторите попытку."
                ),
                keyboard=KeyboardKind.REVIEW,
            )
        await ui.answer_callback(callback, response)

    @router.callback_query(F.data == CallbackData.EDIT)
    async def callback_edit(callback: CallbackQuery) -> None:
        await ui.answer_callback(
            callback,
            await flow.open_edit_menu(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.REVIEW)
    async def callback_review(callback: CallbackQuery) -> None:
        await ui.answer_callback(callback, await flow.show_review(_callback_user_id(callback)))

    @router.callback_query(F.data.startswith("app:edit:"))
    async def callback_edit_field(callback: CallbackQuery) -> None:
        field_value = (callback.data or "").split(":", maxsplit=2)[2]
        await ui.answer_callback(
            callback,
            await flow.select_edit_field(_callback_user_id(callback), FieldName(field_value)),
        )

    @router.message(F.text)
    async def text_answer(message: Message) -> None:
        user_id = _user_id(message)
        await flow.remember_author(user_id, _message_author_name(message))
        if await flow.should_show_llm_processing(user_id):
            await message.answer("Проверяю полноту описания через GigaChat...")
        await ui.send(
            message,
            await flow.handle_text(
                user_id,
                message.text,
                extract_formatting_spans(message.text or "", list(message.entities or [])),
            ),
        )

    return router


async def _show_callback_processing(callback: CallbackQuery, text: str) -> None:
    """Заменить клавиатуру индикатором выполнения долгой операции."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=None)
    except Exception:
        LOGGER.exception(
            "Could not show callback processing state: callback_id=%s data=%s",
            callback.id,
            callback.data,
        )
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(text)
        except Exception:
            LOGGER.exception(
                "Could not remove callback buttons: callback_id=%s data=%s",
                callback.id,
                callback.data,
            )




async def _answer_bulk_reservation_registration(
    callback: CallbackQuery,
    flow: ApplicationFlow,
    ui: ActiveMessageManager,
    reservation_id: str,
) -> None:
    """Register filled rows from the new bulk reservation without editing the link message."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        LOGGER.exception(
            "Could not disable bulk reservation button: reservation_id=%s",
            reservation_id,
        )
    progress_message = await callback.message.answer(
        "Массовая заявка регистрируется в Google Sheets. Подождите..."
    )
    try:
        response = await flow.confirm_bulk_reservation_filled(
            _callback_user_id(callback),
            reservation_id,
        )
    except Exception:
        LOGGER.exception(
            "Unexpected bulk reservation registration failure: reservation_id=%s telegram_user_id=%s",
            reservation_id,
            _callback_user_id(callback),
        )
        response = BotResponse(
            text=(
                "Не удалось завершить регистрацию массовой заявки. "
                "Исходный диапазон доступен в сообщении выше. Повторите попытку."
            ),
            keyboard=KeyboardKind.BULK_RESERVATION_CREATED,
            keyboard_payload=reservation_id,
        )
    markup = build_keyboard(response.keyboard, response.keyboard_payload)
    registration_completed = response.keyboard == KeyboardKind.BULK_RESERVATION_COMPLETED
    response_markup = None if registration_completed else markup
    if progress_message is not None:
        try:
            await progress_message.edit_text(
                response.text,
                reply_markup=response_markup,
                parse_mode=response.parse_mode,
            )
            if registration_completed:
                await _send_bulk_registration_navigation(
                    callback.message,
                    ui,
                    _callback_user_id(callback),
                )
            else:
                await ui.track_response_message(
                    _callback_user_id(callback),
                    progress_message,
                    markup,
                )
            return
        except Exception:
            LOGGER.exception(
                "Could not edit bulk reservation progress: reservation_id=%s",
                reservation_id,
            )
    sent = await callback.message.answer(
        response.text,
        reply_markup=response_markup,
        parse_mode=response.parse_mode,
    )
    if registration_completed:
        await _send_bulk_registration_navigation(
            callback.message,
            ui,
            _callback_user_id(callback),
        )
    else:
        await ui.track_response_message(
            _callback_user_id(callback),
            sent,
            markup,
        )


async def _send_bulk_registration_navigation(
    message: Message,
    ui: ActiveMessageManager,
    telegram_user_id: int,
) -> None:
    """Keep registration details intact and track navigation separately."""
    markup = build_keyboard(KeyboardKind.BULK_RESERVATION_COMPLETED)
    sent = await message.answer(
        "Можно перейти в главное меню или продолжить работу позже.",
        reply_markup=markup,
    )
    await ui.track_response_message(telegram_user_id, sent, markup)


def _user_id(message: Message) -> int:
    if message.from_user is None:
        raise ValueError("Telegram message has no from_user")
    return message.from_user.id


def _message_author_name(message: Message) -> str | None:
    if message.from_user is None:
        return None
    return message.from_user.full_name


def _callback_user_id(callback: CallbackQuery) -> int:
    if callback.from_user is None:
        raise ValueError("Telegram callback has no from_user")
    return callback.from_user.id


def _callback_author_name(callback: CallbackQuery) -> str:
    return callback.from_user.full_name


def _callback_bypasses_active_message(callback: CallbackQuery) -> bool:
    data = callback.data or ""
    return (
        data.startswith("app:bulk_ready:")
        or data.startswith("app:bulk_reservation_ready:")
        or data == CallbackData.NOTIFICATION_NEW
    )


def _message_coordinates(message: Any) -> tuple[int, int] | None:
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return None
    return int(chat_id), int(message_id)


def _is_private_chat_message(message: Any) -> bool:
    chat = getattr(message, "chat", None)
    return _chat_type_value(getattr(chat, "type", None)) == "private"


def _chat_type_value(chat_type: Any) -> str:
    value = getattr(chat_type, "value", chat_type)
    return str(value or "")


def _is_expired_callback_error(exc: TelegramBadRequest) -> bool:
    message = str(exc).lower()
    return "query is too old" in message or "query id is invalid" in message
