from __future__ import annotations

import json
import re
from html import unescape

import pytest
from app.flow import ApplicationFlow, _bulk_registration_confirmation
from app.keyboards import build_keyboard
from app.models import (
    AnswerType,
    ApplicationStatus,
    BulkReservation,
    BulkReservationState,
    BulkTargetKind,
    ChangeType,
    ChipAfterTextAction,
    Direction,
    FieldName,
    KeyboardKind,
    LlmCheckStatus,
    LlmResult,
    LlmTelemetry,
    Step,
    SubmissionResult,
)
from app.repository import DraftRepository
from app.submission import InMemorySubmissionService
from app.bulk import BulkRegistrationResult, BulkReservationCreationResult


def _application_links(count: int) -> tuple[str, ...]:
    return tuple(
        f"https://docs.google.com/spreadsheets/d/test/edit#gid=1&range=A{index}:X{index}"
        for index in range(1, count + 1)
    )


def test_bulk_registration_confirmation_lists_up_to_five_ids_without_collapsing():
    response = _bulk_registration_confirmation(
        BulkRegistrationResult(
            success=True,
            message="Готово.",
            registered_count=5,
            empty_count=2,
            application_ids=("APP00001", "APP00002", "APP00003", "APP00004", "APP00005"),
            application_links=_application_links(5),
            retry_allowed=False,
        )
    )

    assert "Массовая заявка зарегистрирована" in response
    assert "<b>Статус:</b> Новая" in response
    assert "<b>Зарегистрировано заявок:</b> 5" in response
    assert "<b>Пустых строк оставлено:</b> 2" in response
    assert response.count('<a href="https://docs.google.com/') == 5
    assert ">APP00001</a>" in response
    assert "<blockquote expandable>" not in response


def test_bulk_registration_confirmation_collapses_more_than_five_ids():
    response = _bulk_registration_confirmation(
        BulkRegistrationResult(
            success=True,
            message="Готово.",
            registered_count=6,
            application_ids=tuple(f"APP0000{index}" for index in range(1, 7)),
            application_links=_application_links(6),
            retry_allowed=False,
        )
    )

    assert response.count('<a href="https://docs.google.com/') == 6
    assert response.count("<blockquote expandable>") == 1
    assert response.count("</blockquote>") == 1


def test_bulk_registration_confirmation_fits_telegram_limit_for_fifty_ids():
    response = _bulk_registration_confirmation(
        BulkRegistrationResult(
            success=True,
            message="Готово.",
            registered_count=50,
            application_ids=tuple(f"{index:08X}" for index in range(50)),
            application_links=_application_links(50),
            retry_allowed=False,
        )
    )

    assert response.count('<a href="https://docs.google.com/') == 50
    rendered_text = unescape(re.sub(r"<[^>]+>", "", response))
    assert len(rendered_text) < 4096
    assert "<blockquote expandable>" in response


def test_bulk_registration_confirmation_rejects_missing_application_links():
    with pytest.raises(ValueError, match="incomplete application links"):
        _bulk_registration_confirmation(
            BulkRegistrationResult(
                success=True,
                message="Готово.",
                registered_count=1,
                application_ids=("APP00001",),
            )
        )


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




class FakeBulkRegistrar:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def register_batch(self, batch_id: str, telegram_user_id: int):
        self.calls.append((batch_id, telegram_user_id))
        return self.result


class FakeBulkReservationService:
    def __init__(self, repository: DraftRepository | None = None) -> None:
        self.calls: list[BulkReservation] = []
        self.repository = repository

    async def create_reservation(self, reservation: BulkReservation):
        return await self.create_reservation_with_lock(reservation)

    async def create_reservation_with_lock(self, reservation: BulkReservation):
        self.calls.append(reservation)
        saved_reservation = BulkReservation(
            reservation_id=reservation.reservation_id,
            idempotency_key=reservation.idempotency_key,
            telegram_user_id=reservation.telegram_user_id,
            state=BulkReservationState.CREATED.value,
            direction=reservation.direction,
            target_kind=reservation.target_kind,
            change_type=reservation.change_type,
            requested_count=reservation.requested_count,
            spreadsheet_id="spreadsheet",
            sheet_id=123,
            sheet_name="29.06 (1)",
            start_row=10,
            end_row=14,
            insert_url="https://docs.google.com/spreadsheets/d/spreadsheet/edit#gid=123&range=A10:X14",
        )
        if self.repository is not None:
            saved_reservation = await self.repository.complete_bulk_reservation_creation_and_shift(
                reservation.reservation_id,
                spreadsheet_id=saved_reservation.spreadsheet_id or "",
                sheet_id=saved_reservation.sheet_id or 0,
                sheet_name=saved_reservation.sheet_name or "",
                start_row=saved_reservation.start_row or 0,
                end_row=saved_reservation.end_row or 0,
                insert_url=saved_reservation.insert_url or "",
                shifted_rows=saved_reservation.requested_count or 0,
            ) or saved_reservation
        return BulkReservationCreationResult(
            success=True,
            message="ok",
            reservation=saved_reservation,
            insert_url="https://docs.google.com/spreadsheets/d/spreadsheet/edit#gid=123&range=A10:X14",
        )


class RateLimitedSubmissionService:
    def resolve_target(self, application):
        return ("spreadsheet", "sheet")

    async def submit(self, application):
        return SubmissionResult(
            success=False,
            message=(
                "Google API временно перегружен и не принял заявку.\n\n"
                "Повторите отправку через 2-3 минуты. "
                "Заявка сохранена, заново заполнять её не нужно."
            ),
        )


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


