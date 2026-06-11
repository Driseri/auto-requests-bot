from __future__ import annotations

import json
import logging
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, ValidationError, model_validator

from app.models import LlmContext, LlmResult


DEFAULT_SYSTEM_PROMPT_PATH = "prompts/gigachat_system.md"
DEFAULT_USER_PROMPT_PATH = "prompts/gigachat_user.md"

logger = logging.getLogger(__name__)


class LlmResultSchema(BaseModel):
    is_complete: bool
    blocking_problem: str | None = None
    clarification_instruction: str | None = None

    @model_validator(mode="after")
    def validate_completeness_fields(self) -> "LlmResultSchema":
        if self.is_complete:
            if self.blocking_problem or self.clarification_instruction:
                raise ValueError(
                    "Complete result must not contain a blocker or clarification instruction"
                )
            return self
        if not (self.blocking_problem or "").strip():
            raise ValueError("Incomplete result must contain blocking_problem")
        if not (self.clarification_instruction or "").strip():
            raise ValueError("Incomplete result must contain clarification_instruction")
        return self


class PromptRenderer:
    def __init__(self, system_prompt_path: str, user_prompt_path: str) -> None:
        self.system_prompt_path = Path(system_prompt_path)
        self.user_prompt_path = Path(user_prompt_path)

    def render(self, context: LlmContext) -> tuple[str, str]:
        system_prompt = self._read_prompt(self.system_prompt_path)
        user_template = self._read_prompt(self.user_prompt_path)
        values = {
            "intent": context.intent,
            "scriptwriter": context.scriptwriter,
            "reason": context.reason,
            "raw_change_description": context.raw_change_description,
            "clarification_text": context.clarification_text or "Не было.",
        }
        return system_prompt, _format_prompt(user_template, values)

    @staticmethod
    def _read_prompt(path: Path) -> str:
        return path.read_text(encoding="utf-8")


