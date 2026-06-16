from __future__ import annotations

import json

import pytest
from app.flow import ApplicationFlow
from app.keyboards import build_keyboard
from app.models import (
    AnswerType,
    ApplicationStatus,
    ChangeType,
    Direction,
    FieldName,
    KeyboardKind,
    LlmCheckStatus,
    LlmResult,
    Step,
)
from app.repository import DraftRepository
from app.submission import InMemorySubmissionService


class FakeLlmClient:
    def __init__(self, results: list[LlmResult] | None = None) -> None:
        self.results = results or [
            LlmResult(
                is_complete=True,
                blocking_problem=None,
                clarification_instruction=None,
            )
        ]
        self.calls = []

    async def check_change_description(self, context):
        self.calls.append(context)
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


class FakeBulkService:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str | None]] = []

    async def create_batch(
        self,
        telegram_user_id: int,
        direction: str,
        batch_id: str | None = None,
    ):
        from app.bulk import BulkBatchCreationResult
        from app.models import BulkBatch, utc_now_iso

        now = utc_now_iso()
        self.calls.append((telegram_user_id, direction, batch_id))
        return BulkBatchCreationResult(
            success=True,
            message="ok",
            batch=BulkBatch(
                batch_id=batch_id or "BATCH-ABC12345",
                telegram_user_id=telegram_user_id,
                spreadsheet_id="sheet",
                direction=direction,
                sheet_name="Массовый ввод",
                sheet_id=100,
                start_row=1,
                data_start_row=3,
                reserved_rows=200,
                created_at=now,
                updated_at=now,
            ),
            insert_url="https://docs.google.com/spreadsheets/d/sheet/edit#gid=100&range=A3:G3",
        )


class FakeBulkRegistrar:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def register_batch(self, batch_id: str, telegram_user_id: int):
        self.calls.append((batch_id, telegram_user_id))
        return self.result


async def make_flow(tmp_path, llm_client: FakeLlmClient | None = None):
    repository = DraftRepository(str(tmp_path / "test.db"))
    await repository.init()
    submission_service = InMemorySubmissionService()
    llm_client = llm_client or FakeLlmClient()
    flow = ApplicationFlow(repository, llm_client, submission_service)
    return flow, repository, submission_service, llm_client


async def make_flow_with_json(tmp_path, llm_client: FakeLlmClient | None = None):
    repository = DraftRepository(str(tmp_path / "test.db"))
    await repository.init()
    submission_service = InMemorySubmissionService()
    llm_client = llm_client or FakeLlmClient()
    flow = ApplicationFlow(
        repository,
        llm_client,
        submission_service,
        show_llm_response_json=True,
    )
    return flow, repository, submission_service, llm_client


async def fill_to_review(flow: ApplicationFlow, user_id: int = 100) -> None:
    await flow.start_single(user_id)
    await flow.select_direction(user_id, Direction.FL)
    await flow.select_answer_type(user_id, AnswerType.ROLLOUT)
    await flow.select_change_type(user_id, ChangeType.ADD)
    await flow.handle_text(user_id, "intent.change_limit")
    await flow.handle_text(user_id, "Иван Иванов")
    await flow.handle_text(user_id, "Изменились условия продукта")
    await flow.handle_text(user_id, "Обновить срок рассмотрения с 3 до 5 дней")
    await flow.handle_text(user_id, "Ваше обращение рассмотрим за 3 дня")
    await flow.select_urgency(user_id, True)


@pytest.mark.asyncio
async def test_start_new_application(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)

    response = await flow.start_new(10)
    draft = await repository.get_by_user_id(10)

    assert response.keyboard == KeyboardKind.CREATE_MODE
    assert "как хотите создать заявку" in response.text
    assert draft is None


@pytest.mark.asyncio
async def test_start_single_application(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)

    response = await flow.start_single(10, author_name="РђРІС‚РѕСЂ РўРµСЃС‚РѕРІ")
    draft = await repository.get_by_user_id(10)

    assert response.keyboard == KeyboardKind.DIRECTION
    assert "направление" in response.text.lower()
    assert draft is not None
    assert draft.current_step == Step.DIRECTION
    assert draft.application_id is not None
    assert draft.author_name == "РђРІС‚РѕСЂ РўРµСЃС‚РѕРІ"


