"""Unit tests for SMS webhook idempotency (plan U6). Storage isolated to a temp DB."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import webhook_server as ws  # noqa: E402


class ClaimSmsWebhookEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = self.tmp.name

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def claim(self, message_id, now_ms=1_000_000):
        return ws.claim_sms_webhook_event(message_id, db_path=self.db, now_ms=now_ms)

    def test_first_claim_then_duplicate(self):
        first = self.claim("msg-1")
        self.assertTrue(first["claimed"])
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["status"], "claimed")

        second = self.claim("msg-1")
        self.assertFalse(second["claimed"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["status"], "duplicate")

    def test_distinct_message_ids_both_claim(self):
        self.assertTrue(self.claim("msg-a")["claimed"])
        self.assertTrue(self.claim("msg-b")["claimed"])

    def test_at_most_once_across_many_claims(self):
        # The delivery-correctness invariant: N deliveries of one message_id -> one claim.
        results = [self.claim("msg-repeat") for _ in range(6)]
        claimed = [r for r in results if r["claimed"] and not r["duplicate"]]
        self.assertEqual(len(claimed), 1)

    def test_at_most_once_under_concurrency(self):
        # ThreadingHTTPServer means concurrent retries hit claim() on separate
        # threads/connections. INSERT OR IGNORE + busy_timeout must still yield
        # exactly one winner and zero raised exceptions.
        import threading
        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            try:
                r = ws.claim_sms_webhook_event("concurrent-1", db_path=self.db)
            except Exception as exc:  # noqa: BLE001 - record, don't swallow silently
                with lock:
                    errors.append(exc)
                return
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        winners = [r for r in results if r["claimed"] and not r["duplicate"]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(results), 12)

    def test_missing_message_id_fails_open(self):
        for bad in ("", None):
            r = self.claim(bad)
            self.assertTrue(r["claimed"])  # fail open: never block the webhook
            self.assertEqual(r["status"], "key_missing")

    def test_retention_purges_old_entries(self):
        t0 = 1_000_000
        self.assertTrue(self.claim("msg-old", now_ms=t0)["claimed"])
        # same id after the retention window -> old row purged, claimable again
        later = t0 + ws.SMS_DEDUPE_RETENTION_MS + 1
        self.assertTrue(self.claim("msg-old", now_ms=later)["claimed"])

    def test_fail_open_when_storage_unavailable(self):
        # an unwritable db path must fail open (claimed=True), not raise
        r = ws.claim_sms_webhook_event("msg-x", db_path="/proc/cannot/write/here.db", now_ms=1)
        self.assertTrue(r["claimed"])
        self.assertEqual(r["status"], "dedupe_unavailable")


class SmsDedupeKeyTests(unittest.TestCase):
    def test_prefers_message_id(self):
        self.assertEqual(ws.sms_dedupe_key({"message_id": "M1", "id": "X"}), "M1")

    def test_falls_back_to_id(self):
        self.assertEqual(ws.sms_dedupe_key({"id": "X9"}), "X9")

    def test_synthesizes_stable_key_when_no_id(self):
        payload = {"from_number": "+14155550123", "to_number": "+14155201316",
                   "created_date": "2026-06-19T09:00:00Z", "text": "hello"}
        k1 = ws.sms_dedupe_key(payload)
        k2 = ws.sms_dedupe_key(dict(payload))
        self.assertTrue(k1.startswith("sms-synth:"))
        self.assertEqual(k1, k2)  # deterministic -> retries of an id-less payload dedupe

    def test_synthesized_key_differs_by_text(self):
        base = {"from_number": "+1", "to_number": "+2", "created_date": "t"}
        self.assertNotEqual(
            ws.sms_dedupe_key({**base, "text": "a"}),
            ws.sms_dedupe_key({**base, "text": "b"}),
        )

    def test_synthesized_key_claims_and_dedupes(self):
        payload = {"from_number": "+1", "to_number": "+2", "created_date": "t", "text": "hi"}
        key = ws.sms_dedupe_key(payload)
        import tempfile
        from pathlib import Path as _Path
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            self.assertTrue(ws.claim_sms_webhook_event(key, db_path=tmp.name)["claimed"])
            self.assertTrue(ws.claim_sms_webhook_event(key, db_path=tmp.name)["duplicate"])
        finally:
            _Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
