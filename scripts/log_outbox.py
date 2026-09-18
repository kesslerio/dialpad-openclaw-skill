#!/usr/bin/env python3
"""Durable record-only outbox for successful outbound SMS observations."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


import sms_sqlite
from interaction_log import InteractionLog
from log_api_client import LogApiError, configured_log_url, record_message as remote_record_message


DEFAULT_OUTBOX = Path("~/.dialpad/log-outbox.jsonl")

# Codes that mean the interaction log refused this specific observation, as opposed
# to being unreachable. Retrying identical content against these will not help.
# Only codes that mean this payload was refused. A 404 on the route is an
# infrastructure fault: the identical queued observation becomes deliverable
# once the destination is repaired, so classifying it as a rejection would
# dead-letter data that is perfectly good.
_REJECTION_CODES = frozenset({"invalid_argument"})

DEFAULT_LOCK_TIMEOUT_SECONDS = 2.0

# The drain wants a short wait so it can decline fast and never delay a
# caller. An enqueue wants the opposite: appending without the lock can land on
# an inode a concurrent commit is about to replace, so it waits long enough
# that giving up means the lock is genuinely stuck rather than merely busy.
DEFAULT_ENQUEUE_LOCK_SECONDS = 5.0

# A drain may run on a path a caller is waiting on, and every request costs up to
# DIALPAD_LOG_TIMEOUT (5s by default) with no retry budget of its own. These two
# caps are what make that safe: without them 18 stale entries could add roughly
# 90 seconds to a user-visible send.
DEFAULT_DRAIN_LIMIT = 5
DEFAULT_DRAIN_BUDGET_SECONDS = 2.0
DEFAULT_ALERT_AFTER_SECONDS = 6 * 60 * 60

# Alert delivery runs on the same hot path that just spent its drain budget.
# An alert that cannot travel that fast stays undelivered and is retried on the
# next use, which is the right trade when the alternative is every send paying
# for a wedged notification command.
DEFAULT_NOTIFY_TIMEOUT_SECONDS = 2.0

# How long an unclaimed alert claim may sit before another process may take
# it over. Small on purpose: the claim only covers the notifier call itself,
# so a claim older than this means the process that took it died mid-alert and
# must not keep the host silent.
DEFAULT_ALERT_CLAIM_STALE_SECONDS = 15 * 60


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def _lock_timeout_seconds() -> float:
    return _positive_float_env(
        "DIALPAD_LOG_OUTBOX_LOCK_SECONDS", DEFAULT_LOCK_TIMEOUT_SECONDS
    )


def _enqueue_lock_timeout_seconds() -> float:
    return _positive_float_env(
        "DIALPAD_LOG_OUTBOX_ENQUEUE_LOCK_SECONDS", DEFAULT_ENQUEUE_LOCK_SECONDS
    )


def resolve_outbox_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return Path(os.environ.get("DIALPAD_LOG_OUTBOX", str(DEFAULT_OUTBOX))).expanduser()


def resolve_rejects_path(outbox: Path) -> Path:
    """Observations the interaction log refused on content.

    Kept apart from the unreadable-line quarantine: these parsed fine and are
    forensically interesting, they just can never succeed on retry.
    """
    return outbox.with_name(f"{outbox.stem}-rejected{outbox.suffix or '.jsonl'}")


def resolve_quarantine_path(outbox: Path) -> Path:
    """Sibling of the active outbox, so a poison line costs one line and no more."""
    return outbox.with_name(f"{outbox.stem}-quarantine{outbox.suffix or '.jsonl'}")


def _lock_path(outbox: Path) -> Path:
    return outbox.with_name(f".{outbox.stem}.lock")


@contextmanager
def outbox_lock(outbox: Path, *, timeout_seconds: float | None = None) -> Iterator[bool]:
    """Best-effort exclusive lock across a whole outbox read, drain, and commit.

    Held on a separate file because the active outbox is itself replaced on commit.
    A caller that cannot get it within the timeout proceeds without it rather than
    delaying a confirmed send; a drain instead declines to run.
    """
    timeout = _lock_timeout_seconds() if timeout_seconds is None else timeout_seconds
    path = _lock_path(outbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    handle = open(path, "a+", encoding="utf-8")
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        yield acquired
    finally:
        try:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()



def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact_token(text: str) -> str:
    token = os.environ.get("DIALPAD_LOG_TOKEN", "").strip()
    if not token:
        return text
    return text.replace(token, "[redacted]")


def _failure_class(code: str) -> str:
    """Split infrastructure trouble from a rejection of this observation's own content.

    Anything that is not a deliberate refusal of this payload counts as delivery
    failure, so an outage or a misconfiguration can never be mistaken for bad data.
    """
    return "observation_rejected" if code in _REJECTION_CODES else "delivery_failed"


def _failure_from_error(error: BaseException) -> dict[str, Any]:
    code = getattr(error, "code", None)
    code_text = str(code) if code else type(error).__name__
    retryable = getattr(error, "retryable", None)
    if not isinstance(retryable, bool):
        retryable = isinstance(error, (OSError, TimeoutError))
    message = _redact_token(str(error) or type(error).__name__)[:300]
    return {
        "at": _now(),
        "code": code_text,
        "error_type": type(error).__name__,
        "retryable": retryable,
        "failure_class": _failure_class(code_text),
        "message": message,
    }


def _sanitised_failure(reason: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(reason, dict):
        return None
    safe = dict(reason)
    if "message" in safe:
        safe["message"] = _redact_token(str(safe["message"]))[:300]
    if "failure_class" not in safe:
        safe["failure_class"] = _failure_class(str(safe.get("code", "")))
    return safe


def enqueue_observation(
    observation: dict[str, Any],
    *,
    path: Path | str | None = None,
    reason: dict[str, Any] | None = None,
) -> Path:
    target = resolve_outbox_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {"queued_at": _now(), "observation": observation}
    failure = _sanitised_failure(reason)
    if failure is not None:
        entry["failure"] = failure
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    with outbox_lock(target, timeout_seconds=_enqueue_lock_timeout_seconds()) as acquired:
        # Appending without the lock is a last resort, not an outage path: an
        # append that lands after a commit re-reads can be replaced out of
        # existence. It stays available because losing a confirmed send's
        # record is the worse failure.
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    return target


def _local_destination_available() -> bool:
    if os.environ.get("DIALPAD_SMS_DB", "").strip():
        return True
    return Path(sms_sqlite.DB_PATH).exists()


def _memory_sync_configured() -> bool:
    return bool(
        configured_log_url()
        or os.environ.get("DIALPAD_SMS_DB", "").strip()
        or os.environ.get("DIALPAD_LOG_OUTBOX", "").strip()
        or Path(sms_sqlite.DB_PATH).exists()
    )


def _record_once(observation: dict[str, Any]) -> dict[str, Any]:
    if configured_log_url():
        result, _meta = remote_record_message(observation)
        return {**result, "memory_sync": "synced", "memory_source": "shared_log"}
    if not _local_destination_available():
        raise LogApiError(
            "no local interaction log is configured",
            code="invalid_argument",
            retryable=False,
        )
    result = InteractionLog().record_message(observation)
    return {**result, "memory_sync": "synced", "memory_source": "local_log"}


def build_outbound_observation(
    provider_result: Any,
    *,
    to_numbers: list[str],
    from_number: str,
    body: str,
) -> dict[str, Any]:
    result = provider_result if isinstance(provider_result, dict) else {}
    provider_id = result.get("id") or result.get("message_id") or result.get("dialpad_id")
    provider_timestamp = result.get("created_date") or result.get("timestamp") or result.get("date_created")
    return {
        "provider_id": provider_id,
        "direction": "outbound",
        "from_number": from_number,
        "to_number": to_numbers,
        "body": body,
        "timestamp": provider_timestamp or int(datetime.now(timezone.utc).timestamp() * 1000),
        "observed_at": _now(),
        "source": "local_send",
        "message_status": result.get("message_status") or result.get("status"),
        "delivery_result": result.get("message_delivery_result") or result.get("delivery_result"),
    }


def record_outbound_observation(
    provider_result: Any,
    *,
    to_numbers: list[str],
    from_number: str,
    body: str,
) -> dict[str, Any]:
    """Record a confirmed send; failures only queue the observation, never resend."""
    observation = build_outbound_observation(
        provider_result,
        to_numbers=to_numbers,
        from_number=from_number,
        body=body,
    )
    # Test/dev checkouts without a configured canonical DB or private API
    # should not create a home-directory queue as a side effect of a mocked
    # provider call. On theshop the default canonical DB exists; on Grok the
    # explicit DIALPAD_LOG_URL enables the queue path.
    if not _memory_sync_configured():
        return {"memory_sync": "disabled"}
    try:
        return _record_once(observation)
    except Exception as error:  # noqa: BLE001 - send already succeeded; queue record-only retry.
        failure = _failure_from_error(error)
        try:
            target = enqueue_observation(observation, reason=failure)
        except Exception:
            return {
                "memory_sync": "failed",
                "memory_error": type(error).__name__,
                "memory_failure_code": failure["code"],
            }
        return {
            "memory_sync": "pending",
            "memory_outbox": str(target),
            "memory_error": type(error).__name__,
            "memory_failure_code": failure["code"],
            "memory_failure_class": failure["failure_class"],
        }


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _parse_entry(line: str) -> dict[str, Any] | None:
    """Return the entry dict, or None when the line carries no usable observation."""
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed if isinstance(parsed.get("observation"), dict) else None


def _entry_identity(parsed: dict[str, Any]) -> tuple[Any, Any]:
    """Queue time plus provider id.

    Two entries sharing an identity carry the same observation, and recording it
    twice is already a merge rather than a duplicate, so collapsing them is safe.
    """
    observation = parsed.get("observation") or {}
    # Stringified so an array or object where a scalar belongs still yields a
    # hashable key. An unhashable identity would raise mid-drain and stall
    # every valid entry behind it, which is the failure quarantine exists to
    # prevent.
    return (
        str(parsed.get("queued_at")),
        str(observation.get("provider_id")) if observation.get("provider_id") is not None else None,
    )


def _rewrite(path: Path, lines: list[str]) -> None:
    if not lines:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _append_durable(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())


def _line_with_failure(line: str, failure: dict[str, Any]) -> str:
    """Re-serialise one queued line with its newest failure, keeping every other field."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(entry, dict):
        return line
    entry["failure"] = failure
    return json.dumps(entry, separators=(",", ":")) + "\n"


