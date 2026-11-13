# Team Send Gate

Shared team assistants may send SMS only through the gated path enforced by
`scripts/send_gate.py`, wired into `bin/send_sms.py`, `scripts/send_sms.py`,
and `bin/send_group_intro.py`. The person-approved send paths —
`bin/approve_sms_draft.py`, `scripts/approve_sms_draft.py`, and the Telegram
approval callback in `scripts/webhook_server.py` — pass the same gate before
the provider call; an approval never bypasses it. No provider call happens
before the gate passes, including the approval-draft resolve path and the
generated-CLI path.

## What the gate enforces (all must pass, fail closed)

- **Caller allowlist.** `--caller` (or `DIALPAD_SEND_CALLER`) must name a caller
  in the config. A caller may send from the shared lines it is granted plus,
  when `acts_for` names a teammate, that teammate's own Dialpad line — never
  another teammate's. `extra_allowed_lines` extends the set (operator caller).
  The from-number must match an approved line exactly as E.164 (a bare
  10-digit or 1+10-digit NANP national form reads as `+1...`).
- **Opt-out union.** Dialpad company list (`GET /api/v2/company/{id}/smsoptout?opt_out_state=opted_out`,
  full cursor pagination, company from `DIALPAD_COMPANY_ID`, key from
  `DIALPAD_API_KEY`) UNION `sms_approval_opt_outs` + emergency opt-out markers.
  Either source unreadable refuses the send.
- **Recipient-local window:** 08:00 <= local < 21:00 in every zone the area
  code can cover (`scripts/nanp_area_code_timezones.py`). Unknown or
  non-US/Canada area codes refuse.
- **Minute rule:** recipient-local minute must not be :00/:15/:30/:45.
  Retryable refusals carry `retry_after_seconds`.
- Group sends refuse if any recipient fails.

Refusals exit non-zero with `send_gate_refused` in the wrapper JSON envelope;
allowlist/opt-out refusals are non-retryable, window/minute retryable.
Confirmed sends are recorded via `record_outbound_observation` as before; the
canonical message schema cannot carry caller identity, so caller (and
`acting_for`) ride on the receipt-ledger line instead — no schema change.

## Config

Path from `DIALPAD_SEND_GATE_CONFIG`; missing/unreadable config refuses every
send. Shape (see `references/send-gate.example.json`):

```json
{
  "shared_lines": {"office": "+14159065785", "sales": "+14155201316"},
  "teammates": {"scott": "+14152230323", "lilla": "+14158701945",
                "alexandre": "+14153291662"},
  "callers": {
    "team-scott": {"shared_lines": ["office", "sales"], "acts_for": "scott"},
    "team-lilla": {"shared_lines": ["office", "sales"], "acts_for": "lilla"},
    "team-alexandre": {"shared_lines": ["office", "sales"], "acts_for": "alexandre"},
    "operator": {"shared_lines": ["office", "sales"],
                 "extra_allowed_lines": ["+14153602954"]}
  }
}
```

`time_restricted` (default `true`) controls the window + minute checks for
that caller; the decided defaults keep every caller time-restricted, so the
key is omitted. The example is the decided mapping, not a template: each
`team-*` caller may send from both shared lines plus only its own teammate's
line via `acts_for` — never another teammate's line — and `operator` adds the
approved extra line `+14153602954` through `extra_allowed_lines`. On the host
the file lives outside the repo next to the other Dialpad secrets, referenced
from the environment alongside `DIALPAD_COMPANY_ID`.

## Paths

- **Gated:** `bin/send_sms.py`, `scripts/send_sms.py` (its `main()`),
  `bin/send_group_intro.py` (both mirrored recipients must pass), and the
  person-approved send paths — `bin/approve_sms_draft.py`,
  `scripts/approve_sms_draft.py`, and the Telegram approval callback in
  `scripts/webhook_server.py`, all through `sms_approval.approve_draft`.
- Approval sends evaluate the gate before the draft claim, using the draft's
  `sender_number` as the from-line and `customer_number` as the recipient.
  The caller is `DIALPAD_SEND_CALLER` from the service environment; when
  unset it falls back to `operator`, but only if the config defines an
  `operator` caller — a config without one leaves the caller unset and the
  send is refused. A refusal makes no provider call and leaves the draft in
  its retryable `pending` or `risk_pending` state (never `failed`), reports
  `send_gate_refused` with the reasons, and carries `retry_after_seconds`
  for window/minute refusals.
