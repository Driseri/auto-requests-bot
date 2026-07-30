from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from secrets import token_hex


class Step(StrEnum):
    DIRECTION = "direction"
    ANSWER_TYPE = "answer_type"
    CHANGE_TYPE = "change_type"
    INTENT = "intent"
    SCRIPTWRITER = "scriptwriter"
    REASON = "reason"
    CHANGE_DESCRIPTION = "change_description"
    CHANGE_DESCRIPTION_CLARIFICATION = "change_description_clarification"
    CHANGE_DESCRIPTION_RECOMMENDATION = "change_description_recommendation"
    CHANGE_DESCRIPTION_REVISION = "change_description_revision"
    CHANGE_DESCRIPTION_SKIP_REASON = "change_description_skip_reason"
    SOURCE_TEXT = "source_text"
    CHIP_TEXT_BEFORE = "chip_text_before"
    CHIP_TEXT = "chip_text"
    CHIP_AFTER_TEXT_ACTION = "chip_after_text_action"
    CHIP_RESPONSE_CHANGE_DESCRIPTION = "chip_response_change_description"
    CHIP_TEXT_AFTER = "chip_text_after"
    URGENCY = "urgency"
    PRIORITY = "priority"
    REVIEW = "review"
    COMPLETED = "completed"
    EDIT_DIRECTION = "edit_direction"
    EDIT_ANSWER_TYPE = "edit_answer_type"
    EDIT_CHANGE_TYPE = "edit_change_type"
    EDIT_INTENT = "edit_intent"
    EDIT_SCRIPTWRITER = "edit_scriptwriter"
    EDIT_REASON = "edit_reason"
    EDIT_CHANGE_DESCRIPTION = "edit_change_description"
    EDIT_SOURCE_TEXT = "edit_source_text"
    EDIT_CHIP_TEXT_BEFORE = "edit_chip_text_before"
    EDIT_CHIP_TEXT = "edit_chip_text"
    EDIT_CHIP_AFTER_TEXT_ACTION = "edit_chip_after_text_action"
    EDIT_CHIP_RESPONSE_CHANGE_DESCRIPTION = "edit_chip_response_change_description"
    EDIT_CHIP_TEXT_AFTER = "edit_chip_text_after"
    EDIT_URGENCY = "edit_urgency"
    EDIT_PRIORITY = "edit_priority"


class Priority(StrEnum):
    HIGH = "высокий"
    LOW = "низкий"


class Direction(StrEnum):
    FL = "ФЛ"
    SME = "SME"
    AI = "АИ"
    VOICEBOT = "VoiceBot"
    COLLECTION = "Collection"


DIRECTION_DISPLAY_LABELS = {
    Direction.FL.value: "ФЛ-chatbot",
    Direction.SME.value: "SME-chatbot",
    Direction.AI.value: "АИ-chatbot",
    Direction.VOICEBOT.value: "VoiceBot-chatbot",
    Direction.COLLECTION.value: "Collection-chatbot",
}


def direction_display_label(direction: str | None) -> str:
    if not direction:
        return ""
    return DIRECTION_DISPLAY_LABELS.get(direction, direction)


def direction_value_from_display_label(value: str) -> str:
    text = value.strip()
    for direction, label in DIRECTION_DISPLAY_LABELS.items():
        if text == label:
            return direction
    return text


class AnswerType(StrEnum):
    ROLLOUT = "Раскатка"
    URGENT = "Срочные"
    INTEGRATION = "Интеграция"


class ChangeType(StrEnum):
    ADD = "ADD"
    EDIT = "EDIT"
    CHIPS = "CHIPS"

    @classmethod
    def normalize(cls, value: str | None) -> ChangeType | None:
        normalized = (value or "").strip().upper()
        if normalized == "CHIP":
            normalized = cls.CHIPS.value
        try:
            return cls(normalized)
        except ValueError:
            return None


