"""Ordinary routine artifact evidence, independent of scheduling and replay policy."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tomllib

VERSION = 4
VERIFICATION_SCOPE = "artifact-bytes"


class IdentityMismatch(ValueError):
    """A receipt belongs to a different routine or cycle."""


def artifact_path(output_file: str, *, vault: Path, output_dir: str, file_pattern: str) -> Path:
    """Resolve a nonempty artifact within the caller's declared output boundary."""
    if not isinstance(output_dir, str) or not output_dir or not isinstance(file_pattern, str) or not file_pattern:
        raise ValueError("receipt has no valid output declaration")
    if not isinstance(output_file, str) or not output_file.strip():
        raise ValueError("receipt omitted output_file")
    relative = Path(output_file)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("receipt output_file is unsafe")
    output = (vault / relative).resolve()
    declared = (vault / output_dir).resolve()
    if not output.is_relative_to(vault.resolve()) or not output.is_relative_to(declared):
        raise ValueError("receipt output_file is outside the declaration")
    if output not in {item.resolve() for item in declared.glob(file_pattern) if item.is_file()}:
        raise ValueError("receipt output_file is absent or does not match the declaration")
    if output.stat().st_size == 0:
        raise ValueError("receipt output_file is empty")
    return output


def content_hash(output: Path, *, not_before: float | None = None) -> str:
    """Validate and hash one open-file snapshot; reject changes during the read."""
    with output.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if before.st_size == 0:
            raise ValueError("receipt output_file is empty")
        if not_before is not None and before.st_mtime < not_before:
            raise ValueError("reported output_file is stale")
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        for current in (os.fstat(handle.fileno()), output.stat()):
            if any(getattr(current, field) != getattr(before, field) for field in fields):
                raise ValueError("artifact changed during verification")
    return digest


def read(path: Path, *, routine: str, cycle: str, vault: Path,
         output_dir: str, file_pattern: str = "*.md") -> dict:
    """Read v3/v4 evidence without upgrading it or deciding whether to run again."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("receipt is not a regular file")
    with path.open("rb") as handle:
        receipt = tomllib.load(handle)
    if receipt.get("routine") != routine or receipt.get("cycle_id") != cycle:
        raise IdentityMismatch("receipt has the wrong identity")
    version = receipt.get("contract_version")
    if type(version) is not int or version not in {3, VERSION}:
        raise ValueError("unsupported receipt contract version")
    if receipt.get("verification") == "passed":
        output = artifact_path(receipt.get("output_file"), vault=vault,
                               output_dir=output_dir, file_pattern=file_pattern)
        if version == VERSION and (
            receipt.get("verification_scope") != VERIFICATION_SCOPE
            or receipt.get("artifact_sha256") != content_hash(output)
        ):
            raise ValueError("receipt artifact hash or verification scope does not match")
    return receipt
