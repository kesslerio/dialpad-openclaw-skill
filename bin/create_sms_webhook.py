#!/usr/bin/env python3
"""Compatibility wrapper: create_sms_webhook.py -> dialpad webhook create."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from _dialpad_compat import (
    COMMAND_IDS,
    WrapperArgumentParser,
    emit_success,
    handle_wrapper_exception,
    print_wrapper_error,
    require_generated_cli,
    require_api_key,
    run_generated_json,
    WrapperError,
)



def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="Create SMS webhook subscriptions via Dialpad API")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a webhook + SMS subscription")
    create_parser.add_argument("--url", required=True, help="Webhook URL")
    create_parser.add_argument("--events", help="Comma-separated events (compat only)")
    create_parser.add_argument("--office-id", dest="office_id", help="Optional office ID")
    create_parser.add_argument("--direction", default="all", choices=["all", "inbound", "outbound"])
    create_parser.add_argument("--json", action="store_true", help="Output JSON")

    list_parser = subparsers.add_parser("list", help="List SMS event subscriptions")
    list_parser.add_argument("--json", action="store_true", help="Output JSON")

    delete_parser = subparsers.add_parser("delete", help="Delete SMS event subscription")
    delete_parser.add_argument("id", help="Subscription ID")
    delete_parser.add_argument("--json", action="store_true", help="Output JSON")

    webhook_parser = subparsers.add_parser("webhooks", help="Manage raw webhooks")
    webhook_sub = webhook_parser.add_subparsers(dest="webhook_command", required=True)
    webhook_list = webhook_sub.add_parser("list", help="List webhooks")
    webhook_list.add_argument("--json", action="store_true", help="Output JSON")
    webhook_delete = webhook_sub.add_parser("delete", help="Delete webhook")
    webhook_delete.add_argument("id", help="Webhook ID")
    webhook_delete.add_argument("--json", action="store_true", help="Output JSON")

    return parser



def validate_events(events: str | None) -> None:
    if not events:
        return
    allowed = {"sms_sent", "sms_received"}
    provided = {event.strip() for event in events.split(",") if event.strip()}
    unsupported = sorted(provided - allowed)
    if unsupported:
        raise WrapperError(f"Unsupported events for SMS subscription: {', '.join(unsupported)}")



def handle_create(args: argparse.Namespace) -> dict[str, object]:
    validate_events(args.events)
    webhook = run_generated_json(["webhook", "create", "--hook-url", args.url])
    webhook_id = webhook.get("id")
    if not webhook_id:
        raise WrapperError(f"Webhook create did not return id: {webhook}")

    # Map legacy event names to API direction field:
    #   sms_sent -> outbound, sms_received -> inbound, both -> all
    direction = args.direction
    event_types = None
    if args.events:
        provided = {event.strip() for event in args.events.split(",") if event.strip()}
        event_types = sorted(list(provided))
        if "sms_sent" in provided and "sms_received" in provided:
            direction = "all"
        elif "sms_sent" in provided:
            direction = "outbound"
        elif "sms_received" in provided:
            direction = "inbound"

    try:
        webhook_id = int(webhook_id)
    except ValueError as exc:
        raise WrapperError(f"Invalid webhook_id returned: {webhook_id}") from exc

    payload = {
        "endpoint_id": webhook_id,
        "direction": direction,
    }
    if event_types:
        payload["event_types"] = event_types
    if args.office_id:
        try:
            payload["target_type"] = "office"
            payload["target_id"] = int(args.office_id)
        except ValueError as exc:
            # Clean up the webhook before raising
            try:
                run_generated_json(["webhooks", "webhooks.delete", "--id", str(webhook_id)])
            except WrapperError:
                pass
            raise WrapperError(f"Invalid --office-id: {args.office_id}") from exc

    try:
        subscription = run_generated_json([
            "subscriptions",
            "webhook_sms_event_subscription.create",
            "--data",
            json.dumps(payload),
        ])
    except WrapperError:
        # Subscription failed — clean up the orphaned webhook
        try:
            run_generated_json(["webhooks", "webhooks.delete", "--id", str(webhook_id)])
        except WrapperError:
            pass  # Best-effort cleanup
        raise
    result = {"subscription": subscription, "webhook": webhook}

    if not args.json:
        print("Webhook subscription created!")
        print(f"   Subscription ID: {subscription.get('id')}")
        print(f"   Webhook ID: {webhook.get('id')}")
        print(f"   Webhook URL: {webhook.get('hook_url')}")
        print(f"   Direction: {subscription.get('direction')}")
        print(f"   Enabled: {subscription.get('enabled')}")
    return result



def handle_list(json_mode: bool) -> dict[str, object]:
    result = run_generated_json(["subscriptions", "webhook_sms_event_subscription.list"])
    items = result.get("items", [])
    if not json_mode:
        print(f"SMS Webhook Subscriptions: {len(items)}")
        for sub in items:
            print(f"   ID: {sub.get('id')}")
            print(f"   Direction: {sub.get('direction')}")
            print(f"   Enabled: {sub.get('enabled')}")
            print()
    return {"items": items, "count": len(items)}



def handle_webhooks(args: argparse.Namespace, json_mode: bool) -> tuple[str, dict[str, object]]:
    if args.webhook_command == "list":
        result = run_generated_json(["webhooks", "webhooks.list"])
        items = result.get("items", [])
        if not json_mode:
            print(f"Webhooks: {len(items)}")
            for hook in items:
                print(f"   ID: {hook.get('id')}")
                print(f"   URL: {hook.get('hook_url')}")
                print()
        return "create_sms_webhook.webhooks_list", {"items": items, "count": len(items)}

    if args.webhook_command == "delete":
        run_generated_json(["webhooks", "webhooks.delete", "--id", args.id])
        if not json_mode:
            print(f"Successfully deleted webhook {args.id}")
        return "create_sms_webhook.webhooks_delete", {"id": args.id, "deleted": True}
    raise WrapperError("Unsupported webhook command", code="invalid_argument", retryable=False)



def main() -> int:
    json_mode = "--json" in sys.argv
    wrapper = "create_sms_webhook.py"
    command_key = "create_sms_webhook.create"

    try:
        args = build_parser().parse_args()
        json_mode = bool(getattr(args, "json", False))
        if args.command == "list":
            command_key = "create_sms_webhook.list"
        elif args.command == "delete":
            command_key = "create_sms_webhook.delete"
        elif args.command == "webhooks":
            command_key = (
                "create_sms_webhook.webhooks_delete"
                if getattr(args, "webhook_command", "") == "delete"
                else "create_sms_webhook.webhooks_list"
            )
        require_generated_cli()
        require_api_key()

        if args.command == "create":
            result = handle_create(args)
            if json_mode:
                emit_success(COMMAND_IDS["create_sms_webhook.create"], wrapper, result)
            return 0
        if args.command == "list":
            result = handle_list(json_mode)
            if json_mode:
                emit_success(COMMAND_IDS["create_sms_webhook.list"], wrapper, result)
            return 0
        if args.command == "delete":
            run_generated_json(["subscriptions", "webhook_sms_event_subscription.delete", "--id", args.id])
            if json_mode:
                emit_success(
                    COMMAND_IDS["create_sms_webhook.delete"],
                    wrapper,
                    {"id": args.id, "deleted": True},
                )
            else:
                print(f"Successfully deleted subscription {args.id}")
            return 0
        if args.command == "webhooks":
            command_key, result = handle_webhooks(args, json_mode)
            if json_mode:
                emit_success(COMMAND_IDS[command_key], wrapper, result)
            return 0

        raise WrapperError("Unsupported command")
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(
                COMMAND_IDS[command_key],
                wrapper,
                err,
                True,
            )
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
