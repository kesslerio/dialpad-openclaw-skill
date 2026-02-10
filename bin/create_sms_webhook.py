#!/usr/bin/env python3
"""Compatibility wrapper: create_sms_webhook.py -> dialpad webhook create."""

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
    parser = argparse.ArgumentParser(description="Create SMS webhook subscriptions via Dialpad API")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a webhook + SMS subscription")
    create_parser.add_argument("--url", required=True, help="Webhook URL")
    create_parser.add_argument("--events", help="Comma-separated events (compat only)")
    create_parser.add_argument("--office-id", dest="office_id", help="Optional office ID")
    create_parser.add_argument("--direction", default="all", choices=["all", "inbound", "outbound"])
    create_parser.add_argument("--json", action="store_true", help="Output JSON")

    subparsers.add_parser("list", help="List SMS event subscriptions")

    delete_parser = subparsers.add_parser("delete", help="Delete SMS event subscription")
    delete_parser.add_argument("id", help="Subscription ID")

    webhook_parser = subparsers.add_parser("webhooks", help="Manage raw webhooks")
    webhook_sub = webhook_parser.add_subparsers(dest="webhook_command", required=True)
    webhook_sub.add_parser("list", help="List webhooks")
    webhook_delete = webhook_sub.add_parser("delete", help="Delete webhook")
    webhook_delete.add_argument("id", help="Webhook ID")

    return parser



def validate_events(events: str | None) -> None:
    if not events:
        return
    allowed = {"sms_sent", "sms_received"}
    provided = {event.strip() for event in events.split(",") if event.strip()}
    unsupported = sorted(provided - allowed)
    if unsupported:
        raise WrapperError(f"Unsupported events for SMS subscription: {', '.join(unsupported)}")



def create_subscription(url: str, direction: str, office_id: str | None) -> dict:
    webhook = run_generated_json(["webhook", "create", "--hook-url", url])
    webhook_id = webhook.get("id")
    if not webhook_id:
        raise WrapperError(f"Webhook create did not return id: {webhook}")

    cmd = [
        "subscriptions",
        "webhook_sms_event_subscription.create",
        "--endpoint-id",
        str(webhook_id),
        "--direction",
        direction,
    ]

    if office_id:
        cmd.extend(["--target-type", "office", "--target-id", office_id])

    subscription = run_generated_json(cmd)
    return {
        "subscription": subscription,
        "webhook": webhook,
    }



def main() -> int:
    if not generated_cli_available():
        return run_legacy("create_sms_webhook.py", sys.argv[1:])

    args = build_parser().parse_args()

    try:
        require_api_key()

        if args.command == "create":
            validate_events(args.events)
            result = create_subscription(args.url, args.direction, args.office_id)
            sub = result["subscription"]
            hook = result["webhook"]

            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print("Webhook subscription created!")
                print(f"   Subscription ID: {sub.get('id')}")
                print(f"   Webhook ID: {hook.get('id')}")
                print(f"   Webhook URL: {hook.get('hook_url')}")
                print(f"   Direction: {sub.get('direction')}")
                print(f"   Enabled: {sub.get('enabled')}")
            return 0

        if args.command == "list":
            result = run_generated_json(["subscriptions", "webhook_sms_event_subscription.list"])
            items = result.get("items", [])
            print(f"SMS Webhook Subscriptions: {len(items)}")
            for sub in items:
                print(f"   ID: {sub.get('id')}")
                print(f"   Direction: {sub.get('direction')}")
                print(f"   Enabled: {sub.get('enabled')}")
                print()
            return 0

        if args.command == "delete":
            run_generated_json(["subscriptions", "webhook_sms_event_subscription.delete", "--id", args.id])
            print(f"Successfully deleted subscription {args.id}")
            return 0

        if args.command == "webhooks" and args.webhook_command == "list":
            result = run_generated_json(["webhooks", "webhooks.list"])
            items = result.get("items", [])
            print(f"Webhooks: {len(items)}")
            for hook in items:
                print(f"   ID: {hook.get('id')}")
                print(f"   URL: {hook.get('hook_url')}")
                print()
            return 0

        if args.command == "webhooks" and args.webhook_command == "delete":
            run_generated_json(["webhooks", "webhooks.delete", "--id", args.id])
            print(f"Successfully deleted webhook {args.id}")
            return 0

        raise WrapperError("Unsupported command")
    except WrapperError as err:
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
