"""Pytest fixtures and configuration for Dialpad OpenClaw Skill tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from inbound_driver import InboundDriver, _FakeResponse, _FakeCompletedProcess
import webhook_server


@pytest.fixture(autouse=True)
def _clear_emergency_opt_out_memory():
    if hasattr(webhook_server, "sms_approval") and webhook_server.sms_approval:
        webhook_server.sms_approval._EMERGENCY_OPT_OUT_MEMORY.clear()
    yield
    if hasattr(webhook_server, "sms_approval") and webhook_server.sms_approval:
        webhook_server.sms_approval._EMERGENCY_OPT_OUT_MEMORY.clear()


@pytest.fixture
def inbound_driver(monkeypatch, tmp_path):
    """Fixture providing a configured InboundDriver with isolated SQLite databases and mocks."""
    return InboundDriver(monkeypatch, tmp_path)
