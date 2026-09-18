"""The drain hook must run after a command's own answer, never before it."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent

from log_outbox import enqueue_observation  # noqa: E402


def _load_bin(name: str):
    """Load bin/<name>.py under a private name, with bin/ importable while it runs.

    scripts/ ships a sibling with the same stem, so importing by name resolves
    whichever directory a previous test happened to leave on sys.path. The
    wrappers themselves load their scripts sibling under an alias for the same
    reason, and this mirrors that.
    """
    path = ROOT / "bin" / f"{name}.py"
    alias = f"_outbox_hook_test_{name}"
    sys_path_backup = list(sys.path)
    sys.path.insert(0, str(ROOT / "bin"))
    try:
        spec = importlib.util.spec_from_file_location(alias, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = sys_path_backup
    assert str(module.__file__) == str(path), f"loaded the wrong file: {module.__file__}"
    assert hasattr(module, "_run"), f"{path.name} lost its _run seam"
    return module


def _observation(index: int) -> dict[str, object]:
    return {
        "provider_id": f"msg-hook-{index}",
        "direction": "outbound",
        "from_number": "+14155550140",
        "to_number": "+14155550111",
        "body": f"queued body {index}",
        "timestamp": 1770000000000 + index,
        "observed_at": "2026-02-02T03:04:05Z",
        "source": "local_send",
    }


@pytest.mark.parametrize(
    "module_name",
    ["list_sms_inbox", "list_sms_thread", "list_calls"],
)
def test_a_read_command_drains_a_stale_outbox_and_returns_its_own_answer(
    module_name: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_bin(module_name)
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    monkeypatch.setenv("DIALPAD_LOG_OUTBOX", str(outbox))
    enqueue_observation(_observation(1), path=outbox)

    with patch.object(module, "_run", return_value=7):
        code = module.main()

    assert code == 7, "the command's own exit code is the drain-proof contract"
    assert not outbox.exists()


def test_the_hook_runs_after_the_command_has_committed_its_answer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_bin("list_sms_inbox")
    sms_db = tmp_path / "sms.db"
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_DB", str(sms_db))
    monkeypatch.setenv("DIALPAD_LOG_OUTBOX", str(outbox))
    enqueue_observation(_observation(1), path=outbox)

    order: list[str] = []

    import log_outbox

    original_replay = log_outbox.replay_outbox

    def spy_replay(**kwargs):
        order.append("drain")
        return original_replay(**kwargs)

    with patch.object(module, "_run", side_effect=lambda: order.append("command") or 0), \
         patch("log_outbox.replay_outbox", side_effect=spy_replay):
        module.main()

    assert order == ["command", "drain"]


def test_a_broken_outbox_never_changes_a_commands_exit_code(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_bin("list_sms_inbox")
    outbox = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("DIALPAD_LOG_URL", "http://127.0.0.1:18887")
    monkeypatch.setenv("DIALPAD_LOG_TOKEN", "unit-token")
    monkeypatch.setenv("DIALPAD_LOG_OUTBOX", str(outbox))
    outbox.write_text("{trova\n", encoding="utf-8")

    with patch.object(module, "_run", return_value=0):
        assert module.main() == 0
