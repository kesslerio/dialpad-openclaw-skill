from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import json
import os
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sms_approval


class SmsApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.original_db_path = sms_approval.DB_PATH
        self.original_emergency_path = os.environ.get("DIALPAD_SMS_APPROVAL_EMERGENCY_PATH")
        sms_approval.DB_PATH = Path(self.temp_dir.name) / "approvals.db"
        os.environ["DIALPAD_SMS_APPROVAL_EMERGENCY_PATH"] = str(
            Path(self.temp_dir.name) / "emergency-opt-outs.jsonl"
        )
        sms_approval._EMERGENCY_OPT_OUT_MEMORY.clear()
        self.addCleanup(self._restore_db_path)
        self.addCleanup(self._restore_emergency_path)
        self.conn = sms_approval.init_db()
        self.addCleanup(self.conn.close)

    def _restore_db_path(self):
        sms_approval.DB_PATH = self.original_db_path

    def _restore_emergency_path(self):
        sms_approval._EMERGENCY_OPT_OUT_MEMORY.clear()
        if self.original_emergency_path is None:
            os.environ.pop("DIALPAD_SMS_APPROVAL_EMERGENCY_PATH", None)
        else:
            os.environ["DIALPAD_SMS_APPROVAL_EMERGENCY_PATH"] = self.original_emergency_path

    def _draft(self, **kwargs):
        params = {
            "thread_key": "thread-1",
            "customer_number": "+15125550100",
            "sender_number": "+14155201316",
            "draft_text": "See you at 2:30 PM Central.",
        }
        params.update(kwargs)
        return sms_approval.create_draft(self.conn, **params)

    def test_approve_normal_draft_sends_exact_stored_text(self):
        draft = self._draft()
        calls = []

        def fake_send(to_numbers, message, from_number=None):
            calls.append((to_numbers, message, from_number))
            return {"id": "sms-1", "message_status": "pending"}

        result = sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="12345",
            actor_username="operator",
            send_func=fake_send,
        )

        self.assertTrue(result["sent"])
        self.assertEqual(result["dialpad_sms_id"], "sms-1")
        self.assertEqual(calls, [(["+15125550100"], "See you at 2:30 PM Central.", "+14155201316")])
        stored = sms_approval.get_draft(self.conn, draft["draft_id"])
        self.assertEqual(stored["status"], sms_approval.STATUS_SENT)
        self.assertEqual(stored["approved_by"], "12345")

    def test_stale_draft_does_not_send(self):
        draft = self._draft()
        sms_approval.invalidate_pending(self.conn, thread_key="thread-1", reason="newer_inbound")

        result = sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="12345",
            send_func=lambda *_args, **_kwargs: self.fail("send should not run"),
        )

        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["reason"], "newer_inbound")

    def test_reject_draft_marks_exact_draft_rejected_without_sending(self):
        draft = self._draft()

        result = sms_approval.reject_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="12345",
            actor_username="operator",
            rejected_at_ms=1000,
        )

        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], sms_approval.STATUS_REJECTED)
        stored = sms_approval.get_draft(self.conn, draft["draft_id"])
        self.assertEqual(stored["status"], sms_approval.STATUS_REJECTED)
        self.assertEqual(stored["invalidated_reason"], "operator_rejected")
        self.assertEqual(stored["rejected_by"], "12345")
        self.assertEqual(stored["rejected_username"], "operator")
        self.assertEqual(stored["rejected_at_ms"], 1000)

    def test_reject_draft_rejects_bot_actor_without_mutating(self):
        draft = self._draft()

        result = sms_approval.reject_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="bot",
        )

        self.assertEqual(result["status"], "blocked_actor")
        stored = sms_approval.get_draft(self.conn, draft["draft_id"])
        self.assertEqual(stored["status"], sms_approval.STATUS_PENDING)

    def test_risky_draft_requires_second_confirmation(self):
        draft = self._draft(
            risk_state=sms_approval.RISK_RISKY,
            risk_reason="customer asked for a real person",
        )

        first = sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="12345",
            send_func=lambda *_args, **_kwargs: self.fail("first step should not send"),
        )
        self.assertEqual(first["status"], "risky_confirmation_required")
        self.assertFalse(first["sent"])

        second = sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="67890",
            action=sms_approval.ACTION_CONFIRM_RISK,
            send_func=lambda *_args, **_kwargs: {"id": "sms-risk", "status": "queued"},
        )
        self.assertTrue(second["sent"])
        self.assertEqual(second["dialpad_sms_id"], "sms-risk")

    def test_risky_draft_direct_confirm_first_still_does_not_send(self):
        draft = self._draft(
            risk_state=sms_approval.RISK_RISKY,
            risk_reason="customer asked for a real person",
        )

        result = sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="12345",
            action=sms_approval.ACTION_CONFIRM_RISK,
            send_func=lambda *_args, **_kwargs: self.fail("direct confirm should not send"),
        )

        self.assertEqual(result["status"], "risky_confirmation_required")
        self.assertFalse(result["sent"])
        stored = sms_approval.get_draft(self.conn, draft["draft_id"])
        self.assertEqual(stored["status"], sms_approval.STATUS_PENDING)

    def test_risky_draft_repeated_first_step_preserves_original_confirmer(self):
        draft = self._draft(
            risk_state=sms_approval.RISK_RISKY,
            risk_reason="customer asked for a real person",
        )

        sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="12345",
            actor_username="first",
            send_func=lambda *_args, **_kwargs: self.fail("first step should not send"),
            approved_at_ms=1000,
        )
        sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="67890",
            actor_username="second",
            send_func=lambda *_args, **_kwargs: self.fail("repeat first step should not send"),
            approved_at_ms=2000,
        )

        stored = sms_approval.get_draft(self.conn, draft["draft_id"])
        self.assertEqual(stored["first_confirmed_by"], "12345")
        self.assertEqual(stored["first_confirmed_username"], "first")
        self.assertEqual(stored["first_confirmed_at_ms"], 1000)

    def test_bot_actor_is_rejected(self):
        draft = self._draft()
        result = sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="bot",
            send_func=lambda *_args, **_kwargs: self.fail("send should not run"),
        )
        self.assertEqual(result["status"], "blocked_actor")
        self.assertFalse(result["sent"])

    def test_failed_send_remains_unsent(self):
        draft = self._draft()

        def fake_send(*_args, **_kwargs):
            raise RuntimeError("Dialpad unavailable")

        result = sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="12345",
            send_func=fake_send,
        )

        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], sms_approval.STATUS_FAILED)
        stored = sms_approval.get_draft(self.conn, draft["draft_id"])
        self.assertEqual(stored["status"], sms_approval.STATUS_FAILED)
        self.assertEqual(stored["dialpad_sms_id"], None)
        self.assertIn("Dialpad unavailable", stored["send_error"])

    def test_failed_delivery_status_remains_unsent(self):
        draft = self._draft()

        result = sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="12345",
            send_func=lambda *_args, **_kwargs: {"id": "sms-1", "message_status": "failed"},
        )

        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], sms_approval.STATUS_FAILED)
        self.assertEqual(result["error"], "delivery_status_failed")
        stored = sms_approval.get_draft(self.conn, draft["draft_id"])
        self.assertEqual(stored["status"], sms_approval.STATUS_FAILED)
        self.assertEqual(stored["dialpad_sms_id"], "sms-1")
        self.assertEqual(stored["delivery_status"], "failed")

    def test_malformed_send_response_without_sms_id_remains_unsent(self):
        draft = self._draft()

        result = sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="12345",
            send_func=lambda *_args, **_kwargs: {"message_status": "queued"},
        )

        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], sms_approval.STATUS_FAILED)
        self.assertEqual(result["error"], "missing_dialpad_sms_id")
        stored = sms_approval.get_draft(self.conn, draft["draft_id"])
        self.assertEqual(stored["status"], sms_approval.STATUS_FAILED)
        self.assertEqual(stored["send_error"], "missing_dialpad_sms_id")

    def test_concurrent_approvals_only_send_once(self):
        draft = self._draft()
        send_calls = []
        barrier = threading.Barrier(2)

        def approve_from_new_connection(actor_id):
            conn = sms_approval.init_db(sms_approval.DB_PATH)
            try:
                barrier.wait(timeout=2)
                return sms_approval.approve_draft(
                    conn,
                    draft_id=draft["draft_id"],
                    actor_id=actor_id,
                    send_func=lambda *_args, **_kwargs: send_calls.append(actor_id) or {"id": f"sms-{actor_id}"},
                )
            finally:
                conn.close()

        results = []
        threads = [
            threading.Thread(target=lambda actor_id=actor_id: results.append(approve_from_new_connection(actor_id)))
            for actor_id in ("12345", "67890")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(send_calls), 1)
        self.assertEqual(sum(1 for result in results if result.get("sent")), 1)

    def test_concurrent_replacement_drafts_leave_one_pending(self):
        barrier = threading.Barrier(2)

        def create_from_new_connection(suffix):
            conn = sms_approval.init_db(sms_approval.DB_PATH)
            try:
                barrier.wait(timeout=2)
                return sms_approval.create_replacement_draft(
                    conn,
                    invalidate_thread_key="thread-1",
                    invalidate_customer_number="+15125550100",
                    thread_key="thread-1",
                    customer_number="+15125550100",
                    sender_number="+14155201316",
                    draft_text=f"Replacement draft {suffix}.",
                )
            finally:
                conn.close()

        results = []
        threads = [
            threading.Thread(target=lambda suffix=suffix: results.append(create_from_new_connection(suffix)))
            for suffix in ("A", "B")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        rows = self.conn.execute(
            "SELECT draft_id, status FROM sms_approval_drafts WHERE thread_key = ?",
            ("thread-1",),
        ).fetchall()
        pending = [row for row in rows if row["status"] == sms_approval.STATUS_PENDING]

        self.assertEqual(len(results), 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(pending), 1)

    def test_opted_out_customer_cannot_get_new_or_approved_drafts(self):
        sms_approval.mark_opt_out(
            self.conn,
            customer_number="+15125550100",
            reason="customer_opt_out",
            source="test",
        )

        with self.assertRaisesRegex(ValueError, "opted out"):
            self._draft()

        other_draft = sms_approval.create_draft(
            self.conn,
            thread_key="thread-2",
            customer_number="+15125550101",
            sender_number="+14155201316",
            draft_text="Temporary draft.",
        )
        sms_approval.mark_opt_out(
            self.conn,
            customer_number="+15125550101",
            reason="customer_opt_out",
            source="test",
        )
        result = sms_approval.approve_draft(
            self.conn,
            draft_id=other_draft["draft_id"],
            actor_id="12345",
            send_func=lambda *_args, **_kwargs: self.fail("send should not run"),
        )

        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["sent"])

    def test_emergency_opt_out_blocks_new_and_existing_drafts(self):
        draft = self._draft()
        sms_approval.record_emergency_opt_out(
            customer_number="+15125550100",
            reason="customer_opt_out",
            source="test_failure",
        )

        result = sms_approval.approve_draft(
            self.conn,
            draft_id=draft["draft_id"],
            actor_id="12345",
            send_func=lambda *_args, **_kwargs: self.fail("send should not run"),
        )

        self.assertEqual(result["status"], "blocked_opt_out")
        self.assertFalse(result["sent"])
        with self.assertRaisesRegex(ValueError, "opted out"):
            self._draft(thread_key="thread-2")

    def test_create_draft_cli_persists_without_sending(self):
        db_path = Path(self.temp_dir.name) / "cli-approvals.db"
        env = {**os.environ, "DIALPAD_SMS_APPROVAL_DB": str(db_path)}

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "bin" / "create_sms_draft.py"),
                "--thread-key",
                "cli-thread",
                "--to",
                "+15125550100",
                "--from",
                "+14155201316",
                "--message",
                "Exact CLI draft.",
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["draft"]["draft_text"], "Exact CLI draft.")
        self.assertEqual(payload["data"]["draft"]["status"], sms_approval.STATUS_PENDING)

    def test_approve_draft_cli_rejects_bot_actor_without_sending(self):
        db_path = Path(self.temp_dir.name) / "cli-approvals.db"
        env = {
            **os.environ,
            "DIALPAD_SMS_APPROVAL_DB": str(db_path),
            "DIALPAD_SMS_APPROVAL_TOKEN": "test-token",
        }
        create_result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "bin" / "create_sms_draft.py"),
                "--thread-key",
                "cli-thread",
                "--to",
                "+15125550100",
                "--from",
                "+14155201316",
                "--message",
                "Exact CLI draft.",
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        draft_id = json.loads(create_result.stdout)["data"]["draft"]["draft_id"]

        approve_result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "bin" / "approve_sms_draft.py"),
                draft_id,
                "--actor-id",
                "bot",
                "--actor-is-bot",
                "--approval-token",
                "test-token",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        payload = json.loads(approve_result.stdout)
        self.assertEqual(approve_result.returncode, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["message"], "blocked_actor")


if __name__ == "__main__":
    unittest.main()