class ChipAfterTextAction(StrEnum):
    ADD = "ADD"
    EDIT = "EDIT"
    UNCHANGED = "UNCHANGED"

    @property
    def label(self) -> str:
        return {
            self.ADD: "Добавляется новый текст",
            self.EDIT: "Изменяется текст",
            self.UNCHANGED: "Остается без изменений",
        }[self]


class ApplicationType(StrEnum):
    SINGLE = "Одиночная"


class BulkReservationState(StrEnum):
    AWAITING_DIRECTION = "AWAITING_DIRECTION"
    AWAITING_TARGET = "AWAITING_TARGET"
    AWAITING_CHANGE_TYPE = "AWAITING_CHANGE_TYPE"
    AWAITING_COUNT = "AWAITING_COUNT"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    CREATING = "CREATING"
    CREATED = "CREATED"
    REGISTERING = "REGISTERING"
    REGISTERED = "REGISTERED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class BulkTargetKind(StrEnum):
    ROLLOUT = "rollout"
    URGENT = "urgent"
    INTEGRATION = "integration"


class SubmissionState(StrEnum):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class ApplicationStatus(StrEnum):
    NEW = "Новая"
    IN_PROGRESS = "В работе"
    NEEDS_CLARIFICATION = "Нужны пояснения"
    FINAL_ANSWER_READY = "Итоговый ответ готов"
    ACCEPTED = "Принята"
    REJECTED = "Отклонена"
    POSTPONED = "Отложена"
    DELETION = "Удаление"

    # Backward-compatible aliases for old code/tests while the new model is rolled out.
    NEEDS_SCRIPTWRITER_RESPONSE = "Нужны пояснения"
    RESPONSE_RECEIVED = "Итоговый ответ готов"


class LlmCheckStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    COMPLETE = "complete"
    ERROR = "error"
    STUB_COMPLETE = "stub_complete"
    NEEDS_ATTENTION = "needs_attention"
    SKIPPED = "skipped"


class LlmCheckResult(StrEnum):
    OK = "ok"
    RECOMMENDATION = "recommendation"
    ERROR = "error"


class LlmGapCode(StrEnum):
    MISSING_NEW_ENTITY_CONTENT = "missing_new_entity_content"
    MISSING_CHANGE_CONTENT = "missing_change_content"
    MISSING_APPLICATION_CONTEXT = "missing_application_context"
    MISSING_CHANGE_RATIONALE = "missing_change_rationale"


class KeyboardKind(StrEnum):
    START = "start"
    CREATE_MODE = "create_mode"
    ACTIVE_DRAFT = "active_draft"
    DIRECTION = "direction"
    DIRECTION_WITH_DEFAULT = "direction_with_default"
    ANSWER_TYPE = "answer_type"
    CHANGE_TYPE = "change_type"
    CHIP_AFTER_TEXT_ACTION = "chip_after_text_action"
    STEP = "step"
    INTENT_STEP_WITH_DEFAULT = "intent_step_with_default"
    SCRIPTWRITER_STEP_WITH_DEFAULT = "scriptwriter_step_with_default"
    URGENCY = "urgency"
    PRIORITY = "priority"
    LLM_RECOMMENDATION = "llm_recommendation"
    LLM_SKIP_REASON = "llm_skip_reason"
    REVIEW = "review"
    EDIT_MENU = "edit_menu"
    EDIT_MENU_ROLLOUT = "edit_menu_rollout"
    EDIT_MENU_CHIPS = "edit_menu_chips"
    BULK_MENU = "bulk_menu"
    BULK_DIRECTION = "bulk_direction"
    BULK_TARGET = "bulk_target"
    BULK_CHANGE_TYPE = "bulk_change_type"
    BULK_COUNT_CONFIRM = "bulk_count_confirm"
    BULK_RESERVATION_CREATED = "bulk_reservation_created"
    BULK_RESERVATION_COMPLETED = "bulk_reservation_completed"
    DEFAULTS_MENU = "defaults_menu"
    DEFAULTS_BACK = "defaults_back"
    NOTIFICATION = "notification"
    NOTIFICATION_BULK_RESERVATION_BACK = "notification_bulk_reservation_back"
    NOTIFICATION_SINGLE_BACK = "notification_single_back"
    NONE = "none"


