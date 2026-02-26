#!/usr/bin/env python3
"""Compatibility wrapper: send_sms.py -> dialpad sms send."""

from __future__ import annotations

import argparse
import json

from _dialpad_compat import (
    COMMAND_IDS,
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
    parser = argparse.ArgumentParser(description="Send SMS via Dialpad API")
    parser.add_argument("--to", nargs="+", required=True, help="Recipient E.164 numbers")
    parser.add_argument("--message", required=True, help="SMS text content")
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


def _build_payload(args, sender_number: str) -> dict[str, object]:
    return {
        "to_numbers": args.to,
        "text": args.message,
        "infer_country_code": args.infer_country_code,
        "from_number": sender_number,
    }



def main() -> int:
    args = build_parser().parse_args()
    json_mode = args.json
    command = COMMAND_IDS["send_sms.send"]
    wrapper = "send_sms.py"

    try:
        require_generated_cli()
        sender_number, sender_source = resolve_sender(
            args.from_number, args.profile, allow_profile_mismatch=args.allow_profile_mismatch
        )
        payload = _build_payload(args, sender_number)

        if args.dry_run:
            if json_mode:
                emit_success(
                    command,
                    wrapper,
                    {
                        "mode": "dry_run",
                        "sender_number": sender_number,
                        "sender_source": sender_source,
                        "payload": payload,
                    },
                )
            else:
                print("Dry run: SMS not sent")
                print(f"Selected sender: {sender_number} ({sender_source})")
                print(f"To: {', '.join(args.to)}")
                print(f"Message length: {len(args.message)}")
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
