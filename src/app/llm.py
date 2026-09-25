from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import logging
import re
from pathlib import Path
from string import Formatter
from time import perf_counter
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.models import (
    LlmCheckResult,
    LlmContext,
    LlmGapCode,
    LlmResult,
    LlmTelemetry,
)


DEFAULT_SYSTEM_PROMPT_PATH = "prompts/gigachat_system_v6.2_recommendation.md"
DEFAULT_USER_PROMPT_PATH = "prompts/gigachat_user_v6.1_recommendation.md"
APPLICATION_DATA_JSON_PLACEHOLDER = "{{APPLICATION_DATA_JSON}}"
GIGACHAT_JSON_RETRY_ATTEMPTS = 2
GIGACHAT_JSON_RETRY_DELAY_SECONDS = 1.0
GIGACHAT_RESPONSE_PREVIEW_CHARS = 300
GIGACHAT_RETRYABLE_RESPONSE_KINDS = {
    "empty_response",
    "invalid_json",
    "schema_validation",
}
LLM_ERROR_PREFIX = "Ошибка GigaChat:"

logger = logging.getLogger(__name__)


class LlmResponseError(ValueError):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        raw_response: str = "",
        metadata: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.raw_response = raw_response
        self.metadata = metadata or {}
        if cause is not None:
            self.__cause__ = cause


class LlmResultSchema(BaseModel):
    is_complete: bool = Field(
        description=(
            "True, только если фрагменты текста подтверждают все применимые "
            "критерии системной инструкции."
        )
    )
    blocking_problem: str | None = Field(
        default=None,
        description=(
            "При is_complete=false один из четырех блокеров системной инструкции "
            "с номером критерия и правила; при is_complete=true — null."
        ),
    )
    clarification_instruction: str | None = Field(
        default=None,
        description=(
            "При is_complete=false одна конкретная непустая инструкция автору; "
            "при is_complete=true — null."
        ),
    )

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


V5GapCode = Literal[
    "missing_new_entity_content",
    "missing_change_content",
    "missing_application_context",
    "missing_change_rationale",
]


class LlmRecommendationResultSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_result: Literal["ok", "recommendation"]
    gap_code: V5GapCode | None = None
    recommendation: str | None = None

    @model_validator(mode="after")
    def validate_recommendation_fields(self) -> "LlmRecommendationResultSchema":
        if self.check_result == LlmCheckResult.OK.value:
            if self.gap_code is not None or self.recommendation is not None:
                raise ValueError("ok result must contain null gap_code and recommendation")
            return self
        if self.gap_code is None:
            raise ValueError("recommendation result must contain gap_code")
        if not (self.recommendation or "").strip():
            raise ValueError("recommendation result must contain recommendation")
        return self


class LlmV61RecommendationResultSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_result: Literal["ok", "recommendation"]
    gap_code: LlmGapCode | None = None
    missing_detail: str | None = None

    @model_validator(mode="after")
    def validate_recommendation_fields(self) -> "LlmV61RecommendationResultSchema":
        if self.check_result == LlmCheckResult.OK.value:
            if self.gap_code is not None or self.missing_detail is not None:
                raise ValueError("ok result must contain null gap_code and missing_detail")
            return self
        if self.gap_code is None:
            raise ValueError("recommendation result must contain gap_code")
        if not (self.missing_detail or "").strip():
            raise ValueError("recommendation result must contain missing_detail")
        return self


V2BlockingProblem = Literal[
    "Критерий 1, правило 1.1: не указано содержание нового ответа",
    (
        "Критерий 1, правило 1.2: "
        "не указано конкретное изменение существующего ответа"
    ),
    "Критерий 2, правило 2.1: не указана ситуация применения",
    (
        "Критерий 3, правило 3.1: "
        "не указана причина изменения существующего ответа"
    ),
]

