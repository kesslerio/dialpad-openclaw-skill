from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "bin"))

import _dialpad_compat
import send_group_intro
import send_sms
import sms_approval
import sms_receipts
from _dialpad_compat import WrapperError


def _completed(payload: object, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    stdout = json.dumps(payload) if returncode == 0 else ""
    return subprocess.CompletedProcess(
        args=["generated", "--output", "json"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class SmsReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ledger_path = Path(self.temp_dir.name) / "state" / "sms-receipts.jsonl"
        self.original_max_bytes = sms_receipts.MAX_LEDGER_BYTES
        self.original_env_path = os.environ.get("DIALPAD_SMS_RECEIPT_LEDGER")
        os.environ["DIALPAD_SMS_RECEIPT_LEDGER"] = str(self.ledger_path)
        _dialpad_compat.take_receipt_status()
        self.addCleanup(self._restore_receipts)

    def _restore_receipts(self) -> None:
        sms_receipts.MAX_LEDGER_BYTES = self.original_max_bytes
        if self.original_env_path is None:
            os.environ.pop("DIALPAD_SMS_RECEIPT_LEDGER", None)
        else:
            os.environ["DIALPAD_SMS_RECEIPT_LEDGER"] = self.original_env_path
        _dialpad_compat.take_receipt_status()

    def _read_receipts(self, path: Path | None = None) -> list[dict[str, object]]:
        ledger = path or self.ledger_path
        if not ledger.exists() or ledger.is_dir():
            return []
        return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]

    def _run_send_sms(
        self,
        generated_results: list[subprocess.CompletedProcess[str]],
        *,
        json_mode: bool = False,
        to_number: str = "+14155550111",
    ) -> tuple[int, str, str]:
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str], capture_output: bool = False) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return generated_results.pop(0)

        argv = [
            "bin/send_sms.py",
            "--to",
            to_number,
            "--from",
            "+14155201316",
            "--message",
            "Hello",
        ]
        if json_mode:
            argv.append("--json")

        with patch.object(sys, "argv", argv), \
                patch("send_sms.require_generated_cli"), \
                patch("send_sms.require_api_key"), \
                patch("_dialpad_compat.run_generated", side_effect=fake_run_generated):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = send_sms.main()

        return code, stdout.getvalue(), stderr.getvalue()

    def _run_group_intro(
        self,
        generated_results: list[subprocess.CompletedProcess[str]],
    ) -> tuple[int, str, str]:
        def fake_run_generated(_cmd: list[str], capture_output: bool = False) -> subprocess.CompletedProcess[str]:
            return generated_results.pop(0)

        with patch.object(
            sys,
            "argv",
            [
                "bin/send_group_intro.py",
                "--prospect",
                "+14155550111",
                "--reference",
                "+14155559999",
                "--from",
                "+14155201316",
                "--confirm-share",
                "--json",
            ],
        ), \
                patch("send_group_intro.require_generated_cli"), \
                patch("send_group_intro.require_api_key"), \
                patch("_dialpad_compat.run_generated", side_effect=fake_run_generated):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = send_group_intro.main()

        return code, stdout.getvalue(), stderr.getvalue()

    def test_send_sms_success_appends_receipt_and_preserves_existing_lines(self):
        first = _completed({"id": "msg-1", "message_status": "pending"})
        code, _out, err = self._run_send_sms([first])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertEqual(
            set(receipt.keys()),
            {"schema_version", "message_id", "to", "from", "timestamp_utc", "delivery_status", "source"},
        )
        self.assertEqual(receipt["schema_version"], "1")
        self.assertEqual(receipt["message_id"], "msg-1")
        self.assertEqual(receipt["to"], ["+14155550111"])
        self.assertEqual(receipt["from"], "+14155201316")
        self.assertTrue(str(receipt["timestamp_utc"]).endswith("Z"))
        self.assertEqual(receipt["delivery_status"], "pending")
        self.assertEqual(receipt["source"], "send_sms")

        second = _completed({"id": "msg-2", "message_status": "queued"})
        code, _out, err = self._run_send_sms([second], to_number="+14155550222")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        receipts = self._read_receipts()
        self.assertEqual([item["message_id"] for item in receipts], ["msg-1", "msg-2"])
        self.assertEqual(receipts[1]["to"], ["+14155550222"])

    def test_group_intro_success_and_partial_success_write_successful_legs_only(self):
        code, _out, err = self._run_group_intro(
            [
                _completed({"id": "prospect-msg", "message_status": "pending"}),
                _completed({"id": "reference-msg", "message_status": "pending"}),
            ],
        )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        receipts = self._read_receipts()
        self.assertEqual([item["message_id"] for item in receipts], ["prospect-msg", "reference-msg"])
        self.assertEqual([item["source"] for item in receipts], ["send_group_intro", "send_group_intro"])

        self.ledger_path.unlink()
        code, out, err = self._run_group_intro(
            [
                _completed({"id": "prospect-only", "message_status": "pending"}),
                _completed({}, returncode=1, stderr="Dialpad failed"),
            ],
        )

        self.assertEqual(code, 2)
        self.assertIn("partial_success", out)
        self.assertEqual(err, "")
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["message_id"], "prospect-only")
        self.assertEqual(receipts[0]["to"], ["+14155550111"])

    def test_approval_lane_success_appends_receipt(self):
        db_path = Path(self.temp_dir.name) / "approvals.db"
        original_db_path = sms_approval.DB_PATH
        sms_approval.DB_PATH = db_path
        self.addCleanup(lambda: setattr(sms_approval, "DB_PATH", original_db_path))
        conn = sms_approval.init_db()
        self.addCleanup(conn.close)
        draft = sms_approval.create_draft(
            conn,
            thread_key="thread-1",
            customer_number="+15125550100",
            sender_number="+14155201316",
            draft_text="Approved text",
        )

        result = sms_approval.approve_draft(
            conn,
            draft_id=draft["draft_id"],
            actor_id="12345",
            actor_username="operator",
            send_func=lambda *_args, **_kwargs: {"id": "approval-msg", "message_status": "pending"},
        )

        self.assertTrue(result["sent"])
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["message_id"], "approval-msg")
        self.assertEqual(receipts[0]["to"], ["+15125550100"])
        self.assertEqual(receipts[0]["from"], "+14155201316")
        self.assertEqual(receipts[0]["source"], "approval_lane")

    def test_result_to_numbers_are_not_required_for_recipient_receipt(self):
        code, _out, err = self._run_send_sms([_completed({"id": "msg-no-to", "message_status": "pending"})])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        receipts = self._read_receipts()
        self.assertEqual(receipts[0]["to"], ["+14155550111"])

    def test_failed_sends_do_not_write_receipts(self):
        with self.assertRaises(WrapperError):
            with patch(
                "_dialpad_compat.run_generated",
                return_value=_completed({}, returncode=1, stderr="Dialpad failed"),
            ):
                _dialpad_compat.run_generated_json(
                    [
                        "sms",
                        "send",
                        "--data",
                        json.dumps({"to_numbers": ["+14155550111"], "from_number": "+14155201316"}),
                    ],
                )

        with patch("_dialpad_compat.run_generated", return_value=_completed({"message_status": "pending"})):
            _dialpad_compat.run_generated_json(
                [
                    "sms",
                    "send",
                    "--data",
                    json.dumps({"to_numbers": ["+14155550111"], "from_number": "+14155201316"}),
                ],
            )

        with patch("_dialpad_compat.run_generated", return_value=_completed({"id": "msg-failed", "message_status": "failed"})):
            _dialpad_compat.run_generated_json(
                [
                    "sms",
                    "send",
                    "--data",
                    json.dumps({"to_numbers": ["+14155550111"], "from_number": "+14155201316"}),
                ],
            )

        self.assertEqual(self._read_receipts(), [])

    def test_non_send_command_does_not_touch_ledger_and_modes_are_restrictive(self):
        with patch("_dialpad_compat.run_generated", return_value=_completed({"items": []})):
            result = _dialpad_compat.run_generated_json(["call", "list"])

        self.assertEqual(result, {"items": []})
        self.assertFalse(self.ledger_path.exists())

        code, _out, err = self._run_send_sms([_completed({"id": "msg-mode", "message_status": "pending"})])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(stat.S_IMODE(self.ledger_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.ledger_path.stat().st_mode), 0o600)

    def test_append_failure_is_nonfatal_and_json_meta_reports_marker(self):
        self.ledger_path.parent.mkdir(parents=True)
        self.ledger_path.mkdir()

        code, out, err = self._run_send_sms(
            [_completed({"id": "msg-unwritable", "message_status": "pending"})],
            json_mode=True,
        )

        self.assertEqual(code, 0)
        self.assertIn("failed to append Dialpad SMS receipt ledger", err)
        parsed = json.loads(out)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["meta"]["receipt_ledger"], "append_failed")

    def test_growth_cap_rotates_existing_ledger(self):
        self.ledger_path.parent.mkdir(parents=True)
        self.ledger_path.write_text("x" * 64, encoding="utf-8")
        sms_receipts.MAX_LEDGER_BYTES = 10

        code, _out, err = self._run_send_sms([_completed({"id": "msg-rotated", "message_status": "pending"})])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        rotated = self.ledger_path.with_name(f"{self.ledger_path.name}.1")
        self.assertTrue(rotated.exists())
        self.assertEqual(rotated.read_text(encoding="utf-8"), "x" * 64)
        receipts = self._read_receipts()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["message_id"], "msg-rotated")

    def test_env_override_is_respected(self):
        env_ledger = Path(self.temp_dir.name) / "override" / "sms-receipts.jsonl"
        os.environ["DIALPAD_SMS_RECEIPT_LEDGER"] = str(env_ledger)

        code, _out, err = self._run_send_sms([_completed({"id": "msg-env", "message_status": "pending"})])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertFalse(self.ledger_path.exists())
        receipts = self._read_receipts(env_ledger)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["message_id"], "msg-env")


if __name__ == "__main__":
    unittest.main()
