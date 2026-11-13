"""Tests for scripts/team_mcp_server.py — the loopback team MCP server."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import send_api_server
import sms_sqlite
from team_mcp_server import TeamMcpConfigError, create_server

SCOTT_LINE = "+14152230323"
LILLA_LINE = "+14158701945"
OFFICE_LINE = "+14159065785"
SALES_LINE = "+14155201316"
SUPPORT_LINE = "+14159917155"
CONTACT = "+14155550111"

GATE_CONFIG = {
    "shared_lines": {"office": OFFICE_LINE, "sales": SALES_LINE},
    "teammates": {"scott": SCOTT_LINE, "lilla": LILLA_LINE},
    "callers": {
        "team-scott": {"shared_lines": ["office", "sales"], "acts_for": "scott"},
        "team-lilla": {"shared_lines": ["office"], "acts_for": "lilla"},
        "operator": {"shared_lines": ["office", "sales"]},
        "service-bot": {"shared_lines": ["office"]},
    },
}

SCOTT_TOKEN = "fake-scott-mcp-token-0123456789abcdef"
LILLA_TOKEN = "fake-lilla-mcp-token-0123456789abcdef"
SERVICE_TOKEN = "fake-service-mcp-token-0123456789abcdef"
TEAM_DOMAIN = "team.cloudflareaccess.com"
AUD = "aud-tag-123"
SCOTT_EMAIL = "scott@example.com"

OPENSSL = shutil.which("openssl")


def _mcp_config(tmp_path: Path, **overrides) -> dict:
    config = {
        "access": {"team_domain": TEAM_DOMAIN, "aud": AUD},
        "email_callers": {SCOTT_EMAIL: "team-scott", "op@example.com": "operator"},
        "allow_bearer_fallback": True,
        "allowed_origins": [],
        "alerts": {
            "store": str(tmp_path / "team-alerts.json"),
            "allowed_url_hosts": ["hooks.example.com"],
            "callers": {"team-scott": {"shared_lines": ["office"]}},
        },
    }
    config.update(overrides)
    return config


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _map_entries() -> dict:
    return {
        _digest(SCOTT_TOKEN): {"caller": "team-scott", "label": "scott-mcp"},
        _digest(LILLA_TOKEN): {"caller": "team-lilla", "label": "lilla-mcp"},
        _digest(SERVICE_TOKEN): {"caller": "service-bot", "label": "service-mcp"},
    }


class RecordingRunner:
    """Fake send runner: records the call and returns a canned envelope."""

    def __init__(self, envelope=None, stdout=None):
        self.calls: list[dict] = []
        self.envelope = envelope if envelope is not None else {
            "ok": True,
            "command": "send_sms.send",
            "data": {"mode": "real_send", "id": "msg-1"},
            "meta": {},
        }
        self.stdout = stdout

    def __call__(self, argv, *, message, env, timeout):
        self.calls.append({"argv": list(argv), "message": message, "env": dict(env), "timeout": timeout})
        raw = self.stdout if self.stdout is not None else json.dumps(self.envelope)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout=raw, stderr=b"")


def _seed_db(path: Path) -> None:
    conn = sms_sqlite.init_db(path)
    base = 1700000000000
    sms_sqlite.store_message(conn, {
        "id": 1, "direction": "inbound", "from_number": CONTACT, "to_number": [SCOTT_LINE],
        "text": "hi scott", "created_date": base,
    })
    sms_sqlite.store_message(conn, {
        "id": 2, "direction": "outbound", "from_number": OFFICE_LINE, "to_number": [CONTACT],
        "text": "office out", "created_date": base + 1000,
    })
    sms_sqlite.store_message(conn, {
        "id": 3, "direction": "inbound", "from_number": "+14155550222", "to_number": [SALES_LINE],
        "text": "sales in", "created_date": base + 2000,
    })
    sms_sqlite.store_message(conn, {
        "id": 4, "direction": "inbound", "from_number": CONTACT, "to_number": [LILLA_LINE],
        "text": "lilla in", "created_date": base + 3000,
    })
    sms_sqlite.store_message(conn, {
        "id": 5, "direction": "inbound", "from_number": CONTACT, "to_number": [SUPPORT_LINE],
        "text": "support in", "created_date": base + 4000,
    })
    sms_sqlite.store_message(conn, {
        "id": 6, "direction": "inbound", "from_number": CONTACT, "to_number": [SCOTT_LINE],
        "text": "hi scott again", "created_date": base + 5000,
    })
    conn.close()


@pytest.fixture
def token_map_path(tmp_path: Path) -> Path:
    path = tmp_path / "send-api-tokens.json"
    send_api_server.write_token_map(path, _map_entries())
    return path


@pytest.fixture
def sms_db(tmp_path: Path) -> Path:
    path = tmp_path / "sms.db"
    _seed_db(path)
    return path


@pytest.fixture
def make_server(tmp_path, token_map_path, sms_db):
    servers: list = []

    def _make(runner=None, config=None, env=None, jwks_fetcher=None, **overrides):
        kwargs = {
            "bind": "127.0.0.1",
            "port": 0,
            "config": config if config is not None else _mcp_config(tmp_path),
            "gate_config": GATE_CONFIG,
            "tokens_path": token_map_path,
            "send_runner": runner or RecordingRunner(),
            "env": env if env is not None else {"DIALPAD_SMS_DB": str(sms_db)},
            "jwks_fetcher": jwks_fetcher,
        }
        kwargs.update(overrides)
        server = create_server(**kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    yield _make
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def server(make_server):
    return make_server()


def _request(server, path: str, *, method="POST", body=None, token=SCOTT_TOKEN, jwt=None, headers=None):
    request_headers = {"Content-Type": "application/json"}
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    if jwt is not None:
        request_headers["Cf-Access-Jwt-Assertion"] = jwt
    request_headers.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://{server.server_address[0]}:{server.server_address[1]}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    return urllib.request.urlopen(request, timeout=5)


def _rpc(server, method, params=None, *, request_id=1, **kwargs):
    payload = {"jsonrpc": "2.0", "method": method, "id": request_id}
    if params is not None:
        payload["params"] = params
    return _request(server, "/mcp", body=payload, **kwargs)


def _tool_call(server, name, arguments=None, **kwargs):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    response = json.load(_rpc(server, "tools/call", params, **kwargs))
    return response["result"]


def _tool_payload(result) -> dict:
    return json.loads(result["content"][0]["text"])


# ---------------------------------------------------------------------------
# RSA key material for JWT tests (generated at test time; never committed)
# ---------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def rsa_key(tmp_path_factory):
    if OPENSSL is None:
        pytest.skip("openssl CLI is required to mint RSA test keys")
    workdir = tmp_path_factory.mktemp("jwt-keys")
    key_path = workdir / "key.pem"
    subprocess.run([OPENSSL, "genrsa", "-out", str(key_path), "2048"], check=True, capture_output=True)
    modulus = subprocess.run(
        [OPENSSL, "rsa", "-in", str(key_path), "-noout", "-modulus"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    n = int(modulus.split("=", 1)[1], 16)
    e = 65537
    return {
        "key_path": key_path,
        "n": n.to_bytes((n.bit_length() + 7) // 8, "big"),
        "e": e.to_bytes((e.bit_length() + 7) // 8, "big"),
    }


def _jwks_fetcher(rsa_key):
    def fetch(url: str) -> dict:
        assert url == f"https://{TEAM_DOMAIN}/cdn-cgi/access/certs"
        return {"keys": [{"kty": "RSA", "kid": "test-key", "n": _b64(rsa_key["n"]), "e": _b64(rsa_key["e"])}]}

    return fetch


def _claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": f"https://{TEAM_DOMAIN}",
        "aud": AUD,
        "exp": now + 3600,
        "iat": now - 10,
        "nbf": now - 10,
        "email": SCOTT_EMAIL,
    }
    claims.update(overrides)
    return claims


def _jwt(rsa_key, claims, *, header_overrides=None, tamper=False) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": "test-key"}
    header.update(header_overrides or {})
    head = _b64(json.dumps(header).encode())
    body = _b64(json.dumps(claims).encode())
    signing_input = f"{head}.{body}"
    signature = subprocess.run(
        [OPENSSL, "dgst", "-sha256", "-sign", str(rsa_key["key_path"])],
        input=signing_input.encode(),
        check=True,
        capture_output=True,
    ).stdout
    if tamper:
        signature = bytes(b ^ 0xFF for b in signature[:8]) + signature[8:]
    return f"{signing_input}.{_b64(signature)}"


needs_openssl = pytest.mark.skipif(OPENSSL is None, reason="openssl CLI is required for JWT tests")


# ---------------------------------------------------------------------------
# MCP transport
# ---------------------------------------------------------------------------


def test_initialize_tools_list_round_trip(server) -> None:
    response = json.load(_rpc(server, "initialize", {"protocolVersion": "2025-03-26"}))
    result = response["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "dialpad-team-mcp"
    assert "tools" in result["capabilities"]

    response = json.load(_rpc(server, "initialize", {"protocolVersion": "1999-01-01"}))
    assert response["result"]["protocolVersion"] == "2025-06-18"

    response = json.load(_rpc(server, "ping"))
    assert response["result"] == {}

    response = json.load(_rpc(server, "tools/list"))
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    assert set(tools) == {"send_sms", "list_conversations", "read_thread", "set_sms_alert_url"}
    for tool in tools.values():
        assert tool["inputSchema"]["additionalProperties"] is False


def test_initialized_notification_returns_202(server) -> None:
    response = _request(server, "/mcp", body={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status == 202
    assert response.read() == b""


def test_json_rpc_error_paths(server) -> None:
    response = json.load(_rpc(server, "no/such/method"))
    assert response["error"]["code"] == -32601

    response = json.load(_rpc(server, "tools/call", {"name": "nope"}))
    assert response["error"]["code"] == -32602

    response = json.load(_rpc(server, "tools/call", {"name": "send_sms", "arguments": [1, 2]}))
    assert response["error"]["code"] == -32602

    raw = urllib.request.Request(
        f"http://{server.server_address[0]}:{server.server_address[1]}/mcp",
        data=json.dumps([{"jsonrpc": "2.0", "method": "ping", "id": 1}]).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {SCOTT_TOKEN}"},
    )
    response = json.load(urllib.request.urlopen(raw, timeout=5))
    assert response["error"]["code"] == -32600
    assert "batch" in response["error"]["message"].lower()

    raw = urllib.request.Request(
        f"http://{server.server_address[0]}:{server.server_address[1]}/mcp",
        data=b"{not json",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {SCOTT_TOKEN}"},
    )
    response = json.load(urllib.request.urlopen(raw, timeout=5))
    assert response["error"]["code"] == -32700


def test_routes_and_methods(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/mcp", method="GET")
    assert raised.value.code == 405
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/mcp", method="DELETE")
    assert raised.value.code == 405
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/other", body={})
    assert raised.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="POST", body={})
    assert raised.value.code == 405

    response = json.load(_request(server, "/health", method="GET"))
    assert response == {"caller": "team-scott"}


def test_origin_validation(make_server, tmp_path) -> None:
    server = make_server()
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", headers={"Origin": "https://app.example.com"})
    assert raised.value.code == 403

    server = make_server(config=_mcp_config(tmp_path, allowed_origins=["https://app.example.com"]))
    response = json.load(_request(server, "/health", method="GET", headers={"Origin": "https://app.example.com"}))
    assert response["caller"] == "team-scott"
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", headers={"Origin": "https://evil.example.com"})
    assert raised.value.code == 403


def test_no_auth_is_401(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", token=None)
    assert raised.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as raised:
        _rpc(server, "ping", token="wrong-token")
    assert raised.value.code == 401


# ---------------------------------------------------------------------------
# send_sms tool
# ---------------------------------------------------------------------------


def test_send_sms_defaults_from_own_line(server) -> None:
    result = _tool_call(server, "send_sms", {"to": "+13105550100", "message": "hello there"})
    assert result["isError"] is False
    argv = server.send_runner.calls[0]["argv"]
    assert argv[argv.index("--caller") + 1] == "team-scott"
    assert argv[argv.index("--from") + 1] == SCOTT_LINE
    assert "--message-stdin" in argv


def test_send_sms_message_on_stdin_and_env_scrubbed(make_server, sms_db) -> None:
    server = make_server(env={"DIALPAD_SMS_DB": str(sms_db), "DIALPAD_SEND_CALLER": "team-lilla"})
    secret = "secret body never on argv"
    _tool_call(server, "send_sms", {"to": "+13105550100", "message": secret, "dry_run": True})
    call = server.send_runner.calls[0]
    assert all(secret not in arg for arg in call["argv"])
    assert call["message"] == secret
    assert "--dry-run" in call["argv"]
    assert call["env"].get("DIALPAD_SEND_CALLER") is None


def test_send_sms_cross_teammate_from_refused_end_to_end(tmp_path, token_map_path, sms_db) -> None:
    """team-scott sending from lilla's line hits the real gate and refuses."""
    gate_path = tmp_path / "send-gate.json"
    gate_path.write_text(json.dumps(GATE_CONFIG))
    server = create_server(
        bind="127.0.0.1",
        port=0,
        config=_mcp_config(tmp_path),
        gate_config=GATE_CONFIG,
        tokens_path=token_map_path,
        send_runner=send_api_server.default_send_runner,
        env={"DIALPAD_SMS_DB": str(sms_db), "DIALPAD_SEND_GATE_CONFIG": str(gate_path)},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _tool_call(
            server,
            "send_sms",
            {"to": "+13105550100", "from": LILLA_LINE, "message": "hi", "dry_run": True},
        )
        assert result["isError"] is True
        payload = _tool_payload(result)
        assert payload["error"]["code"] == "send_gate_refused"
        assert any("not an approved line" in reason for reason in payload["error"]["reasons"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("field", ["to", "from"])
def test_send_sms_support_line_refused_before_anything(server, field) -> None:
    args = {"to": "+13105550100", "from": SCOTT_LINE, "message": "hi", field: SUPPORT_LINE}
    result = _tool_call(server, "send_sms", args)
    assert result["isError"] is True
    assert _tool_payload(result)["error"]["code"] == "support_line_refused"
    assert server.send_runner.calls == []


def test_send_sms_gate_refusal_details_passthrough(make_server) -> None:
    envelope = {
        "ok": False,
        "command": "send_sms.send",
        "error": {"code": "send_gate_refused", "message": "Send gate refused", "retryable": True},
        "meta": {
            "send_gate": {
                "allowed": False,
                "reasons": ["recipient +13105550100 outside send window: 22:40 local (America/Los_Angeles)"],
                "retryable": True,
                "retry_after_seconds": 300,
                "caller": "team-scott",
            }
        },
    }
    server = make_server(runner=RecordingRunner(envelope=envelope))
    result = _tool_call(server, "send_sms", {"to": "+13105550100", "message": "hi", "dry_run": True})
    payload = _tool_payload(result)
    assert payload["error"]["code"] == "send_gate_refused"
    assert payload["error"]["retryable"] is True
    assert payload["error"]["retry_after_seconds"] == 300
    assert payload["error"]["send_gate"]["caller"] == "team-scott"


def test_send_sms_timeout_maps_to_unknown_outcome(make_server) -> None:
    def slow_runner(argv, *, message, env, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    server = make_server(runner=slow_runner)
    result = _tool_call(server, "send_sms", {"to": "+13105550100", "message": "hi"})
    payload = _tool_payload(result)
    assert payload["error"]["code"] == "send_outcome_unknown"
    assert payload["error"]["retryable"] is False


def test_send_sms_unknown_arg_is_refused(server) -> None:
    for extra in ({"caller": "team-lilla"}, {"acting_for": "lilla"}, {"verbose": True}):
        result = _tool_call(server, "send_sms", {"to": "+13105550100", "message": "hi", **extra})
        assert result["isError"] is True
        assert _tool_payload(result)["error"]["code"] == "invalid_argument"
    assert server.send_runner.calls == []


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def test_list_conversations_scoped_to_allowed_lines(server) -> None:
    result = _tool_call(server, "list_conversations", {})
    assert result["isError"] is False
    payload = _tool_payload(result)
    lines = {entry["line"] for entry in payload["conversations"]}
    assert lines == {SCOTT_LINE, OFFICE_LINE, SALES_LINE}
    dumped = json.dumps(payload)
    assert LILLA_LINE not in dumped
    assert SUPPORT_LINE not in dumped
    assert "lilla in" not in dumped
    assert "support in" not in dumped
    scott_thread = next(e for e in payload["conversations"] if e["line"] == SCOTT_LINE)
    assert scott_thread["unread_count"] == 2
    assert scott_thread["preview"] == "hi scott again"
    assert scott_thread["direction"] == "inbound"


def test_list_conversations_limit_and_since(server) -> None:
    result = _tool_call(server, "list_conversations", {"limit": 1})
    assert len(_tool_payload(result)["conversations"]) == 1
    result = _tool_call(server, "list_conversations", {"since": "2999-01-01T00:00:00Z"})
    assert _tool_payload(result)["conversations"] == []
    result = _tool_call(server, "list_conversations", {"since": 1700000004000})
    lines = {e["line"] for e in _tool_payload(result)["conversations"]}
    assert lines == {SCOTT_LINE}
    result = _tool_call(server, "list_conversations", {"limit": "many"})
    assert result["isError"] is True


def test_read_thread_scoped_to_allowed_lines(server) -> None:
    result = _tool_call(server, "read_thread", {"contact": CONTACT})
    assert result["isError"] is False
    payload = _tool_payload(result)
    lines = {message["line"] for message in payload["messages"]}
    assert lines == {SCOTT_LINE, OFFICE_LINE}
    texts = [message["text"] for message in payload["messages"]]
    assert "lilla in" not in texts
    assert "support in" not in texts
    assert texts == ["hi scott", "office out", "hi scott again"]


def test_read_thread_line_narrowing(server) -> None:
    result = _tool_call(server, "read_thread", {"contact": CONTACT, "line": OFFICE_LINE})
    payload = _tool_payload(result)
    assert [message["text"] for message in payload["messages"]] == ["office out"]

    result = _tool_call(server, "read_thread", {"contact": CONTACT, "line": LILLA_LINE})
    assert result["isError"] is True
    assert _tool_payload(result)["error"]["code"] == "line_not_allowed"

    result = _tool_call(server, "read_thread", {"contact": CONTACT, "line": SUPPORT_LINE})
    assert _tool_payload(result)["error"]["code"] == "line_not_allowed"


def test_read_thread_national_contact_form(server) -> None:
    result = _tool_call(server, "read_thread", {"contact": "4155550111"})
    payload = _tool_payload(result)
    assert payload["contact"] == CONTACT
    assert len(payload["messages"]) == 3


# ---------------------------------------------------------------------------
# set_sms_alert_url tool
# ---------------------------------------------------------------------------


def test_set_sms_alert_url_registers_without_echoing_key(server, tmp_path) -> None:
    result = _tool_call(
        server, "set_sms_alert_url", {"url": "https://hooks.example.com/sms", "key": "secret-key-1"}
    )
    assert result["isError"] is False
    payload = _tool_payload(result)
    dumped = json.dumps(result)
    assert "secret-key-1" not in dumped
    assert payload["url"] == "https://hooks.example.com/sms"
    assert payload["alert_lines"] == sorted([SCOTT_LINE, OFFICE_LINE])
    store = json.loads((tmp_path / "team-alerts.json").read_text())
    assert store["team-scott"]["key"] == "secret-key-1"


def test_set_sms_alert_url_validation(server) -> None:
    cases = [
        ({"url": "https://evil.example.net/x", "key": "k"}, "invalid_argument"),
        ({"url": "http://example.com/x", "key": "k"}, "invalid_argument"),
        ({"url": "https://127.0.0.1/x", "key": "k"}, "invalid_argument"),
        ({"url": "https://user:pw@hooks.example.com/x", "key": "k"}, "invalid_argument"),
        ({"url": "https://hooks.example.com/x", "key": ""}, "invalid_argument"),
        ({"url": "https://hooks.example.com/x", "key": "k" * 300}, "invalid_argument"),
    ]
    for arguments, code in cases:
        result = _tool_call(server, "set_sms_alert_url", arguments)
        assert result["isError"] is True, arguments
        assert _tool_payload(result)["error"]["code"] == code


def test_set_sms_alert_url_clear(make_server, tmp_path) -> None:
    server = make_server()
    _tool_call(server, "set_sms_alert_url", {"url": "https://hooks.example.com/sms", "key": "k"})
    result = _tool_call(server, "set_sms_alert_url", {"url": "", "key": ""})
    payload = _tool_payload(result)
    assert payload["registered"] is False
    assert payload["alert_lines"] == []
    store = json.loads((tmp_path / "team-alerts.json").read_text())
    assert store == {}


def test_set_sms_alert_url_not_configured(make_server, tmp_path) -> None:
    config = _mcp_config(tmp_path)
    del config["alerts"]
    server = make_server(config=config)
    result = _tool_call(
        server, "set_sms_alert_url", {"url": "https://hooks.example.com/x", "key": "k"}
    )
    assert _tool_payload(result)["error"]["code"] == "alerts_not_configured"


# ---------------------------------------------------------------------------
# Identity: bearer fallback and network mappability
# ---------------------------------------------------------------------------


def test_second_token_maps_to_its_own_caller(server) -> None:
    response = json.load(_request(server, "/health", method="GET", token=LILLA_TOKEN))
    assert response["caller"] == "team-lilla"


def test_service_token_without_acts_for_is_not_mappable(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", token=SERVICE_TOKEN)
    assert raised.value.code == 403
    assert json.loads(raised.value.read())["error"]["code"] == "caller_not_mappable"


def test_bearer_fallback_can_be_disabled(make_server, tmp_path) -> None:
    server = make_server(config=_mcp_config(tmp_path, allow_bearer_fallback=False))
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", token=SCOTT_TOKEN)
    assert raised.value.code == 401


def test_bearer_fallback_enabled_by_config_reload(tmp_path, token_map_path, sms_db) -> None:
    """allow_bearer_fallback false->true via the mtime reload needs no restart."""
    config_path = tmp_path / "team-mcp.json"
    config_path.write_text(json.dumps(_mcp_config(tmp_path, allow_bearer_fallback=False)))
    os.chmod(config_path, 0o600)
    server = create_server(
        bind="127.0.0.1",
        port=0,
        config_path=config_path,
        gate_config=GATE_CONFIG,
        tokens_path=token_map_path,
        send_runner=RecordingRunner(),
        env={"DIALPAD_SMS_DB": str(sms_db)},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            _request(server, "/health", method="GET")
        assert raised.value.code == 401

        config_path.write_text(json.dumps(_mcp_config(tmp_path, allow_bearer_fallback=True)))
        stat_result = config_path.stat()
        os.utime(config_path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 10_000_000))
        response = json.load(_request(server, "/health", method="GET"))
        assert response["caller"] == "team-scott"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Identity: Cloudflare Access JWT
# ---------------------------------------------------------------------------


@needs_openssl
def test_jwt_valid_maps_email_to_caller(make_server, tmp_path, rsa_key) -> None:
    server = make_server(jwks_fetcher=_jwks_fetcher(rsa_key))
    token = _jwt(rsa_key, _claims())
    response = json.load(_request(server, "/health", method="GET", jwt=token, token=None))
    assert response == {"caller": "team-scott"}


@needs_openssl
@pytest.mark.parametrize(
    "mutate",
    [
        "bad_signature",
        "wrong_aud",
        "wrong_iss",
        "expired",
        "alg_none",
        "unknown_email",
    ],
)
def test_jwt_refusals(make_server, tmp_path, rsa_key, mutate) -> None:
    server = make_server(jwks_fetcher=_jwks_fetcher(rsa_key))
    if mutate == "bad_signature":
        token = _jwt(rsa_key, _claims(), tamper=True)
        expected = 401
    elif mutate == "wrong_aud":
        token = _jwt(rsa_key, _claims(aud="other-aud"))
        expected = 401
    elif mutate == "wrong_iss":
        token = _jwt(rsa_key, _claims(iss="https://evil.cloudflareaccess.com"))
        expected = 401
    elif mutate == "expired":
        token = _jwt(rsa_key, _claims(exp=int(time.time()) - 600))
        expected = 401
    elif mutate == "alg_none":
        token = _jwt(rsa_key, _claims(), header_overrides={"alg": "none", "kid": None})
        expected = 401
    else:
        token = _jwt(rsa_key, _claims(email="stranger@example.com"))
        expected = 403
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", jwt=token, token=None)
    assert raised.value.code == expected


@needs_openssl
def test_jwt_invalid_never_falls_back_to_bearer(make_server, tmp_path, rsa_key) -> None:
    server = make_server(jwks_fetcher=_jwks_fetcher(rsa_key))
    bad = _jwt(rsa_key, _claims(), tamper=True)
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", jwt=bad, token=SCOTT_TOKEN)
    assert raised.value.code == 401


@needs_openssl
def test_jwt_header_without_access_config_is_refused(make_server, tmp_path, rsa_key) -> None:
    config = _mcp_config(tmp_path)
    del config["access"]
    server = make_server(config=config, jwks_fetcher=_jwks_fetcher(rsa_key))
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", jwt=_jwt(rsa_key, _claims()), token=None)
    assert raised.value.code == 401


@needs_openssl
def test_jwt_email_mapped_to_unmappable_caller(make_server, tmp_path, rsa_key) -> None:
    server = make_server(jwks_fetcher=_jwks_fetcher(rsa_key))
    token = _jwt(rsa_key, _claims(email="op@example.com"))
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", jwt=token, token=None)
    assert raised.value.code == 403


@needs_openssl
def test_jwt_unknown_kid_refetches_once_then_refuses(make_server, tmp_path, rsa_key) -> None:
    calls = []

    def fetch(url):
        calls.append(url)
        return {"keys": [{"kty": "RSA", "kid": "other-key", "n": _b64(rsa_key["n"]), "e": _b64(rsa_key["e"])}]}

    server = make_server(jwks_fetcher=fetch)
    token = _jwt(rsa_key, _claims())
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", jwt=token, token=None)
    assert raised.value.code == 401
    assert len(calls) <= 1


# ---------------------------------------------------------------------------
# Startup refusals and file-backed config reload
# ---------------------------------------------------------------------------


def test_create_server_refuses_non_loopback_bind(token_map_path, tmp_path) -> None:
    for bind in ("0.0.0.0", "10.0.0.5", "localhost", "::1"):
        with pytest.raises(ValueError):
            create_server(
                bind=bind, port=0, config=_mcp_config(tmp_path), gate_config=GATE_CONFIG,
                tokens_path=token_map_path, send_runner=RecordingRunner(),
            )


def test_config_file_mode_and_reload(tmp_path, token_map_path, sms_db) -> None:
    config_path = tmp_path / "team-mcp.json"
    config_path.write_text(json.dumps(_mcp_config(tmp_path)))
    os.chmod(config_path, 0o644)
    with pytest.raises(TeamMcpConfigError):
        create_server(
            bind="127.0.0.1", port=0, config_path=config_path, gate_config=GATE_CONFIG,
            tokens_path=token_map_path, send_runner=RecordingRunner(),
        )
    os.chmod(config_path, 0o600)
    server = create_server(
        bind="127.0.0.1", port=0, config_path=config_path, gate_config=GATE_CONFIG,
        tokens_path=token_map_path, send_runner=RecordingRunner(),
        env={"DIALPAD_SMS_DB": str(sms_db)},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert json.load(_request(server, "/health", method="GET"))["caller"] == "team-scott"

        config_path.write_text("{corrupt")
        stat_result = config_path.stat()
        os.utime(config_path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 10_000_000))
        with pytest.raises(urllib.error.HTTPError) as raised:
            _request(server, "/health", method="GET")
        assert raised.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_startup_requires_configs_and_tokens(tmp_path, token_map_path) -> None:
    with pytest.raises(TeamMcpConfigError):
        create_server(
            bind="127.0.0.1", port=0, config=_mcp_config(tmp_path), send_runner=RecordingRunner(), env={},
        )
    with pytest.raises(TeamMcpConfigError):
        create_server(
            bind="127.0.0.1", port=0, config=_mcp_config(tmp_path), gate_config=GATE_CONFIG,
            send_runner=RecordingRunner(), env={},
        )
    server = create_server(
        bind="127.0.0.1", port=0, config=_mcp_config(tmp_path, allow_bearer_fallback=False),
        gate_config=GATE_CONFIG, send_runner=RecordingRunner(), env={},
    )
    server.server_close()
