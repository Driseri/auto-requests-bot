from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.llm import LlmClient, PromptRenderer
from app.models import LlmContext


class FakeGigaChatClient:
    def __init__(self, raw_response: str | None = None, exc: Exception | None = None) -> None:
        self.raw_response = raw_response or "{}"
        self.exc = exc
        self.calls = []
        self._settings = SimpleNamespace(model=None, profanity_check=None, flags=None)

    async def achat(self, chat):
        self.calls.append(
            {
                "chat": chat,
            }
        )
        if self.exc:
            raise self.exc
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=self.raw_response),
                )
            ]
        )


def write_prompts(tmp_path):
    system_prompt = tmp_path / "system.md"
    user_prompt = tmp_path / "user.md"
    system_prompt.write_text("System prompt", encoding="utf-8")
    user_prompt.write_text(
        "intent={intent}\n"
        "scriptwriter={scriptwriter}\n"
        "reason={reason}\n"
        "raw={raw_change_description}\n"
        "clarification={clarification_text}",
        encoding="utf-8",
    )
    return system_prompt, user_prompt


def make_context(**overrides):
    values = {
        "intent": "intent.test",
        "scriptwriter": "Иван",
        "reason": "Причина",
        "raw_change_description": "Суть",
        "clarification_text": "",
    }
    values.update(overrides)
    return LlmContext(**values)


def test_prompt_renderer_substitutes_all_placeholders(tmp_path):
    system_prompt, user_prompt = write_prompts(tmp_path)
    renderer = PromptRenderer(str(system_prompt), str(user_prompt))

    system, user = renderer.render(make_context())

    assert system == "System prompt"
    assert "intent=intent.test" in user
    assert "scriptwriter=Иван" in user
    assert "reason=Причина" in user
    assert "raw=Суть" in user
    assert "clarification=Не было." in user


@pytest.mark.asyncio
async def test_gigachat_client_maps_structured_response(tmp_path):
    system_prompt, user_prompt = write_prompts(tmp_path)
    fake_client = FakeGigaChatClient(
        raw_response=(
            '{"is_complete": true, "quality_score": 0.87, "problems": [], '
            '"clarifying_question": null, '
            '"formatted_change_description": "Готовая формулировка", '
            '"short_summary": "Коротко"}'
        )
    )
    llm_client = LlmClient(
        credentials="credentials",
        system_prompt_path=str(system_prompt),
        user_prompt_path=str(user_prompt),
        gigachat_client=fake_client,
    )

    result = await llm_client.check_change_description(make_context())

    assert result.is_complete is True
    assert result.quality_score == 0.87
    assert result.formatted_change_description == "Готовая формулировка"
    assert result.short_summary == "Коротко"
    assert fake_client.calls[0]["chat"].messages


@pytest.mark.asyncio
async def test_gigachat_client_returns_fallback_on_exception(tmp_path):
    system_prompt, user_prompt = write_prompts(tmp_path)
    llm_client = LlmClient(
        credentials="credentials",
        system_prompt_path=str(system_prompt),
        user_prompt_path=str(user_prompt),
        gigachat_client=FakeGigaChatClient(exc=RuntimeError("boom")),
    )

    result = await llm_client.check_change_description(make_context(raw_change_description="raw"))

    assert result.is_complete is True
    assert result.quality_score is None
    assert result.formatted_change_description == "raw"
    assert result.problems == ["Ошибка GigaChat: RuntimeError: boom"]


@pytest.mark.asyncio
async def test_gigachat_client_accepts_missing_quality_score(tmp_path):
    system_prompt, user_prompt = write_prompts(tmp_path)
    fake_client = FakeGigaChatClient(
        raw_response=(
            '{"is_complete": false, '
            '"problems": ["Не хватает исходного текста"], '
            '"clarifying_question": "Пришлите исходный текст.", '
            '"formatted_change_description": null, '
            '"short_summary": null}'
        )
    )
    llm_client = LlmClient(
        credentials="credentials",
        system_prompt_path=str(system_prompt),
        user_prompt_path=str(user_prompt),
        gigachat_client=fake_client,
    )

    result = await llm_client.check_change_description(make_context())

    assert result.is_complete is False
    assert result.quality_score is None
    assert result.problems == ["Не хватает исходного текста"]


@pytest.mark.asyncio
async def test_gigachat_client_logs_raw_response_before_validation(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="app.llm")
    system_prompt, user_prompt = write_prompts(tmp_path)
    llm_client = LlmClient(
        credentials="credentials",
        system_prompt_path=str(system_prompt),
        user_prompt_path=str(user_prompt),
        gigachat_client=FakeGigaChatClient(raw_response='{"is_complete": true}'),
    )

    await llm_client.check_change_description(make_context())

    assert 'Raw GigaChat response: {"is_complete": true}' in caplog.text
