#!/usr/bin/env python3
"""Compatibility wrapper: update_contact.py -> dialpad contacts update."""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from _dialpad_compat import (
    print_wrapper_error,
    require_generated_cli,
    require_api_key,
    run_generated_json,
    WrapperError,
)


PHONE_RE = re.compile(r"^\+\d{7,15}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update contacts via Dialpad API")
    parser.add_argument("--id", required=True, help="Contact ID")
    parser.add_argument("--first-name", help="Contact first name")
    parser.add_argument("--last-name", help="Contact last name")
    parser.add_argument("--phone", action="append", help="Phone number (E.164). Repeatable.")
    parser.add_argument("--email", action="append", help="Email address. Repeatable.")
    parser.add_argument("--company-name", help="Company name")
    parser.add_argument("--job-title", help="Job title")
    parser.add_argument("--extension", help="Extension")
    parser.add_argument("--url", action="append", help="Associated URL. Repeatable.")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser


def parse_repeated(values: list[str] | None) -> list[str]:
    if not values:
        return []
    collected: list[str] = []
    for value in values:
        collected.extend(part.strip() for part in str(value).split(",") if part.strip())
    return collected


def validate_args(
    contact_id: str,
    phones: list[str],
    emails: list[str],
    urls: list[str],
    first_name: str | None,
    last_name: str | None,
    company_name: str | None,
    job_title: str | None,
    extension: str | None,
) -> None:
    if not contact_id.strip():
        raise WrapperError("Missing --id")

    if not any([first_name, last_name, phones, emails, company_name, job_title, extension, urls]):
        raise WrapperError(
            "No update fields provided. Provide at least one of: "
            "--first-name, --last-name, --phone, --email, --company-name, --job-title, --extension, --url"
        )

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
    first_name: str | None,
    last_name: str | None,
    phones: list[str],
    emails: list[str],
    urls: list[str],
    company_name: str | None,
    job_title: str | None,
    extension: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if first_name:
        payload["first_name"] = first_name
    if last_name:
        payload["last_name"] = last_name
    if company_name:
        payload["company_name"] = company_name
    if job_title:
        payload["job_title"] = job_title
    if extension:
        payload["extension"] = extension
    if phones:
        payload["phones"] = phones
    if emails:
        payload["emails"] = emails
    if urls:
        payload["urls"] = urls
    return payload


def clear_not_found_error(contact_id: str, message: str) -> None:
    lowered = message.lower()
    if "404" in lowered and "not found" in lowered:
        raise WrapperError(f"Contact not found: {contact_id}")
    raise WrapperError(message)


def main() -> int:
    args = build_parser().parse_args()

    try:
        require_generated_cli()
        require_api_key()

        phones = parse_repeated(args.phone)
        emails = parse_repeated(args.email)
        urls = parse_repeated(args.url)
        validate_args(
            args.id,
            phones,
            emails,
            urls,
            args.first_name,
            args.last_name,
            args.company_name,
            args.job_title,
            args.extension,
        )

        payload = build_payload(
            args.first_name,
            args.last_name,
            phones,
            emails,
            urls,
            args.company_name,
            args.job_title,
            args.extension,
        )

        try:
            result = run_generated_json(
                ["contacts", "contacts.update", "--id", args.id, "--data", json.dumps(payload)]
            )
        except WrapperError as err:
            clear_not_found_error(args.id, str(err))

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Updated contact {args.id}:")
            print(f"   ID: {result.get('id', 'N/A')}")
        return 0
    except WrapperError as err:
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
