from __future__ import annotations
import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery

from app.bot import (
    STALE_CALLBACK_TEXT,
    ActiveMessageManager,
    CallbackQueryAckMiddleware,
    PrivateChatOnlyMiddleware,
    UserActionLockMiddleware,
    _answer_bulk_reservation_registration,
    _show_callback_processing,
)
from app.models import BotResponse, KeyboardKind
from app.repository import DraftRepository


class FakeCallback:
    def __init__(
        self,
        *,
        answer_error: Exception | None = None,
        message=None,
        data: str = "app:bulk_ready:BATCH-1234",
        user_id: int = 100,
    ) -> None:
        self.id = "callback-id"
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)
        self.answer_error = answer_error
        self.answer_calls = 0
        self.answer_texts: list[str | None] = []

    async def answer(self, text=None) -> None:
        self.answer_calls += 1
        self.answer_texts.append(text)
        if self.answer_error is not None:
            raise self.answer_error


class FakeMessage:
    def __init__(self, *, edit_error: Exception | None = None) -> None:
        self.edit_error = edit_error
        self.edits = []
        self.markup_edits = []
        self.answers = []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(
            {
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            }
        )

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.answers.append(
            {
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            }
        )


class FakeBot:
    def __init__(self) -> None:
        self.markup_edits: list[dict] = []

    async def edit_message_reply_markup(self, **kwargs):
        self.markup_edits.append(kwargs)


@pytest.mark.asyncio
async def test_bulk_registration_confirmation_remains_separate_from_navigation() -> None:
    class ProgressMessage:
        def __init__(self) -> None:
            self.edits = []

        async def edit_text(self, text, reply_markup=None, parse_mode=None):
            self.edits.append((text, reply_markup, parse_mode))

    class CallbackMessage:
        def __init__(self) -> None:
            self.progress = ProgressMessage()
            self.answers = []
            self.markup_edits = []

        async def edit_reply_markup(self, reply_markup=None):
            self.markup_edits.append(reply_markup)

        async def answer(self, text, reply_markup=None, parse_mode=None):
            self.answers.append((text, reply_markup, parse_mode))
            if len(self.answers) == 1:
                return self.progress
            return SimpleNamespace(chat=SimpleNamespace(id=100), message_id=77)

    class Flow:
        async def confirm_bulk_reservation_filled(self, telegram_user_id, reservation_id):
            assert telegram_user_id == 100
            assert reservation_id == "RES-1"
            return BotResponse(
                text="Регистрация завершена: APP00001",
                keyboard=KeyboardKind.BULK_RESERVATION_COMPLETED,
                parse_mode="HTML",
            )

    class Ui:
        def __init__(self) -> None:
            self.tracked = []

        async def track_response_message(self, telegram_user_id, message, markup):
            self.tracked.append((telegram_user_id, message, markup))

    message = CallbackMessage()
    callback = FakeCallback(message=message, data="app:bulk_reservation_ready:RES-1")
    ui = Ui()

    await _answer_bulk_reservation_registration(callback, Flow(), ui, "RES-1")

    assert message.progress.edits == [("Регистрация завершена: APP00001", None, "HTML")]
    assert len(message.answers) == 2
    assert message.answers[1][0] == "Можно перейти в главное меню или продолжить работу позже."
    assert message.answers[1][1] is not None
    assert len(ui.tracked) == 1
    assert ui.tracked[0][0] == 100
    assert ui.tracked[0][1].message_id == 77


