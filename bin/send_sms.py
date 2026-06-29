#!/usr/bin/env python3
"""Compatibility wrapper: send_sms.py -> dialpad sms send."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _dialpad_compat import (
    COMMAND_IDS,
    WrapperArgumentParser,
    emit_success,
    handle_wrapper_exception,
    print_wrapper_error,
    require_generated_cli,
    require_api_key,
    resolve_sender,
    run_generated_json,
    WrapperError,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sms_approval


def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="Send SMS via Dialpad API")
    parser.add_argument("--to", nargs="+", required=True, help="Recipient E.164 numbers")
    message_group = parser.add_mutually_exclusive_group(required=True)
    message_group.add_argument("--message", help="SMS text content")
    message_group.add_argument(
        "--message-file",
        help="Read SMS text from a UTF-8 file path (safer for $ and shell-sensitive content)",
    )
    message_group.add_argument(
        "--message-stdin",
        action="store_true",
        help="Read SMS text from stdin (safer for $ and shell-sensitive content)",
    )
    parser.add_argument("--from", dest="from_number", help="Sender number")
    parser.add_argument("--profile", choices=("work", "sales"), help="Sender profile")
    parser.add_argument(
        "--allow-profile-mismatch",
        action="store_true",
        help="Allow --from to differ from mapped profile number",
    )
    parser.add_argument("--infer-country-code", action="store_true", help="Infer country code from sender")
    parser.add_argument("--dry-run", action="store_true", help="Print request and selected sender without sending")
    parser.add_argument(
        "--allow-suspicious-currency",
        action="store_true",
        help="Bypass malformed-currency preflight for intentionally unusual numeric text",
    )
    parser.add_argument(
        "--resolve-draft-id",
        help="Resolve an existing SMS approval draft after explicit operator approval",
    )
    parser.add_argument(
        "--approval-actor-id",
        help="Operator id/name that explicitly approved this direct agent send",
    )
    parser.add_argument("--approval-actor-username", help="Operator username/display name for audit context")
    parser.add_argument("--approval-actor-is-bot", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--confirm-risk",
        action="store_true",
        help="Confirm the operator explicitly approved sending a risky draft",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser


def resolve_message_text(args: argparse.Namespace) -> tuple[str, str]:
    if args.message is not None:
        message_text = args.message
        message_source = "--message"
    elif args.message_file:
        message_source = "--message-file"
        try:
            message_text = Path(args.message_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WrapperError(
                f"Failed to read {message_source} '{args.message_file}': {exc}",
                code="invalid_argument",
                retryable=False,
            ) from exc
    elif args.message_stdin:
        message_source = "--message-stdin"
        message_text = sys.stdin.read()
    else:
        raise WrapperError(
            "One of --message, --message-file, or --message-stdin is required.",
            code="invalid_argument",
            retryable=False,
        )

    if message_text == "":
        if args.message_file:
            raise WrapperError(
                f"Message text from --message-file '{args.message_file}' is empty.",
                code="invalid_argument",
                retryable=False,
            )
        if args.message_stdin:
            raise WrapperError(
                "Message text from --message-stdin is empty. Pipe content into stdin.",
                code="invalid_argument",
                retryable=False,
            )
        raise WrapperError("Message text cannot be empty.", code="invalid_argument", retryable=False)

    return message_text, message_source


def suspicious_currency_reasons(message_text: str) -> list[str]:
    checks = [
        (r"(?<!\S),\d{3}\b", "amount with stripped leading currency like ',035'"),
        (
            r"(?i)\b(?:amount|buyout|cost|credit|discount|financing|payment|price|quote|total)"
            r"\s*(?::|=|-|is|was|of|for|at)?\s*(?<![$\d])\d{1,3},\d{3}\b",
            "currency-context amount with stripped leading currency like '0,035' or '20,035'",
        ),
        (r"(?i)(?:about|around|roughly|approx(?:imately)?|~)\s*\d{1,3}\s+(?:less|more|off|credit)\b", "currency word with bare number"),
        (r"~\d{2,4}\s*-\s*\d{2,4}\s*/\s*month\b", "monthly range missing currency symbol"),
        (r"\bcurrent\s+\d{2,4}\s*/\s*month\b", "current monthly amount missing currency symbol"),
    ]
    return [reason for pattern, reason in checks if re.search(pattern, message_text)]


def validate_message_text(message_text: str, allow_suspicious_currency: bool = False) -> None:
    reasons = suspicious_currency_reasons(message_text)
    if reasons and not allow_suspicious_currency:
        raise WrapperError(
            "SMS text looks like shell expansion stripped currency symbols: "
            + "; ".join(reasons)
            + ". Use --message-file or --message-stdin to preserve '$', or pass "
            "--allow-suspicious-currency if this formatting is intentional.",
            code="invalid_argument",
            retryable=False,
        )


def _build_payload(
    to_numbers: list[str],
    message_text: str,
    infer_country_code: bool,
    sender_number: str,
) -> dict[str, object]:
    return {
        "to_numbers": to_numbers,
        "text": message_text,
        "infer_country_code": infer_country_code,
        "from_number": sender_number,
    }


def summarize_message_status(result: object) -> tuple[str, str | None]:
    if not isinstance(result, dict):
        return "unknown", None

    raw_status = result.get("message_status")
    if raw_status is None:
        raw_status = result.get("status")
    if raw_status is None:
        return "unknown", None

    raw_text = str(raw_status).strip()
    if not raw_text:
        return "unknown", None

    normalized = "accepted/queued" if raw_text.lower() == "pending" else raw_text
    return normalized, raw_text


def annotate_message_status(result: object) -> object:
    if not isinstance(result, dict):
        return result

    normalized_status, raw_status = summarize_message_status(result)
    annotated = dict(result)
    annotated["delivery_status"] = normalized_status
    if raw_status is not None:
        annotated["delivery_status_raw"] = raw_status
        annotated["status"] = normalized_status
        annotated["status_raw"] = raw_status
    return annotated


def _audit_args_present(args: argparse.Namespace) -> bool:
    return any(
        (
            args.resolve_draft_id,
            args.approval_actor_id,
            args.approval_actor_username,
            args.approval_actor_is_bot,
            args.confirm_risk,
        )
    )


def preflight_approval_audit(
    args: argparse.Namespace,
    *,
    sender_number: str,
    message_text: str,
    claim: bool = False,
) -> dict[str, object] | None:
    if not _audit_args_present(args):
        return None
    if not args.resolve_draft_id:
        raise WrapperError(
            "Approval audit flags require --resolve-draft-id.",
            code="invalid_argument",
            retryable=False,
        )
    if not args.approval_actor_id:
        raise WrapperError(
            "--approval-actor-id is required with --resolve-draft-id.",
            code="invalid_argument",
            retryable=False,
        )
    if len(args.to) != 1:
        raise WrapperError(
            "--resolve-draft-id supports exactly one --to recipient.",
            code="invalid_argument",
            retryable=False,
        )

    conn = sms_approval.init_db()
    try:
        result = sms_approval.preflight_agent_direct_send(
            conn,
            draft_id=args.resolve_draft_id,
            actor_id=args.approval_actor_id,
            actor_username=args.approval_actor_username,
            customer_number=args.to[0],
            sender_number=sender_number,
            draft_text=message_text,
            actor_is_bot=args.approval_actor_is_bot,
            confirm_risk=args.confirm_risk,
            claim=claim,
        )
    finally:
        conn.close()

    expected_status = "claimed" if claim else "ready"
    if not result.get("ok") or result.get("status") != expected_status:
        sanitized_result = {
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
            "reason": result.get("reason"),
            "draft_id": args.resolve_draft_id,
            "approval_source": sms_approval.APPROVAL_SOURCE_AGENT_DIRECT_SEND,
            "approval_actor_trust": sms_approval.APPROVAL_ACTOR_TRUST_AGENT_ASSERTED,
        }
        raise WrapperError(
            str(result.get("reason") or result.get("status") or "approval_audit_preflight_failed"),
            code="validation_failed",
            retryable=False,
            meta={"approval_audit": sanitized_result},
        )
    return {
        "draft_id": args.resolve_draft_id,
        "status": expected_status,
        "approval_source": sms_approval.APPROVAL_SOURCE_AGENT_DIRECT_SEND,
        "approval_actor_trust": sms_approval.APPROVAL_ACTOR_TRUST_AGENT_ASSERTED,
    }


def record_approval_audit(args: argparse.Namespace, send_result: object) -> dict[str, object] | None:
    if not args.resolve_draft_id:
        return None

    try:
        conn = sms_approval.init_db()
        try:
            result = sms_approval.record_agent_direct_send(
                conn,
                draft_id=args.resolve_draft_id,
                actor_id=args.approval_actor_id,
                actor_username=args.approval_actor_username,
                send_result=send_result,
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - SMS API already returned; report audit failure without encouraging retry.
        return {
            "ok": False,
            "status": "audit_record_failed",
            "draft_id": args.resolve_draft_id,
            "error": str(exc),
            "approval_source": sms_approval.APPROVAL_SOURCE_AGENT_DIRECT_SEND,
            "approval_actor_trust": sms_approval.APPROVAL_ACTOR_TRUST_AGENT_ASSERTED,
        }

    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "sent": bool(result.get("sent")),
        "draft_id": args.resolve_draft_id,
        "dialpad_sms_id": result.get("dialpad_sms_id"),
        "delivery_status": result.get("delivery_status"),
        "error": result.get("error"),
        "reason": result.get("reason"),
        "approval_source": result.get("approval_source") or sms_approval.APPROVAL_SOURCE_AGENT_DIRECT_SEND,
        "approval_actor_trust": result.get("approval_actor_trust") or sms_approval.APPROVAL_ACTOR_TRUST_AGENT_ASSERTED,
    }


def fail_claimed_approval_audit(args: argparse.Namespace, error: Exception) -> None:
    if not args.resolve_draft_id:
        return

    try:
        conn = sms_approval.init_db()
        try:
            sms_approval.fail_agent_direct_send(
                conn,
                draft_id=args.resolve_draft_id,
                error=str(error),
            )
        finally:
            conn.close()
    except Exception:
        pass


def attach_approval_audit(result: object, approval_audit: dict[str, object] | None) -> object:
    if approval_audit is None:
        return result
    if not isinstance(result, dict):
        return {"result": result, "approval_audit": approval_audit}
    annotated = dict(result)
    annotated["approval_audit"] = approval_audit
    return annotated


def main() -> int:
    json_mode = "--json" in sys.argv
    command = COMMAND_IDS["send_sms.send"]
    wrapper = "send_sms.py"

    try:
        args = build_parser().parse_args()
        json_mode = args.json
        require_generated_cli()
        sender_number, sender_source = resolve_sender(
            args.from_number, args.profile, allow_profile_mismatch=args.allow_profile_mismatch
        )
        message_text, message_source = resolve_message_text(args)
        validate_message_text(message_text, args.allow_suspicious_currency)
        approval_preflight = preflight_approval_audit(args, sender_number=sender_number, message_text=message_text)
        payload = _build_payload(args.to, message_text, args.infer_country_code, sender_number)

        if args.dry_run:
            if json_mode:
                data = {
                    "mode": "dry_run",
                    "sender_number": sender_number,
                    "sender_source": sender_source,
                    "message_source": message_source,
                    "payload": payload,
                }
                if approval_preflight is not None:
                    data["approval_audit"] = approval_preflight
                emit_success(
                    command,
                    wrapper,
                    data,
                )
            else:
                print("Dry run: SMS not sent")
                print(f"Selected sender: {sender_number} ({sender_source})")
                print(f"Message source: {message_source}")
                print(f"To: {', '.join(args.to)}")
                print(f"Message length: {len(message_text)}")
                if approval_preflight:
                    print(f"Approval audit: ready ({approval_preflight['draft_id']})")
                print("Message preview:")
                print(message_text)
            return 0

        require_api_key()
        approval_preflight = preflight_approval_audit(args, sender_number=sender_number, message_text=message_text, claim=True)
        try:
            result = run_generated_json(["sms", "send", "--data", json.dumps(payload)])
        except WrapperError as err:
            fail_claimed_approval_audit(args, err)
            raise
        approval_audit = record_approval_audit(args, result)
        annotated_result = attach_approval_audit(result, approval_audit)

        if json_mode:
            emit_success(
                command,
                wrapper,
                annotate_message_status(annotated_result) if isinstance(annotated_result, dict) else {"result": annotated_result},
            )
        else:
            print(f"Selected sender: {sender_number} ({sender_source})")
            print("SMS sent successfully!")
            print(f"   ID: {result.get('id', 'N/A')}")
            status_label, raw_status = summarize_message_status(result)
            status_line = f"   Status: {status_label}"
            if raw_status and raw_status != status_label:
                status_line += f" (raw: {raw_status})"
            print(status_line)
            print(f"   From: {sender_number}")
            to_numbers = result.get("to_numbers") or args.to
            print(f"   To: {', '.join(to_numbers)}")
            if approval_audit:
                audit_status = approval_audit.get("status")
                audit_label = "recorded" if approval_audit.get("ok") else "failed"
                print(f"   Approval audit: {audit_label} ({audit_status})")

        return 0
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
