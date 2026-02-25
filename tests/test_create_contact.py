from pathlib import Path
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import create_contact
from _dialpad_compat import WrapperError


class CreateContactTests(unittest.TestCase):
    def _run_main(self, args):
        with patch.object(sys, "argv", ["bin/create_contact.py", *args]):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = create_contact.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_create_contact_success(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {"items": []}
            if cmd[:2] == ["contacts", "contacts.create"]:
                return {"id": "contact-123"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.generated_cli_available", return_value=True), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Alice",
                "--last-name", "Miller",
                "--phone", "+14155550123",
                "--email", "alice@example.com",
                "--company-name", "Acme",
                "--job-title", "VP",
                "--extension", "101",
                "--url", "https://acme.example",
            ])

        self.assertEqual(code, 0)
        self.assertIn("Created contact:", out)
        self.assertEqual(err, "")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[1][:2], ["contacts", "contacts.create"])
        payload = json.loads(calls[1][3])
        self.assertEqual(payload["first_name"], "Alice")
        self.assertEqual(payload["last_name"], "Miller")
        self.assertEqual(payload["phones"], ["+14155550123"])
        self.assertEqual(payload["emails"], ["alice@example.com"])

    def test_create_contact_api_error_propagates(self):
        with patch("create_contact.generated_cli_available", return_value=True), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=WrapperError("permission denied")):
            code, out, err = self._run_main([
                "--first-name", "Bob",
                "--last-name", "Jones",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Error: permission denied", err)

    def test_create_contact_duplicate_precheck_blocks_by_default(self):
        def fake_run_generated(cmd: list[str]):
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {
                    "items": [
                        {
                            "id": "contact-555",
                            "first_name": "Existing",
                            "last_name": "User",
                            "phones": ["+14155550123"],
                        }
                    ]
                }
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.generated_cli_available", return_value=True), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "New",
                "--last-name", "Contact",
                "--phone", "+14155550123",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Duplicate contact detected", err)

    def test_create_contact_rejects_zero_max_pages(self):
        with patch("create_contact.generated_cli_available", return_value=True), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json"):
            code, out, err = self._run_main([
                "--first-name", "Invalid",
                "--last-name", "Pages",
                "--phone", "+14155550123",
                "--max-pages", "0",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Invalid --max-pages value. Use a positive integer.", err)

    def test_create_contact_rejects_negative_max_pages(self):
        with patch("create_contact.generated_cli_available", return_value=True), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json"):
            code, out, err = self._run_main([
                "--first-name", "Invalid",
                "--last-name", "Pages",
                "--phone", "+14155550123",
                "--max-pages", "-1",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Invalid --max-pages value. Use a positive integer.", err)


if __name__ == "__main__":
    unittest.main()
