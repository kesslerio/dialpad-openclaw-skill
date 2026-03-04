from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from webhook_server import (
    classify_inbound_notification,
    detect_reliable_missed_call_hint,
    extract_message_text,
    is_sensitive_message,
    resolve_missed_call_context,
)


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

    def test_text_content_fallback_used_when_text_is_blank(self):
        payload = {
            "direction": "inbound",
            "text": "   ",
            "text_content": "Real body",
        }
        self.assertEqual(extract_message_text(payload), "Real body")
        self.assertEqual(classify_inbound_notification(payload), "sms")


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

    def test_history_backfill_resolution(self):
        payload = {
            "date_started": 1760000000000,
            "event_type": "call.missed",
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
        self.assertEqual(resolved["caller_resolution_path"], "history_backfill")
        self.assertEqual(resolved["line_resolution_path"], "history_backfill")

    def test_unresolved_guard_behavior(self):
        payload = {"event_type": "call.missed", "timestamp": 1760000000000}
        resolved = resolve_missed_call_context(payload, history_fetcher=lambda _ts: [])
        self.assertEqual(resolved["from_number"], "Unknown")
        self.assertIsNone(resolved["line_display"])
        self.assertEqual(resolved["caller_resolution_path"], "unresolved")
        self.assertEqual(resolved["line_resolution_path"], "unresolved")


if __name__ == "__main__":
    unittest.main()
