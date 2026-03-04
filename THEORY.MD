# Theory: Resilient Sender Enrichment Under Dialpad Auth Drift

## Problem
Inbound SMS handling depends on contact lookup to enrich sender identity before forwarding events to OpenClaw hooks and optional Telegram alerts. When Dialpad contact lookup returns `401`, sender enrichment silently degrades into weaker identifiers. The webhook still returns `200` (correct for reliability), but operators lose observability into why enrichment quality dropped. The structural issue is not transport reliability; it is missing semantic distinction between "no matching contact" and "lookup failed due to authorization state." Without that distinction, failures blend into normal unknown-sender traffic.

## Operating Theory
The enrichment pipeline has two coupled concerns: identity quality and delivery continuity. Continuity must remain best-effort and non-blocking, but identity quality should emit explicit health signals. A `401` is not one failure mode: expired tokens, missing scopes, and audience/environment mismatches lead to different remediation paths. Therefore a coarse `unauthorized` label is operationally weak. The right leverage point is the contact lookup boundary, where HTTP status and error payload hints are available and can be classified without exposing secrets.

## Strategy
Keep endpoint behavior stable and avoid broad refactors. Add a narrow classification layer around Dialpad contact lookup, return structured enrichment state, and consume that state once per inbound message so webhook and Telegram formatting remain consistent. Preserve graceful fallback to cached contact names from SQLite storage, but surface degraded enrichment explicitly in webhook response metadata and logs. This retains uptime behavior while making auth drift diagnosable.

## Key Discoveries
The inbound webhook path previously performed contact lookup more than once (hook formatting and Telegram formatting), which made behavior inconsistent under transient auth errors and wasted calls. Also, prior error logging used raw exception text and lacked explicit degraded signaling. Separating lookup result (`contact_name`, `status`, `degraded`, `degraded_reason`) from consumer formatting made it possible to keep compatibility while adding observability.

## Open Questions
Dialpad may return some scope or permission errors as `403` rather than `401` depending on token type and tenant policy. Current classification targets the concrete `401` failure family because that is the observed degradation trigger. If production telemetry shows significant non-401 auth denials, classification should expand to include explicit `403` auth semantics without conflating them with transport failures.
