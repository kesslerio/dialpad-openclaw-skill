"""Auth and storage-error responses for legacy POST /store."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import webhook_server

STORE_TOKEN = "unit-test-store-token"
_PROXY_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "Cdn-Loop")


def _handler(payload, *, headers=None, client_ip="127.0.0.1"):
    raw = json.dumps(payload).encode("utf-8")
    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler.headers = {"Content-Length": str(len(raw))}
    if headers:
        handler.headers.update(headers)
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.client_address = (client_ip, 12345)
    status = {"code": None}
    handler.send_response = lambda code: status.__setitem__("code", code)
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None
    handler.send_error = lambda code, *_args: status.__setitem__("code", code)
    return handler, status


def _json_body(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _store_payload():
    return {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": "+14155550140",
        "text": "hello",
    }


class StoreAuthTests(unittest.TestCase):
    def test_store_rejects_when_token_unset(self):
        handler, status = _handler(
            _store_payload(),
            headers={"X-Store-Token": STORE_TOKEN},
        )
        with patch.object(webhook_server, "DIALPAD_STORE_TOKEN", ""), patch.object(
            webhook_server, "handle_sms_webhook"
        ) as store:
            handler.handle_store()
        self.assertEqual(status["code"], 403)
        self.assertEqual(_json_body(handler), {"error": "forbidden"})
        self.assertNotIn(STORE_TOKEN, handler.wfile.getvalue().decode("utf-8"))
        store.assert_not_called()

    def test_store_rejects_without_presented_token(self):
        handler, status = _handler(_store_payload())
        with patch.object(webhook_server, "DIALPAD_STORE_TOKEN", STORE_TOKEN), patch.object(
            webhook_server, "handle_sms_webhook"
        ) as store:
            handler.handle_store()
        self.assertEqual(status["code"], 403)
        self.assertEqual(_json_body(handler), {"error": "forbidden"})
        store.assert_not_called()

    def test_store_rejects_wrong_token(self):
        handler, status = _handler(
            _store_payload(),
            headers={"X-Store-Token": "not-the-store-token"},
        )
        with patch.object(webhook_server, "DIALPAD_STORE_TOKEN", STORE_TOKEN), patch.object(
            webhook_server, "handle_sms_webhook"
        ) as store:
            handler.handle_store()
        self.assertEqual(status["code"], 403)
        self.assertEqual(_json_body(handler), {"error": "forbidden"})
        store.assert_not_called()

    def test_store_rejects_proxy_headers_from_localhost(self):
        for header in _PROXY_HEADERS:
            with self.subTest(header=header):
                handler, status = _handler(
                    _store_payload(),
                    headers={
                        "X-Store-Token": STORE_TOKEN,
                        header: "203.0.113.5",
                    },
                )
                with patch.object(webhook_server, "DIALPAD_STORE_TOKEN", STORE_TOKEN), patch.object(
                    webhook_server, "handle_sms_webhook"
                ) as store:
                    handler.handle_store()
                self.assertEqual(status["code"], 403)
                self.assertEqual(_json_body(handler), {"error": "forbidden"})
                store.assert_not_called()

    def test_store_rejects_remote_client_with_valid_token(self):
        handler, status = _handler(
            _store_payload(),
            headers={"X-Store-Token": STORE_TOKEN},
            client_ip="203.0.113.10",
        )
        with patch.object(webhook_server, "DIALPAD_STORE_TOKEN", STORE_TOKEN), patch.object(
            webhook_server, "handle_sms_webhook"
        ) as store:
            handler.handle_store()
        self.assertEqual(status["code"], 403)
        self.assertEqual(_json_body(handler), {"error": "forbidden"})
        store.assert_not_called()

    def test_store_accepts_valid_token_from_localhost(self):
        for client_ip, headers in (
            ("127.0.0.1", {"X-Store-Token": STORE_TOKEN}),
            ("::1", {"Authorization": f"Bearer {STORE_TOKEN}"}),
        ):
            with self.subTest(client_ip=client_ip, headers=tuple(headers)):
                handler, status = _handler(_store_payload(), headers=headers, client_ip=client_ip)
                stored = {"stored": True, "status": "success"}
                with patch.object(webhook_server, "DIALPAD_STORE_TOKEN", STORE_TOKEN), patch.object(
                    webhook_server, "handle_sms_webhook", return_value=stored
                ):
                    handler.handle_store()
                self.assertEqual(status["code"], 200)
                self.assertEqual(_json_body(handler), stored)

    def test_store_storage_exception_body_is_generic(self):
        handler, status = _handler(
            _store_payload(),
            headers={"X-Store-Token": STORE_TOKEN},
        )
        detail = "sqlite disk I/O error at /var/lib/private/sms.db"
        stdout = io.StringIO()
        with patch.object(webhook_server, "DIALPAD_STORE_TOKEN", STORE_TOKEN), patch.object(
            webhook_server, "handle_sms_webhook", side_effect=RuntimeError(detail)
        ), redirect_stdout(stdout):
            handler.handle_store()
        body = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(status["code"], 500)
        self.assertEqual(json.loads(body), {"error": "internal error"})
        self.assertNotIn(detail, body)
        self.assertIn(detail, stdout.getvalue())

    def test_store_failed_result_body_is_generic(self):
        handler, status = _handler(
            _store_payload(),
            headers={"X-Store-Token": STORE_TOKEN},
        )
        detail = "schema mismatch on private column"
        stdout = io.StringIO()
        with patch.object(webhook_server, "DIALPAD_STORE_TOKEN", STORE_TOKEN), patch.object(
            webhook_server,
            "handle_sms_webhook",
            return_value={"stored": False, "error": detail},
        ), redirect_stdout(stdout):
            handler.handle_store()
        body = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(status["code"], 500)
        self.assertEqual(json.loads(body), {"error": "internal error"})
        self.assertNotIn(detail, body)
        self.assertIn(detail, stdout.getvalue())


class WebhookStorageErrorTests(unittest.TestCase):
    def test_webhook_storage_exception_body_is_generic(self):
        payload = {
            **_store_payload(),
            "message_id": "storelock-err-1",
        }
        handler, status = _handler(payload)
        detail = "UNIQUE constraint failed: messages.private_note"
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        stdout = io.StringIO()
        with patch.object(webhook_server, "verify_webhook_auth", return_value=(True, "test")), patch.object(
            webhook_server, "handle_sms_webhook", side_effect=RuntimeError(detail)
        ), patch.object(
            webhook_server, "_sms_dedupe_db_path", return_value=Path(tmp.name)
        ), redirect_stdout(stdout):
            handler.handle_webhook()
        body = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(status["code"], 500)
        self.assertEqual(json.loads(body), {"error": "internal error"})
        self.assertNotIn(detail, body)
        self.assertIn(detail, stdout.getvalue())
        again = webhook_server.claim_sms_webhook_event("storelock-err-1", db_path=tmp.name)
        self.assertTrue(again["claimed"])
        self.assertFalse(again["duplicate"])


class BindHostConfigTests(unittest.TestCase):
    def _import_config(self, bind_hosts):
        env = os.environ.copy()
        env.pop("DIALPAD_STORE_TOKEN", None)
        if bind_hosts is None:
            env.pop("DIALPAD_BIND_HOSTS", None)
        else:
            env["DIALPAD_BIND_HOSTS"] = bind_hosts
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.path.insert(0, 'scripts'); "
                    "import webhook_server; "
                    "print(webhook_server.DIALPAD_BIND_HOSTS); "
                    "print(repr(webhook_server.DIALPAD_STORE_TOKEN))"
                ),
            ],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        bind_line, token_line = result.stdout.strip().splitlines()
        return bind_line, token_line

    def test_defaults_are_loopback_and_unset_store_token(self):
        bind_line, token_line = self._import_config(None)
        self.assertEqual(bind_line, "['127.0.0.1']")
        self.assertEqual(token_line, "''")

    def test_bind_hosts_split_and_blank_fallback(self):
        bind_line, _token_line = self._import_config("127.0.0.1, 172.17.0.1,")
        self.assertEqual(bind_line, "['127.0.0.1', '172.17.0.1']")
        blank_line, _token_line = self._import_config(" , ")
        self.assertEqual(blank_line, "['127.0.0.1']")