class NotificationOutboxState(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class DashboardOutboxState(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"


class DashboardEntityType(StrEnum):
    APPLICATION = "APPLICATION"


class StatusPollingState(StrEnum):
    ACTIVE = "ACTIVE"
    NOT_FOUND = "NOT_FOUND"


class FieldName(StrEnum):
    DIRECTION = "direction"
    ANSWER_TYPE = "answer_type"
    CHANGE_TYPE = "change_type"
    INTENT = "intent"
    SCRIPTWRITER = "scriptwriter"
    REASON = "reason"
    CHANGE_DESCRIPTION = "change_description"
    SOURCE_TEXT = "source_text"
    CHIP_TEXT_BEFORE = "chip_text_before"
    CHIP_TEXT = "chip_text"
    CHIP_AFTER_TEXT_ACTION = "chip_after_text_action"
    CHIP_TEXT_AFTER = "chip_text_after"
    URGENCY = "is_urgent"
    PRIORITY = "priority"


TEXT_FIELDS = {
    FieldName.DIRECTION.value,
    FieldName.ANSWER_TYPE.value,
    FieldName.INTENT.value,
    FieldName.SCRIPTWRITER.value,
    FieldName.REASON.value,
    "raw_change_description",
    "formatted_change_description",
    FieldName.SOURCE_TEXT.value,
    "source_text_formatting_json",
    FieldName.CHIP_TEXT_BEFORE.value,
    "chip_text_before_formatting_json",
    FieldName.CHIP_TEXT.value,
    "chip_text_formatting_json",
    FieldName.CHIP_AFTER_TEXT_ACTION.value,
    FieldName.CHIP_TEXT_AFTER.value,
    "chip_text_after_formatting_json",
    "application_type",
    "change_type",
    "author_name",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_application_id(now: datetime | None = None) -> str:
    return token_hex(4).upper()


@dataclass(slots=True)
class Draft:
    telegram_user_id: int
    current_step: Step
    application_id: str | None = None
    direction: str | None = None
    answer_type: str | None = None
    is_urgent: bool | None = None
    application_type: str = ApplicationType.SINGLE.value
    change_type: str | None = None
    author_name: str | None = None
    intent: str | None = None
    scriptwriter: str | None = None
    reason: str | None = None
    raw_change_description: str | None = None
    formatted_change_description: str | None = None
    source_text: str | None = None
    source_text_formatting_json: str | None = None
    chip_text_before: str | None = None
    chip_text_before_formatting_json: str | None = None
    chip_text: str | None = None
    chip_text_formatting_json: str | None = None
    chip_after_text_action: str | None = None
    chip_text_after: str | None = None
    chip_text_after_formatting_json: str | None = None
    priority: str | None = None
    llm_check_status: str = LlmCheckStatus.NOT_CHECKED.value
    llm_score: float | None = None
    clarification_count: int = 0
    submission_state: str = SubmissionState.DRAFT.value
    submission_started_at: str | None = None
    submission_spreadsheet_id: str | None = None
    submission_sheet_name: str | None = None
    submission_sheet_id: int | None = None
    submission_row_number: int | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_active(self) -> bool:
        return self.current_step != Step.COMPLETED


@dataclass(slots=True)
class UserSettings:
    telegram_user_id: int
    default_direction: str | None = None
    default_intent: str | None = None
    default_scriptwriter: str | None = None
    pending_action: str | None = None
    active_chat_id: int | None = None
    active_message_id: int | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class LlmContext:
    intent: str
    scriptwriter: str
    reason: str
    raw_change_description: str
    direction: str = ""
    answer_type: str = ""
    change_type: str = ""
    clarification_text: str = ""
    initial_change_description: str = ""
    previous_gap_code: str = ""
    previous_recommendation: str = ""
    iteration_number: int = 1


@dataclass(slots=True)
class LlmTelemetry:
    prompt_version: str = "unknown"
    prompt_hash: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    response_attempts: int | None = None
    validation_retries: int | None = None
    error_kind: str | None = None


@dataclass(slots=True)
class LlmResult:
    is_complete: bool
    blocking_problem: str | None
    clarification_instruction: str | None
    telemetry: LlmTelemetry | None = None
    check_result: str = LlmCheckResult.OK.value
    gap_code: str | None = None
    recommendation: str | None = None
    raw_response: str | None = None


@dataclass(slots=True)
class SubmissionResult:
    success: bool
    message: str
    spreadsheet_id: str | None = None
    sheet_id: int | None = None
    sheet_name: str | None = None
    row_number: int | None = None
    row_link: str | None = None
    submitted_at: str | None = None


@dataclass(slots=True)
class LinkedSubmissionResult:
    success: bool
    message: str
    chips_result: SubmissionResult | None = None
    response_result: SubmissionResult | None = None
    row_shifts: tuple[tuple[str, int, int, int], ...] = ()


@dataclass(slots=True)
class SubmittedApplication:
    application_id: str
    telegram_user_id: int
    sheet_name: str
    last_known_status: str
    spreadsheet_id: str | None = None
    sheet_id: int | None = None
    direction: str | None = None
    answer_type: str | None = None
    application_type: str | None = None
    change_type: str | None = None
    is_urgent: bool | None = None
    batch_id: str | None = None
    last_seen_row_number: int | None = None
    last_seen_editor: str | None = None
    last_seen_editor_comment: str | None = None
    last_seen_final_answer: str | None = None
    last_seen_scriptwriter_response: str | None = None
    pending_editor_comment: str | None = None
    pending_editor_comment_seen_count: int = 0
    pending_scriptwriter_response: str | None = None
    pending_scriptwriter_response_seen_count: int = 0
    submitted_at: str | None = None
    polling_state: str = StatusPollingState.ACTIVE.value
    not_found_count: int = 0
    last_not_found_at: str | None = None
    next_status_check_at: str | None = None
    deletion_seen_count: int = 0
    deletion_last_seen_at: str | None = None
    deletion_error: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    id: int
    application_id: str | None
    telegram_user_id: int | None
    event_type: str
    event_at: str
    old_value: str | None
    new_value: str | None
    metadata_json: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class LlmRecommendationProcess:
    application_id: str
    telegram_user_id: int
    field_code: str
    state: str
    process_json: str
    created_at: str
    updated_at: str




@dataclass(slots=True)
class NotificationOutboxItem:
    event_id: str
    dedupe_key: str
    telegram_user_id: int
    event_type: str
    snapshot_json: str
    html: str
    chunk_index: int
    chunk_count: int
    state: str = NotificationOutboxState.PENDING.value
    attempts: int = 0
    next_attempt_at: str | None = None
    sending_started_at: str | None = None
    last_error: str | None = None
    telegram_message_id: int | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class DashboardOutboxItem:
    entity_type: str
    entity_id: str
    snapshot_json: str
    state: str = DashboardOutboxState.PENDING.value
    attempts: int = 0
    next_attempt_at: str | None = None
    sending_started_at: str | None = None
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""




@dataclass(slots=True)
class BulkReservation:
    reservation_id: str
    idempotency_key: str
    telegram_user_id: int
    state: str
    direction: str | None = None
    target_kind: str | None = None
    change_type: str | None = None
    requested_count: int | None = None
    spreadsheet_id: str | None = None
    sheet_id: int | None = None
    sheet_name: str | None = None
    start_row: int | None = None
    end_row: int | None = None
    insert_url: str | None = None
    registered_count: int = 0
    last_error: str | None = None
    started_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    registered_at: str | None = None


@dataclass(slots=True)
class BotResponse:
    text: str
    keyboard: KeyboardKind = KeyboardKind.NONE
    draft: Draft | None = None
    parse_mode: str | None = None
    keyboard_payload: str | None = None
