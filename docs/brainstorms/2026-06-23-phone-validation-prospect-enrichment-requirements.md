---
date: 2026-06-23
topic: phone-validation-prospect-enrichment
---

# Requirements — Phone validation and prospect enrichment for Dialpad inbound drafts

## Summary

Unknown Sales callers should get a shared phone-intelligence pass before SMS or missed-call drafts fall back to generic copy. The webhook should validate the number, expose caller-risk and reverse-lookup facts to the operator, optionally search public business context, and let the draft model use only compact tool facts when producing approval-gated replies.

---

## Problem Frame

A low-confidence Sales missed call from a valid active wireless number currently appears as only the raw phone number. That protects against false personalization, but it also hides useful triage facts: whether the number is valid, active, likely mobile or VOIP, associated with spam, and whether a reverse name plus location can point to a business owner or prospect.

IPQualityScore can provide phone validation, carrier, line type, active-line, risk, location, and reverse-name signals. Those signals are useful for operator triage, but they are not strong enough by themselves to make a customer-facing identity claim. Public web search can add business-prospect clues, but weak or personal-only results must not become draft personalization.

---

## Key Decisions

- **Use a shared caller-intelligence layer.** SMS and missed-call webhooks should call the same logic so phone validation, risk labels, public-search context, and model facts stay consistent.
- **Treat IPQS as operator evidence, not identity authority.** A reverse name can help the operator understand the caller, but it must not make identity confidence high by itself.
- **Search for business relevance only after phone validation.** Web search should be bounded to validated, active numbers with usable reverse-name or location facts, and should look for public business/professional matches rather than personal background details.
- **Keep model drafting fact-grounded and approval-gated.** The model should draft from compact tool facts and deterministic fallback text; it must fail closed when facts are weak, unsafe, or unsupported.
- **Sync Dialpad contacts only after the identity bar is met.** Enrichment should update or create Dialpad contact records automatically when the evidence is clear, but ambiguous reverse lookup or weak public search should only produce an operator-visible suggestion.

---

## Requirements

**Phone intelligence**

- R1. Inbound Sales SMS and Sales missed-call events must attempt shared phone validation when Dialpad contact lookup and CRM lookup do not produce high-confidence identity.
- R2. Phone validation must surface compact operator-facing facts for validity, active-line status, country, city, region, carrier, line type, reverse name, and abusive/fraud status when the provider returns them; missing active-line fields must remain unknown, not inactive.
- R3. Phone validation status must distinguish `usable`, `not_configured`, `not_found`, `invalid`, `inactive`, `risky`, `unavailable`, `timeout`, `budget_exceeded`, `rate_limited`, and `unsafe_output`.
- R4. Invalid, inactive, or abusive-number signals must be visible in Telegram and draft metadata before any operator approves a reply.
- R5. Phone validation must fail closed to the existing generic or CRM-aware behavior when the provider is missing, slow, or unavailable.

**Identity and prospect context**

- R6. Reverse-name or location facts from phone validation must not raise customer-facing identity confidence to high without an exact CRM/Dialpad match or other strong owned-source evidence.
- R7. When phone validation returns a reverse name and location for a valid, not-inactive, low-risk number, the system may run a bounded public web search for business/professional context.
- R8. Public-search context must be summarized as operator-only prospect evidence only when bounded evidence ties the reverse name plus location to a business, role, or organization relevant to ShapeScale for Business.
- R9. Public-search context must avoid sensitive personal details and must not store raw search-result pages or long snippets in approval metadata.
- R10. The Telegram card must distinguish "possible caller identity" from "confirmed contact identity" so operators do not confuse reverse lookup with CRM identity.

**Draft behavior**

- R11. The customer-facing draft must remain generic when only phone validation facts are available.
- R12. The draft model may use phone validation and public-search facts to choose tone and context, but it must not greet by reverse name or mention a business unless the final facts meet the same safety bar as other high-confidence personalization.
- R13. For valid, not-inactive, low-risk unknown Sales callers, the generic fallback should remain helpful and human: acknowledge the missed call or inbound SMS and ask how Sales can help.
- R14. For high-risk, inactive, or invalid numbers, the system must withhold generated customer-facing drafts and route the event to human-only handling.
- R15. Model-generated drafts must be rejected when they introduce unsupported identity claims, unsupported business claims, raw internal source names, unapproved links, or sensitive personal details.

