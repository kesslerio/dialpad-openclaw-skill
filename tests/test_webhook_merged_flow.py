import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import webhook_server


class ImmediateTimer:
    def __init__(self, _seconds, callback, args=()):
        self.callback = callback
        self.args = args
        self.daemon = False
        self.started = False

    def start(self):
        self.started = True


class RecordingTimer:
    instances = []

    def __init__(self, seconds, callback, args=()):
        self.seconds = seconds
        self.callback = callback
        self.args = args
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture(autouse=True)
def reset_merged_flow_counters():
    with webhook_server._MERGED_FLOW_COUNTER_LOCK:
        webhook_server._MERGED_FLOW_COUNTERS.update(
            {"callback": 0, "fallback": 0, "consecutive_fallback": 0}
        )
    RecordingTimer.instances.clear()


def _build_handler(payload, headers=None):
    raw = json.dumps(payload).encode("utf-8")
    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler.headers = {"Content-Length": str(len(raw))}
    if headers:
        handler.headers.update(headers)
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 12345)

    status = {"code": None}

    def _send_response(code):
        status["code"] = code

    handler.send_response = _send_response
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None
    handler.send_error = lambda code, _message=None: status.update({"code": code})
    return handler, status


def _stub_common_inbound_dependencies(monkeypatch, telegram_sends, pending_rows=None):
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "lookup_contact_enrichment", lambda _number: {"contact_name": "Jane"})
    monkeypatch.setattr(webhook_server, "apply_payload_contact_fallback", lambda enrichment, _data: enrichment)
    monkeypatch.setattr(
        webhook_server,
        "assess_inbound_sms_alert_eligibility",
        lambda *_args, **_kwargs: {
            "eligible": True,
            "reason_code": "eligible",
            "sensitive_filtered": False,
            "notification_type": "sms",
        },
    )
    monkeypatch.setattr(webhook_server, "get_line_name", lambda _number: "Sales")
    monkeypatch.setattr(webhook_server, "lookup_recent_sms_context", lambda *_args, **_kwargs: {})
    def _prepare_inbound_reply_event(normalized_event, *_args, **_kwargs):
        normalized_event["crm_context"] = {"usable": True, "company": "Acme", "stage": "Demo booked"}
        normalized_event["calendar_context"] = {"usable": True, "summary": "Demo tomorrow"}
        normalized_event["comms_context"] = {"usable": True, "summary": "2 recent SMS"}
        normalized_event["rich_reply"] = {"usable": True, "basis": "shapescale_knowledge"}

    monkeypatch.setattr(webhook_server, "prepare_inbound_reply_event", _prepare_inbound_reply_event)
    monkeypatch.setattr(webhook_server, "should_send_proactive_reply", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        webhook_server,
        "create_proactive_reply_draft",
        lambda *_args, **_kwargs: (True, "draft_created", "Draft text", "smsdraft_1", {"state": "eligible"}),
    )
    monkeypatch.setattr(webhook_server, "log_auto_send_shadow", lambda _event: None)
    monkeypatch.setattr(webhook_server, "write_attio_inbound_note", lambda *_args, **_kwargs: {"status": "disabled"})
    monkeypatch.setattr(webhook_server, "resolve_telegram_route", lambda *_args: ("chat-1", 42))
    monkeypatch.setattr(webhook_server, "_generate_draft_job_id", lambda: "draft-job-1")
    monkeypatch.setattr(webhook_server, "_generate_callback_token", lambda: "token-1")
    monkeypatch.setattr(webhook_server.threading, "Timer", ImmediateTimer)
    def _insert_pending_draft(job_id, event_dict, fallback_draft, callback_token, **kwargs):
        if pending_rows is not None:
            pending_rows.append(
                {
                    "job_id": job_id,
                    "event": event_dict,
                    "fallback_draft": fallback_draft,
                    "callback_token": callback_token,
                    **kwargs,
                }
            )
        return True

    monkeypatch.setattr(webhook_server, "insert_pending_draft", _insert_pending_draft)
    monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", lambda *_args, **_kwargs: (True, "http_200"))
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_sends.append(text) or True)


