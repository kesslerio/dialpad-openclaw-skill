#!/usr/bin/env python3
"""Authenticated, Tailscale-only HTTP API for the canonical interaction log."""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from interaction_log import InteractionLog


DEFAULT_BIND = "100.85.254.62"
DEFAULT_PORT = 18887
MAX_BODY_BYTES = 64 * 1024
MAX_LIMIT = 500
COMMANDS = {
    "/health": "interaction_log.health",
    "/v1/sms/thread": "list_sms_thread.list",
    "/v1/sms/inbox": "list_sms_inbox.list",
    "/v1/calls": "list_calls.list",
    "/v1/sms/record": "interaction_log.record_message",
}


class LogApiError(Exception):
    def __init__(self, message: str, *, status: int = 500, code: str = "internal_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _error_payload(command: str, code: str, message: str) -> dict[str, object]:
    return {
        "ok": False,
        "command": command,
        "error": {"code": code, "message": message, "retryable": code in {"network_error", "timeout"}},
        "meta": {"schema_version": "1", "service": "dialpad-log-api"},
    }


def _success_payload(command: str, data: dict[str, object]) -> dict[str, object]:
    return {
        "ok": True,
        "command": command,
        "data": data,
        "meta": {"schema_version": "1", "service": "dialpad-log-api"},
    }


def _query_value(query: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    values = query.get(key)
    if not values:
        return default
    value = values[0].strip()
    return value if value else default


def _positive_int(value: str | None, *, name: str, default: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LogApiError(f"{name} must be an integer", status=400, code="invalid_argument") from exc
    if parsed <= 0 or parsed > maximum:
        raise LogApiError(f"{name} must be between 1 and {maximum}", status=400, code="invalid_argument")
    return parsed


def _bool_query(value: str | None, *, name: str) -> bool:
    if value is None:
        return False
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise LogApiError(f"{name} must be a boolean", status=400, code="invalid_argument")


def validate_bind_address(bind: str) -> str:
    """Reject wildcard binds; production defaults to the host's Tailscale IPv4."""
    value = str(bind or "").strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("DIALPAD_LOG_BIND must be a literal IPv4 address") from exc
    if address.version != 4 or address.is_unspecified:
        raise ValueError("DIALPAD_LOG_BIND must be a non-wildcard Tailscale IPv4 address")
    return value


class LogApiRequestHandler(BaseHTTPRequestHandler):
    server: "LogApiServer"

    def log_message(self, _format: str, *_args: object) -> None:
        # Do not emit request URLs or bodies: query strings and payloads can
        # contain phone numbers and message text.
        return

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, separator, token = header.partition(" ")
        return bool(
            separator
            and scheme.lower() == "bearer"
            and token
            and hmac.compare_digest(token, self.server.log_token)
        )

    def _write_json(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _fail(self, error: LogApiError, command: str) -> None:
        self._write_json(error.status, _error_payload(command, error.code, str(error)))

    def _route_command(self, path: str) -> str:
        return COMMANDS.get(path, "interaction_log.unknown")

    def _read_body(self) -> dict[str, object]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise LogApiError("Content-Length must be an integer", status=400, code="invalid_argument") from exc
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            raise LogApiError("request body is too large", status=413, code="invalid_argument")
        raw = self.rfile.read(content_length)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LogApiError("request body must be valid JSON", status=400, code="invalid_argument") from exc
        if not isinstance(payload, dict):
            raise LogApiError("request body must be a JSON object", status=400, code="invalid_argument")
        return payload

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        command = self._route_command(path)
        if not self._authorized():
            self._write_json(401, _error_payload(command, "auth_missing", "valid bearer authorization is required"))
            return
        try:
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            if path == "/health":
                data = {"status": "ok", "service": "dialpad-log-api"}
            elif path == "/v1/sms/thread":
                phone = _query_value(query, "phone")
                if not phone:
                    raise LogApiError("phone is required", status=400, code="invalid_argument")
                limit = _positive_int(_query_value(query, "limit"), name="limit", default=100, maximum=MAX_LIMIT)
                data = self.server.interaction_log.thread(phone, limit=limit)
            elif path == "/v1/sms/inbox":
                limit = _positive_int(_query_value(query, "limit"), name="limit", default=100, maximum=MAX_LIMIT)
                data = self.server.interaction_log.inbox(
                    limit=limit,
                    unread_only=_bool_query(_query_value(query, "unread_only"), name="unread_only"),
                )
            elif path == "/v1/calls":
                hours_raw = _query_value(query, "hours")
                hours = _positive_int(hours_raw, name="hours", default=24, maximum=24 * 31)
                limit = _positive_int(_query_value(query, "limit"), name="limit", default=100, maximum=MAX_LIMIT)
                data = self.server.interaction_log.calls(
                    hours=hours,
                    missed=_bool_query(_query_value(query, "missed"), name="missed"),
                    with_phone=_query_value(query, "with"),
                    limit=limit,
                )
            else:
                raise LogApiError("route not found", status=404, code="not_found")
            self._write_json(200, _success_payload(command, data))
        except LogApiError as error:
            self._fail(error, command)
        except (OSError, ValueError) as error:
            self._fail(LogApiError("interaction log request failed", status=500), command)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        command = self._route_command(path)
        if not self._authorized():
            self._write_json(401, _error_payload(command, "auth_missing", "valid bearer authorization is required"))
            return
        if path != "/v1/sms/record":
            self._write_json(404, _error_payload(command, "not_found", "route not found"))
            return
        try:
            payload = self._read_body()
            direction = str(payload.get("direction") or "").strip().lower()
            if direction != "outbound":
                raise LogApiError("only outbound message observations may be recorded", status=400, code="invalid_argument")
            body = payload.get("body", payload.get("text"))
            if not isinstance(body, str) or not body.strip():
                raise LogApiError("message observation requires a non-empty body", status=400, code="invalid_argument")
            if not payload.get("from_number", payload.get("from")) or not payload.get("to_number", payload.get("to")):
                raise LogApiError("message observation requires from and to numbers", status=400, code="invalid_argument")
            result = self.server.interaction_log.record_message(payload)
            self._write_json(200, _success_payload(command, result))
        except LogApiError as error:
            self._fail(error, command)
        except (OSError, ValueError) as error:
            self._fail(LogApiError(str(error), status=400, code="invalid_argument"), command)

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        self._write_json(405, _error_payload(self._route_command(urlparse(self.path).path), "invalid_argument", "method not allowed"))

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        self.do_PUT()


class LogApiServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, token: str, interaction_log: InteractionLog) -> None:
        self.log_token = token
        self.interaction_log = interaction_log
        super().__init__(address, LogApiRequestHandler)


def create_server(
    bind: str | None = None,
    port: int | None = None,
    *,
    token: str | None = None,
    interaction_log: InteractionLog | None = None,
) -> LogApiServer:
    resolved_bind = validate_bind_address(bind or os.environ.get("DIALPAD_LOG_BIND", DEFAULT_BIND))
    resolved_port = int(port if port is not None else os.environ.get("DIALPAD_LOG_PORT", str(DEFAULT_PORT)))
    if not 0 <= resolved_port <= 65535:
        raise ValueError("DIALPAD_LOG_PORT must be between 1 and 65535")
    resolved_token = token if token is not None else os.environ.get("DIALPAD_LOG_TOKEN", "")
    if not resolved_token.strip():
        raise RuntimeError("DIALPAD_LOG_TOKEN is required; refusing to start log API")
    return LogApiServer((resolved_bind, resolved_port), token=resolved_token, interaction_log=interaction_log or InteractionLog())


def main() -> int:
    try:
        server = create_server()
    except (RuntimeError, ValueError, OSError) as error:
        print(f"Log API configuration error: {error}")
        return 1
    print(f"Dialpad log API listening on {server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