**Operations and cost control**

- R16. The IPQS API key must be loaded from secrets, not committed configuration; the observed 1Password item is `IPQUALITYSCORE IPQS API Key`.
- R17. Phone validation and public search must run only after the relevant idempotency claim and webhook ACK; short timeouts and bounded retries must keep Telegram delivery from being blocked by the provider.
- R18. Results should be cached by normalized phone number for a bounded TTL so repeated webhook events do not burn unnecessary validation credits, with different TTLs for phone validation and public search.
- R19. Public web search must be optional, bounded, budgeted, and separately statused from IPQS so a search failure or budget cap does not erase phone-validation facts.
- R20. SMS and missed-call tests must cover the same shared helper contract rather than duplicating separate source logic.

**Dialpad contact sync**

- R21. When enrichment produces high-confidence owned-source identity or public business/professional evidence that directly corroborates the validated phone number, the system should automatically create or update the Dialpad contact using the existing contact wrappers.
- R22. Automatic contact sync must never use IPQS reverse name alone, area code, weak public search, or same-name personal results as the basis for a contact name, company, or merge.
- R23. Automatic updates must fill missing non-sensitive fields or append confirmed identifiers by merging current contact values before writeback, not overwrite populated Dialpad fields with lower-confidence data.
- R24. Ambiguous, conflicting, risky, inactive, invalid, or budget-degraded enrichment must produce an operator-visible contact-sync suggestion or warning instead of a writeback.

---

## Actors

- A1. **Caller** — the person or business contacting the Sales line.
- A2. **Operator** — the human reviewing Telegram context and approving or rejecting SMS drafts.
- A3. **Dialpad webhook service** — the service normalizing inbound SMS and missed-call events, enriching context, creating approval drafts, and forwarding OpenClaw hook payloads.
- A4. **Phone intelligence provider** — IPQualityScore phone validation.
- A5. **Public-search source** — bounded web search used only for business/prospect clues.
- A6. **Draft model** — a cheap configured model that can rewrite approval drafts from compact facts but cannot send.
- A7. **Dialpad contact sync** — existing create/update wrappers used only after identity and ambiguity gates pass.

---

## Key Flows

- F1. Unknown valid Sales missed call
  - **Trigger:** A Sales missed call arrives from a number with no CRM or Dialpad exact match.
  - **Actors:** A1, A2, A3, A4, A5, A6
  - **Steps:** The webhook validates the number, surfaces valid/active/carrier/location/reverse-name facts, runs bounded business-context search when eligible, and creates an approval draft from deterministic fallback plus compact facts.
  - **Outcome:** The operator sees why the caller may be a real prospect while the customer-facing text stays safe unless identity is confirmed.

- F2. Unknown inbound Sales SMS
  - **Trigger:** A first-contact Sales SMS arrives from an unknown number.
  - **Actors:** A1, A2, A3, A4, A5, A6, A7
  - **Steps:** The webhook uses the same phone-intelligence helper as missed calls, adds phone and prospect facts to the operator context, lets the draft model improve the generic reply only within the fact boundary, and runs contact sync only if the identity/writeback bar is met.
  - **Outcome:** SMS and missed-call cards expose the same source statuses and safety semantics, and clear matches can keep Dialpad contacts current without manual data entry.

- F3. Risky or invalid caller
  - **Trigger:** Phone validation says the number is invalid, inactive, VOIP/disposable with high risk, or reported abusive.
  - **Actors:** A1, A2, A3, A4
  - **Steps:** The webhook marks the risk in Telegram, avoids unsupported personalization, and withholds generated customer-facing drafts.
  - **Outcome:** The operator can still act manually, but automation does not present a risky reply as routine.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R10, R11.** Given an unknown Sales missed call from a valid active wireless number with a reverse name, when the draft is created, then Telegram shows the phone intelligence and the customer-facing draft does not greet by that reverse name.
