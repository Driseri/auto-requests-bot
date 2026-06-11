from __future__ import annotations

import json

import pytest

from app.flow import ApplicationFlow
from app.formatting import TextFormattingSpan
from app.models import AnswerType, ChangeType, Direction, FieldName, LlmResult
from app.repository import DraftRepository
from app.submission import InMemorySubmissionService


class FakeLlmClient:
    async def check_change_description(self, context):
        return LlmResult(
            is_complete=True,
            blocking_problem=None,
            clarification_instruction=None,
        )


async def make_flow(tmp_path):
    repository = DraftRepository(str(tmp_path / "test.db"))
    await repository.init()
    flow = ApplicationFlow(repository, FakeLlmClient(), InMemorySubmissionService())
    return flow, repository


async def fill_to_review(flow: ApplicationFlow, user_id: int) -> None:
    await flow.start_new(user_id)
    await flow.select_direction(user_id, Direction.FL)
    await flow.select_answer_type(user_id, AnswerType.ROLLOUT)
    await flow.select_change_type(user_id, ChangeType.ADD)
    await flow.handle_text(user_id, "intent.change_limit")
    await flow.handle_text(user_id, "Ivan")
    await flow.handle_text(user_id, "reason")
    await flow.handle_text(user_id, "change description")
    await flow.handle_text(user_id, "source text")
    await flow.select_urgency(user_id, True)


@pytest.mark.asyncio
async def test_source_text_saves_formatting_json(tmp_path):
    flow, repository = await make_flow(tmp_path)
    await flow.start_new(26)
    await flow.select_direction(26, Direction.FL)
    await flow.select_answer_type(26, AnswerType.ROLLOUT)
    await flow.select_change_type(26, ChangeType.ADD)
    await flow.handle_text(26, "intent.change_limit")
    await flow.handle_text(26, "Ivan")
    await flow.handle_text(26, "reason")
    await flow.handle_text(26, "change description")

    await flow.handle_text(
        26,
        "abcdef",
        [TextFormattingSpan(start=0, end=3, bold=True)],
    )
    draft = await repository.get_by_user_id(26)

    assert draft is not None
    assert draft.source_text == "abcdef"
    assert draft.source_text_formatting_json is not None
    assert json.loads(draft.source_text_formatting_json) == [
        {"start": 0, "end": 3, "bold": True, "strikethrough": False}
    ]


@pytest.mark.asyncio
async def test_edit_source_text_replaces_formatting_json(tmp_path):
    flow, repository = await make_flow(tmp_path)
    await fill_to_review(flow, 27)

    await flow.select_edit_field(27, FieldName.SOURCE_TEXT)
    await flow.handle_text(
        27,
        "new text",
        [TextFormattingSpan(start=4, end=8, strikethrough=True)],
    )
    draft = await repository.get_by_user_id(27)

    assert draft is not None
    assert draft.source_text == "new text"
    assert draft.source_text_formatting_json is not None
    assert json.loads(draft.source_text_formatting_json) == [
        {"start": 4, "end": 8, "bold": False, "strikethrough": True}
    ]


@pytest.mark.asyncio
async def test_review_renders_source_text_bold_and_strikethrough(tmp_path):
    flow, _ = await make_flow(tmp_path)
    await flow.start_new(28)
    await flow.select_direction(28, Direction.FL)
    await flow.select_answer_type(28, AnswerType.ROLLOUT)
    await flow.select_change_type(28, ChangeType.ADD)
    await flow.handle_text(28, "intent.change_limit")
    await flow.handle_text(28, "Ivan")
    await flow.handle_text(28, "reason")
    await flow.handle_text(28, "change description")
    await flow.handle_text(
        28,
        "abcdef",
        [
            TextFormattingSpan(start=0, end=3, bold=True),
            TextFormattingSpan(start=2, end=5, strikethrough=True),
        ],
    )
    await flow.select_urgency(28, True)

    response = await flow.show_review(28)

    assert response.parse_mode == "HTML"
    assert "<b>ab</b><b><s>c</s></b><s>de</s>f" in response.text


@pytest.mark.asyncio
async def test_review_escapes_source_text_before_formatting(tmp_path):
    flow, _ = await make_flow(tmp_path)
    await flow.start_new(29)
    await flow.select_direction(29, Direction.FL)
    await flow.select_answer_type(29, AnswerType.ROLLOUT)
    await flow.select_change_type(29, ChangeType.ADD)
    await flow.handle_text(29, "intent.change_limit")
    await flow.handle_text(29, "Ivan")
    await flow.handle_text(29, "reason")
    await flow.handle_text(29, "change description")
    await flow.handle_text(
        29,
        "<tag>",
        [TextFormattingSpan(start=0, end=5, bold=True)],
    )
    await flow.select_urgency(29, True)

    response = await flow.show_review(29)

    assert "<b>&lt;tag&gt;</b>" in response.text
