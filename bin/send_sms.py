#!/usr/bin/env python3
"""Compatibility wrapper: send_sms.py -> dialpad sms send."""

from __future__ import annotations

import argparse
import json
import sys

from _dialpad_compat import (
    generated_cli_available,
    print_wrapper_error,
    require_api_key,
    run_generated_json,
    run_legacy,
    WrapperError,
)



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send SMS via Dialpad API")
    parser.add_argument("--to", nargs="+", required=True, help="Recipient E.164 numbers")
    parser.add_argument("--message", required=True, help="SMS text content")
    parser.add_argument("--from", dest="from_number", help="Sender number")
    parser.add_argument("--infer-country-code", action="store_true", help="Infer country code from sender")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser



def main() -> int:
    if not generated_cli_available():
        return run_legacy("send_sms.py", sys.argv[1:])

    args = build_parser().parse_args()

    try:
        require_api_key()

        payload = {
            "to_numbers": args.to,
            "text": args.message,
            "infer_country_code": args.infer_country_code,
        }
        if args.from_number:
            payload["from_number"] = args.from_number

        result = run_generated_json(["sms", "send", "--data", json.dumps(payload)])

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("SMS sent successfully!")
            print(f"   ID: {result.get('id', 'N/A')}")
            print(f"   Status: {result.get('message_status') or result.get('status', 'unknown')}")
            print(f"   From: {result.get('from_number', 'N/A')}")
            to_numbers = result.get("to_numbers") or args.to
            print(f"   To: {', '.join(to_numbers)}")

        return 0
    except WrapperError as err:
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