- AE2. **Covers R7, R8, R12.** Given phone validation returns a reverse name and Fort Worth location, when bounded public search finds a strong business/professional match, then Telegram may show a possible prospect summary and the model may use it only if the identity safety bar is met.
- AE3. **Covers R3, R4, R14.** Given IPQS marks a number invalid, inactive, or abusive, when the webhook processes the event, then the Telegram card shows the risk status and no generated customer-facing draft is created.
- AE4. **Covers R5, R17, R19.** Given IPQS times out and web search is unavailable, when a Sales SMS arrives, then the webhook still ACKs, creates the existing safe fallback when eligible, and records phone intelligence as unavailable.
- AE5. **Covers R20.** Given equivalent unknown caller facts for SMS and missed-call events, when tests run, then both paths use the same helper output and render consistent source statuses.
- AE6. **Covers R21-R24.** Given a validated active number and a single unambiguous owned-source or phone-corroborated public-business match, when enrichment completes, then Dialpad contact sync fills missing confirmed fields; given only reverse-name/location or conflicting evidence, then no writeback occurs and Telegram shows a suggested contact update.

---

## Success Criteria

- Unknown Sales callers no longer appear as raw phone numbers when IPQS can provide safe validation context.
- Operators can distinguish valid active prospects from invalid, inactive, VOIP/disposable, or abuse-reported numbers before approving a reply or handling a high-risk caller manually.
- Customer-facing drafts do not leak reverse-lookup names or public-search guesses.
- SMS and missed-call webhook paths share source semantics, status labels, and model-fact boundaries.
- Provider outages do not break webhook ACKs, Telegram notifications, or existing safe fallback drafts.
- Clear enriched identities can update Dialpad contacts automatically, while ambiguous or risky matches stay human-reviewed.

---

## Scope Boundaries

- No autonomous SMS sending expansion.
- No phone-number identity resolver that treats IPQS or web search as equivalent to CRM/Dialpad identity.
- No voicemail support in the first version.
- No generated customer-facing draft for high-risk, inactive, or invalid callers in the first version.
- No automatic contact overwrite or merge from reverse-name-only, same-name, area-code, or weak public-search evidence.
- No storage of raw public-search pages, sensitive personal details, or full IPQS payloads in draft metadata.
- No bulk lead enrichment outside inbound webhook-triggered Sales events.

---

## Dependencies / Assumptions

- The IPQS secret exists in 1Password as `IPQUALITYSCORE IPQS API Key` and should be exposed to the runtime through an environment variable.
- IPQS phone validation returns enough compact fields to support validity, active-line, carrier, line type, location, reverse-name, and fraud/abuse status.
- Public web search may produce weak or personal-only matches; weak matches are useful operator clues at most and should not affect customer-facing personalization.
- Existing low-confidence draft guardrails from the model-draft layer remain the safety boundary for names, companies, links, and unsupported claims.
- Existing `bin/create_contact.py` and `bin/update_contact.py` wrappers remain the supported Dialpad writeback surface.

---

## Sources / Research

- `scripts/webhook_server.py` — shared inbound context, SMS webhook processing, missed-call webhook processing, approval-draft creation, and Telegram rendering.
- `scripts/draft_model.py` — compact model fact boundary and draft rejection rules.
- `bin/create_contact.py` and `bin/update_contact.py` — existing Dialpad contact writeback wrappers.
- `docs/ideation/2026-03-26-dialpad-contact-merge-ambiguity-ideation.md` — ambiguity and writeback guardrails.
- `docs/brainstorms/2026-06-22-missed-call-enrichment-requirements.md` — missed-call enrichment source-status and PII safety requirements.
- `docs/brainstorms/2026-06-23-calendar-aware-demo-context-requirements.md` — compact tool facts and model-draft safety requirements.
- IPQualityScore Phone Number Validation API documentation: `https://www.ipqualityscore.com/documentation/phone-number-validation-api/overview`
- IPQualityScore advanced options documentation: `https://www.ipqualityscore.com/documentation/phone-number-validation-api/advanced-options`
