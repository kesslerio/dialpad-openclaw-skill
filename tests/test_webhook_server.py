import io
import json
import os
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import webhook_server
from webhook_server import (
    assess_inbound_sms_alert_eligibility,
    classify_inbound_notification,
    detect_reliable_missed_call_hint,
    extract_message_text,
    is_sensitive_message,
    resolve_missed_call_context,
)


def _build_handler(payload, headers=None):
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


def _telegram_callback_payload(draft_id, action="a", chat_id="-100123", user_id=42, is_bot=False):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "callback-1",
            "from": {
                "id": user_id,
                "is_bot": is_bot,
                "username": "operator",
            },
            "message": {
                "message_id": 99,
                "chat": {"id": chat_id, "type": "group"},
                "text": "review",
            },
            "data": f"smsa:{action}:{draft_id}",
        },
    }


class WebhookNotificationClassificationTests(unittest.TestCase):
    def test_normal_inbound_sms_classified_as_sms(self):
        payload = {
            "direction": "inbound",
            "from_number": "+14155551234",
            "to_number": ["+14150001111"],
            "text": "Hello there",
        }
        self.assertEqual(classify_inbound_notification(payload), "sms")

    def test_blank_inbound_sms_classified_as_blank(self):
        payload = {
            "direction": "inbound",
            "from_number": "+14155551234",
            "to_number": ["+14150001111"],
            "text": "   ",
        }
        self.assertEqual(classify_inbound_notification(payload), "blank_sms")

    def test_missed_call_hint_requires_call_context_and_signal(self):
        payload = {
            "direction": "inbound",
            "from_number": "+14155551234",
            "to_number": ["+14150001111"],
            "text": "",
            "event_type": "call.missed",
            "call_state": "missed",
            "call_id": "abc123",
        }
        self.assertTrue(detect_reliable_missed_call_hint(payload))
        self.assertEqual(classify_inbound_notification(payload), "missed_call")

    def test_blank_sms_without_missed_signal_not_treated_as_call(self):
        payload = {
            "direction": "inbound",
            "from_number": "+14155551234",
            "to_number": ["+14150001111"],
            "text": "",
            "event_type": "sms.received",
        }
        self.assertFalse(detect_reliable_missed_call_hint(payload))
        self.assertEqual(classify_inbound_notification(payload), "blank_sms")

    def test_sensitive_google_verification_message_detected(self):
        text = "Google verification code: 482991. Do not share this code."
        self.assertTrue(is_sensitive_message(text=text, sender="Google"))

    def test_sensitive_bank_otp_message_detected(self):
        text = "Your OTP is 773311 for login. If not you, contact your bank."
        self.assertTrue(is_sensitive_message(text=text, sender="Capital One"))

    def test_non_sensitive_message_not_detected(self):
        text = "See you at 6pm for dinner."
        self.assertFalse(is_sensitive_message(text=text, sender="Friend"))

    def test_contact_match_requires_exact_phone(self):
        contact = {
            "first_name": "Keysha",
            "last_name": "Griffin",
            "phones": ["+17202248024"],
            "primary_phone": "+17202248024",
        }

        self.assertFalse(webhook_server.contact_contains_phone(contact, "+14782326499"))
        self.assertTrue(webhook_server.contact_contains_phone(contact, "+17202248024"))

    def test_lookup_ignores_fuzzy_contact_without_matching_phone(self):
        payload = {
            "items": [
                {
                    "first_name": "Keysha",
                    "last_name": "Griffin",
                    "phones": ["+17202248024"],
                    "primary_phone": "+17202248024",
                }
            ]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        with patch.object(webhook_server, "DIALPAD_API_KEY", "token"), patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ):
            result = webhook_server.lookup_contact_enrichment("+14782326499")

        self.assertEqual(result["status"], "not_found")
        self.assertIsNone(result["contact_name"])

    def test_lookup_resolves_contact_with_matching_phone(self):
        payload = {
            "items": [
                {
                    "first_name": "Nicole",
                    "last_name": "Roberson",
                    "phones": ["+14782326499"],
                    "primary_phone": "+14782326499",
                }
            ]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        with patch.object(webhook_server, "DIALPAD_API_KEY", "token"), patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ):
            result = webhook_server.lookup_contact_enrichment("+14782326499")

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["contact_name"], "Nicole Roberson")

    def test_inbound_alert_eligibility_filters_sensitive_sms(self):
        payload = {
            "direction": "inbound",
            "from_number": "+14155551234",
            "text": "Your OTP code is 773311 for login",
        }
        decision = assess_inbound_sms_alert_eligibility(
            payload,
            from_number="+14155551234",
            text=payload["text"],
            sender="Capital One",
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["reason_code"], "filtered_sensitive")

    def test_inbound_alert_eligibility_filters_shortcode_sender(self):
        payload = {
            "direction": "inbound",
            "from_number": "12345",
            "text": "Use 998812 to continue",
        }
        decision = assess_inbound_sms_alert_eligibility(
            payload,
            from_number=payload["from_number"],
            text=payload["text"],
            sender="Unknown",
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["reason_code"], "filtered_shortcode")

    def test_inbound_alert_eligibility_allows_benign_sms(self):
        payload = {
            "direction": "inbound",
            "from_number": "+14155551234",
            "text": "Can we meet tomorrow at 2?",
        }
        decision = assess_inbound_sms_alert_eligibility(
            payload,
            from_number=payload["from_number"],
            text=payload["text"],
            sender="Friend",
        )
        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["reason_code"], "eligible")

    def test_text_content_fallback_used_when_text_is_blank(self):
        payload = {
            "direction": "inbound",
            "text": "   ",
            "text_content": "Real body",
        }
        self.assertEqual(extract_message_text(payload), "Real body")
        self.assertEqual(classify_inbound_notification(payload), "sms")

    def test_sms_approval_reply_markup_uses_compact_callback_data(self):
        with patch.object(webhook_server, "DIALPAD_TELEGRAM_APPROVAL_BUTTONS_ENABLED", True), \
                patch.object(webhook_server, "TELEGRAM_BOT_TOKEN", "bot-token"), \
                patch.object(webhook_server, "TELEGRAM_CHAT_ID", "-100123"), \
                patch.object(webhook_server, "TELEGRAM_WEBHOOK_SECRET", "secret"):
            markup = webhook_server.build_sms_approval_reply_markup(
                "smsdraft_1234567890abcdef",
                {"state": "normal"},
            )

        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "Approve send")
        callback_data = markup["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(callback_data, "smsa:a:smsdraft_1234567890abcdef")
        self.assertLessEqual(len(callback_data.encode("utf-8")), 64)

    def test_sms_approval_reply_markup_disabled_without_webhook_secret(self):
        with patch.object(webhook_server, "DIALPAD_TELEGRAM_APPROVAL_BUTTONS_ENABLED", True), \
                patch.object(webhook_server, "TELEGRAM_BOT_TOKEN", "bot-token"), \
                patch.object(webhook_server, "TELEGRAM_CHAT_ID", "-100123"), \
                patch.object(webhook_server, "TELEGRAM_WEBHOOK_SECRET", ""):
            markup = webhook_server.build_sms_approval_reply_markup(
                "smsdraft_1234567890abcdef",
                {"state": "normal"},
            )

        self.assertIsNone(markup)

    def test_risky_sms_approval_reply_markup_requires_acknowledgement_first(self):
        with patch.object(webhook_server, "DIALPAD_TELEGRAM_APPROVAL_BUTTONS_ENABLED", True), \
                patch.object(webhook_server, "TELEGRAM_BOT_TOKEN", "bot-token"), \
                patch.object(webhook_server, "TELEGRAM_CHAT_ID", "-100123"), \
                patch.object(webhook_server, "TELEGRAM_WEBHOOK_SECRET", "secret"):
            markup = webhook_server.build_sms_approval_reply_markup(
                "smsdraft_1234567890abcdef",
                {"state": "risky"},
            )

        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "Acknowledge risk")
        self.assertEqual(
            markup["inline_keyboard"][0][0]["callback_data"],
            "smsa:a:smsdraft_1234567890abcdef",
        )

    def test_parse_telegram_callback_data_rejects_unknown_namespace(self):
        self.assertIsNone(webhook_server.parse_telegram_callback_data("other:a:smsdraft_1"))

    def test_send_to_telegram_includes_reply_markup_payload(self):
        requests = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_urlopen(request, timeout=10):
            requests.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        reply_markup = {
            "inline_keyboard": [[{"text": "Approve send", "callback_data": "smsa:a:smsdraft_1"}]]
        }
        with patch.object(webhook_server, "TELEGRAM_BOT_TOKEN", "bot-token"), \
                patch.object(webhook_server, "TELEGRAM_CHAT_ID", "-100123"), \
                patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            sent = webhook_server.send_to_telegram("Review", reply_markup=reply_markup)

        self.assertTrue(sent)
        self.assertEqual(requests[0]["reply_markup"], reply_markup)


