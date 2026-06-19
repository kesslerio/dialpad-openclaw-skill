"""Handler-level tests for ACK-first + SMS idempotency (plan U5/U6).

Verifies the inbound webhook acknowledges Dialpad with a 200 before the slow
side-effectful processing, and that a duplicate Dialpad message_id ACKs without
re-running that processing.
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import webhook_server as ws  # noqa: E402


def _build_handler(payload):
    raw = json.dumps(payload).encode("utf-8")
    handler = object.__new__(ws.DialpadWebhookHandler)
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 12345)
    status = {"code": None}
    handler.send_response = lambda code: status.__setitem__("code", code)
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    handler.send_error = lambda code, *_: status.__setitem__("code", code)
    return handler, status


def _inbound(message_id):
    return {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": "+14155201316",
        "text": "hi there",
        "message_id": message_id,
    }


class AckFirstIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = self.tmp.name
        # eligibility is the first processing step after the ACK -> use it as the
        # "did processing run?" probe. Return a non-eligible decision to keep the
        # downstream path minimal.
        self.assess = MagicMock(return_value={
            "eligible": False,
            "reason_code": "blocked_test",
            "sensitive_filtered": False,
            "notification_type": "inbound",
        })
        self.patchers = [
            patch.object(ws, "verify_webhook_auth", lambda *a, **k: (True, "test")),
            patch.object(ws, "handle_sms_webhook", lambda data: {"stored": True, "message": {}}),
            patch.object(ws, "lookup_contact_enrichment", lambda n: {"contact_name": None, "status": "not_found"}),
            patch.object(ws, "apply_payload_contact_fallback", lambda enr, data: enr),
            patch.object(ws, "invalidate_pending_sms_drafts", lambda **k: None),
            patch.object(ws, "send_to_telegram", lambda *a, **k: None),
            patch.object(ws, "assess_inbound_sms_alert_eligibility", self.assess),
            patch.object(ws, "_sms_dedupe_db_path", lambda: Path(self.db)),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        Path(self.db).unlink(missing_ok=True)

    def test_new_message_acks_async_and_runs_processing(self):
        handler, status = _build_handler(_inbound("msg-1"))
        handler.handle_webhook()
        self.assertEqual(status["code"], 200)
        self.assertIn('"processing": "async"', handler.wfile.getvalue().decode())
        self.assertEqual(self.assess.call_count, 1)  # processing ran

    def test_duplicate_message_acks_without_reprocessing(self):
        first, _ = _build_handler(_inbound("dup-1"))
        first.handle_webhook()
        self.assertEqual(self.assess.call_count, 1)

        second, status = _build_handler(_inbound("dup-1"))
        second.handle_webhook()
        self.assertEqual(status["code"], 200)
        self.assertIn('"processing": "duplicate"', second.wfile.getvalue().decode())
        # processing must NOT run again for the duplicate -> still 1
        self.assertEqual(self.assess.call_count, 1)

    def test_distinct_messages_both_process(self):
        for mid in ("a", "b"):
            h, _ = _build_handler(_inbound(mid))
            h.handle_webhook()
        self.assertEqual(self.assess.call_count, 2)

    def test_ack_written_before_processing_runs(self):
        # The ACK body must already be on the wire when processing begins.
        handler, _ = _build_handler(_inbound("ord-1"))
        seen = {}

        def _probe(*a, **k):
            seen["ack_len"] = len(handler.wfile.getvalue())
            return {"eligible": False, "reason_code": "blocked", "sensitive_filtered": False,
                    "notification_type": "inbound"}

        with patch.object(ws, "assess_inbound_sms_alert_eligibility", _probe):
            handler.handle_webhook()
        self.assertGreater(seen.get("ack_len", 0), 0)

    def test_outbound_acks_once_without_claim(self):
        handler, status = _build_handler({
            "direction": "outbound", "from_number": "+14155201316",
            "to_number": "+14155550123", "text": "hi", "message_id": "out-1",
        })
        with patch.object(ws, "sms_approval", None):
            handler.handle_webhook()
        self.assertEqual(status["code"], 200)
        self.assertIn('"processing": "async"', handler.wfile.getvalue().decode())

    def test_storage_failure_sends_500_and_releases_claim(self):
        handler, status = _build_handler(_inbound("sf-1"))
        with patch.object(ws, "handle_sms_webhook", lambda data: {"stored": False, "error": "boom"}):
            handler.handle_webhook()
        self.assertEqual(status["code"], 500)  # send_error, not a 200 ACK
        self.assertNotIn("processing", handler.wfile.getvalue().decode())  # no ACK body
        # claim was released, so a retry of the same message is NOT a duplicate
        again = ws.claim_sms_webhook_event(ws.sms_dedupe_key(_inbound("sf-1")), db_path=self.db)
        self.assertTrue(again["claimed"])
        self.assertFalse(again["duplicate"])

    def test_dedupe_unavailable_still_acks_and_processes(self):
        with patch.object(ws, "claim_sms_webhook_event",
                          lambda key, **k: {"claimed": True, "duplicate": False, "status": "dedupe_unavailable"}):
            handler, status = _build_handler(_inbound("fo-1"))
            handler.handle_webhook()
        self.assertEqual(status["code"], 200)
        self.assertIn('"processing": "async"', handler.wfile.getvalue().decode())
        self.assertEqual(self.assess.call_count, 1)  # fail-open -> processing runs

    def test_post_ack_exception_releases_claim_for_retry(self):
        # A post-ACK failure must release the claim so a Dialpad retry recovers.
        handler, status = _build_handler(_inbound("pa-1"))
        with patch.object(ws, "assess_inbound_sms_alert_eligibility",
                          MagicMock(side_effect=RuntimeError("boom"))):
            handler.handle_webhook()  # must not raise; 200 already sent
        self.assertEqual(status["code"], 200)
        again = ws.claim_sms_webhook_event(ws.sms_dedupe_key(_inbound("pa-1")), db_path=self.db)
        self.assertTrue(again["claimed"])
        self.assertFalse(again["duplicate"])


class ServerConfigTests(unittest.TestCase):
    def test_main_uses_threading_http_server(self):
        # ACK-first relies on per-request threads -> main() must instantiate
        # ThreadingHTTPServer, not the single-threaded HTTPServer.
        import inspect
        src = inspect.getsource(ws.main)
        self.assertIn("ThreadingHTTPServer(", src)
        self.assertNotIn("= HTTPServer(", src)


if __name__ == "__main__":
    unittest.main()