def test_default_callback_url_uses_webhook_port_env():
    env = os.environ.copy()
    env["PORT"] = "9099"
    env.pop("DIALPAD_DRAFT_CALLBACK_URL", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.path.insert(0, 'scripts'); "
                "import webhook_server; "
                "print(webhook_server.DIALPAD_DRAFT_CALLBACK_URL)"
            ),
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "http://host.docker.internal:9099/internal/draft-callback"


def test_merged_sms_flow_does_not_send_immediate_telegram(monkeypatch):
    telegram_sends = []
    hook_payloads = []
    pending_rows = []
    _stub_common_inbound_dependencies(monkeypatch, telegram_sends, pending_rows=pending_rows)
    monkeypatch.setattr(webhook_server, "DIALPAD_MERGED_DRAFT_FLOW", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_DRAFT_CALLBACK_URL", "http://callback/internal/draft-callback")
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized, **_kwargs: hook_payloads.append(dict(normalized)) or (True, "http_200"),
    )

    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler._process_inbound_post_ack(
        {
            "direction": "inbound",
            "from_number": "+14155550123",
            "to_number": ["+14155201316"],
            "text": "Need help",
            "id": "msg-1",
        },
        {"message": {}},
        "+14155550123",
        ["+14155201316"],
        "Need help",
        "inbound",
        "sms",
        "2026-07-06T00:00:00",
        "disabled",
    )

    assert telegram_sends == []
    assert hook_payloads[0]["operator_notification"]["deliver"] is True
    assert hook_payloads[0]["operator_notification"]["hookDelivery"] == "visible"
    assert pending_rows[0]["event"]["crm_context"]["company"] == "Acme"
    assert pending_rows[0]["event"]["calendar_context"]["summary"] == "Demo tomorrow"
    assert pending_rows[0]["event"]["comms_context"]["summary"] == "2 recent SMS"
    assert pending_rows[0]["event"]["rich_reply"]["basis"] == "shapescale_knowledge"


def test_merged_sms_storage_failure_sends_local_card_without_callback(monkeypatch):
    telegram_sends = []
    hook_payloads = []
    _stub_common_inbound_dependencies(monkeypatch, telegram_sends)
    monkeypatch.setattr(webhook_server, "DIALPAD_MERGED_DRAFT_FLOW", True)
    monkeypatch.setattr(webhook_server, "insert_pending_draft", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_CHANNEL", "telegram")
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_TO", "telegram:group:chat-1:topic:42")
    monkeypatch.setattr(webhook_server, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized, **_kwargs: hook_payloads.append(dict(normalized)) or (True, "http_200"),
    )

    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler._process_inbound_post_ack(
        {
            "direction": "inbound",
            "from_number": "+14155550123",
            "to_number": ["+14155201316"],
            "text": "Need help",
            "id": "msg-1",
        },
        {"message": {}},
        "+14155550123",
        ["+14155201316"],
        "Need help",
        "inbound",
        "sms",
        "2026-07-06T00:00:00",
        "disabled",
    )

    assert len(telegram_sends) == 1
    assert "Draft text" in telegram_sends[0]
    assert hook_payloads
    assert "callback_url" not in hook_payloads[0]
    assert "callback_job_id" not in hook_payloads[0]
    assert hook_payloads[0]["operator_notification"]["deliver"] is False


def test_merged_sms_storage_failure_allows_hook_when_local_card_fails(monkeypatch):
    telegram_sends = []
    hook_payloads = []
    _stub_common_inbound_dependencies(monkeypatch, telegram_sends)
    monkeypatch.setattr(webhook_server, "DIALPAD_MERGED_DRAFT_FLOW", True)
    monkeypatch.setattr(webhook_server, "insert_pending_draft", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized, **_kwargs: hook_payloads.append(dict(normalized)) or (True, "http_200"),
    )

    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler._process_inbound_post_ack(
        {
            "direction": "inbound",
            "from_number": "+14155550123",
            "to_number": ["+14155201316"],
            "text": "Need help",
            "id": "msg-1",
        },
        {"message": {}},
        "+14155550123",
        ["+14155201316"],
        "Need help",
        "inbound",
        "sms",
        "2026-07-06T00:00:00",
        "disabled",
    )

    assert hook_payloads
    assert hook_payloads[0]["operator_notification"]["deliver"] is True


def test_merged_sms_without_approval_draft_sends_immediate_card(monkeypatch):
    telegram_sends = []
    pending_rows = []
    _stub_common_inbound_dependencies(monkeypatch, telegram_sends, pending_rows=pending_rows)
    monkeypatch.setattr(webhook_server, "DIALPAD_MERGED_DRAFT_FLOW", True)
    monkeypatch.setattr(
        webhook_server,
        "create_proactive_reply_draft",
        lambda *_args, **_kwargs: (False, "approval_unavailable", None, None, None),
    )

    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler._process_inbound_post_ack(
        {
            "direction": "inbound",
            "from_number": "+14155550123",
            "to_number": ["+14155201316"],
            "text": "Need help",
            "id": "msg-1",
        },
        {"message": {}},
        "+14155550123",
        ["+14155201316"],
        "Need help",
        "inbound",
        "sms",
        "2026-07-06T00:00:00",
        "disabled",
    )

    assert len(telegram_sends) == 1
    assert "Dialpad SMS" in telegram_sends[0]
    assert pending_rows == []


def test_merged_sms_hook_failure_renders_local_card_immediately(monkeypatch, tmp_path):
    telegram_sends = []
    hook_payloads = []
    real_insert_pending_draft = webhook_server.insert_pending_draft
    _stub_common_inbound_dependencies(monkeypatch, telegram_sends)
    monkeypatch.setattr(webhook_server, "DIALPAD_MERGED_DRAFT_FLOW", True)
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "pending.db")
    monkeypatch.setattr(webhook_server, "insert_pending_draft", real_insert_pending_draft)
    monkeypatch.setattr(webhook_server.threading, "Timer", RecordingTimer)
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized, **_kwargs: hook_payloads.append(dict(normalized)) or (False, "disabled"),
    )

    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler._process_inbound_post_ack(
        {
            "direction": "inbound",
            "from_number": "+14155550123",
            "to_number": ["+14155201316"],
            "text": "Need help",
            "id": "msg-1",
        },
        {"message": {}},
        "+14155550123",
        ["+14155201316"],
        "Need help",
        "inbound",
        "sms",
        "2026-07-06T00:00:00",
        "disabled",
    )

    assert len(telegram_sends) == 1
    assert "Draft text" in telegram_sends[0]
    assert hook_payloads[0]["callback_job_id"] == "draft-job-1"
    assert RecordingTimer.instances[0].cancelled is True


