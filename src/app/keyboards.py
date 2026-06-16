from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.models import (
    AnswerType,
    ChangeType,
    Direction,
    FieldName,
    KeyboardKind,
    direction_display_label,
)


class CallbackData:
    NEW = "app:new"
    NOTIFICATION_NEW = "app:notification:new"
    SINGLE = "app:single"
    BULK_UPLOAD = "app:bulk_upload"
    BULK_TEMPLATE = "app:bulk_template"
    BULK_CREATE = "app:bulk_create"
    DEFAULTS = "app:defaults"
    SET_DEFAULT_DIRECTION = "app:defaults:set_direction"
    SET_DEFAULT_INTENT = "app:defaults:set_intent"
    SET_DEFAULT_SCRIPTWRITER = "app:defaults:set_scriptwriter"
    CLEAR_DEFAULT_DIRECTION = "app:defaults:clear_direction"
    CLEAR_DEFAULT_INTENT = "app:defaults:clear_intent"
    CLEAR_DEFAULT_SCRIPTWRITER = "app:defaults:clear_scriptwriter"
    USE_DEFAULT_DIRECTION = "app:use_default:direction"
    USE_DEFAULT_INTENT = "app:use_default:intent"
    USE_DEFAULT_SCRIPTWRITER = "app:use_default:scriptwriter"
    CONTINUE = "app:continue"
    RESTART = "app:restart"
    CANCEL = "app:cancel"
    BACK = "app:back"
    URGENCY_YES = "app:urgency:yes"
    URGENCY_NO = "app:urgency:no"
    SUBMIT = "app:submit"
    EDIT = "app:edit"
    REVIEW = "app:review"

    # Legacy callbacks kept so old inline messages do not crash the bot.
    PRIORITY_HIGH = "app:priority:high"
    PRIORITY_LOW = "app:priority:low"

    @staticmethod
    def edit_field(field: FieldName) -> str:
        return f"app:edit:{field.value}"

    @staticmethod
    def direction(value: str) -> str:
        return f"app:direction:{value}"

    @staticmethod
    def answer_type(value: str) -> str:
        return f"app:answer_type:{value}"

    @staticmethod
    def change_type(value: str) -> str:
        return f"app:change_type:{value}"

    @staticmethod
    def bulk_ready(batch_id: str) -> str:
        return f"app:bulk_ready:{batch_id}"

    @staticmethod
    def bulk_direction(idempotency_key: str, direction: str) -> str:
        return f"app:bulk_direction:{idempotency_key}:{direction}"


