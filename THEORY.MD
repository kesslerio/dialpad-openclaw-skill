# Theory: Dialpad Skill as a Reliable Communications Control Plane

## Problem
The Dialpad skill exists to turn OpenClaw from a chat assistant into an operational communications endpoint for real-world workflows. The broad problem is not “send an SMS” or “make a call.” The real problem is enabling automated and human-in-the-loop outreach that is deterministic, auditable, and safe across multiple channels and business contexts.

Without this skill, phone/SMS actions are fragmented across ad-hoc scripts, inconsistent sender selection, and weak observability. That creates five recurring failure classes:

1. **Delivery ambiguity** — messages may send from the wrong line or profile.
2. **Identity loss** — inbound events without contact context reduce triage quality.
3. **Auth drift fragility** — token/scope/env issues degrade behavior without actionable signals.
4. **State blindness** — no durable local query layer for “what happened?” after sends/receives.
5. **Tooling mismatch** — high-level workflow usage and low-level API control are often disconnected.

The Dialpad skill is intended to solve those classes systematically, not just provide thin endpoint wrappers.

## Operating Theory
The skill should be treated as a **communications control plane** with layered responsibilities:

- **Execution layer**: generated OpenAPI CLI for full Dialpad API surface.
- **Safety/UX layer**: `bin/*.py` wrappers enforce sane defaults, auth bridging, sender resolution, and predictable operator behavior.
- **Event ingress layer**: webhook server normalizes inbound traffic and routes it into OpenClaw hooks/Telegram while preserving graceful degradation.
- **Persistence/inspection layer**: SQLite + FTS enables local history, debugging, and retrospective analysis.

In this model, success is measured by **reliable outcomes under imperfect conditions**: stale tokens, partial contact data, malformed payloads, and operational drift. The system must continue delivering messages/events while making degradations visible and diagnosable.

## Strategy
The long-term strategy is to harden each layer while preserving operator ergonomics:

1. **Deterministic sending behavior**
   - explicit E.164 handling
   - clear sender/profile resolution rules
   - dry-run and mismatch guardrails

2. **Auth as a first-class boundary**
   - canonical key source (`DIALPAD_API_KEY`) with controlled bridges
   - explicit classification of auth failures where possible
   - no secret leakage in logs

3. **Inbound normalization with continuity guarantees**
   - always-200 webhook semantics for resilience
   - robust filtering/classification for notification quality
   - hook payloads that preserve context for downstream automations

4. **Progressive observability**
   - structured status fields for degraded modes
   - durable local storage for message/search/debug loops
   - concise operator-facing release notes to reduce hidden behavior changes

5. **Two-speed interface contract**
   - wrappers for routine usage and safer defaults
   - generated CLI for advanced and complete API access

This keeps the skill useful to both daily operators and deep-debug maintainers.

## Key Discoveries
Recent hardening cycles reinforced a broader point: communication systems fail more often at **boundaries** (auth, identity resolution, endpoint assumptions) than at transport.

Concrete discoveries from this cycle:

- Silent fallback hides operational debt; explicit degraded-state signaling is mandatory.
- `401` is not a single diagnosis; remediation depends on classification (expired token vs scope vs environment/audience mismatch).
- Inbound paths must share one enrichment result per event to avoid inconsistent outputs and duplicated failure surfaces.
- Sparse/inconsistent webhook payloads are common enough that missed-call handling must be multi-stage (`payload_direct` → `payload_inferred` → `history_backfill`), with explicit unresolved semantics.
- Preserving graceful behavior (webhook continuity) and increasing observability are compatible if failure semantics are structured.

## Open Questions
- Should auth classification expand to explicit `403` handling when Dialpad scope/policy denials surface outside `401`?
- Should enrichment degradation emit standardized counters/metrics beyond logs/response fields for fleet-level monitoring?
- How far should the skill go toward multi-tenant line policy enforcement vs leaving policy to calling workflows?
- Should release notes evolve into versioned migration guidance as wrapper behavior becomes more opinionated?

## Scope Alignment with SKILL.md
SKILL.md defines the user-facing contract: send SMS/calls, manage contacts, query history, and use safer wrappers with canonical auth/source-of-truth conventions. This theory document extends that contract into system intent:

- **Why** those features exist together (communications control plane)
- **How** reliability and safety are maintained under drift
- **Where** current hardening work (like auth classification and missed-call context backfill) fits the larger architecture

The intent is that individual fixes should always strengthen this broader control-plane model, not become isolated patches.
