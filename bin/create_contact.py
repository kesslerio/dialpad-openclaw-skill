#!/usr/bin/env python3
"""Compatibility wrapper: create_contact.py -> dialpad contacts create."""

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
PHONE_RE = re.compile(r"^\+\d{7,15}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create contacts via Dialpad API")
    parser.add_argument("--first-name", required=True, help="Contact first name")
    parser.add_argument("--last-name", required=True, help="Contact last name")
    parser.add_argument("--phone", action="append", help="Phone number (E.164). Repeatable.")
    parser.add_argument("--email", action="append", help="Email address. Repeatable.")
    parser.add_argument("--company-name", help="Company name")
    parser.add_argument("--job-title", help="Job title")
    parser.add_argument("--extension", help="Extension")
    parser.add_argument("--url", action="append", help="Associated URL. Repeatable.")
    parser.add_argument("--owner-id", help="Create a local contact for this owner")
    parser.add_argument("--allow-duplicate", action="store_true", help="Create even if duplicate found")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="Max pages to scan for duplicate checks")
    return parser


def parse_repeated(values: list[str] | None) -> list[str]:
    if not values:
        return []
    collected: list[str] = []
    for value in values:
        collected.extend(part.strip() for part in str(value).split(",") if part.strip())
    return collected


def normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def validate_args(
    phones: list[str],
    emails: list[str],
    urls: list[str],
) -> None:
    for phone in phones:
        if not PHONE_RE.fullmatch(phone):
            raise WrapperError(
                f"Invalid --phone '{phone}'. Use E.164 format, e.g. +14155550123."
            )
    for email in emails:
        if not EMAIL_RE.fullmatch(email):
            raise WrapperError(f"Invalid --email '{email}'. Use a valid email address.")
    for url in urls:
        if not url.strip():
            raise WrapperError("Empty --url value is not allowed.")


def build_payload(
    first_name: str,
    last_name: str,
    phones: list[str],
    emails: list[str],
    urls: list[str],
    company_name: str | None,
    job_title: str | None,
    extension: str | None,
    owner_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "first_name": first_name,
        "last_name": last_name,
    }
    if company_name:
        payload["company_name"] = company_name
    if job_title:
        payload["job_title"] = job_title
    if extension:
        payload["extension"] = extension
    if owner_id:
        payload["owner_id"] = owner_id
    if phones:
        payload["phones"] = phones
    if emails:
        payload["emails"] = emails
    if urls:
        payload["urls"] = urls
    return payload


def get_contact_list_values(contact: dict[str, Any], key: str) -> set[str]:
    values = contact.get(key) or []
    if isinstance(values, str):
        return {values}
    if isinstance(values, list):
        return {str(item).strip().lower() for item in values if isinstance(item, str)}
    return set()


def is_duplicate_contact(contact: dict[str, Any], phones: list[str], emails: list[str]) -> bool:
    phone_values = get_contact_list_values(contact, "phones")
    phone_values.update(get_contact_list_values(contact, "phone_numbers"))
    if contact.get("primary_phone"):
        phone_values.add(str(contact.get("primary_phone")))
    for phone in phones:
        if normalize_phone(phone) in {normalize_phone(candidate) for candidate in phone_values}:
            return True

    email_values = get_contact_list_values(contact, "emails")
    if contact.get("primary_email"):
        email_values.add(str(contact.get("primary_email")).strip().lower())
    for email in emails:
        if email.strip().lower() in email_values:
            return True

    return False


def find_duplicates(phones: list[str], emails: list[str], owner_id: str | None, max_pages: int) -> list[dict[str, Any]]:
    if not phones and not emails:
        return []

    matches: list[dict[str, Any]] = []
    cursor: str | None = None
    include_local = "true" if owner_id else None

    for _ in range(max_pages):
        args = ["contacts", "contacts.list"]
        if cursor:
            args.extend(["--cursor", cursor])
        if owner_id:
            args.extend(["--owner-id", owner_id])
        if include_local:
            args.extend(["--include-local", include_local])

        result = run_generated_json(args)
        items = result.get("items") or []
        for contact in items:
            if isinstance(contact, dict) and is_duplicate_contact(contact, phones, emails):
                matches.append(contact)

        cursor = result.get("cursor")
        if not cursor:
            break
    return matches


def format_contact_name(contact: dict[str, Any]) -> str:
    display = contact.get("display_name")
    if display:
        return str(display)
    first = (contact.get("first_name") or "").strip()
    last = (contact.get("last_name") or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    return "Unknown"


def duplicate_error(phones: list[str], emails: list[str], matches: list[dict[str, Any]]) -> None:
    preview = []
    for contact in matches[:3]:
        preview.append(f"{format_contact_name(contact)} (id={contact.get('id', 'unknown')})")
    more = f", plus {len(matches)-3} more" if len(matches) > 3 else ""
    raise WrapperError(
        f"Duplicate contact detected for provided "
        f"phone/email identifiers {phones + emails}. "
        f"Existing matches: {', '.join(preview)}{more}. "
        "Use --allow-duplicate to create anyway."
    )


def main() -> int:
    if not generated_cli_available():
        return run_legacy("create_contact.py", sys.argv[1:])

    args = build_parser().parse_args()

    try:
        require_api_key()

        phones = parse_repeated(args.phone)
        emails = parse_repeated(args.email)
        urls = parse_repeated(args.url)
        validate_args(phones, emails, urls)

        duplicates = find_duplicates(phones, emails, args.owner_id, args.max_pages)
        if duplicates and not args.allow_duplicate:
            duplicate_error(phones, emails, duplicates)

        payload = build_payload(
            first_name=args.first_name,
            last_name=args.last_name,
            phones=phones,
            emails=emails,
            urls=urls,
            company_name=args.company_name,
            job_title=args.job_title,
            extension=args.extension,
            owner_id=args.owner_id,
        )
        result = run_generated_json(["contacts", "contacts.create", "--data", json.dumps(payload)])

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("Created contact:")
            print(f"   ID: {result.get('id', 'N/A')}")
            print(f"   Name: {args.first_name} {args.last_name}")
            if phones:
                print(f"   Primary phone: {phones[0]}")
            if args.owner_id:
                print(f"   Owner ID: {args.owner_id}")
        return 0
    except WrapperError as err:
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
