import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "adapters"))

import phone_intelligence  # noqa: E402


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("IPQS_API_KEY", "secret-test-key")
    monkeypatch.setenv("DIALPAD_PHONE_INTELLIGENCE_CACHE_DB", str(tmp_path / "cache" / "phone.db"))
    monkeypatch.setenv("DIALPAD_PHONE_INTELLIGENCE_MAX_CALLS_PER_WINDOW", "120")


def test_valid_active_wireless_number_normalizes_fields(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)

    def fake_urlopen(req, timeout):
        assert req.headers["Ipqs-key"] == "secret-test-key"
        assert "secret-test-key" not in req.full_url
        return _FakeResponse({
            "success": True,
            "valid": True,
            "active": True,
            "formatted": "+1 202-555-0142",
            "country": "US",
            "region": "DC",
            "city": "Washington",
            "carrier": "Verizon Wireless",
            "line_type": "wireless",
            "fraud_score": 2,
            "name": "Jordan Example",
        })

    monkeypatch.setattr(phone_intelligence.urllib.request, "urlopen", fake_urlopen)

    out = phone_intelligence.lookup_phone_intelligence("(202) 555-0142", now=100)

    assert out["usable"] is True
    assert out["status"] == "usable"
    assert out["phone"]["e164"] == "+12025550142"
    assert out["line"]["activeStatus"] == "active"
    assert out["line"]["type"] == "wireless"
    assert out["risk"]["level"] == "low"
    assert out["possibleIdentity"]["reverseName"] == "Jordan Example"
    assert "secret-test-key" not in json.dumps(out)


def test_unknown_active_line_does_not_become_inactive(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        phone_intelligence.urllib.request,
        "urlopen",
        lambda _req, timeout: _FakeResponse({"success": True, "valid": True, "fraud_score": 1}),
    )

    out = phone_intelligence.lookup_phone_intelligence("+12025550143", now=100)

    assert out["usable"] is True
    assert out["line"]["active"] is None
    assert out["line"]["activeStatus"] == "unknown"
    assert out["risk"]["level"] == "low"


def test_invalid_number_returns_unusable_high_risk():
    out = phone_intelligence.normalize_ipqs_payload("+12025550144", {"success": True, "valid": False, "fraud_score": 0})

    assert out["usable"] is False
    assert out["status"] == "invalid"
    assert out["risk"]["level"] == "high"
    assert "invalid" in out["risk"]["reasons"]


def test_ipqs_success_false_valid_false_stays_invalid():
    out = phone_intelligence.normalize_ipqs_payload("+12025550144", {"success": False, "valid": False, "fraud_score": 0})

    assert out["usable"] is False
    assert out["status"] == "invalid"
    assert out["risk"]["level"] == "high"
    assert "invalid" in out["risk"]["reasons"]


def test_ipqs_success_false_without_valid_is_unavailable_or_rate_limited():
    unavailable = phone_intelligence.normalize_ipqs_payload("+12025550144", {"success": False, "message": "provider error"})
    limited = phone_intelligence.normalize_ipqs_payload("+12025550144", {"success": False, "message": "quota limit exceeded"})

    assert unavailable["status"] == "unavailable"
    assert limited["status"] == "rate_limited"


def test_successful_ipqs_payload_without_valid_is_invalid():
    out = phone_intelligence.normalize_ipqs_payload("+12025550144", {"success": True, "fraud_score": 0})

    assert out["usable"] is False
    assert out["status"] == "invalid"
    assert out["risk"]["level"] == "high"
    assert "invalid" in out["risk"]["reasons"]


def test_disconnected_line_status_is_inactive():
    out = phone_intelligence.normalize_ipqs_payload(
        "+12025550144",
        {"success": True, "valid": True, "active_status": "Disconnected Line", "fraud_score": 0},
    )

    assert out["usable"] is False
    assert out["status"] == "inactive"
    assert out["line"]["activeStatus"] == "inactive"
    assert "inactive" in out["risk"]["reasons"]


def test_medium_and_high_risk_thresholds():
    medium = phone_intelligence.normalize_ipqs_payload("+12025550145", {"success": True, "valid": True, "fraud_score": 75})
    high = phone_intelligence.normalize_ipqs_payload("+12025550146", {"success": True, "valid": True, "fraud_score": 85})
    voip = phone_intelligence.normalize_ipqs_payload("+12025550147", {"success": True, "valid": True, "line_type": "VOIP"})
    disposable = phone_intelligence.normalize_ipqs_payload("+12025550148", {"success": True, "valid": True, "line_type": "disposable"})

    assert medium["risk"]["level"] == "medium"
    assert medium["status"] == "usable"
    assert high["risk"]["level"] == "high"
    assert high["status"] == "risky"
    assert voip["status"] == "usable"
    assert voip["risk"]["level"] == "low"
    assert disposable["status"] == "disposable"
    assert disposable["risk"]["level"] == "high"


