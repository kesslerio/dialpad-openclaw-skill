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

## Delivery record — outbox self-drain (2026-09-17)

Branch `fix/dialpad-outbox-self-drain`, through commit `b049cc8`. Applied set is
`references/outbox-runtime-files.txt`: `scripts/log_outbox.py` plus the four
command wrappers `bin/send_sms.py`, `bin/list_sms_inbox.py`,
`bin/list_sms_thread.py`, `bin/list_calls.py`.

### Grok Bot — applied, verified clean

- Before: 5 of 5 manifest entries differed.
- Backups: `~/.dialpad/backups/outbox-self-drain-20260918T033655Z/` on grokbot,
  preserving mode and mtime.
- After: all five content hashes match the repo.
- All four commands run as real scripts (`--help`, exit 0, no error output).
- Outbox was absent on arrival, so nothing needed draining during cutover.
- `configured_log_url()` resolves to `http://100.85.254.62:18887`, so
  `drain_on_use` has a destination there and is not a silent no-op.

### Gateway (theshop) — deliberately not touched

Before: 5 of 5 differed. Left as-is, and this is a decision rather than an
oversight.

That copy predates PR #153, which added env auto-loading to
`scripts/log_api_client.py`, and `log_api_client.py` is not in this manifest.
Applying only these five files there would leave a drain that finds no
configured destination and returns a no-op — the exact silent-failure shape this
change exists to remove. Shipping a working drain there means carrying
`log_api_client.py` too, which widens the reviewed set beyond this change and
belongs in its own decision.

The risk of leaving it is smaller than it looks. The lossy read-then-replace
compaction that U2 fixed only loses data when something appends during a drain,
and that concurrency is created by drain-on-use — which this copy does not have.
A manual `log_outbox.py replay` there stays the single-threaded path it always
was.

To reverse or revisit: re-run the probe above against that root.

## Rollback

Restore Grok Bot's five files from the backup directory named above, then
re-run the probe and expect 5 differing entries.
