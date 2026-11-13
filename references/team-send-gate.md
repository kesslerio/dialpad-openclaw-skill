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