async def fill_urgent_to_review(flow: ApplicationFlow, user_id: int = 100) -> None:
    await flow.start_single(user_id)
    await flow.select_direction(user_id, Direction.FL)
    await flow.select_answer_type(user_id, AnswerType.URGENT)
    await flow.select_change_type(user_id, ChangeType.ADD)
    await flow.handle_text(user_id, "urgent.intent")
    await flow.handle_text(user_id, "Urgent Scriptwriter")
    await flow.handle_text(user_id, "Client case")
    await flow.handle_text(user_id, "Fix urgent answer")
    await flow.handle_text(user_id, "Source answer")


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
async def test_change_description_prompt_explains_expected_detail(tmp_path):
    flow, _, _, _ = await make_flow(tmp_path)
    await flow.start_single(12)
    await flow.select_direction(12, Direction.FL)
    await flow.select_answer_type(12, AnswerType.ROLLOUT)
    await flow.select_change_type(12, ChangeType.ADD)
    await flow.handle_text(12, "intent.change_limit")
    await flow.handle_text(12, "Иван Иванов")

    response = await flow.handle_text(12, "Клиентское сообщение")

    assert "Опишите, что именно меняется в тексте" in response.text
    assert "Не подменяйте суть кейсом" in response.text


@pytest.mark.asyncio
async def test_reason_prompt_distinguishes_context_from_change_description(tmp_path):
    flow, _, _, _ = await make_flow(tmp_path)
    await flow.start_single(13)
    await flow.select_direction(13, Direction.FL)
    await flow.select_answer_type(13, AnswerType.ROLLOUT)
    await flow.select_change_type(13, ChangeType.ADD)
    await flow.handle_text(13, "intent.change_limit")
    response = await flow.handle_text(13, "Иван Иванов")

    assert "Это контекст" in response.text
    assert "это следующий шаг" in response.text


@pytest.mark.asyncio
async def test_source_text_prompt_uses_proposed_text_label(tmp_path):
    flow, _, _, _ = await make_flow(tmp_path)
    await flow.start_single(14)
    await flow.select_direction(14, Direction.FL)
    await flow.select_answer_type(14, AnswerType.ROLLOUT)
    await flow.select_change_type(14, ChangeType.ADD)
    await flow.handle_text(14, "intent.change_limit")
    await flow.handle_text(14, "Иван Иванов")
    await flow.handle_text(14, "Клиентское сообщение")

    response = await flow.handle_text(14, "Обновить срок ответа")

    assert response.keyboard == KeyboardKind.STEP
    assert "Пришлите предлагаемый текст" in response.text
    assert "Пришлите исходный текст" not in response.text


@pytest.mark.asyncio
async def test_missing_reason_uses_client_case_label(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await fill_to_review(flow, 13)
    await repository.save_answer(13, FieldName.REASON.value, "")

    response = await flow.submit(13)

    assert "Кейс или сообщения клиента" in response.text
    assert "Причина изменений" not in response.text


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
    assert draft.current_step == Step.SCRIPTWRITER
    assert draft.change_type == ChangeType.CHIPS.value


@pytest.mark.asyncio
async def test_chips_collects_dedicated_fields_without_gigachat(tmp_path):
    flow, repository, _, llm_client = await make_flow(tmp_path)
    user_id = 103
    await flow.start_single(user_id)
    await flow.select_direction(user_id, Direction.FL)
    await flow.select_answer_type(user_id, AnswerType.ROLLOUT)
    response = await flow.select_change_type(user_id, ChangeType.CHIPS)

    assert response.draft is not None
    assert response.draft.current_step == Step.SCRIPTWRITER
    assert await flow.should_show_llm_processing(user_id) is False

    response = await flow.handle_text(user_id, "Scriptwriter")
    assert response.draft.current_step == Step.INTENT
    response = await flow.handle_text(user_id, "intent.chips")
    assert response.draft.current_step == Step.REASON
    response = await flow.handle_text(user_id, "reason")
    assert response.draft.current_step == Step.CHIP_TEXT_BEFORE
    response = await flow.handle_text(user_id, "before")
    assert response.draft.current_step == Step.CHIP_TEXT
    response = await flow.handle_text(user_id, "chip")
    assert response.draft.current_step == Step.CHIP_AFTER_TEXT_ACTION
    response = await flow.select_chip_after_text_action(
        user_id,
        ChipAfterTextAction.UNCHANGED,
    )
    assert response.draft.current_step == Step.CHIP_TEXT_AFTER
    response = await flow.handle_text(user_id, "after")

    draft = await repository.get_by_user_id(user_id)
    assert response.keyboard == KeyboardKind.REVIEW
    assert draft is not None
    assert draft.current_step == Step.REVIEW
    assert draft.llm_check_status == LlmCheckStatus.SKIPPED.value
    assert (draft.chip_text_before, draft.chip_text, draft.chip_text_after) == (
        "before",
        "chip",
        "after",
    )
    assert llm_client.calls == []
    assert await repository.list_application_events(
        application_id=draft.application_id,
        event_type="llm_check_completed",
    ) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_change_type"),
    [
        (ChipAfterTextAction.ADD, ChangeType.ADD.value),
        (ChipAfterTextAction.EDIT, ChangeType.EDIT.value),
    ],
)
async def test_chips_action_creates_two_independent_tracked_applications(
    tmp_path,
    action,
    expected_change_type,
):
    flow, repository, submission_service, llm_client = await make_flow(tmp_path)
    user_id = 1103
    await flow.start_single(user_id)
    await flow.select_direction(user_id, Direction.FL)
    await flow.select_answer_type(user_id, AnswerType.ROLLOUT)
    await flow.select_change_type(user_id, ChangeType.CHIPS)
    for value in ("Writer", "intent.chips", "reason", "before", "chip"):
        await flow.handle_text(user_id, value)
    action_response = await flow.select_chip_after_text_action(user_id, action)
    assert action_response.draft is not None
    assert action_response.draft.current_step == Step.CHIP_RESPONSE_CHANGE_DESCRIPTION
    after_description = await flow.handle_text(
        user_id,
        "Нужно обновить текст после чипса",
    )
    assert after_description.draft is not None
    assert after_description.draft.current_step == Step.CHIP_TEXT_AFTER
    await flow.handle_text(user_id, "response after chip")

    result = await flow.submit(user_id)
    draft = await repository.get_by_user_id(user_id)
    tracked = await repository.list_submitted_applications()

    assert draft is not None
    assert draft.submission_state == "SENT"
    assert result.keyboard == KeyboardKind.CREATE_MODE
    assert len(submission_service.submitted) == 2
    assert {item.change_type for item in tracked} == {
        ChangeType.CHIPS.value,
        expected_change_type,
    }
    response = next(item for item in submission_service.submitted if item.change_type != "CHIPS")
    assert response.source_text == "response after chip"
    assert "Нужно обновить текст после чипса" in (
        response.formatted_change_description or ""
    )
    assert response.llm_check_status == LlmCheckStatus.SKIPPED.value
    assert draft.application_id in (response.formatted_change_description or "")
    assert llm_client.calls == []