class MissedCallResolutionTests(unittest.TestCase):
    def test_sparse_payload_nested_key_resolution(self):
        payload = {
            "event": {
                "timestamp": 1760000000000,
                "call": {
                    "from_number": "+14155550123",
                    "to_number": "+14155201316",
                },
            }
        }
        resolved = resolve_missed_call_context(payload)
        self.assertEqual(resolved["from_number"], "+14155550123")
        self.assertEqual(resolved["to_number"], "+14155201316")
        self.assertEqual(resolved["caller_resolution_path"], "payload_inferred")
        self.assertEqual(resolved["line_resolution_path"], "payload_inferred")

    def test_inferred_line_resolution_uses_line_name_when_phone_missing(self):
        payload = {
            "date_started": 1760000000000,
            "from_number": "+14155550123",
            "line": {"name": "Support Front Desk"},
        }
        resolved = resolve_missed_call_context(payload)
        self.assertEqual(resolved["line_display"], "Support Front Desk")
        self.assertEqual(resolved["line_resolution_path"], "payload_inferred")

    def test_legacy_alias_numbers_are_treated_as_payload_direct(self):
        payload = {
            "timestamp": 1760000000000,
            "caller_number": "+14155550123",
            "called_number": "+14155201316",
        }
        resolved = resolve_missed_call_context(payload)
        self.assertEqual(resolved["from_number"], "+14155550123")
        self.assertEqual(resolved["to_number"], "+14155201316")
        self.assertEqual(resolved["caller_resolution_path"], "payload_direct")
        self.assertEqual(resolved["line_resolution_path"], "payload_direct")

    def test_legacy_line_number_fallback_infers_line_display(self):
        payload = {
            "timestamp": 1760000000000,
            "line_number": "+14155201316",
        }
        resolved = resolve_missed_call_context(payload, history_fetcher=lambda _ts: [])
        self.assertEqual(resolved["line_display"], "Sales (415) 520-1316")
        self.assertEqual(resolved["line_resolution_path"], "payload_inferred")

    def test_history_backfill_resolution(self):
        payload = {
            "date_started": 1760000000000,
            "event_type": "call.missed",
            "from_number": "+14155550999",
        }

        def fake_history(_event_ts_ms):
            return [
                {
                    "direction": "inbound",
                    "state": "missed",
                    "date_started": 1760000000500,
                    "external_number": "+14155550999",
                    "entry_point_target": {
                        "phone": "+14159917155",
                        "name": "Support",
                    },
                }
            ]

        resolved = resolve_missed_call_context(payload, history_fetcher=fake_history)
        self.assertEqual(resolved["from_number"], "+14155550999")
        self.assertEqual(resolved["to_number"], "+14159917155")
        self.assertEqual(resolved["caller_resolution_path"], "payload_direct")
        self.assertEqual(resolved["line_resolution_path"], "history_backfill")

    def test_unresolved_guard_behavior(self):
        payload = {"event_type": "call.missed", "timestamp": 1760000000000}
        resolved = resolve_missed_call_context(payload, history_fetcher=lambda _ts: [])
        self.assertEqual(resolved["from_number"], "Unknown")
        self.assertIsNone(resolved["line_display"])
        self.assertEqual(resolved["caller_resolution_path"], "unresolved")
        self.assertEqual(resolved["line_resolution_path"], "unresolved")

    def test_history_backfill_requires_inbound_missed_row(self):
        payload = {"event_type": "call.missed", "timestamp": 1760000000000}

        def fake_history(_event_ts_ms):
            return [
                {
                    "direction": "outbound",
                    "state": "answered",
                    "duration": 65,
                    "date_started": 1760000000050,
                    "external_number": "+14155550999",
                    "entry_point_target": {"phone": "+14159917155", "name": "Support"},
                }
            ]

        resolved = resolve_missed_call_context(payload, history_fetcher=fake_history)
        self.assertEqual(resolved["from_number"], "Unknown")
        self.assertIsNone(resolved["line_display"])
        self.assertEqual(resolved["caller_resolution_path"], "unresolved")
        self.assertEqual(resolved["line_resolution_path"], "unresolved")

    def test_history_backfill_requires_number_match_evidence(self):
        payload = {
            "event_type": "call.missed",
            "timestamp": 1760000000000,
            "from_number": "+14155550000",
        }

        def fake_history(_event_ts_ms):
            return [
                {
                    "direction": "inbound",
                    "state": "missed",
                    "duration": 0,
                    "date_started": 1760000000050,
                    "external_number": "+14155550999",
                    "entry_point_target": {"phone": "+14159917155", "name": "Support"},
                }
            ]

        resolved = resolve_missed_call_context(payload, history_fetcher=fake_history)
        self.assertEqual(resolved["from_number"], "+14155550000")
        self.assertIsNone(resolved["to_number"])
        self.assertEqual(resolved["caller_resolution_path"], "payload_direct")
        self.assertEqual(resolved["line_resolution_path"], "unresolved")

    def test_history_duration_parse_failure_not_missed_like(self):
        payload = {
            "event_type": "call.missed",
            "timestamp": 1760000000000,
            "from_number": "+14155550000",
        }

        def fake_history(_event_ts_ms):
            return [
                {
                    "direction": "inbound",
                    "state": "",
                    "duration": "",
                    "date_started": 1760000000050,
                    "external_number": "+14155550000",
                    "entry_point_target": {"phone": "+14159917155", "name": "Support"},
                }
            ]

        resolved = resolve_missed_call_context(payload, history_fetcher=fake_history)
        self.assertIsNone(resolved["to_number"])
        self.assertEqual(resolved["line_resolution_path"], "unresolved")


