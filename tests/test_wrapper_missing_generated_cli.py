from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import create_sms_webhook
import export_sms
import lookup_contact
import make_call
from _dialpad_compat import WrapperError


class MissingGeneratedCliTests(unittest.TestCase):
    def _run(self, module, argv: list[str]) -> tuple[int, str, str]:
        with patch.object(sys, "argv", argv):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = module.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_make_call_fails_when_generated_cli_unavailable(self):
        with patch(
            "make_call.require_generated_cli",
            side_effect=WrapperError("Generated CLI not found at /tmp/generated/dialpad"),
        ):
            code, out, err = self._run(make_call, ["bin/make_call.py", "--to", "+14155550111"])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Generated CLI not found", err)

    def test_lookup_contact_fails_when_generated_cli_unavailable(self):
        with patch(
            "lookup_contact.require_generated_cli",
            side_effect=WrapperError("Generated CLI not found at /tmp/generated/dialpad"),
        ):
            code, out, err = self._run(lookup_contact, ["bin/lookup_contact.py", "+14155550111"])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Generated CLI not found", err)

    def test_create_sms_webhook_fails_when_generated_cli_unavailable(self):
        with patch(
            "create_sms_webhook.require_generated_cli",
            side_effect=WrapperError("Generated CLI not found at /tmp/generated/dialpad"),
        ):
            code, out, err = self._run(
                create_sms_webhook,
                ["bin/create_sms_webhook.py", "list"],
            )

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Generated CLI not found", err)

    def test_export_sms_fails_when_generated_cli_unavailable(self):
        with patch(
            "export_sms.require_generated_cli",
            side_effect=WrapperError("Generated CLI not found at /tmp/generated/dialpad"),
        ):
            code, out, err = self._run(export_sms, ["bin/export_sms.py"])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Generated CLI not found", err)


if __name__ == "__main__":
    unittest.main()
