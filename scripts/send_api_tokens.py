#!/usr/bin/env python3
"""Operator CLI for the send-api bearer token map.

Adds, lists, and revokes team-assistant tokens in
``DIALPAD_SEND_API_TOKENS``. The map stores SHA-256 digests only; a raw
token is read once from stdin and is never printed, stored, or passed on
argv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import send_api_server
import send_gate

MIN_TOKEN_CHARS = 32


class CliError(Exception):
    """A user-facing CLI failure; printed to stderr with exit 2."""


def _tokens_path() -> Path:
    raw = (os.environ.get(send_api_server.TOKENS_ENV) or "").strip()
    if not raw:
        raise CliError(f"{send_api_server.TOKENS_ENV} is not set")
    return Path(raw).expanduser()


def _read_tokens(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliError(f"token map unreadable at {path}: {exc}") from exc
    entries = raw.get("tokens") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        raise CliError(f"token map at {path} must be a JSON object with a 'tokens' object")
    return dict(entries)


def _gate_callers() -> dict[str, Any]:
    try:
        config = send_gate.load_config_from_env()
    except Exception as exc:  # noqa: BLE001 - any config failure refuses the add.
        raise CliError(f"send gate config unavailable: {exc}") from exc
    callers = config.get("callers") if isinstance(config, dict) else None
    if not isinstance(callers, dict):
        raise CliError("send gate config has no callers")
    return callers


def cmd_add(args: argparse.Namespace) -> int:
    token = sys.stdin.read().strip()
    if len(token) < MIN_TOKEN_CHARS:
        raise CliError(f"token must be at least {MIN_TOKEN_CHARS} characters")
    callers = _gate_callers()
    if args.caller == send_api_server.OPERATOR_CALLER:
        raise CliError("the operator caller cannot be mapped to an API token")
    if args.caller not in callers:
        raise CliError(f"caller {args.caller!r} is not defined in the send gate config")
    path = _tokens_path()
    entries = _read_tokens(path)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    for existing in entries.values():
        if isinstance(existing, dict) and existing.get("label") == args.label:
            raise CliError(f"label {args.label!r} is already in use")
    if digest in entries:
        raise CliError("token is already present in the map")
    entries[digest] = {"caller": args.caller, "label": args.label}
    send_api_server.write_token_map(path, entries)
    print(f"stored token for caller {args.caller!r} as label {args.label!r}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    path = _tokens_path()
    entries = _read_tokens(path)
    match = next(
        (digest for digest, entry in entries.items() if isinstance(entry, dict) and entry.get("label") == args.label),
        None,
    )
    if match is None:
        raise CliError(f"no token with label {args.label!r}")
    del entries[match]
    send_api_server.write_token_map(path, entries)
    print(f"revoked token label {args.label!r}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    entries = _read_tokens(_tokens_path())
    for digest in sorted(entries, key=lambda d: str(entries[d].get("label") if isinstance(entries[d], dict) else "")):
        entry = entries[digest]
        if isinstance(entry, dict):
            print(f"{entry.get('label', '-')}\t{entry.get('caller', '-')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage send-api bearer tokens; the map stores SHA-256 digests only.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="store a token read from stdin")
    add.add_argument("--caller", required=True, help="send-gate caller id this token maps to")
    add.add_argument("--label", required=True, help="unique operator-facing label")
    add.set_defaults(func=cmd_add)
    revoke = sub.add_parser("revoke", help="remove a token by label")
    revoke.add_argument("--label", required=True)
    revoke.set_defaults(func=cmd_revoke)
    list_cmd = sub.add_parser("list", help="print label and caller for each token")
    list_cmd.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print(f"send_api_tokens: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