V2_BLOCKER_INSTRUCTIONS = {
    "Критерий 1, правило 1.1: не указано содержание нового ответа": (
        "Укажите факт, правило, условие, ограничение или действие для нового ответа."
    ),
    (
        "Критерий 1, правило 1.2: "
        "не указано конкретное изменение существующего ответа"
    ): (
        "Укажите действие и конкретный факт, правило, условие или утверждение, "
        "которое нужно изменить."
    ),
    "Критерий 2, правило 2.1: не указана ситуация применения": (
        "Укажите сообщение или действие клиента, тему обращения либо условие "
        "применения ответа."
    ),
    (
        "Критерий 3, правило 3.1: "
        "не указана причина изменения существующего ответа"
    ): (
        "Укажите текущую проблему, требуемое изменение и явную связь между ними."
    ),
}
V2_BLOCKER_BY_CODE = {
    blocker.split("правило ", 1)[1].split(":", 1)[0]: blocker
    for blocker in V2_BLOCKER_INSTRUCTIONS
}
V2_NEW_ENTITY_PATTERN = re.compile(
    r"\bнов(?:ый|ая|ое)\b(?:\s+[а-яё-]+){0,3}\s+"
    r"(?:ответ|акци[яи]|продукт|услуг[аи]|правил[оа]|инициатив[аы])\b",
    re.IGNORECASE,
)
V2_CURRENT_STATE_PATTERNS = {
    "bot_response": re.compile(
        r"\b(?:сейчас|бот\s+(?:пишет|отвечает|сообщает|обещает)|"
        r"ответ\s+(?:содержит|не\s+содержит)|"
        r"в\s+ответе\s+(?:указано|отсутствует|лишнее|неверно)|"
        r"информаци(?:я|и)\s+устарел\w*)\b",
        re.IGNORECASE,
    ),
    "client_problem": re.compile(
        r"\bклиент(?:ы|а|у|ом|е|ам|ами|ах)?\s+(?:"
        r"не\s+понима\w*|счита\w*|дума\w*|ожида\w*|"
        r"получа\w*\s+неверн\w+\s+информац\w*|не\s+мож\w+|"
        r"сталкива\w*|обраща\w*|жалу\w*)\b",
        re.IGNORECASE,
    ),
    "external_change": re.compile(
        r"\b(?:(?:изменил(?:ись|ось|ась|ся)|изменен\w*|"
        r"обновил(?:ись|ось|ась|ся)|обновлен\w*|введен\w*|отменен\w*)"
        r"(?:\s+\w+){0,3}\s+(?:правил\w*|услови\w*|тариф\w*|"
        r"ограничени\w*|процесс\w*)|"
        r"(?:правил\w*|услови\w*|тариф\w*|ограничени\w*|процесс\w*)"
        r"(?:\s+\w+){0,3}\s+(?:изменил(?:ись|ось|ась|ся)|изменен\w*|"
        r"обновил(?:ись|ось|ась|ся)|обновлен\w*|введен\w*|отменен\w*))\b",
        re.IGNORECASE,
    ),
}


class LlmV2ResultSchema(LlmResultSchema):
    is_complete: bool = Field(
        description=(
            "True, только если выполнены все применимые правила V2. Для изменения "
            "существующего ответа без явного маркера текущего содержания по "
            "правилу 3.1 значение обязательно false, даже если желаемое изменение "
            "указано."
        )
    )
    blocking_problem: V2BlockingProblem | None = Field(
        default=None,
        description=(
            "При is_complete=false один из четырех полных блокеров с номером "
            "критерия и правила; при is_complete=true — null."
        ),
    )


