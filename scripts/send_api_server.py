#!/usr/bin/env python3
"""Loopback HTTP send service for shared team callers.

Each team assistant authenticates with its own bearer token; the service
hashes it with SHA-256 and maps the digest to a send-gate caller id from the
token map (``DIALPAD_SEND_API_TOKENS``). The caller id comes only from the
map, never from the request. The service then runs the existing gated
``bin/send_sms.py`` path as a subprocess with ``DIALPAD_SEND_CALLER``
scrubbed from the child environment, so only this service holds the Dialpad
API key. It binds to loopback only; a cloudflared tunnel + Cloudflare
Access sit in front on the deploy host.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import outbound_destination_policy
import send_gate
import sms_approval

ROOT = Path(__file__).resolve().parents[1]
SEND_SMS_SCRIPT = ROOT / "bin" / "send_sms.py"

BIND_ENV = "DIALPAD_SEND_API_BIND"
PORT_ENV = "DIALPAD_SEND_API_PORT"
TOKENS_ENV = "DIALPAD_SEND_API_TOKENS"

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 18889
MAX_BODY_BYTES = 16 * 1024
MAX_MESSAGE_CHARS = 1600
SEND_TIMEOUT_SECONDS = 60.0
SUPPORT_LINE = "+14159917155"
OPERATOR_CALLER = "operator"

COMMANDS = {
    "/health": "send_api.health",
    "/v1/sms/send": "send_api.send",
}
UNKNOWN_COMMAND = "send_api.unknown"
SERVICE_META = {"schema_version": "1", "service": "dialpad-send-api"}

ALLOWED_FIELDS = {"to", "from", "message", "dry_run"}
REQUIRED_FIELDS = {"to", "from", "message"}
OUTCOME_UNKNOWN_MESSAGE = "send outcome unknown; check the thread before resending"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SUPPORT_LINE_NORMALIZED = sms_approval.normalize_phone_number(SUPPORT_LINE)


class SendApiConfigError(Exception):
    """Startup or token-map configuration is invalid; the service refuses."""


class SendApiError(Exception):
    def __init__(self, message: str, *, status: int = 500, code: str = "internal_error", retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


# --------------------------------------------------------------------------
# Token map
# --------------------------------------------------------------------------


def validate_token_map(raw: Any, *, gate_config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Return {digest: {"caller", "label"}} or raise SendApiConfigError."""
    if not isinstance(raw, dict) or not isinstance(raw.get("tokens"), dict):
        raise SendApiConfigError("token map must be a JSON object with a 'tokens' object")
    entries = raw["tokens"]
    if not entries:
        raise SendApiConfigError("token map has no tokens")
    callers = gate_config.get("callers") if isinstance(gate_config, dict) else None
    labels: set[str] = set()
    normalized: dict[str, dict[str, str]] = {}
    for digest, entry in entries.items():
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise SendApiConfigError("token map keys must be lowercase SHA-256 hex digests")
        if not isinstance(entry, dict):
            raise SendApiConfigError("token map entries must be objects with caller and label")
        caller = entry.get("caller")
        label = entry.get("label")
        if not isinstance(caller, str) or not caller.strip():
            raise SendApiConfigError("token map entry caller must be a non-empty string")
        if not isinstance(label, str) or not label.strip():
            raise SendApiConfigError("token map entry label must be a non-empty string")
        caller = caller.strip()
        label = label.strip()
        if caller == OPERATOR_CALLER:
            raise SendApiConfigError("the operator caller cannot be mapped to an API token")
        if not isinstance(callers, dict) or caller not in callers:
            raise SendApiConfigError(f"token map caller {caller!r} is not defined in the send gate config")
        if label in labels:
            raise SendApiConfigError(f"token map labels must be unique ({label!r})")
        labels.add(label)
        normalized[digest] = {"caller": caller, "label": label}
    return normalized