@pytest.mark.asyncio
async def test_rollout_requires_change_type_before_intent(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await flow.start_single(101)
    await flow.select_direction(101, Direction.FL)

    response = await flow.select_answer_type(101, AnswerType.ROLLOUT)
    assert response.keyboard == KeyboardKind.CHANGE_TYPE

    response = await flow.select_change_type(101, ChangeType.CHIPS)
    draft = await repository.get_by_user_id(101)

    assert response.keyboard == KeyboardKind.STEP
    assert draft is not None
    assert draft.current_step == Step.INTENT
    assert draft.change_type == ChangeType.CHIPS.value


@pytest.mark.asyncio
async def test_non_rollout_skips_and_clears_change_type(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await flow.start_single(102)
    await flow.select_direction(102, Direction.FL)
    await repository.save_answer(102, FieldName.CHANGE_TYPE.value, ChangeType.ADD.value)

    response = await flow.select_answer_type(102, AnswerType.URGENT)
    draft = await repository.get_by_user_id(102)

    assert response.keyboard == KeyboardKind.STEP
    assert draft is not None
    assert draft.current_step == Step.INTENT
    assert not draft.change_type


@pytest.mark.asyncio
async def test_collects_application_and_submits_stub(tmp_path):
    flow, repository, submission_service, llm_client = await make_flow(tmp_path)
    flow.dashboard_enabled = True

    await fill_to_review(flow, 11)
    draft = await repository.get_by_user_id(11)

    assert draft is not None
    assert draft.current_step == Step.REVIEW
    assert draft.intent == "intent.change_limit"
    assert draft.scriptwriter == "Иван Иванов"
    assert draft.reason == "Изменились условия продукта"
    assert draft.raw_change_description == "Обновить срок рассмотрения с 3 до 5 дней"
    assert draft.formatted_change_description == "Обновить срок рассмотрения с 3 до 5 дней"
    assert draft.llm_check_status == LlmCheckStatus.COMPLETE.value
    assert draft.llm_score is None
    assert draft.source_text == "Ваше обращение рассмотрим за 3 дня"
    assert draft.direction == Direction.FL.value
    assert draft.answer_type == AnswerType.ROLLOUT.value
    assert draft.is_urgent is False

    response = await flow.submit(11)
    completed = await repository.get_by_user_id(11)
    tracked = await repository.get_submitted_application(draft.application_id or "")

    assert "Заявка отправлена в таблицу" in response.text
    assert len(submission_service.submitted) == 1
    assert len(llm_client.calls) == 1
    assert completed is not None
    assert completed.current_step == Step.COMPLETED
    assert tracked is not None
    assert tracked.telegram_user_id == 11
    assert tracked.sheet_name == "01.06"
    assert tracked.spreadsheet_id == "test-spreadsheet"
    assert tracked.last_known_status == ApplicationStatus.NEW.value
    dashboard_outbox = await repository.list_dashboard_outbox()
    assert len(dashboard_outbox) == 1
    dashboard_row = json.loads(dashboard_outbox[0].snapshot_json)["row"]
    assert dashboard_row[0] == draft.application_id
    assert dashboard_row[8] == ApplicationStatus.NEW.value


@pytest.mark.asyncio
async def test_review_contains_emoji_labels_and_bold_html(tmp_path):
    flow, _, _, _ = await make_flow(tmp_path)
    await fill_to_review(flow, 22)

    response = await flow.show_review(22)

    assert response.parse_mode == "HTML"
    assert "🎯 <b>Интент:</b>" in response.text
    assert "👤 <b>Закрепленный сценарист:</b>" in response.text
    assert "📝 <b>Причина изменений:</b>" in response.text
    assert "🔧 <b>Суть изменений:</b>" in response.text
    assert "📄 <b>Исходный текст:</b>" in response.text
    assert "🧭 <b>Направление:</b>" in response.text
    assert "🏷 <b>Тип ответа:</b>" in response.text
    assert "⚡ <b>Срочная:</b>" in response.text
    assert "ФЛ-chatbot" in response.text


def test_direction_keyboard_uses_chatbot_labels_and_internal_callbacks():
    keyboard = build_keyboard(KeyboardKind.DIRECTION)

    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].text == "ФЛ-chatbot"
    assert keyboard.inline_keyboard[0][0].callback_data == "app:direction:ФЛ"
    assert keyboard.inline_keyboard[0][1].text == "SME-chatbot"
    assert keyboard.inline_keyboard[0][1].callback_data == "app:direction:SME"


def test_bulk_created_keyboard_has_only_ready_button():
    keyboard = build_keyboard(KeyboardKind.BULK_CREATED, "BATCH-ABC12345")

    assert keyboard is not None
    assert len(keyboard.inline_keyboard) == 1
    assert len(keyboard.inline_keyboard[0]) == 1
    assert keyboard.inline_keyboard[0][0].text == "Заявка заполнена"
    assert keyboard.inline_keyboard[0][0].callback_data == "app:bulk_ready:BATCH-ABC12345"


def test_bulk_completed_keyboard_has_only_main_menu():
    keyboard = build_keyboard(KeyboardKind.BULK_COMPLETED)

    assert keyboard is not None
    assert len(keyboard.inline_keyboard) == 1
    assert len(keyboard.inline_keyboard[0]) == 1
    assert keyboard.inline_keyboard[0][0].text == "Главное меню"
    assert keyboard.inline_keyboard[0][0].callback_data == "app:new"


@pytest.mark.asyncio
async def test_should_show_processing_only_on_llm_steps(tmp_path):
    flow, _, _, _ = await make_flow(tmp_path)
    await flow.start_new(23)

    assert await flow.should_show_llm_processing(23) is False

    await flow.select_direction(23, Direction.FL)
    await flow.select_answer_type(23, AnswerType.ROLLOUT)
    await flow.select_change_type(23, ChangeType.ADD)
    await flow.handle_text(23, "intent.change_limit")
    await flow.handle_text(23, "Иван Иванов")
    await flow.handle_text(23, "Изменились условия продукта")

    assert await flow.should_show_llm_processing(23) is True


@pytest.mark.asyncio
async def test_empty_answer_does_not_change_step(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await flow.start_new(12)

    response = await flow.handle_text(12, "   ")
    draft = await repository.get_by_user_id(12)

    assert "Поле не должно быть пустым" in response.text
    assert draft is not None
    assert draft.current_step == Step.DIRECTION
    assert draft.intent is None


@pytest.mark.asyncio
async def test_back_returns_to_previous_step(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await flow.start_new(13)
    await flow.select_direction(13, Direction.FL)

    response = await flow.back(13)
    draft = await repository.get_by_user_id(13)

    assert "предыдущий шаг" in response.text
    assert draft is not None
    assert draft.current_step == Step.DIRECTION


@pytest.mark.asyncio
async def test_cancel_deletes_draft(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await flow.start_new(14)

    response = await flow.cancel(14)
    draft = await repository.get_by_user_id(14)

    assert "Заявка отменена" in response.text
    assert draft is None


@pytest.mark.asyncio
async def test_edit_text_field_returns_to_review(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await fill_to_review(flow, 15)

    menu = await flow.open_edit_menu(15)
    prompt = await flow.select_edit_field(15, FieldName.REASON)
    response = await flow.handle_text(15, "Юридическое требование")
    draft = await repository.get_by_user_id(15)

    assert menu.keyboard == KeyboardKind.EDIT_MENU_ROLLOUT
    assert "новую причину" in prompt.text
    assert response.keyboard == KeyboardKind.REVIEW
    assert draft is not None
    assert draft.current_step == Step.REVIEW
    assert draft.reason == "Юридическое требование"


@pytest.mark.asyncio
async def test_edit_urgency_returns_to_review(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await fill_to_review(flow, 16)

    response = await flow.select_edit_field(16, FieldName.URGENCY)
    await flow.select_urgency(16, False)
    draft = await repository.get_by_user_id(16)

    assert response.keyboard == KeyboardKind.REVIEW
    assert draft is not None
    assert draft.current_step == Step.REVIEW
    assert draft.is_urgent is False


@pytest.mark.asyncio
async def test_new_with_active_draft_offers_navigation(tmp_path):
    flow, _, _, _ = await make_flow(tmp_path)
    await flow.start_single(17)

    response = await flow.start_single(17)

    assert response.keyboard == KeyboardKind.ACTIVE_DRAFT
    assert "незавершенная заявка" in response.text


@pytest.mark.asyncio
async def test_bulk_upload_stub_does_not_create_draft(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)

    response = await flow.bulk_upload_stub(171)
    draft = await repository.get_by_user_id(171)

    assert response.keyboard == KeyboardKind.BULK_MENU
    assert "Массовая загрузка работает через отдельный лист Google Sheets" in response.text
    assert "xlsx" not in response.text.lower()
    assert draft is None


@pytest.mark.asyncio
async def test_create_bulk_batch_returns_ready_button(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_flow.db"))
    await repository.init()
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        bulk_service=FakeBulkService(),
    )
    direction_response = await flow.create_bulk_batch(180)
    idempotency_key = direction_response.keyboard_payload
    assert idempotency_key is not None
    response = await flow.select_bulk_direction(180, idempotency_key, Direction.FL)

    assert response.keyboard == KeyboardKind.BULK_CREATED
    assert response.keyboard_payload is not None
    assert "Заявка заполнена" in response.text
    assert "до 100 строк" in response.text


@pytest.mark.asyncio
async def test_bulk_creation_callback_is_idempotent(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_idempotent.db"))
    await repository.init()
    service = FakeBulkService()
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        bulk_service=service,
    )
    direction_response = await flow.create_bulk_batch(180)
    idempotency_key = direction_response.keyboard_payload
    assert idempotency_key is not None

    first = await flow.select_bulk_direction(180, idempotency_key, Direction.FL)
    second = await flow.select_bulk_direction(180, idempotency_key, Direction.FL)

    assert first.keyboard_payload == second.keyboard_payload
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_notification_menu_restores_bulk_direction_selection(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_notification_direction.db"))
    await repository.init()
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        bulk_service=FakeBulkService(),
    )
    initial = await flow.create_bulk_batch(180)

    response = await flow.resume_from_notification(180)

    assert response.keyboard == KeyboardKind.BULK_DIRECTION
    assert response.keyboard_payload == initial.keyboard_payload
    assert "выберите направление" in response.text


@pytest.mark.asyncio
async def test_notification_menu_restores_unregistered_bulk_batch(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_notification_created.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=180,
        spreadsheet_id="sheet",
        direction=Direction.FL.value,
        sheet_name="Массовый ввод",
        sheet_id=100,
        start_row=1,
        data_start_row=3,
        reserved_rows=100,
    )
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
    )

    response = await flow.resume_from_notification(180)

    assert response.keyboard == KeyboardKind.BULK_CREATED
    assert response.keyboard_payload == "BATCH-ABC12345"
    assert "Заявка заполнена" in response.text
    assert "gid=100&amp;range=A3:G3" in response.text


@pytest.mark.asyncio
async def test_notification_menu_restores_exact_single_application_step(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await flow.start_single(181)
    await flow.select_direction(181, Direction.FL)

    response = await flow.resume_from_notification(181)
    draft = await repository.get_by_user_id(181)

    assert draft is not None
    assert draft.current_step == Step.ANSWER_TYPE
    assert response.keyboard == KeyboardKind.ANSWER_TYPE
    assert "Продолжаем незавершенную заявку" in response.text


@pytest.mark.asyncio
async def test_notification_menu_prefers_bulk_over_single_application(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await flow.start_single(182)
    await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=182,
        spreadsheet_id="sheet",
        direction=Direction.FL.value,
        sheet_name="Массовый ввод",
        sheet_id=100,
        start_row=1,
        data_start_row=3,
        reserved_rows=100,
    )

    response = await flow.resume_from_notification(182)

    assert response.keyboard == KeyboardKind.BULK_CREATED
    assert response.keyboard_payload == "BATCH-ABC12345"


@pytest.mark.asyncio
async def test_notification_menu_ignores_registered_bulk_batch(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_notification_registered.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=180,
        spreadsheet_id="sheet",
        direction=Direction.FL.value,
        sheet_name="Массовый ввод",
        sheet_id=100,
        start_row=1,
        data_start_row=3,
        reserved_rows=100,
    )
    await repository.claim_bulk_batch_registration(
        "BATCH-ABC12345",
        stale_after_seconds=600,
    )
    await repository.complete_bulk_batch_registration(
        "BATCH-ABC12345",
        data_end_row=3,
        registered_count=1,
    )
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
    )

    response = await flow.resume_from_notification(180)

    assert response.keyboard == KeyboardKind.CREATE_MODE
    assert response.keyboard_payload is None


@pytest.mark.asyncio
async def test_confirm_bulk_batch_filled_calls_registrar(tmp_path):
    from app.bulk import BulkRegistrationResult

    repository = DraftRepository(str(tmp_path / "bulk_confirm.db"))
    await repository.init()
    registrar = FakeBulkRegistrar(
        BulkRegistrationResult(
            success=True,
            message="Массовая заявка зарегистрирована. Строк зарегистрировано: 2.",
            registered_count=2,
        )
    )
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        bulk_registrar=registrar,
    )

    response = await flow.confirm_bulk_batch_filled(180, "BATCH-ABC12345")

    assert registrar.calls == [("BATCH-ABC12345", 180)]
    assert response.keyboard == KeyboardKind.BULK_COMPLETED
    assert "Строк зарегистрировано: 2" in response.text


@pytest.mark.asyncio
async def test_confirm_bulk_batch_filled_keeps_ready_button_on_empty_rows(tmp_path):
    from app.bulk import BulkRegistrationResult

    repository = DraftRepository(str(tmp_path / "bulk_confirm_empty.db"))
    await repository.init()
    registrar = FakeBulkRegistrar(
        BulkRegistrationResult(
            success=False,
            message="В массовой заявке не найдены заполненные строки.",
        )
    )
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        bulk_registrar=registrar,
    )

    response = await flow.confirm_bulk_batch_filled(180, "BATCH-ABC12345")

    assert response.keyboard == KeyboardKind.BULK_CREATED
    assert response.keyboard_payload == "BATCH-ABC12345"


@pytest.mark.asyncio
async def test_confirm_bulk_batch_filled_hides_ready_button_while_registering(tmp_path):
    from app.bulk import BulkRegistrationResult

    repository = DraftRepository(str(tmp_path / "bulk_confirm_registering.db"))
    await repository.init()
    registrar = FakeBulkRegistrar(
        BulkRegistrationResult(
            success=False,
            message="Массовая заявка уже регистрируется.",
            retry_allowed=False,
        )
    )
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        bulk_registrar=registrar,
    )

    response = await flow.confirm_bulk_batch_filled(180, "BATCH-ABC12345")

    assert response.keyboard == KeyboardKind.BULK_MENU
    assert response.keyboard_payload is None


@pytest.mark.asyncio
async def test_default_direction_accepts_chatbot_label(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)

    await flow.start_set_default_direction(181)
    response = await flow.handle_text(181, "ФЛ-chatbot")
    settings = await repository.get_user_settings(181)

    assert response.keyboard == KeyboardKind.DEFAULTS_MENU
    assert settings.default_direction == Direction.FL.value
    assert "ФЛ-chatbot" in response.text


@pytest.mark.asyncio
async def test_default_intent_can_be_used_on_intent_step(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await repository.save_user_setting(172, "default_intent", "intent.default")

    response = await flow.start_single(172)
    assert response.keyboard == KeyboardKind.DIRECTION

    await flow.select_direction(172, Direction.FL)
    await flow.select_answer_type(172, AnswerType.ROLLOUT)
    response = await flow.select_change_type(172, ChangeType.ADD)
    assert response.keyboard == KeyboardKind.INTENT_STEP_WITH_DEFAULT

    response = await flow.use_default_intent(172)
    draft = await repository.get_by_user_id(172)

    assert response.keyboard == KeyboardKind.STEP
    assert draft is not None
    assert draft.intent == "intent.default"
    assert draft.current_step == Step.SCRIPTWRITER


@pytest.mark.asyncio
async def test_default_scriptwriter_can_be_used_on_scriptwriter_step(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await repository.save_user_setting(173, "default_scriptwriter", "Иван Иванов")
    await flow.start_single(173)
    await flow.select_direction(173, Direction.FL)
    await flow.select_answer_type(173, AnswerType.ROLLOUT)
    await flow.select_change_type(173, ChangeType.ADD)
    await flow.handle_text(173, "intent.change_limit")

    response = await flow.use_default_scriptwriter(173)
    draft = await repository.get_by_user_id(173)

    assert response.keyboard == KeyboardKind.STEP
    assert draft is not None
    assert draft.scriptwriter == "Иван Иванов"
    assert draft.current_step == Step.REASON


@pytest.mark.asyncio
async def test_defaults_menu_saves_text_without_creating_draft(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)

    prompt = await flow.start_set_default_intent(174)
    response = await flow.handle_text(174, "intent.saved")
    settings = await repository.get_user_settings(174)
    draft = await repository.get_by_user_id(174)

    assert prompt.keyboard == KeyboardKind.DEFAULTS_BACK
    assert response.keyboard == KeyboardKind.DEFAULTS_MENU
    assert settings.default_intent == "intent.saved"
    assert settings.pending_action is None
    assert draft is None


@pytest.mark.asyncio
async def test_incomplete_change_description_asks_one_clarification(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=False,
                blocking_problem="Не указан ожидаемый результат",
                clarification_instruction=(
                    "Дополните поле: укажите, какой результат должен получиться после правки."
                ),
            ),
            LlmResult(
                is_complete=True,
                blocking_problem=None,
                clarification_instruction=None,
            ),
        ]
    )
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await flow.start_new(18)
    await flow.select_direction(18, Direction.FL)
    await flow.select_answer_type(18, AnswerType.ROLLOUT)
    await flow.select_change_type(18, ChangeType.ADD)
    await flow.handle_text(18, "intent.change_limit")
    await flow.handle_text(18, "Иван Иванов")
    await flow.handle_text(18, "Изменились условия продукта")

    first_response = await flow.handle_text(18, "Поменять срок")
    draft = await repository.get_by_user_id(18)

    assert "не хватает информации" in first_response.text
    assert "Не указан ожидаемый результат" in first_response.text
    assert "Дополните поле: укажите, какой результат" in first_response.text
    assert draft is not None
    assert draft.current_step == Step.CHANGE_DESCRIPTION_CLARIFICATION

    second_response = await flow.handle_text(18, "Нужно указать 5 рабочих дней")
    draft = await repository.get_by_user_id(18)

    assert "исходный текст" in second_response.text.lower()
    assert draft is not None
    assert draft.current_step == Step.SOURCE_TEXT
    assert draft.clarification_count == 1
    assert draft.raw_change_description == "Поменять срок"
    assert draft.formatted_change_description == (
        "Поменять срок\n\nУточнение сценариста: Нужно указать 5 рабочих дней"
    )
    assert draft.llm_check_status == LlmCheckStatus.COMPLETE.value
    assert len(llm_client.calls) == 2
    assert llm_client.calls[0].direction == Direction.FL.value
    assert llm_client.calls[0].answer_type == AnswerType.ROLLOUT.value
    assert llm_client.calls[0].change_type == ChangeType.ADD.value
    assert llm_client.calls[1].raw_change_description == "Поменять срок"
    assert llm_client.calls[1].clarification_text == "Нужно указать 5 рабочих дней"


@pytest.mark.asyncio
async def test_incomplete_after_clarification_continues_with_attention_status(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=False,
                blocking_problem="Не указано конкретное изменение",
                clarification_instruction=(
                    "Перепишите поле целиком: укажите, что изменить, что сделать "
                    "и какой результат должен получиться."
                ),
            ),
            LlmResult(
                is_complete=False,
                blocking_problem="После ответа задача все еще неоднозначна",
                clarification_instruction=(
                    "Укажите конкретный итоговый результат изменения."
                ),
            ),
        ]
    )
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await flow.start_new(19)
    await flow.select_direction(19, Direction.FL)
    await flow.select_answer_type(19, AnswerType.ROLLOUT)
    await flow.select_change_type(19, ChangeType.ADD)
    await flow.handle_text(19, "intent.change_limit")
    await flow.handle_text(19, "Иван Иванов")
    await flow.handle_text(19, "Изменились условия продукта")
    await flow.handle_text(19, "Поменять текст")

    response = await flow.handle_text(19, "Сделать лучше")
    draft = await repository.get_by_user_id(19)

    assert "дополнительное внимание" in response.text
    assert draft is not None
    assert draft.current_step == Step.SOURCE_TEXT
    assert draft.llm_check_status == LlmCheckStatus.NEEDS_ATTENTION.value
    assert draft.formatted_change_description == (
        "Поменять текст\n\nУточнение сценариста: Сделать лучше"
    )
    assert draft.raw_change_description == "Поменять текст"


