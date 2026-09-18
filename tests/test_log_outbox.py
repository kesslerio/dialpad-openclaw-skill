from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import log_outbox
from interaction_log import InteractionLog
from log_api_client import LogApiError
from log_outbox import (
    drain_on_use,
    enqueue_observation,
    record_outbound_observation,
    replay_outbox,
    resolve_quarantine_path,
)


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
        "quarantined": 0,
        "rejected": 0,
        "skipped": 0,
        "budget_hit": 0,
        "remaining": 0,
        "hook_error": 0,
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
    assert result["remaining"] == 1
    assert result["rejected"] == 1
    codes = [
        json.loads(line)["failure"]["code"]
        for line in outbox.read_text(encoding="utf-8").splitlines()
    ]
    # Only the retryable one stays queued. A permanent refusal would otherwise
    # retry from the head and spend the attempt cap on itself every run, so
    # valid observations behind it could never reach the log.
    assert codes == ["network_error"]
    reject_lines = log_outbox.resolve_rejects_path(outbox).read_text(
        encoding="utf-8"
    ).splitlines()
    assert [json.loads(line)["failure"]["code"] for line in reject_lines] == [
        "invalid_argument"
    ]


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


def test_entry_appended_during_a_drain_survives_it(tmp_path: Path, monkeypatch) -> None:
    """A send appending mid-drain must not be silently deleted by the drain's commit."""
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    enqueue_observation(_observation(), path=outbox)

    appended: list[bool] = []
    original_record = log_outbox._record_once

    def racy_record(observation: dict) -> dict:
        result = original_record(observation)
        if not appended:
            appended.append(True)
            enqueue_observation(
                {**_observation(), "provider_id": "msg-interloper"}, path=outbox
            )
        return result

    with patch("log_outbox._record_once", side_effect=racy_record):
        result = replay_outbox(path=outbox, limit=10)

    assert result["succeeded"] == 1
    assert result["attempted"] == 1
    assert result["failed"] == 0
    lines = outbox.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["observation"]["provider_id"] == "msg-interloper"


def test_a_second_drain_attempt_records_nothing_already_recorded(tmp_path: Path, monkeypatch) -> None:
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    enqueue_observation(_observation(), path=outbox)

    first = replay_outbox(path=outbox, limit=10)
    second = replay_outbox(path=outbox, limit=10)

    assert first["succeeded"] == 1
    assert second == {
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "delivery_failed": 0,
        "observation_rejected": 0,
        "quarantined": 0,
        "rejected": 0,
        "skipped": 0,
        "budget_hit": 0,
        "remaining": 0,
        "hook_error": 0,
    }
    assert InteractionLog(sms_db=sms_db, calls_db=tmp_path / "calls.db").thread(
        "+14155550111", limit=10
    )["count"] == 1


def test_a_poison_line_is_quarantined_and_the_active_file_can_reach_zero(
    tmp_path: Path, monkeypatch
) -> None:
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    outbox.write_text("this is not json\n", encoding="utf-8")
    enqueue_observation(_observation(), path=outbox)

    result = replay_outbox(path=outbox, limit=10)

    assert result["succeeded"] == 1
    assert result["quarantined"] == 1
    assert result["remaining"] == 0
    assert not outbox.exists()
    quarantine = resolve_quarantine_path(outbox)
    assert quarantine.read_text(encoding="utf-8") == "this is not json\n"


def test_a_failed_quarantine_write_keeps_the_line_instead_of_dropping_it(
    tmp_path: Path, monkeypatch
) -> None:
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    outbox.write_text("this is not json\n", encoding="utf-8")

    with patch("log_outbox._append_durable", side_effect=OSError("read-only")):
        result = replay_outbox(path=outbox, limit=10)

    assert result["quarantined"] == 0
    assert result["remaining"] == 1
    assert outbox.read_text(encoding="utf-8") == "this is not json\n"


def test_a_held_lock_makes_the_drain_decline_rather_than_report_no_work(
    tmp_path: Path, monkeypatch
) -> None:
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_LOG_OUTBOX_LOCK_SECONDS", "0.05")
    enqueue_observation(_observation(), path=outbox)

    with log_outbox.outbox_lock(outbox):
        result = replay_outbox(path=outbox, limit=10)

    assert result["skipped"] == 1
    assert result["attempted"] == 0
    assert result["remaining"] == 1


def test_a_lock_file_left_behind_by_a_dead_process_does_not_block_the_next_drain(
    tmp_path: Path, monkeypatch
) -> None:
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    enqueue_observation(_observation(), path=outbox)
    lock_file = outbox.parent / f".{outbox.stem}.lock"
    lock_file.write_text("stale", encoding="utf-8")

    result = replay_outbox(path=outbox, limit=10)

    assert result["skipped"] == 0
    assert result["succeeded"] == 1
    assert not outbox.exists()