def load_token_map(path: Path | str, *, gate_config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    path = Path(path).expanduser()
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError as exc:
        raise SendApiConfigError(f"token map unreadable at {path}: {exc}") from exc
    if mode & 0o077:
        raise SendApiConfigError(f"token map at {path} must be mode 0600 or stricter")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SendApiConfigError(f"token map unreadable at {path}: {exc}") from exc
    return validate_token_map(raw, gate_config=gate_config)


def write_token_map(path: Path | str, entries: Mapping[str, Any]) -> None:
    """Atomically replace the map (tempfile + os.replace) at mode 0600."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"tokens": dict(entries)}, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Request/response helpers
# --------------------------------------------------------------------------


def _error_payload(command: str, code: str, message: str, *, retryable: bool = False, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
    if extra:
        error.update(extra)
    return {"ok": False, "command": command, "error": error, "meta": dict(SERVICE_META)}


def _success_payload(command: str, data: Any) -> dict[str, Any]:
    return {"ok": True, "command": command, "data": data, "meta": dict(SERVICE_META)}


def _log_request(*, caller: str | None, route: str, outcome: str, dry_run: bool, to_tail: str) -> None:
    sys.stderr.write(
        f"send_api caller={caller or '-'} route={route} outcome={outcome} "
        f"dry_run={str(bool(dry_run)).lower()} to_tail={to_tail}\n"
    )


def validate_send_body(payload: Mapping[str, Any]) -> tuple[str, str, str, bool]:
    unknown = sorted(set(payload) - ALLOWED_FIELDS)
    if unknown:
        raise SendApiError(f"unknown field(s): {', '.join(unknown)}", status=400, code="invalid_argument")
    missing = sorted(REQUIRED_FIELDS - set(payload))
    if missing:
        raise SendApiError(f"missing required field(s): {', '.join(missing)}", status=400, code="invalid_argument")
    to = payload["to"]
    if not isinstance(to, str) or not outbound_destination_policy.is_supported_outbound_destination(to.strip()):
        raise SendApiError("to must be a single +1 E.164 number", status=400, code="invalid_argument")
    from_number = payload["from"]
    if not isinstance(from_number, str) or not outbound_destination_policy.is_supported_outbound_destination(from_number.strip()):
        raise SendApiError("from must be a +1 E.164 number", status=400, code="invalid_argument")
    message = payload["message"]
    if not isinstance(message, str) or not message.strip():
        raise SendApiError("message must be a non-empty string", status=400, code="invalid_argument")
    if len(message) > MAX_MESSAGE_CHARS:
        raise SendApiError(f"message must be at most {MAX_MESSAGE_CHARS} characters", status=400, code="invalid_argument")
    dry_run = payload.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise SendApiError("dry_run must be a boolean", status=400, code="invalid_argument")
    return to.strip(), from_number.strip(), message, dry_run


def _is_support_line(number: str) -> bool:
    return sms_approval.normalize_phone_number(number) == _SUPPORT_LINE_NORMALIZED


def validate_bind_address(bind: str) -> str:
    """The send API is loopback-only; anything else refuses to start."""
    value = str(bind or "").strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("DIALPAD_SEND_API_BIND must be a literal IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_loopback:
        raise ValueError("DIALPAD_SEND_API_BIND must be an IPv4 loopback address (127.0.0.0/8)")
    return value


def default_send_runner(argv: list[str], *, message: str, env: Mapping[str, str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        input=message.encode("utf-8"),
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------


class SendApiRequestHandler(BaseHTTPRequestHandler):
    server: "SendApiServer"

    def log_message(self, _format: str, *_args: object) -> None:
        # Suppress access logging; request URLs and bodies can carry phone
        # numbers and message text.
        return

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _finish(
        self,
        status: int,
        command: str,
        payload: dict[str, Any],
        *,
        caller: str | None,
        route: str,
        dry_run: bool = False,
        to_tail: str = "-",
    ) -> None:
        self._write_json(status, payload)
        outcome = "ok" if payload.get("ok") else str((payload.get("error") or {}).get("code") or "error")
        _log_request(caller=caller, route=route, outcome=outcome, dry_run=dry_run, to_tail=to_tail)

    def _bearer_entry(self) -> dict[str, str] | None:
        header = self.headers.get("Authorization", "")
        scheme, separator, token = header.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            return None
        return self.server.entry_for_token(token)

    def _read_json_body(self) -> dict[str, Any]:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise SendApiError("Content-Type must be application/json", status=400, code="invalid_argument")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise SendApiError("Content-Length must be an integer", status=400, code="invalid_argument") from exc
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            raise SendApiError("request body is too large", status=413, code="invalid_argument")
        raw = self.rfile.read(content_length)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SendApiError("request body must be valid JSON", status=400, code="invalid_argument") from exc
        if not isinstance(payload, dict):
            raise SendApiError("request body must be a JSON object", status=400, code="invalid_argument")
        return payload

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

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        route = f"{method} {path}"
        command = COMMANDS.get(path, UNKNOWN_COMMAND)
        entry = self._bearer_entry()
        if entry is None:
            self._finish(
                401,
                command,
                _error_payload(command, "auth_missing", "valid bearer authorization is required"),
                caller=None,
                route=route,
            )
            return
        caller = entry["caller"]
        if method == "GET" and path == "/health":
            self._finish(200, command, _success_payload(command, {"status": "ok", "caller": caller}), caller=caller, route=route)
            return
        if method == "POST" and path == "/v1/sms/send":
            self._handle_send(caller, command, route)
            return
        if path in COMMANDS:
            self._finish(405, command, _error_payload(command, "invalid_argument", "method not allowed"), caller=caller, route=route)
            return
        self._finish(404, command, _error_payload(command, "not_found", "route not found"), caller=caller, route=route)

    def _handle_send(self, caller: str, command: str, route: str) -> None:
        try:
            payload = self._read_json_body()
            to, from_number, message, dry_run = validate_send_body(payload)
        except SendApiError as error:
            self._finish(
                error.status,
                command,
                _error_payload(command, error.code, str(error), retryable=error.retryable),
                caller=caller,
                route=route,
            )
            return
        to_tail = to[-4:]
        if _is_support_line(to) or _is_support_line(from_number):
            self._finish(
                403,
                command,
                _error_payload(command, "support_line_refused", "the support line cannot be a send endpoint"),
                caller=caller,
                route=route,
                dry_run=dry_run,
                to_tail=to_tail,
            )
            return
        status, response = execute_gated_send(
            to=to,
            from_number=from_number,
            message=message,
            caller=caller,
            dry_run=dry_run,
            environ=self.server.environ,
            send_runner=self.server.send_runner,
            command=command,
        )
        self._finish(status, command, response, caller=caller, route=route, dry_run=dry_run, to_tail=to_tail)


def execute_gated_send(
    *,
    to: str,
    from_number: str,
    message: str,
    caller: str,
    dry_run: bool,
    environ: Mapping[str, str],
    send_runner: Any,
    timeout: float = SEND_TIMEOUT_SECONDS,
    command: str = "send_api.send",
) -> tuple[int, dict[str, Any]]:
    """Run the gated ``bin/send_sms.py`` subprocess for one mapped caller.

    The message rides on stdin and ``DIALPAD_SEND_CALLER`` is scrubbed from
    the child environment. Returns (status, service envelope); shared by the
    send API and the team MCP server so both relay the same child codes.
    """
    argv = [
        sys.executable,
        str(SEND_SMS_SCRIPT),
        "--to",
        to,
        "--from",
        from_number,
        "--caller",
        caller,
        "--message-stdin",
        "--json",
    ]
    if dry_run:
        argv.append("--dry-run")
    env = {key: value for key, value in environ.items() if key != send_gate.CALLER_ENV}
    try:
        proc = send_runner(argv, message=message, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 504, _error_payload(command, "send_outcome_unknown", OUTCOME_UNKNOWN_MESSAGE)
    except Exception:  # noqa: BLE001 - a runner failure must not leak details upstream.
        return 502, _error_payload(command, "upstream_error", "upstream send failed")
    return _map_child_result(proc, command, dry_run=dry_run)


def _map_child_result(proc: subprocess.CompletedProcess, command: str, *, dry_run: bool) -> tuple[int, dict[str, Any]]:
    """Translate the gated CLI's JSON envelope into the service response.

    The child's stdout envelope is structured data; its stderr and exception
    text are never forwarded.
    """
    raw = proc.stdout
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        envelope = json.loads(raw or "")
    except (TypeError, ValueError):
        envelope = None
    if not isinstance(envelope, dict) or "ok" not in envelope:
        if dry_run:
            return 502, _error_payload(command, "upstream_error", "upstream send failed")
        return 502, _error_payload(command, "send_outcome_unknown", OUTCOME_UNKNOWN_MESSAGE)
    if envelope.get("ok"):
        data = envelope.get("data")
        return 200, _success_payload(command, data if isinstance(data, dict) else {"result": data})
    child_error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    code = str(child_error.get("code") or "upstream_error")
    retryable = bool(child_error.get("retryable"))
    if code == "send_gate_refused":
        child_meta = envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {}
        gate = child_meta.get("send_gate") if isinstance(child_meta.get("send_gate"), dict) else {}
        error: dict[str, Any] = {
            "code": "send_gate_refused",
            "message": "send gate refused",
            "retryable": bool(gate.get("retryable", retryable)),
            "reasons": [str(reason) for reason in gate.get("reasons") or []],
            "send_gate": gate,
        }
        if gate.get("retry_after_seconds") is not None:
            error["retry_after_seconds"] = gate["retry_after_seconds"]
        return 422, {"ok": False, "command": command, "error": error, "meta": dict(SERVICE_META)}
    status = 400 if code == "invalid_argument" else 502
    return status, _error_payload(command, code, "upstream send failed", retryable=retryable)


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


class SendApiServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        tokens_path: Path | str,
        gate_config: Mapping[str, Any] | None = None,
        send_runner: Any = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.tokens_path = Path(tokens_path).expanduser()
        self.environ = os.environ if environ is None else environ
        self.send_runner = send_runner or default_send_runner
        self._gate_config_from_env = gate_config is None
        if self._gate_config_from_env:
            try:
                gate_config = send_gate.load_config_from_env(self.environ)
            except Exception as exc:  # noqa: BLE001 - any config failure fails closed.
                raise SendApiConfigError(f"send gate config unavailable: {exc}") from exc
        self.gate_config = gate_config
        self._tokens_lock = threading.Lock()
        self._token_map = load_token_map(self.tokens_path, gate_config=self.gate_config)
        self._token_mtime = os.stat(self.tokens_path).st_mtime_ns
        self._gate_mtime = self._current_gate_mtime()
        super().__init__(address, SendApiRequestHandler)

    def _current_gate_mtime(self) -> int | None:
        """Mtime of the env-configured gate file; None when config was injected."""
        if not self._gate_config_from_env:
            return None
        raw = (self.environ.get(send_gate.CONFIG_ENV) or "").strip()
        if not raw:
            return None
        try:
            return os.stat(Path(raw).expanduser()).st_mtime_ns
        except OSError:
            return None

    def tokens(self) -> dict[str, dict[str, str]]:
        """Current token map; re-reads on mtime change and fails closed."""
        try:
            mtime = os.stat(self.tokens_path).st_mtime_ns
        except OSError:
            mtime = None
        gate_mtime = self._current_gate_mtime()
        if (
            mtime is not None
            and self._token_map is not None
            and mtime == self._token_mtime
            and gate_mtime == self._gate_mtime
        ):
            return self._token_map
        with self._tokens_lock:
            if mtime is None:
                self._token_map = {}
            else:
                try:
                    if self._gate_config_from_env:
                        self.gate_config = send_gate.load_config_from_env(self.environ)
                    self._token_map = load_token_map(self.tokens_path, gate_config=self.gate_config)
                except Exception:  # noqa: BLE001 - a failed reload refuses all requests.
                    self._token_map = {}
            self._token_mtime = mtime
            self._gate_mtime = gate_mtime
            return self._token_map

    def entry_for_token(self, token: str) -> dict[str, str] | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return self.tokens().get(digest)


def create_server(
    bind: str | None = None,
    port: int | None = None,
    *,
    tokens_path: Path | str | None = None,
    gate_config: Mapping[str, Any] | None = None,
    send_runner: Any = None,
    env: Mapping[str, str] | None = None,
) -> SendApiServer:
    environ = os.environ if env is None else env
    resolved_bind = validate_bind_address(bind if bind is not None else environ.get(BIND_ENV, DEFAULT_BIND))
    resolved_port = int(port if port is not None else environ.get(PORT_ENV, str(DEFAULT_PORT)))
    if not 0 <= resolved_port <= 65535:
        raise ValueError("DIALPAD_SEND_API_PORT must be between 0 and 65535")
    resolved_tokens = tokens_path or (environ.get(TOKENS_ENV) or "").strip()
    if not resolved_tokens:
        raise SendApiConfigError(f"{TOKENS_ENV} is not set")
    return SendApiServer(
        (resolved_bind, resolved_port),
        tokens_path=resolved_tokens,
        gate_config=gate_config,
        send_runner=send_runner,
        environ=environ,
    )


def main() -> int:
    try:
        server = create_server()
    except (SendApiConfigError, ValueError, OSError) as error:
        print(f"send API configuration error: {error}", file=sys.stderr)
        return 1
    print(f"Dialpad send API listening on {server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
