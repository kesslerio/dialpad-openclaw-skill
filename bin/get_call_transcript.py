#!/usr/bin/env python3
"""Supported wrapper for retrieving one Dialpad call transcript."""

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
SCRIPT_PATH = ROOT / "scripts" / "get_transcript.py"
SCRIPTS_PATH = str(ROOT / "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.append(SCRIPTS_PATH)


def _load_script_module():
    spec = importlib.util.spec_from_file_location("_dialpad_get_transcript_script", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load transcript helpers from {SCRIPT_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SCRIPT = _load_script_module()
DialpadApiError = _SCRIPT.DialpadApiError
DialpadConfigError = _SCRIPT.DialpadConfigError
resolve_call_transcript = _SCRIPT.resolve_call_transcript


def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="Retrieve a Dialpad call transcript")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--call-id", help="Dialpad call ID")
    group.add_argument("--last", action="store_true", help="Use the most recent call")
    parser.add_argument(
        "--with",
        dest="with_value",
        help="Filter --last by phone number or contact substring",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.with_value and not args.last:
        raise WrapperError("--with can only be used with --last", code="invalid_argument", retryable=False)


def _wrapper_error_from_dialpad(exc: Exception) -> WrapperError:
    if isinstance(exc, DialpadConfigError):
        return WrapperError(str(exc), code="auth_missing", retryable=False)
    if isinstance(exc, DialpadApiError):
        lowered = str(exc).lower()
        if "no calls found" in lowered:
            return WrapperError(str(exc), code="not_found", retryable=False)
        if "missing call_id" in lowered or "either --call-id or --last" in lowered:
            return WrapperError(str(exc), code="validation_failed", retryable=False)
        return WrapperError(str(exc), code="upstream_error", retryable=True)
    return WrapperError(str(exc))


def main() -> int:
    json_mode = "--json" in sys.argv
    command = COMMAND_IDS["get_call_transcript.get"]
    wrapper = "get_call_transcript.py"

    try:
        args = build_parser().parse_args()
        json_mode = args.json
        _validate_args(args)

        require_api_key()
        try:
            result = resolve_call_transcript(args.call_id, args.last, args.with_value)
        except (DialpadApiError, DialpadConfigError) as exc:
            raise _wrapper_error_from_dialpad(exc) from exc

        if json_mode:
            emit_success(command, wrapper, result)
            return 0

        print(f"Transcript for call {result['call_id']}")
        if result.get("available"):
            print()
            print(result.get("transcript_text") or "")
        else:
            reason = result.get("unavailable_reason") or "unavailable"
            print()
            print(f"Transcript unavailable ({reason}).")
        return 0
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