def test_merged_missed_call_waiting_hook_stays_visible(monkeypatch):
    hook_payloads = []
    monkeypatch.setattr(webhook_server, "verify_webhook_auth", lambda *_args: (True, "test"))
    monkeypatch.setattr(webhook_server, "DIALPAD_MERGED_DRAFT_FLOW", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_DRAFT_CALLBACK_URL", "http://callback/internal/draft-callback")
    monkeypatch.setattr(webhook_server, "_generate_draft_job_id", lambda: "draft-job-1")
    monkeypatch.setattr(webhook_server, "_generate_callback_token", lambda: "token-1")
    monkeypatch.setattr(webhook_server.threading, "Timer", RecordingTimer)
    monkeypatch.setattr(
        webhook_server,
        "resolve_missed_call_context",
        lambda *_args, **_kwargs: {
            "from_number": "+14155550123",
            "to_number": "+14155201316",
            "event_ts_ms": 1770000000000,
            "line_display": "Sales",
            "caller_resolution_path": "payload_direct",
            "line_resolution_path": "payload_direct",
            "caller_confidence": "high",
            "line_confidence": "high",
            "history_matched": False,
        },
    )
    monkeypatch.setattr(webhook_server, "build_missed_call_dedupe_key", lambda *_args, **_kwargs: "call-1")
    monkeypatch.setattr(webhook_server, "claim_missed_call_notification", lambda *_args, **_kwargs: {"duplicate": False, "status": "claimed"})
    monkeypatch.setattr(webhook_server, "lookup_contact_enrichment", lambda _number: {"contact_name": "Jane"})
    monkeypatch.setattr(webhook_server, "apply_payload_contact_fallback", lambda enrichment, _data: enrichment)
    monkeypatch.setattr(webhook_server, "get_line_name", lambda _number: "Sales")
    monkeypatch.setattr(webhook_server, "lookup_recent_call_context", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(webhook_server, "prepare_inbound_reply_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(webhook_server, "should_send_proactive_reply", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        webhook_server,
        "create_proactive_reply_draft",
        lambda *_args, **_kwargs: (True, "draft_created", "Draft text", "smsdraft_1", {"state": "eligible"}),
    )
    monkeypatch.setattr(webhook_server, "log_auto_send_shadow", lambda _event: None)
    monkeypatch.setattr(webhook_server, "resolve_telegram_route", lambda *_args: ("chat-1", 42))
    monkeypatch.setattr(webhook_server, "insert_pending_draft", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        webhook_server,
        "send_to_openclaw_hooks",
        lambda normalized, **_kwargs: hook_payloads.append(dict(normalized)) or (True, "http_200"),
    )

    handler, status = _build_handler(
        {
            "direction": "inbound",
            "call_missed": True,
            "duration": 0,
            "from_number": "+14155550123",
            "to_number": "+14155201316",
            "call_id": "call-1",
        }
    )
    handler.handle_call_webhook()

    assert status["code"] == 200
    assert hook_payloads
    assert hook_payloads[0]["operator_notification"]["deliver"] is True
    assert hook_payloads[0]["operator_notification"]["hookDelivery"] == "visible"


def test_merged_missed_call_without_approval_draft_sends_immediate_card(monkeypatch):
    hook_payloads = []
    telegram_sends = []
    pending_calls = []
    monkeypatch.setattr(webhook_server, "verify_webhook_auth", lambda *_args: (True, "test"))
    monkeypatch.setattr(webhook_server, "DIALPAD_MERGED_DRAFT_FLOW", True)
    monkeypatch.setattr(
        webhook_server,
        "resolve_missed_call_context",
        lambda *_args, **_kwargs: {
            "from_number": "+14155550123",
            "to_number": "+14155201316",
            "event_ts_ms": 1770000000000,
            "line_display": "Sales",
            "caller_resolution_path": "payload_direct",
            "line_resolution_path": "payload_direct",
            "caller_confidence": "high",
            "line_confidence": "high",
            "history_matched": False,
        },
    )
    monkeypatch.setattr(webhook_server, "build_missed_call_dedupe_key", lambda *_args, **_kwargs: "call-1")
    monkeypatch.setattr(webhook_server, "claim_missed_call_notification", lambda *_args, **_kwargs: {"duplicate": False, "status": "claimed"})
    monkeypatch.setattr(webhook_server, "lookup_contact_enrichment", lambda _number: {"contact_name": "Jane"})
    monkeypatch.setattr(webhook_server, "apply_payload_contact_fallback", lambda enrichment, _data: enrichment)
    monkeypatch.setattr(webhook_server, "get_line_name", lambda _number: "Sales")
    monkeypatch.setattr(webhook_server, "lookup_recent_call_context", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(webhook_server, "prepare_inbound_reply_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(webhook_server, "should_send_proactive_reply", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        webhook_server,
        "create_proactive_reply_draft",
        lambda *_args, **_kwargs: (False, "approval_unavailable", None, None, None),
    )
    monkeypatch.setattr(webhook_server, "log_auto_send_shadow", lambda _event: None)
    monkeypatch.setattr(webhook_server, "resolve_telegram_route", lambda *_args: ("chat-1", 42))
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_CHANNEL", "telegram")
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_TO", "telegram:group:chat-1:topic:42")
    monkeypatch.setattr(webhook_server, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(webhook_server, "insert_pending_draft", lambda *args, **_kwargs: pending_calls.append(args) or True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_sends.append(text) or True)
    monkeypatch.setattr(
        webhook_server,
        "send_to_openclaw_hooks",
        lambda normalized, **_kwargs: hook_payloads.append(dict(normalized)) or (True, "http_200"),
    )

    handler, status = _build_handler(
        {
            "direction": "inbound",
            "call_missed": True,
            "duration": 0,
            "from_number": "+14155550123",
            "to_number": "+14155201316",
            "call_id": "call-1",
        }
    )
    handler.handle_call_webhook()

    assert status["code"] == 200
    assert pending_calls == []
    assert len(telegram_sends) == 1
    assert "Missed Call" in telegram_sends[0]
    assert hook_payloads
    assert "callback_job_id" not in hook_payloads[0]
    assert hook_payloads[0]["operator_notification"]["deliver"] is False


def test_non_merged_sms_flow_sends_one_immediate_telegram(monkeypatch):
    telegram_sends = []
    _stub_common_inbound_dependencies(monkeypatch, telegram_sends)
    monkeypatch.setattr(webhook_server, "DIALPAD_MERGED_DRAFT_FLOW", False)

    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler._process_inbound_post_ack(
        {
            "direction": "inbound",
            "from_number": "+14155550123",
            "to_number": ["+14155201316"],
            "text": "Need help",
            "id": "msg-1",
        },
        {"message": {}},
        "+14155550123",
        ["+14155201316"],
        "Need help",
        "inbound",
        "sms",
        "2026-07-06T00:00:00",
        "disabled",
    )

    assert len(telegram_sends) == 1
    assert "Dialpad SMS" in telegram_sends[0]


def test_hook_message_uses_context_aware_draft_guidance():
    message = webhook_server.format_hook_message(
        {
            "sender": "Jane",
            "sender_number": "+14155550123",
            "recipient_number": "+14155201316",
            "text": "Can I move my demo?",
            "conversation_id": "conv-1",
        },
        line_display="Sales",
        callback_url="http://callback/internal/draft-callback",
        callback_job_id="draft-job-1",
        callback_token="token-1",
    )

    assert "specific, warm plain-text SMS reply" in message
    assert "booked-demo details" in message
    assert "submit_draft tool" in message
    assert '- token: "token-1"' in message
    assert "X-Callback-Token: token-1" in message


def test_render_merged_card_includes_provenance_and_counters(monkeypatch, capsys):
    telegram_sends = []
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_sends.append(text) or True)

    webhook_server._render_merged_card(
        "draft-job-1",
        "Draft text",
        {
            "route_chat_id": "chat-1",
            "route_thread_id": 42,
            "event": {
                "event_type": "sms",
                "sender_number": "+14155550123",
                "recipient_number": "+14155201316",
                "text": "Hello",
                "auto_reply_draft_id": "smsdraft_1",
                "fallback_draft": "Fallback text",
                "reply_policy": {"state": "eligible"},
                "crm_context": {"usable": True, "company": "Acme", "stage": "Demo booked"},
            },
        },
        path="fallback_timeout",
        elapsed_ms=180000,
    )

    assert "Attio: Acme" in telegram_sends[0]
    assert "stage: Demo booked" in telegram_sends[0]
    output = capsys.readouterr().out
    assert "path=fallback_timeout" in output
    assert "callback=0 fallback=1 consecutive_fallback=1" in output


def test_fallback_warning_threshold_and_callback_reset(capsys):
    webhook_server._record_merged_flow_path("fallback_timeout")
    webhook_server._record_merged_flow_path("fallback_timeout")
    webhook_server._record_merged_flow_path("fallback_timeout")
    assert "consecutive fallback threshold reached" in capsys.readouterr().out

    webhook_server._record_merged_flow_path("callback_lost")
    assert webhook_server._MERGED_FLOW_COUNTERS["consecutive_fallback"] == 0


def test_non_timeout_fallback_does_not_increment_dead_pipe_counter():
    webhook_server._record_merged_flow_path("fallback")

    with webhook_server._MERGED_FLOW_COUNTER_LOCK:
        assert webhook_server._MERGED_FLOW_COUNTERS == {
            "callback": 0,
            "fallback": 0,
            "consecutive_fallback": 0,
        }


def test_resume_pending_drafts_after_restart_renders_expired_and_leaves_fresh(monkeypatch, tmp_path):
    db_path = tmp_path / "pending.db"
    telegram_sends = []
    monkeypatch.setattr(webhook_server.threading, "Timer", RecordingTimer)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_sends.append(text) or True)
    webhook_server.insert_pending_draft(
        "old-job",
        {"event_type": "sms"},
        "Fallback",
        "token",
        db_path=db_path,
    )
    webhook_server.insert_pending_draft(
        "fresh-job",
        {"event_type": "sms"},
        "Fallback",
        "token",
        db_path=db_path,
    )
    conn = webhook_server._init_pending_drafts_db(db_path=db_path)
    try:
        conn.execute(
            f"UPDATE {webhook_server.PENDING_DRAFTS_TABLE} SET created_at_ms=? WHERE job_id=?",
            (webhook_server._now_ms() - 5000, "old-job"),
        )
        conn.commit()
    finally:
        conn.close()

    resumed = webhook_server.resume_pending_drafts_after_restart(timeout_seconds=1, db_path=db_path)

    assert resumed == {"rendered": 1, "scheduled": 1}
    assert len(telegram_sends) == 1
    assert webhook_server.claim_pending_draft("fresh-job", db_path=db_path)["job_id"] == "fresh-job"


def test_resume_pending_drafts_after_restart_schedules_fresh_rows(monkeypatch, tmp_path):
    db_path = tmp_path / "pending.db"
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: db_path)
    monkeypatch.setattr(webhook_server.threading, "Timer", RecordingTimer)
    webhook_server.insert_pending_draft(
        "fresh-job",
        {"event_type": "sms"},
        "Fallback",
        "token",
    )
    conn = webhook_server._init_pending_drafts_db(db_path=db_path)
    try:
        conn.execute(
            f"UPDATE {webhook_server.PENDING_DRAFTS_TABLE} SET created_at_ms=? WHERE job_id=?",
            (webhook_server._now_ms() - 500, "fresh-job"),
        )
        conn.commit()
    finally:
        conn.close()

    resumed = webhook_server.resume_pending_drafts_after_restart(timeout_seconds=2)

    assert resumed == {"rendered": 0, "scheduled": 1}
    assert RecordingTimer.instances[0].started is True
    assert 1.0 <= RecordingTimer.instances[0].seconds <= 2.0
    assert RecordingTimer.instances[0].args[0] == "fresh-job"


def test_resume_pending_drafts_after_restart_skips_over_retention_rows(monkeypatch, tmp_path):
    db_path = tmp_path / "pending.db"
    telegram_sends = []
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: db_path)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_sends.append(text) or True)
    webhook_server.insert_pending_draft(
        "stale-job",
        {"event_type": "sms"},
        "Fallback",
        "token",
    )
    conn = webhook_server._init_pending_drafts_db(db_path=db_path)
    try:
        conn.execute(
            f"UPDATE {webhook_server.PENDING_DRAFTS_TABLE} SET created_at_ms=? WHERE job_id=?",
            (webhook_server._now_ms() - webhook_server.PENDING_DRAFTS_RETENTION_MS - 1000, "stale-job"),
        )
        conn.commit()
    finally:
        conn.close()

    resumed = webhook_server.resume_pending_drafts_after_restart(timeout_seconds=1)

    assert resumed == {"rendered": 0, "scheduled": 0}
    assert telegram_sends == []
    assert webhook_server.claim_pending_draft("stale-job", db_path=db_path) is None


