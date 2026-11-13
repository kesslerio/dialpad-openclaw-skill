# Changelog

## 2026-11-13

- feat(team_mcp): add `scripts/team_mcp_server.py`, a stdlib-only loopback MCP server (`127.0.0.1:18890`, streamable HTTP JSON-RPC at `POST /mcp`) so each teammate's assistant can use Dialpad texting as a remote connector. Identity: Cloudflare Access JWT from `Cf-Access-Jwt-Assertion` verified in pure stdlib (RS256 `kid`→JWKS on the team domain, `iss`/`aud`/expiry, email→caller map) with a 401-never-fallback rule, or the existing send-api bearer token map when `allow_bearer_fallback`; only `team-*` callers with `acts_for` are mappable. Tools: `send_sms` (same gated `bin/send_sms.py` path via the extracted `send_api_server.execute_gated_send`, support line refused before anything), `list_conversations`/`read_thread` scoped to the caller's own + office + sales lines, and `set_sms_alert_url` (validated HTTPS/loopback-HTTP webhook, mode-0600 store, key never echoed). Adds `references/team-mcp.example.json`, `systemd/dialpad-team-mcp.service` and a host drop-in example; the Cloudflare tunnel route + Managed-OAuth Access app stays pending for the account owner.
- feat(team_alerts): add `scripts/team_alerts.py`, wired into `scripts/webhook_server.py` next to `ops_wake.notify_sms_event` — inbound SMS on a caller's alert lines (own line by default; office/sales opt-in via config, always within the caller's allowed set, support line never) posts a minimal dedupe-claimed wake on a daemon thread so the receiving assistant re-reads the thread via MCP.
- refactor(send_api): extract `execute_gated_send` (argv build, stdin message, `DIALPAD_SEND_CALLER` scrub, timeout/envelope mapping) shared by the send API and the MCP `send_sms` tool; add public `send_gate.allowed_lines_for_caller`.
- test(team_mcp): cover the HTTP round trip (initialize/tools/list/tools/call, notifications 202, 405/404/batch/parse errors, Origin gate, health), support-line and cross-teammate refusals including a real gated dry-run, scoped conversation/thread reads, alert-URL validation and clear, bearer fallback enable/disable, unmappable callers, config mode + fail-closed reload, and stdlib JWT verification (valid mapping, bad signature, wrong aud/iss, expired, alg none, unknown email, no bearer fallback, unknown-kid refetch) with keys minted by `openssl` at test time.
- test(team_alerts): cover own-line forwarding with minimal payloads, office/sales opt-in, other-teammate and support lines never forwarded, dedupe, per-caller targeting, forward-time URL revalidation, corrupt config fail-closed, and the non-blocking daemon handoff.
- feat(send_api): add `scripts/send_api_server.py`, a loopback-only authenticated HTTP send service for shared team assistants — per-assistant bearer tokens map (SHA-256 digests in mode-0600 `DIALPAD_SEND_API_TOKENS`, reloaded with the gate config when either file's mtime changes, fail-closed) to send-gate caller ids and run the existing gated `bin/send_sms.py --message-stdin --json` path with `DIALPAD_SEND_CALLER` scrubbed from the child env; refuses sends to/from the support line before anything runs, accepts only +1 10-digit numbers, maps child envelopes to 200/400/422/502/504 (a timeout or unparseable real-send envelope is non-retryable `send_outcome_unknown`), and adds `scripts/send_api_tokens.py` (stdin-only `add`, `revoke`, `list`) plus `systemd/dialpad-send-api.service` and a host drop-in example. Cloudflare Access + tunnel edge stays pending for the account owner.
- test(send_api): cover bearer auth, token-to-caller mapping, body validation, support-line refusal, dry-run argv, stdin message delivery, env scrubbing, gate-refusal envelopes, timeout/unparseable upstream handling, startup refusals, token-map revoke/corruption fail-closed, the real `bin/send_sms.py --dry-run` subprocess path, and the token CLI.

## 2026-11-12

- fix(webhook): commit the hardening already running on the deploy host since Oct 29 so a redeploy can't drop it: `/store` accepts only loopback clients with no proxy headers and a matching `DIALPAD_STORE_TOKEN` (unset token disables `/store`); the server binds to `DIALPAD_BIND_HOSTS` (default `127.0.0.1`, extra hosts best-effort) instead of `0.0.0.0`; the hooks call no longer invents an `X-Forwarded-For` (only sent when `OPENCLAW_HOOKS_CLIENT_IP` is set); missed-call history lookups stay 15s behind local now; 500 responses no longer echo exception text.
- test(webhook): cover the `/store` localhost + token gate — non-local clients, cloudflared proxy headers (`Cf-Connecting-Ip`, `Cf-Ray`, `X-Forwarded-For`, `Cdn-Loop`), unset and wrong `DIALPAD_STORE_TOKEN`, `X-Store-Token` and `Authorization: Bearer` paths, `::1`/`::ffff:127.0.0.1` loopback — plus the generic `{"error":"internal error"}` 500 body and `DIALPAD_BIND_HOSTS` parsing.
- test(webhook): update the `/store` receipt-mutation regression to pass the new gate (local client + store token) so it still proves delivery-status mutation is refused with `delivery_status_requires_authenticated_webhook`.
- fix(webhook): the `delivery_status` branch of `/webhook/dialpad` now returns the generic `{"error":"internal error"}` 500 body and logs the failure server-side instead of echoing `str(exc)`; README and `systemd/dialpad-log-api.service` now document `DIALPAD_BIND_HOSTS` (default `127.0.0.1`, Docker bridge gateway as an extra host for container reachability) and the `/store` loopback + `DIALPAD_STORE_TOKEN` contract.

## 2026-09-22

- fix(send_sms): run the generated Dialpad CLI through a deterministic managed environment — the wrapper's own interpreter with `vendor/`, constructed from the pinned, hash-verified `requirements.txt` by `scripts/build_vendor.py` (the tree itself is untracked; see `docs/reference/vendor-build.md`) — instead of discovering an ambient `uv` on `PATH`, which the deployed gateway runtime does not carry. This closes the #89/#155 recurrence where the CLI failed with `ModuleNotFoundError: No module named 'click'` and every approved send needed the direct-API fallback.
- fix(send_sms): release a claimed approval draft back to a retryable `pending`/`risk_pending` state after a local wrapper failure instead of marking it terminally `failed`; the error envelope carries sanitized recovery context, and no receipt or outbound observation is written for a send that never completed.
- fix(dialpad): the `generated/dialpad` facade runs the raw CLI under the same vendored `PYTHONPATH` and drops its ambient `uv` probe (`DIALPAD_OPENAPI_PYTHON` still pins an explicit interpreter).
- test(send_sms): add managed-environment regressions that fail if click is unavailable to the generated CLI path (including the delivery-step build and its click-less control), pin-list/hash assertions, a byte-identical reconstruction check, plus draft-retryability coverage for local failures and `release_agent_direct_send_claim`.

## 2026-09-01

- fix(webhook): verify and decode Dialpad JWT-encoded event bodies. When the Dialpad webhook has a signature secret configured, events arrive as HS256 JWTs with the event JSON inside the payload; `scripts/webhook_server.py` now verifies the signature against `DIALPAD_WEBHOOK_SECRET` and decodes the payload for the SMS, call, and voicemail handlers instead of rejecting every event with 401.
- fix(webhook): reject JWTs whose header JSON is not an object (both Bearer and JWT-encoded-event paths) instead of raising an unhandled `AttributeError`.
- test(webhook): cover the JWT-encoded event body auth/decode paths — valid signed bodies, tampered signatures, `alg` confusion, non-object JWT headers, and plain-JSON fallback.
- docs(webhook): document JWT-encoded event handling and correct the example hook agent to `romeo-work`.

## 2026-03-25

- feat(list_calls): add `bin/list_calls.py` as the supported agent-facing recent-call wrapper with table and JSON envelope output.
- test(list_calls): cover wrapper JSON output plus structured recent-call summaries for issue `#50`.
- docs(list_calls): document the supported recent-call wrapper across `README.md`, `SKILL.md`, and reference docs.

## 2026-03-20

- fix(sms_sqlite): stop using `MAX(contact_name)` for contact summaries and instead use the latest non-empty message contact name per phone number to prevent stale identity mappings.
- feat(sms_sqlite): add `python3 scripts/sms_sqlite.py cleanup [number]` to reconcile stale local contact cache rows and remove orphaned contact entries.
- test(sms_sqlite): add regressions covering latest-name selection and cleanup reconciliation behavior.

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
