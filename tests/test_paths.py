import zipfile
from pathlib import Path, PureWindowsPath

import pytest

from ai_control.core.models import FileAccessMode
from ai_control.files.policy import (
    PathPolicy,
    PathPolicyError,
    is_sensitive,
    normalize_windows_path,
    safe_inbox_destination,
    validate_archive_member,
)
from ai_control.files.service import safe_extract_zip


def test_project_policy_blocks_outside_and_follows_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    secret = outside / "data.txt"
    secret.write_text("secret")
    link = project / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks are not available")
    policy = PathPolicy(project, FileAccessMode.PROJECT_ONLY)
    decision = policy.check(link)
    assert not decision.allowed
    assert decision.path == secret.resolve()


def test_approved_paths_and_sensitive_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    approved = tmp_path / "approved"
    project.mkdir()
    approved.mkdir()
    ordinary = approved / "report.pdf"
    ordinary.write_text("ok")
    policy = PathPolicy(project, FileAccessMode.APPROVED_PATHS, (approved,))
    assert policy.check(ordinary).allowed
    env = project / ".env"
    env.write_text("TOKEN=x")
    assert policy.check(env).sensitive
    assert not policy.check(env).allowed


def test_read_only_policy_allows_reads_but_blocks_writes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    existing = project / "report.txt"
    existing.write_text("ok")
    policy = PathPolicy(project, FileAccessMode.READ_ONLY)

    assert policy.check(existing).allowed
    decision = policy.check(existing, write=True)
    assert not decision.allowed
    assert decision.reason == "read_only"


def test_windows_paths_are_case_insensitive_and_reject_traversal() -> None:
    assert normalize_windows_path(r"C:\Projects\App") == PureWindowsPath(r"C:\Projects\App")
    with pytest.raises(PathPolicyError):
        normalize_windows_path(r"C:\Projects\App\..\secret")
    with pytest.raises(PathPolicyError):
        normalize_windows_path(r"\\server\share\file")
    assert is_sensitive(PureWindowsPath(r"C:\Users\Me\.ssh\id_ed25519"))


def test_inbox_does_not_overwrite(tmp_path: Path) -> None:
    first = safe_inbox_destination(tmp_path, "../../report.txt")
    first.write_text("one")
    second = safe_inbox_destination(tmp_path, "report.txt")
    assert second.name == "report_1.txt"


@pytest.mark.parametrize("name", ["../evil", "/etc/passwd", r"C:\evil", r"folder\..\evil"])
def test_archive_path_traversal_rejected(name: str) -> None:
    with pytest.raises(PathPolicyError):
        validate_archive_member(name)


def test_zip_slip_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escaped.txt", "bad")
    with pytest.raises(PathPolicyError):
        safe_extract_zip(archive, tmp_path / "out", 1024)
    assert not (tmp_path / "escaped.txt").exists()