def test_drain_on_use_clears_a_reachable_backlog(tmp_path: Path, monkeypatch) -> None:
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    enqueue_observation(_observation(), path=outbox)

    result = drain_on_use(path=outbox)

    assert result["succeeded"] == 1
    assert result["skipped"] == 0
    assert not outbox.exists()


def test_drain_on_use_keeps_entries_with_reasons_when_the_log_is_unreachable(
    tmp_path: Path, monkeypatch
) -> None:
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_LOG_URL", "http://127.0.0.1:18887")
    monkeypatch.setenv("DIALPAD_LOG_TOKEN", "unit-token")
    enqueue_observation(_observation(), path=outbox)

    with patch(
        "log_outbox._record_once",
        side_effect=LogApiError("connection refused", code="network_error", retryable=True),
    ):
        result = drain_on_use(path=outbox)

    assert result["succeeded"] == 0
    assert result["delivery_failed"] == 1
    assert result["remaining"] == 1
    stored = json.loads(outbox.read_text(encoding="utf-8").splitlines()[0])
    assert stored["failure"]["code"] == "network_error"


def test_drain_on_use_returns_within_its_budget_against_a_black_holing_log(
    tmp_path: Path, monkeypatch
) -> None:
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    for index in range(4):
        enqueue_observation({**_observation(), "provider_id": f"msg-{index}"}, path=outbox)

    clock = iter([0.0, 0.0, 0.0, 100.0, 100.0, 100.0])
    with patch("log_outbox.time.monotonic", side_effect=lambda: next(clock)), patch(
        "log_outbox._record_once", side_effect=TimeoutError("black hold")
    ):
        result = replay_outbox(path=outbox, limit=10, budget_seconds=1.0)

    assert result["budget_hit"] == 1
    assert result["attempted"] < 4
    assert result["remaining"] >= 1


def test_drain_on_use_touches_nothing_without_a_configured_destination(
    tmp_path: Path, monkeypatch
) -> None:
    outbox = tmp_path / "outbox.jsonl"
    for name in (
        "DIALPAD_LOG_URL",
        "DIALPAD_LOG_TOKEN",
        "DIALPAD_SMS_DB",
        "DIALPAD_LOG_OUTBOX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(log_outbox.sms_sqlite, "DB_PATH", str(tmp_path / "absent.db"))
    outbox.write_text("queued\n", encoding="utf-8")

    with patch("log_outbox.replay_outbox") as replay:
        result = drain_on_use(path=outbox)

    assert result["attempted"] == 0
    replay.assert_not_called()
    assert outbox.read_text(encoding="utf-8") == "queued\n"
    assert not any(entry.name.endswith(".lock") for entry in tmp_path.iterdir())


def test_drain_on_use_never_creates_an_outbox_that_is_not_there(
    tmp_path: Path, monkeypatch
) -> None:
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))

    result = drain_on_use(path=outbox)

    assert result == {
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "delivery_failed": 0,
        "observation_rejected": 0,
        "quarantined": 0,
        "rejected": 0,
        "skipped": 0,
        "budget_hit": 0,
        "remaining": 0,
        "hook_error": 0,
    }
    assert not outbox.exists()
    assert not any(path.name.endswith(".lock") for path in tmp_path.iterdir())


def test_drain_stops_at_the_entry_cap_and_keeps_the_remainder_in_append_order(
    tmp_path: Path, monkeypatch
) -> None:
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    monkeypatch.setenv("DIALPAD_LOG_OUTBOX_DRAIN_LIMIT", "2")
    for index in range(4):
        enqueue_observation({**_observation(), "provider_id": f"msg-{index}"}, path=outbox)

    result = drain_on_use(path=outbox)

    assert result["attempted"] == 2
    assert result["succeeded"] == 2
    assert result["remaining"] == 2
    kept = [json.loads(line)["observation"]["provider_id"] for line in outbox.read_text(encoding="utf-8").splitlines()]
    assert kept == ["msg-2", "msg-3"]


def test_drain_on_use_never_raises_at_its_caller(tmp_path: Path, monkeypatch) -> None:
    """A confirmed send must not be able to fail because of the drain hook."""
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    enqueue_observation(_observation(), path=outbox)

    with patch("log_outbox.replay_outbox", side_effect=RuntimeError("outbox on fire")):
        result = drain_on_use(path=outbox)

    assert result["hook_error"] == 1
    assert result["succeeded"] == 0
    assert outbox.exists()