def test_resume_pending_drafts_after_restart_keeps_rows_inside_long_timeout(monkeypatch, tmp_path):
    db_path = tmp_path / "pending.db"
    monkeypatch.setattr(webhook_server.threading, "Timer", RecordingTimer)
    webhook_server.insert_pending_draft(
        "slow-job",
        {"event_type": "sms"},
        "Fallback",
        "token",
        db_path=db_path,
    )
    conn = webhook_server._init_pending_drafts_db(db_path=db_path)
    try:
        conn.execute(
            f"UPDATE {webhook_server.PENDING_DRAFTS_TABLE} SET created_at_ms=? WHERE job_id=?",
            (webhook_server._now_ms() - (80 * 60 * 1000), "slow-job"),
        )
        conn.commit()
    finally:
        conn.close()

    resumed = webhook_server.resume_pending_drafts_after_restart(timeout_seconds=7200, db_path=db_path)

    assert resumed == {"rendered": 0, "scheduled": 1}
    assert RecordingTimer.instances[0].args[0] == "slow-job"
    assert webhook_server.claim_pending_draft("slow-job", db_path=db_path)["job_id"] == "slow-job"


def test_draft_callback_rejects_missing_or_wrong_token(monkeypatch):
    rendered = []
    monkeypatch.setattr(webhook_server, "get_pending_draft_callback_token", lambda _job_id: {"token": "expected"})
    monkeypatch.setattr(webhook_server, "claim_pending_draft", lambda _job_id: {"event": {}})
    monkeypatch.setattr(webhook_server, "_render_merged_card", lambda *_args, **_kwargs: rendered.append(True))

    handler, status = _build_handler({"jobId": "job-1", "draft": "Draft"}, headers={"X-Callback-Token": "wrong"})
    handler.handle_draft_callback()

    assert status["code"] == 401
    assert rendered == []


