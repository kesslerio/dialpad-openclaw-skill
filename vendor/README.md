# vendor/ — managed dependencies for the generated Dialpad CLI

This tree is this repo's **deterministic managed environment** for
`generated/dialpad.openapi`. The wrappers put it on `PYTHONPATH`
(`_dialpad_compat._env_with_auth()` / `generated/dialpad._auth_env()`), so
`click` and `requests` always import from here — never from an ambient system
python, a `PATH` lookup, or a `uv` binary.

Why it exists: the deployed gateway runtime resolves the wrapper outside any
login shell (agent exec / systemd / cron), has **no `uv`** anywhere the old
`_find_uv()` discovery looked, and its system python cannot `import click`. The
previous `uv run --with ...` fix (#89) silently degraded to the bare CLI path
there and every send fell back to the direct Dialpad API (#155). A vendored
tree ships with the skill and needs no network, tool, or interpreter feature
beyond CPython ≥ 3.8.

## Contents

| Package | Version | License | Role |
| --- | --- | --- | --- |
| click | 8.1.8 | BSD-3-Clause | CLI framework of the generated CLI (hard import) |
| requests | 2.32.4 | Apache-2.0 | HTTP client of the generated CLI (hard import) |
| urllib3 | 2.2.3 | MIT | requests dependency |
| idna | 3.10 | BSD-3-Clause | requests dependency |
| certifi | 2026.7.22 | MPL-2.0 | CA bundle for TLS |
| charset-normalizer | 3.4.1 | MIT | requests dependency (pure-Python fallback used) |

Licenses ship inside each `<package>-<version>.dist-info/` directory.

`rich` is intentionally **not** vendored: the generated CLI guards it behind
`RICH_AVAILABLE` and falls back to JSON output for `--output table` (the only
mode that uses it), and every wrapper speaks JSON. Adding it back means ~6.5 MB
of `pygments`/`rich` for cosmetic tables only.

## Constraints

- **Pure Python only.** Compiled extensions (`*.so`), console scripts
  (`bin/`), `__pycache__/`, and `.pyc` files are stripped after install (and
  `*.so` is gitignored), so the tree works under any CPython ≥ 3.8 on any
  platform — verified against 3.11 (deployed gateway container), 3.12, 3.14.
- **Pinned.** Versions above are exact; regenerate deliberately, not en route
  to an unrelated change.
- Never symlink or relocate individual packages out of this directory; the
  `PYTHONPATH` contract expects the six top-level package dirs here.

## Upgrading a package

```bash
rm -rf vendor
uv pip install --target vendor \
  click==8.1.8 requests==2.32.4 urllib3==2.2.3 idna==3.10 \
  charset-normalizer==3.4.1 certifi==<version>
rm -rf vendor/bin
find vendor \( -name "*.so" -o -name "*.pyd" \) -delete
find vendor -type d -name __pycache__ -prune -exec rm -rf {} +
find vendor -name "*.pyc" -delete
# Update the table above, then run the suite:
uv run --with pytest python -m pytest tests/ -q
```

The managed-environment regressions in
`tests/test_send_sms_dependency_fallback.py` fail if click stops resolving from
this directory.
