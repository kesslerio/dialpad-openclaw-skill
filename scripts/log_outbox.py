#!/usr/bin/env python3
"""Durable record-only outbox for successful outbound SMS observations."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


import sms_sqlite
from interaction_log import InteractionLog
from log_api_client import LogApiError, configured_log_url, record_message as remote_record_message


DEFAULT_OUTBOX = Path("~/.dialpad/log-outbox.jsonl")

# Codes that mean the interaction log refused this specific observation, as opposed
# to being unreachable. Retrying identical content against these will not help.
_REJECTION_CODES = frozenset({"invalid_argument", "not_found"})

DEFAULT_LOCK_TIMEOUT_SECONDS = 2.0

# A drain may run on a path a caller is waiting on, and every request costs up to
# DIALPAD_LOG_TIMEOUT (5s by default) with no retry budget of its own. These two
# caps are what make that safe: without them 18 stale entries could add roughly
# 90 seconds to a user-visible send.
DEFAULT_DRAIN_LIMIT = 5
DEFAULT_DRAIN_BUDGET_SECONDS = 2.0


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


def resolve_outbox_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return Path(os.environ.get("DIALPAD_LOG_OUTBOX", str(DEFAULT_OUTBOX))).expanduser()


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
    with outbox_lock(target):
        # A lock timeout never drops a confirmed send's record; the drain's
        # identity-based commit is what keeps a concurrent append alive.
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
    return (parsed.get("queued_at"), observation.get("provider_id"))


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

        keep: list[str] = []
        poison: list[str] = []
        for line in _read_lines(target):
            parsed = _parse_entry(line)
            if parsed is None:
                poison.append(line)
                continue
            identity = _entry_identity(parsed)
            if identity in drained:
                continue
            if identity in retries:
                keep.append(_line_with_failure(line, retries[identity]))
            else:
                keep.append(line)

        if poison:
            try:
                _append_durable(resolve_quarantine_path(target), poison)
            except OSError:
                keep.extend(poison)  # never drop a line we failed to move somewhere else
            else:
                result["quarantined"] = len(poison)

        _rewrite(target, keep)
        result["remaining"] = len(keep)
    return result


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
