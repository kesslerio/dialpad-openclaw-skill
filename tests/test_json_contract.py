from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import create_contact
import create_sms_webhook
import export_sms
import lookup_contact
import make_call
import send_sms
import update_contact
from _dialpad_compat import ERROR_CODES, WrapperError


class JsonContractTests(unittest.TestCase):
    def _run(self, module, argv: list[str]) -> tuple[int, str, str]:
        with patch.object(sys, "argv", argv):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = module.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _parse(self, raw: str) -> dict[str, object]:
        parsed = json.loads(raw)
        self.assertIsInstance(parsed, dict)
        return parsed

    def _assert_success(self, parsed: dict[str, object], command: str) -> None:
        self.assertEqual(set(parsed.keys()), {"ok", "command", "data", "meta"})
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["command"], command)
        self.assertEqual(parsed["meta"]["schema_version"], "1")
        self.assertTrue(parsed["meta"]["wrapper"])
        self.assertTrue(parsed["meta"]["timestamp_utc"])

    def _assert_error(self, parsed: dict[str, object], command: str) -> None:
        self.assertEqual(set(parsed.keys()), {"ok", "command", "error", "meta"})
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["command"], command)
        self.assertIn(parsed["error"]["code"], ERROR_CODES)
        self.assertIsInstance(parsed["error"]["retryable"], bool)
        self.assertEqual(parsed["meta"]["schema_version"], "1")

    def test_send_sms_json_success_envelope(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json", return_value={"id": "msg-1", "status": "pending"}), \
                patch("send_sms.require_api_key"):
            code, out, err = self._run(
                send_sms,
                ["bin/send_sms.py", "--to", "+14155550111", "--message", "hello", "--from", "+14155201316", "--json"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "send_sms.send")

    def test_send_sms_json_error_envelope(self):
        with patch(
            "send_sms.require_generated_cli",
            side_effect=WrapperError("Generated CLI not found", code="missing_generated_cli", retryable=False),
        ):
            code, out, err = self._run(send_sms, ["bin/send_sms.py", "--to", "+14155550111", "--message", "hello", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        self._assert_error(self._parse(out), "send_sms.send")

    def test_lookup_contact_invalid_argument_in_json_mode(self):
        code, out, err = self._run(lookup_contact, ["bin/lookup_contact.py", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "lookup_contact.lookup")
        self.assertEqual(parsed["error"]["code"], "invalid_argument")

    def test_make_call_json_success_envelope(self):
        with patch("make_call.require_generated_cli"), \
                patch("make_call.require_api_key"), \
                patch("make_call.resolve_user_id", return_value="u1"), \
                patch("make_call.run_generated_json", return_value={"call_id": "c1"}):
            code, out, err = self._run(
                make_call,
                ["bin/make_call.py", "--to", "+14155550111", "--user-id", "u1", "--json"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "make_call.call")

    def test_create_contact_json_success_envelope(self):
        def fake_run(cmd: list[str]):
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {"items": []}
            if cmd[:2] == ["contacts", "contacts.create"]:
                return {"id": "ct-1"}
            raise AssertionError(cmd)

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run):
            code, out, err = self._run(
                create_contact,
                ["bin/create_contact.py", "--first-name", "A", "--last-name", "B", "--phone", "+14155550111", "--json"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "create_contact.upsert")

    def test_update_contact_not_found_maps_to_not_found(self):
        with patch("update_contact.require_generated_cli"), \
                patch("update_contact.require_api_key"), \
                patch("update_contact.run_generated_json", side_effect=WrapperError("404 Not Found")):
            code, out, err = self._run(
                update_contact,
                ["bin/update_contact.py", "--id", "missing", "--first-name", "A", "--json"],
            )
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "update_contact.update")
        self.assertEqual(parsed["error"]["code"], "not_found")

    def test_create_sms_webhook_subcommand_list_json(self):
        with patch("create_sms_webhook.require_generated_cli"), \
                patch("create_sms_webhook.require_api_key"), \
                patch("create_sms_webhook.run_generated_json", return_value={"items": [{"id": "s1"}]}):
            code, out, err = self._run(create_sms_webhook, ["bin/create_sms_webhook.py", "list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "create_sms_webhook.list")

    def test_create_sms_webhook_subcommand_delete_json(self):
        with patch("create_sms_webhook.require_generated_cli"), \
                patch("create_sms_webhook.require_api_key"), \
                patch("create_sms_webhook.run_generated_json", return_value={"ok": True}):
            code, out, err = self._run(create_sms_webhook, ["bin/create_sms_webhook.py", "delete", "sub-1", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "create_sms_webhook.delete")

    def test_create_sms_webhook_webhooks_list_json(self):
        with patch("create_sms_webhook.require_generated_cli"), \
                patch("create_sms_webhook.require_api_key"), \
                patch("create_sms_webhook.run_generated_json", return_value={"items": [{"id": "w1"}]}):
            code, out, err = self._run(create_sms_webhook, ["bin/create_sms_webhook.py", "webhooks", "list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "create_sms_webhook.webhooks_list")

    def test_export_sms_json_success_has_single_json_output(self):
        call_count = {"n": 0}

        def fake_run(cmd: list[str]):
            if cmd[:2] == ["sms", "export"]:
                return {"request_id": "r1"}
            if cmd[:2] == ["stats", "stats.get"]:
                call_count["n"] += 1
                return {"status": "complete", "download_url": "http://example.com/file.csv"}
            raise AssertionError(cmd)

        with patch("export_sms.require_generated_cli"), \
                patch("export_sms.require_api_key"), \
                patch("export_sms.run_generated_json", side_effect=fake_run):
            code, out, err = self._run(export_sms, ["bin/export_sms.py", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_success(parsed, "export_sms.export")
        self.assertIn("progress", parsed["meta"])

    def test_export_sms_timeout_maps_to_timeout(self):
        with patch("export_sms.require_generated_cli"), \
                patch("export_sms.require_api_key"), \
                patch("export_sms.run_generated_json", return_value={"request_id": "r1"}), \
                patch("export_sms.poll_for_completion", side_effect=WrapperError("Timed out after 10 seconds")):
            code, out, err = self._run(export_sms, ["bin/export_sms.py", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "export_sms.export")
        self.assertEqual(parsed["error"]["code"], "timeout")


if __name__ == "__main__":
    unittest.main()