def build_keyboard(kind: KeyboardKind, payload: str | None = None) -> InlineKeyboardMarkup | None:
    """Построить клавиатуру по состоянию flow и callback payload."""
    match kind:
        case KeyboardKind.START:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Создать заявку", callback_data=CallbackData.NEW)]
                ]
            )
        case KeyboardKind.CREATE_MODE:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Одиночная заявка",
                            callback_data=CallbackData.SINGLE,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Массовая заявка",
                            callback_data=CallbackData.BULK_UPLOAD,
                        )
                    ],
                    [InlineKeyboardButton(text="Дефолты", callback_data=CallbackData.DEFAULTS)],
                ]
            )
        case KeyboardKind.ACTIVE_DRAFT:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Продолжить", callback_data=CallbackData.CONTINUE)],
                    [
                        InlineKeyboardButton(
                            text="Начать заново",
                            callback_data=CallbackData.RESTART,
                        )
                    ],
                    [InlineKeyboardButton(text="Отменить", callback_data=CallbackData.CANCEL)],
                ]
            )
        case KeyboardKind.DIRECTION:
            return _direction_keyboard(include_default=False)
        case KeyboardKind.DIRECTION_WITH_DEFAULT:
            return _direction_keyboard(include_default=True)
        case KeyboardKind.ANSWER_TYPE:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=AnswerType.ROLLOUT.value,
                            callback_data=CallbackData.answer_type(AnswerType.ROLLOUT.value),
                        ),
                        InlineKeyboardButton(
                            text=AnswerType.URGENT.value,
                            callback_data=CallbackData.answer_type(AnswerType.URGENT.value),
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text=AnswerType.INTEGRATION.value,
                            callback_data=CallbackData.answer_type(AnswerType.INTEGRATION.value),
                        )
                    ],
                    [InlineKeyboardButton(text="Назад", callback_data=CallbackData.BACK)],
                    [InlineKeyboardButton(text="Отменить", callback_data=CallbackData.CANCEL)],
                ]
            )
        case KeyboardKind.CHANGE_TYPE:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=change_type.value,
                            callback_data=CallbackData.change_type(change_type.value),
                        )
                        for change_type in ChangeType
                    ],
                    [InlineKeyboardButton(text="Назад", callback_data=CallbackData.BACK)],
                    [InlineKeyboardButton(text="Отменить", callback_data=CallbackData.CANCEL)],
                ]
            )
        case KeyboardKind.STEP:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Назад", callback_data=CallbackData.BACK)],
                    [InlineKeyboardButton(text="Отменить", callback_data=CallbackData.CANCEL)],
                ]
            )
        case KeyboardKind.INTENT_STEP_WITH_DEFAULT:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Использовать интент по умолчанию",
                            callback_data=CallbackData.USE_DEFAULT_INTENT,
                        )
                    ],
                    [InlineKeyboardButton(text="Назад", callback_data=CallbackData.BACK)],
                    [InlineKeyboardButton(text="Отменить", callback_data=CallbackData.CANCEL)],
                ]
            )
        case KeyboardKind.SCRIPTWRITER_STEP_WITH_DEFAULT:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Использовать сценариста по умолчанию",
                            callback_data=CallbackData.USE_DEFAULT_SCRIPTWRITER,
                        )
                    ],
                    [InlineKeyboardButton(text="Назад", callback_data=CallbackData.BACK)],
                    [InlineKeyboardButton(text="Отменить", callback_data=CallbackData.CANCEL)],
                ]
            )
        case KeyboardKind.URGENCY:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Да", callback_data=CallbackData.URGENCY_YES),
                        InlineKeyboardButton(text="Нет", callback_data=CallbackData.URGENCY_NO),
                    ],
                    [InlineKeyboardButton(text="Назад", callback_data=CallbackData.BACK)],
                    [InlineKeyboardButton(text="Отменить", callback_data=CallbackData.CANCEL)],
                ]
            )
        case KeyboardKind.PRIORITY:
            # Legacy fallback for old drafts. New flow uses URGENCY.
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Высокий",
                            callback_data=CallbackData.PRIORITY_HIGH,
                        ),
                        InlineKeyboardButton(
                            text="Низкий",
                            callback_data=CallbackData.PRIORITY_LOW,
                        ),
                    ],
                    [InlineKeyboardButton(text="Назад", callback_data=CallbackData.BACK)],
                    [InlineKeyboardButton(text="Отменить", callback_data=CallbackData.CANCEL)],
                ]
            )
        case KeyboardKind.REVIEW:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Отправить", callback_data=CallbackData.SUBMIT)],
                    [InlineKeyboardButton(text="Редактировать", callback_data=CallbackData.EDIT)],
                    [
                        InlineKeyboardButton(text="Назад", callback_data=CallbackData.BACK),
                        InlineKeyboardButton(text="Отменить", callback_data=CallbackData.CANCEL),
                    ],
                ]
            )
        case KeyboardKind.EDIT_MENU | KeyboardKind.EDIT_MENU_ROLLOUT:
            change_type_rows = (
                [
                    [
                        InlineKeyboardButton(
                            text="Тип изменения",
                            callback_data=CallbackData.edit_field(FieldName.CHANGE_TYPE),
                        )
                    ]
                ]
                if kind == KeyboardKind.EDIT_MENU_ROLLOUT
                else []
            )
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Направление",
                            callback_data=CallbackData.edit_field(FieldName.DIRECTION),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Тип ответа",
                            callback_data=CallbackData.edit_field(FieldName.ANSWER_TYPE),
                        )
                    ],
                    *change_type_rows,
                    [
                        InlineKeyboardButton(
                            text="Интент",
                            callback_data=CallbackData.edit_field(FieldName.INTENT),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Сценарист",
                            callback_data=CallbackData.edit_field(FieldName.SCRIPTWRITER),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Причина",
                            callback_data=CallbackData.edit_field(FieldName.REASON),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Суть изменений",
                            callback_data=CallbackData.edit_field(FieldName.CHANGE_DESCRIPTION),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Исходный текст",
                            callback_data=CallbackData.edit_field(FieldName.SOURCE_TEXT),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Срочная",
                            callback_data=CallbackData.edit_field(FieldName.URGENCY),
                        )
                    ],
                    [InlineKeyboardButton(text="К заявке", callback_data=CallbackData.REVIEW)],
                ]
            )
        case KeyboardKind.BULK_MENU:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Создать массовую заявку",
                            callback_data=CallbackData.BULK_CREATE,
                        )
                    ],
                    [InlineKeyboardButton(text="Назад", callback_data=CallbackData.NEW)],
                ]
            )
        case KeyboardKind.BULK_DIRECTION:
            if not payload:
                return build_keyboard(KeyboardKind.BULK_MENU)
            return _bulk_direction_keyboard(payload)
        case KeyboardKind.BULK_CREATED:
            if not payload:
                return build_keyboard(KeyboardKind.BULK_MENU)
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Заявка заполнена",
                            callback_data=CallbackData.bulk_ready(payload),
                        )
                    ],
                ]
            )
        case KeyboardKind.DEFAULTS_MENU:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Задать направление",
                            callback_data=CallbackData.SET_DEFAULT_DIRECTION,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Задать интент",
                            callback_data=CallbackData.SET_DEFAULT_INTENT,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Задать сценариста",
                            callback_data=CallbackData.SET_DEFAULT_SCRIPTWRITER,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Очистить направление",
                            callback_data=CallbackData.CLEAR_DEFAULT_DIRECTION,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Очистить интент",
                            callback_data=CallbackData.CLEAR_DEFAULT_INTENT,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Очистить сценариста",
                            callback_data=CallbackData.CLEAR_DEFAULT_SCRIPTWRITER,
                        )
                    ],
                    [InlineKeyboardButton(text="Назад", callback_data=CallbackData.NEW)],
                ]
            )
        case KeyboardKind.DEFAULTS_BACK:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="К дефолтам", callback_data=CallbackData.DEFAULTS)],
                    [
                        InlineKeyboardButton(
                            text="К созданию заявки",
                            callback_data=CallbackData.NEW,
                        )
                    ],
                ]
            )
        case KeyboardKind.NOTIFICATION:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Главное меню",
                            callback_data=CallbackData.NOTIFICATION_NEW,
                        )
                    ]
                ]
            )
        case KeyboardKind.NOTIFICATION_BULK_BACK:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Назад к заявке",
                            callback_data=CallbackData.NOTIFICATION_NEW,
                        )
                    ]
                ]
            )
        case KeyboardKind.NOTIFICATION_SINGLE_BACK:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Назад к заведению заявки",
                            callback_data=CallbackData.NOTIFICATION_NEW,
                        )
                    ]
                ]
            )
        case KeyboardKind.BULK_COMPLETED:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Главное меню",
                            callback_data=CallbackData.NEW,
                        )
                    ]
                ]
            )
        case _:
            return None


