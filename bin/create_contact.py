#!/usr/bin/env python3
"""Compatibility wrapper: create_contact.py -> dialpad contacts create."""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from _dialpad_compat import (
    COMMAND_IDS,
    emit_success,
    handle_wrapper_exception,
    print_wrapper_error,
    require_generated_cli,
    require_api_key,
    run_generated_json,
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
    parser.add_argument(
        "--scope",
        choices=["auto", "shared", "local", "both"],
        default="auto",
        help="Target scope: shared, local, both, or auto (owner-id => both, else shared)",
    )
    parser.add_argument("--owner-id", action="append", default=[], help="Local owner ID targets. Repeatable.")
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
    max_pages: int,
    owner_ids: list[str],
    scope: str,
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
    if max_pages <= 0:
        raise WrapperError("Invalid --max-pages value. Use a positive integer.")
    if scope in {"local", "both"} and not owner_ids:
        raise WrapperError(f"--owner-id is required when --scope is '{scope}'.")


def resolve_scope(scope: str, owner_ids: list[str]) -> str:
    if scope == "auto":
        return "both" if owner_ids else "shared"
    return scope


def unique_owner_ids(owner_ids: list[str]) -> list[str]:
    if not owner_ids:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for owner_id in owner_ids:
        value = (owner_id or "").strip()
        if not value:
            continue
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


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


def _contact_ref(contact: dict[str, Any]) -> str:
    contact_id = str(contact.get("id") or "unknown")
    name = str(contact.get("display_name") or "").strip()
    if not name:
        first = str(contact.get("first_name") or "").strip()
        last = str(contact.get("last_name") or "").strip()
        name = f"{first} {last}".strip() or "Unknown"
    return f"{name} (id={contact_id})"


def find_matching_contact(
    phones: list[str],
    emails: list[str],
    owner_id: str | None,
    max_pages: int,
    include_local: bool,
) -> dict[str, Any] | None:
    if not phones and not emails:
        return None

    cursor: str | None = None
    matches: list[dict[str, Any]] = []

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
                if len(matches) > 1:
                    scope = f"owner {owner_id}" if owner_id else "shared"
                    refs = ", ".join(_contact_ref(m) for m in matches[:3])
                    raise WrapperError(
                        "Ambiguous contact match for "
                        f"{phones + emails} in {scope} scope. "
                        f"Matched {len(matches)} contacts: {refs}. "
                        "Refine identifiers or use explicit contact update by ID."
                    )

        cursor = result.get("cursor")
        if not cursor:
            break

    return matches[0] if matches else None


def create_contact(payload: dict[str, Any]) -> dict[str, Any]:
    return run_generated_json(["contacts", "contacts.create", "--data", json.dumps(payload)])


def update_contact(contact_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return run_generated_json(
        ["contacts", "contacts.update", "--id", contact_id, "--data", json.dumps(payload)]
    )


def is_owner_not_found_error(message: str) -> bool:
    lowered = message.lower()
    return "404" in lowered and "owner" in lowered and "not found" in lowered


def sync_shared_contact(
    base_payload: dict[str, Any],
    phones: list[str],
    emails: list[str],
    allow_duplicate: bool,
    max_pages: int,
) -> tuple[str, dict[str, Any]]:
    if allow_duplicate:
        result = create_contact(base_payload)
        return "created", result

    match = find_matching_contact(phones, emails, owner_id=None, max_pages=max_pages, include_local=False)
    if not match:
        return "created", create_contact(base_payload)
    return "updated", update_contact(str(match.get("id")), base_payload)


def sync_local_contact(
    owner_id: str,
    base_payload: dict[str, Any],
    phones: list[str],
    emails: list[str],
    allow_duplicate: bool,
    max_pages: int,
) -> tuple[str, dict[str, Any]] | tuple[str, str]:
    if allow_duplicate:
        payload = {**base_payload, "owner_id": owner_id}
        return "created", create_contact(payload)

    match = find_matching_contact(
        phones,
        emails,
        owner_id=owner_id,
        max_pages=max_pages,
        include_local="true",
    )
    if match:
        return "updated", update_contact(str(match.get("id")), base_payload)

    payload = {**base_payload, "owner_id": owner_id}
    return "created", create_contact(payload)


def main() -> int:
    args = build_parser().parse_args()
    json_mode = args.json
    command = COMMAND_IDS["create_contact.upsert"]
    wrapper = "create_contact.py"

    try:
        require_generated_cli()
        require_api_key()

        phones = parse_repeated(args.phone)
        emails = parse_repeated(args.email)
        urls = parse_repeated(args.url)
        owner_ids = unique_owner_ids(args.owner_id)
        scope = resolve_scope(args.scope, owner_ids)
        validate_args(phones, emails, urls, args.max_pages, owner_ids, scope)
        if args.scope == "auto":
            args.scope = scope

        base_payload = build_payload(
            first_name=args.first_name,
            last_name=args.last_name,
            phones=phones,
            emails=emails,
            urls=urls,
            company_name=args.company_name,
            job_title=args.job_title,
            extension=args.extension,
            owner_id=None,
        )

        results: dict[str, Any] = {
            "scope": args.scope,
            "shared": None,
            "locals": [],
            "warnings": [],
        }

        if scope in {"shared", "both"}:
            action, contact = sync_shared_contact(
                base_payload,
                phones,
                emails,
                args.allow_duplicate,
                args.max_pages,
            )
            results["shared"] = {"owner_id": None, "action": action, "contact": contact}

        if scope in {"local", "both"}:
            for owner_id in owner_ids:
                try:
                    action, contact = sync_local_contact(
                        owner_id,
                        base_payload,
                        phones,
                        emails,
                        args.allow_duplicate,
                        args.max_pages,
                    )
                    results["locals"].append(
                        {"owner_id": owner_id, "action": action, "contact": contact}
                    )
                except WrapperError as err:
                    message = str(err)
                    if is_owner_not_found_error(message):
                        results["warnings"].append(
                            {
                                "owner_id": owner_id,
                                "code": "owner_not_found",
                                "message": (
                                    f"Owner {owner_id} not found. Create was skipped for this owner. "
                                    "Remove it, or retry with a valid owner ID."
                                ),
                            }
                        )
                        if args.scope != "local":
                            continue
                    raise

        if json_mode:
            emit_success(command, wrapper, results)
        else:
            if results["shared"]:
                shared = results["shared"]
                print(f"{shared['action'].title()} shared contact:")
                contact = shared["contact"] or {}
                print(f"   ID: {contact.get('id', 'N/A')}")
                if phones:
                    print(f"   Primary phone: {phones[0]}")
            for item in results["locals"]:
                action = item["action"].title()
                print(f"{action} local contact for owner {item['owner_id']}:")
                contact = item["contact"] or {}
                print(f"   ID: {contact.get('id', 'N/A')}")
            if not results["shared"] and not results["locals"]:
                print("No contact sync targets were configured.")
            if results["warnings"]:
                print("\nWarnings:")
                for warning in results["warnings"]:
                    print(f" - {warning['message']}")
        return 0
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
