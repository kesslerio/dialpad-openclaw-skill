---
date: 2026-06-22
topic: missed-call-enrichment
---

# Requirements — Missed-call enrichment for Dialpad Sales drafts

## Summary

Sales missed-call approval drafts should attempt the same enrichment intent as Sales SMS drafts before falling back to boilerplate. The Telegram handoff should show which context sources were attempted, which matched, and why a generic fallback was used.

---

## Problem Frame

The Sales missed-call path can already create approval drafts, but low-confidence or payload-only callers still get generic copy even when Attio, calendar, or QMD context might make the operator draft more useful. This repeats the earlier SMS "super generic" failure mode on a different event type.

The existing SMS enrichment work established a useful safety rule: enrichment can be shown to the operator at low confidence, but customer-facing names and company data must not appear unless identity confidence is high. Missed calls should inherit that rule rather than reopening the PII boundary.

---

## Key Decisions

- **Extend the enrichment intent to missed calls.** Missed-call drafts should not be treated as unsupported solely because they are not SMS events.
- **Keep call-specific customer copy.** Enriched missed-call drafts should still sound like a missed-call follow-up, not a reused inbound-text response.
- **Preserve operator-only low-confidence context.** Attio, calendar, and QMD matches can be surfaced in Telegram and draft metadata at low confidence, but low-confidence customer-facing text stays generic.
- **Make generic fallback explainable.** A generic draft should mean applicable sources were unavailable, unusable, or unsafe, not that the system skipped enrichment silently.

---

## Requirements

**Draft selection**

- R1. Sales missed-call approval drafts must attempt applicable enrichment before selecting generic fallback.
- R2. Missed-call enrichment must support CRM-aware context when Attio returns usable context for the caller.
- R3. Missed-call enrichment must support meeting-aware context when the caller appears tied to a scheduled or recent sales/demo context.
- R4. Missed-call enrichment must not require a high-confidence Dialpad identity before operator-facing enrichment can run.
- R5. Generic missed-call fallback must remain available when all applicable enrichment sources are unusable or unavailable.

**Customer-facing safety**

- R6. Low-confidence missed-call drafts must not include a payload-only name, low-confidence Attio name, or low-confidence company name in customer-facing SMS text.
- R7. High-confidence missed-call drafts may use known-contact personalization when the same safety conditions used by the SMS path are satisfied.
- R8. Missed-call drafts must keep missed-call-specific wording, including an apology for the missed call where appropriate.

**Operator handoff**

- R9. The Telegram handoff must distinguish CRM-aware, meeting-aware, knowledge-backed, context-aware, and generic missed-call draft bases.
- R10. The Telegram handoff must show source provenance for usable enrichment in an operator-facing location.
- R11. When a generic fallback is created, the Telegram handoff must show enough source status to explain whether enrichment was not attempted, not configured, not found, degraded, unsafe, or otherwise unusable.

**Regression protection**

- R12. Tests must cover missed calls entering the enrichment-eligible approval-draft lane.
- R13. Tests must cover low-confidence missed calls surfacing enrichment to the operator without leaking low-confidence names or company data into customer-facing text.
- R14. Tests must cover the unchanged generic fallback behavior when enrichment sources are unusable.

---

## Actors

- A1. **Caller** — the person who called the Sales line and receives any approved SMS follow-up.
- A2. **Operator** — the human reviewing the Telegram approval card before any SMS is sent.
- A3. **Dialpad webhook service** — the standalone service that normalizes Dialpad events, creates approval drafts, and forwards OpenClaw hook context.
- A4. **Context sources** — Attio, calendar context, and QMD knowledge lookup.

---

## Key Flows

- F1. Missed call with usable CRM context
  - **Trigger:** A Sales line missed call arrives.
  - **Actors:** A1, A2, A3, A4
  - **Steps:** The webhook identifies the event as a Sales missed call, attempts enrichment, receives usable CRM context, creates an approval draft, and renders operator-facing provenance in Telegram.
  - **Outcome:** The operator sees a CRM-aware missed-call draft and can approve, edit, or reject it.

- F2. Low-confidence missed call with a context match
  - **Trigger:** A missed call includes only payload-level identity or a weak local match.
  - **Actors:** A1, A2, A3, A4
  - **Steps:** The webhook attempts enrichment, shows any usable match to the operator, and keeps customer-facing copy generic unless identity confidence is high.
  - **Outcome:** The operator can use the context without risking a customer-facing PII leak.

- F3. Missed call with no usable enrichment
  - **Trigger:** A Sales missed call arrives and context sources are disabled, unavailable, unsafe, or return no match.
  - **Actors:** A1, A2, A3, A4
  - **Steps:** The webhook creates the existing generic missed-call approval draft and includes source status in the Telegram handoff.
  - **Outcome:** The operator sees that the generic response was a fallback, not an unexplained first choice.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R9, R10.** Given a Sales missed call from a caller with usable Attio context, when the approval draft is created, then the draft basis is CRM-aware and Telegram shows Attio provenance.
- AE2. **Covers R4, R6, R10, R13.** Given a low-confidence missed call with an Attio match, when the approval draft is created, then Telegram may show the match but the customer-facing SMS text does not include the low-confidence name or company.
- AE3. **Covers R5, R8, R11, R14.** Given a Sales missed call where enrichment sources are unusable, when the approval draft is created, then the generic missed-call copy is used and Telegram shows why enrichment did not supply the draft.
- AE4. **Covers R7, R8.** Given a high-confidence known caller with fresh context, when the missed-call draft is created, then customer-facing text may use safe personalization and remains written as a missed-call follow-up.

---

## Success Criteria

- A missed call from a caller with known Attio context produces an operator draft with useful CRM context instead of the generic missed-call text.
- A low-confidence missed call can show useful operator-facing context without leaking low-confidence identity data to the caller.
- A truly cold missed call still gets the existing generic fallback draft.
- Telegram makes the draft basis and source status clear enough that the operator can tell whether the draft is enriched or generic.

---

## Scope Boundaries

- No unattended auto-send expansion; the change is for approval drafts only.
- No full phone-first identity resolver rewrite.
- No new source cascade beyond the existing Attio, calendar, and QMD enrichment contracts.
- No change to non-Sales missed-call behavior.

---

## Dependencies / Assumptions

- The existing enrichment adapter contracts remain the source boundary for Attio, calendar, and QMD context.
- The SMS low-confidence PII rule applies unchanged to missed-call drafts.
- Existing missed-call dedupe and approval-draft storage remain responsible for preventing duplicate operator-visible drafts.

---

## Sources / Research

- `scripts/webhook_server.py` — missed-call draft eligibility, enrichment gates, draft text selection, and Telegram basis rendering.
- `tests/test_ungate_provenance.py` — current SMS-only enrichment gate assertions.
- `tests/test_webhook_server.py` — current missed-call draft tests for known, stale, and payload-only callers.
- `docs/brainstorms/2026-06-18-s1-wire-enrichment-paths-requirements.md` — prior SMS enrichment requirements.
- `docs/solutions/ungate-enrichment-customer-pii.md` — low-confidence enrichment and PII safety rule.
- `docs/reference/enrichment-adapters.md` — context adapter contracts.
