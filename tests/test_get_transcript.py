from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import get_transcript
from call_lookup import DialpadApiError


class GetTranscriptTests(unittest.TestCase):
    def test_format_transcript_reads_transcription_text(self):
        self.assertEqual(
            get_transcript.format_transcript({"transcription_text": " Please call back. "}),
            "Please call back.",
        )

    def test_format_transcript_reads_utterances_with_speakers(self):
        payload = {
            "utterances": [
                {"speaker_name": "Agent", "text": "Hello"},
                {"speaker_name": "Prospect", "text": "I have a question"},
            ]
        }

        self.assertEqual(
            get_transcript.format_transcript(payload),
            "Agent: Hello\nProspect: I have a question",
        )

    def test_get_call_transcript_returns_endpoint_transcript(self):
        with patch(
            "get_transcript.api_get",
            return_value={"transcript": "Transcript body"},
        ) as api_get:
            result = get_transcript.get_call_transcript("call-123", call={"call_id": "call-123"})

        api_get.assert_called_once_with("/transcripts/call-123")
        self.assertTrue(result["available"])
        self.assertEqual(result["call_id"], "call-123")
        self.assertEqual(result["transcript_text"], "Transcript body")
        self.assertEqual(result["source"], "transcripts")

    def test_get_call_transcript_falls_back_to_call_payload(self):
        def fake_api_get(path):
            if path == "/transcripts/call-123":
                raise DialpadApiError("missing", status_code=404)
            if path == "/call/call-123":
                return {"call_id": "call-123", "transcription_text": "Call text"}
            raise AssertionError(path)

        with patch("get_transcript.api_get", side_effect=fake_api_get):
            result = get_transcript.get_call_transcript("call-123")

        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "call")
        self.assertEqual(result["transcript_text"], "Call text")
        self.assertEqual(result["call"]["call_id"], "call-123")

    def test_get_call_transcript_reports_unavailable_when_no_text(self):
        def fake_api_get(path):
            if path == "/transcripts/call-123":
                return {"items": []}
            if path == "/call/call-123":
                return {"call_id": "call-123"}
            raise AssertionError(path)

        with patch("get_transcript.api_get", side_effect=fake_api_get):
            result = get_transcript.get_call_transcript("call-123")

        self.assertFalse(result["available"])
        self.assertIsNone(result["transcript_text"])
        self.assertEqual(result["unavailable_reason"], "no_transcript")

    def test_get_call_transcript_reports_not_found(self):
        with patch(
            "get_transcript.api_get",
            side_effect=DialpadApiError("missing", status_code=404),
        ):
            result = get_transcript.get_call_transcript("call-123")

        self.assertFalse(result["available"])
        self.assertEqual(result["unavailable_reason"], "not_found")

    def test_get_call_transcript_raises_non_404_errors(self):
        with patch(
            "get_transcript.api_get",
            side_effect=DialpadApiError("server error", status_code=500),
        ):
            with self.assertRaises(DialpadApiError):
                get_transcript.get_call_transcript("call-123")


if __name__ == "__main__":
    unittest.main()
