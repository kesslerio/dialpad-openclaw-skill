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
