#!/usr/bin/env python3
"""Supported wrapper for recent Dialpad call history."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from _dialpad_compat import (
    COMMAND_IDS,
    WrapperArgumentParser,
    WrapperError,
    emit_success,
    handle_wrapper_exception,
    print_wrapper_error,
    require_api_key,
)
from log_api_client import LogApiError, configured_log_url, get_data

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "list_calls.py"


def _load_script_module():
    # Load the operator script under an alias so this wrapper can share its logic
    # without colliding with the wrapper module name (`list_calls`).
    spec = importlib.util.spec_from_file_location("_dialpad_list_calls_script", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load list_calls helpers from {SCRIPT_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SCRIPT = _load_script_module()
DEFAULT_HOURS = _SCRIPT.DEFAULT_HOURS
DEFAULT_LIMIT = _SCRIPT.DEFAULT_LIMIT
compute_window = _SCRIPT.compute_window
fetch_calls = _SCRIPT.fetch_calls
render_table = _SCRIPT.render_table
to_call_summary = _SCRIPT.to_call_summary
to_row = _SCRIPT.to_row
write_csv = _SCRIPT.write_csv
get_caller_phone = _SCRIPT.get_caller_phone


def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="List recent calls from shared history or Dialpad API")
    time_group = parser.add_mutually_exclusive_group()
    time_group.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HOURS,
        help=f"Look back this many hours (default: {DEFAULT_HOURS})",
    )
    time_group.add_argument(
        "--today",
        action="store_true",
        help="Only include calls from local midnight to now",
    )
    parser.add_argument("--missed", action="store_true", help="Only show missed calls")
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--local-log",
        action="store_true",
        help="Read canonical calls through DIALPAD_LOG_URL (shared mode)",
    )
    source_group.add_argument(
        "--local",
        action="store_true",
        help="Read the local calls SQLite database (legacy owner mode)",
    )
    source_group.add_argument(
        "--live",
        action="store_true",
        help="Read live Dialpad provider history explicitly",
    )
    parser.add_argument("--with", dest="with_phone", help="Only calls involving this phone number")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Maximum number of calls to return (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument("--output", help="Write CSV output to file")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser


def _validated_positive_int(value: int, flag: str) -> int:
    if value <= 0:
        raise WrapperError(f"{flag} must be greater than 0", code="invalid_argument", retryable=False)
    return value


def _phone_matches(call: dict[str, object], wanted: str) -> bool:
    actual = get_caller_phone(call)
    wanted_digits = "".join(ch for ch in str(wanted) if ch.isdigit())
    actual_digits = "".join(ch for ch in str(actual or "") if ch.isdigit())
    return bool(wanted_digits and actual_digits and (wanted_digits in actual_digits or actual_digits in wanted_digits))


def _shared_call_summary(call: dict[str, object]) -> dict[str, object]:
    """Keep the wrapper's summary shape while preserving canonical fields."""
    status = str(call.get("status") or call.get("disposition") or "unknown")
    duration_seconds = int(call.get("duration_seconds") or 0)
    return {
        "call_id": call.get("call_id"),
        "started_at": call.get("started_at"),
        "contact": call.get("contact") or call.get("contact_phone") or "-",
        "contact_phone": call.get("contact_phone"),
        "direction": call.get("direction") or "unknown",
        "duration_seconds": duration_seconds,
        "duration_display": call.get("duration_display") or "0:00",
        "status": status,
        "state": call.get("state"),
        "line": call.get("line"),
        "recording_url": call.get("recording_url"),
        "outcome": call.get("disposition") or status,
        "source": call.get("source"),
        "observed_at": call.get("observed_at"),
    }


