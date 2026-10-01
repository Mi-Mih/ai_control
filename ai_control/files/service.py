from __future__ import annotations

import hashlib
import shutil
import stat
import zipfile
from pathlib import Path

from ai_control.files.policy import PathPolicyError, safe_inbox_destination, validate_archive_member


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def store_upload(source: Path, inbox: Path, original_name: str, max_bytes: int) -> Path:
    if source.stat().st_size > max_bytes:
        raise PathPolicyError("file exceeds configured size limit")
    destination = safe_inbox_destination(inbox, original_name)
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
    return destination


def safe_extract_zip(archive: Path, destination: Path, max_uncompressed_bytes: int) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(archive) as bundle:
        total = 0
        for member in bundle.infolist():
            validate_archive_member(member.filename)
            unix_mode = member.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise PathPolicyError("archive symbolic links are not allowed")
            total += member.file_size
            if total > max_uncompressed_bytes:
                raise PathPolicyError("archive exceeds uncompressed size limit")
            if member.is_dir():
                continue
            target = destination / member.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            resolved = target.resolve(strict=False)
            if destination.resolve() not in (resolved, *resolved.parents):
                raise PathPolicyError("Zip Slip detected")
            with bundle.open(member) as source, target.open("xb") as sink:
                shutil.copyfileobj(source, sink, length=1024 * 1024)
            extracted.append(target)
    return extracted
