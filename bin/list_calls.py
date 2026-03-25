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


def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="List recent calls via Dialpad API")
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


def main() -> int:
    json_mode = "--json" in sys.argv
    command = COMMAND_IDS["list_calls.list"]
    wrapper = "list_calls.py"

    try:
        args = build_parser().parse_args()
        json_mode = args.json
        limit = _validated_positive_int(args.limit, "--limit")
        hours = None if args.today else _validated_positive_int(args.hours, "--hours")

        require_api_key()
        started_after, started_before = compute_window(args.hours, args.today)
        try:
            raw_calls = fetch_calls(started_after, started_before, limit, missed_only=args.missed)
        except RuntimeError as exc:
            raise WrapperError(str(exc)) from exc

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
                    "calls": [to_call_summary(call) for call in raw_calls],
                    "output_path": args.output,
                },
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
