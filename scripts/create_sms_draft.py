#!/usr/bin/env python3
"""Create a reviewable Dialpad SMS draft without sending it."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from sms_approval import RISK_NORMAL, RISK_RISKY, create_draft, init_db, invalidate_pending


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a Dialpad SMS approval draft")
    parser.add_argument("--thread-key", required=True)
    parser.add_argument("--to", required=True, dest="customer_number")
    parser.add_argument("--from", required=True, dest="sender_number")
    message_group = parser.add_mutually_exclusive_group(required=True)
    message_group.add_argument("--message")
    message_group.add_argument("--message-file")
    message_group.add_argument("--message-stdin", action="store_true")
    parser.add_argument("--source-inbound-id")
    parser.add_argument("--risk-state", choices=(RISK_NORMAL, RISK_RISKY), default=RISK_NORMAL)
    parser.add_argument("--risk-reason")
    parser.add_argument("--context-fingerprint")
    parser.add_argument("--keep-pending", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def resolve_message_text(args: argparse.Namespace) -> str:
    if args.message is not None:
        return args.message
    if args.message_file:
        return Path(args.message_file).read_text(encoding="utf-8")
    return sys.stdin.read()


def main() -> int:
    args = build_parser().parse_args()
    message = resolve_message_text(args)
    conn = init_db()
    try:
        if not args.keep_pending:
            invalidate_pending(
                conn,
                thread_key=args.thread_key,
                reason="superseded_by_new_draft",
            )
        draft = create_draft(
            conn,
            thread_key=args.thread_key,
            customer_number=args.customer_number,
            sender_number=args.sender_number,
            draft_text=message,
            source_inbound_id=args.source_inbound_id,
            risk_state=args.risk_state,
            risk_reason=args.risk_reason,
            context_fingerprint=args.context_fingerprint,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"ok": True, "draft": draft}, sort_keys=True))
    else:
        print(f"Draft created: {draft['draft_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