class PromptRenderer:
    def __init__(self, system_prompt_path: str, user_prompt_path: str) -> None:
        self.system_prompt_path = Path(system_prompt_path)
        self.user_prompt_path = Path(user_prompt_path)
        self._prompt_hash: str | None = None

    def render(self, context: LlmContext) -> tuple[str, str]:
        """Собрать system/user prompts из шаблонов и уже известных полей заявки."""
        system_prompt = self._read_prompt(self.system_prompt_path)
        user_template = self._read_prompt(self.user_prompt_path)
        self._prompt_hash = _prompt_templates_hash(system_prompt, user_template)
        if APPLICATION_DATA_JSON_PLACEHOLDER in user_template:
            if context.iteration_number not in {1, 2}:
                raise ValueError("V6.1 check_iteration must be 1 or 2")
            is_repeat = context.iteration_number == 2
            application_data = {
                "check_iteration": context.iteration_number,
                "client_case": (context.reason or "").strip() or None,
                "change_description": context.raw_change_description,
                "previous_gap_code": (
                    (context.previous_gap_code or "").strip() or None
                )
                if is_repeat
                else None,
                "previous_missing_detail": (
                    (context.previous_missing_detail or None)
                    if is_repeat else None
                ),
            }
            application_data_json = json.dumps(
                application_data,
                ensure_ascii=False,
                indent=2,
            )
            return (
                system_prompt,
                user_template.replace(
                    APPLICATION_DATA_JSON_PLACEHOLDER,
                    application_data_json,
                ),
            )
        values = {
            "direction": context.direction or "Не указано.",
            "answer_type": context.answer_type or "Не указан.",
            "change_type": context.change_type or "Не указан.",
            "intent": context.intent,
            "scriptwriter": context.scriptwriter,
            "reason": context.reason,
            "raw_change_description": context.raw_change_description,
            "clarification_text": context.clarification_text or "Не было.",
            "initial_change_description": (
                context.initial_change_description
                or context.raw_change_description
                or "Не указано."
            ),
            "previous_gap_code": context.previous_gap_code or "Не было.",
            "previous_recommendation": context.previous_recommendation or "Не было.",
            "iteration_number": str(context.iteration_number),
        }
        return system_prompt, _format_prompt(user_template, values)

    def identity(self) -> tuple[str, str | None]:
        if self._prompt_hash is None:
            try:
                self._prompt_hash = _prompt_templates_hash(
                    self._read_prompt(self.system_prompt_path),
                    self._read_prompt(self.user_prompt_path),
                )
            except OSError:
                return _prompt_version(self.system_prompt_path), None
        return _prompt_version(self.system_prompt_path), self._prompt_hash

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
        model: str = "GigaChat-2-Pro",
        verify_ssl_certs: bool = True,
        ca_bundle_file: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_backoff_factor: float = 1.0,
        system_prompt_path: str = DEFAULT_SYSTEM_PROMPT_PATH,
        user_prompt_path: str = DEFAULT_USER_PROMPT_PATH,
        log_full_request: bool = False,
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
        self.log_full_request = log_full_request
        self.prompt_renderer = PromptRenderer(system_prompt_path, user_prompt_path)
        system_prompt_name = Path(system_prompt_path).name
        if system_prompt_name == "gigachat_system_v2.md":
            self.response_schema: type[BaseModel] = LlmV2ResultSchema
        elif re.search(r"v6\.(?:1|2)_recommendation", system_prompt_name):
            self.response_schema = LlmV61RecommendationResultSchema
        elif "v5_recommendation" in system_prompt_name:
            self.response_schema = LlmRecommendationResultSchema
        else:
            self.response_schema = LlmResultSchema
        self._client = gigachat_client

    async def check_change_description(self, context: LlmContext) -> LlmResult:
        """Check description completeness with one retry for malformed JSON responses."""
        started_at = perf_counter()
        if not self.credentials and self._client is None:
            logger.warning(
                "GigaChat credentials are not configured; using fallback LLM result"
            )
            return self._with_telemetry(
                _fallback_result(
                    context,
                    "GIGACHAT_CREDENTIALS РЅРµ Р·Р°РґР°РЅ.",
                ),
                started_at=started_at,
                response_attempts=0,
                error_kind="not_configured",
            )

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
        self._log_full_request(system_prompt, user_prompt)
        last_error: BaseException | None = None
        for attempt in range(1, GIGACHAT_JSON_RETRY_ATTEMPTS + 1):
            try:
                parsed, raw_response = await self._request_structured_result(
                    system_prompt,
                    user_prompt,
                    attempt=attempt,
                    total_attempts=GIGACHAT_JSON_RETRY_ATTEMPTS,
                )
                result = _schema_to_result(parsed, context, raw_response=raw_response)
                logger.info(
                    "GigaChat recommendation check completed: check_result=%s "
                    "gap_code=%s has_recommendation=%s attempt=%s/%s",
                    result.check_result,
                    result.gap_code,
                    bool(result.recommendation or result.missing_detail),
                    attempt,
                    GIGACHAT_JSON_RETRY_ATTEMPTS,
                )
                return self._with_telemetry(
                    result,
                    started_at=started_at,
                    response_attempts=attempt,
                )
            except LlmResponseError as exc:
                last_error = exc
                self._log_response_failure(exc, attempt, GIGACHAT_JSON_RETRY_ATTEMPTS)
                if exc.kind in GIGACHAT_RETRYABLE_RESPONSE_KINDS and attempt == 1:
                    await asyncio.sleep(GIGACHAT_JSON_RETRY_DELAY_SECONDS)
                    continue
                break
            except Exception as exc:
                last_error = exc
                metadata = _response_metadata(exc)
                logger.exception(
                    "GigaChat check failed; using fallback result. "
                    "model=%s scope=%s base_url=%s auth_url=%s verify_ssl=%s "
                    "ca_bundle_configured=%s timeout=%s max_retries=%s "
                    "response_kind=%s status_code=%s content_type=%s "
                    "error_type=%s error=%s error_chain=%s",
                    self.model,
                    self.scope,
                    self.base_url,
                    self.auth_url,
                    self.verify_ssl_certs,
                    bool(self.ca_bundle_file),
                    self.timeout,
                    self.max_retries,
                    "transport_or_sdk_error",
                    metadata.get("status_code", "unknown"),
                    metadata.get("content_type", "unknown"),
                    type(exc).__name__,
                    exc,
                    _exception_chain(exc),
                )
                break
        assert last_error is not None
        return self._with_telemetry(
            _fallback_result(
                context,
                f"{LLM_ERROR_PREFIX} {_exception_chain(last_error) or last_error}",
                raw_response=getattr(last_error, "raw_response", None),
            ),
            started_at=started_at,
            response_attempts=attempt,
            error_kind=_normalized_error_kind(last_error),
        )

    def _with_telemetry(
        self,
        result: LlmResult,
        *,
        started_at: float,
        response_attempts: int,
        error_kind: str | None = None,
    ) -> LlmResult:
        prompt_version, prompt_hash = self.prompt_renderer.identity()
        return replace(
            result,
            telemetry=LlmTelemetry(
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                model=self.model,
                duration_ms=max(0, round((perf_counter() - started_at) * 1000)),
                response_attempts=response_attempts,
                validation_retries=max(0, response_attempts - 1),
                error_kind=error_kind,
            ),
        )

    async def _request_structured_result(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        attempt: int,
        total_attempts: int,
    ) -> tuple[BaseModel, str]:
        client = self._get_client()
        completion = await client.achat(
            _build_structured_chat(
                system_prompt,
                user_prompt,
                self.response_schema,
                getattr(
                    client,
                    "_settings",
                    SimpleNamespace(model=None, profanity_check=None, flags=None),
                ),
                model=self.model,
            )
        )
        metadata = _response_metadata(completion)
        raw_response = _extract_completion_content(completion)
        logger.info(
            "GigaChat response received: response_kind=%s attempt=%s/%s "
            "status_code=%s content_type=%s finish_reason=%s raw_len=%s raw_preview=%s",
            "received",
            attempt,
            total_attempts,
            metadata.get("status_code", "unknown"),
            metadata.get("content_type", "unknown"),
            metadata.get("finish_reason", "unknown"),
            len(raw_response or ""),
            _safe_response_preview(raw_response or ""),
        )
        return (
            _parse_llm_result(
                raw_response,
                metadata=metadata,
                response_schema=self.response_schema,
            ),
            raw_response,
        )

    def _log_response_failure(
        self,
        exc: LlmResponseError,
        attempt: int,
        total_attempts: int,
    ) -> None:
        metadata = exc.metadata
        logger.exception(
            "GigaChat response validation failed; %s. "
            "model=%s scope=%s base_url=%s auth_url=%s verify_ssl=%s "
            "ca_bundle_configured=%s timeout=%s max_retries=%s "
            "response_kind=%s attempt=%s/%s status_code=%s content_type=%s "
            "finish_reason=%s raw_len=%s raw_preview=%s error_type=%s "
            "error=%s error_chain=%s",
            (
                "retrying once"
                if exc.kind in GIGACHAT_RETRYABLE_RESPONSE_KINDS and attempt == 1
                else "using fallback result"
            ),
            self.model,
            self.scope,
            self.base_url,
            self.auth_url,
            self.verify_ssl_certs,
            bool(self.ca_bundle_file),
            self.timeout,
            self.max_retries,
            exc.kind,
            attempt,
            total_attempts,
            metadata.get("status_code", "unknown"),
            metadata.get("content_type", "unknown"),
            metadata.get("finish_reason", "unknown"),
            len(exc.raw_response or ""),
            _safe_response_preview(exc.raw_response),
            type(exc).__name__,
            exc,
            _exception_chain(exc),
        )

    async def _legacy_check_change_description(self, context: LlmContext) -> LlmResult:
        """Проверить выполнимость описания без переформулировки текста автором LLM."""
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
            self._log_full_request(system_prompt, user_prompt)
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
                    model=self.model,
                )
            )
            raw_response = _extract_completion_content(completion)
            logger.info(
                "GigaChat response received: response_kind=%s raw_len=%s raw_preview=%s",
                "legacy_received",
                len(raw_response or ""),
                _safe_response_preview(raw_response or ""),
            )
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
            return _fallback_result(context, f"{LLM_ERROR_PREFIX} {_exception_chain(exc) or exc}")

    def _log_full_request(self, system_prompt: str, user_prompt: str) -> None:
        if not self.log_full_request:
            return
        logger.info(
            "FULL GigaChat request (local diagnostics; contains user data): "
            "model=%s system_path=%s user_path=%s\n"
            "----- SYSTEM PROMPT -----\n%s\n"
            "----- USER PROMPT -----\n%s\n"
            "----- END GIGACHAT REQUEST -----",
            self.model,
            self.prompt_renderer.system_prompt_path,
            self.prompt_renderer.user_prompt_path,
            system_prompt,
            user_prompt,
        )

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