class TelegramCallbackHandlerTests(unittest.TestCase):
    def test_telegram_callback_requires_secret(self):
        payload = _telegram_callback_payload("smsdraft_1234567890abcdef")
        with patch.object(webhook_server, "TELEGRAM_WEBHOOK_SECRET", "secret"), \
                patch.object(webhook_server, "TELEGRAM_CHAT_ID", "-100123"):
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_telegram_webhook(handler)

        self.assertEqual(status["code"], 401)

    def test_telegram_callback_rejects_wrong_chat_without_dispatch(self):
        payload = _telegram_callback_payload("smsdraft_1234567890abcdef", chat_id="-100999")
        with patch.object(webhook_server, "TELEGRAM_WEBHOOK_SECRET", "secret"), \
                patch.object(webhook_server, "TELEGRAM_CHAT_ID", "-100123"), \
                patch.object(webhook_server, "answer_telegram_callback", return_value=True) as answer, \
                patch.object(webhook_server, "dispatch_telegram_approval_callback") as dispatch:
            handler, status = _build_handler(
                payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            )
            webhook_server.DialpadWebhookHandler.handle_telegram_webhook(handler)

        self.assertEqual(status["code"], 403)
        answer.assert_called_once()
        dispatch.assert_not_called()

    def test_telegram_callback_approve_sends_exact_stored_draft(self):
        send_calls = []
        telegram_api_calls = []

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(webhook_server.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"), \
                patch.object(webhook_server, "TELEGRAM_WEBHOOK_SECRET", "secret"), \
                patch.object(webhook_server, "TELEGRAM_CHAT_ID", "-100123"), \
                patch.object(webhook_server, "TELEGRAM_BOT_TOKEN", "bot-token"), \
                patch.object(
                    webhook_server,
                    "dialpad_send_sms",
                    side_effect=lambda to_numbers, message, from_number=None: send_calls.append(
                        (to_numbers, message, from_number)
                    ) or {"id": "sms-1", "message_status": "pending"},
                ), \
                patch.object(
                    webhook_server,
                    "call_telegram_api",
                    side_effect=lambda method, payload, timeout=10: telegram_api_calls.append(
                        (method, payload)
                    ) or True,
                ):
            conn = webhook_server.sms_approval.init_db()
            try:
                draft = webhook_server.sms_approval.create_draft(
                    conn,
                    thread_key="thread-1",
                    customer_number="+15125550100",
                    sender_number="+14155201316",
                    draft_text="Stored exact text.",
                )
            finally:
                conn.close()

            handler, status = _build_handler(
                _telegram_callback_payload(draft["draft_id"], action="a"),
                headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            )
            webhook_server.DialpadWebhookHandler.handle_telegram_webhook(handler)

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertTrue(response["sent"])
        self.assertEqual(send_calls, [(["+15125550100"], "Stored exact text.", "+14155201316")])
        self.assertIn("answerCallbackQuery", [method for method, _payload in telegram_api_calls])
        self.assertIn("editMessageReplyMarkup", [method for method, _payload in telegram_api_calls])
        self.assertIn("sendMessage", [method for method, _payload in telegram_api_calls])

    def test_telegram_callback_risky_first_click_replaces_markup_without_send(self):
        send_calls = []
        telegram_api_calls = []

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(webhook_server.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"), \
                patch.object(webhook_server, "DIALPAD_TELEGRAM_APPROVAL_BUTTONS_ENABLED", True), \
                patch.object(webhook_server, "TELEGRAM_WEBHOOK_SECRET", "secret"), \
                patch.object(webhook_server, "TELEGRAM_CHAT_ID", "-100123"), \
                patch.object(webhook_server, "TELEGRAM_BOT_TOKEN", "bot-token"), \
                patch.object(
                    webhook_server,
                    "dialpad_send_sms",
                    side_effect=lambda *_args, **_kwargs: send_calls.append(True) or {"id": "sms-1"},
                ), \
                patch.object(
                    webhook_server,
                    "call_telegram_api",
                    side_effect=lambda method, payload, timeout=10: telegram_api_calls.append(
                        (method, payload)
                    ) or True,
                ):
            conn = webhook_server.sms_approval.init_db()
            try:
                draft = webhook_server.sms_approval.create_draft(
                    conn,
                    thread_key="thread-1",
                    customer_number="+15125550100",
                    sender_number="+14155201316",
                    draft_text="Stored exact text.",
                    risk_state=webhook_server.sms_approval.RISK_RISKY,
                    risk_reason="customer asked for a real person",
                )
            finally:
                conn.close()

            handler, status = _build_handler(
                _telegram_callback_payload(draft["draft_id"], action="a"),
                headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            )
            webhook_server.DialpadWebhookHandler.handle_telegram_webhook(handler)

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertFalse(response["sent"])
        self.assertEqual(response["approval_status"], "risky_confirmation_required")
        self.assertEqual(send_calls, [])
        reply_markups = [
            payload.get("reply_markup")
            for method, payload in telegram_api_calls
            if method == "editMessageReplyMarkup"
        ]
        self.assertEqual(
            reply_markups[0]["inline_keyboard"][0][0]["callback_data"],
            f"smsa:c:{draft['draft_id']}",
        )

    def test_telegram_callback_reject_marks_draft_rejected_without_send(self):
        send_calls = []

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(webhook_server.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"), \
                patch.object(webhook_server, "TELEGRAM_WEBHOOK_SECRET", "secret"), \
                patch.object(webhook_server, "TELEGRAM_CHAT_ID", "-100123"), \
                patch.object(webhook_server, "TELEGRAM_BOT_TOKEN", "bot-token"), \
                patch.object(
                    webhook_server,
                    "dialpad_send_sms",
                    side_effect=lambda *_args, **_kwargs: send_calls.append(True) or {"id": "sms-1"},
                ), \
                patch.object(webhook_server, "call_telegram_api", return_value=True):
            conn = webhook_server.sms_approval.init_db()
            try:
                draft = webhook_server.sms_approval.create_draft(
                    conn,
                    thread_key="thread-1",
                    customer_number="+15125550100",
                    sender_number="+14155201316",
                    draft_text="Stored exact text.",
                )
            finally:
                conn.close()

            handler, status = _build_handler(
                _telegram_callback_payload(draft["draft_id"], action="r"),
                headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            )
            webhook_server.DialpadWebhookHandler.handle_telegram_webhook(handler)
            conn = webhook_server.sms_approval.init_db()
            try:
                stored = webhook_server.sms_approval.get_draft(conn, draft["draft_id"])
            finally:
                conn.close()

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertFalse(response["sent"])
        self.assertEqual(response["approval_status"], "rejected")
        self.assertEqual(send_calls, [])
        self.assertEqual(stored["status"], "rejected")

    def test_non_terminal_actor_failure_keeps_existing_approval_buttons(self):
        callback_query = {
            "message": {"message_id": 99, "chat": {"id": "-100123"}},
        }

        with patch.object(webhook_server, "edit_telegram_message_reply_markup") as edit_markup, \
                patch.object(webhook_server, "send_to_telegram", return_value=True) as send_status:
            webhook_server.update_telegram_review_after_callback(
                callback_query,
                {
                    "ok": False,
                    "status": "actor_not_allowed",
                    "sent": False,
                    "reason": "actor_not_in_allowlist",
                    "draft": {"draft_id": "smsdraft_1234567890abcdef"},
                },
                "smsdraft_1234567890abcdef",
            )

        edit_markup.assert_not_called()
        send_status.assert_called_once()


