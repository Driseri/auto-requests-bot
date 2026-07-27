from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from html import escape
from typing import Any


SUPPORTED_ENTITY_TYPES = {"bold", "strikethrough"}


@dataclass(frozen=True, slots=True)
class TextFormattingSpan:
    start: int
    end: int
    bold: bool = False
    strikethrough: bool = False


def extract_formatting_spans(text: str, entities: list[Any] | None) -> list[TextFormattingSpan]:
    """Извлечь поддерживаемые Telegram-стили, сохраняя UTF-16 offsets API."""
    if not text or not entities:
        return []

    text_length = utf16_length(text)
    spans: list[TextFormattingSpan] = []
    for entity in entities:
        entity_type = str(getattr(entity, "type", ""))
        if entity_type not in SUPPORTED_ENTITY_TYPES:
            continue

        start = int(getattr(entity, "offset", 0))
        length = int(getattr(entity, "length", 0))
        end = min(start + length, text_length)
        if start < 0 or end <= start:
            continue

        spans.append(
            TextFormattingSpan(
                start=start,
                end=end,
                bold=entity_type == "bold",
                strikethrough=entity_type == "strikethrough",
            )
        )
    return spans


def serialize_formatting_spans(spans: list[TextFormattingSpan] | None) -> str | None:
    if not spans:
        return None
    return json.dumps([asdict(span) for span in spans], ensure_ascii=False)


def deserialize_formatting_spans(value: str | None) -> list[TextFormattingSpan]:
    if not value:
        return []
    try:
        raw_spans = json.loads(value)
    except (TypeError, ValueError):
        return []

    spans: list[TextFormattingSpan] = []
    if not isinstance(raw_spans, list):
        return spans
    for item in raw_spans:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item["start"])
            end = int(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        spans.append(
            TextFormattingSpan(
                start=start,
                end=end,
                bold=bool(item.get("bold")),
                strikethrough=bool(item.get("strikethrough")),
            )
        )
    return spans


def build_text_format_runs(
    text: str,
    spans: list[TextFormattingSpan] | None,
) -> list[dict[str, Any]]:
    """Преобразовать пересекающиеся spans в Google Sheets textFormatRuns."""
    if not text or not spans:
        return []

    text_length = utf16_length(text)
    boundaries = {0, text_length}
    normalized: list[TextFormattingSpan] = []
    for span in spans:
        start = max(0, min(span.start, text_length))
        end = max(0, min(span.end, text_length))
        if end <= start:
            continue
        normalized.append(
            TextFormattingSpan(
                start=start,
                end=end,
                bold=span.bold,
                strikethrough=span.strikethrough,
            )
        )
        boundaries.update({start, end})

    if not normalized:
        return []

    runs: list[dict[str, Any]] = []
    last_format: dict[str, bool] | None = None
    for start in sorted(boundaries):
        if start >= text_length:
            continue
        active = _format_at_offset(start, normalized)
        if active == last_format:
            continue
        runs.append(
            {
                "startIndex": start,
                "format": {
                    "bold": active["bold"],
                    "strikethrough": active["strikethrough"],
                },
            }
        )
        last_format = active

    return runs if any(_is_styled_run(run) for run in runs) else []


def render_html_with_formatting(
    text: str | None,
    spans: list[TextFormattingSpan] | None,
) -> str:
    """Безопасно отрендерить сохраненные bold/strikethrough spans в Telegram HTML."""
    if not text:
        return "-"
    if not spans:
        return escape(text)

    text_length = utf16_length(text)
    boundaries = {0, text_length}
    normalized: list[TextFormattingSpan] = []
    for span in spans:
        start = max(0, min(span.start, text_length))
        end = max(0, min(span.end, text_length))
        if end <= start:
            continue
        normalized.append(
            TextFormattingSpan(
                start=start,
                end=end,
                bold=span.bold,
                strikethrough=span.strikethrough,
            )
        )
        boundaries.update({start, end})

    if not normalized:
        return escape(text)

    parts: list[str] = []
    sorted_boundaries = sorted(boundaries)
    for left, right in zip(sorted_boundaries, sorted_boundaries[1:]):
        if right <= left:
            continue
        segment = text[_utf16_offset_to_py_index(text, left) : _utf16_offset_to_py_index(text, right)]
        if not segment:
            continue
        active = _format_at_offset(left, normalized)
        escaped = escape(segment)
        if active["strikethrough"]:
            escaped = f"<s>{escaped}</s>"
        if active["bold"]:
            escaped = f"<b>{escaped}</b>"
        parts.append(escaped)

    return "".join(parts) or escape(text)


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _utf16_offset_to_py_index(text: str, offset: int) -> int:
    if offset <= 0:
        return 0
    current = 0
    for index, character in enumerate(text):
        next_offset = current + utf16_length(character)
        if offset < next_offset:
            return index
        if offset == next_offset:
            return index + 1
        current = next_offset
    return len(text)


def _format_at_offset(offset: int, spans: list[TextFormattingSpan]) -> dict[str, bool]:
    return {
        "bold": any(span.bold and span.start <= offset < span.end for span in spans),
        "strikethrough": any(
            span.strikethrough and span.start <= offset < span.end for span in spans
        ),
    }


def _is_styled_run(run: dict[str, Any]) -> bool:
    fmt = run.get("format", {})
    return bool(fmt.get("bold") or fmt.get("strikethrough"))
