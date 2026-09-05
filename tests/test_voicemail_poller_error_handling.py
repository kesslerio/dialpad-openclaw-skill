import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import poll_voicemails


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class VoicemailPollerErrorTests(unittest.TestCase):
    def test_main_exits_with_error_on_missing_api_key(self):
        with patch.dict("poll_voicemails.os.environ", {"DIALPAD_API_KEY": ""}, clear=True):
            with patch("poll_voicemails.DIALPAD_API_KEY", ""):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    exit_code = poll_voicemails.main()
                    self.assertEqual(exit_code, 2)
                    self.assertNotIn("found 0 voicemail(s)", mock_stdout.getvalue())

    def test_main_exits_with_error_on_http_failure(self):
        with patch("poll_voicemails.urllib.request.urlopen", side_effect=urllib.error.HTTPError("url", 500, "Internal Server Error", {}, None)):
            with patch.dict("poll_voicemails.os.environ", {"DIALPAD_API_KEY": "fake-key"}):
                with patch("poll_voicemails.sqlite3.connect"):
                    with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                        exit_code = poll_voicemails.main()
                        self.assertEqual(exit_code, 1)
                        self.assertNotIn("found 0 voicemail(s)", mock_stdout.getvalue())

    def test_main_exits_with_error_on_network_timeout(self):
        with patch("poll_voicemails.urllib.request.urlopen", side_effect=RuntimeError("The read operation timed out")):
            with patch.dict("poll_voicemails.os.environ", {"DIALPAD_API_KEY": "fake-key"}):
                with patch("poll_voicemails.sqlite3.connect"):
                    with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                        exit_code = poll_voicemails.main()
                        self.assertEqual(exit_code, 1)
                        self.assertNotIn("found 0 voicemail(s)", mock_stdout.getvalue())

    def test_main_exits_with_error_on_sqlite_failure(self):
        with patch.dict("poll_voicemails.os.environ", {"DIALPAD_API_KEY": "fake-key"}):
            with patch("poll_voicemails.sqlite3.connect", side_effect=poll_voicemails.sqlite3.OperationalError("disk I/O error")):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    exit_code = poll_voicemails.main()
                    self.assertEqual(exit_code, 1)
                    self.assertNotIn("found 0 voicemail(s)", mock_stdout.getvalue())

    def test_main_exits_with_zero_on_success_with_no_calls(self):
        response = _FakeResponse({"items": []})
        with patch("poll_voicemails.urllib.request.urlopen", return_value=response):
            with patch.dict("poll_voicemails.os.environ", {"DIALPAD_API_KEY": "fake-key"}):
                with patch("poll_voicemails.sqlite3.connect"):
                    with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                        exit_code = poll_voicemails.main()
                        self.assertEqual(exit_code, 0)
                        self.assertIn("found 0 voicemail(s), notified 0 new", mock_stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