def test_draft_callback_persists_agent_text_before_render(monkeypatch, tmp_path):
    approval_db = tmp_path / "approvals.db"
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
    conn = webhook_server.sms_approval.init_db()
    try:
        webhook_server.sms_approval.create_draft(
            conn,
            draft_id="smsdraft_1",
            thread_key="thread-1",
            customer_number="+14155550123",
            sender_number="+14155201316",
            draft_text="Fallback text",
        )
    finally:
        conn.close()

    db_path = tmp_path / "pending.db"
    webhook_server.insert_pending_draft(
        "job-1",
        {
            "event_type": "sms",
            "sender_number": "+14155550123",
            "recipient_number": "+14155201316",
            "text": "Hello",
            "auto_reply_draft_id": "smsdraft_1",
            "reply_policy": {"state": "eligible"},
        },
        "Fallback text",
        "expected",
        db_path=db_path,
    )
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: db_path)
    telegram_sends = []
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_sends.append(text) or True)

    handler, status = _build_handler(
        {"jobId": "job-1", "draft": "Please have a real person call me."},
        headers={"X-Callback-Token": "expected"},
    )
    handler.handle_draft_callback()

    conn = webhook_server.sms_approval.init_db()
    try:
        stored = webhook_server.sms_approval.get_draft(conn, "smsdraft_1")
        first_approval = webhook_server.sms_approval.approve_draft(
            conn,
            draft_id="smsdraft_1",
            send_func=lambda *_args, **_kwargs: {"id": "sms-1"},
            actor_id="human",
        )
    finally:
        conn.close()
    assert status["code"] == 200
    assert stored["draft_text"] == "Please have a real person call me."
    assert stored["risk_state"] == webhook_server.sms_approval.RISK_RISKY
    assert stored["metadata"]["reply_policy"]["state"] == "risky"
    assert first_approval["status"] == "risky_confirmation_required"
    assert "Risk:" in telegram_sends[0]
    assert "Second confirmation required" in telegram_sends[0]
    assert "Please have a real person call me." in telegram_sends[0]


