"""Tests for scripts/send_api_tokens.py — operator token-map CLI."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import send_api_tokens

GATE_CONFIG = {
    "shared_lines": {"office": "+14159065785", "sales": "+14155201316"},
    "teammates": {"scott": "+14152230323", "lilla": "+14158701945"},
    "callers": {
        "team-scott": {"shared_lines": ["office", "sales"], "acts_for": "scott"},
        "team-lilla": {"shared_lines": ["office"], "acts_for": "lilla"},
        "operator": {"shared_lines": ["office", "sales"]},
    },
}

TOKEN = "fake-token-material-" + "a" * 24
TOKEN2 = "fake-token-material-" + "b" * 24


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gate_path = tmp_path / "send-gate.json"
    gate_path.write_text(json.dumps(GATE_CONFIG))
    tokens_path = tmp_path / "send-api-tokens.json"
    monkeypatch.setenv("DIALPAD_SEND_GATE_CONFIG", str(gate_path))
    monkeypatch.setenv("DIALPAD_SEND_API_TOKENS", str(tokens_path))
    return tokens_path


def _run(argv: list[str], stdin_text: str, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    return send_api_tokens.main(argv)


def test_add_reads_stdin_writes_digest_mode_0600(cli_env, monkeypatch, capsys) -> None:
    rc = _run(["add", "--caller", "team-scott", "--label", "scott-pipeline"], TOKEN + "\n", monkeypatch)
    assert rc == 0
    data = json.loads(cli_env.read_text())
    digest = _digest(TOKEN)
    assert data["tokens"][digest] == {"caller": "team-scott", "label": "scott-pipeline"}
    assert stat.S_IMODE(os.stat(cli_env).st_mode) == 0o600
    out = capsys.readouterr()
    assert TOKEN not in out.out and TOKEN not in out.err
    assert digest not in out.out and digest not in out.err
    assert TOKEN not in cli_env.read_text()


def test_add_rejects_short_token(cli_env, monkeypatch) -> None:
    rc = _run(["add", "--caller", "team-scott", "--label", "short"], "tiny", monkeypatch)
    assert rc == 2
    assert not cli_env.exists()


def test_add_rejects_unknown_and_operator_callers(cli_env, monkeypatch) -> None:
    rc = _run(["add", "--caller", "team-nobody", "--label", "x"], TOKEN, monkeypatch)
    assert rc == 2
    rc = _run(["add", "--caller", "operator", "--label", "op"], TOKEN, monkeypatch)
    assert rc == 2
    assert not cli_env.exists()


def test_add_refuses_duplicate_label_and_digest(cli_env, monkeypatch) -> None:
    assert _run(["add", "--caller", "team-scott", "--label", "scott-pipeline"], TOKEN, monkeypatch) == 0
    # Same label, different token.
    rc = _run(["add", "--caller", "team-lilla", "--label", "scott-pipeline"], TOKEN2, monkeypatch)
    assert rc == 2
    # Different label, same token (same digest).
    rc = _run(["add", "--caller", "team-lilla", "--label", "other-label"], TOKEN, monkeypatch)
    assert rc == 2
    data = json.loads(cli_env.read_text())
    assert len(data["tokens"]) == 1


def test_add_appends_to_existing_map(cli_env, monkeypatch) -> None:
    assert _run(["add", "--caller", "team-scott", "--label", "scott-pipeline"], TOKEN, monkeypatch) == 0
    assert _run(["add", "--caller", "team-lilla", "--label", "lilla-pipeline"], TOKEN2, monkeypatch) == 0
    data = json.loads(cli_env.read_text())
    assert set(data["tokens"]) == {_digest(TOKEN), _digest(TOKEN2)}
    assert stat.S_IMODE(os.stat(cli_env).st_mode) == 0o600


def test_list_prints_label_and_caller_never_digests(cli_env, monkeypatch, capsys) -> None:
    assert _run(["add", "--caller", "team-scott", "--label", "scott-pipeline"], TOKEN, monkeypatch) == 0
    capsys.readouterr()
    rc = _run(["list"], "", monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "scott-pipeline" in out
    assert "team-scott" in out
    assert _digest(TOKEN) not in out
    assert TOKEN not in out


def test_revoke_removes_entry(cli_env, monkeypatch) -> None:
    assert _run(["add", "--caller", "team-scott", "--label", "scott-pipeline"], TOKEN, monkeypatch) == 0
    assert _run(["revoke", "--label", "scott-pipeline"], "", monkeypatch) == 0
    data = json.loads(cli_env.read_text())
    assert data["tokens"] == {}
    assert stat.S_IMODE(os.stat(cli_env).st_mode) == 0o600


def test_revoke_unknown_label_fails(cli_env, monkeypatch) -> None:
    rc = _run(["revoke", "--label", "missing"], "", monkeypatch)
    assert rc == 2


def test_add_requires_tokens_env(cli_env, monkeypatch) -> None:
    monkeypatch.delenv("DIALPAD_SEND_API_TOKENS")
    rc = _run(["add", "--caller", "team-scott", "--label", "x"], TOKEN, monkeypatch)
    assert rc == 2


def test_add_strips_label_and_caller(cli_env, monkeypatch, capsys) -> None:
    rc = _run(["add", "--caller", "team-scott", "--label", "  scott-pipeline  "], TOKEN + "\n", monkeypatch)
    assert rc == 0
    data = json.loads(cli_env.read_text())
    assert data["tokens"][_digest(TOKEN)] == {"caller": "team-scott", "label": "scott-pipeline"}
    out = capsys.readouterr().out
    assert "'scott-pipeline'" in out


def test_add_rejects_blank_label(cli_env, monkeypatch) -> None:
    rc = _run(["add", "--caller", "team-scott", "--label", "   "], TOKEN, monkeypatch)
    assert rc == 2
    assert not cli_env.exists()


def test_add_refuses_label_duplicates_after_strip(cli_env, monkeypatch) -> None:
    assert _run(["add", "--caller", "team-scott", "--label", "scott-pipeline"], TOKEN, monkeypatch) == 0
    rc = _run(["add", "--caller", "team-lilla", "--label", "  scott-pipeline "], TOKEN2, monkeypatch)
    assert rc == 2
    assert len(json.loads(cli_env.read_text())["tokens"]) == 1


def test_revoke_matches_label_after_strip(cli_env, monkeypatch) -> None:
    assert _run(["add", "--caller", "team-scott", "--label", "scott-pipeline"], TOKEN, monkeypatch) == 0
    assert _run(["revoke", "--label", "  scott-pipeline  "], "", monkeypatch) == 0
    assert json.loads(cli_env.read_text())["tokens"] == {}
