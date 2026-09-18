#!/usr/bin/env python3
"""Compare a runtime copy of this skill against the repo, one named file at a time.

Three copies of this skill run in the world and only one of them is a
repository. This exists so "is the running copy actually current" has an
answer, and so applying a fix leaves a checkable record rather than a hope.

It is deliberately narrow: it compares a reviewed manifest, never a tree. A
recursive sync would be unsafe in either direction here, because no copy is
uniformly ahead of the others.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


STATUS_ORDER = ("same", "differs", "missing-in-target", "missing-in-repo", "unreadable")


def _read_manifest(path: Path) -> list[str]:
    entries: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def compare(*, repo_root: Path, target_root: Path, manifest: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for relative in manifest:
        repo_path = repo_root / relative
        target_path = target_root / relative
        repo_digest = _digest(repo_path) if repo_path.is_file() else None
        target_digest = _digest(target_path) if target_path.is_file() else None

        if repo_path.is_file() and repo_digest is None:
            status = "unreadable"
        elif target_path.is_file() and target_digest is None:
            status = "unreadable"
        elif repo_digest is None:
            status = "missing-in-repo"
        elif target_digest is None:
            status = "missing-in-target"
        elif repo_digest == target_digest:
            status = "same"
        else:
            status = "differs"
        rows.append({"path": relative, "status": status})
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", required=True, help="File listing repo-relative runtime paths")
    parser.add_argument("--target", required=True, help="Root of a runtime copy to compare against")
    parser.add_argument("--repo-root", default=None, help="Repo root (defaults to this script's parent)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable rows")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).expanduser().resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    manifest_path = Path(args.manifest).expanduser()
    if not manifest_path.is_file():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    target_root = Path(args.target).expanduser().resolve()
    if not target_root.is_dir():
        print(f"Target root is not a directory: {target_root}", file=sys.stderr)
        return 2

    rows = compare(
        repo_root=repo_root,
        target_root=target_root,
        manifest=_read_manifest(manifest_path),
    )

    if args.json:
        print(json.dumps({"rows": rows}, indent=2))
    else:
        for row in rows:
            print(f"{row['status']:<18} {row['path']}")

    drifted = [row for row in rows if row["status"] != "same"]
    if not drifted:
        print(f"Manifest clean: {len(rows)} of {len(rows)} files match.")
        return 0
    print(f"{len(drifted)} of {len(rows)} manifest entries are not current.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
