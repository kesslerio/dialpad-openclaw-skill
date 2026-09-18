"""Draining the outbox must merge into what the webhook already wrote.

This is the 2026-09-17 fleet condition exactly: the webhook had already
written all 18 rows within about two seconds, while the outbox held a
second, wrong copy of each. A drain that duplicated would have turned a
quiet bookkeeping bug into corrupted operational history, so the merge is
checked through the real drain path rather than only at the log seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from interaction_log import InteractionLog
from log_outbox import enqueue_observation, replay_outbox


def _observation(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider_id": "msg-61f0a2d0",
        "direction": "outbound",
        "from_number": "+14155550140",
        "to_number": "+14155550111",
        "body": "Confirming your scan for Thursday at 2pm.",
        "timestamp": 1770000000000,
        "observed_at": "2026-02-02T03:04:05Z",
        "source": "local_send",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def dbs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    sms_db, calls_db = tmp_path / "sms.db", tmp_path / "calls.db"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    monkeypatch.setenv("DIALPAD_LOG_URL", "")
    return sms_db, calls_db


def test_a_drained_observation_merges_into_the_row_the_webhook_already_wrote(
    dbs: tuple[Path, Path], tmp_path: Path
) -> None:
    sms_db, calls_db = dbs
    log = InteractionLog(sms_db=sms_db, calls_db=calls_db)
    webhook = log.record_message(_observation(source="dialpad_webhook"))

    assert webhook["created"] is True
    outbox = tmp_path / "outbox.jsonl"
    enqueue_observation(_observation(), path=outbox)

    result = replay_outbox(path=outbox, limit=10)

    assert result["succeeded"] == 1
    assert not outbox.exists()

    thread = log.thread("+14155550111", limit=10)
    assert thread["count"] == 1, "a drain must not add a second row for one send"
    message = thread["messages"][0]
    assert message["text"] == "Confirming your scan for Thursday at 2pm."


def test_repeated_drains_of_the_same_queued_observation_still_merge(
    dbs: tuple[Path, Path], tmp_path: Path
) -> None:
    """Two hosts can both queue one send; the log still holds one interaction."""
    sms_db, calls_db = dbs
    log = InteractionLog(sms_db=sms_db, calls_db=calls_db)
    log.record_message(_observation(source="dialpad_webhook"))

    host_a = tmp_path / "host-a.jsonl"
    host_b = tmp_path / "host-b.jsonl"
    enqueue_observation(_observation(), path=host_a)
    enqueue_observation(_observation(), path=host_b)

    assert replay_outbox(path=host_a, limit=10)["succeeded"] == 1
    assert replay_outbox(path=host_b, limit=10)["succeeded"] == 1

    assert log.thread("+14155550111", limit=10)["count"] == 1


def test_a_drain_unions_provenance_rather_than_replacing_the_row(
    dbs: tuple[Path, Path], tmp_path: Path
) -> None:
    """One interaction, two witnesses: the merge is visible in the row, not just its count."""
    sms_db, calls_db = dbs
    log = InteractionLog(sms_db=sms_db, calls_db=calls_db)
    log.record_message(_observation(source="dialpad_webhook"))

    outbox = tmp_path / "outbox.jsonl"
    enqueue_observation(_observation(), path=outbox)
    replay_outbox(path=outbox, limit=10)

    message = log.thread("+14155550111", limit=10)["messages"][0]
    assert message["interaction_key"] == "provider:msg-61f0a2d0"
    assert message["dialpad_id"] == "msg-61f0a2d0"
    assert message["source"] == "dialpad_webhook|local_send"
    assert message["body_source"] == "dialpad_webhook|local_send"


def test_a_queued_observation_with_no_body_never_blanks_one_the_webhook_wrote(
    dbs: tuple[Path, Path], tmp_path: Path
) -> None:
    """The body_known gate, exercised through the drain path.

    An observation that carries no text is a provenance event, not a rewrite.
    This is the direction that could actually destroy operational history.
    """
    sms_db, calls_db = dbs
    log = InteractionLog(sms_db=sms_db, calls_db=calls_db)
    log.record_message(_observation(body="Confirming your scan for Thursday at 2pm."))

    outbox = tmp_path / "outbox.jsonl"
    bodyless = _observation()
    del bodyless["body"]
    enqueue_observation(bodyless, path=outbox)
    replay_outbox(path=outbox, limit=10)

    thread = log.thread("+14155550111", limit=10)
    assert thread["count"] == 1
    assert thread["messages"][0]["text"] == "Confirming your scan for Thursday at 2pm."