def _empty_result() -> dict[str, int]:
    return {
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


def replay_outbox(
    *,
    path: Path | str | None = None,
    limit: int = 100,
    budget_seconds: float | None = None,
) -> dict[str, int]:
    """Record queued observations, then compact the outbox against the live file.

    The commit re-reads rather than reusing the first read, so an observation
    appended by a concurrent send during this run survives it instead of being
    written back out of existence.
    """
    target = resolve_outbox_path(path)
    result = _empty_result()
    with outbox_lock(target) as acquired:
        if not acquired:
            result["skipped"] = 1
            result["remaining"] = len(_read_lines(target))
            return result

        scheduled: list[tuple[tuple[Any, Any], dict[str, Any]]] = []
        for line in _read_lines(target):
            parsed = _parse_entry(line)
            if parsed is not None:
                scheduled.append((_entry_identity(parsed), parsed["observation"]))

    # Deliberately outside the lock. One attempt is a network call of up to
    # DIALPAD_LOG_TIMEOUT with no retry budget of its own, so holding the queue
    # lock across it would make every sender behind us wait on a host that is
    # already unreachable - and a sender whose lock wait expired would then be
    # free to append while we still held the file.
    drained: set[tuple[Any, Any]] = set()
    retries: dict[tuple[Any, Any], dict[str, Any]] = {}
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
    for identity, observation in scheduled:
        if result["attempted"] >= limit:
            break
        if deadline is not None and time.monotonic() >= deadline:
            # Only counts once work was actually possible, so a generous
            # budget that never mattered is not reported as a breach.
            if result["attempted"]:
                result["budget_hit"] = 1
            break
        result["attempted"] += 1
        try:
            _record_once(observation)
        except Exception as error:  # noqa: BLE001 - keep the entry, record why it is still here.
            failure = _failure_from_error(error)
            retries[identity] = failure
            result["failed"] += 1
            if failure["failure_class"] == "observation_rejected":
                result["observation_rejected"] += 1
            else:
                result["delivery_failed"] += 1
        else:
            drained.add(identity)
            result["succeeded"] += 1

    # The commit takes the lock back on its own. Everything in this block is
    # local file work, so an enqueue waiting behind it clears in microseconds
    # rather than behind a network timeout, and cannot be appending to the file
    # we are about to replace.
    with outbox_lock(target) as acquired:
        if not acquired:
            # Counts stay as they are: this run really did record those
            # observations. The entries remain queued, and recording one twice
            # merges rather than duplicates, so the next holder retries them.
            result["skipped"] = 1
            result["remaining"] = len(_read_lines(target))
            return result

        keep: list[str] = []
        poison: list[str] = []
        rejected: list[str] = []
        for line in _read_lines(target):
            parsed = _parse_entry(line)
            if parsed is None:
                poison.append(line)
                continue
            identity = _entry_identity(parsed)
            if identity in drained:
                continue
            if identity in retries:
                failure = retries[identity]
                if failure.get("failure_class") == "observation_rejected":
                    # Annotated on the way out: a line that leaves the queue
                    # carries the reason it left, or it becomes unexplainable.
                    rejected.append(_line_with_failure(line, failure))
                else:
                    keep.append(_line_with_failure(line, failure))
            else:
                keep.append(line)

        for lines, destination, key in (
            (poison, resolve_quarantine_path(target), "quarantined"),
            (rejected, resolve_rejects_path(target), "rejected"),
        ):
            if not lines:
                continue
            try:
                _append_durable(destination, lines)
            except OSError:
                # Never drop a line we failed to move somewhere else, even though
                # keeping it means it costs another attempt next run.
                keep.extend(lines)
            else:
                result[key] = len(lines)

        _rewrite(target, keep)
        result["remaining"] = len(keep)
    return result


def resolve_drain_log_path(outbox: Path) -> Path:
    """Fixed sibling holding one record per drain run.

    Nothing reads this to decide behavior. It exists so a later pass, or a
    person, can tell when this host last drained at all.
    """
    return outbox.with_name(f"{outbox.stem}-drains{outbox.suffix or '.jsonl'}")


def resolve_alert_marker_path(outbox: Path) -> Path:
    """One object, not a stream, so it is .json rather than the outbox's .jsonl."""
    return outbox.with_name(f"{outbox.stem}-alert.json")


def _alert_threshold_seconds() -> float:
    return _positive_float_env(
        "DIALPAD_LOG_OUTBOX_ALERT_AFTER_SECONDS", DEFAULT_ALERT_AFTER_SECONDS
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def backlog_state(outbox: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Depth and the age of the oldest entry, derived from the file itself.

    Deliberately not indexed. The outbox is the only truth about what is
    waiting, and a second copy of that would need its own repair tooling.
    """
    moment = now or datetime.now(timezone.utc)
    depth = 0
    oldest: datetime | None = None
    for line in _read_lines(outbox):
        parsed = _parse_entry(line)
        if parsed is None:
            continue
        depth += 1
        queued_at = _parse_timestamp(parsed.get("queued_at"))
        if queued_at is not None and (oldest is None or queued_at < oldest):
            oldest = queued_at
    age_seconds = max(0.0, (moment - oldest).total_seconds()) if oldest else 0.0
    return {
        "depth": depth,
        "oldest_queued_at": oldest.isoformat().replace("+00:00", "Z") if oldest else None,
        "oldest_age_seconds": round(age_seconds, 3),
    }


def _read_marker(marker_path: Path) -> dict[str, Any] | None:
    """None means no marker. An unreadable marker is treated as undelivered."""
    if not marker_path.exists():
        return None
    try:
        parsed = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"delivered": False}
    if not isinstance(parsed, dict):
        return {"delivered": False}
    return {**parsed, "delivered": parsed.get("delivered") is True}


def _write_marker(marker_path: Path, marker: dict[str, Any]) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=marker_path.parent, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(marker, handle, separators=(",", ":"), sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, marker_path)


def _deliver_alert(notifier: Callable[[str, dict[str, Any]], bool] | None, text: str, context: dict[str, Any]) -> bool:
    if notifier is None:
        return False
    try:
        return notifier(text, context) is True
    except Exception:  # noqa: BLE001 - an alert that cannot travel is still undelivered.
        return False


def _claim_alert(claim_path: Path, stale_after_seconds: float) -> bool:
    """Take the one-shot right to notify, across processes.

    An atomic marker replace does not make a read-check-send sequence atomic: two
    commands draining the same breached outbox can both read an absent marker and
    both notify. Exclusive create is the only primitive here that is atomic
    between processes, so it is what guards the send.
    """
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (0, 1):
        try:
            descriptor = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if attempt:
                return False
            try:
                age = time.time() - claim_path.stat().st_mtime
            except OSError:
                return False
            if age < stale_after_seconds:
                return False
            try:
                claim_path.unlink(missing_ok=True)
            except OSError:
                return False
            continue
        os.close(descriptor)
        return True
    return False


def notify_backlog_breach(
    outbox: Path,
    *,
    state: dict[str, Any],
    notifier: Callable[[str, dict[str, Any]], bool] | None = None,
    threshold_seconds: float | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Alert once per breach, deduplicated on delivered state rather than existence.

    The marker is written undelivered before anything is attempted, so a run
    that dies mid-alert leaves a record that retries. A duplicate alert is
    survivable; a breach that quietly suppresses itself is not.
    """
    moment = now or datetime.now(timezone.utc)
    threshold = _alert_threshold_seconds() if threshold_seconds is None else threshold_seconds
    marker_path = resolve_alert_marker_path(outbox)

    if dry_run:
        breach = state["depth"] > 0 and state["oldest_age_seconds"] >= threshold
        return {"alerted": False, "breach": bool(breach), "dry_run": True}

    marker = _read_marker(marker_path)
    claim_path = marker_path.with_name(f".{marker_path.stem}.claim")

    if state["depth"] == 0:
        if marker is not None:
            marker_path.unlink(missing_ok=True)
        # A cleared backlog is a new breach later, so the next one gets its own
        # claim rather than inheriting a stale one.
        claim_path.unlink(missing_ok=True)
        return {"alerted": False, "breach": False, "cleared": True}

    if state["oldest_age_seconds"] < threshold:
        return {"alerted": False, "breach": False}

    # The dedupe key names the breaching entry and nothing else. An age ticks on
    # its own, and depth is not stable either: during a long outage every new
    # queued observation would mint a fresh key and re-send an alert for a breach
    # that was already reported. The breaching entry is what defines the breach.
    breach_id = str(state["oldest_queued_at"])
    if marker is not None and marker.get("breach_id") == breach_id and marker.get("delivered") is True:
        return {"alerted": False, "breach": True, "suppressed": True}

    written = {
        "breach_id": breach_id,
        "delivered": False,
        "depth": state["depth"],
        "oldest_age_seconds": state["oldest_age_seconds"],
        "threshold_seconds": threshold,
        "seen_at": moment.isoformat().replace("+00:00", "Z"),
    }
    if not _claim_alert(
        claim_path,
        _positive_float_env(
            "DIALPAD_LOG_OUTBOX_ALERT_CLAIM_SECONDS", DEFAULT_ALERT_CLAIM_STALE_SECONDS
        ),
    ):
        return {"alerted": False, "breach": True, "suppressed": True}

    _write_marker(marker_path, written)

    text = (
        f"Dialpad interaction-log outbox: {state['depth']} observation(s) still unwritten, "
        f"oldest {int(state['oldest_age_seconds'] // 60)} min."
    )
    delivered = _deliver_alert(notifier, text, {"outbox": str(outbox), **written})

    if delivered:
        written["delivered"] = True
        _write_marker(marker_path, written)
    return {"alerted": delivered, "breach": True}


def record_drain_run(
    outbox: Path,
    *,
    result: dict[str, Any],
    state: dict[str, Any],
    now: datetime | None = None,
    dry_run: bool = False,
) -> Path | None:
    """Append one run record. Derived numbers only; no new state to keep correct."""
    if dry_run:
        return None
    entry = {
        "at": (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        "attempted": result.get("attempted", 0),
        "succeeded": result.get("succeeded", 0),
        "failed": result.get("failed", 0),
        "delivery_failed": result.get("delivery_failed", 0),
        "observation_rejected": result.get("observation_rejected", 0),
        "quarantined": result.get("quarantined", 0),
        "skipped": result.get("skipped", 0),
        "budget_hit": result.get("budget_hit", 0),
        "hook_error": result.get("hook_error", 0),
        "depth": state["depth"],
        "oldest_age_seconds": state["oldest_age_seconds"],
    }
    path = resolve_drain_log_path(outbox)
    _append_durable(path, [json.dumps(entry, separators=(",", ":")) + "\n"])
    return path


def _configured_notifier() -> Callable[[str, dict[str, Any]], bool] | None:
    """Alert transport is opt-in and external.

    This skill does not own an operator channel. When a host wants one, point
    DIALPAD_LOG_OUTBOX_NOTIFY_CMD at a command that takes the message as one
    argument and exits 0 only on a confirmed send.
    """
    raw = os.environ.get("DIALPAD_LOG_OUTBOX_NOTIFY_CMD", "").strip()
    if not raw:
        return None

    def notify(text: str, _context: dict[str, Any]) -> bool:
        try:
            completed = subprocess.run(
                shlex.split(raw) + [text],
                check=False,
                capture_output=True,
                timeout=_positive_float_env(
                    "DIALPAD_LOG_OUTBOX_NOTIFY_SECONDS", DEFAULT_NOTIFY_TIMEOUT_SECONDS
                ),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    return notify


def drain_on_use(*, path: Path | str | None = None) -> dict[str, int]:
    """Drain opportunistically from a code path a caller is actually waiting on.

    This is only acceptable on a hot path because it costs a single stat call when
    no outbox exists, and defers to an entry cap and a wall-clock budget when one
    does. Callers treat a non-zero ``skipped`` as "later", never as "empty".
    """
    try:
        target = resolve_outbox_path(path)
        if not target.exists():
            return _empty_result()
        if not _memory_sync_configured():
            return _empty_result()
        result = replay_outbox(
            path=target,
            limit=_positive_int_env("DIALPAD_LOG_OUTBOX_DRAIN_LIMIT", DEFAULT_DRAIN_LIMIT),
            budget_seconds=_positive_float_env(
                "DIALPAD_LOG_OUTBOX_DRAIN_SECONDS", DEFAULT_DRAIN_BUDGET_SECONDS
            ),
        )
        # Observed after the fact, from files already on disk, so a drain that
        # could not reach the log still reports its own backlog accurately.
        state = backlog_state(target)
        record_drain_run(target, result=result, state=state)
        notify_backlog_breach(target, state=state, notifier=_configured_notifier())
    except Exception:  # noqa: BLE001 - a best-effort drain must not break its caller.
        reported = _empty_result()
        reported["hook_error"] = 1
        return reported
    result.setdefault("hook_error", 0)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay record-only Dialpad interaction-log observations")
    parser.add_argument("replay", nargs="?", default="replay", choices=("replay",))
    parser.add_argument("--path", help="Outbox JSONL path")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be greater than 0")
    result = replay_outbox(path=args.path, limit=args.limit)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["skipped"]:
            print("Interaction-log outbox replay: deferred, another drain holds the lock")
            return 0
        breakdown = ""
        if result["failed"]:
            breakdown = (
                f" ({result['delivery_failed']} delivery, "
                f"{result['observation_rejected']} rejected)"
            )
        print(
            "Interaction-log outbox replay: "
            f"{result['succeeded']} succeeded, {result['failed']} failed{breakdown}, "
            f"{result['remaining']} remaining"
        )
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
