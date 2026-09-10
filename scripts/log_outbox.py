#!/usr/bin/env python3
"""Durable record-only outbox for successful outbound SMS observations."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sms_sqlite
from interaction_log import InteractionLog
from log_api_client import LogApiError, configured_log_url, record_message as remote_record_message


DEFAULT_OUTBOX = Path("~/.dialpad/log-outbox.jsonl")


def resolve_outbox_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return Path(os.environ.get("DIALPAD_LOG_OUTBOX", str(DEFAULT_OUTBOX))).expanduser()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def enqueue_observation(observation: dict[str, Any], *, path: Path | str | None = None) -> Path:
    target = resolve_outbox_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = {"queued_at": _now(), "observation": observation}
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
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
        try:
            target = enqueue_observation(observation)
        except Exception:
            return {
                "memory_sync": "failed",
                "memory_error": type(error).__name__,
            }
        return {
            "memory_sync": "pending",
            "memory_outbox": str(target),
            "memory_error": type(error).__name__,
        }


def _read_entries(path: Path) -> list[tuple[str, dict[str, Any] | None]]:
    if not path.exists():
        return []
    entries: list[tuple[str, dict[str, Any] | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        try:
            parsed = json.loads(line)
            observation = parsed.get("observation") if isinstance(parsed, dict) else None
            if isinstance(observation, dict):
                entries.append((line, observation))
            else:
                entries.append((line, None))
        except json.JSONDecodeError:
            entries.append((line, None))
    return entries


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


def replay_outbox(*, path: Path | str | None = None, limit: int = 100) -> dict[str, int]:
    target = resolve_outbox_path(path)
    entries = _read_entries(target)
    remaining: list[str] = []
    attempted = succeeded = failed = 0
    for index, (line, observation) in enumerate(entries):
        if observation is None or attempted >= limit:
            remaining.append(line)
            continue
        attempted += 1
        try:
            _record_once(observation)
        except Exception:
            failed += 1
            remaining.append(line)
        else:
            succeeded += 1
    _rewrite(target, remaining)
    return {"attempted": attempted, "succeeded": succeeded, "failed": failed, "remaining": len(remaining)}


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
        print(
            "Interaction-log outbox replay: "
            f"{result['succeeded']} succeeded, {result['failed']} failed, "
            f"{result['remaining']} remaining"
        )
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