@pytest.mark.asyncio
async def test_chips_unchanged_creates_only_chips_application(tmp_path):
    flow, repository, submission_service, _ = await make_flow(tmp_path)
    user_id = 1104
    await flow.start_single(user_id)
    await flow.select_direction(user_id, Direction.FL)
    await flow.select_answer_type(user_id, AnswerType.ROLLOUT)
    await flow.select_change_type(user_id, ChangeType.CHIPS)
    for value in ("Writer", "intent.chips", "reason", "before", "chip"):
        await flow.handle_text(user_id, value)
    await flow.select_chip_after_text_action(user_id, ChipAfterTextAction.UNCHANGED)
    await flow.handle_text(user_id, "context")

    await flow.submit(user_id)

    assert len(submission_service.submitted) == 1
    assert submission_service.submitted[0].change_type == ChangeType.CHIPS.value
    assert len(await repository.list_submitted_applications()) == 1


@pytest.mark.asyncio
async def test_chips_uses_default_values_in_chips_order(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    user_id = 104
    await repository.save_user_setting(user_id, "default_scriptwriter", "Default writer")
    await repository.save_user_setting(user_id, "default_intent", "default.intent")
    await flow.start_single(user_id)
    await flow.select_direction(user_id, Direction.FL)
    await flow.select_answer_type(user_id, AnswerType.ROLLOUT)
    await flow.select_change_type(user_id, ChangeType.CHIPS)

    response = await flow.use_default_scriptwriter(user_id)
    assert response.draft.current_step == Step.INTENT
    response = await flow.use_default_intent(user_id)
    assert response.draft.current_step == Step.REASON


@pytest.mark.asyncio
async def test_legacy_chips_draft_resumes_at_first_new_required_field(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    user_id = 105
    await flow.start_single(user_id)
    await repository.save_answer(user_id, FieldName.DIRECTION.value, Direction.FL.value)
    await repository.save_answer(user_id, FieldName.ANSWER_TYPE.value, AnswerType.ROLLOUT.value)
    await repository.save_answer(user_id, FieldName.CHANGE_TYPE.value, ChangeType.CHIPS.value)
    await repository.save_answer(user_id, FieldName.SCRIPTWRITER.value, "Writer")
    await repository.save_answer(user_id, FieldName.INTENT.value, "intent")
    await repository.save_answer(user_id, FieldName.REASON.value, "reason")
    await repository.set_step(user_id, Step.SOURCE_TEXT)

    response = await flow.continue_existing(user_id)
    draft = await repository.get_by_user_id(user_id)

    assert draft is not None
    assert response.draft.current_step == Step.CHIP_TEXT_BEFORE
    assert draft.current_step == Step.CHIP_TEXT_BEFORE


@pytest.mark.asyncio
async def test_urgent_requires_change_type(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await flow.start_single(102)
    await flow.select_direction(102, Direction.FL)
    response = await flow.select_answer_type(102, AnswerType.URGENT)
    draft = await repository.get_by_user_id(102)

    assert response.keyboard == KeyboardKind.CHANGE_TYPE
    assert draft is not None
    assert draft.current_step == Step.CHANGE_TYPE
    assert not draft.change_type


@pytest.mark.asyncio
async def test_integration_clears_change_type(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await flow.start_single(106)
    await flow.select_direction(106, Direction.FL)
    await repository.save_answer(106, FieldName.CHANGE_TYPE.value, ChangeType.ADD.value)

    response = await flow.select_answer_type(106, AnswerType.INTEGRATION)
    draft = await repository.get_by_user_id(106)

    assert response.keyboard == KeyboardKind.STEP
    assert draft is not None
    assert draft.current_step == Step.INTENT
    assert not draft.change_type


@pytest.mark.asyncio
async def test_urgent_chips_skips_gigachat_and_uses_chips_fields(tmp_path):
    flow, repository, _, llm_client = await make_flow(tmp_path)
    user_id = 107
    await flow.start_single(user_id)
    await flow.select_direction(user_id, Direction.FL)
    await flow.select_answer_type(user_id, AnswerType.URGENT)
    await flow.select_change_type(user_id, ChangeType.CHIPS)
    await flow.handle_text(user_id, "Writer")
    await flow.handle_text(user_id, "urgent.chips")
    await flow.handle_text(user_id, "Reason")
    await flow.handle_text(user_id, "Before")
    await flow.handle_text(user_id, "Chip")
    await flow.select_chip_after_text_action(user_id, ChipAfterTextAction.UNCHANGED)
    response = await flow.handle_text(user_id, "After")

    draft = await repository.get_by_user_id(user_id)
    assert response.keyboard == KeyboardKind.REVIEW
    assert draft is not None
    assert draft.answer_type == AnswerType.URGENT.value
    assert draft.change_type == ChangeType.CHIPS.value
    assert draft.llm_check_status == LlmCheckStatus.SKIPPED.value
    assert draft.is_urgent is True
    assert llm_client.calls == []


@pytest.mark.asyncio
async def test_urgent_add_keeps_gigachat_and_shows_change_type(tmp_path):
    flow, repository, _, llm_client = await make_flow(tmp_path)
    user_id = 109
    await flow.start_single(user_id)
    await flow.select_direction(user_id, Direction.FL)
    await flow.select_answer_type(user_id, AnswerType.URGENT)
    response = await flow.select_change_type(user_id, ChangeType.ADD)
    assert response.draft is not None
    assert response.draft.current_step == Step.INTENT
    await flow.handle_text(user_id, "urgent.add")
    await flow.handle_text(user_id, "Writer")
    await flow.handle_text(user_id, "Client case")
    assert await flow.should_show_llm_processing(user_id) is True
    await flow.handle_text(user_id, "Change description")
    response = await flow.handle_text(user_id, "Source answer")

    draft = await repository.get_by_user_id(user_id)
    assert draft is not None
    assert response.keyboard == KeyboardKind.REVIEW
    assert draft.llm_check_status == LlmCheckStatus.COMPLETE.value
    assert len(llm_client.calls) == 1
    assert "Тип изменения:</b> ADD" in response.text


@pytest.mark.asyncio
async def test_old_urgent_draft_without_change_type_resumes_at_selection(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    user_id = 108
    await flow.start_single(user_id)
    await repository.save_answer(user_id, FieldName.DIRECTION.value, Direction.FL.value)
    await repository.save_answer(
        user_id,
        FieldName.ANSWER_TYPE.value,
        AnswerType.URGENT.value,
    )
    await repository.set_step(user_id, Step.INTENT)

    response = await flow.continue_existing(user_id)

    assert response.keyboard == KeyboardKind.CHANGE_TYPE
    assert response.draft is not None
    assert response.draft.current_step == Step.CHANGE_TYPE


@pytest.mark.asyncio
async def test_regular_draft_keeps_generic_change_description_label(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await flow.start_single(109)
    draft = await repository.get_by_user_id(109)

    assert draft is not None
    missing = flow._missing_required_fields(draft)
    assert "Суть изменений" in missing
    assert "Суть изменений текста после чипса" not in missing


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
    assert f"ID заявки: {draft.application_id}" in response.text
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
async def test_urgent_application_enqueues_editor_notification(tmp_path):
    repository = DraftRepository(str(tmp_path / "test.db"))
    await repository.init()
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        urgent_editor_notifications_enabled=True,
        editor_urgent_chat_id=-100123456,
    )

    await fill_urgent_to_review(flow, 14)
    draft = await repository.get_by_user_id(14)
    assert draft is not None

    await flow.submit(14)

    outbox = await repository.list_notification_outbox()
    assert len(outbox) == 1
    assert outbox[0].telegram_user_id == -100123456
    assert outbox[0].event_type == "urgent-editor-application-created"
    assert outbox[0].dedupe_key.startswith(
        f"urgent-editor-application-created:{draft.application_id}:"
    )
    assert "<b>Направление:</b>" in outbox[0].html
    assert "Urgent Scriptwriter" in outbox[0].html
    assert "urgent.intent" in outbox[0].html
    assert "Открыть заявку</a>" in outbox[0].html


@pytest.mark.asyncio
async def test_non_urgent_application_does_not_enqueue_editor_notification(tmp_path):
    repository = DraftRepository(str(tmp_path / "test.db"))
    await repository.init()
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        urgent_editor_notifications_enabled=True,
        editor_urgent_chat_id=-100123456,
    )

    await fill_to_review(flow, 15)
    await flow.submit(15)

    assert await repository.list_notification_outbox() == []


@pytest.mark.asyncio
async def test_urgent_editor_notification_is_deduplicated_on_retry(tmp_path):
    repository = DraftRepository(str(tmp_path / "test.db"))
    await repository.init()
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        urgent_editor_notifications_enabled=True,
        editor_urgent_chat_id=-100123456,
    )

    await fill_urgent_to_review(flow, 16)
    await flow.submit(16)
    await flow.submit(16)

    outbox = await repository.list_notification_outbox()
    assert len(outbox) == 1


@pytest.mark.asyncio
async def test_submit_rate_limit_keeps_draft_on_review_for_retry(tmp_path):
    repository = DraftRepository(str(tmp_path / "test.db"))
    await repository.init()
    flow = ApplicationFlow(repository, FakeLlmClient(), RateLimitedSubmissionService())

    await fill_to_review(flow, 12)
    response = await flow.submit(12)
    draft = await repository.get_by_user_id(12)

    assert response.keyboard == KeyboardKind.REVIEW
    assert "Google API временно перегружен" in response.text
    assert "Повторите отправку через 2-3 минуты" in response.text
    assert draft is not None
    assert draft.current_step == Step.REVIEW
    assert draft.submission_state == "FAILED"
    assert draft.application_id


@pytest.mark.asyncio
async def test_review_contains_emoji_labels_and_bold_html(tmp_path):
    flow, _, _, _ = await make_flow(tmp_path)
    await fill_to_review(flow, 22)

    response = await flow.show_review(22)

    assert response.parse_mode == "HTML"
    assert "🎯 <b>Интент:</b>" in response.text
    assert "👤 <b>Закрепленный сценарист:</b>" in response.text
    assert "📝 <b>Кейс или сообщения клиента:</b>" in response.text
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


def test_review_keyboard_uses_client_case_label():
    keyboard = build_keyboard(KeyboardKind.EDIT_MENU_ROLLOUT)

    assert keyboard is not None
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "Кейс/сообщения" in labels
    assert "Причина" not in labels


def test_llm_recommendation_keyboards_use_expected_callbacks():
    recommendation = build_keyboard(KeyboardKind.LLM_RECOMMENDATION)
    reasons = build_keyboard(KeyboardKind.LLM_SKIP_REASON)

    assert recommendation is not None
    assert [
        (button.text, button.callback_data)
        for row in recommendation.inline_keyboard
        for button in row
    ] == [
        ("Дополнить", "app:llm:add"),
        ("Пропустить", "app:llm:skip"),
    ]
    assert reasons is not None
    assert [
        (button.text, button.callback_data)
        for row in reasons.inline_keyboard
        for button in row
    ] == [
        ("Замечание можно пропустить", "app:llm:skip_reason:optional"),
        ("Проверка ошиблась", "app:llm:skip_reason:incorrect"),
        ("Рекомендация непонятна", "app:llm:skip_reason:unclear"),
        ("Назад", "app:back"),
    ]






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
    assert "новый кейс или сообщения клиента" in prompt.text
    assert response.keyboard == KeyboardKind.REVIEW
    assert draft is not None
    assert draft.current_step == Step.REVIEW
    assert draft.reason == "Юридическое требование"


@pytest.mark.asyncio
async def test_edit_source_text_prompt_uses_proposed_text_label(tmp_path):
    flow, _, _, _ = await make_flow(tmp_path)
    await fill_to_review(flow, 151)

    prompt = await flow.select_edit_field(151, FieldName.SOURCE_TEXT)

    assert "Введите новый предлагаемый текст" in prompt.text
    assert "Введите новый исходный текст" not in prompt.text


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

    assert response.keyboard == KeyboardKind.CREATE_MODE
    assert "не настроены" in response.text
    assert "xlsx" not in response.text.lower()
    assert draft is None


@pytest.mark.asyncio
async def test_legacy_bulk_callback_is_disabled_without_database_writes(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)

    response = await flow.confirm_bulk_batch_filled(171, "BATCH-ABC12345")

    assert response.keyboard == KeyboardKind.BULK_MENU
    assert "Старый формат" in response.text
    assert await repository.get_active_bulk_reservation(171) is None


@pytest.mark.asyncio
async def test_resume_clears_retired_bulk_pending_action(tmp_path):
    flow, repository, _, _ = await make_flow(tmp_path)
    await repository.save_user_setting(
        171,
        "pending_action",
        "create_bulk_direction:legacy-request",
    )

    response = await flow.resume_from_notification(171)
    settings = await repository.get_user_settings(171)

    assert response.keyboard == KeyboardKind.CREATE_MODE
    assert settings.pending_action is None










@pytest.mark.asyncio
async def test_bulk_reservation_flow_creates_rows_after_confirmation(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_reservation_flow.db"))
    await repository.init()
    service = FakeBulkReservationService(repository)
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        bulk_reservation_service=service,
        bulk_max_rows=5,
    )

    direction = await flow.create_bulk_batch(180)
    reservation_id = direction.keyboard_payload
    assert reservation_id is not None

    target = await flow.select_bulk_direction(180, reservation_id, Direction.FL)
    assert target.keyboard == KeyboardKind.BULK_TARGET

    change_type = await flow.select_bulk_target(
        180,
        reservation_id,
        target_kind=BulkTargetKind.ROLLOUT,
    )
    assert change_type.keyboard == KeyboardKind.BULK_CHANGE_TYPE

    count = await flow.select_bulk_change_type(180, reservation_id, ChangeType.ADD)
    assert count.keyboard == KeyboardKind.STEP

    confirmation = await flow.handle_text(180, "5")
    assert confirmation.keyboard == KeyboardKind.BULK_COUNT_CONFIRM

    created = await flow.confirm_bulk_reservation_creation(180, reservation_id)
    assert created.keyboard == KeyboardKind.BULK_RESERVATION_CREATED
    assert created.keyboard_payload == reservation_id
    assert len(service.calls) == 1

    reservation = await repository.get_bulk_reservation(reservation_id)
    assert reservation is not None
    assert reservation.state == BulkReservationState.CREATED.value
    assert reservation.start_row == 10
    assert reservation.end_row == 14


@pytest.mark.asyncio
async def test_bulk_reservation_count_validation_uses_configured_limit(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_reservation_count.db"))
    await repository.init()
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        bulk_reservation_service=FakeBulkReservationService(),
        bulk_max_rows=3,
    )

    response = await flow.create_bulk_batch(180)
    reservation_id = response.keyboard_payload
    assert reservation_id is not None
    await flow.select_bulk_direction(180, reservation_id, Direction.FL)
    await flow.select_bulk_target(
        180,
        reservation_id,
        target_kind=BulkTargetKind.URGENT,
    )
    await flow.select_bulk_change_type(180, reservation_id, ChangeType.EDIT)

    invalid = await flow.handle_text(180, "4")

    assert invalid.keyboard == KeyboardKind.STEP
    assert "1 до 3" in invalid.text


@pytest.mark.asyncio
async def test_bulk_reservation_double_confirm_while_creating_does_not_insert_again(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_reservation_creating.db"))
    await repository.init()
    service = FakeBulkReservationService()
    flow = ApplicationFlow(
        repository,
        FakeLlmClient(),
        InMemorySubmissionService(),
        bulk_reservation_service=service,
        bulk_max_rows=5,
    )
    response = await flow.create_bulk_batch(180)
    reservation_id = response.keyboard_payload
    assert reservation_id is not None
    await flow.select_bulk_direction(180, reservation_id, Direction.FL)
    await flow.select_bulk_target(180, reservation_id, target_kind=BulkTargetKind.ROLLOUT)
    await flow.select_bulk_change_type(180, reservation_id, ChangeType.ADD)
    await flow.handle_text(180, "3")
    await repository.claim_bulk_reservation_creation(
        reservation_id,
        stale_after_seconds=600,
    )

    result = await flow.confirm_bulk_reservation_creation(180, reservation_id)

    assert service.calls == []
    assert result.keyboard == KeyboardKind.BULK_COUNT_CONFIRM
    assert "создаются" in result.text


def test_bulk_integration_change_type_keyboard_hides_chips():
    keyboard = build_keyboard(KeyboardKind.BULK_CHANGE_TYPE, "RES-123:integration")
    labels = [
        button.text
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert ChangeType.ADD.value in labels
    assert ChangeType.EDIT.value in labels
    assert ChangeType.CHIPS.value not in labels


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
async def test_recommendation_can_be_applied_with_one_revision(tmp_path):
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

    assert "Дополните поле: укажите, какой результат" in first_response.text
    assert draft is not None
    assert draft.current_step == Step.CHANGE_DESCRIPTION_RECOMMENDATION
    assert first_response.keyboard == KeyboardKind.LLM_RECOMMENDATION

    revision_prompt = await flow.accept_llm_recommendation(18)
    assert revision_prompt.keyboard == KeyboardKind.STEP
    assert "только текст, который нужно дописать" in revision_prompt.text
    assert "Поменять срок" in revision_prompt.text
    second_response = await flow.handle_text(
        18,
        "Нужно указать 5 рабочих дней",
    )
    draft = await repository.get_by_user_id(18)

    assert "Пришлите предлагаемый текст" in second_response.text
    assert draft is not None
    assert draft.current_step == Step.SOURCE_TEXT
    assert draft.clarification_count == 1
    assert draft.raw_change_description == "Поменять срок\nНужно указать 5 рабочих дней"
    assert draft.formatted_change_description == draft.raw_change_description
    assert draft.llm_check_status == LlmCheckStatus.COMPLETE.value
    assert len(llm_client.calls) == 2
    assert llm_client.calls[0].direction == Direction.FL.value
    assert llm_client.calls[0].answer_type == AnswerType.ROLLOUT.value
    assert llm_client.calls[0].change_type == ChangeType.ADD.value
    assert (
        llm_client.calls[1].raw_change_description
        == "Поменять срок\nНужно указать 5 рабочих дней"
    )
    assert llm_client.calls[1].initial_change_description == "Поменять срок"
    assert llm_client.calls[1].iteration_number == 2
    events = await repository.list_application_events(application_id=draft.application_id)
    events = [event for event in events if event.event_type.startswith("llm_")]
    assert [event.event_type for event in reversed(events)] == [
        "llm_check_completed",
        "llm_clarification_submitted",
        "llm_check_completed",
    ]
    clarification_metadata = json.loads(events[1].metadata_json or "{}")
    repeated_check_metadata = json.loads(events[0].metadata_json or "{}")
    assert clarification_metadata == {
        "schema_version": 1,
        "clarification_number": 1,
        "trigger": "create",
    }
    assert repeated_check_metadata["stage"] == "clarification"
    assert repeated_check_metadata["trigger"] == "create"


@pytest.mark.asyncio
async def test_second_recommendation_continues_with_attention_status(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=False,
                blocking_problem="missing_change_content",
                clarification_instruction="что именно необходимо изменить",
                check_result="recommendation",
                gap_code="missing_change_content",
                missing_detail="что именно необходимо изменить",
            ),
            LlmResult(
                is_complete=False,
                blocking_problem="missing_change_content",
                clarification_instruction="какой итоговый результат требуется",
                check_result="recommendation",
                gap_code="missing_change_content",
                missing_detail="какой итоговый результат требуется",
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

    await flow.accept_llm_recommendation(19)
    response = await flow.handle_text(19, "Сделать лучше")
    draft = await repository.get_by_user_id(19)

    assert (
        "Может быть, тут не хватает информации о том, "
        "какой итоговый результат требуется"
    ) in response.text
    assert draft is not None
    assert draft.current_step == Step.SOURCE_TEXT
    assert draft.llm_check_status == LlmCheckStatus.NEEDS_ATTENTION.value
    assert draft.formatted_change_description == "Поменять текст\nСделать лучше"
    assert draft.raw_change_description == "Поменять текст\nСделать лучше"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["optional", "incorrect", "unclear"])
async def test_recommendation_can_be_skipped_with_reason(tmp_path, reason):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=False,
                blocking_problem="missing_change_content",
                clarification_instruction="Может быть, тут не хватает содержания изменения.",
                check_result="recommendation",
                gap_code="missing_change_content",
                recommendation="Может быть, тут не хватает содержания изменения.",
                raw_response='{"check_result":"recommendation"}',
            )
        ]
    )
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await flow.start_single(191)
    await flow.select_direction(191, Direction.FL)
    await flow.select_answer_type(191, AnswerType.ROLLOUT)
    await flow.select_change_type(191, ChangeType.ADD)
    await flow.handle_text(191, "intent.change_limit")
    await flow.handle_text(191, "Иван Иванов")
    await flow.handle_text(191, "Изменились условия продукта")
    await flow.handle_text(191, "Обновить ответ")

    reason_prompt = await flow.skip_llm_recommendation(191)
    response = await flow.select_llm_skip_reason(191, reason)
    draft = await repository.get_by_user_id(191)

    assert reason_prompt.keyboard == KeyboardKind.LLM_SKIP_REASON
    assert response.keyboard == KeyboardKind.STEP
    assert draft is not None
    assert draft.current_step == Step.SOURCE_TEXT
    assert draft.llm_check_status == LlmCheckStatus.NEEDS_ATTENTION.value
    stored = await repository.get_llm_recommendation_process(draft.application_id or "")
    assert stored is not None
    assert stored.state == "skipped"
    process = json.loads(stored.process_json)
    assert process["state"] == "skipped"
    assert process["final_outcome"] == f"skipped_{reason}"
    assert process["actions"][-1]["reason"] == reason
    assert len(process["iterations"]) == 1


@pytest.mark.asyncio
async def test_recommendation_process_uses_one_row_for_two_checks(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=False,
                blocking_problem="missing_change_content",
                clarification_instruction="какой срок необходимо указать",
                check_result="recommendation",
                gap_code="missing_change_content",
                missing_detail="какой срок необходимо указать",
                raw_response=(
                    '{"check_result":"recommendation",'
                    '"gap_code":"missing_change_content",'
                    '"missing_detail":"какой срок необходимо указать"}'
                ),
            ),
            LlmResult(
                is_complete=True,
                blocking_problem=None,
                clarification_instruction=None,
                check_result="ok",
                raw_response='{"check_result":"ok","gap_code":null,"missing_detail":null}',
            ),
        ]
    )
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await flow.start_single(192)
    await flow.select_direction(192, Direction.FL)
    await flow.select_answer_type(192, AnswerType.ROLLOUT)
    await flow.select_change_type(192, ChangeType.ADD)
    await flow.handle_text(192, "intent.change_limit")
    await flow.handle_text(192, "Иван Иванов")
    await flow.handle_text(192, "Изменились условия продукта")
    first_response = await flow.handle_text(192, "Обновить ответ")
    await flow.accept_llm_recommendation(192)
    await flow.handle_text(192, "Добавить в ответ срок пять дней")
    draft = await repository.get_by_user_id(192)

    assert draft is not None
    stored = await repository.get_llm_recommendation_process(draft.application_id or "")
    assert stored is not None
    assert stored.state == "completed"
    process = json.loads(stored.process_json)
    assert process["state"] == "completed"
    assert process["final_outcome"] == "ok"
    assert len(process["iterations"]) == 2
    assert process["iterations"][0]["raw_response"]
    assert process["iterations"][1]["raw_response"]
    assert process["iterations"][0]["response"] == {
        "check_result": "recommendation",
        "gap_code": "missing_change_content",
        "recommendation": None,
        "missing_detail": "какой срок необходимо указать",
    }
    assert (
        first_response.text
        == "Может быть, тут не хватает информации о том, какой срок необходимо "
        "указать\n\nХотите дополнить описание или пропустить рекомендацию?"
    )
    assert llm_client.calls[1].previous_gap_code == "missing_change_content"
    assert llm_client.calls[1].previous_missing_detail == "какой срок необходимо указать"
    assert process["actions"][0]["text_changed"] is True
    assert process["current_text"] == "Обновить ответ\nДобавить в ответ срок пять дней"
    assert process["iterations"][1]["input"]["current_text"] == process["current_text"]
    assert draft.raw_change_description == process["current_text"]
    assert process["actions"][0]["text_delta_chars"] == len(
        "\nДобавить в ответ срок пять дней"
    )
    async with repository._connection() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM llm_recommendation_processes WHERE application_id = ?",
            (draft.application_id,),
        )
        assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_unchanged_revision_still_ends_after_second_check(tmp_path):
    recommendation = LlmResult(
        is_complete=False,
        blocking_problem="missing_change_content",
        clarification_instruction="Уточните содержание изменения.",
        check_result="recommendation",
        gap_code="missing_change_content",
        recommendation="Уточните содержание изменения.",
    )
    llm_client = FakeLlmClient([recommendation, recommendation])
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await flow.start_single(193)
    await flow.select_direction(193, Direction.FL)
    await flow.select_answer_type(193, AnswerType.ROLLOUT)
    await flow.select_change_type(193, ChangeType.ADD)
    await flow.handle_text(193, "intent.change_limit")
    await flow.handle_text(193, "Иван Иванов")
    await flow.handle_text(193, "Изменились условия продукта")
    await flow.handle_text(193, "Обновить ответ")

    await flow.accept_llm_recommendation(193)
    await flow.handle_text(193, "Обновить ответ")
    stale_response = await flow.accept_llm_recommendation(193)
    draft = await repository.get_by_user_id(193)

    assert draft is not None
    assert draft.current_step == Step.SOURCE_TEXT
    assert draft.llm_check_status == LlmCheckStatus.NEEDS_ATTENTION.value
    assert len(llm_client.calls) == 2
    assert "уже обработана" in stale_response.text
    stored = await repository.get_llm_recommendation_process(draft.application_id or "")
    assert stored is not None
    process = json.loads(stored.process_json)
    assert len(process["iterations"]) == 2
    assert process["actions"][0]["text_changed"] is False
    assert process["final_outcome"] == "recommendation_after_limit"