def test_draft_callback_opt_out_blocks_existing_approval(monkeypatch, tmp_path):
    approval_db = tmp_path / "approval.db"
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
    conn = webhook_server.sms_approval.init_db()
    try:
        webhook_server.sms_approval.create_draft(
            conn,
            draft_id="smsdraft_1",
            thread_key="thread-1",
            customer_number="+14155550123",
            sender_number="+14155201316",
            draft_text="Fallback text",
        )
    finally:
        conn.close()

    db_path = tmp_path / "pending.db"
    webhook_server.insert_pending_draft(
        "job-1",
        {
            "event_type": "sms",
            "sender_number": "+14155550123",
            "recipient_number": "+14155201316",
            "text": "Hello",
            "auto_reply_draft_id": "smsdraft_1",
            "reply_policy": {"state": "eligible"},
        },
        "Fallback text",
        "expected",
        db_path=db_path,
    )
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: db_path)
    telegram_sends = []
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_sends.append(text) or True)

    handler, status = _build_handler(
        {"jobId": "job-1", "draft": "Please stop texting me."},
        headers={"X-Callback-Token": "expected"},
    )
    handler.handle_draft_callback()

    conn = webhook_server.sms_approval.init_db()
    try:
        stored = webhook_server.sms_approval.get_draft(conn, "smsdraft_1")
        preflight = webhook_server.sms_approval.preflight_agent_direct_send(
            conn,
            draft_id="smsdraft_1",
            actor_id="human",
            customer_number="+14155550123",
            sender_number="+14155201316",
            draft_text="Fallback text",
        )
        opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
    finally:
        conn.close()

    assert status["code"] == 200
    assert opted_out is True
    assert stored["status"] == webhook_server.sms_approval.STATUS_STALE
    assert preflight["status"] in {"blocked_opt_out", "stale"}
    assert "Automation blocked" in telegram_sends[0]
    assert "Approve send" not in telegram_sends[0]
    assert "approve_sms_draft.py" not in telegram_sends[0]


