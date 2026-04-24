#!/usr/bin/env python3
"""Stable wrapper for approving Dialpad SMS drafts."""

from __future__ import annotations

import argparse
import hmac
import os
import sys
from pathlib import Path

from _dialpad_compat import (
    COMMAND_IDS,
    WrapperArgumentParser,
    WrapperError,
    emit_success,
    handle_wrapper_exception,
    print_wrapper_error,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from sms_approval import ACTION_APPROVE, ACTION_CONFIRM_RISK, approve_draft, init_db


def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="Approve a Dialpad SMS draft")
    parser.add_argument("draft_id")
    parser.add_argument("--action", choices=(ACTION_APPROVE, ACTION_CONFIRM_RISK), default=ACTION_APPROVE)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--actor-username")
    parser.add_argument("--actor-is-bot", action="store_true")
    parser.add_argument("--approval-token")
    parser.add_argument("--json", action="store_true")
    return parser


def require_approval_token(provided_token: str | None) -> None:
    expected_token = os.environ.get("DIALPAD_SMS_APPROVAL_TOKEN")
    if not expected_token:
        raise WrapperError(
            "DIALPAD_SMS_APPROVAL_TOKEN is not configured; CLI approval is disabled.",
            code="validation_failed",
            retryable=False,
        )
    if not provided_token or not hmac.compare_digest(provided_token, expected_token):
        raise WrapperError("Invalid or missing approval token.", code="validation_failed", retryable=False)


def main() -> int:
    json_mode = "--json" in sys.argv
    command = COMMAND_IDS["approve_sms_draft.approve"]
    wrapper = "approve_sms_draft.py"

    try:
        args = build_parser().parse_args()
        json_mode = args.json
        require_approval_token(args.approval_token)
        conn = init_db()
        try:
            result = approve_draft(
                conn,
                draft_id=args.draft_id,
                actor_id=args.actor_id,
                actor_username=args.actor_username,
                action=args.action,
                actor_is_bot=args.actor_is_bot,
            )
        finally:
            conn.close()

        if not result.get("ok"):
            raise WrapperError(str(result.get("status") or "approval_failed"), code="validation_failed", retryable=False, meta={"result": result})
        if json_mode:
            emit_success(command, wrapper, result)
        elif result.get("sent"):
            print(f"SMS sent: {result.get('dialpad_sms_id') or 'unknown id'}")
        else:
            print(f"SMS not sent: {result.get('status')}")
        return 0 if result.get("ok") else 2
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2
    except Exception as err:  # noqa: BLE001 - wrapper boundary
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
