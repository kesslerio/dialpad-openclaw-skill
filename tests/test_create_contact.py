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

    def _get_option(self, cmd: list[str], flag: str):
        if flag not in cmd:
            return None
        return cmd[cmd.index(flag) + 1]

    def _get_json_option(self, cmd: list[str], flag: str):
        value = self._get_option(cmd, flag)
        if value is None:
            return None
        return json.loads(value)

    def test_create_contact_success_shared_create(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {"items": []}
            if cmd[:2] == ["contacts", "contacts.create"]:
                return {"id": "contact-123"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
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
        self.assertIn("Created shared contact:", out)
        self.assertEqual(err, "")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[1][:2], ["contacts", "contacts.create"])
        self.assertEqual(self._get_option(calls[1], "--first-name"), "Alice")
        self.assertEqual(self._get_option(calls[1], "--last-name"), "Miller")
        payload = self._get_json_option(calls[1], "--data")
        self.assertEqual(payload["phones"], ["+14155550123"])
        self.assertEqual(payload["emails"], ["alice@example.com"])
        self.assertEqual(payload["company_name"], "Acme")

    def test_build_create_contact_command_args_uses_required_flags_and_data_payload(self):
        payload = create_contact.build_payload(
            first_name="Phil",
            last_name="Stockton",
            phones=["+13174411610"],
            emails=["phil@example.com"],
            urls=["https://stockton.training/"],
            company_name="Stockton Training Grounds",
            job_title="Owner",
            extension="101",
            owner_id=None,
        )

        cmd = create_contact.build_create_contact_command_args(payload)

        self.assertEqual(cmd[:2], ["contacts", "contacts.create"])
        self.assertEqual(self._get_option(cmd, "--first-name"), "Phil")
        self.assertEqual(self._get_option(cmd, "--last-name"), "Stockton")
        payload_arg = self._get_json_option(cmd, "--data")
        self.assertEqual(payload_arg["company_name"], "Stockton Training Grounds")
        self.assertEqual(payload_arg["phones"], ["+13174411610"])
        self.assertEqual(payload_arg["emails"], ["phil@example.com"])
        self.assertEqual(payload_arg["urls"], ["https://stockton.training/"])

    def test_create_contact_api_error_propagates(self):
        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=WrapperError("permission denied")):
            code, out, err = self._run_main([
                "--first-name", "Bob",
                "--last-name", "Jones",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Error: permission denied", err)

    def test_create_contact_shared_scope_updates_existing(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
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
            if cmd[:2] == ["contacts", "contacts.update"]:
                return {"id": "contact-555"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "New",
                "--last-name", "Contact",
                "--phone", "+14155550123",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Updated shared contact:", out)
        self.assertEqual(calls[0][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[1][:2], ["contacts", "contacts.update"])
        payload = self._get_json_option(calls[1], "--data")
        self.assertEqual(payload["first_name"], "New")
        self.assertEqual(payload["last_name"], "Contact")

    def test_create_contact_auto_scope_with_owner_targets_shared_and_local(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {"items": []}
            if cmd[:2] == ["contacts", "contacts.create"]:
                payload = self._get_json_option(cmd, "--data")
                if payload.get("owner_id") == "owner-9":
                    return {"id": "local-1"}
                return {"id": "shared-1"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Sam",
                "--last-name", "Auto",
                "--phone", "+14155550123",
                "--owner-id", "owner-9",
                "--scope", "auto",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Created local contact for owner owner-9:", out)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[1][:2], ["contacts", "contacts.create"])
        self.assertEqual(calls[2][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[3][:2], ["contacts", "contacts.create"])

    def test_create_contact_local_scope_updates_existing_per_owner(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {
                    "items": [
                        {
                            "id": "contact-777",
                            "first_name": "Existing",
                            "last_name": "Local",
                            "phones": ["+14155550123"],
                        }
                    ]
                }
            if cmd[:2] == ["contacts", "contacts.update"]:
                return {"id": "contact-777"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Local",
                "--last-name", "User",
                "--phone", "+14155550123",
                "--scope", "local",
                "--owner-id", "owner-11",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Updated local contact for owner owner-11:", out)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[1][:2], ["contacts", "contacts.update"])

    def test_create_contact_local_owner_not_found_warning(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {"items": []}
            if cmd[:2] == ["contacts", "contacts.create"]:
                payload = self._get_json_option(cmd, "--data")
                if payload.get("owner_id") == "missing-owner":
                    raise WrapperError("Request failed: 404 owner not found")
                return {"id": "shared-1"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Warn",
                "--last-name", "Owner",
                "--phone", "+14155550123",
                "--scope", "both",
                "--owner-id", "missing-owner",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Warnings:", out)
        self.assertIn("Owner missing-owner not found", out)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[1][:2], ["contacts", "contacts.create"])
        self.assertEqual(calls[2][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[3][:2], ["contacts", "contacts.create"])

    def test_create_contact_rejects_ambiguous_shared_match(self):
        def fake_run_generated(cmd: list[str]):
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {
                    "items": [
                        {"id": "a1", "display_name": "Alice One", "phones": ["+14155550123"]},
                        {"id": "a2", "display_name": "Alice Two", "phones": ["+14155550123"]},
                    ]
                }
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Alice",
                "--last-name", "User",
                "--phone", "+14155550123",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Ambiguous contact match", err)

    def test_create_contact_rejects_ambiguous_local_match(self):
        def fake_run_generated(cmd: list[str]):
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {
                    "items": [
                        {"id": "l1", "display_name": "Local One", "phones": ["+14155550123"]},
                        {"id": "l2", "display_name": "Local Two", "phones": ["+14155550123"]},
                    ]
                }
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Local",
                "--last-name", "User",
                "--phone", "+14155550123",
                "--scope", "local",
                "--owner-id", "owner-11",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Ambiguous contact match", err)

    def test_create_contact_rejects_zero_max_pages(self):
        with patch("create_contact.require_generated_cli"), \
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
        with patch("create_contact.require_generated_cli"), \
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

    def test_create_contact_fails_when_generated_cli_unavailable(self):
        with patch(
            "create_contact.require_generated_cli",
            side_effect=WrapperError("Generated CLI not found at /tmp/generated/dialpad"),
        ):
            code, out, err = self._run_main([
                "--first-name", "Alice",
                "--last-name", "Miller",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Generated CLI not found", err)


if __name__ == "__main__":
    unittest.main()
