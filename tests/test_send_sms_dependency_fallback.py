from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import importlib.util
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN_DIR))

import _dialpad_compat
from _dialpad_compat import WrapperError, is_missing_dependency_error, _find_uv

SEND_SMS_SPEC = importlib.util.spec_from_file_location(
    "bin_send_sms",
    BIN_DIR / "send_sms.py",
)
assert SEND_SMS_SPEC is not None and SEND_SMS_SPEC.loader is not None
send_sms = importlib.util.module_from_spec(SEND_SMS_SPEC)
SEND_SMS_SPEC.loader.exec_module(send_sms)


class SendSmsDependencyFallbackTests(unittest.TestCase):
    def _run_send_sms(self, argv: list[str]) -> tuple[int, str, str]:
        with patch.object(sys, "argv", argv):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = send_sms.main()
            return code, stdout.getvalue(), stderr.getvalue()

    def test_is_missing_dependency_error(self):
        self.assertTrue(is_missing_dependency_error("ModuleNotFoundError: No module named 'click'"))
        self.assertTrue(is_missing_dependency_error("Traceback ... No module named requests"))
        self.assertTrue(is_missing_dependency_error("ImportError: cannot import name 'rich' from ..."))
        self.assertFalse(is_missing_dependency_error("Dialpad API error (HTTP 404): Contact not found"))
        self.assertFalse(is_missing_dependency_error("Connection timed out"))

    def test_find_uv_discovers_path_or_fallbacks(self):
        with patch("shutil.which", return_value="/custom/path/uv"):
            self.assertEqual(_find_uv(), "/custom/path/uv")

        with patch("shutil.which", return_value=None):
            with patch.object(Path, "is_file", return_value=True):
                with patch("os.access", return_value=True):
                    found = _find_uv()
                    self.assertIsNotNone(found)
                    self.assertTrue(found.endswith("uv"))

    def test_send_sms_falls_back_to_direct_api_on_missing_dependency(self):
        fake_api_response = {
            "id": "sms_998877",
            "message_status": "sent",
            "from_number": "+14155201316",
            "to_numbers": ["+14155550111"],
            "text": "Fallback delivered test",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_file = Path(temp_dir) / "sms-receipts.jsonl"
            with patch.dict(
                "os.environ",
                {
                    "DIALPAD_API_KEY": "fake_token",
                    "DIALPAD_PROFILE_SALES_FROM": "+14155201316",
                    "DIALPAD_SMS_RECEIPT_LEDGER": str(ledger_file),
                },
            ):
                with patch.object(send_sms, "require_generated_cli"):
                    # Simulate generated CLI failing with missing click dependency
                    with patch.object(
                        send_sms,
                        "run_generated_json",
                        side_effect=WrapperError(
                            "Generated CLI runtime dependencies missing: ModuleNotFoundError: No module named 'click'",
                            code="missing_generated_cli",
                        ),
                    ):
                        with patch.object(
                            send_sms._direct_send_sms_module,
                            "send_sms",
                            return_value=fake_api_response,
                        ) as mock_direct_send:
                            code, out, err = self._run_send_sms(
                                [
                                    "bin/send_sms.py",
                                    "--to",
                                    "+14155550111",
                                    "--from",
                                    "+14155201316",
                                    "--message",
                                    "Fallback delivered test",
                                    "--json",
                                ]
                            )

                            self.assertEqual(code, 0)
                            self.assertIn("using direct Dialpad API send fallback", err)
                            mock_direct_send.assert_called_once_with(
                                to_numbers=["+14155550111"],
                                message="Fallback delivered test",
                                from_number="+14155201316",
                                infer_country_code=False,
                            )
                            parsed = json.loads(out)
                            self.assertTrue(parsed["ok"])
                            self.assertEqual(parsed["data"]["id"], "sms_998877")

                            # Verify receipt ledger was written
                            self.assertTrue(ledger_file.exists())
                            receipt_lines = ledger_file.read_text().strip().splitlines()
                            self.assertEqual(len(receipt_lines), 1)
                            receipt_entry = json.loads(receipt_lines[0])
                            self.assertEqual(receipt_entry["source"], "direct_sms_fallback")
                            self.assertEqual(receipt_entry["message_id"], "sms_998877")
                            self.assertEqual(receipt_entry["to"], ["+14155550111"])

    def test_send_sms_fallback_raises_if_direct_send_fails(self):
        with patch.dict(
            "os.environ",
            {
                "DIALPAD_API_KEY": "fake_token",
                "DIALPAD_PROFILE_SALES_FROM": "+14155201316",
            },
        ):
            with patch.object(send_sms, "require_generated_cli"):
                with patch.object(
                    send_sms,
                    "run_generated_json",
                    side_effect=WrapperError(
                        "Generated CLI runtime dependencies missing: ModuleNotFoundError: No module named 'click'",
                        code="missing_generated_cli",
                    ),
                ):
                    with patch.object(
                        send_sms._direct_send_sms_module,
                        "send_sms",
                        side_effect=RuntimeError("Dialpad API error (HTTP 500): Server Error"),
                    ):
                        code, out, err = self._run_send_sms(
                            [
                                "bin/send_sms.py",
                                "--to",
                                "+14155550111",
                                "--from",
                                "+14155201316",
                                "--message",
                                "Should fail gracefully",
                                "--json",
                            ]
                        )
                        self.assertEqual(code, 2)
                        parsed = json.loads(out)
                        self.assertFalse(parsed["ok"])
                        self.assertEqual(parsed["error"]["code"], "upstream_error")
                        self.assertIn("Direct SMS send failed", parsed["error"]["message"])


if __name__ == "__main__":
    unittest.main()
