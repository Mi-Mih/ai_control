from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

from ai_control.core.models import FileAccessMode

SENSITIVE_PARTS = frozenset(
    {
        ".ssh",
        ".gnupg",
        "appdata",
        "credentials",
        "credential",
        "tokens",
        "token",
        "cookies",
        "browser profiles",
        "system32",
        "windows",
    }
)
SENSITIVE_NAMES = frozenset({".env", "id_rsa", "id_ed25519", "credentials.json"})
PRIVATE_KEY_SUFFIXES = (".pem", ".p12", ".pfx", ".key")


class PathPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PathDecision:
    allowed: bool
    path: Path | PureWindowsPath
    reason: str
    sensitive: bool = False


def _contains_parent(path: PurePath) -> bool:
    return ".." in path.parts


def normalize_windows_path(raw: str) -> PureWindowsPath:
    path = PureWindowsPath(raw)
    if not path.is_absolute() or _contains_parent(path):
        raise PathPolicyError("Windows path must be absolute and cannot contain '..'")
    if path.drive.startswith("\\\\"):
        raise PathPolicyError("UNC paths are not enabled")
    return path


def normalize_local_path(raw: str | Path, *, must_exist: bool = True) -> Path:
    source = Path(raw).expanduser()
    if not source.is_absolute():
        raise PathPolicyError("path must be absolute")
    if ".." in source.parts:
        raise PathPolicyError("path cannot contain '..'")
    try:
        return source.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise PathPolicyError(f"cannot resolve path: {exc}") from exc


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_within_windows(path: PureWindowsPath, root: PureWindowsPath) -> bool:
    path_parts = tuple(item.casefold() for item in path.parts)
    root_parts = tuple(item.casefold() for item in root.parts)
    return path_parts[: len(root_parts)] == root_parts


def is_sensitive(path: Path | PureWindowsPath) -> bool:
    lowered = [part.casefold() for part in path.parts]
    name = path.name.casefold()
    return (
        any(part in SENSITIVE_PARTS for part in lowered)
        or name in SENSITIVE_NAMES
        or name.endswith(PRIVATE_KEY_SUFFIXES)
    )


class PathPolicy:
    def __init__(
        self,
        project_root: Path,
        mode: FileAccessMode,
        approved_paths: tuple[Path, ...] = (),
    ) -> None:
        self.project_root = normalize_local_path(project_root)
        self.mode = mode
        self.approved_paths = tuple(normalize_local_path(path) for path in approved_paths)

    def check(self, raw: str | Path, *, write: bool = False, must_exist: bool = True) -> PathDecision:
        try:
            path = normalize_local_path(raw, must_exist=must_exist)
        except PathPolicyError as exc:
            return PathDecision(False, Path(raw), str(exc))
        sensitive = is_sensitive(path)
        if sensitive:
            return PathDecision(False, path, "sensitive_path_requires_explicit_approval", True)
        if write and self.mode == FileAccessMode.READ_ONLY:
            return PathDecision(False, path, "read_only")
        if self.mode == FileAccessMode.FULL_ACCESS:
            return PathDecision(True, path, "full_access")
        if _is_within(path, self.project_root):
            return PathDecision(True, path, "inside_project")
        if self.mode == FileAccessMode.APPROVED_PATHS and any(_is_within(path, root) for root in self.approved_paths):
            return PathDecision(True, path, "inside_approved_path")
        return PathDecision(False, path, "outside_allowed_paths")


class ExportPolicy:
    def __init__(self, project_root: Path, approved_paths: tuple[Path, ...]) -> None:
        self.project_root = normalize_local_path(project_root)
        self.approved_paths = tuple(normalize_local_path(item) for item in approved_paths)

    def check(self, raw: str | Path) -> PathDecision:
        try:
            path = normalize_local_path(raw)
        except PathPolicyError as exc:
            return PathDecision(False, Path(raw), str(exc))
        if not path.is_file():
            return PathDecision(False, path, "not_a_regular_file")
        sensitive = is_sensitive(path)
        if sensitive:
            return PathDecision(False, path, "sensitive_file_requires_strong_approval", True)
        roots = (self.project_root, *self.approved_paths)
        if any(_is_within(path, root) for root in roots):
            return PathDecision(True, path, "approved_for_export")
        return PathDecision(False, path, "external_export_requires_approval")


def safe_inbox_destination(inbox: Path, supplied_name: str) -> Path:
    name = Path(supplied_name).name
    if not name or name in {".", ".."} or "\x00" in name:
        raise PathPolicyError("invalid file name")
    inbox = inbox.resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    candidate = inbox / name
    counter = 1
    while candidate.exists():
        candidate = inbox / f"{Path(name).stem}_{counter}{Path(name).suffix}"
        counter += 1
    resolved_parent = candidate.parent.resolve(strict=True)
    if resolved_parent != inbox:
        raise PathPolicyError("path traversal detected")
    return candidate


def validate_archive_member(name: str) -> None:
    path = PureWindowsPath(name) if "\\" in name else PurePath(name)
    if path.is_absolute() or _contains_parent(path) or (path.drive if isinstance(path, PureWindowsPath) else ""):
        raise PathPolicyError(f"unsafe archive member: {name}")
    if os.path.normpath(name).startswith(".."):
        raise PathPolicyError(f"unsafe archive member: {name}")
