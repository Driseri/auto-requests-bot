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
    SOURCE_TEXT = "source_text"
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


class ApplicationType(StrEnum):
    SINGLE = "Одиночная"
    BULK = "Массовая"


class BulkBatchStatus(StrEnum):
    NEW = "Новая пачка"
    IN_PROGRESS = "В работе"
    DONE = "Готова"


class BulkApplicationStatus(StrEnum):
    NEW = "Новая"
    NEEDS_CLARIFICATION = "Нужны пояснения"
    ACCEPTED = "Принято"


class ApplicationStatus(StrEnum):
    NEW = "Новая"
    IN_PROGRESS = "В работе"
    NEEDS_CLARIFICATION = "Нужны пояснения"
    FINAL_ANSWER_READY = "Итоговый ответ готов"
    ACCEPTED = "Принята"
    REJECTED = "Отклонена"
    POSTPONED = "Отложена"

    # Backward-compatible aliases for old code/tests while the new model is rolled out.
    NEEDS_SCRIPTWRITER_RESPONSE = "Нужны пояснения"
    RESPONSE_RECEIVED = "Итоговый ответ готов"


class LlmCheckStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    COMPLETE = "complete"
    ERROR = "error"
    STUB_COMPLETE = "stub_complete"
    NEEDS_ATTENTION = "needs_attention"


class KeyboardKind(StrEnum):
    START = "start"
    CREATE_MODE = "create_mode"
    ACTIVE_DRAFT = "active_draft"
    DIRECTION = "direction"
    DIRECTION_WITH_DEFAULT = "direction_with_default"
    ANSWER_TYPE = "answer_type"
    CHANGE_TYPE = "change_type"
    STEP = "step"
    INTENT_STEP_WITH_DEFAULT = "intent_step_with_default"
    SCRIPTWRITER_STEP_WITH_DEFAULT = "scriptwriter_step_with_default"
    URGENCY = "urgency"
    PRIORITY = "priority"
    REVIEW = "review"
    EDIT_MENU = "edit_menu"
    EDIT_MENU_ROLLOUT = "edit_menu_rollout"
    BULK_MENU = "bulk_menu"
    BULK_CREATED = "bulk_created"
    DEFAULTS_MENU = "defaults_menu"
    DEFAULTS_BACK = "defaults_back"
    NONE = "none"


class FieldName(StrEnum):
    DIRECTION = "direction"
    ANSWER_TYPE = "answer_type"
    CHANGE_TYPE = "change_type"
    INTENT = "intent"
    SCRIPTWRITER = "scriptwriter"
    REASON = "reason"
    CHANGE_DESCRIPTION = "change_description"
    SOURCE_TEXT = "source_text"
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
    "application_type",
    "change_type",
    "author_name",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_application_id(now: datetime | None = None) -> str:
    return token_hex(4).upper()


def generate_batch_id(now: datetime | None = None) -> str:
    return f"BATCH-{token_hex(4).upper()}"


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
    priority: str | None = None
    llm_check_status: str = LlmCheckStatus.NOT_CHECKED.value
    llm_score: float | None = None
    clarification_count: int = 0
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
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class LlmContext:
    intent: str
    scriptwriter: str
    reason: str
    raw_change_description: str
    clarification_text: str = ""


@dataclass(slots=True)
class LlmResult:
    is_complete: bool
    quality_score: float | None
    problems: list[str]
    clarifying_question: str | None
    formatted_change_description: str | None
    short_summary: str | None


@dataclass(slots=True)
class SubmissionResult:
    success: bool
    message: str
    spreadsheet_id: str | None = None
    sheet_id: int | None = None
    sheet_name: str | None = None
    row_number: int | None = None
    row_link: str | None = None


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
    is_urgent: bool | None = None
    batch_id: str | None = None
    last_seen_row_number: int | None = None
    last_seen_editor: str | None = None
    last_seen_editor_comment: str | None = None
    last_seen_final_answer: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class BulkBatch:
    batch_id: str
    telegram_user_id: int
    spreadsheet_id: str
    direction: str
    sheet_name: str
    sheet_id: int
    start_row: int
    data_start_row: int
    reserved_rows: int
    status_schema_version: int = 2
    batch_status: str = BulkBatchStatus.NEW.value
    last_known_batch_status: str = BulkBatchStatus.NEW.value
    last_seen_final_answers_digest_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class BotResponse:
    text: str
    keyboard: KeyboardKind = KeyboardKind.NONE
    draft: Draft | None = None
    parse_mode: str | None = None
    keyboard_payload: str | None = None
