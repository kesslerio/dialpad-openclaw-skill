# Theory: Dialpad Skill as a Reliable Communications Control Plane

## Problem
The Dialpad skill is not just an API wrapper. It is the control plane that decides how communication actions are executed, how inbound events are filtered and enriched, and how operators avoid making expensive mistakes at the wrapper boundary.

The recurring failure mode is boundary drift. Sometimes that drift is between two downstream notification paths that should share one policy decision. Sometimes it is between a human-friendly wrapper and the generated CLI it drives. And sometimes it is between operator intent and the shell that mutates command text before Python ever starts.

Those are different failure shapes, but the same class of bug: the system boundary that looked “obvious” was not the real boundary that enforced behavior.

Recent work exposed two especially important boundaries:

1. **Inbound SMS fan-out policy** — one event must produce one eligibility decision that every downstream sink reuses.
2. **Outbound SMS operator input** — one intended message must survive shell parsing and wrapper handling without silent corruption.

The outbound issue is deceptively stupid but high impact. A message like `It's $499 typically.` can become `It's 99 typically.` before the wrapper runs if the operator uses double quotes and the shell expands `$4`. Once that happens, the wrapper cannot reconstruct intent. So the correct fix is not magical detection. It is changing the operator boundary so the safe path is explicit and inspectable.

## Operating Theory
The skill should operate as a layered system with one policy or contract source per boundary:

- **Execution layer**: generated OpenAPI CLI for complete Dialpad API access.
- **Safety/UX layer**: wrappers in `bin/` for predictable operator behavior, auth bridging, sender resolution, and safe input handling.
- **Event ingress layer**: `scripts/webhook_server.py` normalizes inbound payloads and applies policy before fan-out.
- **Persistence layer**: SQLite/FTS stores full event history regardless of downstream notification outcomes.

For inbound SMS, the correct model is: one event → one eligibility decision → many outputs. Hook forwarding and Telegram alerting are downstream effects of a shared decision, not independent checks.

For outbound SMS, the correct model is: one intended message → one trusted input path → one visible preflight preview. Inline shell arguments are acceptable for trivial text, but shell-sensitive content needs an input path that bypasses interpolation (`--message-file` or `--message-stdin`) and a dry-run preview that shows the exact payload, not a fake comfort metric like message length.

## Strategy
The strategy is to harden boundaries where divergence or silent mutation risk is highest while preserving graceful service behavior:

1. Keep webhook continuity strict: storage and response semantics remain non-blocking with always-200 success for valid authenticated events.
2. Centralize high-risk inbound policy checks: sensitive-content and shortcode filtering must be decided once and reused.
3. Make decisions observable with safe reason codes instead of payload dumps.
4. Preserve enrichment and routing determinism so sender context is resolved once per event and reused.
5. Maintain two-speed ergonomics: safe defaults for daily use, full API/tooling depth for advanced operations.
6. Treat shell boundaries as real system boundaries: if the shell can corrupt operator intent before the wrapper runs, the wrapper must offer non-shell-sensitive input modes and an exact preview path.

## Key Discoveries
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
- Wrapper failures can be local parser failures masquerading as upstream API problems; reproductions must separate generated CLI parse-time validation from network/API behavior.
- Outbound shell expansion is not something the wrapper can “fix after the fact.” If `$499` becomes `99` in the shell, the wrapper only sees broken input. Prevention must happen at the input interface.
- Plain dry-run output that hides the actual message body is operationally dishonest for shell-sensitive content. Exact preview beats approximate reassurance.
- Safer input modes (`--message-file`, `--message-stdin`) are a better control than heuristic price-correction guesses.

## Open Questions
- Should similar safe input modes be added to other free-text wrappers like `bin/send_group_intro.py`?
- Should shell-sensitive examples be systematically audited across every wrapper and legacy script, not just SMS?
- Should wrapper dry-runs ever redact message content by default in some environments, or is explicit dry-run enough consent to show exact text?
- Should inbound policy reasons be exported as metrics counters for long-term drift detection?
- Should policy become configurable per destination channel, or remain globally consistent for all notification sinks?
- Should similar centralized policy gating be applied to missed-call and voicemail Telegram paths for uniform audit semantics?

## Scope Alignment with SKILL.md
SKILL.md promises safe operational workflows around Dialpad messaging and calls. That promise only holds if the wrappers are safer than raw shell + generated CLI usage, not just more convenient.

This theory therefore treats two invariants as first-class:

- every inbound notification sink must honor the same eligibility decision
- every outbound message path must preserve operator intent across shell and wrapper boundaries, or make mutation obvious before send

If a change strengthens those invariants, it belongs here. If it only adds convenience while keeping silent corruption possible, it is not enough.