def test_cache_hit_avoids_provider_and_policy_version_invalidates(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    calls = []

    def fake_urlopen(_req, timeout):
        calls.append(1)
        return _FakeResponse({"success": True, "valid": True, "fraud_score": 0, "active": True})

    monkeypatch.setattr(phone_intelligence.urllib.request, "urlopen", fake_urlopen)

    first = phone_intelligence.lookup_phone_intelligence("+12025550148", now=100)
    second = phone_intelligence.lookup_phone_intelligence("+12025550148", now=110)
    monkeypatch.setenv("IPQS_ENHANCED_LINE_CHECK", "true")
    third = phone_intelligence.lookup_phone_intelligence("+12025550148", now=120)

    assert first["cache"]["hit"] is False
    assert second["cache"]["hit"] is True
    assert third["cache"]["hit"] is False
    assert len(calls) == 2


def test_cache_files_are_private(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        phone_intelligence.urllib.request,
        "urlopen",
        lambda _req, timeout: _FakeResponse({"success": True, "valid": True, "fraud_score": 0}),
    )

    phone_intelligence.lookup_phone_intelligence("+12025550149", now=100)
    db_path = Path(os.environ["DIALPAD_PHONE_INTELLIGENCE_CACHE_DB"])

    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_cache_preserves_existing_parent_permissions(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    monkeypatch.setenv("IPQS_API_KEY", "secret-test-key")
    monkeypatch.setenv("DIALPAD_PHONE_INTELLIGENCE_CACHE_DB", str(shared / "phone.db"))
    monkeypatch.setenv("DIALPAD_PHONE_INTELLIGENCE_MAX_CALLS_PER_WINDOW", "120")
    monkeypatch.setattr(
        phone_intelligence.urllib.request,
        "urlopen",
        lambda _req, timeout: _FakeResponse({"success": True, "valid": True, "fraud_score": 0}),
    )

    phone_intelligence.lookup_phone_intelligence("+12025550149", now=100)

    assert stat.S_IMODE(shared.stat().st_mode) == 0o755
    assert stat.S_IMODE((shared / "phone.db").stat().st_mode) == 0o600


def test_budget_exhaustion_returns_without_provider_call(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("DIALPAD_PHONE_INTELLIGENCE_MAX_CALLS_PER_WINDOW", "1")
    calls = []

    def fake_urlopen(_req, timeout):
        calls.append(1)
        return _FakeResponse({"success": True, "valid": True, "fraud_score": 0})

    monkeypatch.setattr(phone_intelligence.urllib.request, "urlopen", fake_urlopen)

    first = phone_intelligence.lookup_phone_intelligence("+12025550150", now=100)
    second = phone_intelligence.lookup_phone_intelligence("+12025550151", now=101)

    assert first["status"] == "usable"
    assert second["status"] == "budget_exceeded"
    assert len(calls) == 1


def test_missing_budget_store_fails_closed(monkeypatch):
    monkeypatch.delenv("DIALPAD_PHONE_INTELLIGENCE_CACHE_DB", raising=False)
    monkeypatch.delenv("DIALPAD_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    assert phone_intelligence.budget_available("ipqs", "+12025550150", 1, now=100) is False


def test_unavailable_budget_store_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("DIALPAD_PHONE_INTELLIGENCE_CACHE_DB", str(tmp_path / "cache" / "phone.db"))

    def _raise(_path):
        raise OSError("read-only")

    monkeypatch.setattr(phone_intelligence, "_private_connect", _raise)

    assert phone_intelligence.budget_available("ipqs", "+12025550150", 1, now=100) is False


def test_missing_secret_and_invalid_input_fail_closed(monkeypatch):
    monkeypatch.delenv("IPQS_API_KEY", raising=False)
    monkeypatch.delenv("IPQUALITYSCORE_API_KEY", raising=False)

    assert phone_intelligence.lookup_phone_intelligence("+12025550152")["status"] == "not_configured"
    assert phone_intelligence.lookup_phone_intelligence("not a phone")["status"] == "invalid"
