---
module: scripts/webhook_server.py
problem_type: pii-safety, enrichment, draft-generation
tags: [un-gate, attio, customer-pii, identity-confidence, provenance, dialpad-draft]
related_pr: "#94"
---

# Un-gating CRM enrichment into customer-facing drafts: the low-confidence PII trap

Learnings from PR3/U7 (un-gate the CRM/QMD/calendar enrichment so it runs for every
sales-line SMS draft, not just high-confidence known contacts).

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
