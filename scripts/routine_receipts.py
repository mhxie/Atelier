"""Ordinary routine artifact evidence, independent of scheduling and replay policy."""

from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import tomllib

VERSION = 4
VERIFICATION_SCOPE = "artifact-body"  # the hash drift is judged by; "artifact-bytes" receipts predate it
SCOPES = {"artifact-bytes", VERIFICATION_SCOPE}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FRONTMATTER = re.compile(rb"---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


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


def body_bytes(data: bytes) -> bytes:
    """The attested body: everything after a leading YAML frontmatter block, minus leading blank lines.
    Reflect stamps `aiSummary` into that block, adds one where none existed, and preserves the mtime."""
    match = _FRONTMATTER.match(data)
    return (data[match.end():] if match else data).lstrip(b"\r\n")


def content_hash(output: Path, *, not_before: float | None = None) -> tuple[str, str]:
    """Validate one open-file snapshot and hash the whole file and its body; reject changes during the read."""
    with output.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if before.st_size == 0:
            raise ValueError("receipt output_file is empty")
        if not_before is not None and before.st_mtime < not_before:
            raise ValueError("reported output_file is stale")
        data = handle.read()
        digests = hashlib.sha256(data).hexdigest(), hashlib.sha256(body_bytes(data)).hexdigest()
        if any(getattr(current, field) != getattr(before, field) for current in (os.fstat(handle.fileno()), output.stat())
               for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
            raise ValueError("artifact changed during verification")
    return digests


def drift_kind(output: Path, completed_at: object) -> str:
    """Name how attested bytes changed: "rewritten" keeps the delivery mtime, "edited" moves it."""
    try:
        return "rewritten" if output.stat().st_mtime <= datetime.fromisoformat(str(completed_at)).timestamp() + 2.0 else "edited"
    except (TypeError, ValueError):
        return "edited"


def read(path: Path, *, routine: str, cycle: str, vault: Path,
         output_dir: str, file_pattern: str = "*.md") -> dict:
    """Read v3/v4 evidence without upgrading it or deciding whether to run again. Attested bytes that moved
    afterwards keep `verification = "passed"` and gain an in-memory `artifact_drift` note: "frontmatter" when
    only the YAML block changed under a body-scope receipt, otherwise "rewritten" or "edited"."""
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
        if version == VERSION:
            scope = receipt.get("verification_scope")
            if scope not in SCOPES:
                raise ValueError("receipt verification scope does not match")
            attested = receipt.get("artifact_sha256")
            body = receipt.get("body_sha256" if scope == VERIFICATION_SCOPE else "artifact_sha256")
            if any(not isinstance(digest, str) or not _SHA256.fullmatch(digest) for digest in (attested, body)):
                raise ValueError("receipt artifact hash is missing or malformed")
            whole, current = content_hash(output)
            if whole != attested:
                receipt["artifact_drift"] = ("frontmatter" if scope == VERIFICATION_SCOPE and current == body
                                             else drift_kind(output, receipt.get("completed_at")))
    return receipt
