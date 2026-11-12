"""Tests for the ops assistant wake forward."""

from __future__ import annotations

import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import ops_wake
import webhook_server

LINE = "+14155550140"
OTHER_LINE = "+14155550199"


class _Capture(BaseHTTPRequestHandler):
    received: list = []
    status = 200

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).received.append(
            {"auth": self.headers.get("Authorization"), "body": json.loads(body)}
        )
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *_args):
        return None


@pytest.fixture
def capture_server():
    _Capture.received = []
    _Capture.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def wake_env(monkeypatch, tmp_path, capture_server):
    ops_wake.reset_state_for_tests()
    monkeypatch.setenv("OPS_WAKE_URL", f"http://127.0.0.1:{capture_server.server_port}/wake")
    monkeypatch.setenv("OPS_WAKE_KEY", "test-key")
    monkeypatch.setenv("OPS_WAKE_LINES", LINE)
    monkeypatch.setenv("OPS_WAKE_DEDUPE_DB", str(tmp_path / "wake.db"))
    monkeypatch.setenv("OPS_WAKE_MAX_PER_MINUTE", "6")
    monkeypatch.setenv("OPS_WAKE_MAX_PER_HOUR", "40")
    yield
    ops_wake.reset_state_for_tests()


def _call(call_id="c1", state="missed", **extra):
    payload = {
        "call_id": call_id,
        "direction": "inbound",
        "state": state,
        "internal_number": LINE,
        "external_number": "+14155550111",
        "date_started": 1700000000000,
        "entry_point_call_id": "ep1",
    }
    payload.update(extra)
    return payload


def _sms(message_id="m1", **extra):
    payload = {
        "id": message_id,
        "direction": "inbound",
        "from_number": "+14155550111",
        "to_number": [LINE],
        "text": "private body",
        "created_date": 1700000000000,
    }
    payload.update(extra)
    return payload


@pytest.mark.parametrize(
    "payload,expected",
    [
        (_call(state="missed"), "missed_call"),
        (_call(state="missed", voicemail_link="https://x"), "voicemail"),
        (_call(state="hangup", voicemail_recording_id="vm1"), "voicemail"),
        (_call(state="hangup", date_connected=1700000001000, talk_time=30), "inbound_call"),
        (_call(state="hangup"), "missed_call"),
        (_call(state="voicemail_uploaded"), "voicemail"),
        (_call(state="ringing"), None),
        (_call(direction="outbound", state="hangup"), None),
        (_call(state="missed", is_internal=True), None),
    ],
)
def test_classify_call_event(payload, expected):
    assert ops_wake.classify_call_event(payload) == expected


def test_disabled_without_url_or_key(monkeypatch):
    monkeypatch.delenv("OPS_WAKE_URL", raising=False)
    monkeypatch.delenv("OPS_WAKE_KEY", raising=False)
    with patch.object(ops_wake, "post_wake") as post:
        assert ops_wake.notify_call_event(_call(), background=False) is False
    post.assert_not_called()


def test_rejects_plain_http_to_remote_host(monkeypatch):
    monkeypatch.setenv("OPS_WAKE_URL", "http://example.com/wake")
    monkeypatch.setenv("OPS_WAKE_KEY", "k")
    assert ops_wake.is_enabled() is False


def test_call_wake_payload_is_minimal_and_authenticated(wake_env):
    assert ops_wake.notify_call_event(_call(), background=False) is True
    assert len(_Capture.received) == 1
    got = _Capture.received[0]
    assert got["auth"] == "Bearer test-key"
    body = got["body"]
    assert body["event"] == "missed_call"
    assert body["line"] == LINE
    assert body["dialpad"]["call_id"] == "c1"
    assert body["occurred_at"].startswith("2023-11-14T")
    assert body["suppressed_since_last"] == 0
    assert "+14155550111" not in json.dumps(body)


def test_sms_wake_omits_body_and_sender(wake_env):
    assert ops_wake.notify_sms_event(_sms(), background=False) is True
    body = _Capture.received[0]["body"]
    assert body["event"] == "inbound_sms"
    assert body["dialpad"]["message_id"] == "m1"
    dumped = json.dumps(body)
    assert "private body" not in dumped
    assert "+14155550111" not in dumped


def test_duplicate_events_wake_once(wake_env):
    assert ops_wake.notify_sms_event(_sms(), background=False) is True
    assert ops_wake.notify_sms_event(_sms(), background=False) is False
    assert ops_wake.notify_call_event(_call(state="missed"), background=False) is True
    # Hangup without connect after missed is the same missed call.
    assert ops_wake.notify_call_event(_call(state="hangup"), background=False) is False
    # A voicemail left on that call is new information.
    assert ops_wake.notify_call_event(
        _call(state="hangup", voicemail_link="https://x"), background=False
    ) is True
    assert len(_Capture.received) == 3


def test_unwatched_line_and_outbound_are_ignored(wake_env):
    assert ops_wake.notify_call_event(_call(internal_number=OTHER_LINE), background=False) is False
    assert ops_wake.notify_sms_event(_sms(direction="outbound"), background=False) is False
    assert _Capture.received == []


def test_rate_cap_suppresses_and_reports(wake_env, monkeypatch):
    monkeypatch.setenv("OPS_WAKE_MAX_PER_MINUTE", "2")
    results = [ops_wake.notify_sms_event(_sms(f"m{i}"), background=False) for i in range(5)]
    assert results == [True, True, False, False, False]
    assert len(_Capture.received) == 2
    # Once the window clears, the next wake reports what was dropped.
    ops_wake._minute_window.clear()
    assert ops_wake.notify_sms_event(_sms("m9"), background=False) is True
    assert _Capture.received[-1]["body"]["suppressed_since_last"] == 3


def test_receiver_failure_does_not_raise(wake_env):
    _Capture.status = 500
    assert ops_wake.notify_sms_event(_sms(), background=False) is True


def test_unreachable_receiver_does_not_raise(wake_env, monkeypatch):
    monkeypatch.setenv("OPS_WAKE_URL", "http://127.0.0.1:9/wake")
    monkeypatch.setenv("OPS_WAKE_TIMEOUT_SECONDS", "0.5")
    assert ops_wake.notify_call_event(_call(), background=False) is True


def test_dedupe_store_failure_fails_closed(wake_env, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("OPS_WAKE_DEDUPE_DB", str(blocker / "sub" / "wake.db"))
    assert ops_wake.notify_call_event(_call(), background=False) is False
    assert _Capture.received == []


def _build_handler(payload):
    raw = json.dumps(payload).encode("utf-8")
    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 1)
    status = {"code": None}
    handler.send_response = lambda code: status.__setitem__("code", code)
    handler.send_header = lambda *_a: None
    handler.end_headers = lambda: None
    handler.send_error = lambda code, _m=None: status.__setitem__("code", code)
    return handler, status


def test_call_webhook_invokes_wake(monkeypatch, tmp_path):
    monkeypatch.setenv("DIALPAD_CALLS_DB", str(tmp_path / "calls.db"))
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", None)
    payload = _call(state="hangup", date_connected=1700000001000, talk_time=30, duration=30)
    calls = []
    monkeypatch.setattr(
        webhook_server.ops_wake,
        "notify_call_event",
        lambda data, **kw: calls.append(data) or False,
    )
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)
    assert status["code"] == 200
    assert calls and calls[0]["call_id"] == "c1"
