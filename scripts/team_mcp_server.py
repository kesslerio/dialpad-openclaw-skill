#!/usr/bin/env python3
"""Team MCP server for Dialpad texting (team-send-gate Phase 3).

A loopback-only, streamable-HTTP MCP endpoint on the deploy host. Each
teammate's assistant connects like any other remote MCP connector: a
Cloudflare tunnel route plus an Access app (Managed OAuth, Rippling IdP)
fronts the hostname, and this server independently verifies the
``Cf-Access-Jwt-Assertion`` JWT — RS256 checked with a stdlib verifier
against the team-domain JWKS — and maps its ``email`` claim to a send-gate
caller through ``DIALPAD_TEAM_MCP_CONFIG``. The send-api bearer token map
(``DIALPAD_SEND_API_TOKENS``) stays as a fallback for non-Access clients.

Identity never comes from the request body or tool arguments: every ``/mcp``
request resolves a caller first, and every tool scopes itself to the
caller's send-gate lines — its shared lines plus its ``acts_for`` teammate
line — with the support line removed everywhere.

Tools: ``send_sms`` (the same gated ``bin/send_sms.py`` path the send API
runs), ``list_conversations`` and ``read_thread`` against the read-only SMS
SQLite store, and ``set_sms_alert_url`` (registers the webhook that
``scripts/team_alerts.py`` posts inbound-SMS notices to).

Stdlib only: the host Python carries no third-party packages.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import interaction_log
import outbound_destination_policy
import send_api_server
import send_gate
import sms_approval
import sms_sqlite
import team_alerts

CONFIG_ENV = "DIALPAD_TEAM_MCP_CONFIG"
BIND_ENV = "DIALPAD_TEAM_MCP_BIND"
PORT_ENV = "DIALPAD_TEAM_MCP_PORT"
TOKENS_ENV = send_api_server.TOKENS_ENV
SMS_DB_ENV = "DIALPAD_SMS_DB"

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 18890
MAX_BODY_BYTES = 32 * 1024
MAX_MESSAGE_CHARS = send_api_server.MAX_MESSAGE_CHARS
MAX_KEY_CHARS = team_alerts.MAX_KEY_CHARS
PREVIEW_CHARS = 120

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
SERVER_INFO = {"name": "dialpad-team-mcp", "version": "1.0.0"}

JWT_LEEWAY_SECONDS = 60
JWKS_TTL_SECONDS = 3600
JWKS_REFETCH_MIN_SECONDS = 60
JWKS_TIMEOUT_SECONDS = 5
_SHA256_DER_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")

_TEAM_DOMAIN_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")

RPC_PARSE_ERROR = -32700
RPC_INVALID_REQUEST = -32600
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
RPC_INTERNAL_ERROR = -32603


class TeamMcpConfigError(Exception):
    """Startup configuration is missing or invalid; the service refuses."""


class AuthError(Exception):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class ToolError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.extra = dict(extra or {})

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": str(self), "retryable": self.retryable}
        error.update(self.extra)
        return {"error": error}


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# File-backed JSON config (mtime reload, fail closed)
# --------------------------------------------------------------------------


class _StaticSource:
    def __init__(self, value: Any) -> None:
        self._value = value

    def current(self) -> Any:
        return self._value


class _ReloadableJson:
    """A JSON file re-parsed when its mtime (or a linked mtime) changes.

    ``current()`` returns the parsed value or None; None means the file is
    missing, unreadable, or invalid and every caller must fail closed. A
    failed parse is retried when either mtime changes.
    """

    def __init__(self, path: str | Path, parse: Any, *, extra_mtime: Any = None) -> None:
        self.path = Path(path).expanduser()
        self._parse = parse
        self._extra_mtime = extra_mtime
        self._lock = threading.Lock()
        self._cached: Any = None
        self._state: tuple[int | None, int | None] | None = None

    def _mtime(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def current(self) -> Any:
        state = (self._mtime(), self._extra_mtime() if self._extra_mtime else None)
        with self._lock:
            if self._state == state:
                return self._cached
            if state[0] is None:
                value = None
            else:
                try:
                    value = self._parse(self.path)
                except Exception:  # noqa: BLE001 - any load failure fails closed.
                    value = None
            self._cached = value
            self._state = state
            return value


# --------------------------------------------------------------------------
# MCP config
# --------------------------------------------------------------------------


def _normalize_origin(origin: str) -> str:
    """Canonical ``scheme://host[:port]`` form for Origin comparison."""
    parsed = urllib.parse.urlsplit(origin.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"invalid origin {origin!r}")
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{host}{port}"


