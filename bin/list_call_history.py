#!/usr/bin/env python3
"""Read-only wrapper for local Dialpad call history and transcripts."""

from __future__ import annotations

import argparse
import sqlite3
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
from call_sqlite import init_db, list_stored_calls, parse_timestamp_ms  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="List local Dialpad call history and transcripts")
    parser.add_argument("--phone", help="Filter calls by contact phone number")
    parser.add_argument(
        "--direction",
        choices=["inbound", "outbound"],
        help="Filter calls by direction (inbound, outbound)",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=None,
        help="Filter calls with duration >= minimum seconds",
    )
    parser.add_argument(
        "--transcript-only",
        action="store_true",
        help="Only return calls that have an available transcript",
    )
    parser.add_argument(
        "--since",
        help="Filter calls started on or after timestamp (seconds, ms, or ISO timestamp)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum calls to return (default: 20)",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser


def _validate_limit(value: int) -> int:
    if value <= 0:
        raise WrapperError("--limit must be greater than 0", code="invalid_argument", retryable=False)
    return min(value, 500)


def _validate_min_duration(value: int | None) -> int | None:
    if value is not None and value < 0:
        raise WrapperError("--min-duration must be non-negative", code="invalid_argument", retryable=False)
    return value


def _format_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        timestamp_ms = int(float(str(value)))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _summarize_call(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "call_id": call.get("call_id"),
        "direction": call.get("direction"),
        "contact_number": call.get("contact_number"),
        "contact_name": call.get("contact_name"),
        "from_number": call.get("from_number"),
        "to_number": call.get("to_number"),
        "date_started": call.get("date_started"),
        "date_started_utc": _format_timestamp(call.get("date_started")),
        "date_ended": call.get("date_ended"),
        "date_ended_utc": _format_timestamp(call.get("date_ended")),
        "duration": call.get("duration", 0),
        "call_state": call.get("call_state"),
        "transcript_present": bool(call.get("transcript_present")),
        "transcript_text": call.get("transcript_text"),
        "transcript_url": call.get("transcript_url"),
    }


def main() -> int:
    json_mode = "--json" in sys.argv
    command = COMMAND_IDS["list_call_history.list"]
    wrapper = "list_call_history.py"

    try:
        args = build_parser().parse_args()
        json_mode = args.json
        limit = _validate_limit(args.limit)
        min_duration = _validate_min_duration(args.min_duration)
        since_ms = parse_timestamp_ms(args.since) if args.since else None
        if args.since and since_ms is None:
            raise WrapperError(
                f"Invalid --since timestamp '{args.since}'",
                code="invalid_argument",
                retryable=False,
            )

        phone = args.phone.strip() if args.phone else None
        direction = args.direction.strip().lower() if args.direction else None

        try:
            stored_calls = list_stored_calls(
                phone=phone,
                direction=direction,
                min_duration=min_duration,
                transcript_only=args.transcript_only,
                since=since_ms,
                limit=limit,
            )
        except (OSError, sqlite3.Error) as exc:
            raise WrapperError(
                f"Failed to read local call history database: {exc}",
                code="internal_error",
                retryable=False,
            ) from exc

        calls_summary = [_summarize_call(call) for call in stored_calls]
        result = {
            "count": len(calls_summary),
            "filters": {
                "phone": phone,
                "direction": direction,
                "min_duration": min_duration,
                "transcript_only": args.transcript_only,
                "since": since_ms,
                "limit": limit,
            },
            "calls": calls_summary,
        }

        if json_mode:
            emit_success(command, wrapper, result)
            return 0

        if not calls_summary:
            print("No calls found in local store for the requested filters.")
            return 0

        print(f"Found {len(calls_summary)} call(s) in local store:")
        for call in calls_summary:
            direction_str = (call["direction"] or "unknown").upper()
            contact_str = call["contact_name"] or call["contact_number"] or "Unknown"
            duration_str = f"{call['duration']}s"
            transcript_str = "transcript available" if call["transcript_present"] else "no transcript"
            when = call["date_started_utc"] or "unknown-time"
            print(f"[{when}] {direction_str} with {contact_str} ({call.get('contact_number') or '-'}) - {duration_str} ({call['call_state']}, {transcript_str})")
        return 0
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2
    except Exception as err:  # noqa: BLE001 - wrappers must return structured JSON in --json mode.
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