- **Not gated:** the raw `generated/dialpad` CLI only. Phase 2 closes it by
  keeping shared team assistants off the raw API key, so the gated endpoint
  is their only send path; direct key holders stay on the gated CLIs.

## Dry run

```bash
bin/send_sms.py --to "+14155551234" --from "+14159065785" --caller team-scott \
  --message "hi" --dry-run --json
```

Evaluates the full gate (both opt-out sources read) and prints the decision
plus what would be sent; no `POST /sms`, no shared-log write. Non-zero exit on
refusal.

## Phase 2: team send API (built on the host, edge pending)

`scripts/send_api_server.py` is a loopback-only HTTP service on the deploy
host (`DIALPAD_SEND_API_BIND`, default `127.0.0.1`; `DIALPAD_SEND_API_PORT`,
default `18889`). Each assistant holds its own bearer token; the service
hashes it with SHA-256 and maps the digest to a gate caller id from
`DIALPAD_SEND_API_TOKENS` — a JSON map of `sha256(token) -> {caller, label}`
stored mode 0600, never raw tokens. The caller id comes only from the map,
never from the request. The service refuses any send to or from the support
line `+14159917155` before running anything, then runs the gated
`bin/send_sms.py --message-stdin --json` subprocess with
`DIALPAD_SEND_CALLER` scrubbed from the child environment, so nobody but
this service holds the Dialpad API key.

