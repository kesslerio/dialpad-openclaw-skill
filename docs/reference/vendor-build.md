# vendor/ — pinned managed dependencies for the generated Dialpad CLI

`vendor/` is this repo's deterministic managed environment for
`generated/dialpad.openapi`: the wrappers put it on `PYTHONPATH`
(`bin/_dialpad_compat._env_with_auth()` / `generated/dialpad._auth_env()`), so
`click` and `requests` always import from there — never from an ambient system
python, a `PATH` lookup, or a `uv` binary.

**`vendor/` itself is deliberately not tracked in git.** Git tracks the build
inputs instead: the six pinned packages with `sha256` hashes in
[`requirements.txt`](../../requirements.txt), plus this document. The tree is
constructed by `scripts/build_vendor.py` during runtime-copy delivery
(`docs/reference/runtime-copies.md`), and by test runs in dev checkouts.

Why: third-party code belongs in the review as a reviewed pin list, not as a
52k-line snapshot nobody can diff; Dependabot and other tooling read a standard
requirements file; and one build path means every copy is reconstructed from
the reviewed pins instead of drifting from a committed snapshot. The deployed
container still receives the identical self-contained offline tree — it never
runs `uv`, `pip`, or any network install (#89, #155).

## Contents

| Package | Version | License | Role |
| --- | --- | --- | --- |
| `click` | 8.1.8 | BSD-3-Clause | CLI framework of the generated CLI (hard import) |
| `requests` | 2.32.4 | Apache-2.0 | HTTP client of the generated CLI (hard import) |
| `urllib3` | 2.2.3 | MIT | requests dependency |
| `idna` | 3.10 | BSD-3-Clause | requests dependency |
| `certifi` | 2026.7.22 | MPL-2.0 | CA bundle for TLS |
| `charset-normalizer` | 3.4.1 | MIT | requests dependency (pure-Python fallback used) |

Licenses ship inside each `<package>-<version>.dist-info/` directory of the
built tree. `rich` is intentionally **not** included: the generated CLI guards
it behind `RICH_AVAILABLE` and falls back to JSON output for `--output table`
(the only mode that uses it), and every wrapper speaks JSON.

## Building

From the repo root, on a machine with network and tooling:

```bash
python3 scripts/build_vendor.py                 # -> <repo>/vendor (dev checkout)
python3 scripts/build_vendor.py <copy>/vendor   # delivery step for a runtime copy
```

The script installs from `requirements.txt` with `--require-hashes` (using `uv`
when present, `pip` otherwise), strips installer noise (console scripts,
`*.so`, `*.pyd`, `*.pyc`, `__pycache__/`, `.lock`), and fails unless `click`
and `requests` import **from the built tree** (path-asserted, bytecode writes
disabled). Build on linux x86_64 to match the gateway/grokbot runtimes.

## Verifying

The build contract is pinned by `tests/test_send_sms_dependency_fallback.py`:

- the pin list is exactly these six packages, each with `sha256` hashes, and
  the versions above match this document;
- the constructed tree runs the generated CLI on a click-less interpreter
  (`--help` exits 0), while the control without the tree fails with
  `ModuleNotFoundError: No module named 'click'`;
- two reconstructions are byte-identical (`diff -r` clean).

## Upgrading a pin

1. Edit the pin in `requirements.txt` and regenerate the hash block (command in
   that file's header); review the hash diff.
2. Update the version table above.
3. Rebuild (`python3 scripts/build_vendor.py`) and run the suite:
   `env -u DIALPAD_WEBHOOK_SECRET uv run --with pytest python -m pytest tests/ -q`.
4. The runtime copies pick the new tree up on their next delivery step.

## Constraints

- **Pure Python only** — compiled extensions and bytecode are stripped, so the
  tree works under any CPython ≥ 3.8 (verified on 3.11, the deployed gateway
  interpreter, plus 3.12 and 3.14).
- **Pinned exactly** — never install into `vendor/` by hand; always go through
  `scripts/build_vendor.py` so stripping and verification apply.
- Never symlink individual packages out of `vendor/`; the `PYTHONPATH`
  contract expects the package directories to live here.