def _build_chat(system_prompt: str, user_prompt: str, *, model: str) -> Any:
    from gigachat.models import Chat, Messages, MessagesRole

    return Chat(
        messages=[
            Messages(role=MessagesRole.SYSTEM, content=system_prompt),
            Messages(role=MessagesRole.USER, content=user_prompt),
        ],
        model=model,
        temperature=0.01,
    )


def _build_structured_chat(
    system_prompt: str,
    user_prompt: str,
    response_format: type,
    settings: Any,
    *,
    model: str,
) -> Any:
    from gigachat.client import _prepare_chat_for_parse

    chat = _build_chat(system_prompt, user_prompt, model=model)
    return _prepare_chat_for_parse(chat, settings, response_format, True)


def _extract_completion_content(completion: Any) -> str:
    if not completion.choices:
        raise ValueError("Response has no choices")
    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise ValueError("GigaChat response was truncated")
    return choice.message.content


def _parse_llm_result(
    raw_response: str | None,
    *,
    metadata: dict[str, Any],
    response_schema: type[BaseModel] = LlmResultSchema,
) -> BaseModel:
    raw = raw_response or ""
    if not raw.strip():
        raise LlmResponseError(
            "empty_response",
            "GigaChat returned an empty response",
            raw_response=raw,
            metadata=metadata,
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmResponseError(
            "invalid_json",
            "GigaChat returned non-JSON response",
            raw_response=raw,
            metadata=metadata,
            cause=exc,
        ) from exc
    if response_schema is LlmRecommendationResultSchema:
        required_keys = {"check_result", "gap_code", "recommendation"}
    elif response_schema is LlmV61RecommendationResultSchema:
        required_keys = {"check_result", "gap_code", "missing_detail"}
    else:
        required_keys = {"is_complete", "blocking_problem", "clarification_instruction"}
    if not isinstance(payload, dict) or not required_keys.issubset(payload):
        raise LlmResponseError(
            "schema_validation",
            "GigaChat JSON does not contain all required fields",
            raw_response=raw,
            metadata=metadata,
        )
    if response_schema is LlmV2ResultSchema:
        payload = _normalize_v2_payload(payload)
    try:
        return response_schema.model_validate(payload)
    except ValidationError as exc:
        raise LlmResponseError(
            "schema_validation",
            "GigaChat JSON does not match the expected schema",
            raw_response=raw,
            metadata=metadata,
            cause=exc,
        ) from exc
    except ValueError as exc:
        raise LlmResponseError(
            "schema_validation",
            "GigaChat JSON violates the expected response contract",
            raw_response=raw,
            metadata=metadata,
            cause=exc,
        ) from exc


def _normalize_v2_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("is_complete") is not False:
        return payload
    normalized = dict(payload)
    blocker = normalized.get("blocking_problem")
    if isinstance(blocker, str):
        blocker = V2_BLOCKER_BY_CODE.get(blocker.strip(), blocker.strip())
        normalized["blocking_problem"] = blocker
    if blocker in V2_BLOCKER_INSTRUCTIONS and not str(
        normalized.get("clarification_instruction") or ""
    ).strip():
        normalized["clarification_instruction"] = V2_BLOCKER_INSTRUCTIONS[blocker]
    return normalized


def _safe_response_preview(raw: str, limit: int = GIGACHAT_RESPONSE_PREVIEW_CHARS) -> str:
    normalized = re.sub(r"\s+", " ", raw).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."


def _response_metadata(completion_or_exc: Any) -> dict[str, Any]:
    metadata = {
        "status_code": "unknown",
        "content_type": "unknown",
        "finish_reason": "unknown",
    }
    response = getattr(completion_or_exc, "response", None)
    if response is None:
        response = getattr(completion_or_exc, "resp", None)
    status_code = (
        getattr(completion_or_exc, "status_code", None)
        or getattr(response, "status_code", None)
        or getattr(response, "status", None)
    )
    if status_code is not None:
        metadata["status_code"] = status_code
    headers = getattr(response, "headers", None) or getattr(completion_or_exc, "headers", None)
    content_type = _header_value(headers, "content-type")
    if content_type:
        metadata["content_type"] = content_type
    choices = getattr(completion_or_exc, "choices", None) or []
    if choices:
        finish_reason = getattr(choices[0], "finish_reason", None)
        if finish_reason is not None:
            metadata["finish_reason"] = finish_reason
    return metadata


def _header_value(headers: Any, name: str) -> Any:
    if not headers:
        return None
    if hasattr(headers, "get"):
        return headers.get(name) or headers.get(name.title())
    return None


def _prompt_version(system_prompt_path: Path) -> str:
    match = re.search(
        r"_v(?P<version>\d+(?:\.\d+)?)(?:_|$)",
        system_prompt_path.stem,
        re.IGNORECASE,
    )
    if match:
        return f"v{match.group('version')}"
    return system_prompt_path.stem or "unknown"


def _prompt_templates_hash(system_prompt: str, user_template: str) -> str:
    payload = f"{system_prompt}\0{user_template}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _normalized_error_kind(exc: BaseException) -> str:
    if isinstance(exc, LlmResponseError):
        return exc.kind if exc.kind in {
            "empty_response",
            "invalid_json",
            "schema_validation",
            "truncated_response",
        } else "unknown"

    metadata = _response_metadata(exc)
    try:
        status_code = int(metadata.get("status_code"))
    except (TypeError, ValueError):
        status_code = 0
    if status_code in {401, 403}:
        return "auth"
    if status_code == 429:
        return "rate_limit"
    if status_code >= 500:
        return "provider_error"

    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    text = " ".join(
        f"{type(item).__name__} {item}" for item in chain
    ).lower()
    if any(isinstance(item, TimeoutError) for item in chain) or "timeout" in text:
        return "timeout"
    if any(token in text for token in ("unauthorized", "forbidden", "autherror")):
        return "auth"
    if any(token in text for token in ("rate limit", "ratelimit", "too many requests")):
        return "rate_limit"
    if any(token in text for token in ("bad gateway", "service unavailable")):
        return "provider_error"
    if "truncat" in text or "finish_reason" in text and "length" in text:
        return "truncated_response"
    if any(
        token in text
        for token in (
            "connection",
            "network",
            "dns",
            "connector",
            "connecterror",
            "connectionerror",
            "clienterror",
            "socketerror",
        )
    ):
        return "network"
    return "unknown"


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


def _schema_to_result(
    schema: BaseModel,
    context: LlmContext,
    *,
    raw_response: str | None = None,
) -> LlmResult:
    if isinstance(schema, LlmV61RecommendationResultSchema):
        is_complete = schema.check_result == LlmCheckResult.OK.value
        gap_code = schema.gap_code.value if schema.gap_code is not None else None
        missing_detail = schema.missing_detail
        return LlmResult(
            is_complete=is_complete,
            blocking_problem=gap_code,
            clarification_instruction=missing_detail,
            check_result=schema.check_result,
            gap_code=gap_code,
            missing_detail=missing_detail,
            raw_response=raw_response,
        )
    if isinstance(schema, LlmRecommendationResultSchema):
        is_complete = schema.check_result == LlmCheckResult.OK.value
        gap_code = schema.gap_code
        recommendation = (schema.recommendation or "").strip() or None
        return LlmResult(
            is_complete=is_complete,
            blocking_problem=gap_code,
            clarification_instruction=recommendation,
            check_result=schema.check_result,
            gap_code=gap_code,
            recommendation=recommendation,
            raw_response=raw_response,
        )
    if (
        isinstance(schema, LlmV2ResultSchema)
        and schema.is_complete
        and _v2_existing_answer_lacks_current_state(context)
    ):
        blocker = (
            "Критерий 3, правило 3.1: "
            "не указана причина изменения существующего ответа"
        )
        return LlmResult(
            is_complete=False,
            blocking_problem=blocker,
            clarification_instruction=V2_BLOCKER_INSTRUCTIONS[blocker],
            check_result=LlmCheckResult.RECOMMENDATION.value,
            gap_code=LlmGapCode.MISSING_CHANGE_RATIONALE.value,
            recommendation=V2_BLOCKER_INSTRUCTIONS[blocker],
            raw_response=raw_response,
        )
    assert isinstance(schema, LlmResultSchema)
    gap_code = _legacy_gap_code(schema.blocking_problem)
    recommendation = schema.clarification_instruction
    return LlmResult(
        is_complete=schema.is_complete,
        blocking_problem=schema.blocking_problem,
        clarification_instruction=schema.clarification_instruction,
        check_result=(
            LlmCheckResult.OK.value
            if schema.is_complete
            else LlmCheckResult.RECOMMENDATION.value
        ),
        gap_code=gap_code,
        recommendation=recommendation,
        raw_response=raw_response,
    )


def _legacy_gap_code(blocking_problem: str | None) -> str | None:
    text = (blocking_problem or "").lower()
    if "1.1" in text or "нового ответа" in text:
        return LlmGapCode.MISSING_NEW_ENTITY_CONTENT.value
    if "1.2" in text or "конкретн" in text and "измен" in text:
        return LlmGapCode.MISSING_CHANGE_CONTENT.value
    if "2.1" in text or "ситуац" in text:
        return LlmGapCode.MISSING_APPLICATION_CONTEXT.value
    if "3.1" in text or "причин" in text or "основан" in text:
        return LlmGapCode.MISSING_CHANGE_RATIONALE.value
    return None


def _v2_existing_answer_lacks_current_state(context: LlmContext) -> bool:
    text = "\n".join(
        part
        for part in (
            context.intent,
            context.reason,
            context.raw_change_description,
            context.clarification_text,
        )
        if part
    )
    return not V2_NEW_ENTITY_PATTERN.search(text) and not any(
        pattern.search(text) for pattern in V2_CURRENT_STATE_PATTERNS.values()
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


def _fallback_result(
    context: LlmContext,
    problem: str,
    *,
    raw_response: str | None = None,
) -> LlmResult:
    return LlmResult(
        is_complete=True,
        blocking_problem=problem,
        clarification_instruction=None,
        check_result=LlmCheckResult.ERROR.value,
        raw_response=raw_response,
    )
