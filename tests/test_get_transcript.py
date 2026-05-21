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

    def test_format_transcript_reads_dialpad_lines(self):
        payload = {
            "call_id": "call-123",
            "lines": [
                {
                    "type": "moment",
                    "name": "Agent",
                    "content": "whole_call_summary_fragment",
                },
                {
                    "type": "transcript",
                    "name": "Agent",
                    "content": "Hello.",
                },
                {
                    "type": "transcript",
                    "name": "Prospect",
                    "content": "I have a question.",
                },
            ],
        }

        self.assertEqual(
            get_transcript.format_transcript(payload),
            "Agent: Hello.\nProspect: I have a question.",
        )

    def test_format_transcript_keeps_typed_non_line_items(self):
        payload = {
            "items": [
                {"type": "message", "speaker_name": "Agent", "text": "Hello"},
            ],
        }

        self.assertEqual(get_transcript.format_transcript(payload), "Agent: Hello")

    def test_get_call_transcript_returns_endpoint_transcript(self):
        def fake_api_get(path):
            if path == "/transcripts/call-123/url":
                return {"url": "https://dialpad.com/review/call-123"}
            if path == "/transcripts/call-123":
                return {"transcript": "Transcript body"}
            raise AssertionError(path)

        with patch("get_transcript.api_get", side_effect=fake_api_get) as api_get:
            result = get_transcript.get_call_transcript("call-123", call={"call_id": "call-123"})

        self.assertEqual(api_get.call_count, 2)
        self.assertTrue(result["available"])
        self.assertEqual(result["call_id"], "call-123")
        self.assertEqual(result["transcript_text"], "Transcript body")
        self.assertEqual(result["transcript_review_url"], "https://dialpad.com/review/call-123")
        self.assertEqual(result["source"], "transcripts")

    def test_get_call_transcript_falls_back_to_call_payload(self):
        def fake_api_get(path):
            if path == "/transcripts/call-123/url":
                return {}
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
            if path == "/transcripts/call-123/url":
                raise DialpadApiError("missing", status_code=404)
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
        with patch("get_transcript.api_get", side_effect=DialpadApiError("missing", status_code=404)):
            result = get_transcript.get_call_transcript("call-123")

        self.assertFalse(result["available"])
        self.assertEqual(result["unavailable_reason"], "not_found")

    def test_get_call_transcript_raises_non_404_errors(self):
        def fake_api_get(path):
            if path == "/transcripts/call-123/url":
                return {}
            raise DialpadApiError("server error", status_code=500)

        with patch("get_transcript.api_get", side_effect=fake_api_get):
            with self.assertRaises(DialpadApiError):
                get_transcript.get_call_transcript("call-123")

    def test_get_call_transcript_ignores_review_url_errors(self):
        def fake_api_get(path):
            if path == "/transcripts/call-123/url":
                raise DialpadApiError("url failed", status_code=500)
            if path == "/transcripts/call-123":
                return {"transcript": "Transcript body"}
            raise AssertionError(path)

        with patch("get_transcript.api_get", side_effect=fake_api_get):
            result = get_transcript.get_call_transcript("call-123")

        self.assertTrue(result["available"])
        self.assertEqual(result["transcript_text"], "Transcript body")
        self.assertIsNone(result["transcript_review_url"])

    def test_format_transcript_review_url_reads_url_fields(self):
        self.assertEqual(
            get_transcript.format_transcript_review_url({"transcript_url": " https://example.test/review "}),
            "https://example.test/review",
        )


if __name__ == "__main__":
    unittest.main()
