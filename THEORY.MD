# Theory: Dialpad Skill as a Reliable Communications Control Plane

## Problem
The Dialpad skill is not just an API wrapper. It is the control plane that decides which communication events are safe to fan out, how they are enriched, and how they remain observable when dependencies fail. The core failure mode is boundary drift: when two outbound paths consume the same inbound event but enforce policy differently, sensitive data can leak even when one path appears protected.

In practical terms, the highest-risk boundary is inbound SMS fan-out. The system stores all messages for auditability, but notification channels (OpenClaw hooks and Telegram alerts) must gate sensitive content consistently. If policy is duplicated or implicit, regressions appear as bypasses rather than explicit failures.

## Operating Theory
The skill should operate as a layered system with one policy source per boundary:

- Execution layer: generated OpenAPI CLI for complete Dialpad API access.
- Safety/UX layer: wrappers in `bin/` for predictable operator behavior.
- Event ingress layer: `scripts/webhook_server.py` normalizes inbound payloads and applies policy before fan-out.
- Persistence layer: SQLite/FTS stores full event history regardless of downstream notification outcomes.

For inbound SMS, the correct model is: one event -> one eligibility decision -> many outputs. Hook forwarding and Telegram alerting are downstream effects of a shared decision, not independent checks.

## Strategy
The strategy is to harden boundaries where divergence risk is highest while preserving graceful service behavior:

1. Keep webhook continuity strict: storage and response semantics remain non-blocking with always-200 success for valid authenticated events.
2. Centralize high-risk policy checks: sensitive-content and shortcode filtering must be decided once and reused.
3. Make decisions observable with safe reason codes instead of payload dumps.
4. Preserve enrichment and routing determinism so sender context is resolved once per event and reused.
5. Maintain two-speed ergonomics: safe defaults for daily use, full API/tooling depth for advanced operations.

## Key Discoveries
The previous assumption was that a shared boolean (`sensitive_filtered`) was enough to coordinate hook and Telegram behavior. The flaw is that implicit coupling is brittle: policy intent was spread across separate branches, making bypasses easier to introduce during routine changes.

Current understanding:

- Silent fallback hides operational debt; explicit degraded-state signaling is mandatory.
- `401` is not a single diagnosis; remediation depends on classification (expired token vs scope vs environment/audience mismatch).
- Inbound paths must share one enrichment result per event to avoid inconsistent outputs and duplicated failure surfaces.
- Sparse/inconsistent webhook payloads are common enough that missed-call handling must be multi-stage (`payload_direct` → `payload_inferred` → `history_backfill`), with explicit unresolved semantics.
- History backfill must be evidence-gated: inbound+missed-like is necessary but not sufficient; at least one normalized caller/line match should exist before enriching unresolved fields.
- Missing or unparsable duration should remain unknown (`None`) rather than coerced to zero, otherwise answered/ambiguous rows can be misclassified as missed-like.
- Inbound fan-out policy must be explicit and centrally computed (eligibility helper with reason code).
- OTP/2FA and shortcode suppression are policy decisions, not channel-specific behaviors.
- Observability should report policy outcomes (`eligible`, `filtered_sensitive`, `filtered_shortcode`, etc.) without leaking message secrets.
- Preserving graceful behavior (webhook continuity) and increasing observability are compatible if failure semantics are structured.

## Open Questions
- Should inbound policy reasons be exported as metrics counters for long-term drift detection?
- Should policy become configurable per destination channel, or remain globally consistent for all notification sinks?
- Should similar centralized policy gating be applied to missed-call and voicemail Telegram paths for uniform audit semantics?

## Scope Alignment with SKILL.md
SKILL.md promises safe operational workflows around Dialpad messaging and calls. This theory tightens that promise by treating inbound policy as a first-class invariant: every fan-out target must honor the same eligibility decision, and every decision must be explainable without exposing secrets.