class CallWebhookHandlerTests(unittest.TestCase):
    class _FakeMoment:
        def __init__(self, text):
            self._text = text

        def astimezone(self):
            return self

        def strftime(self, _fmt):
            return self._text

        def isoformat(self):
            return "2026-03-26T11:11:00-07:00"

    class _FakeDatetime:
        @classmethod
        def now(cls):
            return CallWebhookHandlerTests._FakeMoment("11:11 PM")

        @classmethod
        def fromtimestamp(cls, _value):
            return CallWebhookHandlerTests._FakeMoment("9:42 AM")

    def test_call_webhook_requires_auth_when_secret_configured(self):
        with patch.object(webhook_server, "WEBHOOK_SECRET", "secret-123"):
            payload = {
                "direction": "inbound",
                "call_direction": "inbound",
                "call_missed": True,
                "call_id": "call-123",
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        self.assertEqual(status["code"], 401)

    def test_inbound_missed_call_forwards_hook_and_telegram(self):
        hook_calls = []
        telegram_messages = []
        sms_calls = []

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(webhook_server.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"), \
                patch.object(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True), \
                patch.object(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316"), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_CALL_ENABLED", True), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_TOKEN", "token-123"), \
                patch.object(
                    webhook_server,
                    "lookup_contact_enrichment",
                    return_value={
                        "contact_name": None,
                        "first_name": None,
                        "last_name": None,
                        "company": None,
                        "job_title": None,
                        "status": "not_found",
                        "degraded": False,
                        "degraded_reason": None,
                    },
                ), \
                patch.object(
                    webhook_server,
                    "send_to_openclaw_hooks",
                    side_effect=lambda normalized_event, line_display=None: (
                        hook_calls.append({"normalized_event": normalized_event, "line_display": line_display}) or
                        (True, "http_200")
                    ),
                ), \
                patch.object(
                    webhook_server,
                    "send_to_telegram",
                    side_effect=lambda text: telegram_messages.append(text) or True,
                ), \
                patch.object(
                    webhook_server,
                    "dialpad_send_sms",
                    side_effect=lambda to_numbers, message, from_number=None, infer_country_code=False: sms_calls.append(
                        {
                            "to_numbers": to_numbers,
                            "message": message,
                            "from_number": from_number,
                            "infer_country_code": infer_country_code,
                        }
                    ) or {"id": "msg-1", "message_status": "pending"},
                ):
            payload = {
                "direction": "inbound",
                "call_direction": "inbound",
                "call_missed": True,
                "call_id": "call-123",
                "from_number": "+14155550123",
                "to_number": "+14155201316",
                "date_started": 1760000000000,
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertEqual(len(hook_calls), 1)
        self.assertEqual(sms_calls, [])
        self.assertEqual(hook_calls[0]["normalized_event"]["event_type"], "missed_call")
        self.assertEqual(hook_calls[0]["normalized_event"]["call_id"], "call-123")
        self.assertEqual(hook_calls[0]["normalized_event"]["first_contact"]["knownContact"], False)
        self.assertEqual(hook_calls[0]["normalized_event"]["first_contact"]["keepBrief"], False)
        self.assertEqual(hook_calls[0]["normalized_event"]["first_contact"]["identityState"], "not_found")
        self.assertFalse(hook_calls[0]["normalized_event"]["auto_reply"]["sent"])
        self.assertTrue(hook_calls[0]["normalized_event"]["auto_reply"]["draftCreated"])
        self.assertTrue(hook_calls[0]["normalized_event"]["auto_reply"]["draftId"])
        self.assertEqual(len(telegram_messages), 1)
        self.assertTrue(response["missed_call"])
        self.assertTrue(response["hook_forwarded"])
        self.assertEqual(response["hook_status"], "http_200")
        self.assertTrue(response["telegram_sent"])
        self.assertFalse(response["auto_reply_sent"])
        self.assertEqual(response["auto_reply_status"], "draft_created")
        self.assertTrue(response["auto_reply_draft_id"])

    def test_inbound_missed_call_respects_disabled_hook_config(self):
        telegram_messages = []

        with patch.object(webhook_server, "OPENCLAW_HOOKS_CALL_ENABLED", False), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_TOKEN", "token-123"), \
                patch.object(
                    webhook_server,
                    "lookup_contact_enrichment",
                    return_value={
                        "contact_name": "Jane Doe",
                        "status": "resolved",
                        "degraded": False,
                        "degraded_reason": None,
                    },
                ), \
                patch.object(
                    webhook_server,
                    "send_to_telegram",
                    side_effect=lambda text: telegram_messages.append(text) or True,
                ):
            payload = {
                "direction": "inbound",
                "call_direction": "inbound",
                "call_missed": True,
                "call_id": "call-123",
                "from_number": "+14155550123",
                "to_number": "+14155201316",
                "date_started": 1760000000000,
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertEqual(len(telegram_messages), 1)
        self.assertTrue(response["missed_call"])
        self.assertFalse(response["hook_forwarded"])
        self.assertEqual(response["hook_status"], "disabled_by_config")
        self.assertTrue(response["telegram_sent"])

    def test_inbound_missed_call_hook_failure_keeps_webhook_200(self):
        telegram_messages = []

        with patch.object(webhook_server, "OPENCLAW_HOOKS_CALL_ENABLED", True), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_TOKEN", "token-123"), \
                patch.object(
                    webhook_server,
                    "lookup_contact_enrichment",
                    return_value={
                        "contact_name": "Jane Doe",
                        "status": "resolved",
                        "degraded": False,
                        "degraded_reason": None,
                    },
                ), \
                patch.object(
                    webhook_server,
                    "send_to_openclaw_hooks",
                    return_value=(False, "request_failed"),
                ), \
                patch.object(
                    webhook_server,
                    "send_to_telegram",
                    side_effect=lambda text: telegram_messages.append(text) or True,
                ):
            payload = {
                "direction": "inbound",
                "call_direction": "inbound",
                "call_missed": True,
                "call_id": "call-123",
                "from_number": "+14155550123",
                "to_number": "+14155201316",
                "date_started": 1760000000000,
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertTrue(response["missed_call"])
        self.assertFalse(response["hook_forwarded"])
        self.assertEqual(response["hook_status"], "request_failed")
        self.assertTrue(response["telegram_sent"])

    def test_inbound_missed_call_token_missing_keeps_webhook_200(self):
        telegram_messages = []

        with patch.object(webhook_server, "OPENCLAW_HOOKS_CALL_ENABLED", True), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_TOKEN", ""), \
                patch.object(
                    webhook_server,
                    "lookup_contact_enrichment",
                    return_value={
                        "contact_name": "Jane Doe",
                        "status": "resolved",
                        "degraded": False,
                        "degraded_reason": None,
                    },
                ), \
                patch.object(
                    webhook_server,
                    "send_to_telegram",
                    side_effect=lambda text: telegram_messages.append(text) or True,
                ):
            payload = {
                "direction": "inbound",
                "call_direction": "inbound",
                "call_state": "missed",
                "call_id": "call-123",
                "from_number": "+14155550123",
                "to_number": "+14155201316",
                "date_started": 1760000000000,
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertTrue(response["missed_call"])
        self.assertFalse(response["hook_forwarded"])
        self.assertEqual(response["hook_status"], "token_missing")
        self.assertTrue(response["telegram_sent"])

    def test_outbound_call_does_not_forward_hook_or_telegram(self):
        hook_calls = []
        telegram_messages = []

        with patch.object(webhook_server, "OPENCLAW_HOOKS_CALL_ENABLED", True), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_TOKEN", "token-123"), \
                patch.object(
                    webhook_server,
                    "send_to_openclaw_hooks",
                    side_effect=lambda normalized_event, line_display=None: (
                        hook_calls.append({"normalized_event": normalized_event, "line_display": line_display}) or
                        (True, "http_200")
                    ),
                ), \
                patch.object(
                    webhook_server,
                    "send_to_telegram",
                    side_effect=lambda text: telegram_messages.append(text) or True,
                ):
            payload = {
                "direction": "outbound",
                "call_direction": "outbound",
                "duration": 12,
                "call_state": "answered",
                "call_id": "call-123",
                "from_number": "+14155550123",
                "to_number": "+14155201316",
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertEqual(hook_calls, [])
        self.assertEqual(telegram_messages, [])
        self.assertFalse(response["missed_call"])
        self.assertIsNone(response["hook_forwarded"])
        self.assertIsNone(response["hook_status"])
        self.assertIsNone(response["telegram_sent"])

    def test_inbound_missed_call_telegram_uses_event_timestamp_and_escapes_markdown(self):
        telegram_messages = []

        with patch.object(webhook_server, "datetime", self._FakeDatetime), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_CALL_ENABLED", False), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_TOKEN", "token-123"), \
                patch.object(
                    webhook_server,
                    "lookup_contact_enrichment",
                    return_value={
                        "contact_name": "Jane_Doe",
                        "status": "resolved",
                        "degraded": False,
                        "degraded_reason": None,
                    },
                ), \
                patch.object(
                    webhook_server,
                    "send_to_telegram",
                    side_effect=lambda text: telegram_messages.append(text) or True,
                ):
            payload = {
                "direction": "inbound",
                "call_direction": "inbound",
                "call_missed": True,
                "call_id": "call-123",
                "from_number": "+14155550123",
                "to_number": "+14155201316",
                "date_started": 1760000000000,
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        self.assertEqual(status["code"], 200)
        self.assertEqual(len(telegram_messages), 1)
        self.assertIn("*Time:* 9:42 AM", telegram_messages[0])
        self.assertIn(r"Jane\_Doe", telegram_messages[0])
        self.assertNotIn("11:11 PM", telegram_messages[0])

    def test_known_recent_missed_call_creates_context_approval_draft(self):
        hook_calls = []
        telegram_messages = []
        sms_calls = []
        event_ts = 1760000000000
        recent_call = {
            "external_number": "+14322083277",
            "entry_point_target": {"phone": "+14155201316", "name": "Sales"},
            "date_started": event_ts - (2 * 24 * 60 * 60 * 1000),
            "direction": "inbound",
            "state": "hangup",
            "duration": 120000,
        }

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(webhook_server.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"), \
                patch.object(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True), \
                patch.object(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316"), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_CALL_ENABLED", True), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_TOKEN", "token-123"), \
                patch.object(
                    webhook_server,
                    "lookup_contact_enrichment",
                    return_value={
                        "contact_name": "Ann Harper",
                        "first_name": "Ann",
                        "last_name": "Harper",
                        "company": "Prospect",
                        "job_title": None,
                        "status": "resolved",
                        "degraded": False,
                        "degraded_reason": None,
                    },
                ), \
                patch.object(webhook_server, "_fetch_recent_calls_around", return_value=[recent_call]), \
                patch.object(
                    webhook_server,
                    "send_to_openclaw_hooks",
                    side_effect=lambda normalized_event, line_display=None: (
                        hook_calls.append({"normalized_event": normalized_event, "line_display": line_display}) or
                        (True, "http_200")
                    ),
                ), \
                patch.object(
                    webhook_server,
                    "send_to_telegram",
                    side_effect=lambda text, **_kwargs: telegram_messages.append(text) or True,
                ), \
                patch.object(
                    webhook_server,
                    "dialpad_send_sms",
                    side_effect=lambda *args, **kwargs: sms_calls.append((args, kwargs)) or {"id": "msg-1", "message_status": "pending"},
                ):
            payload = {
                "direction": "inbound",
                "call_direction": "inbound",
                "call_missed": True,
                "call_id": "call-123",
                "from_number": "+14322083277",
                "to_number": "+14155201316",
                "date_started": event_ts,
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        inbound_context = hook_calls[0]["normalized_event"]["inbound_context"]
        self.assertEqual(status["code"], 200)
        self.assertEqual(sms_calls, [])
        self.assertEqual(inbound_context["contactName"], "Ann Harper")
        self.assertEqual(inbound_context["identityConfidence"], "high")
        self.assertEqual(inbound_context["recency"]["state"], "fresh")
        self.assertTrue(inbound_context["contextDraftAllowed"])
        self.assertTrue(hook_calls[0]["normalized_event"]["auto_reply"]["draftCreated"])
        self.assertEqual(response["auto_reply_status"], "draft_created")
        self.assertTrue(response["auto_reply_draft_id"])
        self.assertIn("Inbound context", telegram_messages[0])
        self.assertIn("Ann Harper", telegram_messages[0])
        self.assertIn("SMS approval draft", telegram_messages[0])

    def test_stale_known_missed_call_gets_context_without_draft(self):
        hook_calls = []
        telegram_messages = []
        event_ts = 1760000000000
        stale_call = {
            "external_number": "+14322083277",
            "entry_point_target": {"phone": "+14155201316", "name": "Sales"},
            "date_started": event_ts - (16 * 24 * 60 * 60 * 1000),
            "direction": "inbound",
            "state": "hangup",
            "duration": 120000,
        }

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(webhook_server.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"), \
                patch.object(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True), \
                patch.object(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316"), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_CALL_ENABLED", True), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_TOKEN", "token-123"), \
                patch.object(
                    webhook_server,
                    "lookup_contact_enrichment",
                    return_value={
                        "contact_name": "Ann Harper",
                        "first_name": "Ann",
                        "last_name": "Harper",
                        "company": "Prospect",
                        "job_title": None,
                        "status": "resolved",
                        "degraded": False,
                        "degraded_reason": None,
                    },
                ), \
                patch.object(webhook_server, "_fetch_recent_calls_around", return_value=[stale_call]), \
                patch.object(
                    webhook_server,
                    "send_to_openclaw_hooks",
                    side_effect=lambda normalized_event, line_display=None: (
                        hook_calls.append({"normalized_event": normalized_event, "line_display": line_display}) or
                        (True, "http_200")
                    ),
                ), \
                patch.object(
                    webhook_server,
                    "send_to_telegram",
                    side_effect=lambda text, **_kwargs: telegram_messages.append(text) or True,
                ):
            payload = {
                "direction": "inbound",
                "call_direction": "inbound",
                "call_missed": True,
                "call_id": "call-123",
                "from_number": "+14322083277",
                "to_number": "+14155201316",
                "date_started": event_ts,
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        inbound_context = hook_calls[0]["normalized_event"]["inbound_context"]
        self.assertEqual(status["code"], 200)
        self.assertEqual(inbound_context["recency"]["state"], "stale")
        self.assertFalse(inbound_context["contextDraftAllowed"])
        self.assertFalse(hook_calls[0]["normalized_event"]["auto_reply"]["draftCreated"])
        self.assertEqual(response["auto_reply_status"], "not_eligible")
        self.assertIsNone(response["auto_reply_draft_id"])
        self.assertIn("Inbound context", telegram_messages[0])
        self.assertNotIn("SMS approval draft", telegram_messages[0])

    def test_payload_only_contact_name_does_not_create_context_draft(self):
        hook_calls = []
        telegram_messages = []
        event_ts = 1760000000000
        recent_call = {
            "external_number": "+14322083277",
            "entry_point_target": {"phone": "+14155201316", "name": "Sales"},
            "date_started": event_ts - (2 * 24 * 60 * 60 * 1000),
            "direction": "inbound",
            "state": "hangup",
            "duration": 120000,
        }

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(webhook_server.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"), \
                patch.object(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True), \
                patch.object(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316"), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_CALL_ENABLED", True), \
                patch.object(webhook_server, "OPENCLAW_HOOKS_TOKEN", "token-123"), \
                patch.object(
                    webhook_server,
                    "lookup_contact_enrichment",
                    return_value={
                        "contact_name": None,
                        "first_name": None,
                        "last_name": None,
                        "company": None,
                        "job_title": None,
                        "status": "not_found",
                        "degraded": False,
                        "degraded_reason": None,
                    },
                ), \
                patch.object(webhook_server, "_fetch_recent_calls_around", return_value=[recent_call]), \
                patch.object(
                    webhook_server,
                    "send_to_openclaw_hooks",
                    side_effect=lambda normalized_event, line_display=None: (
                        hook_calls.append({"normalized_event": normalized_event, "line_display": line_display}) or
                        (True, "http_200")
                    ),
                ), \
                patch.object(
                    webhook_server,
                    "send_to_telegram",
                    side_effect=lambda text, **_kwargs: telegram_messages.append(text) or True,
                ):
            payload = {
                "direction": "inbound",
                "call_direction": "inbound",
                "call_missed": True,
                "call_id": "call-123",
                "from_number": "+14322083277",
                "to_number": "+14155201316",
                "date_started": event_ts,
                "contact": {"name": "Payload Person"},
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_call_webhook(handler)

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        first_contact = hook_calls[0]["normalized_event"]["first_contact"]
        inbound_context = hook_calls[0]["normalized_event"]["inbound_context"]
        self.assertEqual(status["code"], 200)
        self.assertEqual(first_contact["identityState"], "payload_contact")
        self.assertFalse(first_contact["knownContact"])
        self.assertEqual(inbound_context["contactName"], "Payload Person")
        self.assertEqual(inbound_context["identityConfidence"], "low")
        self.assertFalse(inbound_context["contextDraftAllowed"])
        self.assertNotIn("exact_phone_match", inbound_context["evidence"])
        self.assertEqual(response["auto_reply_status"], "not_eligible")
        self.assertIsNone(response["auto_reply_draft_id"])
        self.assertIn("Inbound context", telegram_messages[0])
        self.assertNotIn("SMS approval draft", telegram_messages[0])


class VoicemailWebhookHandlerTests(unittest.TestCase):
    def setUp(self):
        webhook_server.sms_approval._EMERGENCY_OPT_OUT_MEMORY.clear()
        self.addCleanup(webhook_server.sms_approval._EMERGENCY_OPT_OUT_MEMORY.clear)

    def test_voicemail_webhook_requires_auth_when_secret_configured(self):
        with patch.object(webhook_server, "WEBHOOK_SECRET", "secret-123"):
            payload = {
                "from_number": "+14155550123",
                "to_number": ["+14155201316"],
                "duration": 19,
                "voicemail_transcription": "Please call me back.",
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_voicemail_webhook(handler)

        self.assertEqual(status["code"], 401)

    def test_voicemail_sales_auto_reply_creates_approval_draft(self):
        sms_calls = []
        telegram_messages = []

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            webhook_server.sms_approval,
            "DB_PATH",
            Path(temp_dir) / "approvals.db",
        ), patch.object(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True), patch.object(
            webhook_server,
            "DIALPAD_AUTO_REPLY_SALES_LINE",
            "4155201316",
        ), patch.object(
            webhook_server,
            "lookup_contact_enrichment",
            return_value={
                "contact_name": None,
                "first_name": None,
                "last_name": None,
                "company": None,
                "job_title": None,
                "status": "not_found",
                "degraded": False,
                "degraded_reason": None,
            },
        ), patch.object(
            webhook_server,
            "send_to_telegram",
            side_effect=lambda text: telegram_messages.append(text) or True,
        ), patch.object(
            webhook_server,
            "dialpad_send_sms",
            side_effect=lambda to_numbers, message, from_number=None, infer_country_code=False: sms_calls.append(
                {
                    "to_numbers": to_numbers,
                    "message": message,
                    "from_number": from_number,
                    "infer_country_code": infer_country_code,
                }
            ) or {"id": "msg-3", "message_status": "pending"},
        ):
            payload = {
                "from_number": "+14155550123",
                "to_number": ["+14155201316"],
                "duration": 19,
                "voicemail_transcription": "Please call me back.",
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_voicemail_webhook(handler)

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertEqual(len(telegram_messages), 1)
        self.assertEqual(sms_calls, [])
        self.assertFalse(response["auto_reply_sent"])
        self.assertEqual(response["auto_reply_status"], "draft_created")
        self.assertTrue(response["auto_reply_draft_id"])

    def test_voicemail_opt_out_transcription_blocks_draft_and_persists_opt_out(self):
        telegram_messages = []

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            webhook_server.sms_approval,
            "DB_PATH",
            Path(temp_dir) / "approvals.db",
        ), patch.object(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True), patch.object(
            webhook_server,
            "DIALPAD_AUTO_REPLY_SALES_LINE",
            "4155201316",
        ), patch.object(
            webhook_server,
            "lookup_contact_enrichment",
            return_value={
                "contact_name": None,
                "first_name": None,
                "last_name": None,
                "company": None,
                "job_title": None,
                "status": "not_found",
                "degraded": False,
                "degraded_reason": None,
            },
        ), patch.object(
            webhook_server,
            "send_to_telegram",
            side_effect=lambda text: telegram_messages.append(text) or True,
        ):
            payload = {
                "from_number": "+14155550123",
                "to_number": ["+14155201316"],
                "duration": 19,
                "voicemail_transcription": "STOP texting me.",
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_voicemail_webhook(handler)

            conn = webhook_server.sms_approval.init_db()
            try:
                opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
            finally:
                conn.close()

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertFalse(response["auto_reply_sent"])
        self.assertEqual(response["auto_reply_status"], "blocked_opt_out")
        self.assertIsNone(response["auto_reply_draft_id"])
        self.assertTrue(opted_out)
        self.assertEqual(len(telegram_messages), 1)
        self.assertIn("Automation blocked", telegram_messages[0])
        self.assertIn("human", telegram_messages[0])
        self.assertIn("No SMS approval draft", telegram_messages[0])

    def test_voicemail_opt_out_persistence_failure_returns_failed_status(self):
        customer_number = "+14155550987"
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"DIALPAD_SMS_APPROVAL_EMERGENCY_PATH": temp_dir},
        ), patch.object(
            webhook_server.sms_approval,
            "DB_PATH",
            Path(temp_dir) / "approvals.db",
        ), patch.object(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True), patch.object(
            webhook_server,
            "DIALPAD_AUTO_REPLY_SALES_LINE",
            "4155201316",
        ), patch.object(
            webhook_server,
            "lookup_contact_enrichment",
            return_value={
                "contact_name": None,
                "first_name": None,
                "last_name": None,
                "company": None,
                "job_title": None,
                "status": "not_found",
                "degraded": False,
                "degraded_reason": None,
            },
        ), patch.object(
            webhook_server,
            "send_to_telegram",
            return_value=True,
        ), patch.object(
            webhook_server.sms_approval,
            "mark_opt_out",
            side_effect=OSError("simulated approval db failure"),
        ):
            payload = {
                "from_number": customer_number,
                "to_number": ["+14155201316"],
                "duration": 19,
                "voicemail_transcription": "STOP texting me.",
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_voicemail_webhook(handler)

            conn = webhook_server.sms_approval.init_db()
            try:
                opted_out = webhook_server.sms_approval.is_opted_out(conn, customer_number)
            finally:
                conn.close()

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertEqual(response["auto_reply_status"], "opt_out_persistence_failed")
        self.assertIsNone(response["auto_reply_draft_id"])
        self.assertTrue(opted_out)

    def test_known_contact_voicemail_opt_out_persists_even_when_reply_not_eligible(self):
        telegram_messages = []
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            webhook_server.sms_approval,
            "DB_PATH",
            Path(temp_dir) / "approvals.db",
        ), patch.object(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True), patch.object(
            webhook_server,
            "DIALPAD_AUTO_REPLY_SALES_LINE",
            "4155201316",
        ), patch.object(
            webhook_server,
            "lookup_contact_enrichment",
            return_value={
                "contact_name": "Jane Doe",
                "first_name": "Jane",
                "last_name": "Doe",
                "company": "Example Co",
                "job_title": "Owner",
                "status": "resolved",
                "degraded": False,
                "degraded_reason": None,
            },
        ), patch.object(
            webhook_server,
            "send_to_telegram",
            side_effect=lambda text: telegram_messages.append(text) or True,
        ):
            payload = {
                "from_number": "+14155550123",
                "to_number": ["+14155201316"],
                "duration": 19,
                "voicemail_transcription": "Please stop texting me.",
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_voicemail_webhook(handler)

            conn = webhook_server.sms_approval.init_db()
            try:
                opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
            finally:
                conn.close()

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status["code"], 200)
        self.assertEqual(response["auto_reply_status"], "blocked_opt_out")
        self.assertIsNone(response["auto_reply_draft_id"])
        self.assertTrue(opted_out)
        self.assertEqual(len(telegram_messages), 1)
        self.assertIn("Automation blocked", telegram_messages[0])
        self.assertIn("No SMS approval draft", telegram_messages[0])

    def test_voicemail_risky_transcription_creates_risky_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            webhook_server.sms_approval,
            "DB_PATH",
            Path(temp_dir) / "approvals.db",
        ), patch.object(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True), patch.object(
            webhook_server,
            "DIALPAD_AUTO_REPLY_SALES_LINE",
            "4155201316",
        ), patch.object(
            webhook_server,
            "lookup_contact_enrichment",
            return_value={
                "contact_name": None,
                "first_name": None,
                "last_name": None,
                "company": None,
                "job_title": None,
                "status": "not_found",
                "degraded": False,
                "degraded_reason": None,
            },
        ), patch.object(webhook_server, "send_to_telegram", return_value=True):
            payload = {
                "from_number": "+14155550123",
                "to_number": ["+14155201316"],
                "duration": 19,
                "voicemail_transcription": "I need to talk to a real person.",
            }
            handler, status = _build_handler(payload)
            webhook_server.DialpadWebhookHandler.handle_voicemail_webhook(handler)

            response = json.loads(handler.wfile.getvalue().decode("utf-8"))
            conn = webhook_server.sms_approval.init_db()
            try:
                draft = webhook_server.sms_approval.get_draft(conn, response["auto_reply_draft_id"])
            finally:
                conn.close()

        self.assertEqual(status["code"], 200)
        self.assertEqual(response["auto_reply_status"], "draft_created")
        self.assertEqual(draft["risk_state"], webhook_server.sms_approval.RISK_RISKY)


if __name__ == "__main__":
    unittest.main()
