"""Tests for scripts/team_alerts.py — per-teammate inbound SMS alerts."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import ops_wake
import team_alerts

SCOTT_LINE = "+14152230323"
LILLA_LINE = "+14158701945"
OFFICE_LINE = "+14159065785"
SALES_LINE = "+14155201316"
SUPPORT_LINE = "+14159917155"

GATE_CONFIG = {
    "shared_lines": {"office": OFFICE_LINE, "sales": SALES_LINE},
    "teammates": {"scott": SCOTT_LINE, "lilla": LILLA_LINE},
    "callers": {
        "team-scott": {"shared_lines": ["office", "sales"], "acts_for": "scott"},
        "team-lilla": {"shared_lines": ["office"], "acts_for": "lilla"},
    },
}


class _Capture(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).received.append({"auth": self.headers.get("Authorization"), "body": json.loads(body)})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):
        return None


@pytest.fixture
def capture_server():
    _Capture.received = []
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _write_private(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload))
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def alert_setup(tmp_path, capture_server):
    """Env dict + helpers for one registered caller."""
    ops_wake.reset_state_for_tests()
    gate_path = _write_private(tmp_path / "send-gate.json", GATE_CONFIG)
    setup = {
        "store": tmp_path / "team-alerts.json",
        "config": tmp_path / "team-mcp.json",
        "url": f"http://127.0.0.1:{capture_server.server_port}/hook",
        "tmp_path": tmp_path,
    }

    def env(*, callers=None, allowed_url_hosts=None, extra=None):
        alerts = {
            "store": str(setup["store"]),
            "dedupe_db": str(tmp_path / "team-alerts.dedupe.db"),
            "allowed_url_hosts": allowed_url_hosts or [],
            "callers": callers or {},
        }
        _write_private(setup["config"], {"alerts": alerts})
        result = {
            "DIALPAD_SEND_GATE_CONFIG": str(gate_path),
            "DIALPAD_TEAM_MCP_CONFIG": str(setup["config"]),
            "OPS_WAKE_DEDUPE_DB": str(tmp_path / "ops-wake.db"),
        }
        result.update(extra or {})
        return result

    def register(caller="team-scott", url=None, key="alert-key-1"):
        team_alerts.set_alert_registration(setup["store"], caller, url or setup["url"], key)

    setup["env"] = env
    setup["register"] = register
    yield type("Setup", (), setup)
    ops_wake.reset_state_for_tests()


def _sms(message_id="m1", line=SCOTT_LINE, **extra):
    payload = {
        "id": message_id,
        "direction": "inbound",
        "from_number": "+14155550111",
        "to_number": [line],
        "text": "private body",
        "created_date": 1700000000000,
    }
    payload.update(extra)
    return payload


def test_own_line_forwards_minimal_payload(alert_setup):
    env = alert_setup.env()
    alert_setup.register()
    assert team_alerts.notify_sms_event(_sms(), background=False, environ=env) is True
    assert len(_Capture.received) == 1
    got = _Capture.received[0]
    assert got["auth"] == "Bearer alert-key-1"
    body = got["body"]
    assert body["event"] == "inbound_sms"
    assert body["line"] == SCOTT_LINE
    assert body["dialpad"]["message_id"] == "m1"
    dumped = json.dumps(body)
    assert "private body" not in dumped
    assert "+14155550111" not in dumped


def test_office_line_not_forwarded_by_default(alert_setup):
    alert_setup.register()
    assert team_alerts.notify_sms_event(_sms(line=OFFICE_LINE), background=False, environ=alert_setup.env()) is False
    assert _Capture.received == []


def test_office_line_forwards_when_configured(alert_setup):
    env = alert_setup.env(callers={"team-scott": {"shared_lines": ["office"]}})
    alert_setup.register()
    assert team_alerts.notify_sms_event(_sms(line=OFFICE_LINE), background=False, environ=env) is True
    assert _Capture.received[0]["body"]["line"] == OFFICE_LINE


def test_other_teammate_line_never_forwards(alert_setup):
    alert_setup.register()
    assert team_alerts.notify_sms_event(_sms(line=LILLA_LINE), background=False, environ=alert_setup.env()) is False
    assert _Capture.received == []


def test_support_line_never_forwards(alert_setup):
    alert_setup.register()
    assert team_alerts.notify_sms_event(_sms(line=SUPPORT_LINE), background=False, environ=alert_setup.env()) is False
    assert _Capture.received == []


def test_outbound_and_internal_do_not_forward(alert_setup):
    alert_setup.register()
    env = alert_setup.env()
    assert team_alerts.notify_sms_event(_sms(direction="outbound"), background=False, environ=env) is False
    assert team_alerts.notify_sms_event(_sms(is_internal=True), background=False, environ=env) is False
    assert _Capture.received == []


def test_duplicate_events_forward_once(alert_setup):
    alert_setup.register()
    env = alert_setup.env()
    assert team_alerts.notify_sms_event(_sms(), background=False, environ=env) is True
    assert team_alerts.notify_sms_event(_sms(), background=False, environ=env) is False
    assert len(_Capture.received) == 1


def test_each_caller_gets_only_its_own_line(alert_setup, capture_server):
    env = alert_setup.env()
    alert_setup.register("team-scott", key="k1")
    alert_setup.register("team-lilla", key="k2", url=f"http://127.0.0.1:{capture_server.server_port}/lilla")
    assert team_alerts.notify_sms_event(_sms(line=LILLA_LINE), background=False, environ=env) is True
    assert len(_Capture.received) == 1
    assert _Capture.received[0]["auth"] == "Bearer k2"


def test_no_config_or_store_means_no_forward(tmp_path, alert_setup):
    env = {"DIALPAD_SEND_GATE_CONFIG": str(tmp_path / "missing-gate.json")}
    assert team_alerts.notify_sms_event(_sms(), background=False, environ=env) is False
    env = alert_setup.env()  # config but no registrations
    assert team_alerts.notify_sms_event(_sms(), background=False, environ=env) is False
    assert _Capture.received == []


def test_corrupt_config_fails_closed(alert_setup, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{corrupt")
    os.chmod(bad, 0o600)
    env = alert_setup.env()
    env["DIALPAD_TEAM_MCP_CONFIG"] = str(bad)
    assert team_alerts.notify_sms_event(_sms(), background=False, environ=env) is False
    assert _Capture.received == []


def test_stored_url_revalidated_at_forward_time(alert_setup):
    """A registration whose host is outside allowed_url_hosts never receives a POST."""
    env = alert_setup.env(allowed_url_hosts=["hooks.example.com"])
    alert_setup.register("team-lilla", url="https://evil.example.net/hook", key="k")
    assert team_alerts.notify_sms_event(_sms(line=LILLA_LINE), background=False, environ=env) is False
    assert _Capture.received == []


def test_background_mode_hands_off_to_daemon_thread(alert_setup, monkeypatch):
    env = alert_setup.env()
    alert_setup.register()
    started = threading.Event()
    release = threading.Event()
    caller = threading.current_thread()
    seen = []

    def _slow_dispatch(wake, *, environ):
        seen.append(threading.current_thread() is caller)
        started.set()
        release.wait(5)
        return True

    monkeypatch.setattr(team_alerts, "_dispatch", _slow_dispatch)
    assert team_alerts.notify_sms_event(_sms(), environ=env) is True
    assert started.wait(5)
    release.set()
    assert seen == [False]


# ---------------------------------------------------------------------------
# URL + section validation and the registration store
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,allowed,ok",
    [
        ("https://hooks.example.com/sms", ["hooks.example.com"], True),
        ("https://a.hooks.example.com/sms", ["hooks.example.com"], True),
        ("https://hooks.example.com/sms", ["example.com"], True),
        ("https://hooks.example.net/sms", ["example.com"], False),
        ("https://example.com.evil.net/sms", ["example.com"], False),
        ("https://hooks.example.com/sms", [], True),
        ("http://127.0.0.1:8080/hook", ["hooks.example.com"], True),
        ("http://localhost:9999/hook", [], True),
        ("http://example.com/hook", [], False),
        ("https://127.0.0.1/hook", [], False),
        ("https://user:pw@hooks.example.com/", ["hooks.example.com"], False),
        ("ftp://hooks.example.com/x", [], False),
        ("", [], False),
        ("notaurl", [], False),
    ],
)
def test_validate_alert_url(url, allowed, ok):
    if ok:
        assert team_alerts.validate_alert_url(url, allowed_hosts=allowed) == url
    else:
        with pytest.raises(ValueError):
            team_alerts.validate_alert_url(url, allowed_hosts=allowed)


def test_set_and_clear_registration(tmp_path):
    store = tmp_path / "alerts.json"
    team_alerts.set_alert_registration(store, "team-scott", "https://hooks.example.com/x", "k1")
    mode = stat.S_IMODE(store.stat().st_mode)
    assert mode == 0o600
    entries = team_alerts.load_alert_store(store)
    assert entries["team-scott"]["url"] == "https://hooks.example.com/x"
    team_alerts.set_alert_registration(store, "team-scott", "", "")
    assert team_alerts.load_alert_store(store) == {}


def test_concurrent_registrations_all_survive(tmp_path):
    """Unlocked read-modify-write dropped callers; every writer must win."""
    store = tmp_path / "alerts.json"
    workers = 8
    rounds = 20
    barrier = threading.Barrier(workers)
    errors = []

    def register(index):
        try:
            barrier.wait(10)
            for round_ in range(rounds):
                team_alerts.set_alert_registration(
                    store, f"caller-{index}", f"https://h{index}.example.com/{round_}", f"k{index}"
                )
        except Exception as exc:  # noqa: BLE001 - surfaced below.
            errors.append(exc)

    threads = [threading.Thread(target=register, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    entries = team_alerts.load_alert_store(store)
    assert set(entries) == {f"caller-{index}" for index in range(workers)}
    for index in range(workers):
        assert entries[f"caller-{index}"]["url"] == f"https://h{index}.example.com/{rounds - 1}"


def test_alert_store_mode_is_checked(tmp_path):
    """The key-bearing store is refused when group/world-readable."""
    store = tmp_path / "alerts.json"
    store.write_text(json.dumps({"team-scott": {"url": "https://x.example.com/h", "key": "k"}}))
    os.chmod(store, 0o644)
    assert team_alerts.load_alert_store(store) == {}
    with pytest.raises(ValueError):
        team_alerts.load_alert_store(store, strict=True)
    with pytest.raises(ValueError):
        team_alerts.set_alert_registration(store, "team-lilla", "https://y.example.com/h", "k2")
    os.chmod(store, 0o600)
    assert "team-scott" in team_alerts.load_alert_store(store)


def test_validate_alerts_section_rejects_out_of_scope_lines():
    with pytest.raises(ValueError):
        team_alerts.validate_alerts_section(
            {"store": "/tmp/x", "callers": {"team-scott": {"shared_lines": ["+14155550111"]}}},
            gate_config=GATE_CONFIG,
        )
    with pytest.raises(ValueError):
        team_alerts.validate_alerts_section(
            {"store": "/tmp/x", "callers": {"team-scott": {"shared_lines": [SUPPORT_LINE]}}},
            gate_config=GATE_CONFIG,
        )
    with pytest.raises(ValueError):
        team_alerts.validate_alerts_section(
            {"store": "/tmp/x", "callers": {"nobody": {"shared_lines": []}}},
            gate_config=GATE_CONFIG,
        )


def test_alert_lines_for_caller():
    alerts = team_alerts.validate_alerts_section(
        {"store": "/tmp/x", "callers": {"team-scott": {"shared_lines": ["office", SALES_LINE]}}},
        gate_config=GATE_CONFIG,
    )
    assert team_alerts.alert_lines_for_caller("team-scott", gate_config=GATE_CONFIG, alerts=alerts) == {
        SCOTT_LINE,
        OFFICE_LINE,
        SALES_LINE,
    }
    empty = team_alerts.validate_alerts_section({"store": "/tmp/x", "callers": {}}, gate_config=GATE_CONFIG)
    assert team_alerts.alert_lines_for_caller("team-lilla", gate_config=GATE_CONFIG, alerts=empty) == {LILLA_LINE}


def test_private_json_mode_check(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{}")
    os.chmod(path, 0o644)
    with pytest.raises(ValueError):
        team_alerts.read_private_json(path)
    os.chmod(path, 0o640)
    assert team_alerts.read_private_json(path) == {}
