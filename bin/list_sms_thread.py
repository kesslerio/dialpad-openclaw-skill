#!/usr/bin/env python3
"""Read-only wrapper for local Dialpad SMS thread history."""

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
from sms_sqlite import filter_messages, init_db  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="List local Dialpad SMS history for one phone number")
    parser.add_argument("--phone", required=True, help="Contact phone number in E.164 format")
    parser.add_argument("--limit", type=int, default=20, help="Maximum messages to return (default: 20)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser


def _validate_limit(value: int) -> int:
    if value <= 0:
        raise WrapperError("--limit must be greater than 0", code="invalid_argument", retryable=False)
    return min(value, 100)


def _format_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        timestamp_ms = int(float(str(value)))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _summarize_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "dialpad_id": message.get("dialpad_id"),
        "direction": message.get("direction"),
        "from_number": message.get("from_number"),
        "to_number": message.get("to_number"),
        "contact_name": message.get("contact_name"),
        "timestamp": message.get("timestamp"),
        "timestamp_utc": _format_timestamp(message.get("timestamp")),
        "message_status": message.get("message_status"),
        "delivery_result": message.get("delivery_result"),
        "text": message.get("text") or "",
    }


def load_thread_summary(conn: Any, phone: str, limit: int) -> dict[str, Any]:
    counts_row = conn.execute(
        """
        SELECT
          COUNT(*) AS count,
          SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) AS outbound_count,
          SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) AS inbound_count,
          MAX(CASE WHEN direction = 'outbound' THEN timestamp ELSE NULL END) AS latest_outbound_timestamp
        FROM messages
        WHERE contact_number = ?
        """,
        (phone,),
    ).fetchone()
    rows = conn.execute(
        """
        SELECT *
        FROM messages
        WHERE contact_number = ?
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
        """,
        (phone, limit),
    ).fetchall()

    count = int(counts_row["count"] or 0) if counts_row else 0
    outbound_count = int(counts_row["outbound_count"] or 0) if counts_row else 0
    inbound_count = int(counts_row["inbound_count"] or 0) if counts_row else 0
    latest_outbound_timestamp = counts_row["latest_outbound_timestamp"] if counts_row else None
    messages = filter_messages([dict(row) for row in reversed(rows)])
    return {
        "phone": phone,
        "count": count,
        "outbound_count": outbound_count,
        "inbound_count": inbound_count,
        "has_outbound": outbound_count > 0,
        "latest_outbound_timestamp": latest_outbound_timestamp,
        "latest_outbound_timestamp_utc": _format_timestamp(latest_outbound_timestamp),
        "messages": [_summarize_message(msg) for msg in messages],
    }


def main() -> int:
    json_mode = "--json" in sys.argv
    command = COMMAND_IDS["list_sms_thread.list"]
    wrapper = "list_sms_thread.py"

    try:
        args = build_parser().parse_args()
        json_mode = args.json
        limit = _validate_limit(args.limit)
        phone = args.phone.strip()
        if not phone:
            raise WrapperError("--phone is required", code="invalid_argument", retryable=False)

        conn = init_db()
        try:
            summary = load_thread_summary(conn, phone, limit=limit)
        finally:
            conn.close()

        if json_mode:
            emit_success(command, wrapper, summary)
            return 0

        print(f"Thread {phone}: {summary['count']} message(s), {summary['outbound_count']} outbound")
        for message in summary["messages"]:
            arrow = "OUT" if message["direction"] == "outbound" else "IN"
            when = message["timestamp_utc"] or "unknown-time"
            text = str(message["text"])
            preview = text[:140] + ("..." if len(text) > 140 else "")
            print(f"[{when}] {arrow}: {preview}")
        return 0
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
