---
date: 2026-06-23
topic: calendar-aware-demo-context
---

# Requirements — Calendar-aware demo context for missed-call drafts

## Summary

Missed-call enrichment should tell the operator whether a prospect already has a scheduled demo and what recent comms preceded the call, especially when CRM shows a demo-request deal. The follow-up should turn calendar and comms context from silent gaps into explicit sources that can produce warmer, more accurate approval drafts.

---

## Problem Frame

The missed-call card for Dr Chris / White House Chiropractic correctly found an Attio demo-request deal, but it reported `Calendar: not configured` and created a generic CRM-aware draft. A human then had to infer from Attio and prior SMS history that the likely reason for the call was unfinished booking.

Three current facts explain the miss:

- The live webhook environment has CRM and QMD configured, but calendar context is not wired.
- The calendar adapter should use the ShapeScale sales calendars in Google Calendar as the primary source for actual scheduled events.
- The calendar adapter can also surface a demo time when it finds a scheduled-demo field on the Attio deal or a Calendly event by invitee email.
- Local SMS history and strict Gmail search can show whether booking links or recent email/SMS comms preceded the missed call.

---

## Key Decisions

- **Treat ShapeScale Google Calendar as the calendar source of truth.** If CRM says the sender is in a demo-request or demo-stage segment, the card should check the Work/Martin, Alex, and Lilla calendars before treating scheduling context as absent.
- **Distinguish absence from misconfiguration.** Operators need to know whether calendar lookup was not configured, attempted but not found, or found a scheduled demo.
- **Use deterministic comms retrieval before models.** SMS/Gmail comms context should expose bounded counts/facts to the operator; any model summarization should be optional, cheap, and off the customer-facing path.
- **Prefer warmer operator-approved drafts, not autonomous sends.** The improvement changes approval draft quality and provenance, not the no-auto-send boundary.

---

## Requirements

**Calendar source behavior**

- R1. Demo-stage missed-call enrichment must attempt calendar context whenever CRM context identifies a prospect demo segment.
- R2. Calendar source status must distinguish `not configured`, `not found`, `usable`, `unavailable`, and `not applicable` in the Telegram card and draft metadata.
- R3. When a scheduled demo is found in ShapeScale Google Calendar, Attio, or Calendly, the operator-facing card must show a concise scheduling summary.
- R4. When no scheduled demo is found for a demo-request prospect, the draft should assume booking may be incomplete rather than implying a scheduled demo exists.
- R4a. Demo-request missed-call enrichment should show compact prior-comms evidence from local SMS history and strict Gmail search when available.

**Draft behavior**

- R5. Demo-request missed-call drafts should acknowledge the missed call and the demo-request context in a warm way.
- R6. If booking appears incomplete, the draft should offer both the booking link and a human coordination path.
- R7. Customer-facing draft text must not claim there is or is not a scheduled meeting unless calendar context supports that claim.

**Operational readiness**

- R8. Deployment must make calendar wiring visible enough that `Calendar: not configured` after rollout is treated as an ops failure, not normal behavior.
- R9. The improvement must fail closed to the existing CRM-aware draft when calendar lookup is unavailable or inconclusive.
- R10. Raw SMS/email bodies must not be inserted into customer-facing draft text; operator provenance may show compact counts/facts.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R4, R4a, R6.** Given a missed call from a demo-request prospect with no scheduled demo found, the card shows CRM usable, calendar not found, comms evidence when present, and the draft offers the booking link plus help coordinating.
- AE2. **Covers R2, R3, R7.** Given a missed call from a prospect with a future demo found, the card shows the scheduled-demo summary and the draft avoids re-sending the booking link as if booking failed.
- AE3. **Covers R8, R9.** Given the calendar command is missing in production, the card says calendar not configured, the draft falls back safely, and the deploy checklist flags the missing command.

---

## Scope Boundaries

- Do not build autonomous SMS sending.
- Do not replace the CRM identity resolver.
- Do not require calendar context for non-demo missed calls.
- Do not expose secrets or raw calendar payloads in Telegram or draft metadata.
- Do not put raw SMS/email bodies into draft metadata or customer-facing text.

---

## Dependencies / Assumptions

- The calendar source needs deployment wiring for the existing calendar context command and required calendar credentials.
- Attio may not contain a scheduled-demo field for every booked prospect, so a calendar provider path remains necessary.
- Calendly lookup depends on resolving or carrying an invitee email into the calendar query.
- ShapeScale Google Calendar reads should use `shapescale-gog` through `martin@shapescale.com`, querying the Work/Martin, Alex, and Lilla calendars as the sales demo search set.
- ShapeScale Gmail reads should use `shapescale-gog` through `martin@shapescale.com` with strict exact-match queries; weak Gmail results should be ignored rather than summarized.
- No model is required for the first comms implementation. If summarization is later needed, it should run through an explicitly configured cheap model command and remain operator-only by default.

---

## Sources

- `docs/reference/enrichment-adapters.md`
- `docs/brainstorms/2026-06-18-s1-wire-enrichment-paths-requirements.md`
- `docs/brainstorms/2026-06-22-missed-call-enrichment-requirements.md`
- `scripts/webhook_server.py`
- `scripts/adapters/calendar_context.py`
- `scripts/adapters/attio_context.py`