def _direction_keyboard(*, include_default: bool) -> InlineKeyboardMarkup:
    rows = []
    if include_default:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Использовать направление по умолчанию",
                    callback_data=CallbackData.USE_DEFAULT_DIRECTION,
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=direction_display_label(Direction.FL.value),
                    callback_data=CallbackData.direction(Direction.FL.value),
                ),
                InlineKeyboardButton(
                    text=direction_display_label(Direction.SME.value),
                    callback_data=CallbackData.direction(Direction.SME.value),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=direction_display_label(Direction.AI.value),
                    callback_data=CallbackData.direction(Direction.AI.value),
                ),
                InlineKeyboardButton(
                    text=direction_display_label(Direction.VOICEBOT.value),
                    callback_data=CallbackData.direction(Direction.VOICEBOT.value),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=direction_display_label(Direction.COLLECTION.value),
                    callback_data=CallbackData.direction(Direction.COLLECTION.value),
                )
            ],
            [InlineKeyboardButton(text="Отменить", callback_data=CallbackData.CANCEL)],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bulk_direction_keyboard(idempotency_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=direction_display_label(direction.value),
                    callback_data=CallbackData.bulk_direction(
                        idempotency_key,
                        direction.value,
                    ),
                )
                for direction in (Direction.FL, Direction.SME)
            ],
            [
                InlineKeyboardButton(
                    text=direction_display_label(direction.value),
                    callback_data=CallbackData.bulk_direction(
                        idempotency_key,
                        direction.value,
                    ),
                )
                for direction in (Direction.AI, Direction.VOICEBOT)
            ],
            [
                InlineKeyboardButton(
                    text=direction_display_label(Direction.COLLECTION.value),
                    callback_data=CallbackData.bulk_direction(
                        idempotency_key,
                        Direction.COLLECTION.value,
                    ),
                )
            ],
            [InlineKeyboardButton(text="Отменить", callback_data=CallbackData.BULK_UPLOAD)],
        ]
    )
