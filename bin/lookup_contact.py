#!/usr/bin/env python3
"""Compatibility wrapper: lookup_contact.py -> dialpad contact lookup."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from _dialpad_compat import (
    generated_cli_available,
    print_wrapper_error,
    require_api_key,
    run_generated_json,
    run_legacy,
    WrapperError,
)

DEFAULT_MAX_PAGES = 20


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lookup contact by phone/email/name")
    parser.add_argument("query_pos", nargs="?", help="Lookup query (phone, email, or name)")
    parser.add_argument("--query", help="Lookup query (overrides positional)")
    parser.add_argument("--owner-id", help="Filter by owner user ID")
    parser.add_argument("--include-local", action="store_true", help="Include local contacts")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="Max pages to scan")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser



def normalize(value: str) -> str:
    return re.sub(r"\W+", "", value or "").lower()



def extract_strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, list):
        for item in value:
            found.extend(extract_strings(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(extract_strings(item))
    return found



def contact_matches(contact: dict[str, Any], query: str) -> bool:
    raw = query.strip()
    raw_norm = normalize(raw)

    for candidate in extract_strings(contact):
        if not candidate:
            continue
        if raw.lower() in candidate.lower():
            return True
        if raw_norm and raw_norm in normalize(candidate):
            return True
    return False



def find_contact(query: str, owner_id: str | None, include_local: bool, max_pages: int) -> dict[str, Any] | None:
    cursor: str | None = None

    for _ in range(max_pages):
        cmd = ["contacts", "contacts.list"]
        if cursor:
            cmd.extend(["--cursor", cursor])
        if owner_id:
            cmd.extend(["--owner-id", owner_id])
        if include_local:
            cmd.extend(["--include-local", "true"])

        result = run_generated_json(cmd)
        items = result.get("items") or []

        for contact in items:
            if isinstance(contact, dict) and contact_matches(contact, query):
                return contact

        cursor = result.get("cursor")
        if not cursor:
            break

    return None



def main() -> int:
    if not generated_cli_available():
        return run_legacy("lookup_contact.py", sys.argv[1:])

    args = build_parser().parse_args()

    query = args.query or args.query_pos
    if not query:
        print("Error: provide a query via --query or positional argument", file=sys.stderr)
        return 2

    try:
        require_api_key()
        match = find_contact(query, args.owner_id, args.include_local, args.max_pages)

        if args.json:
            print(json.dumps(match, indent=2))
        else:
            if not match:
                print(f"Lookup for {query}: None")
            else:
                first = match.get("first_name", "")
                last = match.get("last_name", "")
                display = match.get("display_name") or f"{first} {last}".strip() or "Known Contact (No Name)"
                print(f"Lookup for {query}: {display}")

        return 0
    except WrapperError as err:
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
