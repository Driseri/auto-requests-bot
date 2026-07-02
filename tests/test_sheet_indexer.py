from app.models import AnswerType, ChangeType, Direction
from app.sheet_indexer import find_index_candidates, plan_urgent_chips_layout_migration
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


def test_plan_urgent_chips_layout_migration_moves_legacy_chips_into_day():
    add_row = [""] * len(WORKSHEET_HEADERS)
    add_row[11] = "ADD00001"
    chips_row = [""] * len(CHIPS_WORKSHEET_HEADERS)
    chips_row[11] = "CHIP0001"
    chips_row[14] = "03.06.2026 10:00"
    rows = [
        WORKSHEET_HEADERS,
        ["03.06.26"],
        add_row,
        [ChangeType.CHIPS.value],
        CHIPS_WORKSHEET_HEADERS,
        ["03.06.26"],
        chips_row,
    ]

    report = plan_urgent_chips_layout_migration(
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="Срочные",
        rows=rows,
    )

    assert report["migrated"] == [
        {
            "application_id": "CHIP0001",
            "source_row": 7,
            "target_row": 6,
            "date": "03.06.26",
            "row_link": (
                "https://docs.google.com/spreadsheets/d/spreadsheet/edit"
                "#gid=123&range=A6:U6"
            ),
        }
    ]
    assert report["rows"][:6] == [
        WORKSHEET_HEADERS,
        ["03.06.26"],
        add_row,
        [ChangeType.CHIPS.value],
        CHIPS_WORKSHEET_HEADERS,
        chips_row,
    ]
    updates = {
        item["application_id"]: item["last_seen_row_number"]
        for item in report["tracking_updates"]
    }
    assert updates == {"ADD00001": 3, "CHIP0001": 6}


def test_plan_urgent_chips_layout_migration_skips_nested_headers():
    chips_row = [""] * len(CHIPS_WORKSHEET_HEADERS)
    chips_row[11] = "CHIP0001"
    chips_row[14] = "03.06.2026 10:00"
    rows = [
        WORKSHEET_HEADERS,
        ["03.06.26"],
        [ChangeType.CHIPS.value],
        CHIPS_WORKSHEET_HEADERS,
        ["03.06.26"],
        chips_row,
        CHIPS_WORKSHEET_HEADERS,
    ]

    report = plan_urgent_chips_layout_migration(
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="Срочные",
        rows=rows,
    )

    assert [item["application_id"] for item in report["migrated"]] == ["CHIP0001"]
