from __future__ import annotations

import tempfile
import time
import unittest
import io
import json
import sqlite3
from pathlib import Path

import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "bin"))

import create_sms_webhook
import sms_sqlite
import webhook_server
from webhook_sqlite import classify_sms_webhook_event, handle_sms_webhook


class SmsDeliveryEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "sms.db"
        self.original_db_path = sms_sqlite.DB_PATH
        sms_sqlite.DB_PATH = self.db_path
        self.addCleanup(lambda: setattr(sms_sqlite, "DB_PATH", self.original_db_path))

    def _message(self, *, message_id: int = 12345, event_timestamp: int | None = None) -> dict:
        return {
            "id": message_id,
            "created_date": event_timestamp or 1_730_000_000_000,
            "event_timestamp": event_timestamp or 1_730_000_000_000,
            "direction": "outbound",
            "from_number": "+14155550100",
            "to_number": ["+14155550200"],
            "text": "The message body must survive receipt updates.",
            "message_status": "pending",
        }

    def test_receipt_updates_in_place_and_preserves_message_truth(self) -> None:
        now_ms = int(time.time() * 1000)
        stored = handle_sms_webhook(self._message(event_timestamp=now_ms - 1_000))
        self.assertTrue(stored["stored"])

        result = handle_sms_webhook({
            "id": 12345,
            "event_timestamp": now_ms,
            "message_status": "delivered",
            "message_delivery_result": "success",
        })

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["updated"])
        self.assertEqual(result["outcome"], "delivered")

        conn = sms_sqlite.init_db()
        try:
            row = conn.execute(
                "SELECT id, text, from_number, to_number, message_status, delivery_result, "
                "delivery_event_timestamp FROM messages WHERE dialpad_id = ?",
                (12345,),
            ).fetchone()
            self.assertEqual(row["id"], stored["message"]["id"])
            self.assertEqual(row["text"], "The message body must survive receipt updates.")
            self.assertEqual(row["from_number"], "+14155550100")
            self.assertEqual(row["to_number"], "+14155550200")
            self.assertEqual(row["message_status"], "delivered")
            self.assertEqual(row["delivery_result"], "success")
            self.assertEqual(row["delivery_event_timestamp"], now_ms)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
                    ("survive",),
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_receipt_replay_and_stale_event_are_noops(self) -> None:
        now_ms = int(time.time() * 1000)
        handle_sms_webhook(self._message(event_timestamp=now_ms - 2_000))
        receipt = {
            "id": 12345,
            "event_timestamp": now_ms,
            "message_status": "delivered",
            "message_delivery_result": "success",
        }
        first = handle_sms_webhook(receipt)
        duplicate = handle_sms_webhook(receipt)
        stale = handle_sms_webhook({**receipt, "event_timestamp": now_ms - 1})

        self.assertTrue(first["updated"])
        self.assertFalse(duplicate["updated"])
        self.assertEqual(duplicate["reason"], "duplicate")
        self.assertFalse(stale["updated"])
        self.assertEqual(stale["reason"], "stale")

    def test_out_of_window_and_same_timestamp_conflict_do_not_mutate(self) -> None:
        now_ms = int(time.time() * 1000)
        handle_sms_webhook(self._message(event_timestamp=now_ms - 1_000))
        receipt = {
            "id": 12345,
            "event_timestamp": now_ms,
            "message_status": "delivered",
            "message_delivery_result": "success",
        }
        self.assertTrue(handle_sms_webhook(receipt)["updated"])
        same_time_conflict = handle_sms_webhook({
            **receipt,
            "message_status": "failed",
            "message_delivery_result": "invalid_destination",
        })
        expired = handle_sms_webhook({
            **receipt,
            "event_timestamp": now_ms - sms_sqlite.DELIVERY_EVENT_MAX_AGE_MS - 1,
        })
        self.assertEqual(same_time_conflict["status"], "conflict")
        self.assertEqual(expired["reason"], "event_timestamp_out_of_window")
        conn = sms_sqlite.init_db()
        try:
            row = conn.execute(
                "SELECT message_status, delivery_result, delivery_event_timestamp "
                "FROM messages WHERE dialpad_id = 12345"
            ).fetchone()
            self.assertEqual(tuple(row), ("delivered", "success", now_ms))
        finally:
            conn.close()

    def test_unallowlisted_status_result_pairs_fail_closed(self) -> None:
        self.assertEqual(
            sms_sqlite.classify_delivery_status("pending", "mystery")["outcome"],
            "delivery_unknown",
        )
        self.assertEqual(
            sms_sqlite.classify_delivery_status("failed", "mystery")["outcome"],
            "delivery_unknown",
        )

    def test_existing_database_gets_additive_timestamp_migration(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, dialpad_id INTEGER UNIQUE, "
            "contact_number TEXT NOT NULL, contact_name TEXT, direction TEXT, from_number TEXT, "
            "to_number TEXT, text TEXT, message_status TEXT, delivery_result TEXT, mms BOOLEAN, "
            "mms_url TEXT, timestamp INTEGER, received_at TEXT, read BOOLEAN)"
        )
        conn.execute(
            "INSERT INTO messages(dialpad_id, contact_number, direction, text, message_status) "
            "VALUES (55, '+14155550100', 'outbound', 'legacy', 'pending')"
        )
        conn.commit()
        conn.close()

        migrated = sms_sqlite.init_db()
        try:
            columns = {row[1] for row in migrated.execute("PRAGMA table_info(messages)").fetchall()}
            self.assertIn("delivery_event_timestamp", columns)
            row = migrated.execute(
                "SELECT text, delivery_event_timestamp FROM messages WHERE dialpad_id = 55"
            ).fetchone()
            self.assertEqual(row["text"], "legacy")
            self.assertIsNone(row["delivery_event_timestamp"])
        finally:
            migrated.close()

    def test_failure_results_and_terminal_conflicts_fail_closed(self) -> None:
        now_ms = int(time.time() * 1000)
        handle_sms_webhook(self._message(event_timestamp=now_ms - 1_000))
        failed = handle_sms_webhook({
            "id": 12345,
            "event_timestamp": now_ms,
            "message_status": "failed",
            "message_delivery_result": "invalid_destination",
        })
        conflict = handle_sms_webhook({
            "id": 12345,
            "event_timestamp": now_ms + 1_000,
            "message_status": "delivered",
            "message_delivery_result": "success",
        })

        self.assertEqual(failed["outcome"], "undelivered")
        self.assertTrue(failed["updated"])
        self.assertEqual(conflict["status"], "conflict")
        self.assertFalse(conflict["updated"])

    def test_invalid_receipts_do_not_mutate_or_claim_full_message_path(self) -> None:
        now_ms = int(time.time() * 1000)
        missing_time = handle_sms_webhook({
            "id": 12345,
            "message_status": "delivered",
        })
        unknown_id = handle_sms_webhook({
            "id": 99999,
            "event_timestamp": now_ms,
            "message_status": "delivered",
        })
        future = handle_sms_webhook({
            "id": 12345,
            "event_timestamp": now_ms + 10 * 60 * 1000,
            "message_status": "delivered",
        })

        self.assertEqual(missing_time["status"], "error")
        self.assertEqual(unknown_id["status"], "not_found")
        self.assertEqual(future["status"], "error")
        self.assertEqual(classify_sms_webhook_event({"id": 12345, "message_status": "delivered"}), "delivery_status")
        self.assertEqual(
            classify_sms_webhook_event({
                "id": 12345,
                "message_status": "delivered",
                "text": "complete event",
                "direction": "outbound",
                "from_number": "+14155550100",
                "to_number": ["+14155550200"],
            }),
            "full_message",
        )
        self.assertEqual(
            classify_sms_webhook_event({"id": 12345, "message_status": "delivered", "text": "partial"}),
            "rejected",
        )

    def test_subscription_creation_requests_delivery_status_events(self) -> None:
        args = type("Args", (), {
            "events": None,
            "url": "https://example.test/webhook/dialpad",
            "office_id": None,
            "direction": "all",
            "json": True,
        })()
        with patch.object(
            create_sms_webhook,
            "run_generated_json",
            side_effect=[
                {"id": 42, "hook_url": args.url},
                {"id": "sub-1", "direction": "all", "enabled": True},
            ],
        ) as run_generated:
            create_sms_webhook.handle_create(args)

        subscription_command = run_generated.call_args_list[1].args[0]
        payload = json.loads(subscription_command[subscription_command.index("--data") + 1])
        self.assertTrue(payload["status"])
        self.assertEqual(payload["endpoint_id"], 42)
        self.assertEqual(payload["direction"], "all")

    def test_http_receipt_updates_without_dedupe_or_post_ack_fanout(self) -> None:
        now_ms = int(time.time() * 1000)
        handle_sms_webhook(self._message(event_timestamp=now_ms - 1_000))
        payload = {
            "id": 12345,
            "event_timestamp": now_ms,
            "message_status": "delivered",
            "message_delivery_result": "success",
        }
        raw = json.dumps(payload).encode("utf-8")
        handler = object.__new__(webhook_server.DialpadWebhookHandler)
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 12345)
        status = {"code": None}
        handler.send_response = lambda code: status.__setitem__("code", code)
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, *_args: status.__setitem__("code", code)

        with patch.object(webhook_server, "verify_webhook_auth", return_value=(True, "test")), \
                patch.object(webhook_server, "claim_sms_webhook_event") as claim, \
                patch.object(webhook_server.DialpadWebhookHandler, "_process_inbound_post_ack") as post_ack:
            handler.handle_webhook()

        self.assertEqual(status["code"], 200)
        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(response["event_type"], "delivery_status")
        self.assertTrue(response["updated"])
        claim.assert_not_called()
        post_ack.assert_not_called()

    def test_store_endpoint_rejects_unauthenticated_receipt_mutation(self) -> None:
        now_ms = int(time.time() * 1000)
        handle_sms_webhook(self._message(event_timestamp=now_ms - 1_000))
        payload = {
            "id": 12345,
            "event_timestamp": now_ms,
            "message_status": "delivered",
            "message_delivery_result": "success",
        }
        raw = json.dumps(payload).encode("utf-8")
        handler = object.__new__(webhook_server.DialpadWebhookHandler)
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.wfile = io.BytesIO()
        status = {"code": None}
        handler.send_response = lambda code: status.__setitem__("code", code)
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, *_args: status.__setitem__("code", code)

        handler.handle_store()

        self.assertEqual(status["code"], 403)
        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(response["error"], "delivery_status_requires_authenticated_webhook")
        conn = sms_sqlite.init_db()
        try:
            row = conn.execute(
                "SELECT message_status, delivery_result, delivery_event_timestamp "
                "FROM messages WHERE dialpad_id = 12345"
            ).fetchone()
            self.assertEqual(tuple(row), ("pending", None, now_ms - 1_000))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