@pytest.mark.asyncio
async def test_cancel_preserves_recommendation_process(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=False,
                blocking_problem="missing_application_context",
                clarification_instruction="Уточните контекст заявки.",
                check_result="recommendation",
                gap_code="missing_application_context",
                recommendation="Уточните контекст заявки.",
            )
        ]
    )
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await flow.start_single(194)
    await flow.select_direction(194, Direction.FL)
    await flow.select_answer_type(194, AnswerType.ROLLOUT)
    await flow.select_change_type(194, ChangeType.ADD)
    await flow.handle_text(194, "intent.change_limit")
    await flow.handle_text(194, "Иван Иванов")
    await flow.handle_text(194, "Изменились условия продукта")
    await flow.handle_text(194, "Обновить ответ")
    draft = await repository.get_by_user_id(194)
    assert draft is not None
    application_id = draft.application_id or ""

    await flow.cancel(194)

    assert await repository.get_by_user_id(194) is None
    stored = await repository.get_llm_recommendation_process(application_id)
    assert stored is not None
    process = json.loads(stored.process_json)
    assert process["state"] == "cancelled"
    assert process["final_outcome"] == "cancelled"
    assert process["actions"][-1]["action"] == "cancelled"


@pytest.mark.asyncio
async def test_new_application_preserves_restarted_recommendation_process(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=False,
                blocking_problem="missing_application_context",
                clarification_instruction="Уточните контекст заявки.",
                check_result="recommendation",
                gap_code="missing_application_context",
                recommendation="Уточните контекст заявки.",
            )
        ]
    )
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await flow.start_single(195)
    await flow.select_direction(195, Direction.FL)
    await flow.select_answer_type(195, AnswerType.ROLLOUT)
    await flow.select_change_type(195, ChangeType.ADD)
    await flow.handle_text(195, "intent.change_limit")
    await flow.handle_text(195, "Иван Иванов")
    await flow.handle_text(195, "Изменились условия продукта")
    await flow.handle_text(195, "Обновить ответ")
    previous = await repository.get_by_user_id(195)
    assert previous is not None
    previous_application_id = previous.application_id or ""

    await flow.start_single(195, force=True)

    current = await repository.get_by_user_id(195)
    assert current is not None
    assert current.application_id != previous_application_id
    stored = await repository.get_llm_recommendation_process(previous_application_id)
    assert stored is not None
    process = json.loads(stored.process_json)
    assert process["state"] == "cancelled"
    assert process["final_outcome"] == "restarted"
    assert process["actions"][-1]["action"] == "restarted"


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
    events = await repository.list_application_events(
        application_id=draft.application_id,
        event_type="llm_check_completed",
    )
    assert len(events) == 1
    assert events[0].new_value == "passed"
    assert events[0].application_id == draft.application_id
    assert events[0].event_at.endswith("+00:00")
    metadata = json.loads(events[0].metadata_json or "{}")
    assert metadata["stage"] == "initial"
    assert metadata["trigger"] == "create"
    assert metadata["prompt_version"] == "unknown"
    assert metadata["prompt_hash"] is None


