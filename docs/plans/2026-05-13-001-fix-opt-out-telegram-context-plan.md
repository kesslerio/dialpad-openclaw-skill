---
title: "fix: include opt-out context in Telegram alerts"
status: active
created: 2026-05-13
---

# Problem

Inbound Dialpad SMS messages that match explicit opt-out language correctly block automation and send a human-only Telegram notice, but the notice only includes the phone number. Operators cannot see who opted out or what the inbound message said without opening Dialpad.

# Scope

Add contact identity and the inbound SMS body to the existing opt-out Telegram notification. Do not weaken opt-out blocking, do not create drafts for opt-out messages, and do not change outbound SMS behavior.

# Requirements

- Keep `filtered_opt_out` as a hard stop for automation.
- Preserve durable opt-out persistence through `sms_approval.mark_opt_out`.
- Send Telegram opt-out notices with:
  - the contact display name when available, using the same display style as ordinary inbound SMS notices
  - the phone number
  - the inbound message text, for example `STOP`
- Avoid sending opt-out messages to OpenClaw hooks or creating approval drafts.

# Implementation Units

## U1: Enrich opt-out Telegram notice

Files:
- Modify: `scripts/webhook_server.py`
- Test: `tests/test_sender_enrichment.py`

Approach:
- Reuse the `sender_enrichment["contact_name"]` value already computed in `DialpadWebhookHandler.handle_webhook`.
- Build the opt-out `From:` line as `Contact Name (+number)` when contact identity exists, otherwise keep the current phone-only fallback.
- Add a `Message:` line containing escaped inbound text.
- Keep the final human-only warning text.

Test scenarios:
- Opt-out inbound text with an enriched contact sends a Telegram notice containing the contact name, number, and exact inbound text.
- Opt-out inbound text still returns `hook_status == "filtered_opt_out"`, creates no draft, sends no SMS, calls no OpenClaw hook, and persists opt-out state.

# Verification

- `python3 -m pytest tests/test_sender_enrichment.py -q`
- Optional broader smoke: `python3 -m pytest tests/test_webhook_server.py tests/test_sender_enrichment.py -q`