@pytest.mark.asyncio
async def test_complete_check_keeps_score_empty(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=True,
                blocking_problem=None,
                clarification_instruction=None,
            )
        ]
    )
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await flow.start_new(24)
    await flow.select_direction(24, Direction.FL)
    await flow.select_answer_type(24, AnswerType.ROLLOUT)
    await flow.select_change_type(24, ChangeType.ADD)
    await flow.handle_text(24, "intent.change_limit")
    await flow.handle_text(24, "Иван Иванов")
    await flow.handle_text(24, "Изменились условия продукта")

    await flow.handle_text(24, "Обновить срок")
    draft = await repository.get_by_user_id(24)

    assert draft is not None
    assert draft.current_step == Step.SOURCE_TEXT
    assert draft.llm_check_status == LlmCheckStatus.COMPLETE.value
    assert draft.llm_score is None


@pytest.mark.asyncio
async def test_llm_error_keeps_user_text_and_continues(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=True,
                blocking_problem="Ошибка GigaChat: ConnectError",
                clarification_instruction=None,
            )
        ]
    )
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await flow.start_new(26)
    await flow.select_direction(26, Direction.FL)
    await flow.select_answer_type(26, AnswerType.ROLLOUT)
    await flow.select_change_type(26, ChangeType.ADD)
    await flow.handle_text(26, "intent.change_limit")
    await flow.handle_text(26, "Иван Иванов")
    await flow.handle_text(26, "Изменились условия")

    response = await flow.handle_text(26, "Поменять срок ответа на 5 дней")
    draft = await repository.get_by_user_id(26)

    assert draft is not None
    assert draft.current_step == Step.SOURCE_TEXT
    assert draft.llm_check_status == LlmCheckStatus.ERROR.value
    assert draft.raw_change_description == "Поменять срок ответа на 5 дней"
    assert draft.formatted_change_description == "Поменять срок ответа на 5 дней"
    assert draft.llm_score is None
    assert "Продолжаем с исходным текстом" in response.text