@pytest.mark.asyncio
async def test_llm_error_keeps_user_text_and_continues(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=True,
                blocking_problem="Ошибка GigaChat: ConnectError",
                clarification_instruction=None,
                telemetry=LlmTelemetry(error_kind="network"),
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
    assert "GigaChat временно не смог корректно проверить описание" in response.text
    assert "Продолжаем заполнение заявки" in response.text
    events = await repository.list_application_events(
        application_id=draft.application_id,
        event_type="llm_check_completed",
    )
    assert len(events) == 1
    assert events[0].new_value == "technical_fallback"
    assert json.loads(events[0].metadata_json or "{}")["error_kind"] == "network"


@pytest.mark.asyncio
async def test_incomplete_check_keeps_score_empty(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=False,
                blocking_problem=(
                    "Критерий 1, правило 1.2: не указано конкретное изменение"
                ),
                clarification_instruction=(
                    "Дополните поле: укажите изменяемый фрагмент и требуемый результат."
                ),
                telemetry=LlmTelemetry(
                    prompt_version="v3",
                    prompt_hash="abc123def456",
                    model="GigaChat-2-Max",
                    duration_ms=321,
                    response_attempts=2,
                    validation_retries=1,
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
    assert draft.current_step == Step.CHANGE_DESCRIPTION_RECOMMENDATION
    assert draft.llm_check_status == LlmCheckStatus.NEEDS_ATTENTION.value
    assert draft.llm_score is None
    events = await repository.list_application_events(
        application_id=draft.application_id,
        event_type="llm_check_completed",
    )
    assert len(events) == 1
    assert events[0].new_value == "needs_clarification"
    metadata = json.loads(events[0].metadata_json or "{}")
    assert metadata == {
        "schema_version": 1,
        "stage": "initial",
        "trigger": "create",
        "prompt_version": "v3",
        "prompt_hash": "abc123def456",
        "model": "GigaChat-2-Max",
        "blocking_rule": "1.2",
        "gap_code": None,
        "duration_ms": 321,
        "response_attempts": 2,
        "validation_retries": 1,
        "error_kind": None,
    }
    serialized = events[0].metadata_json or ""
    assert "Обновить текст" not in serialized
    assert "Дополните поле" not in serialized


@pytest.mark.asyncio
async def test_v61_change_action_event_uses_gap_code_without_legacy_rule(tmp_path):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=False,
                blocking_problem="missing_change_action",
                clarification_instruction="что необходимо сделать с информацией о комиссии",
                check_result="recommendation",
                gap_code="missing_change_action",
                missing_detail="что необходимо сделать с информацией о комиссии",
                telemetry=LlmTelemetry(
                    prompt_version="v6.1",
                    prompt_hash="def456abc123",
                    model="GigaChat",
                ),
            )
        ]
    )
    flow, repository, _, _ = await make_flow(tmp_path, llm_client)
    await flow.start_single(251)
    await flow.select_direction(251, Direction.FL)
    await flow.select_answer_type(251, AnswerType.ROLLOUT)
    await flow.select_change_type(251, ChangeType.ADD)
    await flow.handle_text(251, "intent.change_limit")
    await flow.handle_text(251, "Иван Иванов")
    await flow.handle_text(251, "Клиент спрашивает о комиссии")
    await flow.handle_text(251, "Информация о комиссии")
    draft = await repository.get_by_user_id(251)

    assert draft is not None
    events = await repository.list_application_events(
        application_id=draft.application_id,
        event_type="llm_check_completed",
    )
    metadata = json.loads(events[0].metadata_json or "{}")
    assert metadata["prompt_version"] == "v6.1"
    assert metadata["gap_code"] == "missing_change_action"
    assert metadata["blocking_rule"] is None


@pytest.mark.asyncio
async def test_edit_change_description_does_not_run_llm_again(tmp_path):
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
    assert len(llm_client.calls) == 1
    events = await repository.list_application_events(
        application_id=draft.application_id,
        event_type="llm_check_completed",
    )
    assert len(events) == 1
    assert json.loads(events[0].metadata_json or "{}")["trigger"] == "create"


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_version", ["v6.1", "v6.2"])
async def test_can_show_gigachat_json_response_in_bot_message(
    tmp_path,
    prompt_version,
):
    llm_client = FakeLlmClient(
        [
            LlmResult(
                is_complete=True,
                blocking_problem=None,
                clarification_instruction=None,
                check_result="ok",
                telemetry=LlmTelemetry(prompt_version=prompt_version),
            )
        ]
    )
    flow, _, _, _ = await make_flow_with_json(tmp_path, llm_client)
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
    assert '"check_result": "ok"' in response.text
    assert '"gap_code": null' in response.text
    assert '"missing_detail": null' in response.text
    assert '"recommendation"' not in response.text
