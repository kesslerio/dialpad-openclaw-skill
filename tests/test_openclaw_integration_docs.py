from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_openclaw_docs_require_current_turn_verification():
    readme = (ROOT / "README.md").read_text().lower()
    skill = (ROOT / "SKILL.md").read_text().lower()
    api_reference = (ROOT / "references/api-reference.md").read_text().lower()
    integration = (ROOT / "references/openclaw-integration.md").read_text().lower()

    assert "current-turn verification" in readme
    assert "current-turn verification" in skill
    assert "current-turn verification" in api_reference
    assert "current-turn verification" in integration

    assert "stale session memory" in readme
    assert "stale session memory" in skill
    assert "fresh tool result in the same turn" in api_reference
    assert "fresh tool result in the same turn" in integration
    assert "identitystate" in readme
    assert "identitystate" in api_reference
    assert "identitystate" in integration
    assert "ambiguous" in integration
    assert "first name" in integration
    assert "area code" in integration


def test_openclaw_docs_require_sms_approval_drafts_not_autonomous_send():
    readme = (ROOT / "README.md").read_text().lower()
    skill = (ROOT / "SKILL.md").read_text().lower()
    api_reference = (ROOT / "references/api-reference.md").read_text().lower()
    integration = (ROOT / "references/openclaw-integration.md").read_text().lower()

    assert "approval draft" in readme
    assert "approval drafts" in api_reference
    assert "approval drafts" in integration
    assert "inbound hooks may create sms approval drafts" in skill
    assert "must not send customer sms directly" in skill
    assert "intentionally unsupported" in integration
    assert "explicit opt-out language creates no draft" in readme
    assert "explicit opt-out language creates no draft" in api_reference
    assert "autonomous sms send is not supported" in integration
