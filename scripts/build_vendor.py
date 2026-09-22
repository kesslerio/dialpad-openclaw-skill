#!/usr/bin/env python3
"""Construct the vendored managed-dependency tree for the generated Dialpad CLI.

Builds `vendor/` from the pinned, hash-verified `requirements.txt`, strips
installer noise (console scripts, compiled extensions, bytecode caches), and
installs the finished tree atomically. Used by the runtime-copy delivery step
(docs/reference/runtime-copies.md), by dev checkouts before running the test
suite, and by the regression tests (tests/test_send_sms_dependency_fallback.py).

Requires network plus `uv` (preferred) or `pip` on the BUILD machine. The
deployed gateway container never runs this script - it receives the finished,
self-contained tree. vendor/ is untracked on purpose; see
docs/reference/vendor-build.md.

Usage: scripts/build_vendor.py [TARGET_DIR]   (default: <repo>/vendor)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
JUNK_DIRS = ("__pycache__",)
JUNK_FILES = ("*.so", "*.pyd", "*.pyc", ".lock")


def _installer() -> list[str]:
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--quiet", "--target"]
    return [sys.executable, "-m", "pip", "install", "--quiet", "--target"]


def _strip(tree: Path) -> None:
    for name in ("bin", *JUNK_DIRS):
        path = tree / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    for pattern in JUNK_DIRS:
        for path in sorted(tree.rglob(pattern), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
    for pattern in JUNK_FILES:
        for path in tree.rglob(pattern):
            if path.is_file():
                path.unlink(missing_ok=True)


def _verify(tree: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sys\n"
                "import click, requests\n"
                "assert os.path.realpath(click.__file__).startswith(os.path.realpath(sys.argv[1])), click.__file__\n"
                "assert os.path.realpath(requests.__file__).startswith(os.path.realpath(sys.argv[1])), requests.__file__\n"
            ),
            str(tree),
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(tree),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(f"built tree failed the dependency self-check:\n{proc.stderr.strip()}")


def build(target: Path) -> None:
    if not REQUIREMENTS.is_file():
        raise SystemExit(f"missing pinned requirements: {REQUIREMENTS}")
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.build.", dir=target.parent))
    try:
        cmd = [*_installer(), str(staging), "--require-hashes", "-r", str(REQUIREMENTS)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(
                f"dependency install failed ({' '.join(cmd[:4])}...):\n{(proc.stderr or proc.stdout).strip()}"
            )
        _strip(staging)
        _verify(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    backup = target.parent / f"{target.name}.old"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    if target.exists():
        os.replace(target, backup)
    try:
        os.replace(staging, target)
    except OSError:
        if backup.exists():
            os.replace(backup, target)
        raise
    shutil.rmtree(backup, ignore_errors=True)
    file_count = sum(1 for p in target.rglob("*") if p.is_file())
    print(f"built {target} ({file_count} files, verified click+requests import from it)")


def main() -> int:
    raw = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "vendor")
    if raw == "-h" or raw == "--help":
        print(__doc__)
        return 0
    build(Path(raw).expanduser())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
