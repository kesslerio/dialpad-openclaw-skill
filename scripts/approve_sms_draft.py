#!/usr/bin/env python3
"""Approve a previously created Dialpad SMS draft."""

from __future__ import annotations

import argparse
import json
import os
import sys
import hmac
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from sms_approval import ACTION_APPROVE, ACTION_CONFIRM_RISK, approve_draft, init_db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Approve a Dialpad SMS draft")
    parser.add_argument("draft_id")
    parser.add_argument("--action", choices=(ACTION_APPROVE, ACTION_CONFIRM_RISK), default=ACTION_APPROVE)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--actor-username")
    parser.add_argument("--actor-is-bot", action="store_true")
    parser.add_argument("--approval-token")
    parser.add_argument("--json", action="store_true")
    return parser


def validate_approval_token(provided_token: str | None) -> dict[str, object] | None:
    expected_token = os.environ.get("DIALPAD_SMS_APPROVAL_TOKEN")
    if not expected_token:
        return {
            "ok": False,
            "status": "approval_token_required",
            "sent": False,
            "reason": "DIALPAD_SMS_APPROVAL_TOKEN_not_configured",
        }
    if not provided_token or not hmac.compare_digest(provided_token, expected_token):
        return {
            "ok": False,
            "status": "approval_token_invalid",
            "sent": False,
            "reason": "approval_token_missing_or_invalid",
        }
    return None


def main() -> int:
    args = build_parser().parse_args()
    token_error = validate_approval_token(args.approval_token)
    if token_error is not None:
        if args.json:
            print(json.dumps(token_error, sort_keys=True))
        else:
            print(f"SMS not sent: {token_error['status']}")
        return 2

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

    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif result.get("sent"):
        print(f"SMS sent: {result.get('dialpad_sms_id') or 'unknown id'}")
    else:
        print(f"SMS not sent: {result.get('status')}")
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
