"""Delivery to a runtime copy has to be checkable, or it is a hope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from outbox_drift_probe import compare, main


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative, text in {
        "scripts/log_outbox.py": "DRAIN = 1\n",
        "bin/send_sms.py": "drain_on_use()\n",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.txt"
    path.write_text(
        "# reviewed set\n\nscripts/log_outbox.py\nbin/send_sms.py\n",
        encoding="utf-8",
    )
    return path


def _copy_runtime(repo: Path, target: Path) -> None:
    for relative in ("scripts/log_outbox.py", "bin/send_sms.py"):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text((repo / relative).read_text(encoding="utf-8"), encoding="utf-8")


def test_a_freshly_applied_file_set_reports_clean(tmp_path: Path, repo: Path, manifest: Path) -> None:
    target = tmp_path / "runtime"
    _copy_runtime(repo, target)

    rows = compare(
        repo_root=repo,
        target_root=target,
        manifest=["scripts/log_outbox.py", "bin/send_sms.py"],
    )

    assert [row["status"] for row in rows] == ["same", "same"]
    assert main(["--manifest", str(manifest), "--target", str(target), "--repo-root", str(repo)]) == 0


def test_reverting_one_runtime_file_names_exactly_that_file(
    tmp_path: Path, repo: Path, manifest: Path, capsys
) -> None:
    target = tmp_path / "runtime"
    _copy_runtime(repo, target)
    (target / "bin/send_sms.py").write_text("pass\n", encoding="utf-8")

    exit_code = main(["--manifest", str(manifest), "--target", str(target), "--repo-root", str(repo)])

    lines = capsys.readouterr().out.splitlines()

    assert exit_code == 1
    assert lines[0] == "same               scripts/log_outbox.py"
    assert lines[1] == "differs            bin/send_sms.py"
    assert lines[2] == "1 of 2 manifest entries are not current."


def test_a_file_the_copy_never_received_is_reported_as_missing(tmp_path: Path, repo: Path) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    (target / "scripts").mkdir()
    (target / "scripts/log_outbox.py").write_text(
        (repo / "scripts/log_outbox.py").read_text(encoding="utf-8"), encoding="utf-8"
    )

    rows = compare(
        repo_root=repo,
        target_root=target,
        manifest=["scripts/log_outbox.py", "bin/send_sms.py"],
    )

    assert [row["status"] for row in rows] == ["same", "missing-in-target"]


def test_the_manifest_is_the_compared_set_no_more(tmp_path: Path, repo: Path) -> None:
    """An unlisted drift goes unreported by design: the set is reviewed, not walked."""
    target = tmp_path / "runtime"
    _copy_runtime(repo, target)
    (target / "bin/send_sms.py").write_text("drift\n", encoding="utf-8")

    rows = compare(repo_root=repo, target_root=target, manifest=["scripts/log_outbox.py"])

    assert [row["status"] for row in rows] == ["same"]


def test_a_manifest_entry_the_repo_no_longer_has_is_visible(
    tmp_path: Path, repo: Path, capsys
) -> None:
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("scripts/retired.py\n", encoding="utf-8")
    target = tmp_path / "runtime"
    _copy_runtime(repo, target)

    exit_code = main(["--manifest", str(manifest), "--target", str(target), "--repo-root", str(repo)])

    assert exit_code == 1
    assert "missing-in-repo    scripts/retired.py" in capsys.readouterr().out


def test_the_shipped_manifest_names_files_that_exist_in_this_repo() -> None:
    """A delivery manifest that silently rots is the failure this probe exists to catch."""
    root = Path(__file__).resolve().parent.parent
    from outbox_drift_probe import _read_manifest

    entries = _read_manifest(root / "references/outbox-runtime-files.txt")

    assert entries, "the manifest must name the runtime set"
    missing = [entry for entry in entries if not (root / entry).is_file()]
    assert missing == []


def test_the_shipped_manifest_covers_every_runtime_file_this_change_touched() -> None:
    """U1-U4 changed five runtime files; the manifest must name exactly those."""
    root = Path(__file__).resolve().parent.parent
    from outbox_drift_probe import _read_manifest

    assert set(_read_manifest(root / "references/outbox-runtime-files.txt")) == {
        "scripts/log_outbox.py",
        "bin/send_sms.py",
        "bin/list_sms_inbox.py",
        "bin/list_sms_thread.py",
        "bin/list_calls.py",
    }
