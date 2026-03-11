# Changelog

## 2026-03-11

- fix(send_sms): add `--message-file` and `--message-stdin` safe input paths so pricing text like `$499` survives shell boundaries.
- fix(send_sms): make plain `--dry-run` print the exact message preview instead of only message length.
- docs(send_sms): switch inline examples to safe quoting and document stdin/file workflows for shell-sensitive content.

## 2026-03-04

- fix(webhook): close inbound Telegram OTP/2FA bypass by centralizing inbound SMS alert eligibility (`assess_inbound_sms_alert_eligibility`) and applying the same sensitive/shortcode decision to both OpenClaw hook forwarding and direct Telegram alerts.
- fix(webhook): add safe inbound alert observability reason codes (`inbound_alert_reason`, `inbound_alert_eligible`, `telegram_status`) without exposing message secrets/tokens.
- test(webhook): cover sensitive OTP filtering, shortcode filtering, benign SMS allow-path, and hook/Telegram decision consistency for inbound SMS.
- fix(webhook): classify Dialpad contact lookup `401` failures (`expired_token`, `missing_scope`, `invalid_audience_or_environment`, `unauthorized`) and emit explicit degraded sender-enrichment status while preserving cached-contact fallback for inbound SMS hook and Telegram notification flows.
- fix(webhook): resolve missed-call caller/line across sparse nested payloads before defaulting to `Unknown`.
- fix(webhook): add deterministic resolution paths (`payload_direct`, `payload_inferred`, `history_backfill`, `unresolved`) and include them in debug logs.
- fix(webhook): backfill unresolved missed-call caller/line from recent Dialpad call history near event timestamp (non-blocking).
- test(webhook): cover nested payload parsing, inferred line labels, history backfill, and unresolved guard behavior.
- fix(webhook): require caller/line match evidence before applying missed-call history backfill to unresolved fields.
- fix(webhook): treat unparsable call-history duration as unknown (`None`) instead of missed (`0`) to avoid false missed-like classification.
- test(webhook): add regressions for no-match backfill rejection and duration-parse-failure non-missed behavior.
