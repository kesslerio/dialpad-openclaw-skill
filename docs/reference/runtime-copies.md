# Runtime copies of this skill

Three copies of `dialpad` run, and only one is a repository. They have drifted in
both directions, so **no recursive copy is safe in either direction** — copying a
tree ships one copy's staleness into another.

| Copy | Location | Kind | Role |
| --- | --- | --- | --- |
| Repo | `~/projects/skills/work/dialpad` | git | Source of truth. |
| Gateway | `~/.openclaw/skills/dialpad` on theshop | plain directory | Skill root the OpenClaw gateway loads. |
| Grok Bot | `~/.ai/skills/dialpad` on grokbot | plain directory | Runtime for SMS sends. No scheduler. |

Compare any copy with:

```bash
python3 scripts/outbox_drift_probe.py \
  --manifest references/outbox-runtime-files.txt --target <copy-root>
```

The probe compares content hashes and is restricted to the reviewed manifest; it
has no mode that walks a tree. A clean report means the manifest is clean, not
that the copy is identical to the repo.

## Delivery record — outbox self-drain (2026-09-18)

PR #154, squashed as `1b1f297`. Applied set is
`references/outbox-runtime-files.txt`: `scripts/log_outbox.py` plus the four
command wrappers `bin/send_sms.py`, `bin/list_sms_inbox.py`,
`bin/list_sms_thread.py`, `bin/list_calls.py`.

### Grok Bot — applied, verified clean

- First pass delivered 5 of 5. The review round changed only
  `scripts/log_outbox.py`, so that was the only file re-delivered.
- Backups, oldest first: `~/.dialpad/backups/outbox-self-drain-20260918T033655Z/`
  and `~/.dialpad/backups/outbox-self-drain-20260918T165007Z/`, preserving mode
  and mtime.
- After: all five content hashes match the repo. Four wrappers were untouched by
  the review round and still matched from the first pass.
- All four commands run as real scripts on both copies (`--help`, exit 0).
  Imports are not the check: the first pass shipped a wrapper that imported
  cleanly and died on invocation, and only a real run caught it.
- Outbox absent on arrival, so nothing needed draining during cutover.
  `drain_on_use()` returns a full zeroed result, and `configured_log_url()`
  resolves to `http://100.85.254.62:18887`, so the drain has a destination there
  and is not a silent no-op.

### Gateway (theshop) — applied, verified clean

Before: 5 of 5 differed. Now 5 of 5 same, probe exit 0.

An earlier note here claimed this copy predated PR #153 and would therefore take
a drain with no configured destination. That was wrong. Its
`scripts/log_api_client.py` is byte-identical to the repo's, so
`configured_log_url` and `record_message` were already present, and the
dependency argument for deferring it did not hold. Backups are under
`~/.openclaw/skills/dialpad/.backups/outbox-self-drain-20260918T165019Z/`.

## Rollback

Restore the files named above from the backup directory for the copy you are
rolling back, then re-run the probe against that root and expect differing
entries.