def validate_mcp_config(raw: Any) -> dict[str, Any]:
    """Fail closed on a malformed config: any invalid shape refuses all requests."""
    if not isinstance(raw, dict):
        raise TeamMcpConfigError("team MCP config: top level must be a JSON object")
    config: dict[str, Any] = {}
    access = raw.get("access")
    if access is not None:
        if not isinstance(access, dict):
            raise TeamMcpConfigError("team MCP config: 'access' must be an object")
        team_domain = access.get("team_domain")
        aud = access.get("aud")
        if not isinstance(team_domain, str) or not _TEAM_DOMAIN_RE.fullmatch(team_domain.strip().lower()):
            raise TeamMcpConfigError("team MCP config: access.team_domain must be a hostname")
        team_domain = team_domain.strip().lower()
        if not team_domain.endswith(".cloudflareaccess.com"):
            raise TeamMcpConfigError("team MCP config: access.team_domain must be a *.cloudflareaccess.com host")
        if not isinstance(aud, str) or not aud.strip():
            raise TeamMcpConfigError("team MCP config: access.aud must be a non-empty string")
        config["access"] = {"team_domain": team_domain, "aud": aud.strip()}
    email_callers = raw.get("email_callers", {})
    if not isinstance(email_callers, dict):
        raise TeamMcpConfigError("team MCP config: 'email_callers' must be an object")
    normalized_emails: dict[str, str] = {}
    for email, caller in email_callers.items():
        if not isinstance(email, str) or "@" not in email:
            raise TeamMcpConfigError("team MCP config: email_callers keys must be email addresses")
        if not isinstance(caller, str) or not caller.strip():
            raise TeamMcpConfigError("team MCP config: email_callers values must be caller ids")
        normalized_emails[email.strip().lower()] = caller.strip()
    config["email_callers"] = normalized_emails
    fallback = raw.get("allow_bearer_fallback", True)
    if not isinstance(fallback, bool):
        raise TeamMcpConfigError("team MCP config: 'allow_bearer_fallback' must be a boolean")
    config["allow_bearer_fallback"] = fallback
    origins = raw.get("allowed_origins", [])
    if not isinstance(origins, list) or any(not isinstance(o, str) or not o.strip() for o in origins):
        raise TeamMcpConfigError("team MCP config: 'allowed_origins' must be a list of origin strings")
    try:
        config["allowed_origins"] = [_normalize_origin(origin) for origin in origins]
    except ValueError as exc:
        raise TeamMcpConfigError(f"team MCP config: allowed_origins: {exc}") from exc
    alerts = raw.get("alerts")
    if alerts is not None and not isinstance(alerts, dict):
        raise TeamMcpConfigError("team MCP config: 'alerts' must be an object")
    config["alerts"] = alerts
    return config


def _load_config_file(path: str | Path) -> dict[str, Any]:
    return validate_mcp_config(team_alerts.read_private_json(path))


# --------------------------------------------------------------------------
# Cloudflare Access JWT verification (stdlib RS256)
# --------------------------------------------------------------------------


def _b64url_decode(segment: str) -> bytes:
    if not _B64URL_RE.fullmatch(segment):
        raise ValueError("invalid base64url segment")
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _verify_rs256(signing_input: bytes, signature: bytes, jwk: Mapping[str, Any]) -> bool:
    """RSASSA-PKCS1-v1_5 SHA-256 check: rebuild the full expected encoded
    block and compare it whole — the decrypted block is never parsed."""
    if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
        return False
    try:
        n = int.from_bytes(_b64url_decode(str(jwk["n"])), "big")
        e = int.from_bytes(_b64url_decode(str(jwk["e"])), "big")
    except (KeyError, ValueError, TypeError):
        return False
    k = (n.bit_length() + 7) // 8
    if not n or not e or len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    digest_info_len = len(_SHA256_DER_PREFIX) + 32
    if k < digest_info_len + 11:  # 0x00 0x01 PS(>=8) 0x00 T
        return False
    expected = (
        b"\x00\x01"
        + b"\xff" * (k - digest_info_len - 3)
        + b"\x00"
        + _SHA256_DER_PREFIX
        + hashlib.sha256(signing_input).digest()
    )
    return hmac.compare_digest(em, expected)


