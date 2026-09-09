from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from interaction_log import InteractionLog


def _message(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider_id": "msg-123",
        "direction": "outbound",
        "from_number": "+14155201316",
        "to_number": "+14155550111",
        "body": "The exact body",
        "timestamp": 1770000000000,
        "observed_at": "2026-02-02T03:04:05Z",
        "source": "local_send",
    }
    payload.update(overrides)
    return payload


def _call(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "call_id": "call-123",
        "direction": "inbound",
        "from_number": "+14155550111",
        "to_number": "+14155201316",
        "date_started": 1770000000000,
        "duration": 0,
        "call_missed": True,
        "source": "webhook",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def log_paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "sms.db", tmp_path / "calls.db"


def test_record_message_is_idempotent_and_preserves_known_body(log_paths: tuple[Path, Path]) -> None:
    sms_db, calls_db = log_paths
    log = InteractionLog(sms_db=sms_db, calls_db=calls_db)

    first = log.record_message(_message())
    duplicate = log.record_message(
        _message(
            body="",
            body_known=False,
            source="export_repair",
            observed_at="2026-02-02T03:05:05Z",
        )
    )

    assert first["created"] is True
    assert duplicate["created"] is False
    assert duplicate["message"]["text"] == "The exact body"
    assert duplicate["message"]["body_known"] is True
    assert duplicate["message"]["source"] == "local_send|export_repair"

    conn = sqlite3.connect(sms_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        conn.close()


def test_missing_provider_id_uses_fingerprint_and_does_not_erase_body(
    log_paths: tuple[Path, Path],
) -> None:
    sms_db, calls_db = log_paths
    log = InteractionLog(sms_db=sms_db, calls_db=calls_db)

    first = log.record_message(_message(provider_id=None, source="webhook"))
    replay = log.record_message(
        _message(
            provider_id=None,
            body=None,
            body_known=False,
            source="export_repair",
        )
    )

    assert first["created"] is True
    assert replay["created"] is False
    assert replay["message"]["text"] == "The exact body"
    assert replay["message"]["fingerprint"]
    assert replay["message"]["interaction_key"].startswith("fingerprint:")


def test_provider_id_upgrades_unknown_or_known_fallback_without_identity_downgrade(
    log_paths: tuple[Path, Path],
) -> None:
    sms_db, calls_db = log_paths
    log = InteractionLog(sms_db=sms_db, calls_db=calls_db)

    unknown = log.record_message(_message(provider_id=None, body="", body_known=False))
    upgraded = log.record_message(_message(provider_id="provider-after", body="Recovered body"))
    assert unknown["created"] is True
    assert upgraded["created"] is False
    assert upgraded["message"]["dialpad_id"] == "provider-after"
    assert upgraded["message"]["text"] == "Recovered body"
    assert upgraded["message"]["interaction_key"] == "provider:provider-after"

    known = log.record_message(
        _message(provider_id=None, body="Known body", timestamp=1770000060000)
    )
    provider_repair = log.record_message(
        _message(
            provider_id="provider-known",
            body="",
            body_known=False,
            timestamp=1770000060000,
        )
    )
    assert known["created"] is True
    assert provider_repair["created"] is False
    assert provider_repair["message"]["text"] == "Known body"
    assert provider_repair["message"]["interaction_key"] == "provider:provider-known"


def test_known_fallback_messages_with_same_participants_and_minute_do_not_merge(
    log_paths: tuple[Path, Path],
) -> None:
    log = InteractionLog(sms_db=log_paths[0], calls_db=log_paths[1])

    first = log.record_message(_message(provider_id=None, body="First", timestamp=1770000000000))
    second = log.record_message(_message(provider_id=None, body="Second", timestamp=1770000001000))

    assert first["created"] is True
    assert second["created"] is True
    assert log.thread("+14155550111")["count"] == 2


def test_provider_id_does_not_merge_different_known_body_on_fallback_base(
    log_paths: tuple[Path, Path],
) -> None:
    log = InteractionLog(sms_db=log_paths[0], calls_db=log_paths[1])

    first = log.record_message(
        _message(provider_id=None, body="First known body", timestamp=1770000120000)
    )
    second = log.record_message(
        _message(
            provider_id="provider-different-body",
            body="Second known body",
            timestamp=1770000120000,
        )
    )

    assert first["created"] is True
    assert second["created"] is True
    assert log.thread("+14155550111")["count"] == 2


def test_inbox_returns_inbound_messages_only(log_paths: tuple[Path, Path]) -> None:
    sms_db, calls_db = log_paths
    log = InteractionLog(sms_db=sms_db, calls_db=calls_db)
    log.record_message(
        _message(
            provider_id="inbound-1",
            direction="inbound",
            from_number="+14155550111",
            to_number="+14155201316",
            body="Inbound body",
            source="webhook",
        )
    )
    log.record_message(_message(provider_id="outbound-1"))

    inbox = log.inbox(limit=10)

    assert inbox["count"] == 1
    assert [message["direction"] for message in inbox["messages"]] == ["inbound"]
    assert inbox["messages"][0]["text"] == "Inbound body"


def test_calls_reuse_calls_db_and_are_idempotent(log_paths: tuple[Path, Path]) -> None:
    sms_db, calls_db = log_paths
    log = InteractionLog(sms_db=sms_db, calls_db=calls_db)

    first = log.record_call(_call(), owner=True)
    replay = log.record_call(_call(), owner=True)

    assert first["created"] is True
    assert replay["created"] is False
    assert replay["call"]["disposition"] == "missed"
    assert log.calls(hours=None)["count"] == 1

    conn = sqlite3.connect(calls_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1
    finally:
        conn.close()


def test_record_call_requires_owner_or_reconciler(log_paths: tuple[Path, Path]) -> None:
    log = InteractionLog(sms_db=log_paths[0], calls_db=log_paths[1])

    with pytest.raises(PermissionError):
        log.record_call(_call())
