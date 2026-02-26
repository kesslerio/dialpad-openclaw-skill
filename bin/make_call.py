#!/usr/bin/env python3
"""Compatibility wrapper: make_call.py -> dialpad call make."""

from __future__ import annotations

import argparse
import json
import os

from _dialpad_compat import (
    print_wrapper_error,
    require_generated_cli,
    require_api_key,
    run_generated_json,
    WrapperError,
)

# Map of E.164 phone numbers to Dialpad user IDs.
# Set via DIALPAD_USER_MAP env var as JSON, e.g.:
#   export DIALPAD_USER_MAP='{"+15551234567": "1234567890"}'
# Fallback: empty dict (--user-id flag required).
_DEFAULT_USER_MAP: dict[str, str] = {}


def _load_user_map() -> dict[str, str]:
    raw = os.environ.get("DIALPAD_USER_MAP", "")
    if not raw:
        return _DEFAULT_USER_MAP
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise WrapperError("DIALPAD_USER_MAP must be a JSON object")
        return {str(k): str(v) for k, v in parsed.items()}
    except json.JSONDecodeError as exc:
        raise WrapperError(f"DIALPAD_USER_MAP is not valid JSON: {exc}") from exc


KNOWN_USERS = _load_user_map()



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
    if from_number:
        if from_number in KNOWN_USERS:
            return KNOWN_USERS[from_number]
        raise WrapperError(f"Unknown --from number: {from_number}. Map it in KNOWN_USERS or provide --user-id.")
    # Default to first known user if nothing specified
    if KNOWN_USERS:
        return next(iter(KNOWN_USERS.values()))
    raise WrapperError("user_id is required; provide --user-id or --from with a known number")



def main() -> int:
    args = build_parser().parse_args()

    try:
        require_generated_cli()
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

        # Use --data to send the full payload; --data alone is sufficient and
        # avoids duplicating phone_number/user_id as individual flags.
        cmd = [
            "call", "call.call",
            "--data", json.dumps(payload),
        ]
        result = run_generated_json(cmd)

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
