from __future__ import annotations

import io
import json
import os
import subprocess
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
from _dialpad_compat import WrapperError, is_missing_dependency_error

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

    def test_send_sms_falls_back_to_direct_api_on_missing_dependency(self):
        fake_api_response = {
            "id": "sms_998877",
            "message_status": "sent",
            "from_number": "+14155550140",
            "to_numbers": ["+14155550111"],
            "text": "Fallback delivered test",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_file = Path(temp_dir) / "sms-receipts.jsonl"
            with patch.dict(
                "os.environ",
                {
                    "DIALPAD_API_KEY": "fake_token",
                    "DIALPAD_PROFILE_SALES_FROM": "+14155550140",
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
                                    "+14155550140",
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
                                from_number="+14155550140",
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
                "DIALPAD_PROFILE_SALES_FROM": "+14155550140",
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
                                "+14155550140",
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


class ManagedCliEnvironmentTests(unittest.TestCase):
    """Regression for #155: the generated CLI path must get click from the repo's managed environment.

    The deployed gateway runtime resolves the wrapper outside any login shell,
    carries no `uv` on PATH or in the old discovery candidates, and its system
    python cannot import click. The #89 fix relied on ambient `uv` discovery and
    silently degraded to the bare CLI path there. These tests fail if the
    generated CLI command, the PYTHONPATH injection, or the vendored
    dependencies that keep click importable are removed or broken.
    """

    def test_generated_command_is_deterministic_without_uv_or_path_lookup(self):
        # Simulates a runtime where no uv is discoverable anywhere.
        with patch("shutil.which", return_value=None):
            cmd = _dialpad_compat._generated_command(["--help"])

        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], str(_dialpad_compat.GENERATED_DIALPAD))
        self.assertFalse(any(Path(part).name == "uv" for part in cmd), cmd)

    def test_managed_env_resolves_click_and_requests_from_vendor(self):
        # The vendored tree must exist and win over ambient site-packages, even
        # on interpreters that happen to have click installed.
        self.assertTrue(
            (_dialpad_compat.VENDOR_DIR / "click" / "__init__.py").is_file(),
            "vendored click is missing from vendor/",
        )
        self.assertTrue(
            (_dialpad_compat.VENDOR_DIR / "requests" / "__init__.py").is_file(),
            "vendored requests is missing from vendor/",
        )

        env = _dialpad_compat._env_with_auth()
        pythonpath = env.get("PYTHONPATH", "")
        self.assertTrue(pythonpath, "managed env must put vendor/ on PYTHONPATH")
        self.assertEqual(
            Path(pythonpath.split(os.pathsep)[0]).resolve(),
            _dialpad_compat.VENDOR_DIR.resolve(),
        )

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import click, requests; print(click.__file__); print(requests.__file__)",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        click_file, requests_file = proc.stdout.split()
        vendor = _dialpad_compat.VENDOR_DIR.resolve()
        self.assertTrue(Path(click_file).resolve().is_relative_to(vendor), click_file)
        self.assertTrue(Path(requests_file).resolve().is_relative_to(vendor), requests_file)

    def test_generated_cli_runs_under_managed_env_when_ambient_python_lacks_click(self):
        # End-to-end facade -> dialpad.openapi under the wrapper's managed
        # command. On an interpreter without click (the deployed-runtime
        # failure mode: ModuleNotFoundError: No module named 'click'), any
        # regression to the bare-path command fails here.
        cmd = _dialpad_compat._generated_command(["--help"])
        proc = subprocess.run(
            cmd,
            env=_dialpad_compat._env_with_auth(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Usage:", proc.stdout)
        self.assertNotIn("No module named", proc.stderr)

    def test_mocked_sms_send_through_managed_path_emits_no_missing_click_error(self):
        # A real `sms send` invocation through the full managed chain (facade ->
        # dialpad.openapi -> click parse -> requests POST), pointed at a
        # loopback fake Dialpad API so nothing leaves the machine. The healthy
        # managed path must surface the API answer, never the missing-click
        # error that recurred in #155.
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        received = {}

        class FakeDialpadHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                received["path"] = self.path
                received["body"] = body
                payload = b'{"error":{"message":"fake rejection"}}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence test output
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDialpadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = _dialpad_compat._env_with_auth()
            env["DIALPAD_API_KEY"] = "test-key-155"
            payload = json.dumps(
                {
                    "to_numbers": ["+14155550111"],
                    "text": "managed path check",
                    "infer_country_code": False,
                    "from_number": "+14155550140",
                }
            )
            cmd = _dialpad_compat._generated_command(
                ["--base-url", f"http://127.0.0.1:{server.server_port}", "sms", "send", "--data", payload]
            )
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        finally:
            server.shutdown()
            server.server_close()

        # The request reached the fake API through click+requests: the CLI
        # reports the 401, never an import failure.
        self.assertIn("/sms", received.get("path", ""))
        self.assertIn(b"managed path check", received.get("body", b""))
        self.assertNotIn("No module named", proc.stderr)
        self.assertNotIn("No module named", proc.stdout)
        self.assertNotIn("ImportError", proc.stderr)
        self.assertEqual(proc.returncode, 1, (proc.stdout, proc.stderr))
        self.assertIn("401", proc.stderr)


if __name__ == "__main__":
    unittest.main()
