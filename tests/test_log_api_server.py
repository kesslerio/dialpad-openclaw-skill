from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from interaction_log import InteractionLog
from log_api_server import LogApiServer, create_server


@pytest.fixture
def running_server(tmp_path: Path):
    log = InteractionLog(sms_db=tmp_path / "sms.db", calls_db=tmp_path / "calls.db")
    # Bind the test socket locally while production create_server enforces the
    # Tailscale CGNAT range.
    server = LogApiServer(
        ("127.0.0.1", 0),
        token="unit-test-token",
        interaction_log=log,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(server, path: str, *, token: str | None = "unit-test-token", method: str = "GET", body: dict | None = None):
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    encoded = json.dumps(body).encode() if body is not None else None
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://{server.server_address[0]}:{server.server_address[1]}{path}",
        data=encoded,
        headers=headers,
        method=method,
    )
    return urllib.request.urlopen(request, timeout=2)


def test_api_rejects_missing_or_wrong_bearer_token(running_server) -> None:
    for token in (None, "wrong-token"):
        with pytest.raises(urllib.error.HTTPError) as raised:
            _request(running_server, "/health", token=token)
        assert raised.value.code == 401


def test_server_requires_token_and_rejects_wildcard_bind() -> None:
    with pytest.raises(RuntimeError, match="DIALPAD_LOG_TOKEN"):
        create_server(bind="100.85.254.62", port=0, token="")
    with pytest.raises(ValueError, match="non-wildcard"):
        create_server(bind="0.0.0.0", port=0, token="unit-test-token")


def test_record_endpoint_is_idempotent_and_thread_reads_same_data(running_server) -> None:
    observation = {
        "provider_id": "msg-api-1",
        "direction": "outbound",
        "from_number": "+14155550140",
        "to_number": "+14155550111",
        "body": "Recorded once",
        "timestamp": 1770000000000,
        "source": "local_send",
    }
    first = json.load(_request(running_server, "/v1/sms/record", method="POST", body=observation))
    second = json.load(_request(running_server, "/v1/sms/record", method="POST", body=observation))
    thread = json.load(
        _request(running_server, "/v1/sms/thread?phone=%2B14155550111&limit=10")
    )

    assert first["ok"] is True
    assert second["data"]["created"] is False
    assert thread["ok"] is True
    assert thread["data"]["count"] == 1
    assert thread["data"]["messages"][0]["text"] == "Recorded once"


def test_record_endpoint_does_not_expose_call_record_route(running_server) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(running_server, "/v1/calls/record", method="POST", body={})
    assert raised.value.code == 404
