from __future__ import annotations

import sqlite3

import pytest

from app.loadtest import (
    CountingNotifier,
    LoadtestState,
    cleanup_sqlite,
    load_profile,
    parse_args,
)
from app.models import LlmContext
from app.loadtest import CompleteFakeLlm


def test_pilot15_profile_shape() -> None:
    profile = load_profile("pilot15")

    assert profile.users == 15
    assert profile.singles_per_user == 10
    assert profile.users * profile.singles_per_user == 150
    assert profile.bulk_batches == 3
    assert profile.bulk_rows == 30
    assert profile.bulk_batches * profile.bulk_rows == 90
    assert profile.polling_cycles == 30


def test_polling_cycles_override() -> None:
    profile = load_profile("baseline", polling_cycles=12)

    assert profile.name == "baseline"
    assert profile.polling_cycles == 12


def test_parse_args_defaults() -> None:
    args = parse_args(
        [
            "--profile",
            "stress",
            "--cleanup-sqlite",
            "--concurrency",
            "7",
            "--google-throttle-seconds",
            "0.5",
        ]
    )

    assert args.profile == "stress"
    assert args.cleanup_sqlite is True
    assert args.concurrency == 7
    assert args.google_throttle_seconds == 0.5


@pytest.mark.asyncio
async def test_fake_llm_returns_complete_result() -> None:
    llm = CompleteFakeLlm()

    result = await llm.check_change_description(
        LlmContext(
            intent="intent",
            scriptwriter="writer",
            reason="reason",
            raw_change_description="change",
        )
    )

    assert result.is_complete is True
    assert result.blocking_problem is None
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_counting_notifier_does_not_send_telegram() -> None:
    notifier = CountingNotifier()

    result = await notifier.send_message(123, "<b>hello</b>", parse_mode="HTML")

    assert result.message_id == 1
    assert notifier.messages == [
        {
            "chat_id": 123,
            "text_len": 12,
            "parse_mode": "HTML",
            "has_reply_markup": False,
        }
    ]


def test_cleanup_sqlite_removes_only_current_run(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE drafts (telegram_user_id INTEGER PRIMARY KEY);
        CREATE TABLE user_settings (telegram_user_id INTEGER PRIMARY KEY);
        CREATE TABLE submitted_applications (
            application_id TEXT PRIMARY KEY,
            telegram_user_id INTEGER,
            batch_id TEXT
        );
        CREATE TABLE bulk_batches (
            batch_id TEXT PRIMARY KEY,
            telegram_user_id INTEGER
        );
        CREATE TABLE bulk_creation_requests (
            idempotency_key TEXT PRIMARY KEY,
            telegram_user_id INTEGER,
            batch_id TEXT
        );
        CREATE TABLE dashboard_outbox (
            entity_type TEXT,
            entity_id TEXT,
            snapshot_json TEXT,
            PRIMARY KEY (entity_type, entity_id)
        );
        CREATE TABLE notification_outbox (
            event_id TEXT PRIMARY KEY,
            snapshot_json TEXT,
            html TEXT
        );
        """
    )
    conn.execute("INSERT INTO drafts VALUES (9100000001)")
    conn.execute("INSERT INTO drafts VALUES (42)")
    conn.execute("INSERT INTO user_settings VALUES (9100000001)")
    conn.execute("INSERT INTO user_settings VALUES (42)")
    conn.execute("INSERT INTO submitted_applications VALUES ('APP-1', 9100000001, NULL)")
    conn.execute("INSERT INTO submitted_applications VALUES ('APP-MISSED', 9100000001, NULL)")
    conn.execute("INSERT INTO submitted_applications VALUES ('APP-OTHER', 42, NULL)")
    conn.execute("INSERT INTO submitted_applications VALUES ('BULK-APP-1', 9100000001, 'BATCH-1')")
    conn.execute("INSERT INTO bulk_batches VALUES ('BATCH-1', 9100000001)")
    conn.execute("INSERT INTO bulk_batches VALUES ('BATCH-MISSED', 9100000001)")
    conn.execute("INSERT INTO bulk_batches VALUES ('BATCH-OTHER', 42)")
    conn.execute("INSERT INTO bulk_creation_requests VALUES ('KEY-1', 9100000001, 'BATCH-1')")
    conn.execute("INSERT INTO bulk_creation_requests VALUES ('KEY-MISSED', 9100000001, 'BATCH-MISSED')")
    conn.execute("INSERT INTO bulk_creation_requests VALUES ('KEY-2', 42, 'BATCH-OTHER')")
    conn.execute(
        "INSERT INTO dashboard_outbox VALUES ('APPLICATION', 'APP-1', '{}')"
    )
    conn.execute(
        "INSERT INTO dashboard_outbox VALUES ('APPLICATION', 'APP-OTHER', '{}')"
    )
    conn.execute(
        "INSERT INTO dashboard_outbox VALUES ('BULK_BATCH', 'BATCH-1', '{}')"
    )
    conn.execute(
        "INSERT INTO notification_outbox VALUES ('EV-1', 'LOADTEST-RUN', '')"
    )
    conn.execute(
        "INSERT INTO notification_outbox VALUES ('EV-2', 'OTHER', '')"
    )
    conn.commit()
    conn.close()

    result = cleanup_sqlite(
        str(db_path),
        "LOADTEST-RUN",
        LoadtestState(
            application_ids=["APP-1"],
            batch_ids=["BATCH-1"],
            user_ids=[9100000001],
        ),
    )

    assert result["submitted_applications"] == 3
    assert result["submitted_applications_by_batch"] == 0
    assert result["bulk_creation_requests"] == 2
    assert result["bulk_creation_requests_by_user"] == 0
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM user_settings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM submitted_applications").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM bulk_batches").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM bulk_creation_requests").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dashboard_outbox").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
    conn.close()
