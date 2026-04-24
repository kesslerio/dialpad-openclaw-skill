#!/usr/bin/env python3
"""Stable wrapper for creating Dialpad SMS approval drafts."""

from __future__ import annotations

import argparse
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

from sms_approval import RISK_NORMAL, RISK_RISKY, create_draft, create_replacement_draft, init_db


def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="Create a Dialpad SMS approval draft")
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


def resolve_message_text(args: argparse.Namespace) -> tuple[str, str]:
    if args.message is not None:
        return args.message, "--message"
    if args.message_file:
        try:
            return Path(args.message_file).read_text(encoding="utf-8"), "--message-file"
        except (OSError, UnicodeDecodeError) as exc:
            raise WrapperError(
                f"Failed to read --message-file '{args.message_file}': {exc}",
                code="invalid_argument",
                retryable=False,
            ) from exc
    message = sys.stdin.read()
    if not message:
        raise WrapperError("Message text from --message-stdin is empty.", code="invalid_argument", retryable=False)
    return message, "--message-stdin"


def main() -> int:
    json_mode = "--json" in sys.argv
    command = COMMAND_IDS["create_sms_draft.create"]
    wrapper = "create_sms_draft.py"

    try:
        args = build_parser().parse_args()
        json_mode = args.json
        message, message_source = resolve_message_text(args)
        conn = init_db()
        try:
            draft_args = {
                "thread_key": args.thread_key,
                "customer_number": args.customer_number,
                "sender_number": args.sender_number,
                "draft_text": message,
                "source_inbound_id": args.source_inbound_id,
                "risk_state": args.risk_state,
                "risk_reason": args.risk_reason,
                "context_fingerprint": args.context_fingerprint,
                "metadata": {"message_source": message_source},
            }
            if args.keep_pending:
                draft = create_draft(conn, **draft_args)
            else:
                draft = create_replacement_draft(
                    conn,
                    invalidate_thread_key=args.thread_key,
                    invalidate_customer_number=args.customer_number,
                    **draft_args,
                )
        finally:
            conn.close()

        if json_mode:
            emit_success(command, wrapper, {"draft": draft, "message_source": message_source})
        else:
            print(f"Draft created: {draft['draft_id']}")
        return 0
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
