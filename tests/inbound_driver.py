"""Shared inbound webhook test driver fixture.

Provides a standardized, deterministic test harness for driving
Dialpad webhook server inbound SMS and call events, capturing observable
side effects (ACK responses, OpenClaw hook deliveries, Telegram messages,
outbound SMS attempts, and approval drafts) without coupling tests to
internal response structures or duplicating boilerplate.
"""

from __future__ import annotations

import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import webhook_server


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeCompletedProcess:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


@dataclass
class InboundCapture:
    ack_code: int
    ack_body: dict[str, Any]
    hook_calls: list[dict[str, Any]] = field(default_factory=list)
    telegram_messages: list[str] = field(default_factory=list)
    telegram_calls: list[dict[str, Any]] = field(default_factory=list)
    sms_calls: list[dict[str, Any]] = field(default_factory=list)
    drafts_db: Path = field(default_factory=Path)
    dedupe_db: Path = field(default_factory=Path)
    sms_db: Path = field(default_factory=Path)
    handler: Any = None


def build_handler(payload: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[Any, dict[str, Any]]:
    raw = json.dumps(payload).encode("utf-8")
    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler.headers = {"Content-Length": str(len(raw))}
    if headers:
        handler.headers.update(headers)
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 12345)

    status: dict[str, Any] = {"code": None, "headers": {}}

    def _send_response(code: int):
        status["code"] = code

    def _send_header(name: str, value: str):
        status["headers"][name] = value

    def _end_headers():
        pass

    def _send_error(code: int, message: str | None = None):
        status["code"] = code
        status["error_message"] = message

    handler.send_response = _send_response
    handler.send_header = _send_header
    handler.end_headers = _end_headers
    handler.send_error = _send_error
    return handler, status


class InboundDriver:
    """Harness to configure mocks, dispatch inbound webhooks, and collect side effects."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.dedupe_db = tmp_path / "dedupe.db"
        self.drafts_db = tmp_path / "approvals.db"
        self.sms_db = tmp_path / "sms.db"

        # Side effect capture buckets
        self.telegram_messages: list[str] = []
        self.telegram_calls: list[dict[str, Any]] = []
        self.hook_calls: list[dict[str, Any]] = []
        self.sms_calls: list[dict[str, Any]] = []
        self.original_send_sms_to_openclaw_hooks = webhook_server.send_sms_to_openclaw_hooks

        # Standard environment & isolation setup
        self.monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
        self.monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: self.dedupe_db)
        if hasattr(webhook_server, "sms_approval") and webhook_server.sms_approval:
            self.monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", self.drafts_db)
        if hasattr(webhook_server, "sms_sqlite") and webhook_server.sms_sqlite:
            self.monkeypatch.setattr(webhook_server.sms_sqlite, "DB_PATH", self.sms_db)
        import sms_sqlite
        self.monkeypatch.setattr(sms_sqlite, "DB_PATH", self.sms_db)
        self.monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)

        def _fake_send_telegram(text: str, reply_markup=None, chat_id=None, message_thread_id=None, **kwargs):
            self.telegram_messages.append(text)
            self.telegram_calls.append({
                "text": text,
                "reply_markup": reply_markup,
                "chat_id": chat_id,
                "message_thread_id": message_thread_id,
                **kwargs,
            })
            return True

        def _fake_send_hook(normalized_sms: dict[str, Any], line_display: str | None = None, **kwargs):
            self.hook_calls.append({
                "normalized_sms": normalized_sms,
                "line_display": line_display,
                **kwargs,
            })
            return True, "http_200"

        def _fake_send_sms(to_numbers, message, from_number=None, infer_country_code=False):
            self.sms_calls.append({
                "to_numbers": to_numbers,
                "message": message,
                "from_number": from_number,
                "infer_country_code": infer_country_code,
            })
            return {"id": "msg-1", "message_status": "pending"}

        self.monkeypatch.setattr(webhook_server, "send_to_telegram", _fake_send_telegram)
        self.monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", _fake_send_hook)
        self.monkeypatch.setattr(webhook_server, "dialpad_send_sms", _fake_send_sms)

        # Default contact lookup returns not_found
        self.set_contact_lookup(status="not_found")
        # Default handle_sms_webhook returns stored: True
        self.monkeypatch.setattr(
            webhook_server,
            "handle_sms_webhook",
            lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
        )

    def set_contact_lookup(
        self,
        contact_name: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        company: str | None = None,
        job_title: str | None = None,
        status: str = "resolved",
        degraded: bool = False,
        degraded_reason: str | None = None,
    ):
        result = {
            "contact_name": contact_name,
            "first_name": first_name,
            "last_name": last_name,
            "company": company,
            "job_title": job_title,
            "status": status,
            "degraded": degraded,
            "degraded_reason": degraded_reason,
        }
        self.monkeypatch.setattr(webhook_server, "lookup_contact_enrichment", lambda _number: result)
        return result

    def dispatch_sms(
        self,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> InboundCapture:
        handler, status = build_handler(payload, headers)
        webhook_server.DialpadWebhookHandler.handle_webhook(handler)

        ack_body = {}
        try:
            handler.wfile.seek(0)
            raw = handler.wfile.read()
            if raw:
                ack_body = json.loads(raw.decode("utf-8"))
        except Exception:
            pass

        return InboundCapture(
            ack_code=status["code"],
            ack_body=ack_body,
            hook_calls=self.hook_calls,
            telegram_messages=self.telegram_messages,
            telegram_calls=self.telegram_calls,
            sms_calls=self.sms_calls,
            drafts_db=self.drafts_db,
            dedupe_db=self.dedupe_db,
            sms_db=self.sms_db,
            handler=handler,
        )

    def dispatch_call(
        self,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> InboundCapture:
        handler, status = build_handler(payload, headers)
        webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        ack_body = {}
        try:
            handler.wfile.seek(0)
            raw = handler.wfile.read()
            if raw:
                ack_body = json.loads(raw.decode("utf-8"))
        except Exception:
            pass

        return InboundCapture(
            ack_code=status["code"],
            ack_body=ack_body,
            hook_calls=self.hook_calls,
            telegram_messages=self.telegram_messages,
            telegram_calls=self.telegram_calls,
            sms_calls=self.sms_calls,
            drafts_db=self.drafts_db,
            dedupe_db=self.dedupe_db,
            sms_db=self.sms_db,
            handler=handler,
        )
