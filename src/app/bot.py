from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.formatting import extract_formatting_spans
from app.flow import ApplicationFlow
from app.keyboards import CallbackData, build_keyboard
from app.models import (
    AnswerType,
    BotResponse,
    ChangeType,
    Direction,
    FieldName,
    KeyboardKind,
    Priority,
)


def create_router(flow: ApplicationFlow) -> Router:
    router = Router()

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await _send_message(message, await flow.show_start())

    @router.message(Command("new"))
    async def new_application(message: Message) -> None:
        await _send_message(
            message,
            await flow.start_new(_user_id(message), author_name=_message_author_name(message)),
        )

    @router.message(Command("cancel"))
    async def cancel_application(message: Message) -> None:
        await _send_message(message, await flow.cancel(_user_id(message)))

    @router.callback_query(F.data == CallbackData.NEW)
    async def callback_new(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.start_new(
                _callback_user_id(callback),
                author_name=_callback_author_name(callback),
            ),
        )

    @router.callback_query(F.data == CallbackData.SINGLE)
    async def callback_single(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.start_single(
                _callback_user_id(callback),
                author_name=_callback_author_name(callback),
            ),
        )

    @router.callback_query(F.data == CallbackData.BULK_UPLOAD)
    async def callback_bulk_upload(callback: CallbackQuery) -> None:
        await _answer_callback(callback, await flow.open_bulk_menu(_callback_user_id(callback)))

    @router.callback_query(F.data == CallbackData.BULK_TEMPLATE)
    async def callback_bulk_template(callback: CallbackQuery) -> None:
        await _answer_callback(
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
        await _answer_callback(callback, await flow.create_bulk_batch(_callback_user_id(callback)))

    @router.callback_query(F.data.startswith("app:bulk_ready:"))
    async def callback_bulk_ready(callback: CallbackQuery) -> None:
        batch_id = (callback.data or "").split(":", maxsplit=2)[2]
        await _answer_callback(
            callback,
            await flow.confirm_bulk_batch_filled(_callback_user_id(callback), batch_id),
        )

    @router.callback_query(F.data == CallbackData.DEFAULTS)
    async def callback_defaults(callback: CallbackQuery) -> None:
        await _answer_callback(callback, await flow.open_defaults_menu(_callback_user_id(callback)))

    @router.callback_query(F.data == CallbackData.SET_DEFAULT_DIRECTION)
    async def callback_set_default_direction(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.start_set_default_direction(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.SET_DEFAULT_INTENT)
    async def callback_set_default_intent(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.start_set_default_intent(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.SET_DEFAULT_SCRIPTWRITER)
    async def callback_set_default_scriptwriter(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.start_set_default_scriptwriter(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.CLEAR_DEFAULT_DIRECTION)
    async def callback_clear_default_direction(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.clear_default_direction(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.CLEAR_DEFAULT_INTENT)
    async def callback_clear_default_intent(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.clear_default_intent(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.CLEAR_DEFAULT_SCRIPTWRITER)
    async def callback_clear_default_scriptwriter(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.clear_default_scriptwriter(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.USE_DEFAULT_DIRECTION)
    async def callback_use_default_direction(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.use_default_direction(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.USE_DEFAULT_INTENT)
    async def callback_use_default_intent(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.use_default_intent(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.USE_DEFAULT_SCRIPTWRITER)
    async def callback_use_default_scriptwriter(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.use_default_scriptwriter(_callback_user_id(callback)),
        )

    @router.callback_query(F.data == CallbackData.CONTINUE)
    async def callback_continue(callback: CallbackQuery) -> None:
        await _answer_callback(callback, await flow.continue_existing(_callback_user_id(callback)))

    @router.callback_query(F.data == CallbackData.RESTART)
    async def callback_restart(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.start_single(
                _callback_user_id(callback),
                force=True,
                author_name=_callback_author_name(callback),
            ),
        )

    @router.callback_query(F.data == CallbackData.CANCEL)
    async def callback_cancel(callback: CallbackQuery) -> None:
        await _answer_callback(callback, await flow.cancel(_callback_user_id(callback)))

    @router.callback_query(F.data == CallbackData.BACK)
    async def callback_back(callback: CallbackQuery) -> None:
        await _answer_callback(callback, await flow.back(_callback_user_id(callback)))

    @router.callback_query(F.data.startswith("app:direction:"))
    async def callback_direction(callback: CallbackQuery) -> None:
        direction_value = (callback.data or "").split(":", maxsplit=2)[2]
        await _answer_callback(
            callback,
            await flow.select_direction(_callback_user_id(callback), Direction(direction_value)),
        )

    @router.callback_query(F.data.startswith("app:answer_type:"))
    async def callback_answer_type(callback: CallbackQuery) -> None:
        answer_type_value = (callback.data or "").split(":", maxsplit=2)[2]
        await _answer_callback(
            callback,
            await flow.select_answer_type(
                _callback_user_id(callback),
                AnswerType(answer_type_value),
            ),
        )

    @router.callback_query(F.data.startswith("app:change_type:"))
    async def callback_change_type(callback: CallbackQuery) -> None:
        change_type_value = (callback.data or "").split(":", maxsplit=2)[2]
        await _answer_callback(
            callback,
            await flow.select_change_type(
                _callback_user_id(callback),
                ChangeType(change_type_value),
            ),
        )

    @router.callback_query(F.data == CallbackData.URGENCY_YES)
    async def callback_urgency_yes(callback: CallbackQuery) -> None:
        await _answer_callback(callback, await flow.select_urgency(_callback_user_id(callback), True))

    @router.callback_query(F.data == CallbackData.URGENCY_NO)
    async def callback_urgency_no(callback: CallbackQuery) -> None:
        await _answer_callback(callback, await flow.select_urgency(_callback_user_id(callback), False))

    @router.callback_query(F.data == CallbackData.PRIORITY_HIGH)
    async def callback_priority_high(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.select_priority(_callback_user_id(callback), Priority.HIGH),
        )

    @router.callback_query(F.data == CallbackData.PRIORITY_LOW)
    async def callback_priority_low(callback: CallbackQuery) -> None:
        await _answer_callback(
            callback,
            await flow.select_priority(_callback_user_id(callback), Priority.LOW),
        )

    @router.callback_query(F.data == CallbackData.SUBMIT)
    async def callback_submit(callback: CallbackQuery) -> None:
        await _answer_callback(callback, await flow.submit(_callback_user_id(callback)))

    @router.callback_query(F.data == CallbackData.EDIT)
    async def callback_edit(callback: CallbackQuery) -> None:
        await _answer_callback(callback, await flow.open_edit_menu(_callback_user_id(callback)))

    @router.callback_query(F.data == CallbackData.REVIEW)
    async def callback_review(callback: CallbackQuery) -> None:
        await _answer_callback(callback, await flow.show_review(_callback_user_id(callback)))

    @router.callback_query(F.data.startswith("app:edit:"))
    async def callback_edit_field(callback: CallbackQuery) -> None:
        field_value = (callback.data or "").split(":", maxsplit=2)[2]
        await _answer_callback(
            callback,
            await flow.select_edit_field(_callback_user_id(callback), FieldName(field_value)),
        )

    @router.message(F.text)
    async def text_answer(message: Message) -> None:
        user_id = _user_id(message)
        await flow.remember_author(user_id, _message_author_name(message))
        if await flow.should_show_llm_processing(user_id):
            await message.answer("Обрабатываю описание через GigaChat...")
        await _send_message(
            message,
            await flow.handle_text(
                user_id,
                message.text,
                extract_formatting_spans(message.text or "", list(message.entities or [])),
            ),
        )

    return router


async def _send_message(message: Message, response: BotResponse) -> None:
    await message.answer(
        response.text,
        reply_markup=build_keyboard(response.keyboard, response.keyboard_payload),
        parse_mode=response.parse_mode,
    )


async def _answer_callback(callback: CallbackQuery, response: BotResponse) -> None:
    await callback.answer()
    markup = build_keyboard(response.keyboard, response.keyboard_payload)
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            response.text,
            reply_markup=markup,
            parse_mode=response.parse_mode,
        )
    except Exception:
        await callback.message.answer(
            response.text,
            reply_markup=markup,
            parse_mode=response.parse_mode,
        )


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