@pytest.mark.asyncio
async def test_incomplete_check_keeps_score_empty(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=False,
                blocking_problem="Не указано, что изменить",
                clarification_instruction=(
                    "Дополните поле: укажите изменяемый фрагмент и требуемый результат."
                ),
            )
        ]
    )
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await flow.start_new(25)
    await flow.select_direction(25, Direction.FL)
    await flow.select_answer_type(25, AnswerType.ROLLOUT)
    await flow.select_change_type(25, ChangeType.ADD)
    await flow.handle_text(25, "intent.change_limit")
    await flow.handle_text(25, "Иван Иванов")
    await flow.handle_text(25, "Изменились условия продукта")

    await flow.handle_text(25, "Обновить текст")
    draft = await repository.get_by_user_id(25)

    assert draft is not None
    assert draft.current_step == Step.CHANGE_DESCRIPTION_CLARIFICATION
    assert draft.llm_check_status == LlmCheckStatus.NEEDS_ATTENTION.value
    assert draft.llm_score is None


@pytest.mark.asyncio
async def test_edit_change_description_runs_llm_again(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=True,
                blocking_problem=None,
                clarification_instruction=None,
            ),
            LlmResult(
                is_complete=True,
                blocking_problem=None,
                clarification_instruction=None,
            ),
        ]
    )
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await fill_to_review(flow, 20)

    await flow.select_edit_field(20, FieldName.CHANGE_DESCRIPTION)
    response = await flow.handle_text(20, "Новая суть")
    draft = await repository.get_by_user_id(20)

    assert response.keyboard == KeyboardKind.REVIEW
    assert draft is not None
    assert draft.formatted_change_description == "Новая суть"
    assert draft.raw_change_description == "Новая суть"
    assert draft.clarification_count == 0
    assert len(llm_client.calls) == 2


@pytest.mark.asyncio
async def test_can_show_gigachat_json_response_in_bot_message(tmp_path):
    flow, _, _, _ = await make_flow_with_json(tmp_path)
    await flow.start_new(21)
    await flow.select_direction(21, Direction.FL)
    await flow.select_answer_type(21, AnswerType.ROLLOUT)
    await flow.select_change_type(21, ChangeType.ADD)
    await flow.handle_text(21, "intent.change_limit")
    await flow.handle_text(21, "Иван Иванов")
    await flow.handle_text(21, "Изменились условия продукта")

    response = await flow.handle_text(21, "Обновить срок рассмотрения с 3 до 5 дней")

    assert "Полнота описания проверена." in response.text
    assert "Ответ GigaChat:" in response.text
    assert '"is_complete": true' in response.text
    assert '"blocking_problem": null' in response.text
    assert '"clarification_instruction": null' in response.text
