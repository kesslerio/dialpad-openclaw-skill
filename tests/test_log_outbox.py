from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from interaction_log import InteractionLog
from log_api_client import LogApiError
from log_outbox import enqueue_observation, record_outbound_observation, replay_outbox


def _observation() -> dict[str, object]:
    return {
        "provider_id": "msg-outbox-1",
        "direction": "outbound",
        "from_number": "+14155550140",
        "to_number": "+14155550111",
        "body": "exact queued body",
        "timestamp": 1770000000000,
        "observed_at": "2026-02-02T03:04:05Z",
        "source": "local_send",
    }


def test_successful_send_record_failure_is_queued_without_retrying_provider(tmp_path: Path, monkeypatch) -> None:
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_LOG_URL", "http://127.0.0.1:18887")
    monkeypatch.setenv("DIALPAD_LOG_TOKEN", "unit-token")
    monkeypatch.setenv("DIALPAD_LOG_OUTBOX", str(outbox))
    with patch("log_outbox.remote_record_message", side_effect=RuntimeError("owner unavailable")) as record:
        result = record_outbound_observation(
            {"id": "msg-outbox-1", "message_status": "pending"},
            to_numbers=["+14155550111"],
            from_number="+14155550140",
            body="exact queued body",
        )

    assert result["memory_sync"] == "pending"
    record.assert_called_once()
    lines = outbox.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    queued = json.loads(lines[0])["observation"]
    assert queued["body"] == "exact queued body"
    assert queued["provider_id"] == "msg-outbox-1"


def test_replay_records_once_and_removes_only_successful_observations(tmp_path: Path, monkeypatch) -> None:
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    enqueue_observation(_observation(), path=outbox)

    result = replay_outbox(path=outbox, limit=10)
    assert result == {
        "attempted": 1,
        "succeeded": 1,
        "failed": 0,
        "delivery_failed": 0,
        "observation_rejected": 0,
        "remaining": 0,
    }
    assert not outbox.exists()
    assert InteractionLog(sms_db=sms_db, calls_db=tmp_path / "calls.db").thread(
        "+14155550111", limit=10
    )["count"] == 1


def test_retryable_transport_failure_queues_a_reason_and_never_retries_the_provider(
    tmp_path: Path, monkeypatch
) -> None:
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_LOG_URL", "http://127.0.0.1:18887")
    monkeypatch.setenv("DIALPAD_LOG_TOKEN", "unit-token")
    monkeypatch.setenv("DIALPAD_LOG_OUTBOX", str(outbox))

    error = LogApiError("connection refused", code="network_error", retryable=True)
    with patch("log_outbox.remote_record_message", side_effect=error) as record:
        result = record_outbound_observation(
            {"id": "msg-outbox-1", "message_status": "pending"},
            to_numbers=["+14155550111"],
            from_number="+14155550140",
            body="exact queued body",
        )

    assert result["memory_sync"] == "pending"
    record.assert_called_once()
    failure = json.loads(outbox.read_text(encoding="utf-8").splitlines()[0])["failure"]
    assert failure["code"] == "network_error"
    assert failure["retryable"] is True
    assert failure["failure_class"] == "delivery_failed"
    assert failure["at"]


def test_a_transport_failure_and_a_content_rejection_are_counted_separately(
    tmp_path: Path, monkeypatch
) -> None:
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_LOG_URL", "http://127.0.0.1:18887")
    monkeypatch.setenv("DIALPAD_LOG_TOKEN", "unit-token")
    enqueue_observation(_observation(), path=outbox)
    enqueue_observation({**_observation(), "provider_id": "msg-outbox-2"}, path=outbox)

    errors = [
        LogApiError("connection refused", code="network_error", retryable=True),
        LogApiError("body is required", code="invalid_argument", retryable=False),
    ]
    with patch("log_outbox.remote_record_message", side_effect=errors):
        result = replay_outbox(path=outbox, limit=10)

    assert result["failed"] == 2
    assert result["delivery_failed"] == 1
    assert result["observation_rejected"] == 1
    assert result["remaining"] == 2
    codes = [
        json.loads(line)["failure"]["code"]
        for line in outbox.read_text(encoding="utf-8").splitlines()
    ]
    assert codes == ["network_error", "invalid_argument"]


def test_failed_replay_updates_the_reason_and_keeps_the_original_queued_at(
    tmp_path: Path, monkeypatch
) -> None:
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_LOG_URL", "http://127.0.0.1:18887")
    monkeypatch.setenv("DIALPAD_LOG_TOKEN", "unit-token")
    enqueue_observation(_observation(), path=outbox)
    before = json.loads(outbox.read_text(encoding="utf-8").splitlines()[0])

    with patch(
        "log_outbox.remote_record_message",
        side_effect=LogApiError("timed out", code="network_error", retryable=True),
    ):
        replay_outbox(path=outbox, limit=10)

    after = json.loads(outbox.read_text(encoding="utf-8").splitlines()[0])
    assert after["queued_at"] == before["queued_at"]
    assert after["observation"] == before["observation"]
    assert after["failure"]["code"] == "network_error"


def test_entry_written_without_a_failure_field_still_replays(tmp_path: Path, monkeypatch) -> None:
    outbox = tmp_path / "outbox.jsonl"
    sms_db = tmp_path / "sms.db"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    outbox.write_text(
        json.dumps({"queued_at": "2026-09-14T20:42:48.984069Z", "observation": _observation()}) + "\n",
        encoding="utf-8",
    )

    result = replay_outbox(path=outbox, limit=10)

    assert result["succeeded"] == 1
    assert not outbox.exists()


def test_stored_reason_never_carries_the_bearer_token(tmp_path: Path, monkeypatch) -> None:
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_LOG_OUTBOX", str(outbox))
    monkeypatch.setenv("DIALPAD_LOG_TOKEN", "super-secret-token")

    enqueue_observation(
        _observation(),
        path=outbox,
        reason={
            "code": "upstream_error",
            "failure_class": "delivery_failed",
            "message": "rejected header super-secret-token",
        },
    )

    written = outbox.read_text(encoding="utf-8")
    assert "super-secret-token" not in written
    assert "[redacted]" in written