class UiMessage:
    def __init__(
        self,
        *,
        message_id: int,
        chat_id: int = 100,
        user_id: int = 100,
        bot: FakeBot | None = None,
        edit_error: Exception | None = None,
        markup_edit_error: Exception | None = None,
        answer_message_id: int | None = None,
        chat_type: str = "private",
        chat_title: str | None = None,
    ) -> None:
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id, type=chat_type, title=chat_title)
        self.from_user = SimpleNamespace(id=user_id, full_name="Test User")
        self.bot = bot or FakeBot()
        self.edit_error = edit_error
        self.markup_edit_error = markup_edit_error
        self.answer_message_id = answer_message_id
        self.edits: list[dict] = []
        self.markup_edits: list[object] = []
        self.answers: list[dict] = []
        self.last_answer: UiMessage | None = None

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(
            {"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode}
        )

    async def edit_reply_markup(self, reply_markup=None):
        if self.markup_edit_error is not None:
            raise self.markup_edit_error
        self.markup_edits.append(reply_markup)

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.answers.append(
            {"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode}
        )
        message_id = self.answer_message_id or self.message_id + 1
        self.last_answer = UiMessage(
            message_id=message_id,
            chat_id=self.chat.id,
            user_id=self.from_user.id,
            bot=self.bot,
        )
        return self.last_answer


@pytest.mark.asyncio
async def test_callback_ack_happens_before_handler() -> None:
    callback = FakeCallback()
    events: list[str] = []

    async def handler(event, data):
        events.append("handler")
        return "ok"

    original_answer = callback.answer

    async def tracked_answer():
        events.append("answer")
        await original_answer()

    callback.answer = tracked_answer
    result = await CallbackQueryAckMiddleware()(handler, callback, {})

    assert result == "ok"
    assert events == ["answer", "handler"]


@pytest.mark.asyncio
async def test_expired_callback_ack_does_not_block_handler() -> None:
    error = TelegramBadRequest(
        method=AnswerCallbackQuery(callback_query_id="callback-id"),
        message="Bad Request: query is too old and response timeout expired",
    )
    callback = FakeCallback(answer_error=error)
    handled = False

    async def handler(event, data):
        nonlocal handled
        handled = True

    await CallbackQueryAckMiddleware()(handler, callback, {})

    assert handled
    assert callback.answer_calls == 1


@pytest.mark.asyncio
async def test_processing_state_replaces_buttons_with_progress_text() -> None:
    message = FakeMessage()
    callback = FakeCallback(message=message)

    await _show_callback_processing(callback, "Заявка отправляется...")

    assert message.edits == [
        {
            "text": "Заявка отправляется...",
            "reply_markup": None,
            "parse_mode": None,
        }
    ]


@pytest.mark.asyncio
async def test_processing_state_removes_buttons_when_text_edit_fails() -> None:
    message = FakeMessage(edit_error=RuntimeError("edit failed"))
    callback = FakeCallback(message=message)

    await _show_callback_processing(callback, "Заявка отправляется...")

    assert message.markup_edits == [None]
    assert message.answers[0]["text"] == "Заявка отправляется..."


@pytest.mark.asyncio
async def test_user_action_lock_serializes_same_user() -> None:
    middleware = UserActionLockMiddleware()
    event = SimpleNamespace(from_user=SimpleNamespace(id=100))
    entered: list[int] = []
    release_first = asyncio.Event()

    async def handler(current_event, data):
        sequence = data["sequence"]
        entered.append(sequence)
        if sequence == 1:
            await release_first.wait()

    first = asyncio.create_task(middleware(handler, event, {"sequence": 1}))
    await asyncio.sleep(0)
    second = asyncio.create_task(middleware(handler, event, {"sequence": 2}))
    await asyncio.sleep(0)

    assert entered == [1]
    release_first.set()
    await asyncio.gather(first, second)
    assert entered == [1, 2]


@pytest.mark.asyncio
async def test_private_chat_middleware_ignores_group_message(caplog) -> None:
    middleware = PrivateChatOnlyMiddleware()
    event = UiMessage(
        message_id=10,
        chat_id=-100123456,
        chat_type="supergroup",
        chat_title="Editors",
    )
    handled = False

    async def handler(current_event, data):
        nonlocal handled
        handled = True

    with caplog.at_level("INFO"):
        result = await middleware(handler, event, {})

    assert result is None
    assert not handled
    assert "chat_id=-100123456" in caplog.text
    assert "chat_type=supergroup" in caplog.text


@pytest.mark.asyncio
async def test_private_chat_middleware_rejects_group_callback() -> None:
    middleware = PrivateChatOnlyMiddleware()
    message = UiMessage(
        message_id=10,
        chat_id=-100123456,
        chat_type="group",
        chat_title="Editors",
    )
    callback = FakeCallback(message=message, data="app:new")
    handled = False

    async def handler(current_event, data):
        nonlocal handled
        handled = True

    result = await middleware(handler, callback, {})

    assert result is None
    assert not handled
    assert callback.answer_texts == ["Бот принимает заявки только в личном чате."]


@pytest.mark.asyncio
async def test_stale_callback_is_rejected_and_keyboard_disabled(tmp_path) -> None:
    repository = DraftRepository(str(tmp_path / "stale_callback.db"))
    await repository.init()
    await repository.set_active_message(100, chat_id=100, message_id=20)
    message = UiMessage(message_id=10)
    callback = FakeCallback(message=message, data="app:cancel")
    handled = False

    async def handler(event, data):
        nonlocal handled
        handled = True

    await CallbackQueryAckMiddleware(repository)(handler, callback, {})

    assert not handled
    assert callback.answer_texts == [STALE_CALLBACK_TEXT]
    assert message.markup_edits == [None]


@pytest.mark.asyncio
async def test_active_callback_reaches_handler(tmp_path) -> None:
    repository = DraftRepository(str(tmp_path / "active_callback.db"))
    await repository.init()
    await repository.set_active_message(100, chat_id=100, message_id=10)
    callback = FakeCallback(message=UiMessage(message_id=10), data="app:cancel")
    handled = False

    async def handler(event, data):
        nonlocal handled
        handled = True

    await CallbackQueryAckMiddleware(repository)(handler, callback, {})

    assert handled
    assert callback.answer_texts == [None]


@pytest.mark.asyncio
async def test_callback_without_active_message_is_rejected(tmp_path) -> None:
    repository = DraftRepository(str(tmp_path / "missing_active_callback.db"))
    await repository.init()
    callback = FakeCallback(message=UiMessage(message_id=10), data="app:back")
    handled = False

    async def handler(event, data):
        nonlocal handled
        handled = True

    await CallbackQueryAckMiddleware(repository)(handler, callback, {})

    assert not handled
    assert callback.answer_texts == [STALE_CALLBACK_TEXT]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data",
    ["app:bulk_ready:BATCH-1234", "app:notification:new"],
)
async def test_long_lived_callbacks_bypass_active_message_check(
    tmp_path,
    callback_data,
) -> None:
    repository = DraftRepository(str(tmp_path / "callback_exception.db"))
    await repository.init()
    callback = FakeCallback(message=UiMessage(message_id=10), data=callback_data)
    handled = False

    async def handler(event, data):
        nonlocal handled
        handled = True

    await CallbackQueryAckMiddleware(repository)(handler, callback, {})

    assert handled


@pytest.mark.asyncio
async def test_ui_send_disables_previous_keyboard_and_saves_new_message(tmp_path) -> None:
    repository = DraftRepository(str(tmp_path / "ui_send.db"))
    await repository.init()
    await repository.set_active_message(100, chat_id=100, message_id=10)
    bot = FakeBot()
    incoming = UiMessage(message_id=50, bot=bot, answer_message_id=60)
    manager = ActiveMessageManager(repository)

    await manager.send(
        incoming,
        BotResponse(text="Новое меню", keyboard=KeyboardKind.CREATE_MODE),
    )

    settings = await repository.get_user_settings(100)
    assert bot.markup_edits == [
        {"chat_id": 100, "message_id": 10, "reply_markup": None}
    ]
    assert settings.active_chat_id == 100
    assert settings.active_message_id == 60


@pytest.mark.asyncio
async def test_ui_callback_fallback_replaces_active_message(tmp_path) -> None:
    repository = DraftRepository(str(tmp_path / "ui_callback_fallback.db"))
    await repository.init()
    await repository.set_active_message(100, chat_id=100, message_id=10)
    message = UiMessage(
        message_id=10,
        edit_error=RuntimeError("edit failed"),
        answer_message_id=11,
    )
    callback = FakeCallback(message=message, data="app:cancel")
    manager = ActiveMessageManager(repository)

    await manager.answer_callback(
        callback,
        BotResponse(text="Новое меню", keyboard=KeyboardKind.CREATE_MODE),
    )

    settings = await repository.get_user_settings(100)
    assert message.markup_edits == [None]
    assert settings.active_message_id == 11


@pytest.mark.asyncio
async def test_ui_callback_edit_keeps_current_active_message(tmp_path) -> None:
    repository = DraftRepository(str(tmp_path / "ui_callback_edit.db"))
    await repository.init()
    await repository.set_active_message(100, chat_id=100, message_id=10)
    message = UiMessage(message_id=10)
    callback = FakeCallback(message=message, data="app:back")
    manager = ActiveMessageManager(repository)

    await manager.answer_callback(
        callback,
        BotResponse(text="Следующий шаг", keyboard=KeyboardKind.STEP),
    )

    settings = await repository.get_user_settings(100)
    assert len(message.edits) == 1
    assert settings.active_chat_id == 100
    assert settings.active_message_id == 10


@pytest.mark.asyncio
async def test_ui_response_without_keyboard_clears_active_message(tmp_path) -> None:
    repository = DraftRepository(str(tmp_path / "ui_clear.db"))
    await repository.init()
    await repository.set_active_message(100, chat_id=100, message_id=10)
    incoming = UiMessage(message_id=50, answer_message_id=60)
    manager = ActiveMessageManager(repository)

    await manager.send(incoming, BotResponse(text="Готово"))

    settings = await repository.get_user_settings(100)
    assert settings.active_chat_id is None
    assert settings.active_message_id is None


@pytest.mark.asyncio
async def test_ui_disables_new_keyboard_if_active_message_save_fails(
    tmp_path,
    monkeypatch,
) -> None:
    repository = DraftRepository(str(tmp_path / "ui_save_failure.db"))
    await repository.init()
    incoming = UiMessage(message_id=50, answer_message_id=60)
    manager = ActiveMessageManager(repository)

    async def fail_save(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repository, "set_active_message", fail_save)
    await manager.send(
        incoming,
        BotResponse(text="Новое меню", keyboard=KeyboardKind.CREATE_MODE),
    )

    assert incoming.last_answer is not None
    assert incoming.last_answer.markup_edits == [None]


@pytest.mark.asyncio
async def test_notification_menu_preserves_notification_and_sends_new_active_message(
    tmp_path,
) -> None:
    repository = DraftRepository(str(tmp_path / "notification_menu.db"))
    await repository.init()
    await repository.set_active_message(100, chat_id=100, message_id=10)
    bot = FakeBot()
    notification = UiMessage(message_id=30, bot=bot, answer_message_id=31)
    callback = FakeCallback(message=notification, data="app:notification:new")
    manager = ActiveMessageManager(repository)

    await manager.answer_notification_callback(
        callback,
        BotResponse(text="Главное меню", keyboard=KeyboardKind.CREATE_MODE),
    )

    settings = await repository.get_user_settings(100)
    assert bot.markup_edits == [
        {"chat_id": 100, "message_id": 10, "reply_markup": None}
    ]
    assert notification.edits == []
    assert notification.markup_edits == [None]
    assert notification.answers[0]["text"] == "Главное меню"
    assert settings.active_message_id == 31


@pytest.mark.asyncio
async def test_notification_menu_opens_when_keyboard_removal_fails(tmp_path) -> None:
    repository = DraftRepository(str(tmp_path / "notification_menu_failure.db"))
    await repository.init()
    notification = UiMessage(
        message_id=30,
        markup_edit_error=RuntimeError("edit failed"),
        answer_message_id=31,
    )
    callback = FakeCallback(message=notification, data="app:notification:new")
    manager = ActiveMessageManager(repository)

    await manager.answer_notification_callback(
        callback,
        BotResponse(text="Главное меню", keyboard=KeyboardKind.CREATE_MODE),
    )

    settings = await repository.get_user_settings(100)
    assert notification.edits == []
    assert notification.answers[0]["text"] == "Главное меню"
    assert settings.active_message_id == 31