def test_draft_callback_persistence_failure_counts_callback_alive(monkeypatch, tmp_path):
    db_path = tmp_path / "pending.db"
    webhook_server.insert_pending_draft(
        "job-1",
        {
            "event_type": "sms",
            "sender_number": "+14155550123",
            "recipient_number": "+14155201316",
            "text": "Hello",
            "auto_reply_draft_id": "smsdraft_1",
            "reply_policy": {"state": "eligible"},
        },
        "Fallback text",
        "expected",
        db_path=db_path,
    )
    with webhook_server._MERGED_FLOW_COUNTER_LOCK:
        webhook_server._MERGED_FLOW_COUNTERS.update(
            {"callback": 0, "fallback": 2, "consecutive_fallback": 2}
        )
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: db_path)
    monkeypatch.setattr(webhook_server, "_persist_callback_draft_text", lambda *_args, **_kwargs: False)
    telegram_sends = []
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_sends.append(text) or True)

    handler, status = _build_handler(
        {"jobId": "job-1", "draft": "Agent callback text"},
        headers={"X-Callback-Token": "expected"},
    )
    handler.handle_draft_callback()

    assert status["code"] == 200
    assert "Fallback text" in telegram_sends[0]
    with webhook_server._MERGED_FLOW_COUNTER_LOCK:
        assert webhook_server._MERGED_FLOW_COUNTERS == {
            "callback": 1,
            "fallback": 2,
            "consecutive_fallback": 0,
        }


