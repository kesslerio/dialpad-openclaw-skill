# Changelog

## 2026-03-04

- fix(webhook): classify Dialpad contact lookup `401` failures (`expired_token`, `missing_scope`, `invalid_audience_or_environment`, `unauthorized`) and emit explicit degraded sender-enrichment status while preserving cached-contact fallback for inbound SMS hook and Telegram notification flows.
- fix(webhook): resolve missed-call caller/line across sparse nested payloads before defaulting to `Unknown`.
- fix(webhook): add deterministic resolution paths (`payload_direct`, `payload_inferred`, `history_backfill`, `unresolved`) and include them in debug logs.
- fix(webhook): backfill unresolved missed-call caller/line from recent Dialpad call history near event timestamp (non-blocking).
- test(webhook): cover nested payload parsing, inferred line labels, history backfill, and unresolved guard behavior.
