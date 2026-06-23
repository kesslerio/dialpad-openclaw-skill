---
module: scripts/webhook_server.py
problem_type: pii-safety, enrichment, draft-generation
tags: [un-gate, attio, customer-pii, identity-confidence, provenance, dialpad-draft]
related_pr: "#94"
---

# Un-gating CRM enrichment into customer-facing drafts: the low-confidence PII trap

Learnings from PR3/U7 and the missed-call enrichment extension (un-gate the
CRM/QMD/calendar enrichment so it runs for operator-approved sales-line drafts,
not just high-confidence known SMS contacts).

## The trap: low-confidence Attio matches in customer-facing text

The Attio adapter resolves a sender **by phone number alone**. At low/medium identity
confidence (a reused/ported/shared number, a stale Attio record), it can match the
**wrong** person/deal. The danger is not the enrichment running — it's where the
matched data lands:

- **Operator-facing** (provenance line, draft metadata, the Telegram approval card):
  safe at any confidence. The operator sees "Attio: WrongCo · stage: X" and can judge.
- **Customer-facing** (`draft_text` the customer would receive): a wrong company name
  or first name in the SMS is a **customer-to-customer PII disclosure**.

The first cut un-gated everything, so `_crm_reply_message` embedded the matched
company directly in `draft_text` ("...your ShapeScale conversation with **{company}**
here..."). Adversarial review caught the company; Codex then caught the **greeting
name** (`Hi {wrong-name},`) on the same principle.

## The rule

When un-gating enrichment into a draft that has a customer-facing component, gate the
**customer-facing USE** of matched data on high confidence; keep the **operator-facing**
surfacing un-gated:

- Company name in `draft_text`: only at `identityConfidence == "high"`.
- Greeting name in `draft_text`: suppressed at `identityConfidence == "low"` (matches
  the pre-existing generic fallback) via `_draft_greeting`.
- Provenance line: any confidence, operator-only, never in `draft_text`.
- Missed-call drafts follow the same rule. They may show low-confidence Attio,
  calendar, or QMD context to the operator, but low-confidence customer text stays
  generic and call-specific (`Hi there, sorry we missed your call...`).

## This was a CP4 (product) decision

"How aggressively to put low-confidence Attio data into customer-facing drafts" is a
risk-tolerance call the agent should not assume — it was surfaced to the maintainer,
who chose **operator-only at low confidence**. The plan's "operator catches wrong
matches via provenance" intent only holds if the wrong data is operator-facing, not
embedded in a naturally-worded customer draft.

## Safety basis that made it tractable

There is no unattended auto-send (`send_proactive_reply` has zero callers — every
inbound auto-reply is an operator-approval draft). A `NoUnattendedSendInvariantTests`
test fails if that ever changes, so a future S4 auto-send can't silently turn the
un-gate into autonomous customer-PII disclosure without re-gating first.

## Provenance must label sources accurately

Operators use the provenance line to validate sources, so a mislabel is worse than no
label. Whitelist exact bases (`shapescale_knowledge` → "QMD knowledge",
`recent_thread_link` → "Prior-thread link") rather than blacklisting CRM/calendar —
a blacklist mislabels everything else.

For generic missed-call fallbacks, source status is part of the safety story:
`not_applicable` QMD for silent calls is different from a failed knowledge lookup,
and `unsafe` CRM/calendar output is different from no match. Keep those statuses
operator-facing and out of customer SMS text.

## Phone intelligence follows the same boundary

IPQS reverse lookup and public prospect search are weaker than Attio/Dialpad
owned-source identity. Treat them as operator evidence unless independently
corroborated:

- Reverse name may be shown as "possible reverse lookup" in Telegram/metadata.
- Reverse name must not raise `identityConfidence` or produce `Hi {name}`.
- Public business evidence must stay low-confidence operator context unless it is
  directly phone-corroborated or backed by owned-source identity.
- Invalid, inactive, disposable/temporary, abusive, or high-risk phone signals
  block customer-facing drafts entirely and route the event human-only.
- Automatic Dialpad contact sync must never use reverse-name-only, same-name
  personal search, area code, weak public search, or budget-degraded evidence as
  contact identity.
