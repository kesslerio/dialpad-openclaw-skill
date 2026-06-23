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

    def test_post_ack_exception_keeps_claim_to_prevent_replay(self):
        # Invariant: never replay user-visible output. A post-ACK failure (which
        # may be AFTER a draft/hook/Telegram card already fired) must NOT release
        # the claim, so a Dialpad retry is suppressed rather than re-emitting.
        handler, status = _build_handler(_inbound("pa-1"))
        with patch.object(ws, "assess_inbound_sms_alert_eligibility",
                          MagicMock(side_effect=RuntimeError("boom"))):
            handler.handle_webhook()  # must not raise; 200 already sent
        self.assertEqual(status["code"], 200)
        again = ws.claim_sms_webhook_event(ws.sms_dedupe_key(_inbound("pa-1")), db_path=self.db)
        self.assertTrue(again["duplicate"])  # claim kept -> retry suppressed (no replay)


    def test_ack_write_failure_still_processes(self):
        # If Dialpad disconnects mid-ACK, the write raises — the handler must still
        # process (side effects once); Dialpad's retry hits the duplicate branch.
        handler, _ = _build_handler(_inbound("ackfail-1"))
        with patch.object(handler, "_ack_webhook_200",
                          MagicMock(side_effect=BrokenPipeError("client gone"))):
            handler.handle_webhook()  # must not raise
        self.assertEqual(self.assess.call_count, 1)


class MissedCallAckFirstTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = self.tmp.name
        self.patchers = [
            patch.object(ws, "verify_webhook_auth", lambda *a, **k: (True, "test")),
            patch.object(ws, "_missed_call_dedupe_db_path", lambda: Path(self.db)),
            patch.object(ws, "DIALPAD_AUTO_REPLY_ENABLED", False),
            patch.object(ws, "_fetch_recent_calls_around", return_value=[]),
            patch.object(ws, "send_to_openclaw_hooks", return_value=(False, "disabled_by_config")),
            patch.object(ws, "send_to_telegram", return_value=True),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        Path(self.db).unlink(missing_ok=True)

    def _missed_call(self, call_id):
        return {
            "direction": "inbound",
            "call_direction": "inbound",
            "call_missed": True,
            "call_id": call_id,
            "from_number": "+14155550123",
            "to_number": "+14155201316",
            "date_started": 1760000000000,
        }

    def test_missed_call_ack_written_before_enrichment_lookup(self):
        handler, status = _build_handler(self._missed_call("call-ack-1"))
        seen = {}

        def _probe(_number):
            seen["ack_len"] = len(handler.wfile.getvalue())
            return {"contact_name": None, "status": "not_found", "degraded": False, "degraded_reason": None}

        with patch.object(ws, "lookup_contact_enrichment", side_effect=_probe):
            handler.handle_call_webhook()

        self.assertEqual(status["code"], 200)
        self.assertIn('"processing": "async"', handler.wfile.getvalue().decode())
        self.assertGreater(seen.get("ack_len", 0), 0)

    def test_missed_call_ack_written_before_history_backfill(self):
        payload = self._missed_call("call-ack-history-1")
        payload.pop("from_number")
        payload["contact"] = {"phone": "+14155550123"}
        handler, status = _build_handler(payload)
        seen = {}

        def _history(_event_ts_ms):
            seen["ack_len"] = len(handler.wfile.getvalue())
            return []

        with patch.object(ws, "_fetch_recent_calls_around", side_effect=_history):
            handler.handle_call_webhook()

        self.assertEqual(status["code"], 200)
        self.assertIn('"processing": "async"', handler.wfile.getvalue().decode())
        self.assertGreater(seen.get("ack_len", 0), 0)

    def test_duplicate_missed_call_acks_without_side_effects(self):
        lookup = MagicMock(return_value={
            "contact_name": None,
            "status": "not_found",
            "degraded": False,
            "degraded_reason": None,
        })
        with patch.object(ws, "lookup_contact_enrichment", lookup):
            first, _ = _build_handler(self._missed_call("call-dup-1"))
            first.handle_call_webhook()
            second, status = _build_handler(self._missed_call("call-dup-1"))
            second.handle_call_webhook()

        self.assertEqual(status["code"], 200)
        self.assertIn('"processing": "duplicate"', second.wfile.getvalue().decode())
        self.assertEqual(lookup.call_count, 1)

    def test_backfilled_missed_call_duplicate_stops_before_side_effects(self):
        first_payload = {
            "direction": "inbound",
            "call_direction": "inbound",
            "call_missed": True,
            "contact": {"phone": "+14155550123"},
            "date_started": 1760000000000,
            "webhook_event_id": "parent",
        }
        second_payload = {
            **first_payload,
            "webhook_event_id": "child",
            "event": {"delivery": "retry"},
        }
        history_row = {
            "direction": "inbound",
            "state": "missed",
            "duration": 0,
            "date_started": 1760000000500,
            "external_number": "+14155550123",
            "entry_point_target": {"phone": "+14155201316", "name": "Sales"},
        }
        lookup = MagicMock(return_value={
            "contact_name": None,
            "status": "not_found",
            "degraded": False,
            "degraded_reason": None,
        })
        hooks = MagicMock(return_value=(False, "disabled_by_config"))
        telegram = MagicMock(return_value=True)

        with patch.object(ws, "_fetch_recent_calls_around", return_value=[history_row]), \
                patch.object(ws, "lookup_contact_enrichment", lookup), \
                patch.object(ws, "send_to_openclaw_hooks", hooks), \
                patch.object(ws, "send_to_telegram", telegram):
            first, first_status = _build_handler(first_payload)
            first.handle_call_webhook()
            second, second_status = _build_handler(second_payload)
            second.handle_call_webhook()

        self.assertEqual(first_status["code"], 200)
        self.assertEqual(second_status["code"], 200)
        self.assertIn('"processing": "async"', first.wfile.getvalue().decode())
        self.assertIn('"processing": "async"', second.wfile.getvalue().decode())
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(hooks.call_count, 1)
        self.assertEqual(telegram.call_count, 1)


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
