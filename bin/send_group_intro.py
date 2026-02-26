#!/usr/bin/env python3
"""Compatibility wrapper: send_group_intro.py -> mirrored SMS intro fallback."""

from __future__ import annotations

import argparse
import json

from _dialpad_compat import (
    COMMAND_IDS,
    emit_success,
    handle_wrapper_exception,
    print_wrapper_error,
    require_generated_cli,
    require_api_key,
    resolve_sender,
    run_generated_json,
    WrapperError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a mirrored one-to-one group intro")
    parser.add_argument("--prospect", required=True, help="Prospect phone number (E.164)")
    parser.add_argument("--reference", required=True, help="Reference phone number (E.164)")
    parser.add_argument("--from", dest="from_number", help="Sender number")
    parser.add_argument("--profile", choices=("work", "sales"), help="Sender profile")
    parser.add_argument(
        "--allow-profile-mismatch",
        action="store_true",
        help="Allow --from to differ from mapped profile number",
    )
    parser.add_argument("--prospect-name", help="Prospect display name")
    parser.add_argument("--reference-name", help="Reference display name")
    parser.add_argument("--message", help="Custom intro body text")
    parser.add_argument(
        "--confirm-share",
        action="store_true",
        help="Confirm that prospect and reference numbers are being shared",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print request intent without sending")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser


def _label_for_party(name: str | None, fallback: str) -> str:
    return name.strip() if name and name.strip() else fallback


def _build_intro_text(
    to_name: str | None,
    to_number: str,
    other_name: str,
    other_number: str,
    custom_message: str | None,
    to_role: str,
) -> str:
    recipient = _label_for_party(to_name, to_role)
    intro_body = custom_message.strip() if custom_message and custom_message.strip() else (
        f"Hi {recipient}, I’m connecting you with {other_name}."
        f" You can respond to this number directly at {other_number}."
    )
    details = (
        f"\n\nShared intro details:\n"
        f"{other_name}: {other_number}\n"
    )
    return f"{intro_body}{details}"


def _build_payload(sender: str, to_number: str, message: str) -> dict[str, object]:
    return {
        "to_numbers": [to_number],
        "text": message,
        "infer_country_code": False,
        "from_number": sender,
    }


def _send_single_sms(sender: str, to_number: str, message: str) -> dict[str, object]:
    payload = _build_payload(sender, to_number, message)
    return run_generated_json(["sms", "send", "--data", json.dumps(payload)])


def main() -> int:
    command = COMMAND_IDS["send_group_intro.send"]
    wrapper = "send_group_intro.py"
    json_mode = False
    try:
        require_generated_cli()
        args = build_parser().parse_args()
        json_mode = args.json
        if not args.confirm_share:
            raise WrapperError(
                "Refusing to send group intro without --confirm-share because it shares phone numbers."
            )
        sender_number, sender_source = resolve_sender(
            args.from_number, args.profile, allow_profile_mismatch=args.allow_profile_mismatch
        )

        prospect_name = _label_for_party(args.prospect_name, "the prospect")
        reference_name = _label_for_party(args.reference_name, "the reference")

        prospect_message = _build_intro_text(
            args.prospect_name,
            args.prospect,
            reference_name,
            args.reference,
            args.message,
            "prospect",
        )
        reference_message = _build_intro_text(
            args.reference_name,
            args.reference,
            prospect_name,
            args.prospect,
            args.message,
            "reference",
        )

        if args.dry_run:
            summary = {
                "mode": "mirrored_fallback",
                "sender_number": sender_number,
                "sender_source": sender_source,
                "prospect": {
                    "to": args.prospect,
                    "message": prospect_message,
                    "status": "pending",
                },
                "reference": {
                    "to": args.reference,
                    "message": reference_message,
                    "status": "pending",
                },
                "dry_run": True,
            }
            if json_mode:
                emit_success(command, wrapper, summary)
            else:
                print("Dry run: no messages sent")
                print(f"Mode: {summary['mode']}")
                print(f"Selected sender: {summary['sender_number']} ({summary['sender_source']})")
                print(f"Prospect: {summary['prospect']['to']} -> length {len(summary['prospect']['message'])}")
                print(f"Reference: {summary['reference']['to']} -> length {len(summary['reference']['message'])}")
            return 0

        require_api_key()
        prospect_result = _send_single_sms(sender_number, args.prospect, prospect_message)
        prospect_id = prospect_result.get("id") or "N/A"

        try:
            reference_result = _send_single_sms(sender_number, args.reference, reference_message)
        except WrapperError as err:
            raise WrapperError(
                "Prospect message sent successfully "
                f"(first_message_id={prospect_id}). "
                f"Reference message failed: {err}. This is a partial success state.",
                code="partial_success",
                retryable=False,
            ) from err

        if json_mode:
            emit_success(
                command,
                wrapper,
                {
                    "mode": "mirrored_fallback",
                    "sender_number": sender_number,
                    "sender_source": sender_source,
                    "prospect": {
                        "to": args.prospect,
                        "id": prospect_result.get("id"),
                        "status": prospect_result.get("message_status"),
                    },
                    "reference": {
                        "to": args.reference,
                        "id": reference_result.get("id"),
                        "status": reference_result.get("message_status"),
                    },
                },
            )
        else:
            print("Mode: mirrored_fallback")
            print(f"Selected sender: {sender_number} ({sender_source})")
            print(
                "Prospect message: "
                f"{prospect_result.get('id')} / {prospect_result.get('message_status')}"
            )
            print(
                "Reference message: "
                f"{reference_result.get('id')} / {reference_result.get('message_status')}"
            )
        return 0
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