def main() -> int:
    json_mode = "--json" in sys.argv
    command = COMMAND_IDS["list_calls.list"]
    wrapper = "list_calls.py"

    try:
        args = build_parser().parse_args()
        json_mode = args.json
        limit = _validated_positive_int(args.limit, "--limit")
        hours = None if args.today else _validated_positive_int(args.hours, "--hours")

        started_after, started_before = compute_window(args.hours, args.today)
        shared_mode = bool(configured_log_url()) and not args.local and not args.live
        if args.local_log and not configured_log_url():
            raise WrapperError(
                "--local-log requires DIALPAD_LOG_URL",
                code="invalid_argument",
                retryable=False,
            )

        if shared_mode or args.local_log:
            query = {
                "hours": None if args.today else args.hours,
                "missed": str(args.missed).lower(),
                "with": args.with_phone,
                "limit": limit,
                "since_ms": started_after,
                "until_ms": started_before,
            }
            try:
                shared_data, _remote_meta = get_data("/v1/calls", query=query)
            except LogApiError as exc:
                # Shared log auth drift should not brick call history — fall back to live Dialpad.
                if (
                    not args.local_log
                    and getattr(exc, "code", None) in {"auth_missing", "unauthorized"}
                ):
                    shared_mode = False
                    require_api_key()
                    try:
                        raw_calls = fetch_calls(
                            started_after, started_before, limit, missed_only=args.missed
                        )
                    except RuntimeError as live_exc:
                        raise WrapperError(str(live_exc)) from live_exc
                    rows = None  # filled below via to_row path
                else:
                    raise WrapperError(str(exc), code=exc.code, retryable=exc.retryable) from exc
            else:
                raw_calls = shared_data.get("calls") if isinstance(shared_data.get("calls"), list) else []
                raw_calls = [call for call in raw_calls if isinstance(call, dict)]
                rows = []
                for call in raw_calls:
                    rows.append(
                        {
                            "started": str(call.get("started_at") or "-").replace("T", " ").replace("Z", "")[:16],
                            "caller": str(call.get("contact") or call.get("contact_phone") or "-"),
                            "direction": str(call.get("direction") or "unknown"),
                            "duration": str(call.get("duration_display") or "0:00"),
                            "status": str(call.get("status") or call.get("disposition") or "unknown"),
                            "line": str(call.get("line") or "-"),
                        }
                    )
        elif args.local:
            try:
                from call_sqlite import list_stored_calls
                stored = list_stored_calls(
                    since=started_after,
                    limit=limit,
                )
                raw_calls = []
                for sc in stored:
                    if args.missed and sc.get("call_state") != "missed" and sc.get("duration", 0) > 0:
                        continue
                    raw_calls.append({
                        "id": sc["call_id"],
                        "call_id": sc["call_id"],
                        "direction": sc["direction"],
                        "state": sc["call_state"],
                        "date_started": sc["date_started"],
                        "date_ended": sc["date_ended"],
                        "duration": (sc.get("duration") or 0) * 1000,
                        "contact": {
                            "name": sc.get("contact_name"),
                            "phone": sc.get("contact_number"),
                        },
                        "external_number": sc.get("contact_number"),
                        "external_display_name": sc.get("contact_name"),
                        "target": {
                            "phone": sc.get("to_number"),
                        },
                        "transcript_present": sc.get("transcript_present", False),
                        "transcript_url": sc.get("transcript_url"),
                    })
            except Exception as exc:
                raise WrapperError(
                    f"Failed to read local call history database: {exc}",
                    code="internal_error",
                    retryable=False,
                ) from exc
        else:
            require_api_key()
            try:
                raw_calls = fetch_calls(started_after, started_before, limit, missed_only=args.missed)
            except RuntimeError as exc:
                raise WrapperError(str(exc)) from exc


        if args.with_phone and not (shared_mode or args.local_log):
            raw_calls = [call for call in raw_calls if _phone_matches(call, args.with_phone)]

        if not (shared_mode or args.local_log):
            rows = [to_row(call) for call in raw_calls]

        if args.output:
            try:
                write_csv(rows, args.output)
            except OSError as exc:
                raise WrapperError(
                    f"Failed to write CSV output to '{args.output}': {exc}",
                    code="invalid_argument",
                    retryable=False,
                ) from exc

        if json_mode:
            emit_success(
                command,
                wrapper,
                {
                    "count": len(raw_calls),
                    "filters": {
                        "hours": hours,
                        "today": args.today,
                        "missed": args.missed,
                        "limit": limit,
                    },
                    "window": {
                        "started_after_ms": started_after,
                        "started_before_ms": started_before,
                    },
                    "calls": [
                        _shared_call_summary(call) if (shared_mode or args.local_log) else to_call_summary(call)
                        for call in raw_calls
                    ],
                    "output_path": args.output,
                },
                meta_extra={"history_source": "shared_log" if (shared_mode or args.local_log) else ("local_log" if args.local else "live_dialpad")},
            )
            return 0

        if args.output:
            print(f"Saved {len(rows)} call(s) to {args.output}")
            return 0

        if not rows:
            print("No calls found for the requested filters.")
            return 0

        print(render_table(rows))
        return 0
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