- `POST /v1/sms/send` — body `{"to", "from", "message", "dry_run"}`; `to` and
  `from` must be `+1` NANP numbers (400 `invalid_argument`, the same status
  used when the child reports an `invalid_argument` error). `dry_run` maps
  to `bin/send_sms.py --dry-run` (gate evaluated, no provider call). A gate
  refusal returns 422 `send_gate_refused` with the reasons and
  `retry_after_seconds` when present; other child errors return 502 with the
  child's code and retryable flag. When the send outcome cannot be known —
  a child timeout (504) or an unparseable child envelope on a real send
  (502) — the service returns non-retryable `send_outcome_unknown` ("send
  outcome unknown; check the thread before resending") so a blind retry
  cannot double-send; unparseable output on a dry run stays 502
  `upstream_error`. `GET /health` requires the bearer and returns the
  mapped caller.
- `scripts/send_api_tokens.py` — operator CLI: `add` reads one token from
  stdin and stores only its digest; `revoke --label` and `list` (labels and
  callers only). Writes are atomic at mode 0600. The service re-reads the
  map on mtime change — and, when the gate config comes from
  `DIALPAD_SEND_GATE_CONFIG`, re-reads that too — so a revocation or a new
  caller takes effect without a restart and a corrupt map or config fails
  closed.
- `systemd/dialpad-send-api.service` is the user unit (modeled on
  `dialpad-log-api.service`); `systemd/dialpad-send-api.service.d/10-host.conf.example`
  shows the deploy-host drop-in loading the same credential files the live
  webhook uses plus the gate-config and token-map paths.

Edge (pending, set up by the Cloudflare account owner; nothing is created
from this repo). Reuse the existing remotely managed tunnel on the deploy
host that already serves the team knowledge brain; no new tunnel and no
`cloudflared tunnel login`:

1. Add a public hostname route on that tunnel (suggested
   `team-dialpad.shapescale.com`) with service `http://127.0.0.1:18889`.
   The running connector picks the route up from the dashboard; the host
   side needs no change.
2. Create a self-hosted Access application for that hostname only, with a
   single Service Auth policy that allows only the per-assistant service
   tokens. No identity-provider login, no bypass rule.
3. Create one Access service token per assistant (one per `team-*`
   caller), each with its own name and expiry, so one can be revoked
   alone.
4. On the route, turn on origin Access enforcement (Protect with Access,
   using the application's AUD tag) so cloudflared itself rejects requests
   without a valid Access JWT. The service does not parse JWTs.

Shared-log reads go through the existing Interaction Log API with a
read-scoped token.

## Phase 3: team MCP connector (built on the host, edge pending)

`scripts/team_mcp_server.py` is a stdlib-only MCP server (streamable HTTP,
single `POST /mcp` JSON-RPC 2.0 endpoint, `application/json` responses — no
SSE) bound to loopback next to the send API (`DIALPAD_TEAM_MCP_BIND`, default
`127.0.0.1`; `DIALPAD_TEAM_MCP_PORT`, default `18890`; any other bind is
refused). Each teammate's assistant connects to it like any other remote MCP
connector. It implements `initialize` (negotiates `2025-06-18`/`2025-03-26`),
`notifications/initialized` (202), `ping`, `tools/list`, and `tools/call`;
`GET /mcp` and other methods return 405, batch bodies are rejected, and a
non-empty `Origin` must be in `allowed_origins`. `GET /health` requires the
same auth and returns only the mapped caller. The host Python carries no
third-party packages, so JWT verification is implemented over stdlib
`pow()`/`hmac` — no SDK, no `cryptography`, no `jwt`.

Identity is resolved per request, never from request parameters:

- `Cf-Access-Jwt-Assertion` present → verified as a Cloudflare Access JWT:
  RS256 only, `kid` resolved against the team-domain JWKS
  (`https://<team_domain>/cdn-cgi/access/certs`, cached with TTL, one
  refetch per minute on unknown `kid`), RSASSA-PKCS1-v1_5/SHA-256 verified
  by rebuilding the full EMSA block and comparing with `hmac.compare_digest`,
  `iss`/`aud`/`exp`/`nbf`/`iat` checked (60 s leeway). The `email` claim maps
  to a caller via `email_callers`; unknown email → 403. Any JWT failure is
  401 and never falls back to bearer; a JWT header with `access` unset is
  refused too.
- Otherwise `Authorization: Bearer` resolves through the existing send-api
  token map (`DIALPAD_SEND_API_TOKENS`, SHA-256 digests, fail-closed mtime
  reload) when `allow_bearer_fallback` is true.
- Only `team-*` callers with an `acts_for` line in the gate config are
  mappable from the network — `operator` and bare shared-line callers can
  never be.

Config: `DIALPAD_TEAM_MCP_CONFIG` (JSON, mode 0600/0640 only, fail-closed,
reloaded on mtime change) — see `references/team-mcp.example.json`:

```json
{"access": {"team_domain": "<team>.cloudflareaccess.com", "aud": "<aud tag>"},
 "email_callers": {"name@example.com": "team-scott"},
 "allow_bearer_fallback": true,
 "allowed_origins": [],
 "alerts": {"store": "~/.config/dialpad/team-alerts.json",
            "allowed_url_hosts": ["example.com"],
            "callers": {"team-scott": {"shared_lines": ["office"]}}}}
```

Allowed lines per caller come straight from the send-gate config
(`send_gate.allowed_lines_for_caller`): the caller's shared lines plus its
`acts_for` teammate line. The support line `+14159917155` is stripped from
every set everywhere.

Tools (each with `additionalProperties: false`; unknown args error):

- `send_sms(to, message, from?, dry_run?)` — `from` defaults to the caller's
  own `acts_for` line; the support line is refused as `to` or `from` before
  anything runs. Sends go through the exact gated path the send API uses —
  `send_api_server.execute_gated_send` → `bin/send_sms.py --message-stdin
  --json` with `--caller <mapped>` and `DIALPAD_SEND_CALLER` scrubbed — so
  opt-outs, send window, minute rule, and line allowlists are enforced
  unchanged. Gate refusals and unknown outcomes surface as tool errors with
  the same codes (`send_gate_refused`, `send_outcome_unknown`, …). Message
  text never reaches argv or logs.
- `list_conversations(limit?, since?)` — read-only SQLite on
  `DIALPAD_SMS_DB`, grouped per (`our_line`, contact) with last activity,
  direction, preview, and unread counts, restricted to the caller's allowed
  lines.
- `read_thread(contact, limit?, line?)` — messages with a contact on the
  caller's allowed lines; `line` narrows to one allowed line and a
  non-allowed line is an error, not an empty result.
- `set_sms_alert_url(url, key)` — registers this caller's alert webhook:
  HTTPS only (loopback HTTP for tests), host must match
  `alerts.allowed_url_hosts` when configured, no IP literals or userinfo.
  Stored mode 0600 in the alerts store; the key is never echoed. Empty `url`
  clears the registration. The response lists which lines will alert.

Inbound SMS alerts (`scripts/team_alerts.py`, wired in
`scripts/webhook_server.py` next to the existing `ops_wake.notify_sms_event`)
forward a minimal wake payload — `event`, `line`, `occurred_at`, the
Dialpad `message_id`/`conversation_id`/`mms` flags, `sent_at`, and
`suppressed_since_last` —
never message bodies or customer numbers; the receiving assistant calls
`read_thread` for content. Forwarding reuses ops-wake's dedupe claim
(`team-alert:<caller>:` namespace in its own dedupe DB), its minute/hour
rate caps, and its daemon-thread handoff. An inbound SMS on line `L` forwards to each registered
caller whose alert lines contain `L`, where alert lines default to the
caller's own `acts_for` line and grow by `alerts.callers.<caller>.shared_lines`
(each entry must already be in the caller's allowed set). The support line
never forwards, another teammate's line never forwards, and stored URLs are
re-validated at forward time. The webhook service needs the same
`DIALPAD_TEAM_MCP_CONFIG` (and `DIALPAD_SEND_GATE_CONFIG`) in its
environment; alert forwarding never raises and never delays the ACK.

Deploy: `systemd/dialpad-team-mcp.service` (modeled on
`dialpad-send-api.service`, same hardening, `UnsetEnvironment=DIALPAD_SEND_CALLER`)
plus `systemd/dialpad-team-mcp.service.d/10-host.conf.example`. The webhook
service's own drop-in must export `DIALPAD_TEAM_MCP_CONFIG` (and
`DIALPAD_SEND_GATE_CONFIG`, already set for the gate) so inbound forwarding
can load registrations.

Edge (pending, set up by the Cloudflare account owner; nothing is created
from this repo). Same existing team tunnel as Phase 2, separate Access app —
not the team-brain app and not the send-api app, so no token is shared:

1. Route `team-dialpad.shapescale.com` → `http://127.0.0.1:18890` on the
   existing team tunnel.
2. Create its own Access application for that hostname with **Managed OAuth**
   (Rippling IdP) so teammates sign in with their Rippling identity;
   Cloudflare puts the signed JWT in `Cf-Access-Jwt-Assertion`.
3. Set the app's `aud` tag and the team domain in `access` in
   `DIALPAD_TEAM_MCP_CONFIG`; map each teammate's IdP email to their
   `team-*` caller in `email_callers`.
4. Do not rely solely on cloudflared: the origin verifies the JWT itself,
   so a request that somehow reaches the service without a valid Access
   JWT is refused there.

## Owner actions

The caller-to-line mapping is decided in `references/send-gate.example.json`
— no open decisions remain: `team-scott`/`team-lilla`/`team-alexandre` may
send from the `office` and `sales` shared lines plus only their own person's
line via `acts_for`, and `operator` shares the two shared lines plus the
approved extra line `+14153602954`, still time-restricted like every other
caller. Human-approved sends (Telegram approval / draft approve) pass the
gate under one service-wide caller — `DIALPAD_SEND_CALLER` on the
webhook/approval service, or `operator` when it is unset — so with the
default mapping a draft whose sender is a teammate's own line is refused
there (`operator` holds no teammate line); drafts from teammate lines must
be sent by that teammate's own `team-*` caller.

- [ ] Create the config on the host (`DIALPAD_SEND_GATE_CONFIG`) from the
      decided defaults in `references/send-gate.example.json`.
- [ ] Set `DIALPAD_SEND_CALLER` in each shared assistant's service
      environment to its `team-*` caller id.
- [ ] Set `DIALPAD_COMPANY_ID`.
- [ ] Confirm the `DIALPAD_API_KEY` on the host is a company-admin key — the
      smsoptout endpoint requires one — and that the approval DB with
      `sms_approval_opt_outs` exists; otherwise every send is refused.
- [ ] Create the send-api token map (`DIALPAD_SEND_API_TOKENS`, mode 0600)
      with `scripts/send_api_tokens.py add` — one token per `team-*` caller.
- [ ] Install `systemd/dialpad-send-api.service` plus the host drop-in and
      start the user unit.
- [ ] Phase 2 edge: add the hostname route on the existing team tunnel,
      the Access app, and one service token per assistant (Cloudflare
      account owner).
- [ ] Create `DIALPAD_TEAM_MCP_CONFIG` (mode 0600) from
      `references/team-mcp.example.json` with the real team domain, AUD tag,
      email map, and alert URL suffixes.
- [ ] Install `systemd/dialpad-team-mcp.service` plus the host drop-in and
      start the user unit.
- [ ] Export `DIALPAD_TEAM_MCP_CONFIG` in the webhook service's drop-in so
      inbound alert forwarding can load registrations.
- [ ] Phase 3 edge: route `team-dialpad.shapescale.com` to
      `http://127.0.0.1:18890` on the existing team tunnel and create a
      separate Access app with Managed OAuth (Rippling IdP) — the origin
      still verifies the JWT itself (Cloudflare account owner).
