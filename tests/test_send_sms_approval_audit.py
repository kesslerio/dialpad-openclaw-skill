from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import send_sms
from _dialpad_compat import ERROR_CODES, WrapperError


class SendSmsApprovalAuditTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        with patch.object(sys, "argv", argv):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = send_sms.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _parse(self, raw: str) -> dict[str, object]:
        parsed = json.loads(raw)
        self.assertIsInstance(parsed, dict)
        return parsed

    def _assert_success(self, parsed: dict[str, object]) -> None:
        self.assertEqual(set(parsed.keys()), {"ok", "command", "data", "meta"})
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["command"], "send_sms.send")
        self.assertEqual(parsed["meta"]["schema_version"], "1")

    def _assert_error(self, parsed: dict[str, object]) -> None:
        self.assertEqual(set(parsed.keys()), {"ok", "command", "error", "meta"})
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["command"], "send_sms.send")
        self.assertIn(parsed["error"]["code"], ERROR_CODES)

    def _create_draft(self, temp_dir: str, *, draft_text: str = "hello") -> dict[str, object]:
        with patch.object(send_sms.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"):
            conn = send_sms.sms_approval.init_db()
            try:
                return send_sms.sms_approval.create_draft(
                    conn,
                    thread_key="thread-1",
                    customer_number="+14155550111",
                    sender_number="+14155201316",
                    draft_text=draft_text,
                )
            finally:
                conn.close()

    def test_audited_direct_send_records_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(send_sms.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"):
            draft = self._create_draft(temp_dir)

            with patch("send_sms.require_generated_cli"), \
                    patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                    patch("send_sms.run_generated_json", return_value={"id": "msg-audit", "message_status": "pending"}), \
                    patch("send_sms.require_api_key"):
                code, out, err = self._run(
                    [
                        "bin/send_sms.py",
                        "--to",
                        "+14155550111",
                        "--message",
                        "hello",
                        "--from",
                        "+14155201316",
                        "--resolve-draft-id",
                        draft["draft_id"],
                        "--approval-actor-id",
                        "12345",
                        "--approval-actor-username",
                        "operator",
                        "--json",
                    ],
                )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            parsed = self._parse(out)
            self._assert_success(parsed)
            self.assertEqual(parsed["data"]["approval_audit"]["status"], "sent")
            self.assertEqual(parsed["data"]["approval_audit"]["approval_source"], "agent_direct_send")
            self.assertEqual(parsed["data"]["approval_audit"]["approval_actor_trust"], "agent_asserted")

            conn = send_sms.sms_approval.init_db()
            try:
                stored = send_sms.sms_approval.get_draft(conn, draft["draft_id"])
            finally:
                conn.close()
            self.assertEqual(stored["status"], send_sms.sms_approval.STATUS_SENT)
            self.assertEqual(stored["approved_by"], "12345")
            self.assertEqual(stored["approved_username"], "operator")
            self.assertEqual(stored["dialpad_sms_id"], "msg-audit")
            self.assertEqual(stored["metadata"]["approval_actor_trust"], "agent_asserted")

    def test_audited_direct_send_rejects_mismatch_before_api_call(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(send_sms.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"):
            draft = self._create_draft(temp_dir, draft_text="stored text")

            with patch("send_sms.require_generated_cli"), \
                    patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                    patch("send_sms.run_generated_json") as run_generated_json, \
                    patch("send_sms.require_api_key") as require_api_key:
                code, out, err = self._run(
                    [
                        "bin/send_sms.py",
                        "--to",
                        "+14155550111",
                        "--message",
                        "different text",
                        "--from",
                        "+14155201316",
                        "--resolve-draft-id",
                        draft["draft_id"],
                        "--approval-actor-id",
                        "12345",
                        "--json",
                    ],
                )

            self.assertEqual(code, 2)
            self.assertEqual(err, "")
            run_generated_json.assert_not_called()
            require_api_key.assert_not_called()
            parsed = self._parse(out)
            self._assert_error(parsed)
            self.assertIn("draft_text_mismatch", parsed["error"]["message"])
            serialized = json.dumps(parsed)
            self.assertNotIn("stored text", serialized)
            self.assertNotIn("customer_number", serialized)
            self.assertNotIn("draft", parsed["meta"]["approval_audit"])
            self.assertEqual(parsed["meta"]["approval_audit"]["draft_id"], draft["draft_id"])

    def test_audited_direct_send_rejects_stripped_equivalent_text(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(send_sms.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"):
            draft = self._create_draft(temp_dir, draft_text="stored text")

            with patch("send_sms.require_generated_cli"), \
                    patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                    patch("send_sms.run_generated_json") as run_generated_json, \
                    patch("send_sms.require_api_key") as require_api_key:
                code, out, err = self._run(
                    [
                        "bin/send_sms.py",
                        "--to",
                        "+14155550111",
                        "--message",
                        "stored text\n",
                        "--from",
                        "+14155201316",
                        "--resolve-draft-id",
                        draft["draft_id"],
                        "--approval-actor-id",
                        "12345",
                        "--json",
                    ],
                )

            self.assertEqual(code, 2)
            self.assertEqual(err, "")
            run_generated_json.assert_not_called()
            require_api_key.assert_not_called()
            parsed = self._parse(out)
            self._assert_error(parsed)
            self.assertIn("draft_text_mismatch", parsed["error"]["message"])

    def test_record_approval_audit_reports_db_open_failure_after_send(self):
        args = Namespace(resolve_draft_id="smsdraft_abc123", approval_actor_id="12345", approval_actor_username=None)

        with patch.object(send_sms.sms_approval, "init_db", side_effect=OSError("approval db unavailable")):
            result = send_sms.record_approval_audit(args, {"id": "msg-sent", "message_status": "pending"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "audit_record_failed")
        self.assertEqual(result["draft_id"], "smsdraft_abc123")
        self.assertIn("approval db unavailable", result["error"])

    def test_audited_direct_send_failure_marks_draft_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(send_sms.sms_approval, "DB_PATH", Path(temp_dir) / "approvals.db"):
            draft = self._create_draft(temp_dir)

            with patch("send_sms.require_generated_cli"), \
                    patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                    patch(
                        "send_sms.run_generated_json",
                        side_effect=WrapperError("Dialpad unavailable", code="upstream_error", retryable=True),
                    ), \
                    patch("send_sms.require_api_key"):
                code, out, err = self._run(
                    [
                        "bin/send_sms.py",
                        "--to",
                        "+14155550111",
                        "--message",
                        "hello",
                        "--from",
                        "+14155201316",
                        "--resolve-draft-id",
                        draft["draft_id"],
                        "--approval-actor-id",
                        "12345",
                        "--json",
                    ],
                )

            self.assertEqual(code, 2)
            self.assertEqual(err, "")
            parsed = self._parse(out)
            self._assert_error(parsed)

            conn = send_sms.sms_approval.init_db()
            try:
                stored = send_sms.sms_approval.get_draft(conn, draft["draft_id"])
            finally:
                conn.close()
            self.assertEqual(stored["status"], send_sms.sms_approval.STATUS_FAILED)
            self.assertEqual(stored["send_error"], "Dialpad unavailable")

    def test_audited_direct_send_requires_actor(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json") as run_generated_json, \
                patch("send_sms.require_api_key") as require_api_key:
            code, out, err = self._run(
                [
                    "bin/send_sms.py",
                    "--to",
                    "+14155550111",
                    "--message",
                    "hello",
                    "--from",
                    "+14155201316",
                    "--resolve-draft-id",
                    "smsdraft_missing",
                    "--json",
                ],
            )

        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        run_generated_json.assert_not_called()
        require_api_key.assert_not_called()
        parsed = self._parse(out)
        self._assert_error(parsed)
        self.assertEqual(parsed["error"]["code"], "invalid_argument")
        self.assertIn("--approval-actor-id", parsed["error"]["message"])


if __name__ == "__main__":
    unittest.main()