class LlmClient:
    def __init__(
        self,
        *,
        credentials: str = "",
        base_url: str = "https://gigachat.devices.sberbank.ru/api/v1",
        auth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        scope: str = "GIGACHAT_API_PERS",
        model: str = "GigaChat",
        verify_ssl_certs: bool = True,
        ca_bundle_file: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_backoff_factor: float = 1.0,
        system_prompt_path: str = DEFAULT_SYSTEM_PROMPT_PATH,
        user_prompt_path: str = DEFAULT_USER_PROMPT_PATH,
        gigachat_client: Any | None = None,
    ) -> None:
        self.credentials = credentials
        self.base_url = base_url
        self.auth_url = auth_url
        self.scope = scope
        self.model = model
        self.verify_ssl_certs = verify_ssl_certs
        self.ca_bundle_file = ca_bundle_file
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_factor = retry_backoff_factor
        self.prompt_renderer = PromptRenderer(system_prompt_path, user_prompt_path)
        self._client = gigachat_client

    async def check_change_description(self, context: LlmContext) -> LlmResult:
        if not self.credentials and self._client is None:
            logger.warning(
                "GigaChat credentials are not configured; using fallback LLM result"
            )
            return _fallback_result(
                context,
                "GIGACHAT_CREDENTIALS не задан.",
            )

        try:
            logger.info(
                "Checking change description with GigaChat: model=%s scope=%s "
                "base_url=%s auth_url=%s verify_ssl=%s ca_bundle_configured=%s "
                "timeout=%s max_retries=%s retry_backoff_factor=%s "
                "raw_len=%s clarification_len=%s",
                self.model,
                self.scope,
                self.base_url,
                self.auth_url,
                self.verify_ssl_certs,
                bool(self.ca_bundle_file),
                self.timeout,
                self.max_retries,
                self.retry_backoff_factor,
                len(context.raw_change_description),
                len(context.clarification_text or ""),
            )
            system_prompt, user_prompt = self.prompt_renderer.render(context)
            logger.debug(
                "Rendered GigaChat prompts: system_path=%s user_path=%s "
                "system_len=%s user_len=%s",
                self.prompt_renderer.system_prompt_path,
                self.prompt_renderer.user_prompt_path,
                len(system_prompt),
                len(user_prompt),
            )
            client = self._get_client()
            completion = await client.achat(
                _build_structured_chat(
                    system_prompt,
                    user_prompt,
                    LlmResultSchema,
                    getattr(
                        client,
                        "_settings",
                        SimpleNamespace(model=None, profanity_check=None, flags=None),
                    ),
                )
            )
            raw_response = _extract_completion_content(completion)
            logger.info("Raw GigaChat response: %s", raw_response)
            parsed = LlmResultSchema.model_validate(json.loads(raw_response))
            result = _schema_to_result(parsed, context)
            logger.info(
                "GigaChat completeness check completed: is_complete=%s has_blocker=%s "
                "has_clarification_instruction=%s",
                result.is_complete,
                bool(result.blocking_problem),
                bool(result.clarification_instruction),
            )
            return result
        except (ValidationError, ValueError, OSError, Exception) as exc:
            logger.exception(
                "GigaChat check failed; using fallback result. "
                "model=%s scope=%s base_url=%s auth_url=%s verify_ssl=%s "
                "ca_bundle_configured=%s timeout=%s max_retries=%s "
                "error_type=%s error=%s error_chain=%s",
                self.model,
                self.scope,
                self.base_url,
                self.auth_url,
                self.verify_ssl_certs,
                bool(self.ca_bundle_file),
                self.timeout,
                self.max_retries,
                type(exc).__name__,
                exc,
                _exception_chain(exc),
            )
            return _fallback_result(context, f"Ошибка GigaChat: {_exception_chain(exc) or exc}")

    def _get_client(self) -> Any:
        if self._client is None:
            from gigachat import GigaChat

            logger.info(
                "Creating GigaChat SDK client: model=%s scope=%s verify_ssl=%s "
                "base_url=%s auth_url=%s ca_bundle_configured=%s timeout=%s "
                "max_retries=%s retry_backoff_factor=%s",
                self.model,
                self.scope,
                self.verify_ssl_certs,
                self.base_url,
                self.auth_url,
                bool(self.ca_bundle_file),
                self.timeout,
                self.max_retries,
                self.retry_backoff_factor,
            )
            self._client = GigaChat(
                base_url=self.base_url,
                auth_url=self.auth_url,
                credentials=self.credentials,
                scope=self.scope,
                model=self.model,
                verify_ssl_certs=self.verify_ssl_certs,
                ca_bundle_file=self.ca_bundle_file,
                timeout=self.timeout,
                max_retries=self.max_retries,
                retry_backoff_factor=self.retry_backoff_factor,
            )
        return self._client


def _build_chat(system_prompt: str, user_prompt: str) -> Any:
    from gigachat.models import Chat, Messages, MessagesRole

    return Chat(
        messages=[
            Messages(role=MessagesRole.SYSTEM, content=system_prompt),
            Messages(role=MessagesRole.USER, content=user_prompt),
        ]
    )


def _build_structured_chat(
    system_prompt: str,
    user_prompt: str,
    response_format: type,
    settings: Any,
) -> Any:
    from gigachat.client import _prepare_chat_for_parse

    chat = _build_chat(system_prompt, user_prompt)
    return _prepare_chat_for_parse(chat, settings, response_format, True)


def _extract_completion_content(completion: Any) -> str:
    if not completion.choices:
        raise ValueError("Response has no choices")
    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise ValueError("GigaChat response was truncated")
    return choice.message.content


def _format_prompt(template: str, values: dict[str, str]) -> str:
    required_fields = {
        field_name
        for _, field_name, _, _ in Formatter().parse(template)
        if field_name is not None
    }
    missing = required_fields - values.keys()
    if missing:
        raise ValueError(f"Unknown prompt placeholders: {', '.join(sorted(missing))}")
    return template.format(**values)


def _schema_to_result(schema: LlmResultSchema, context: LlmContext) -> LlmResult:
    return LlmResult(
        is_complete=schema.is_complete,
        blocking_problem=schema.blocking_problem,
        clarification_instruction=schema.clarification_instruction,
    )


def _exception_chain(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current) or repr(current)
        parts.append(f"{type(current).__name__}: {message}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def _fallback_result(context: LlmContext, problem: str) -> LlmResult:
    return LlmResult(
        is_complete=True,
        blocking_problem=problem,
        clarification_instruction=None,
    )
