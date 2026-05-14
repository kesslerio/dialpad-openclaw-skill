from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

GET_TRANSCRIPT_SPEC = importlib.util.spec_from_file_location(
    "bin_get_call_transcript_wrapper",
    Path(__file__).resolve().parent.parent / "bin" / "get_call_transcript.py",
)
assert GET_TRANSCRIPT_SPEC is not None and GET_TRANSCRIPT_SPEC.loader is not None
get_call_transcript_wrapper = importlib.util.module_from_spec(GET_TRANSCRIPT_SPEC)
GET_TRANSCRIPT_SPEC.loader.exec_module(get_call_transcript_wrapper)


class GetCallTranscriptWrapperTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        with patch.object(sys, "argv", argv):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = get_call_transcript_wrapper.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_json_mode_returns_available_transcript(self):
        with patch.object(get_call_transcript_wrapper, "require_api_key"), patch.object(
            get_call_transcript_wrapper,
            "resolve_call_transcript",
            return_value={
                "call_id": "call-123",
                "available": True,
                "transcript_text": "Transcript body",
                "source": "transcripts",
                "unavailable_reason": None,
                "call": {"call_id": "call-123", "external_number": "+14155550123"},
            },
        ):
            code, out, err = self._run(["bin/get_call_transcript.py", "--call-id", "call-123", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["command"], "get_call_transcript.get")
        self.assertEqual(parsed["data"]["call_id"], "call-123")
        self.assertTrue(parsed["data"]["available"])
        self.assertEqual(parsed["data"]["transcript_text"], "Transcript body")
        self.assertNotIn("recap", parsed["data"])
        self.assertNotIn("suggested_reply", parsed["data"])

    def test_json_mode_returns_unavailable_transcript(self):
        with patch.object(get_call_transcript_wrapper, "require_api_key"), patch.object(
            get_call_transcript_wrapper,
            "resolve_call_transcript",
            return_value={
                "call_id": "call-123",
                "available": False,
                "transcript_text": None,
                "source": "call",
                "unavailable_reason": "no_transcript",
            },
        ):
            code, out, err = self._run(["bin/get_call_transcript.py", "--call-id", "call-123", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertTrue(parsed["ok"])
        self.assertFalse(parsed["data"]["available"])
        self.assertIsNone(parsed["data"]["transcript_text"])
        self.assertEqual(parsed["data"]["unavailable_reason"], "no_transcript")

    def test_text_mode_prints_transcript(self):
        with patch.object(get_call_transcript_wrapper, "require_api_key"), patch.object(
            get_call_transcript_wrapper,
            "resolve_call_transcript",
            return_value={
                "call_id": "call-123",
                "available": True,
                "transcript_text": "Transcript body",
                "source": "transcripts",
                "unavailable_reason": None,
            },
        ):
            code, out, err = self._run(["bin/get_call_transcript.py", "--call-id", "call-123"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Transcript for call call-123", out)
        self.assertIn("Transcript body", out)

    def test_with_requires_last(self):
        code, out, err = self._run(
            ["bin/get_call_transcript.py", "--call-id", "call-123", "--with", "Jane", "--json"]
        )

        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error"]["code"], "invalid_argument")

    def test_dialpad_errors_become_json_error_envelope(self):
        with patch.object(get_call_transcript_wrapper, "require_api_key"), patch.object(
            get_call_transcript_wrapper,
            "resolve_call_transcript",
            side_effect=get_call_transcript_wrapper.DialpadApiError("Dialpad API error", status_code=500),
        ):
            code, out, err = self._run(["bin/get_call_transcript.py", "--call-id", "call-123", "--json"])

        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error"]["code"], "upstream_error")


if __name__ == "__main__":
    unittest.main()
