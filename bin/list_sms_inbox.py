#!/usr/bin/env python3
"""Read-only wrapper for inbound Dialpad SMS across all threads."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_PATH = str(ROOT / "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.append(SCRIPTS_PATH)

from _dialpad_compat import (  # noqa: E402
    COMMAND_IDS,
    WrapperArgumentParser,
    WrapperError,
    emit_success,
    handle_wrapper_exception,
    print_wrapper_error,
)
from interaction_log import InteractionLog  # noqa: E402
from log_outbox import drain_on_use  # noqa: E402
from log_api_client import LogApiError, configured_log_url, get_data  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="List inbound Dialpad SMS across all threads")
    parser.add_argument("--limit", type=int, default=20, help="Maximum messages to return (default: 20)")
    parser.add_argument("--unread-only", action="store_true", help="Only return unread inbound messages")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser


def _validate_limit(value: int) -> int:
    if value <= 0:
        raise WrapperError("--limit must be greater than 0", code="invalid_argument", retryable=False)
    return min(value, 100)


def _format_timestamp(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(float(str(value))) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return None


def _summarize_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "dialpad_id": message.get("dialpad_id"),
        "direction": message.get("direction"),
        "from_number": message.get("from_number"),
        "to_number": message.get("to_number"),
        "contact_number": message.get("contact_number"),
        "contact_name": message.get("contact_name"),
        "timestamp": message.get("timestamp"),
        "timestamp_utc": _format_timestamp(message.get("timestamp")),
        "text": message.get("text") or "",
        "read": bool(message.get("read")),
        "source": message.get("source"),
        "observed_at": message.get("observed_at"),
    }


def _run() -> int:
    json_mode = "--json" in sys.argv
    command = COMMAND_IDS["list_sms_inbox.list"]
    wrapper = "list_sms_inbox.py"
    try:
        args = build_parser().parse_args()
        json_mode = args.json
        limit = _validate_limit(args.limit)
        if configured_log_url():
            try:
                data, _remote_meta = get_data(
                    "/v1/sms/inbox",
                    query={"limit": limit, "unread_only": str(args.unread_only).lower()},
                )
            except LogApiError as exc:
                raise WrapperError(str(exc), code=exc.code, retryable=exc.retryable) from exc
            history_source = "shared_log"
        else:
            try:
                data = InteractionLog().inbox(limit=limit, unread_only=args.unread_only)
            except (OSError, ValueError) as exc:
                raise WrapperError(
                    f"Failed to read local SMS inbox: {exc}",
                    code="internal_error",
                    retryable=False,
                ) from exc
            history_source = "local_log"

        data = dict(data)
        data["messages"] = [
            _summarize_message(message)
            for message in (data.get("messages") if isinstance(data.get("messages"), list) else [])
        ]
        if json_mode:
            emit_success(command, wrapper, data, meta_extra={"history_source": history_source})
            return 0

        print(f"Inbound SMS: {data.get('count', 0)} message(s)")
        for message in data["messages"]:
            when = message["timestamp_utc"] or "unknown-time"
            sender = message.get("contact_name") or message.get("contact_number") or message.get("from_number") or "Unknown"
            body = str(message.get("text") or "")
            preview = body[:140] + ("..." if len(body) > 140 else "")
            print(f"[{when}] IN from {sender}: {preview}")
        return 0
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2
    except Exception as err:  # noqa: BLE001 - wrappers return structured JSON in --json mode.
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


def main() -> int:
    """Run the command, then opportunistically drain any pending interaction-log records.

    After output, always: the caller's answer is already committed to, and
    drain_on_use is a single stat call when there is nothing to do.
    """
    code = _run()
    drain_on_use()
    return code