def _default_jwks_fetch(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(request, timeout=JWKS_TIMEOUT_SECONDS) as response:
        document = json.loads(response.read().decode("utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise ValueError("JWKS document has no keys list")
    return document


class _JwksCache:
    """Kid-indexed JWKS for one Access team domain, TTL-cached.

    Refetches when the cache is stale or an unknown kid arrives — the
    unknown-kid path is throttled to one fetch per JWKS_REFETCH_MIN_SECONDS.
    """

    def __init__(self, team_domain: str, *, fetcher: Any = None, now: Any = time.time) -> None:
        self.url = f"https://{team_domain}/cdn-cgi/access/certs"
        self._fetcher = fetcher or _default_jwks_fetch
        self._now = now
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at = 0.0
        self._last_attempt = 0.0
        self._lock = threading.Lock()

    def key_for(self, kid: str) -> dict[str, Any] | None:
        with self._lock:
            now = self._now()
            fresh = bool(self._keys) and now - self._fetched_at < JWKS_TTL_SECONDS
            if (not fresh or kid not in self._keys) and (
                not self._keys or now - self._last_attempt >= JWKS_REFETCH_MIN_SECONDS
            ):
                self._last_attempt = now
                self._reload()
            return self._keys.get(kid)

    def _reload(self) -> None:
        document = self._fetcher(self.url)
        keys = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(keys, list):
            raise ValueError("JWKS document has no keys list")
        self._keys = {
            key["kid"]: key
            for key in keys
            if isinstance(key, dict) and isinstance(key.get("kid"), str)
        }
        self._fetched_at = self._now()


def _reject(message: str) -> AuthError:
    return AuthError("auth_invalid", 401, message)


def verify_access_jwt(
    token: str,
    *,
    access: Mapping[str, Any],
    jwks: _JwksCache,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify a Cf-Access-Jwt-Assertion and return its claims.

    RS256 only, signature checked against the team-domain JWKS, then
    iss/aud/exp/nbf/iat with a small leeway. Any failure raises AuthError
    401; there is no fallback.
    """
    now = time.time() if now is None else now
    parts = token.split(".")
    if len(parts) != 3:
        raise _reject("access jwt must have three segments")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    except (ValueError, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise _reject("access jwt is not well formed") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise _reject("access jwt is not well formed")
    if header.get("alg") != "RS256":
        raise _reject("access jwt must use RS256")
    kid = header.get("kid")
    if not isinstance(kid, str):
        raise _reject("access jwt has no kid")
    try:
        jwk = jwks.key_for(kid)
    except Exception as exc:  # noqa: BLE001 - an unreachable JWKS fails closed.
        raise _reject("access jwks is unavailable") from exc
    if jwk is None or not _verify_rs256(signing_input, signature, jwk):
        raise _reject("access jwt signature is invalid")
    if claims.get("iss") != f"https://{access['team_domain']}":
        raise _reject("access jwt issuer is invalid")
    aud_claim = claims.get("aud")
    auds = [aud_claim] if isinstance(aud_claim, str) else aud_claim
    if not isinstance(auds, list) or access["aud"] not in auds:
        raise _reject("access jwt audience is invalid")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or now >= exp + JWT_LEEWAY_SECONDS:
        raise _reject("access jwt is expired")
    for claim_name in ("nbf", "iat"):
        value = claims.get(claim_name)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool) or now < value - JWT_LEEWAY_SECONDS
        ):
            raise _reject(f"access jwt {claim_name} is invalid")
    return claims


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

_OUR_LINE_EXPR = "CASE WHEN direction = 'inbound' THEN to_number ELSE from_number END"

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "send_sms",
        "description": (
            "Send an SMS from one of your Dialpad lines (your own line by default) through the team "
            "send gate. The gate enforces allowed lines, opt-out (STOP) lists, and the 08:00-21:00 "
            "recipient-local send window; a refusal returns error details instead of sending."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "to": {"type": "string", "description": "Recipient as +1 E.164, e.g. +14155550100."},
                "message": {"type": "string", "description": "SMS body, at most 1600 characters."},
                "from": {
                    "type": "string",
                    "description": "From line as +1 E.164; defaults to your own line. Must be one of your allowed lines.",
                },
                "dry_run": {"type": "boolean", "description": "Evaluate the gate without sending."},
            },
            "required": ["to", "message"],
        },
    },
    {
        "name": "list_conversations",
        "description": (
            "List recent SMS conversations on your allowed lines (your own line plus the shared lines "
            "granted to you). Grouped by line and contact with last-activity time, direction, a short "
            "preview, and unread count."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer", "description": "Max conversations (default 20, max 100)."},
                "since": {
                    "type": ["string", "integer"],
                    "description": "Only activity at or after this time: ISO-8601 string or epoch ms.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "read_thread",
        "description": "Read the messages with one contact on your allowed lines.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "contact": {"type": "string", "description": "Contact number; national or E.164 forms accepted."},
                "limit": {"type": "integer", "description": "Max messages, most recent (default 50, max 200)."},
                "line": {"type": "string", "description": "Restrict to one of your allowed lines (+1 E.164)."},
            },
            "required": ["contact"],
        },
    },
    {
        "name": "set_sms_alert_url",
        "description": (
            "Register the webhook that receives a minimal notice when an SMS lands on your own line "
            "(plus any shared lines configured for you), or clear it with an empty url. Notices carry "
            "ids and the line only; call read_thread for content."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "url": {"type": "string", "description": "https webhook URL (http only for loopback); empty clears."},
                "key": {"type": "string", "description": "Bearer key sent with each alert POST."},
            },
            "required": ["url", "key"],
        },
    },
]

TOOL_HANDLERS: dict[str, Any] = {}


def _tool(name: str):
    def register(func):
        TOOL_HANDLERS[name] = func
        return func

    return register


def _check_args(args: Any, *, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ToolError("invalid_argument", "arguments must be an object")
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ToolError("invalid_argument", f"unknown argument(s): {', '.join(unknown)}")
    missing = sorted(required - set(args))
    if missing:
        raise ToolError("invalid_argument", f"missing required argument(s): {', '.join(missing)}")
    return args


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bounded_limit(value: Any, *, default: int, maximum: int, name: str = "limit") -> int:
    if value is None:
        return default
    if not _is_int(value) or not 1 <= value <= maximum:
        raise ToolError("invalid_argument", f"{name} must be an integer between 1 and {maximum}")
    return value


def _normalized_key(number: Any) -> str | None:
    return sms_approval.normalize_phone_number(number)


def _parse_since(value: Any) -> int | None:
    """`since` as epoch ms: ISO-8601 string or epoch number (ms, or s when small)."""
    if value is None:
        return None
    number: float | None = None
    if _is_int(value) or isinstance(value, float):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            number = float(text)
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ToolError("invalid_argument", "since must be an ISO-8601 string or epoch ms") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            number = parsed.timestamp() * 1000
    if number is None or number <= 0:
        raise ToolError("invalid_argument", "since must be an ISO-8601 string or epoch ms")
    # Dialpad stores epoch ms; accept epoch seconds for hand-typed values.
    if number < 100_000_000_000:
        number *= 1000
    return int(number)


def _iso_from_ms(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


@_tool("send_sms")
def _tool_send_sms(server: "TeamMcpServer", caller: str, args: dict[str, Any]) -> Any:
    args = _check_args(args, allowed={"to", "message", "from", "dry_run"}, required={"to", "message"})
    to = args["to"]
    if not isinstance(to, str) or not outbound_destination_policy.is_supported_outbound_destination(to.strip()):
        raise ToolError("invalid_argument", "to must be a single +1 E.164 number")
    to = to.strip()
    from_arg = args.get("from")
    if from_arg is None:
        from_number = server.own_line_for(caller)
        if not from_number:
            raise ToolError("invalid_argument", "caller has no own line to send from")
    else:
        if not isinstance(from_arg, str) or not outbound_destination_policy.is_supported_outbound_destination(from_arg.strip()):
            raise ToolError("invalid_argument", "from must be a +1 E.164 number")
        from_number = from_arg.strip()
    message = args["message"]
    if not isinstance(message, str) or not message.strip():
        raise ToolError("invalid_argument", "message must be a non-empty string")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ToolError("invalid_argument", f"message must be at most {MAX_MESSAGE_CHARS} characters")
    dry_run = args.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ToolError("invalid_argument", "dry_run must be a boolean")
    if send_api_server._is_support_line(to) or send_api_server._is_support_line(from_number):
        raise ToolError("support_line_refused", "the support line cannot be a send endpoint")
    _status, payload = send_api_server.execute_gated_send(
        to=to,
        from_number=from_number,
        message=message,
        caller=caller,
        dry_run=dry_run,
        environ=server.environ,
        send_runner=server.send_runner,
        command="team_mcp.send_sms",
    )
    if payload.get("ok"):
        return payload.get("data")
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    raise ToolError(
        str(error.get("code") or "upstream_error"),
        str(error.get("message") or "send failed"),
        retryable=bool(error.get("retryable")),
        extra={key: error[key] for key in ("reasons", "retry_after_seconds", "send_gate") if key in error},
    )


@_tool("list_conversations")
def _tool_list_conversations(server: "TeamMcpServer", caller: str, args: dict[str, Any]) -> Any:
    args = _check_args(args, allowed={"limit", "since"}, required=set())
    limit = _bounded_limit(args.get("limit"), default=20, maximum=100)
    since_ms = _parse_since(args.get("since"))
    lines = server.allowed_lines(caller)
    if not lines:
        return {"conversations": [], "lines": []}
    lookup = {_normalized_key(line): line for line in lines}
    params: list[Any] = []
    where = "contact_number IS NOT NULL AND contact_number != ''"
    if since_ms is not None:
        where += " AND timestamp >= ?"
        params.append(since_ms)
    conn = server.ro_db()
    try:
        groups = conn.execute(
            f"SELECT {_OUR_LINE_EXPR} AS our_line, contact_number, "
            "MAX(timestamp) AS last_ts, "
            "SUM(CASE WHEN read = 0 AND direction = 'inbound' THEN 1 ELSE 0 END) AS unread_count "
            f"FROM messages WHERE {where} "
            "GROUP BY our_line, contact_number ORDER BY last_ts DESC",
            params,
        ).fetchall()
        conversations: list[dict[str, Any]] = []
        for group in groups:
            canonical = lookup.get(_normalized_key(group["our_line"]))
            if canonical is None:
                continue
            probe_params: list[Any] = [group["contact_number"], group["our_line"]]
            probe_where = "contact_number = ? AND " + _OUR_LINE_EXPR + " = ?"
            if since_ms is not None:
                probe_where += " AND timestamp >= ?"
                probe_params.append(since_ms)
            latest = conn.execute(
                f"SELECT direction, text, contact_name FROM messages WHERE {probe_where} "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                probe_params,
            ).fetchone()
            preview = None
            direction = None
            contact_name = None
            if latest is not None:
                direction = latest["direction"]
                contact_name = latest["contact_name"]
                kept = sms_sqlite.filter_messages([dict(latest)])
                if kept:
                    preview = sms_sqlite.redact_preview(
                        kept[0].get("text") or "",
                        sender=contact_name or "",
                        contact_number=group["contact_number"],
                    )[:PREVIEW_CHARS]
            conversations.append(
                {
                    "line": canonical,
                    "contact": group["contact_number"],
                    "contact_name": contact_name,
                    "last_message_at_ms": group["last_ts"],
                    "last_message_at": _iso_from_ms(group["last_ts"]),
                    "direction": direction,
                    "preview": preview,
                    "unread_count": int(group["unread_count"] or 0),
                }
            )
            if len(conversations) >= limit:
                break
    finally:
        conn.close()
    return {"conversations": conversations, "lines": sorted(lines)}


@_tool("read_thread")
def _tool_read_thread(server: "TeamMcpServer", caller: str, args: dict[str, Any]) -> Any:
    args = _check_args(args, allowed={"contact", "limit", "line"}, required={"contact"})
    contact_raw = args["contact"]
    if not isinstance(contact_raw, str) or not contact_raw.strip():
        raise ToolError("invalid_argument", "contact must be a non-empty string")
    contact_raw = contact_raw.strip()
    contact = send_gate._canonical_sender(contact_raw)
    limit = _bounded_limit(args.get("limit"), default=50, maximum=200)
    lines = server.allowed_lines(caller)
    line_arg = args.get("line")
    narrowed: str | None = None
    if line_arg is not None:
        if not isinstance(line_arg, str) or not line_arg.strip():
            raise ToolError("invalid_argument", "line must be a +1 E.164 string")
        narrowed = send_gate._canonical_sender(line_arg.strip())
        if narrowed not in lines:
            raise ToolError("line_not_allowed", "line is not one of your allowed lines")
    lookup = {_normalized_key(line): line for line in lines}
    candidates = interaction_log._phone_candidates(contact_raw)
    if contact not in candidates:
        candidates.append(contact)
    placeholders = ", ".join("?" for _ in candidates)
    conn = server.ro_db()
    try:
        distinct = conn.execute(
            f"SELECT DISTINCT {_OUR_LINE_EXPR} AS our_line FROM messages "
            f"WHERE contact_number IN ({placeholders})",
            candidates,
        ).fetchall()
        raw_lines = sorted(
            {
                row["our_line"]
                for row in distinct
                if lookup.get(_normalized_key(row["our_line"])) is not None
                and (narrowed is None or lookup[_normalized_key(row["our_line"])] == narrowed)
            }
        )
        if not raw_lines:
            return {"contact": contact, "lines": [], "messages": []}
        line_params = ", ".join("?" for _ in raw_lines)
        rows = conn.execute(
            f"SELECT id, direction, {_OUR_LINE_EXPR} AS our_line, contact_number, contact_name, "
            "text, message_status, delivery_result, mms, timestamp, read FROM messages "
            f"WHERE contact_number IN ({placeholders}) AND {_OUR_LINE_EXPR} IN ({line_params}) "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            [*candidates, *raw_lines, limit],
        ).fetchall()
    finally:
        conn.close()
    kept = sms_sqlite.filter_messages([dict(row) for row in rows])
    messages = [
        {
            "id": row["id"],
            "line": lookup[_normalized_key(row["our_line"])],
            "direction": row["direction"],
            "timestamp_ms": row["timestamp"],
            "timestamp": _iso_from_ms(row["timestamp"]),
            "contact": row["contact_number"],
            "contact_name": row["contact_name"],
            "text": row["text"],
            "status": row["message_status"],
            "delivery_result": row["delivery_result"],
            "mms": bool(row["mms"]),
            "read": bool(row["read"]),
        }
        for row in kept
        if row.get("our_line") is not None
    ]
    messages.reverse()  # fetched newest-first; return chronological
    return {
        "contact": contact,
        "lines": sorted({message["line"] for message in messages}),
        "messages": messages,
    }


@_tool("set_sms_alert_url")
def _tool_set_sms_alert_url(server: "TeamMcpServer", caller: str, args: dict[str, Any]) -> Any:
    args = _check_args(args, allowed={"url", "key"}, required={"url", "key"})
    url = args["url"]
    key = args["key"]
    if not isinstance(url, str):
        raise ToolError("invalid_argument", "url must be a string")
    if not isinstance(key, str) or (url.strip() and (not key.strip() or len(key) > MAX_KEY_CHARS)):
        raise ToolError(
            "invalid_argument",
            f"key must be a non-empty string of at most {MAX_KEY_CHARS} characters",
        )
    config = server.mcp_config() or {}
    gate_config = server.gate_config()
    try:
        alerts = team_alerts.validate_alerts_section(config.get("alerts"), gate_config=gate_config or {})
    except ValueError as exc:
        raise ToolError("alerts_config_invalid", f"alerts config is invalid: {exc}") from exc
    if alerts is None:
        raise ToolError("alerts_not_configured", "sms alerts are not configured on this server")
    url = url.strip()
    if url:
        try:
            url = team_alerts.validate_alert_url(url, allowed_hosts=alerts["allowed_url_hosts"])
        except ValueError as exc:
            raise ToolError("invalid_argument", str(exc)) from exc
    try:
        team_alerts.set_alert_registration(alerts["store"], caller, url, key.strip())
    except (OSError, ValueError) as exc:
        raise ToolError("upstream_error", "alert registration failed") from exc
    if not url:
        return {"registered": False, "alert_lines": []}
    return {
        "registered": True,
        "url": url,
        "alert_lines": sorted(
            team_alerts.alert_lines_for_caller(caller, gate_config=gate_config or {}, alerts=alerts)
        ),
    }


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------


class TeamMcpRequestHandler(BaseHTTPRequestHandler):
    server: "TeamMcpServer"

    def log_message(self, _format: str, *_args: object) -> None:
        # Suppress access logging; request URLs and bodies can carry phone
        # numbers and message text.
        return

    def _log(self, *, caller: str | None, action: str, outcome: str) -> None:
        sys.stderr.write(f"team_mcp caller={caller or '-'} action={action} outcome={outcome}\n")

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _write_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _refuse(self, error: AuthError, *, action: str = "request") -> None:
        self._write_json(error.status, {"error": {"code": error.code, "message": str(error)}})
        self._log(caller=None, action=action, outcome=error.code)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch("OPTIONS")

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin is None:
            return
        allowed = (self.server.mcp_config() or {}).get("allowed_origins") or []
        try:
            normalized = _normalize_origin(origin)
        except ValueError:
            normalized = ""
        if normalized not in allowed:
            raise AuthError("origin_not_allowed", 403, "origin is not allowed")

    def _resolve_caller(self) -> str:
        jwt = self.headers.get("Cf-Access-Jwt-Assertion")
        if jwt is not None:
            # An Access JWT always takes this path; a failure never falls
            # back to the bearer path.
            return self.server.caller_from_access_jwt(jwt.strip())
        return self.server.caller_from_bearer(self.headers.get("Authorization"))

    def _dispatch(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            self._check_origin()
            caller = self._resolve_caller()
        except AuthError as error:
            self._refuse(error)
            return
        if path == "/health":
            if method != "GET":
                self._write_json(405, {"error": {"code": "method_not_allowed", "message": "method not allowed"}})
                self._log(caller=caller, action="health", outcome="method_not_allowed")
                return
            self._write_json(200, {"caller": caller})
            self._log(caller=caller, action="health", outcome="ok")
            return
        if path != "/mcp":
            self._write_json(404, {"error": {"code": "not_found", "message": "route not found"}})
            self._log(caller=caller, action="route", outcome="not_found")
            return
        if method != "POST":
            self._write_json(405, {"error": {"code": "method_not_allowed", "message": "method not allowed"}})
            self._log(caller=caller, action="mcp", outcome="method_not_allowed")
            return
        self._handle_mcp(caller)

    def _read_body(self) -> bytes:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise AuthError("invalid_argument", 400, "Content-Type must be application/json")
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise AuthError("invalid_argument", 400, "Content-Length must be an integer") from exc
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            raise AuthError("request_too_large", 413, "request body is too large")
        return self.rfile.read(content_length)

    def _rpc_result(self, request_id: Any, *, result: Any = None, error: tuple[int, str] | None = None) -> None:
        if error is not None:
            code, message = error
            payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        else:
            payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
        self._write_json(200, payload)

    def _handle_mcp(self, caller: str) -> None:
        try:
            raw = self._read_body()
        except AuthError as error:
            self._refuse(error, action="mcp")
            return
        try:
            request = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            self._rpc_result(None, error=(RPC_PARSE_ERROR, "parse error"))
            return
        if isinstance(request, list):
            self._rpc_result(None, error=(RPC_INVALID_REQUEST, "batch requests are not supported"))
            return
        if (
            not isinstance(request, dict)
            or request.get("jsonrpc") != "2.0"
            or not isinstance(request.get("method"), str)
        ):
            self._rpc_result(None, error=(RPC_INVALID_REQUEST, "invalid request"))
            return
        method = request["method"]
        if method.startswith("notifications/"):
            self._write_empty(202)
            return
        request_id = request.get("id")
        is_notification = "id" not in request
        try:
            result = self._rpc_dispatch(caller, method, request.get("params"))
        except _RpcError as error:
            if is_notification:
                self._write_empty(202)
            else:
                self._rpc_result(request_id, error=(error.code, str(error)))
            return
        except Exception:  # noqa: BLE001 - protocol errors must not leak internals.
            if is_notification:
                self._write_empty(202)
            else:
                self._rpc_result(request_id, error=(RPC_INTERNAL_ERROR, "internal error"))
            return
        if is_notification:
            self._write_empty(202)
        else:
            self._rpc_result(request_id, result=result)

    def _rpc_dispatch(self, caller: str, method: str, params: Any) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": dict(SERVER_INFO),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOL_DEFINITIONS}
        if method == "tools/call":
            if not isinstance(params, dict):
                raise _RpcError(RPC_INVALID_PARAMS, "tools/call params must be an object")
            name = params.get("name")
            if not isinstance(name, str) or name not in TOOL_HANDLERS:
                raise _RpcError(RPC_INVALID_PARAMS, "unknown tool")
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                raise _RpcError(RPC_INVALID_PARAMS, "tool arguments must be an object")
            handler = TOOL_HANDLERS[name]
            try:
                data = handler(self.server, caller, arguments)
                outcome = "ok"
                is_error = False
            except ToolError as error:
                data = error.payload()
                outcome = error.code
                is_error = True
            except Exception:  # noqa: BLE001 - a tool crash must not leak internals.
                data = {"error": {"code": "internal_error", "message": "tool failed", "retryable": False}}
                outcome = "internal_error"
                is_error = True
            self._log(caller=caller, action=f"tool:{name}", outcome=outcome)
            return {
                "content": [{"type": "text", "text": json.dumps(data, separators=(",", ":"))}],
                "isError": is_error,
            }
        raise _RpcError(RPC_METHOD_NOT_FOUND, "method not found")


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


class TeamMcpServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        config: Mapping[str, Any] | None = None,
        config_path: str | Path | None = None,
        gate_config: Mapping[str, Any] | None = None,
        tokens_path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        send_runner: Any = None,
        jwks_fetcher: Any = None,
    ) -> None:
        self.environ = os.environ if environ is None else environ
        self.send_runner = send_runner or send_api_server.default_send_runner
        if config is not None:
            self._config: Any = _StaticSource(validate_mcp_config(config))
        else:
            path = config_path or (self.environ.get(CONFIG_ENV) or "").strip()
            if not path:
                raise TeamMcpConfigError(f"{CONFIG_ENV} is not set")
            self._config = _ReloadableJson(path, _load_config_file)
        if gate_config is not None:
            self._gate: Any = _StaticSource(send_gate.validate_config(dict(gate_config)))
        else:
            gate_path = (self.environ.get(send_gate.CONFIG_ENV) or "").strip()
            if not gate_path:
                raise TeamMcpConfigError(f"{send_gate.CONFIG_ENV} is not set")
            self._gate = _ReloadableJson(gate_path, send_gate.load_config)
        if self.mcp_config() is None:
            raise TeamMcpConfigError(f"team MCP config unreadable at {self._config.path}")
        if self.gate_config() is None:
            raise TeamMcpConfigError("send gate config unavailable")
        resolved_tokens = tokens_path or (self.environ.get(TOKENS_ENV) or "").strip()
        self._tokens: _ReloadableJson | None = None
        if resolved_tokens:
            # Built whenever a map is reachable so flipping
            # allow_bearer_fallback on via a config reload needs no restart.
            self._tokens = _ReloadableJson(
                resolved_tokens,
                lambda path: send_api_server.load_token_map(path, gate_config=self.gate_config()),
                extra_mtime=self._gate_mtime,
            )
        elif self.bearer_fallback_allowed():
            raise TeamMcpConfigError(f"{TOKENS_ENV} is not set")
        raw_db = (self.environ.get(SMS_DB_ENV) or "").strip()
        self.db_path = Path(raw_db).expanduser() if raw_db else sms_sqlite.resolve_db_path()
        self._jwks_fetcher = jwks_fetcher
        self._jwks_caches: dict[str, _JwksCache] = {}
        self._jwks_lock = threading.Lock()
        super().__init__(address, TeamMcpRequestHandler)

    # -- configuration ----------------------------------------------------

    def mcp_config(self) -> dict[str, Any] | None:
        return self._config.current()

    def gate_config(self) -> dict[str, Any] | None:
        return self._gate.current()

    def _gate_mtime(self) -> int | None:
        gate = self._gate
        if not isinstance(gate, _ReloadableJson):
            return None
        try:
            return gate.path.stat().st_mtime_ns
        except OSError:
            return None

    def bearer_fallback_allowed(self) -> bool:
        config = self.mcp_config()
        return bool(config and config.get("allow_bearer_fallback", True))

    # -- identity ----------------------------------------------------------

    def _jwks(self, team_domain: str) -> _JwksCache:
        with self._jwks_lock:
            cache = self._jwks_caches.get(team_domain)
            if cache is None:
                cache = _JwksCache(team_domain, fetcher=self._jwks_fetcher)
                self._jwks_caches[team_domain] = cache
            return cache

    def is_network_mappable(self, caller: str) -> bool:
        """Only team-* callers with an acts_for line can be used from the
        network; operator and other callers never map to a request."""
        gate_config = self.gate_config() or {}
        entry = (gate_config.get("callers") or {}).get(caller)
        return (
            isinstance(entry, dict)
            and caller.startswith("team-")
            and bool(entry.get("acts_for"))
        )

    def caller_from_access_jwt(self, token: str) -> str:
        config = self.mcp_config() or {}
        access = config.get("access")
        if not access:
            raise AuthError("access_not_configured", 401, "access jwt is not configured")
        claims = verify_access_jwt(token, access=access, jwks=self._jwks(access["team_domain"]))
        email = claims.get("email")
        caller = (config.get("email_callers") or {}).get(
            email.strip().lower() if isinstance(email, str) else ""
        )
        if not caller or not self.is_network_mappable(caller):
            raise AuthError("email_not_mapped", 403, "email is not mapped to a caller")
        return caller

    def caller_from_bearer(self, authorization: str | None) -> str:
        missing = AuthError("auth_missing", 401, "valid bearer authorization is required")
        if not self.bearer_fallback_allowed():
            raise missing
        scheme, separator, token = (authorization or "").partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            raise missing
        tokens = self._tokens.current() if self._tokens is not None else None
        entry = (tokens or {}).get(hashlib.sha256(token.strip().encode("utf-8")).hexdigest())
        if entry is None:
            raise missing
        caller = entry["caller"]
        if not self.is_network_mappable(caller):
            raise AuthError("caller_not_mappable", 403, "caller cannot be used from the network")
        return caller

    # -- line scope ---------------------------------------------------------

    def allowed_lines(self, caller: str) -> set[str]:
        gate_config = self.gate_config()
        if not gate_config:
            return set()
        return {
            line
            for line in send_gate.allowed_lines_for_caller(gate_config, caller)
            if not send_api_server._is_support_line(line)
        }

    def own_line_for(self, caller: str) -> str | None:
        gate_config = self.gate_config() or {}
        entry = (gate_config.get("callers") or {}).get(caller) or {}
        acts_for = entry.get("acts_for")
        own = (gate_config.get("teammates") or {}).get(acts_for) if isinstance(acts_for, str) else None
        return send_gate._canonical_sender(own) if own else None

    def ro_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn


def validate_bind_address(bind: str) -> str:
    """The team MCP server is loopback-only; anything else refuses to start."""
    value = str(bind or "").strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"{BIND_ENV} must be a literal IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_loopback:
        raise ValueError(f"{BIND_ENV} must be an IPv4 loopback address (127.0.0.0/8)")
    return value


def create_server(
    bind: str | None = None,
    port: int | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
    gate_config: Mapping[str, Any] | None = None,
    tokens_path: str | Path | None = None,
    send_runner: Any = None,
    jwks_fetcher: Any = None,
    env: Mapping[str, str] | None = None,
) -> TeamMcpServer:
    environ = os.environ if env is None else env
    resolved_bind = validate_bind_address(bind if bind is not None else environ.get(BIND_ENV, DEFAULT_BIND))
    resolved_port = int(port if port is not None else environ.get(PORT_ENV, str(DEFAULT_PORT)))
    if not 0 <= resolved_port <= 65535:
        raise ValueError(f"{PORT_ENV} must be between 0 and 65535")
    return TeamMcpServer(
        (resolved_bind, resolved_port),
        config=config,
        config_path=config_path,
        gate_config=gate_config,
        tokens_path=tokens_path,
        environ=environ,
        send_runner=send_runner,
        jwks_fetcher=jwks_fetcher,
    )


def main() -> int:
    try:
        server = create_server()
    except (TeamMcpConfigError, ValueError, OSError) as error:
        print(f"team MCP configuration error: {error}", file=sys.stderr)
        return 1
    print(f"Dialpad team MCP listening on {server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
