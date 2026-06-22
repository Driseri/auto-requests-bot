from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
import logging
import random
import socket
import ssl
import time
from typing import Callable, TypeVar

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
_GOOGLE_API_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="google-api",
)


@dataclass(frozen=True, slots=True)
class GoogleApiRetryConfig:
    max_attempts: int = 4
    base_seconds: float = 1.0
    max_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("GOOGLE_API_MAX_ATTEMPTS must be at least 1")
        if self.base_seconds < 0:
            raise ValueError("GOOGLE_API_RETRY_BASE_SECONDS must be non-negative")
        if self.max_seconds < self.base_seconds:
            raise ValueError(
                "GOOGLE_API_RETRY_MAX_SECONDS must be greater than or equal to base"
            )


def execute_with_retry(
    operation: Callable[[], T],
    *,
    config: GoogleApiRetryConfig,
    operation_id: str,
    reset_client: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random.random,
) -> T:
    """Выполнить Google API операцию с backoff только для временных ошибок."""
    for attempt in range(1, config.max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            retryable = is_retryable_google_error(exc)
            if not retryable or attempt >= config.max_attempts:
                raise
            if reset_client is not None:
                reset_client()
            delay = min(
                config.max_seconds,
                config.base_seconds * (2 ** (attempt - 1)),
            )
            delay *= 0.5 + random_value() * 0.5
            LOGGER.warning(
                "Temporary Google API failure: operation_id=%s attempt=%s/%s "
                "retry_in=%.2fs error=%r",
                operation_id,
                attempt,
                config.max_attempts,
                delay,
                exc,
            )
            sleep(delay)
    raise AssertionError("unreachable")


async def execute_with_retry_async(
    operation: Callable[[], T],
    *,
    config: GoogleApiRetryConfig,
    operation_id: str,
    reset_client: Callable[[], None] | None = None,
) -> T:
    loop = asyncio.get_running_loop()
    call = partial(
        execute_with_retry,
        operation,
        config=config,
        operation_id=operation_id,
        reset_client=reset_client,
    )
    return await loop.run_in_executor(_GOOGLE_API_EXECUTOR, call)


def is_retryable_google_error(exc: Exception) -> bool:
    """Отделить временные сетевые и серверные сбои от постоянных ошибок запроса."""
    status = _http_status(exc)
    if status is not None:
        return status in RETRYABLE_HTTP_STATUSES
    return isinstance(
        exc,
        (
            TimeoutError,
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            socket.timeout,
            socket.gaierror,
            ssl.SSLError,
        ),
    )


def _http_status(exc: Exception) -> int | None:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    status_code = getattr(exc, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def is_google_rate_limit_error(exc: Exception) -> bool:
    status = _http_status(exc)
    if status == 429:
        return True
    if status is not None:
        return False
    text = _exception_text(exc).lower()
    return any(
        marker in text
        for marker in (
            "quota exceeded",
            "rate limit",
            "ratelimit",
            "read requests per minute",
            "writerequestsperminute",
            "readrequestsperminute",
        )
    )


def _exception_text(exc: Exception) -> str:
    parts = [str(exc)]
    content = getattr(exc, "content", None)
    if isinstance(content, bytes):
        parts.append(content.decode("utf-8", errors="ignore"))
    elif content is not None:
        parts.append(str(content))
    return " ".join(parts)
