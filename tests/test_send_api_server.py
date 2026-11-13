"""Tests for scripts/send_api_server.py — the loopback team send API."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import send_api_server
from send_api_server import SendApiConfigError, create_server

GATE_CONFIG = {
    "shared_lines": {"office": "+14159065785", "sales": "+14155201316"},
    "teammates": {
        "scott": "+14152230323",
        "lilla": "+14158701945",
    },
    "callers": {
        "team-scott": {"shared_lines": ["office", "sales"], "acts_for": "scott"},
        "team-lilla": {"shared_lines": ["office"], "acts_for": "lilla"},
        "operator": {"shared_lines": ["office", "sales"]},
    },
}

SCOTT_TOKEN = "fake-scott-pipeline-token-0123456789abcdef"
LILLA_TOKEN = "fake-lilla-pipeline-token-0123456789abcdef"

SUPPORT_LINE = "+14159917155"
SEND_BODY = {"to": "+13105550100", "from": "+14159065785", "message": "hello there"}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _map_entries() -> dict:
    return {
        _digest(SCOTT_TOKEN): {"caller": "team-scott", "label": "scott-pipeline"},
        _digest(LILLA_TOKEN): {"caller": "team-lilla", "label": "lilla-pipeline"},
    }


def _write_map(path: Path, entries: dict, *, bump_mtime: bool = False) -> None:
    send_api_server.write_token_map(path, entries)
    if bump_mtime:
        stat_result = path.stat()
        os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 10_000_000))


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
        if self.stdout is not None:
            raw = self.stdout
        else:
            raw = json.dumps(self.envelope)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout=raw, stderr=b"")


@pytest.fixture
def token_map_path(tmp_path: Path) -> Path:
    path = tmp_path / "send-api-tokens.json"
    _write_map(path, _map_entries())
    return path


@pytest.fixture
def make_server(token_map_path: Path):
    servers: list[tuple] = []

    def _make(runner=None, **overrides):
        kwargs = {
            "bind": "127.0.0.1",
            "port": 0,
            "tokens_path": token_map_path,
            "gate_config": GATE_CONFIG,
            "send_runner": runner or RecordingRunner(),
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


def _request(server, path: str, *, token: str | None = SCOTT_TOKEN, method: str = "POST", body: dict | None = None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    encoded = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://{server.server_address[0]}:{server.server_address[1]}{path}",
        data=encoded,
        headers=headers,
        method=method,
    )
    return urllib.request.urlopen(request, timeout=5)


def _error_body(raised: urllib.error.HTTPError) -> dict:
    return json.loads(raised.read())


# ---------------------------------------------------------------------------
# Auth and caller mapping
# ---------------------------------------------------------------------------


def test_missing_and_wrong_bearer_get_identical_401s(server) -> None:
    bodies = []
    for token in (None, "wrong-token-value"):
        with pytest.raises(urllib.error.HTTPError) as raised:
            _request(server, "/v1/sms/send", token=token, body=dict(SEND_BODY))
        assert raised.value.code == 401
        bodies.append(_error_body(raised.value))
    assert bodies[0] == bodies[1]
    assert bodies[0]["ok"] is False
    assert bodies[0]["error"]["code"] == "auth_missing"


def test_correct_bearer_maps_to_gate_caller(server) -> None:
    response = json.load(_request(server, "/v1/sms/send", body=dict(SEND_BODY)))
    assert response["ok"] is True
    assert response["command"] == "send_api.send"
    assert response["data"] == {"mode": "real_send", "id": "msg-1"}
    assert response["meta"]["service"] == "dialpad-send-api"
    argv = server.send_runner.calls[0]["argv"]
    assert argv[argv.index("--caller") + 1] == "team-scott"
    assert argv[0] == sys.executable


def test_second_token_maps_to_its_own_caller(server) -> None:
    json.load(_request(server, "/v1/sms/send", token=LILLA_TOKEN, body=dict(SEND_BODY)))
    argv = server.send_runner.calls[0]["argv"]
    assert argv[argv.index("--caller") + 1] == "team-lilla"


def test_health_requires_bearer_and_returns_mapped_caller(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", token=None)
    assert raised.value.code == 401

    response = json.load(_request(server, "/health", method="GET", token=SCOTT_TOKEN))
    assert response["ok"] is True
    assert response["data"] == {"status": "ok", "caller": "team-scott"}


def test_unknown_routes_and_methods(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/nope", method="GET")
    assert raised.value.code == 404
    assert _error_body(raised.value)["error"]["code"] == "not_found"

    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="POST", body=dict(SEND_BODY))
    assert raised.value.code == 405

    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/v1/sms/send", method="GET")
    assert raised.value.code == 405


# ---------------------------------------------------------------------------
# Body validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [{"caller": "operator"}, {"acting_for": "scott"}, {"caller_id": "x"}, {"verbose": True}])
def test_unknown_body_keys_rejected_and_runner_not_called(server, extra) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/v1/sms/send", body={**SEND_BODY, **extra})
    assert raised.value.code == 400
    assert _error_body(raised.value)["error"]["code"] == "invalid_argument"
    assert server.send_runner.calls == []


def test_to_must_be_single_e164_not_list(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/v1/sms/send", body={**SEND_BODY, "to": ["+13105550100", "+13105550101"]})
    assert raised.value.code == 400
    assert server.send_runner.calls == []


def test_message_must_be_nonempty_and_bounded(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/v1/sms/send", body={**SEND_BODY, "message": "   "})
    assert raised.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/v1/sms/send", body={**SEND_BODY, "message": "x" * 1601})
    assert raised.value.code == 400
    assert server.send_runner.calls == []


def test_dry_run_true_passes_dry_run_argv(server) -> None:
    json.load(_request(server, "/v1/sms/send", body={**SEND_BODY, "dry_run": True}))
    argv = server.send_runner.calls[0]["argv"]
    assert "--dry-run" in argv


def test_dry_run_absent_means_no_flag(server) -> None:
    json.load(_request(server, "/v1/sms/send", body=dict(SEND_BODY)))
    assert "--dry-run" not in server.send_runner.calls[0]["argv"]


@pytest.mark.parametrize("bad", ["true", 1, "yes"])
def test_dry_run_must_be_a_json_boolean(server, bad) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/v1/sms/send", body={**SEND_BODY, "dry_run": bad})
    assert raised.value.code == 400
    assert server.send_runner.calls == []


# ---------------------------------------------------------------------------
# Support line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["to", "from"])
def test_support_line_refused_before_anything_runs(server, field) -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/v1/sms/send", body={**SEND_BODY, field: SUPPORT_LINE})
    assert raised.value.code == 403
    body = _error_body(raised.value)
    assert body["error"]["code"] == "support_line_refused"
    assert body["error"]["retryable"] is False
    assert server.send_runner.calls == []


# ---------------------------------------------------------------------------
# Child process contract
# ---------------------------------------------------------------------------


def test_message_goes_to_stdin_and_env_drops_caller(server, monkeypatch) -> None:
    monkeypatch.setenv("DIALPAD_SEND_CALLER", "team-lilla")
    secret_message = "pricing is $499 and never appears on argv"
    json.load(_request(server, "/v1/sms/send", body={**SEND_BODY, "message": secret_message}))
    call = server.send_runner.calls[0]
    assert all(secret_message not in arg for arg in call["argv"])
    assert call["message"] == secret_message
    assert "--message-stdin" in call["argv"]
    assert "DIALPAD_SEND_CALLER" not in call["env"]


def test_runner_timeout_maps_to_504(make_server) -> None:
    def slow_runner(argv, *, message, env, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    server = make_server(runner=slow_runner)
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/v1/sms/send", body=dict(SEND_BODY))
    assert raised.value.code == 504
    body = _error_body(raised.value)
    assert body["error"]["code"] == "timeout"
    assert body["error"]["retryable"] is True


def test_gate_refusal_maps_to_422_with_gate_details(make_server) -> None:
    envelope = {
        "ok": False,
        "command": "send_sms.send",
        "error": {"code": "send_gate_refused", "message": "Send gate refused: ...", "retryable": True},
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
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/v1/sms/send", body=dict(SEND_BODY))
    assert raised.value.code == 422
    body = _error_body(raised.value)
    assert body["error"]["code"] == "send_gate_refused"
    assert body["error"]["retryable"] is True
    assert body["error"]["retry_after_seconds"] == 300
    assert body["error"]["reasons"] == [
        "recipient +13105550100 outside send window: 22:40 local (America/Los_Angeles)"
    ]


def test_unparseable_child_output_is_502_without_leakage(make_server) -> None:
    runner = RecordingRunner(stdout=b"Traceback: child-secret-traceback")
    server = make_server(runner=runner)
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/v1/sms/send", body=dict(SEND_BODY))
    assert raised.value.code == 502
    raw = raised.value.read()
    body = json.loads(raw)
    assert body["error"]["code"] == "upstream_error"
    assert b"child-secret-traceback" not in raw


def test_other_child_errors_pass_code_and_retryable(make_server) -> None:
    envelope = {
        "ok": False,
        "command": "send_sms.send",
        "error": {"code": "network_error", "message": "network error ...", "retryable": True},
        "meta": {},
    }
    server = make_server(runner=RecordingRunner(envelope=envelope))
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/v1/sms/send", body=dict(SEND_BODY))
    assert raised.value.code == 502
    body = _error_body(raised.value)
    assert body["error"]["code"] == "network_error"
    assert body["error"]["retryable"] is True
    assert "network error" not in body["error"]["message"]


# ---------------------------------------------------------------------------
# Startup refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bind", ["0.0.0.0", "10.0.0.5", "localhost", "::"])
def test_create_server_refuses_non_loopback_bind(token_map_path, bind) -> None:
    with pytest.raises(ValueError):
        create_server(bind=bind, port=0, tokens_path=token_map_path, gate_config=GATE_CONFIG, send_runner=RecordingRunner())


def test_create_server_accepts_loopback_literals(token_map_path) -> None:
    for bind in ("127.0.0.1", "127.0.0.9"):
        server = create_server(bind=bind, port=0, tokens_path=token_map_path, gate_config=GATE_CONFIG, send_runner=RecordingRunner())
        server.server_close()


def test_startup_refuses_group_readable_map(tmp_path) -> None:
    path = tmp_path / "tokens.json"
    _write_map(path, _map_entries())
    os.chmod(path, 0o644)
    with pytest.raises(SendApiConfigError):
        create_server(bind="127.0.0.1", port=0, tokens_path=path, gate_config=GATE_CONFIG, send_runner=RecordingRunner())


def test_startup_refuses_missing_or_malformed_map(tmp_path) -> None:
    with pytest.raises(SendApiConfigError):
        create_server(bind="127.0.0.1", port=0, tokens_path=tmp_path / "absent.json", gate_config=GATE_CONFIG, send_runner=RecordingRunner())
    malformed = tmp_path / "malformed.json"
    malformed.write_text("not json")
    os.chmod(malformed, 0o600)
    with pytest.raises(SendApiConfigError):
        create_server(bind="127.0.0.1", port=0, tokens_path=malformed, gate_config=GATE_CONFIG, send_runner=RecordingRunner())


@pytest.mark.parametrize(
    "entries",
    [
        {},
        {_digest("x"): {"caller": "team-scott", "label": "dup"}, _digest("y"): {"caller": "team-lilla", "label": "dup"}},
        {"nothex": {"caller": "team-scott", "label": "bad"}},
        {_digest("x").upper(): {"caller": "team-scott", "label": "upper"}},
        {_digest("x"): {"caller": "team-nobody", "label": "unknown"}},
        {_digest("x"): {"caller": "operator", "label": "op"}},
    ],
    ids=["empty", "duplicate-label", "bad-digest", "uppercase-digest", "unknown-caller", "operator-caller"],
)
def test_startup_refuses_invalid_token_maps(tmp_path, entries) -> None:
    path = tmp_path / "tokens.json"
    _write_map(path, entries)
    with pytest.raises(SendApiConfigError):
        create_server(bind="127.0.0.1", port=0, tokens_path=path, gate_config=GATE_CONFIG, send_runner=RecordingRunner())


def test_startup_requires_gate_config(token_map_path, monkeypatch) -> None:
    monkeypatch.delenv("DIALPAD_SEND_GATE_CONFIG", raising=False)
    with pytest.raises(SendApiConfigError):
        create_server(bind="127.0.0.1", port=0, tokens_path=token_map_path, send_runner=RecordingRunner())


# ---------------------------------------------------------------------------
# Token map reload: revoke and fail-closed
# ---------------------------------------------------------------------------


def test_revoked_token_gets_401_without_restart(server, token_map_path) -> None:
    assert json.load(_request(server, "/health", method="GET"))["ok"] is True

    _write_map(
        token_map_path,
        {_digest(LILLA_TOKEN): {"caller": "team-lilla", "label": "lilla-pipeline"}},
        bump_mtime=True,
    )
    with pytest.raises(urllib.error.HTTPError) as raised:
        _request(server, "/health", method="GET", token=SCOTT_TOKEN)
    assert raised.value.code == 401
    assert json.load(_request(server, "/health", method="GET", token=LILLA_TOKEN))["ok"] is True


def test_corrupt_map_after_reload_refuses_everything(server, token_map_path) -> None:
    assert json.load(_request(server, "/health", method="GET"))["ok"] is True

    token_map_path.write_text("{corrupt")
    os.chmod(token_map_path, 0o600)
    stat_result = token_map_path.stat()
    os.utime(token_map_path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 10_000_000))

    for token in (SCOTT_TOKEN, LILLA_TOKEN):
        with pytest.raises(urllib.error.HTTPError) as raised:
            _request(server, "/health", method="GET", token=token)
        assert raised.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as raised:
            _request(server, "/v1/sms/send", token=token, body=dict(SEND_BODY))
        assert raised.value.code == 401
    assert server.send_runner.calls == []


# ---------------------------------------------------------------------------
# Real subprocess paths (fully offline)
# ---------------------------------------------------------------------------


def test_real_send_sms_dry_run_gate_refusal_end_to_end(tmp_path, monkeypatch) -> None:
    """Run the real bin/send_sms.py --dry-run; the allowlist refusal precedes
    any opt-out source read, so nothing touches the network."""
    gate_path = tmp_path / "send-gate.json"
    gate_path.write_text(json.dumps(GATE_CONFIG))
    monkeypatch.setenv("DIALPAD_SEND_GATE_CONFIG", str(gate_path))

    tokens_path = tmp_path / "send-api-tokens.json"
    _write_map(tokens_path, {_digest(SCOTT_TOKEN): {"caller": "team-scott", "label": "scott-pipeline"}})

    server = create_server(
        bind="127.0.0.1",
        port=0,
        tokens_path=tokens_path,
        gate_config=GATE_CONFIG,
        send_runner=send_api_server.default_send_runner,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            _request(
                server,
                "/v1/sms/send",
                body={"to": "+13105550100", "from": "+15550001111", "message": "hi", "dry_run": True},
            )
        assert raised.value.code == 422
        body = _error_body(raised.value)
        assert body["error"]["code"] == "send_gate_refused"
        assert any("not an approved line" in reason for reason in body["error"]["reasons"])
        assert body["error"]["send_gate"]["caller"] == "team-scott"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_default_runner_executes_script_argv(tmp_path, monkeypatch, make_server) -> None:
    """The real default runner spawns the argv script with the message on stdin."""
    fake = tmp_path / "fake_send_sms.py"
    fake.write_text(
        "import json, sys\n"
        "text = sys.stdin.read()\n"
        "print(json.dumps({'ok': True, 'command': 'send_sms.send', "
        "'data': {'mode': 'dry_run', 'echo_len': len(text)}, 'meta': {}}))\n"
    )
    monkeypatch.setattr(send_api_server, "SEND_SMS_SCRIPT", fake)

    server = make_server(runner=send_api_server.default_send_runner)
    response = json.load(_request(server, "/v1/sms/send", body={**SEND_BODY, "dry_run": True}))
    assert response["ok"] is True
    assert response["data"]["mode"] == "dry_run"
    assert response["data"]["echo_len"] == len(SEND_BODY["message"])
