from pathlib import Path
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import update_contact
from _dialpad_compat import WrapperError


class UpdateContactTests(unittest.TestCase):
    def _run_main(self, args):
        with patch.object(sys, "argv", ["bin/update_contact.py", *args]):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = update_contact.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_update_contact_success(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.update"]:
                return {"id": "contact-999"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("update_contact.require_generated_cli"), \
                patch("update_contact.require_api_key"), \
                patch("update_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--id", "contact-999",
                "--first-name", "Jane",
                "--email", "jane@example.com",
                "--phone", "+14155550123",
                "--url", "https://example.com",
            ])

        self.assertEqual(code, 0)
        self.assertIn("Updated contact contact-999:", out)
        self.assertEqual(err, "")
        self.assertEqual(calls[0][:2], ["contacts", "contacts.update"])
        payload = json.loads(calls[0][5])
        self.assertEqual(payload["first_name"], "Jane")
        self.assertEqual(payload["emails"], ["jane@example.com"])
        self.assertEqual(payload["phones"], ["+14155550123"])

    def test_update_contact_not_found(self):
        with patch("update_contact.require_generated_cli"), \
                patch("update_contact.require_api_key"), \
                patch("update_contact.run_generated_json", side_effect=WrapperError("404 Not Found")):
            code, out, err = self._run_main([
                "--id", "contact-missing",
                "--first-name", "Missing",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Error: Contact not found: contact-missing", err)

    def test_update_contact_rejects_missing_fields(self):
        with patch("update_contact.require_generated_cli"), \
                patch("update_contact.require_api_key"), \
                patch("update_contact.run_generated_json"):
            code, out, err = self._run_main(["--id", "contact-1"])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("No update fields provided", err)

        with patch("update_contact.require_generated_cli"), \
                patch("update_contact.require_api_key"), \
                patch("update_contact.run_generated_json"):
            code, out, err = self._run_main([
                "--id", "contact-1",
                "--phone", "123",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Invalid --phone", err)

    def test_update_contact_fails_when_generated_cli_unavailable(self):
        with patch(
            "update_contact.require_generated_cli",
            side_effect=WrapperError("Generated CLI not found at /tmp/generated/dialpad"),
        ):
            code, out, err = self._run_main([
                "--id", "contact-1",
                "--first-name", "Jane",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Generated CLI not found", err)


if __name__ == "__main__":
    unittest.main()