def test_drain_on_use_reports_no_hook_error_on_a_normal_run(tmp_path: Path, monkeypatch) -> None:
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))

    result = drain_on_use(path=outbox)

    assert result["hook_error"] == 0


def test_the_drain_does_not_hold_the_lock_while_it_is_trying(
    tmp_path: Path, monkeypatch
) -> None:
    """The queue lock must be free during an attempt, not just between runs.

    An attempt is a network call of up to DIALPAD_LOG_TIMEOUT with no retry
    budget, so holding the lock across it means every sender behind us waits on
    a host that is already unreachable. Worse, a sender whose wait expired
    appends with no lock at all, and an append landing after the commit's
    re-read but before its replace is written to a file that is about to be
    unlinked. That is the loss this whole unit exists to close, so the
    invariant is pinned rather than left to timing.
    """
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    enqueue_observation(_observation(), path=outbox)
    observed: list[bool] = []

    def probe_and_record(observation: dict) -> dict:
        with log_outbox.outbox_lock(outbox, timeout_seconds=0.0) as acquired:
            observed.append(acquired)
        return original_record(observation)

    original_record = log_outbox._record_once
    with patch("log_outbox._record_once", side_effect=probe_and_record):
        result = replay_outbox(path=outbox, limit=5)

    assert observed == [True], "the drain held the lock across a network attempt"
    assert result["succeeded"] == 1


def test_permanent_rejections_cannot_starve_the_entries_behind_them(
    tmp_path: Path, monkeypatch
) -> None:
    """Five refusals at the head must not cost the queue everything after them.

    The attempt cap is spent from the head, so refusals that can never succeed
    would take the whole cap on every run and a valid observation behind them
    would never reach the log. This is the one-poison-line rule.
    """
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    monkeypatch.setenv("DIALPAD_LOG_URL", "http://127.0.0.1:18887")
    monkeypatch.setenv("DIALPAD_LOG_TOKEN", "unit-token")
    for index in range(5):
        enqueue_observation(
            {**_observation(), "provider_id": f"msg-bad-{index}"}, path=outbox
        )
    enqueue_observation({**_observation(), "provider_id": "msg-good"}, path=outbox)

    def refuse_bad(observation: dict) -> dict:
        if observation["provider_id"].startswith("msg-bad-"):
            raise LogApiError("body is required", code="invalid_argument", retryable=False)
        return {"ok": True}

    with patch("log_outbox._record_once", side_effect=refuse_bad):
        first = replay_outbox(path=outbox, limit=5)
        assert first["rejected"] == 5
        assert first["remaining"] == 1

        second = replay_outbox(path=outbox, limit=5)

    assert second["attempted"] == 1
    assert second["succeeded"] == 1
    assert second["remaining"] == 0


def test_an_unhashable_identity_quarantines_rather_than_stalls_the_queue(
    tmp_path: Path, monkeypatch
) -> None:
    """An array where a scalar belongs must cost one line, not the whole drain."""
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    monkeypatch.setenv("DIALPAD_LOG_URL", "http://127.0.0.1:18887")
    monkeypatch.setenv("DIALPAD_LOG_TOKEN", "unit-token")
    outbox.write_text(
        json.dumps(
            {"queued_at": "2026-09-17T01:00:00Z", "observation": {"provider_id": ["a"]}}
        )
        + "\n"
        + json.dumps(
            {"queued_at": "2026-09-17T01:01:00Z", "observation": {"provider_id": "msg-ok"}}
        )
        + "\n",
        encoding="utf-8",
    )

    with patch("log_outbox._record_once", return_value={"ok": True}):
        result = replay_outbox(path=outbox, limit=5)

    assert result["attempted"] == 2
    assert result["succeeded"] == 2
    assert not outbox.exists()


def test_a_missing_route_is_an_infrastructure_fault_not_a_bad_observation(
    tmp_path: Path, monkeypatch
) -> None:
    """A 404 on the route is the destination being wrong, not the payload.

    The same queued observation is deliverable once the destination is repaired,
    so classifying it as a rejection would dead-letter perfectly good data.
    """
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    enqueue_observation(_observation(), path=outbox)

    with patch(
        "log_outbox._record_once",
        side_effect=LogApiError("no such route", code="not_found", retryable=True),
    ):
        result = replay_outbox(path=outbox, limit=5)

    assert result["delivery_failed"] == 1
    assert result["observation_rejected"] == 0
    assert result["rejected"] == 0
    assert result["remaining"] == 1
    assert not log_outbox.resolve_rejects_path(outbox).exists()
