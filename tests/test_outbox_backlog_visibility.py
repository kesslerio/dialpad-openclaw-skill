"""Backlog depth and age are derived at run time; a breach announces itself once."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from log_outbox import (
    backlog_state,
    notify_backlog_breach,
    record_drain_run,
    resolve_alert_marker_path,
    resolve_drain_log_path,
    resolve_quarantine_path,
)

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


def _entry(queued_at: str, provider_id: str) -> str:
    entry = {
        "queued_at": queued_at,
        "observation": {
            "provider_id": provider_id,
            "direction": "outbound",
            "from_number": "+14155550140",
            "to_number": "+14155550111",
            "body": "queued body",
            "timestamp": 1770000000000,
            "source": "local_send",
        },
    }
    return json.dumps(entry, separators=(",", ":"))


def _write_outbox(outbox: Path, *queued_ats: str) -> None:
    outbox.write_text(
        "".join(_entry(at, f"msg-{index}") + "\n" for index, at in enumerate(queued_ats)),
        encoding="utf-8",
    )


def test_depth_and_oldest_age_are_derived_from_mixed_age_entries(tmp_path: Path) -> None:
    outbox = tmp_path / "log-outbox.jsonl"
    _write_outbox(
        outbox,
        "2026-09-16T09:00:00Z",  # the previous day
        "2026-09-17T11:30:00Z",
        "2026-09-17T11:59:00Z",
    )

    state = backlog_state(outbox, now=NOW)

    assert state["depth"] == 3
    assert state["oldest_queued_at"] == "2026-09-16T09:00:00Z"
    assert state["oldest_age_seconds"] == pytest.approx((NOW - datetime.fromisoformat("2026-09-16T09:00:00+00:00")).total_seconds())


def test_a_run_where_everything_failed_still_writes_a_full_run_record(tmp_path: Path) -> None:
    outbox = tmp_path / "log-outbox.jsonl"
    _write_outbox(outbox, "2026-09-17T11:00:00Z")
    result = {
        "attempted": 1, "succeeded": 0, "failed": 1, "delivery_failed": 1,
        "observation_rejected": 0, "quarantined": 0, "skipped": 0,
        "budget_hit": 0, "remaining": 1, "hook_error": 0,
    }

    path = record_drain_run(
        outbox, result=result, state=backlog_state(outbox, now=NOW), now=NOW
    )

    assert path == resolve_drain_log_path(outbox)
    assert path.parent == outbox.parent
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert record == {
        "at": "2026-09-17T12:00:00Z",
        "attempted": 1,
        "succeeded": 0,
        "failed": 1,
        "delivery_failed": 1,
        "observation_rejected": 0,
        "quarantined": 0,
        "skipped": 0,
        "budget_hit": 0,
        "hook_error": 0,
        "depth": 1,
        "oldest_age_seconds": 3600.0,
    }


def test_the_marker_is_written_undelivered_first_and_flips_only_on_a_confirmed_send(
    tmp_path: Path,
) -> None:
    outbox = tmp_path / "log-outbox.jsonl"
    marker_path = resolve_alert_marker_path(outbox)
    _write_outbox(outbox, "2026-09-17T01:00:00Z")
    state = backlog_state(outbox, now=NOW)
    seen: list[dict] = []

    def confirmed(text: str, context: dict) -> bool:
        seen.append(json.loads(marker_path.read_text(encoding="utf-8")))
        return True

    outcome = notify_backlog_breach(
        outbox, state=state, notifier=confirmed, threshold_seconds=21600, now=NOW
    )

    assert seen and seen[0]["delivered"] is False, "the marker must exist before anything travels"
    assert outcome["alerted"] is True
    assert json.loads(marker_path.read_text(encoding="utf-8"))["delivered"] is True


@pytest.mark.parametrize("notifier_return", [False, None, "stubbed", "preview"])
def test_a_stubbed_or_unconfirmed_send_never_marks_the_alert_delivered(
    tmp_path: Path, notifier_return: object
) -> None:
    outbox = tmp_path / "log-outbox.jsonl"
    marker_path = resolve_alert_marker_path(outbox)
    _write_outbox(outbox, "2026-09-17T01:00:00Z")
    state = backlog_state(outbox, now=NOW)

    outcome = notify_backlog_breach(
        outbox,
        state=state,
        notifier=lambda text, context: notifier_return,
        threshold_seconds=21600,
        now=NOW,
    )

    assert outcome["alerted"] is False
    assert json.loads(marker_path.read_text(encoding="utf-8"))["delivered"] is False


def test_two_consecutive_breaching_runs_send_one_alert(tmp_path: Path) -> None:
    outbox = tmp_path / "log-outbox.jsonl"
    _write_outbox(outbox, "2026-09-17T01:00:00Z")
    state = backlog_state(outbox, now=NOW)
    sent: list[str] = []

    def notifier(text: str, context: dict) -> bool:
        sent.append(text)
        return True

    notify_backlog_breach(outbox, state=state, notifier=notifier, threshold_seconds=21600, now=NOW)
    second = notify_backlog_breach(
        outbox, state=state, notifier=notifier, threshold_seconds=21600,
        now=NOW + timedelta(minutes=10),
    )

    assert len(sent) == 1
    assert second["suppressed"] is True


def test_clearing_the_backlog_allows_the_next_breach_to_alert(tmp_path: Path) -> None:
    outbox = tmp_path / "log-outbox.jsonl"
    marker_path = resolve_alert_marker_path(outbox)
    _write_outbox(outbox, "2026-09-17T01:00:00Z")
    sent: list[str] = []

    def notifier(text: str, context: dict) -> bool:
        sent.append(text)
        return True

    notify_backlog_breach(
        outbox,
        state=backlog_state(outbox, now=NOW),
        notifier=notifier,
        threshold_seconds=21600,
        now=NOW,
    )
    cleared = notify_backlog_breach(
        outbox,
        state={"depth": 0, "oldest_queued_at": None, "oldest_age_seconds": 0.0},
        notifier=notifier,
        threshold_seconds=21600,
        now=NOW,
    )
    assert cleared["cleared"] is True
    assert not marker_path.exists()

    _write_outbox(outbox, "2026-09-17T05:00:00Z")
    notify_backlog_breach(
        outbox,
        state=backlog_state(outbox, now=NOW),
        notifier=notifier,
        threshold_seconds=21600,
        now=NOW,
    )

    assert len(sent) == 2, "a cleared breach is a fresh breach, not a suppressed one"


def test_the_dedupe_key_ignores_an_age_that_ticks_on_its_own(tmp_path: Path) -> None:
    """A clock-derived field in a delivery hash re-sends an unchanged report."""
    outbox = tmp_path / "log-outbox.jsonl"
    marker_path = resolve_alert_marker_path(outbox)
    _write_outbox(outbox, "2026-09-17T01:00:00Z")
    first_age = backlog_state(outbox, now=NOW)["oldest_age_seconds"]

    notify_backlog_breach(
        outbox,
        state=backlog_state(outbox, now=NOW),
        notifier=lambda text, context: True,
        threshold_seconds=21600,
        now=NOW,
    )
    first = json.loads(marker_path.read_text(encoding="utf-8"))

    later_age = backlog_state(outbox, now=NOW + timedelta(hours=5))["oldest_age_seconds"]
    notify_backlog_breach(
        outbox,
        state=backlog_state(outbox, now=NOW + timedelta(hours=5)),
        notifier=lambda text, context: True,
        threshold_seconds=21600,
        now=NOW + timedelta(hours=5),
    )
    second = json.loads(marker_path.read_text(encoding="utf-8"))

    assert later_age > first_age, "the age really does tick while nothing else changes"
    assert first["breach_id"] == second["breach_id"]


def test_an_unreadable_marker_is_treated_as_undelivered(tmp_path: Path) -> None:
    outbox = tmp_path / "log-outbox.jsonl"
    marker_path = resolve_alert_marker_path(outbox)
    _write_outbox(outbox, "2026-09-17T01:00:00Z")
    marker_path.write_text("{half a marker", encoding="utf-8")
    sent: list[str] = []

    outcome = notify_backlog_breach(
        outbox,
        state=backlog_state(outbox, now=NOW),
        notifier=lambda text, context: sent.append(text) or True,
        threshold_seconds=21600,
        now=NOW,
    )

    assert len(sent) == 1, "an unreadable marker must not silence the lane"
    assert outcome["alerted"] is True


def test_a_dry_run_writes_no_marker_and_clears_nothing(tmp_path: Path) -> None:
    outbox = tmp_path / "log-outbox.jsonl"
    marker_path = resolve_alert_marker_path(outbox)
    _write_outbox(outbox, "2026-09-17T01:00:00Z")
    marker_path.write_text(
        json.dumps({"breach_id": "existing", "delivered": True}), encoding="utf-8"
    )

    outcome = notify_backlog_breach(
        outbox,
        state=backlog_state(outbox, now=NOW),
        notifier=lambda text, context: True,
        threshold_seconds=21600,
        now=NOW,
        dry_run=True,
    )

    assert outcome["dry_run"] is True
    assert outcome["breach"] is True
    assert json.loads(marker_path.read_text(encoding="utf-8"))["delivered"] is True

    cleared = notify_backlog_breach(
        outbox,
        state={"depth": 0, "oldest_queued_at": None, "oldest_age_seconds": 0.0},
        notifier=lambda text, context: True,
        threshold_seconds=21600,
        now=NOW,
        dry_run=True,
    )
    assert cleared["dry_run"] is True
    assert marker_path.exists()


def test_the_observability_files_are_named_siblings_of_the_outbox(tmp_path: Path) -> None:
    outbox = tmp_path / "log-outbox.jsonl"

    assert resolve_drain_log_path(outbox).name == "log-outbox-drains.jsonl"
    assert resolve_alert_marker_path(outbox).name == "log-outbox-alert.json"
    assert resolve_quarantine_path(outbox).name == "log-outbox-quarantine.jsonl"


def test_a_growing_backlog_does_not_re_alert_the_same_breach(tmp_path: Path) -> None:
    """Depth is not stable during an outage, so it cannot name the breach.

    Every send that fails while a breach is open grows the queue, so a key
    containing depth notifies again on every failed send of a long outage.
    """
    outbox = tmp_path / "log-outbox.jsonl"
    _write_outbox(outbox, "2026-09-17T01:00:00Z")
    sent: list[str] = []

    def notifier(text: str, context: dict) -> bool:
        sent.append(text)
        return True

    notify_backlog_breach(
        outbox,
        state=backlog_state(outbox, now=NOW),
        notifier=notifier,
        threshold_seconds=21600,
        now=NOW,
    )
    with outbox.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "queued_at": "2026-09-17T02:30:00Z",
                    "observation": {"provider_id": "msg-later"},
                }
            )
            + "\n"
        )

    grown = backlog_state(outbox, now=NOW)
    second = notify_backlog_breach(
        outbox,
        state=grown,
        notifier=notifier,
        threshold_seconds=21600,
        now=NOW + timedelta(minutes=10),
    )

    assert grown["depth"] == 2, "the queue really did grow"
    assert len(sent) == 1
    assert second["suppressed"] is True


def test_the_alert_claim_suppresses_a_rival_that_never_read_the_marker(
    tmp_path: Path,
) -> None:
    """Two drains can both see no marker; the send still happens once.

    An atomic marker replace does not make read-check-send atomic, so removing
    the marker is exactly what a racing second process did: it looked first.
    """
    outbox = tmp_path / "log-outbox.jsonl"
    marker_path = resolve_alert_marker_path(outbox)
    _write_outbox(outbox, "2026-09-17T01:00:00Z")
    sent: list[str] = []

    def notifier(text: str, context: dict) -> bool:
        sent.append(text)
        return False

    notify_backlog_breach(
        outbox,
        state=backlog_state(outbox, now=NOW),
        notifier=notifier,
        threshold_seconds=21600,
        now=NOW,
    )
    assert json.loads(marker_path.read_text(encoding="utf-8"))["delivered"] is False

    marker_path.unlink()
    second = notify_backlog_breach(
        outbox,
        state=backlog_state(outbox, now=NOW),
        notifier=notifier,
        threshold_seconds=21600,
        now=NOW + timedelta(minutes=1),
    )

    assert second["suppressed"] is True
    assert len(sent) == 1


def test_a_claim_left_by_a_dead_process_expires(tmp_path: Path) -> None:
    """A claim must not outlive the process that took it, or it silences the host."""
    outbox = tmp_path / "log-outbox.jsonl"
    _write_outbox(outbox, "2026-09-17T01:00:00Z")
    notify_backlog_breach(
        outbox,
        state=backlog_state(outbox, now=NOW),
        notifier=lambda text, context: False,
        threshold_seconds=21600,
        now=NOW,
    )
    claim = resolve_alert_marker_path(outbox).with_name(
        f".{resolve_alert_marker_path(outbox).stem}.claim"
    )
    assert claim.exists()
    old = time.time() - 3600
    os.utime(claim, (old, old))

    sent: list[str] = []

    def notifier(text: str, context: dict) -> bool:
        sent.append(text)
        return True

    outcome = notify_backlog_breach(
        outbox,
        state=backlog_state(outbox, now=NOW),
        notifier=notifier,
        threshold_seconds=21600,
        now=NOW + timedelta(hours=1),
    )

    assert outcome["alerted"] is True
    assert len(sent) == 1
