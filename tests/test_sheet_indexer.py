from app.models import AnswerType, ChangeType, Direction
from app.sheet_indexer import find_index_candidates
from app.submission import CHIPS_WORKSHEET_HEADERS, WORKSHEET_HEADERS


def test_sheet_indexer_finds_add_edit_row_without_technical_fields():
    row = [""] * len(WORKSHEET_HEADERS)
    row[0] = "Петров Петр"
    row[2] = "Кейс клиента"
    row[3] = "Суть"
    row[4] = "Текст"
    row[10] = "Интент"
    rows = [WORKSHEET_HEADERS, row]

    candidates, skipped = find_index_candidates(
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="Срочные",
        rows=rows,
        direction=Direction.FL.value,
        answer_type=AnswerType.URGENT.value,
    )

    assert skipped == []
    assert len(candidates) == 1
    assert candidates[0].row_number == 2
    assert candidates[0].change_type == ChangeType.ADD.value
    assert candidates[0].is_urgent is True


def test_sheet_indexer_skips_existing_application_id():
    row = [""] * len(WORKSHEET_HEADERS)
    row[0] = "Петров Петр"
    row[2] = "Кейс клиента"
    row[3] = "Суть"
    row[4] = "Текст"
    row[10] = "Интент"
    row[11] = "ABC12345"
    rows = [WORKSHEET_HEADERS, row]

    candidates, skipped = find_index_candidates(
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="Срочные",
        rows=rows,
        direction=Direction.FL.value,
        answer_type=AnswerType.URGENT.value,
    )

    assert candidates == []
    assert skipped == []


def test_sheet_indexer_reports_partial_row_as_skipped():
    row = [""] * len(WORKSHEET_HEADERS)
    row[0] = "Петров Петр"
    row[2] = "Кейс клиента"
    rows = [WORKSHEET_HEADERS, row]

    candidates, skipped = find_index_candidates(
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="Срочные",
        rows=rows,
        direction=Direction.FL.value,
        answer_type=AnswerType.URGENT.value,
    )

    assert candidates == []
    assert len(skipped) == 1
    assert skipped[0].reason.startswith("missing_required_fields:")


def test_sheet_indexer_finds_chips_row():
    row = [""] * len(CHIPS_WORKSHEET_HEADERS)
    row[0] = "Петров Петр"
    row[2] = "Причина"
    row[3] = "До"
    row[4] = "Чипс"
    row[5] = "После"
    row[10] = "Интент"
    rows = [CHIPS_WORKSHEET_HEADERS, row]

    candidates, skipped = find_index_candidates(
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="Срочные",
        rows=rows,
        direction=Direction.FL.value,
        answer_type=AnswerType.URGENT.value,
    )

    assert skipped == []
    assert len(candidates) == 1
    assert candidates[0].change_type == ChangeType.CHIPS.value
