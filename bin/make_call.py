#!/usr/bin/env python3
"""Compatibility wrapper: make_call.py -> dialpad call make."""

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

KNOWN_USERS = {
    "+14153602954": "5765607478525952",  # Martin Kessler
    "+14158701945": "5625110025338880",  # Lilla Laczo
    "+14152230323": "5964143916400640",  # Scott Sicz
}



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Make voice calls via Dialpad API")
    parser.add_argument("--to", required=True, help="Recipient E.164 phone number")
    parser.add_argument("--from", dest="from_number", help="Caller ID number")
    parser.add_argument("--user-id", dest="user_id", help="Dialpad user ID")
    parser.add_argument("--text", dest="text_to_speak", help="Text-to-speech prompt")
    parser.add_argument("--voice", default="Sam", help="Reserved for compatibility")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser



def resolve_user_id(from_number: str | None, explicit_user_id: str | None) -> str:
    if explicit_user_id:
        return explicit_user_id
    if from_number and from_number in KNOWN_USERS:
        return KNOWN_USERS[from_number]
    for user_id in KNOWN_USERS.values():
        return user_id
    raise WrapperError("user_id is required; provide --user-id or --from with a known number")



def main() -> int:
    if not generated_cli_available():
        return run_legacy("make_call.py", sys.argv[1:])

    args = build_parser().parse_args()

    try:
        require_api_key()
        user_id = resolve_user_id(args.from_number, args.user_id)

        payload = {
            "phone_number": args.to,
            "user_id": user_id,
        }

        if args.from_number:
            payload["outbound_caller_id"] = args.from_number

        if args.text_to_speak:
            payload["command"] = json.dumps({"actions": [{"say": args.text_to_speak}]})

        result = run_generated_json(["call", "make", "--data", json.dumps(payload)])

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Call initiated successfully!")
            print(f"   ID: {result.get('call_id') or result.get('id')}")
            print(f"   To: {args.to}")

        return 0
    except WrapperError as err:
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