def test_draft_callback_rejects_unsafe_code_and_renders_fallback(monkeypatch, tmp_path):
    db_path = tmp_path / "pending.db"
    webhook_server.insert_pending_draft(
        "job-code-1",
        {
            "event_type": "sms",
            "sender_number": "+14155550123",
            "recipient_number": "+14155201316",
            "text": "How much for 50 users?",
            "auto_reply_draft_id": "smsdraft_code_1",
            "reply_policy": {"state": "eligible"},
        },
        "Safe deterministic fallback reply from ShapeScale",
        "expected-tok",
        db_path=db_path,
    )
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: db_path)
    telegram_sends = []
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_sends.append(text) or True)

    with webhook_server._MERGED_FLOW_COUNTER_LOCK:
        webhook_server._MERGED_FLOW_COUNTERS["consecutive_fallback"] = 3

    unsafe_code_draft = "YOURLS-MCP plugin detected.\nconst res = await fetch('http://bad.com');"
    handler, status = _build_handler(
        {"jobId": "job-code-1", "draft": unsafe_code_draft},
        headers={"X-Callback-Token": "expected-tok"},
    )
    handler.handle_draft_callback()

    assert status["code"] == 200
    response_body = json.loads(handler.wfile.getvalue().decode())
    assert response_body["status"] == "rejected"
    assert response_body["reason"] == "unsafe_draft"
    assert len(telegram_sends) == 1
    assert "Safe deterministic fallback reply from ShapeScale" in telegram_sends[0]
    assert "YOURLS" not in telegram_sends[0]
    assert "const res" not in telegram_sends[0]
    with webhook_server._MERGED_FLOW_COUNTER_LOCK:
        assert webhook_server._MERGED_FLOW_COUNTERS["consecutive_fallback"] == 0


def test_draft_callback_rejects_markdown_code_fences_and_renders_fallback(monkeypatch, tmp_path):
    db_path = tmp_path / "pending.db"
    webhook_server.insert_pending_draft(
        "job-fence-1",
        {
            "event_type": "sms",
            "sender_number": "+14155550123",
            "recipient_number": "+14155201316",
            "text": "We have 100 clients a month.",
            "auto_reply_draft_id": "smsdraft_fence_1",
            "reply_policy": {"state": "eligible"},
        },
        "Hi Alex, thanks for the info. Let's set up a demo.",
        "expected-tok-2",
        db_path=db_path,
    )
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: db_path)
    telegram_sends = []
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_sends.append(text) or True)

    unsafe_fence_draft = "```javascript\nconsole.log('client volume');\n```"
    handler, status = _build_handler(
        {"jobId": "job-fence-1", "draft": unsafe_fence_draft},
        headers={"X-Callback-Token": "expected-tok-2"},
    )
    handler.handle_draft_callback()

    assert status["code"] == 200
    response_body = json.loads(handler.wfile.getvalue().decode())
    assert response_body["status"] == "rejected"
    assert len(telegram_sends) == 1
    assert "Hi Alex, thanks for the info" in telegram_sends[0]
    assert "```" not in telegram_sends[0]

