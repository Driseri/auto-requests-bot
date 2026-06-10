from __future__ import annotations

from types import SimpleNamespace

from app.formatting import (
    TextFormattingSpan,
    build_text_format_runs,
    deserialize_formatting_spans,
    extract_formatting_spans,
    render_html_with_formatting,
    serialize_formatting_spans,
)


def test_plain_text_without_entities_has_no_formatting_spans():
    assert extract_formatting_spans("plain text", None) == []


def test_bold_entity_becomes_bold_span():
    spans = extract_formatting_spans(
        "hello",
        [SimpleNamespace(type="bold", offset=1, length=3)],
    )

    assert spans == [TextFormattingSpan(start=1, end=4, bold=True)]


def test_strikethrough_entity_becomes_strikethrough_span():
    spans = extract_formatting_spans(
        "hello",
        [SimpleNamespace(type="strikethrough", offset=0, length=5)],
    )

    assert spans == [TextFormattingSpan(start=0, end=5, strikethrough=True)]


def test_overlapping_spans_are_merged_into_google_text_format_runs():
    runs = build_text_format_runs(
        "abcdefgh",
        [
            TextFormattingSpan(start=0, end=4, bold=True),
            TextFormattingSpan(start=2, end=8, strikethrough=True),
        ],
    )

    assert runs == [
        {"startIndex": 0, "format": {"bold": True, "strikethrough": False}},
        {"startIndex": 2, "format": {"bold": True, "strikethrough": True}},
        {"startIndex": 4, "format": {"bold": False, "strikethrough": True}},
    ]


def test_utf16_offsets_are_preserved_for_text_with_emoji():
    spans = extract_formatting_spans(
        "🙂abcd",
        [SimpleNamespace(type="bold", offset=2, length=2)],
    )

    assert spans == [TextFormattingSpan(start=2, end=4, bold=True)]
    assert build_text_format_runs("🙂abcd", spans) == [
        {"startIndex": 0, "format": {"bold": False, "strikethrough": False}},
        {"startIndex": 2, "format": {"bold": True, "strikethrough": False}},
        {"startIndex": 4, "format": {"bold": False, "strikethrough": False}},
    ]


def test_invalid_serialized_formatting_is_ignored():
    assert deserialize_formatting_spans("{bad json") == []


def test_render_html_with_no_or_invalid_formatting_escapes_text():
    assert render_html_with_formatting("<b>text</b>", []) == "&lt;b&gt;text&lt;/b&gt;"


def test_serialize_empty_spans_returns_none():
    assert serialize_formatting_spans([]) is None
