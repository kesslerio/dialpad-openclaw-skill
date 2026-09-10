from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from interaction_log import InteractionLog
from log_outbox import enqueue_observation, record_outbound_observation, replay_outbox


def _observation() -> dict[str, object]:
    return {
        "provider_id": "msg-outbox-1",
        "direction": "outbound",
        "from_number": "+14155201316",
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
            from_number="+14155201316",
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
    assert result == {"attempted": 1, "succeeded": 1, "failed": 0, "remaining": 0}
    assert not outbox.exists()
    assert InteractionLog(sms_db=sms_db, calls_db=tmp_path / "calls.db").thread(
        "+14155550111", limit=10
    )["count"] == 1
