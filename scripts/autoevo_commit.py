"""Trusted Autoevo publisher.

The nightly process imports the candidate publisher, reconciler, and stable
``cluster_hash`` helper. Nothing here repairs Git state, retries a failed
publication, rolls back, or pushes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git import BOT_EMAIL, BOT_NAME, default_branch, git_path, merge_state, run_git  # noqa: E402


CANDIDATE_TRAILER = "Autoevo-candidate"
SAFE_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
GIT_FILE_MODES = {0o600: "100644", 0o644: "100644", 0o700: "100755", 0o755: "100755"}


class PublicationError(RuntimeError):
    """A candidate was rejected or its live publication became ambiguous."""

    def __init__(self, message: str, *, publication_started: bool) -> None:
        super().__init__(message)
        self.publication_started = publication_started


@dataclass(frozen=True)
class _CandidateChange:
    relative: str
    before_sha256: str | None
    after: bytes | None
    after_sha256: str | None
    mode: int


def cluster_hash(sources: list[str]) -> str:
    """First 12 hex chars of sha1 over the sorted unique source paths.

    Matches protocols/autoevo.md § Revert tombstones (one path per line,
    LF-terminated) so hashes stay stable across re-runs and machines.
    """
    body = "\n".join(sorted(set(sources))) + "\n"
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]


def _git(
    vault: Path, *args: str, bot_identity: bool = False
) -> subprocess.CompletedProcess[str]:
    return run_git(vault, *args, timeout=120, bot_identity=bot_identity)


def _publication_error(message: str, *, started: bool = False) -> PublicationError:
    return PublicationError(message, publication_started=started)


def _git_result(vault: Path, *args: str, started: bool = False, text: bool = True) -> subprocess.CompletedProcess:
    try:
        result = run_git(vault, *args, timeout=120, text=text)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _publication_error(f"git {args[0]} could not complete: {exc}", started=started) from exc
    if result.returncode != 0:
        raw = result.stderr if text else result.stderr.decode("utf-8", errors="replace")
        detail = raw.strip()[:300]
        raise _publication_error(f"git {args[0]} failed: {detail}", started=started)
    return result


def _relative_name(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(mark) < 32 or 0x7F <= ord(mark) <= 0x9F for mark in value)
    ):
        raise _publication_error(f"{field} must be a safe nonempty POSIX relative path")
    path = PurePosixPath(value)
    normalized = path.as_posix()
    if path.is_absolute() or normalized != value or any(part in {"", ".", ".."} for part in path.parts):
        raise _publication_error(f"{field} must be a normalized relative path")
    if any(part.casefold() == ".git" for part in path.parts):
        raise _publication_error(f"{field} must not address Git metadata")
    return normalized


def _scopes(values: tuple[str, ...], field: str) -> tuple[tuple[str, bool], ...]:
    scopes: list[tuple[str, bool]] = []
    for value in values:
        subtree = isinstance(value, str) and value.endswith("/")
        raw = value[:-1] if subtree else value
        scopes.append((_relative_name(raw, field), subtree))
    return tuple(scopes)


def _matches_scope(relative: str, scopes: tuple[tuple[str, bool], ...]) -> bool:
    return any(
        relative.startswith(prefix + "/") if subtree else relative == prefix
        for prefix, subtree in scopes
    )


def _tree_entry(vault: Path, commit: str, relative: str) -> tuple[str, str] | None:
    result = _git_result(vault, "ls-tree", "-z", commit, "--", relative, text=False)
    rows = [row for row in result.stdout.split(b"\0") if row]
    if not rows:
        return None
    if len(rows) != 1 or b"\t" not in rows[0]:
        raise _publication_error(f"cannot resolve one Git tree entry for {relative}")
    metadata, raw_name = rows[0].split(b"\t", 1)
    fields = metadata.decode("ascii", errors="strict").split()
    if len(fields) != 3 or os.fsdecode(raw_name) != relative or fields[1] != "blob":
        raise _publication_error(f"{relative} is not one regular file in the Git tree")
    return fields[0], fields[2]


def _blob_sha256(vault: Path, oid: str) -> str:
    result = _git_result(vault, "cat-file", "blob", oid, text=False)
    return hashlib.sha256(result.stdout).hexdigest()


def _assert_git_boundary(vault: Path, expected_head: str, *, started: bool = False) -> None:
    current = _git_result(vault, "rev-parse", "HEAD", started=started).stdout.strip()
    if current != expected_head:
        raise _publication_error(
            f"vault HEAD changed: expected {expected_head}, found {current}",
            started=started,
        )
    try:
        primary = default_branch(vault)
        branch = _git_result(vault, "symbolic-ref", "--quiet", "--short", "HEAD", started=started).stdout.strip()
        if not primary or branch != primary:
            raise _publication_error("publication requires the checked-out default branch", started=started)
        active = merge_state(vault)
        lock = git_path(vault, "index.lock")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _publication_error(f"cannot inspect Git publication boundary: {exc}", started=started) from exc
    if active:
        raise _publication_error(f"Git operation is in progress: {', '.join(active)}", started=started)
    if lock is not None and lock.exists():
        raise _publication_error("Git index.lock is present", started=started)


def _path_state(vault: Path, relative: str, *, started: bool = False) -> os.stat_result | None:
    path = vault / relative
    current = vault
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _publication_error(f"parent path is not a real directory for {relative}", started=started)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _publication_error(f"touched path is not a regular file: {relative}", started=started)
    if info.st_nlink != 1:
        raise _publication_error(f"touched path has hard links: {relative}", started=started)
    return info


def _is_ignored(vault: Path, relative: str, *, started: bool = False) -> bool:
    try:
        result = run_git(vault, "check-ignore", "--no-index", "-q", "--", relative, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _publication_error(f"cannot check ignore rules for {relative}: {exc}", started=started) from exc
    if result.returncode not in {0, 1}:
        raise _publication_error(f"cannot check ignore rules for {relative}", started=started)
    return result.returncode == 0


def _assert_live_before(
    vault: Path,
    change: _CandidateChange,
    *,
    force_add: frozenset[str] = frozenset(),
    started: bool = False,
) -> None:
    info = _path_state(vault, change.relative, started=started)
    if change.before_sha256 is None:
        if info is not None:
            raise _publication_error(f"candidate target already exists: {change.relative}", started=started)
        ignored = _is_ignored(vault, change.relative, started=started)
        if ignored and change.relative not in force_add:
            raise _publication_error(f"candidate target is ignored by Git: {change.relative}", started=started)
        return
    if info is None:
        raise _publication_error(f"candidate source is missing: {change.relative}", started=started)
    if stat.S_IMODE(info.st_mode) != change.mode:
        raise _publication_error(f"candidate source mode changed: {change.relative}", started=started)
    try:
        actual = hashlib.sha256((vault / change.relative).read_bytes()).hexdigest()
    except OSError as exc:
        raise _publication_error(f"cannot read candidate source {change.relative}: {exc}", started=started) from exc
    if actual != change.before_sha256:
        raise _publication_error(f"candidate source changed: {change.relative}", started=started)
    _assert_clean_paths(vault, [change.relative], started=started)


def _validate_changes(
    vault: Path,
    changes: dict[str, dict[str, object]],
    *,
    expected_head: str,
    allowed_prefixes: tuple[str, ...],
    protected_paths: set[str],
    force_add: frozenset[str],
) -> list[_CandidateChange]:
    if not allowed_prefixes:
        raise _publication_error("allowed_prefixes must not be empty")
    scopes = _scopes(allowed_prefixes, "allowed prefix")
    protected = _scopes(tuple(protected_paths), "protected path")
    validated = _validate_reconciliation_shape(changes)
    for change in validated:
        relative = change.relative
        if not _matches_scope(relative, scopes):
            raise _publication_error(f"candidate path is outside the allowed scope: {relative}")
        if _matches_scope(relative, protected):
            raise _publication_error(f"candidate path is protected: {relative}")
        if change.before_sha256 is None and change.after is None:
            raise _publication_error(f"candidate change is an absent-to-absent no-op: {relative}")
        if change.before_sha256 is not None and change.before_sha256 == change.after_sha256:
            raise _publication_error(f"candidate change does not alter file bytes: {relative}")

    for change in validated:
        base = _tree_entry(vault, expected_head, change.relative)
        if change.relative in force_add:
            if not _is_ignored(vault, change.relative):
                raise _publication_error(f"force-added candidate path is not ignored: {change.relative}")
            if base is None and change.after is None:
                raise _publication_error(f"cannot commit deletion of untracked ignored path: {change.relative}")
            if base is not None:
                if change.before_sha256 is None:
                    raise _publication_error(f"candidate expected an absent base path: {change.relative}")
                base_mode, oid = base
                if base_mode != GIT_FILE_MODES[change.mode] or _blob_sha256(vault, oid) != change.before_sha256:
                    raise _publication_error(f"candidate base evidence does not match: {change.relative}")
        elif change.before_sha256 is None:
            if base is not None:
                raise _publication_error(f"candidate expected an absent base path: {change.relative}")
        else:
            if base is None:
                raise _publication_error(f"candidate base path is absent: {change.relative}")
            base_mode, oid = base
            if base_mode != GIT_FILE_MODES[change.mode]:
                raise _publication_error(f"candidate base mode does not match: {change.relative}")
            if _blob_sha256(vault, oid) != change.before_sha256:
                raise _publication_error(f"candidate base hash does not match: {change.relative}")
        _assert_live_before(vault, change, force_add=force_add)
    return validated


def _write_file(vault: Path, change: _CandidateChange) -> None:
    assert change.after is not None
    destination = vault / change.relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    _path_state(vault, change.relative, started=True)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{destination.name}.autoevo-", dir=destination.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(change.after)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, change.mode)
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _delete_file(vault: Path, change: _CandidateChange) -> None:
    (vault / change.relative).unlink()
    directory = os.open((vault / change.relative).parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _assert_live_after(vault: Path, change: _CandidateChange, *, started: bool) -> None:
    info = _path_state(vault, change.relative, started=started)
    if change.after is None:
        if info is not None:
            raise _publication_error(f"deleted candidate path still exists: {change.relative}", started=started)
        return
    if info is None:
        raise _publication_error(f"published candidate path is missing: {change.relative}", started=started)
    if stat.S_IMODE(info.st_mode) != change.mode:
        raise _publication_error(f"published candidate mode does not match: {change.relative}", started=started)
    try:
        actual = hashlib.sha256((vault / change.relative).read_bytes()).hexdigest()
    except OSError as exc:
        raise _publication_error(f"cannot verify published path {change.relative}: {exc}", started=started) from exc
    if actual != change.after_sha256:
        raise _publication_error(f"published candidate bytes do not match: {change.relative}", started=started)


def _candidate_message(message: str, candidate_id: str) -> str:
    if not isinstance(message, str) or not message.strip() or "\0" in message:
        raise _publication_error("candidate commit message must be nonempty")
    if re.search(rf"(?mi)^{re.escape(CANDIDATE_TRAILER)}\s*:", message):
        raise _publication_error("candidate commit message must not supply its own candidate trailer")
    return f"{message.rstrip()}\n\n{CANDIDATE_TRAILER}: {candidate_id}"


def _candidate_commits(vault: Path, candidate_id: str) -> list[str]:
    needle = f"{CANDIDATE_TRAILER}: {candidate_id}"
    result = _git_result(vault, "log", "--all", "--format=%H", "--fixed-strings", f"--grep={needle}")
    commits: list[str] = []
    for sha in result.stdout.splitlines():
        body = _git_result(vault, "show", "-s", "--format=%B", sha).stdout
        if body.splitlines().count(needle) == 1:
            commits.append(sha)
    return commits


def _commit_paths(vault: Path, sha: str) -> list[str]:
    result = _git_result(
        vault,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "--no-renames",
        "-z",
        sha,
        text=False,
    )
    return sorted(os.fsdecode(value) for value in result.stdout.split(b"\0") if value)


def reconcile_commit(
    vault: Path,
    *,
    candidate_id: str,
    expected_head: str,
    changes: dict[str, dict[str, object]],
    force_add: set[str] | frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Prove one already-published candidate from immutable Git evidence."""
    vault = Path(vault).resolve(strict=True)
    if not SAFE_CANDIDATE_ID.fullmatch(candidate_id):
        raise _publication_error("candidate_id is invalid")
    if not SAFE_COMMIT.fullmatch(expected_head):
        raise _publication_error("expected_head is invalid")
    force = frozenset(_relative_name(value, "force-add path") for value in force_add)
    if not force.issubset(changes):
        raise _publication_error("force_add must be a subset of candidate paths")
    # Scope and live-worktree checks belong to publication. Reconciliation only
    # accepts the candidate's exact typed shape and proves immutable commit data.
    typed = _validate_reconciliation_shape(changes)
    commits = _candidate_commits(vault, candidate_id)
    if len(commits) != 1:
        raise _publication_error(f"expected one commit for candidate {candidate_id}, found {len(commits)}")
    sha = commits[0]
    parents = _git_result(vault, "rev-list", "--parents", "-n", "1", sha).stdout.split()
    if parents != [sha, expected_head]:
        raise _publication_error(f"candidate commit does not have expected parent {expected_head}")
    identities = _git_result(vault, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce", sha, text=False).stdout
    if identities.rstrip(b"\n").split(b"\0") != [
        BOT_NAME.encode(), BOT_EMAIL.encode(), BOT_NAME.encode(), BOT_EMAIL.encode()
    ]:
        raise _publication_error("candidate commit does not use the pinned bot identity")
    paths = sorted(item.relative for item in typed)
    if _commit_paths(vault, sha) != paths:
        raise _publication_error("candidate commit changed a different path set")
    for change in typed:
        before = _tree_entry(vault, expected_head, change.relative)
        if change.relative in force:
            if before is not None:
                if change.before_sha256 is None:
                    raise _publication_error(f"force-added candidate expected absent parent path {change.relative}")
                if (
                    before[0] != GIT_FILE_MODES[change.mode]
                    or _blob_sha256(vault, before[1]) != change.before_sha256
                ):
                    raise _publication_error(f"force-added candidate parent evidence does not match {change.relative}")
            elif change.after is None:
                raise _publication_error(f"force-added candidate cannot delete absent parent path {change.relative}")
        elif change.before_sha256 is None:
            if before is not None:
                raise _publication_error(f"candidate base unexpectedly contains {change.relative}")
        else:
            if before is None or before[0] != GIT_FILE_MODES[change.mode] or _blob_sha256(vault, before[1]) != change.before_sha256:
                raise _publication_error(f"candidate parent evidence does not match {change.relative}")
        after = _tree_entry(vault, sha, change.relative)
        if change.after is None:
            if after is not None:
                raise _publication_error(f"candidate commit did not delete {change.relative}")
        elif (
            after is None
            or after[0] != GIT_FILE_MODES[change.mode]
            or _blob_sha256(vault, after[1]) != change.after_sha256
        ):
            raise _publication_error(f"candidate commit evidence does not match {change.relative}")
    return {
        "sha": sha,
        "parent": expected_head,
        "candidate_id": candidate_id,
        "paths": paths,
        "after_sha256": {item.relative: item.after_sha256 for item in typed},
        "modes": {item.relative: item.mode if item.after is not None else None for item in typed},
        "force_added": sorted(force),
    }


def _validate_reconciliation_shape(changes: dict[str, dict[str, object]]) -> list[_CandidateChange]:
    if not isinstance(changes, dict) or not changes:
        raise _publication_error("candidate changes must be a nonempty object")
    typed: list[_CandidateChange] = []
    for raw_relative, raw in changes.items():
        relative = _relative_name(raw_relative, "change path")
        if not isinstance(raw, dict) or set(raw) != {"before_sha256", "after", "mode"}:
            raise _publication_error(f"candidate change has the wrong fields: {relative}")
        before = raw["before_sha256"]
        after_text = raw["after"]
        mode = raw["mode"]
        if before is not None and (not isinstance(before, str) or not SAFE_SHA256.fullmatch(before)):
            raise _publication_error(f"candidate before_sha256 is invalid: {relative}")
        if after_text is not None and not isinstance(after_text, str):
            raise _publication_error(f"candidate after value is invalid: {relative}")
        if isinstance(mode, bool) or not isinstance(mode, int) or mode not in GIT_FILE_MODES:
            raise _publication_error(f"candidate mode must be 0600, 0644, 0700 or 0755: {relative}")
        after = after_text.encode("utf-8") if isinstance(after_text, str) else None
        typed.append(
            _CandidateChange(
                relative,
                before,
                after,
                hashlib.sha256(after).hexdigest() if after is not None else None,
                mode,
            )
        )
    return sorted(typed, key=lambda item: item.relative)


def _frozen_changes(changes: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    """Copy the accepted typed record so a boundary callback cannot mutate it."""
    return {
        item.relative: {
            "before_sha256": item.before_sha256,
            "after": item.after.decode("utf-8") if item.after is not None else None,
            "mode": item.mode,
        }
        for item in _validate_reconciliation_shape(changes)
    }


def publish_changes(
    vault: Path,
    *,
    changes: dict[str, dict[str, object]],
    message: str,
    candidate_id: str,
    expected_head: str,
    allowed_prefixes: tuple[str, ...],
    protected_paths: set[str],
    recheck: Callable[[], None] | None = None,
    force_add: set[str] | frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Validate and publish exactly one accepted Autoevo operation.

    The caller persists accepted intent before entry. Any error after the first
    filesystem write has ``publication_started=True`` and must be reconciled or
    reviewed; this function never retries, rolls back, repairs Git, or pushes.
    """
    vault = Path(vault).resolve(strict=True)
    if not vault.is_dir():
        raise _publication_error("vault must be a directory")
    if not SAFE_CANDIDATE_ID.fullmatch(candidate_id):
        raise _publication_error("candidate_id is invalid")
    if not SAFE_COMMIT.fullmatch(expected_head):
        raise _publication_error("expected_head is invalid")
    accepted_changes = _frozen_changes(changes)
    accepted_prefixes = tuple(allowed_prefixes)
    accepted_protected = set(protected_paths)
    force = frozenset(_relative_name(value, "force-add path") for value in force_add)
    if not force.issubset(accepted_changes):
        raise _publication_error("force_add must be a subset of candidate paths")
    commit_message = _candidate_message(message, candidate_id)
    _git_result(vault, "cat-file", "-e", f"{expected_head}^{{commit}}")
    _assert_git_boundary(vault, expected_head)
    typed = _validate_changes(
        vault,
        accepted_changes,
        expected_head=expected_head,
        allowed_prefixes=accepted_prefixes,
        protected_paths=accepted_protected,
        force_add=force,
    )
    if recheck is not None:
        try:
            recheck()
        except Exception as exc:
            raise _publication_error(f"candidate boundary recheck failed: {exc}") from exc
        _assert_git_boundary(vault, expected_head)
        typed = _validate_changes(
            vault,
            accepted_changes,
            expected_head=expected_head,
            allowed_prefixes=accepted_prefixes,
            protected_paths=accepted_protected,
            force_add=force,
        )

    started = False
    ordered = [item for item in typed if item.after is not None] + [item for item in typed if item.after is None]
    try:
        for change in ordered:
            _assert_git_boundary(vault, expected_head, started=started)
            _assert_live_before(vault, change, force_add=force, started=started)
            started = True
            if change.after is None:
                _delete_file(vault, change)
            else:
                _write_file(vault, change)
            _assert_live_after(vault, change, started=True)
        for change in typed:
            _assert_live_after(vault, change, started=True)
        _assert_git_boundary(vault, expected_head, started=True)
        paths = [item.relative for item in typed]
        deletions = {item.relative for item in typed if item.after is None}
        ordinary = [path for path in paths if path not in force or path in deletions]
        if ordinary:
            _git_result(vault, "add", "-A", "--", *ordinary, started=True)
        force_present = sorted(force - deletions)
        if force_present:
            _git_result(vault, "add", "-f", "--", *force_present, started=True)
        try:
            commit = _git(vault, "commit", "--only", "-m", commit_message, "--", *paths, bot_identity=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _publication_error(f"git commit could not complete: {exc}", started=True) from exc
        if commit.returncode != 0:
            detail = (commit.stderr.strip() or commit.stdout.strip())[:300]
            raise _publication_error(f"git commit failed: {detail}", started=True)
        evidence = reconcile_commit(
            vault,
            candidate_id=candidate_id,
            expected_head=expected_head,
            changes=accepted_changes,
            force_add=force,
        )
        for change in typed:
            _assert_live_after(vault, change, started=True)
        _assert_clean_paths(vault, paths, started=True)
        return evidence
    except PublicationError as exc:
        exc.publication_started = exc.publication_started or started
        raise
    except Exception as exc:
        raise _publication_error(f"publication failed: {exc}", started=started) from exc


def _assert_clean_paths(vault: Path, paths: list[str], *, started: bool) -> None:
    result = _git_result(
        vault,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        *paths,
        started=started,
        text=False,
    )
    if result.stdout:
        raise _publication_error("candidate paths have uncommitted changes", started=started)
