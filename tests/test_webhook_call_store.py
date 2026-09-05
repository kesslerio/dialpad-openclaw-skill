"""Unit tests verifying webhook_server persists call events into local calls SQLite store."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import call_sqlite
import webhook_server


def _build_handler(payload: dict, headers: dict | None = None):
    raw = json.dumps(payload).encode("utf-8")
    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler.headers = {"Content-Length": str(len(raw))}
    if headers:
        handler.headers.update(headers)
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 12345)

    status = {"code": None}

    def _send_response(code):
        status["code"] = code

    def _send_header(_name, _value):
        return None

    def _end_headers():
        return None

    def _send_error(code, _message=None):
        status["code"] = code

    handler.send_response = _send_response
    handler.send_header = _send_header
    handler.end_headers = _end_headers
    handler.send_error = _send_error
    return handler, status


class WebhookCallStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "calls.db"
        self.env_patch = patch.dict("os.environ", {"DIALPAD_CALLS_DB": str(self.db_path)})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.temp_dir.cleanup()

    @patch.object(webhook_server, "WEBHOOK_SECRET", None)
    @patch.object(webhook_server, "send_to_telegram", return_value=True)
    def test_missed_call_event_persisted_to_calls_db(self, _mock_tg):
        payload = {
            "call_id": "call-missed-101",
            "direction": "inbound",
            "call_direction": "inbound",
            "call_missed": True,
            "from_number": "+14155550111",
            "to_number": "+14155201316",
            "contact_name": "Alice Wonderland",
            "date_started": 1700000000000,
            "duration": 0,
        }
        handler, status = _build_handler(payload)
        webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        self.assertEqual(status["code"], 200)
        stored = call_sqlite.get_call("call-missed-101", db_path=self.db_path)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["call_id"], "call-missed-101")
        self.assertEqual(stored["direction"], "inbound")
        self.assertEqual(stored["contact_number"], "4155550111")
        self.assertEqual(stored["contact_name"], "Alice Wonderland")
        self.assertEqual(stored["call_state"], "missed")
        self.assertEqual(stored["duration"], 0)
        self.assertFalse(stored["transcript_present"])

    @patch.object(webhook_server, "WEBHOOK_SECRET", None)
    def test_answered_call_event_persisted_even_if_not_missed(self):
        payload = {
            "call_id": "call-answered-202",
            "direction": "inbound",
            "call_direction": "inbound",
            "call_missed": False,
            "call_state": "completed",
            "from_number": "+14155550222",
            "to_number": "+14155201316",
            "contact_name": "Bob Builder",
            "date_started": 1700000000000,
            "date_ended": 1700000180000,
            "duration": 180,
            "transcript": "Hello, Bob here discussing project timeline.",
            "transcript_url": "https://dialpad.com/review/call-answered-202",
        }
        handler, status = _build_handler(payload)
        webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        self.assertEqual(status["code"], 200)
        stored = call_sqlite.get_call("call-answered-202", db_path=self.db_path)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["call_id"], "call-answered-202")
        self.assertEqual(stored["duration"], 180)
        self.assertEqual(stored["call_state"], "completed")
        self.assertTrue(stored["transcript_present"])
        self.assertEqual(stored["transcript_text"], "Hello, Bob here discussing project timeline.")
        self.assertEqual(stored["transcript_url"], "https://dialpad.com/review/call-answered-202")

    @patch.object(webhook_server, "WEBHOOK_SECRET", None)
    def test_outbound_call_event_persisted(self):
        payload = {
            "call_id": "call-outbound-303",
            "direction": "outbound",
            "call_direction": "outbound",
            "from_number": "+14155201316",
            "to_number": "+14155550333",
            "date_started": 1700000200000,
            "duration": 45,
            "call_state": "completed",
        }
        handler, status = _build_handler(payload)
        webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        self.assertEqual(status["code"], 200)
        stored = call_sqlite.get_call("call-outbound-303", db_path=self.db_path)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["call_id"], "call-outbound-303")
        self.assertEqual(stored["direction"], "outbound")
        self.assertEqual(stored["contact_number"], "4155550333")
        self.assertEqual(stored["duration"], 45)
