#!/usr/bin/env python3
"""Retrieve Dialpad AI recap for calls."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from call_lookup import (
    DialpadApiError,
    DialpadConfigError,
    api_get,
    resolve_call_id,
)


def _normalize_list(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def format_recap(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("summary"), str) and payload["summary"].strip():
        return payload["summary"].strip()
    if isinstance(payload.get("recap"), str) and payload["recap"].strip():
        return payload["recap"].strip()
    if isinstance(payload.get("ai_recap"), str) and payload["ai_recap"].strip():
        return payload["ai_recap"].strip()

    sections: list[str] = []
    for key in ("short", "medium", "long", "bullet"):
        values = _normalize_list(payload.get(key))
        if values:
            title = key.capitalize()
            if len(values) == 1:
                sections.append(f"{title}: {values[0]}")
            else:
                bullet_lines = "\n".join(f"- {item}" for item in values)
                sections.append(f"{title}:\n{bullet_lines}")

    if sections:
        return "\n\n".join(sections)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieve a Dialpad AI recap")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--call-id", help="Dialpad call ID")
    group.add_argument("--last", action="store_true", help="Use the most recent call")
    parser.add_argument(
        "--with",
        dest="with_value",
        help="Filter most recent call by phone number or contact substring",
    )
    parser.add_argument("--raw-json", action="store_true", help="Print raw JSON response")
    args = parser.parse_args()

    try:
        chosen_call_id = resolve_call_id(args.call_id, args.last, args.with_value)
        recap = api_get(f"/call/{chosen_call_id}/ai_recap")
        if args.raw_json:
            print(json.dumps(recap, indent=2))
            return 0

        body = format_recap(recap)
        print(f"AI recap for call {chosen_call_id}")
        if body:
            print()
            print(body)
        else:
            print()
            print("AI recap data returned, but no readable recap fields were found.")
        return 0
    except DialpadConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except DialpadApiError as exc:
        if exc.status_code == 404:
            call_ref = args.call_id or "(most recent selection)"
            print(f"AI recap unavailable for call {call_ref}.", file=sys.stderr)
            return 2
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
