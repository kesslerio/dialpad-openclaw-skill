#!/usr/bin/env python3
"""Compatibility wrapper: send_sms.py -> dialpad sms send."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _dialpad_compat import (
    COMMAND_IDS,
    WrapperArgumentParser,
    emit_success,
    handle_wrapper_exception,
    print_wrapper_error,
    require_generated_cli,
    require_api_key,
    resolve_sender,
    run_generated_json,
    WrapperError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="Send SMS via Dialpad API")
    parser.add_argument("--to", nargs="+", required=True, help="Recipient E.164 numbers")
    message_group = parser.add_mutually_exclusive_group(required=True)
    message_group.add_argument("--message", help="SMS text content")
    message_group.add_argument(
        "--message-file",
        help="Read SMS text from a UTF-8 file path (safer for $ and shell-sensitive content)",
    )
    message_group.add_argument(
        "--message-stdin",
        action="store_true",
        help="Read SMS text from stdin (safer for $ and shell-sensitive content)",
    )
    parser.add_argument("--from", dest="from_number", help="Sender number")
    parser.add_argument("--profile", choices=("work", "sales"), help="Sender profile")
    parser.add_argument(
        "--allow-profile-mismatch",
        action="store_true",
        help="Allow --from to differ from mapped profile number",
    )
    parser.add_argument("--infer-country-code", action="store_true", help="Infer country code from sender")
    parser.add_argument("--dry-run", action="store_true", help="Print request and selected sender without sending")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser


def resolve_message_text(args: argparse.Namespace) -> tuple[str, str]:
    if args.message is not None:
        message_text = args.message
        message_source = "--message"
    elif args.message_file:
        message_source = "--message-file"
        try:
            message_text = Path(args.message_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WrapperError(
                f"Failed to read {message_source} '{args.message_file}': {exc}",
                code="invalid_argument",
                retryable=False,
            ) from exc
    elif args.message_stdin:
        message_source = "--message-stdin"
        message_text = sys.stdin.read()
    else:
        raise WrapperError(
            "One of --message, --message-file, or --message-stdin is required.",
            code="invalid_argument",
            retryable=False,
        )

    if message_text == "":
        if args.message_file:
            raise WrapperError(
                f"Message text from --message-file '{args.message_file}' is empty.",
                code="invalid_argument",
                retryable=False,
            )
        if args.message_stdin:
            raise WrapperError(
                "Message text from --message-stdin is empty. Pipe content into stdin.",
                code="invalid_argument",
                retryable=False,
            )
        raise WrapperError("Message text cannot be empty.", code="invalid_argument", retryable=False)

    return message_text, message_source


def _build_payload(
    to_numbers: list[str],
    message_text: str,
    infer_country_code: bool,
    sender_number: str,
) -> dict[str, object]:
    return {
        "to_numbers": to_numbers,
        "text": message_text,
        "infer_country_code": infer_country_code,
        "from_number": sender_number,
    }


def main() -> int:
    json_mode = "--json" in sys.argv
    command = COMMAND_IDS["send_sms.send"]
    wrapper = "send_sms.py"

    try:
        args = build_parser().parse_args()
        json_mode = args.json
        require_generated_cli()
        sender_number, sender_source = resolve_sender(
            args.from_number, args.profile, allow_profile_mismatch=args.allow_profile_mismatch
        )
        message_text, message_source = resolve_message_text(args)
        payload = _build_payload(args.to, message_text, args.infer_country_code, sender_number)

        if args.dry_run:
            if json_mode:
                emit_success(
                    command,
                    wrapper,
                    {
                        "mode": "dry_run",
                        "sender_number": sender_number,
                        "sender_source": sender_source,
                        "message_source": message_source,
                        "payload": payload,
                    },
                )
            else:
                print("Dry run: SMS not sent")
                print(f"Selected sender: {sender_number} ({sender_source})")
                print(f"Message source: {message_source}")
                print(f"To: {', '.join(args.to)}")
                print(f"Message length: {len(message_text)}")
                print("Message preview:")
                print(message_text)
            return 0

        require_api_key()
        result = run_generated_json(["sms", "send", "--data", json.dumps(payload)])

        if json_mode:
            emit_success(command, wrapper, result if isinstance(result, dict) else {"result": result})
        else:
            print(f"Selected sender: {sender_number} ({sender_source})")
            print("SMS sent successfully!")
            print(f"   ID: {result.get('id', 'N/A')}")
            print(f"   Status: {result.get('message_status') or result.get('status', 'unknown')}")
            print(f"   From: {sender_number}")
            to_numbers = result.get("to_numbers") or args.to
            print(f"   To: {', '.join(to_numbers)}")

        return 0
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
