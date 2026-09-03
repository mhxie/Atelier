#!/usr/bin/env python3
"""Capture private, replayable snapshots of native Codex and Claude sessions.

Installed hooks invoke this script on every user prompt and completed turn.
Capture is disabled by default, enabled persistently through the machine-local
preference, and overridable for one process through
`ATELIER_SESSION_REPLAY_ENABLED`. When enabled, the prompt event is written
immediately and completed-turn hooks copy the current native transcript
atomically. Native formats are preserved rather than parsed because their
schemas are runtime implementation details that may change.

All output is private runtime data under a machine-local cache by default (or
the path configured by `ATELIER_SESSION_REPLAY_ROOT`). It is intentionally
excluded from semantic retrieval and version control.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import atomic_write  # noqa: E402

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]


RUNTIMES = ("codex", "claude-code")
SCHEMA_VERSION = 1
CHUNK_BYTES = 64 * 1024
SESSION_ID_MAX = 200
ARCHIVE_ROOT_ENV = "ATELIER_SESSION_REPLAY_ROOT"
ENABLED_ENV = "ATELIER_SESSION_REPLAY_ENABLED"
LOCAL_CONFIG_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    / "atelier"
    / "session-replay.toml"
)

# These patterns are intentionally conservative. A sensitive transcript is
# excluded from the archive rather than traded for replay coverage.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "openai_api_key",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~-]{16,}\b"),
    ),
    (
        "password_assignment",
        re.compile(r"(?i)\b(?:password|passwd|api[_-]?key)\s*[:=]\s*\S+"),
    ),
)


def now_local() -> datetime:
    return datetime.now().astimezone()


def replay_activation() -> tuple[bool, str]:
    """Resolve capture activation with an explicit environment override."""
    environment_value = os.environ.get(ENABLED_ENV)
    if environment_value is not None:
        return environment_value == "1", f"environment:{ENABLED_ENV}"

    try:
        with LOCAL_CONFIG_PATH.open("rb") as handle:
            local_config = tomllib.load(handle)
    except FileNotFoundError:
        return False, "default"
    except (OSError, tomllib.TOMLDecodeError):
        return False, "local-config-invalid"

    session_replay = local_config.get("session_replay")
    enabled = (
        session_replay.get("enabled") if isinstance(session_replay, dict) else None
    )
    if not isinstance(enabled, bool):
        return False, "local-config-invalid"
    return enabled, "local-config"


def replay_enabled() -> bool:
    return replay_activation()[0]


def archive_root() -> Path:
    configured = os.environ.get(ARCHIVE_ROOT_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "atelier" / "session-replays"


def session_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result[:4096] if result else None


def archive_key(runtime: str, runtime_session_id: str, source: Path) -> str:
    """Return a readable, collision-resistant key for an archive filename."""
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", runtime_session_id).strip(".-")
    label = label[:SESSION_ID_MAX] or "session"
    identity = f"{runtime}\0{runtime_session_id}\0{source.resolve()}".encode("utf-8")
    return f"{label}--{hashlib.sha256(identity).hexdigest()[:16]}"


def optional_string(value: object, *, max_length: int = 4096) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result[:max_length] if result else None


def event_path(root: Path, captured_at: datetime) -> Path:
    return root / "events" / f"{captured_at.date().isoformat()}.jsonl"


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def append_event(root: Path, captured_at: datetime, payload: dict[str, Any]) -> None:
    path = event_path(root, captured_at)
    ensure_private_directory(root)
    ensure_private_directory(path.parent)
    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def redact_prompt(prompt: str) -> tuple[str, list[str]]:
    redactions: list[str] = []
    result = prompt
    for label, pattern in SECRET_PATTERNS:
        result, count = pattern.subn(f"<redacted:{label}>", result)
        if count:
            redactions.append(label)
    return result, redactions


def runtime_source_roots(runtime: str, *, cwd: object = None) -> tuple[Path, ...]:
    home = Path.home()
    if runtime == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
        roots = [codex_home / "sessions"]
        if isinstance(cwd, str) and cwd.strip():
            try:
                project_root = Path(cwd).expanduser().resolve(strict=True)
                project_transcripts = (project_root / ".codex").resolve(strict=True)
            except OSError:
                project_transcripts = None
            if project_transcripts is not None and project_transcripts.is_dir():
                roots.append(project_transcripts)
        return tuple(roots)
    claude_home = Path(
        os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")
    ).expanduser()
    return (claude_home / "projects",)


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def trusted_transcript_path(
    runtime: str,
    value: object,
    *,
    cwd: object = None,
) -> tuple[Path | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, "path_absent"
    try:
        candidate = Path(value).expanduser().resolve(strict=True)
    except FileNotFoundError:
        return None, "path_missing"
    except OSError:
        return None, "path_unreadable"
    if not candidate.is_file():
        return None, "not_regular_file"
    for root in runtime_source_roots(runtime, cwd=cwd):
        try:
            trusted_root = root.resolve(strict=True)
        except OSError:
            continue
        if is_under(candidate, trusted_root):
            return candidate, None
    return None, "path_untrusted"


def contains_secret(chunks: Iterable[bytes]) -> str | None:
    tail = ""
    for chunk in chunks:
        text = tail + chunk.decode("utf-8", errors="ignore")
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                return label
        tail = text[-512:]
    return None


def transcript_paths(root: Path, runtime: str, key: str) -> tuple[Path, Path]:
    base = root / "transcripts" / runtime
    return base / f"{key}.jsonl", root / "manifests" / runtime / f"{key}.json"


def load_manifest(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def source_signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def local_verified_copy(
    source: Path,
) -> tuple[Path | None, dict[str, int], str | None, str | None]:
    """Copy to private system temp, then scan before anything reaches the archive."""
    signature = source_signature(source)
    descriptor, temp_name = tempfile.mkstemp(
        prefix="atelier-session-replay-", suffix=".jsonl"
    )
    temporary = Path(temp_name)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target:
            while chunk := source_handle.read(CHUNK_BYTES):
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if source_signature(source) != signature:
            raise OSError("transcript_changed_during_capture")
        secret_label = contains_secret(_file_chunks(temporary))
        if secret_label is not None:
            temporary.unlink(missing_ok=True)
            return None, signature, None, secret_label
        return temporary, signature, digest.hexdigest(), None
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _file_chunks(path: Path) -> Iterable[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            yield chunk


def copy_verified_temp(source: Path, destination: Path) -> None:
    """Publish only a previously scanned clean snapshot into the archive."""
    ensure_private_directory(destination.parent)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target:
            while chunk := source_handle.read(CHUNK_BYTES):
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_name, destination)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def snapshot_transcript(
    *,
    root: Path,
    runtime: str,
    session_id: str,
    source: Path,
    captured_at: datetime,
    hook_event: str | None,
    model: str | None,
) -> str:
    ensure_private_directory(root)
    key = archive_key(runtime, session_id, source)
    destination, manifest_path = transcript_paths(root, runtime, key)
    signature = source_signature(source)
    existing = load_manifest(manifest_path)
    if existing is not None and existing.get("source_signature") == signature:
        status = existing.get("capture_status")
        if status == "skipped_sensitive":
            return "skipped_sensitive"
        if destination.is_file():
            append_event(
                root,
                captured_at,
                {
                    "schema": SCHEMA_VERSION,
                    "event_id": uuid.uuid4().hex,
                    "timestamp": captured_at.isoformat(timespec="seconds"),
                    "kind": "transcript_snapshot_reconciled",
                    "runtime": runtime,
                    "session_id": session_id,
                    "hook_event": hook_event,
                    "archive_key": key,
                    "archive_path": existing.get("archive_path"),
                    "sha256": existing.get("sha256"),
                    "bytes": signature["bytes"],
                },
            )
            return "unchanged"

    verified_temp: Path | None = None
    try:
        verified_temp, signature, digest, secret_label = local_verified_copy(source)
        if secret_label is not None:
            manifest = {
                "schema": SCHEMA_VERSION,
                "runtime": runtime,
                "session_id": session_id,
                "archive_key": key,
                "native_format": "runtime-transcript-jsonl",
                "captured_at": captured_at.isoformat(timespec="seconds"),
                "hook_event": hook_event,
                "model": model,
                "source_signature": signature,
                "capture_status": "skipped_sensitive",
                "secret_kind": secret_label,
            }
            write_json_atomically(manifest_path, manifest)
            append_event(
                root,
                captured_at,
                {
                    "schema": SCHEMA_VERSION,
                    "event_id": uuid.uuid4().hex,
                    "timestamp": captured_at.isoformat(timespec="seconds"),
                    "kind": "transcript_snapshot_skipped_sensitive",
                    "runtime": runtime,
                    "session_id": session_id,
                    "hook_event": hook_event,
                    "archive_key": key,
                    "secret_kind": secret_label,
                },
            )
            return "skipped_sensitive"

        assert verified_temp is not None and digest is not None
        copy_verified_temp(verified_temp, destination)
        relative = destination.relative_to(root).as_posix()
        manifest = {
            "schema": SCHEMA_VERSION,
            "runtime": runtime,
            "session_id": session_id,
            "archive_key": key,
            "native_format": "runtime-transcript-jsonl",
            "captured_at": captured_at.isoformat(timespec="seconds"),
            "hook_event": hook_event,
            "model": model,
            "source_signature": signature,
            "capture_status": "current_snapshot",
            "archive_path": relative,
            "sha256": digest,
        }
        write_json_atomically(manifest_path, manifest)
        append_event(
            root,
            captured_at,
            {
                "schema": SCHEMA_VERSION,
                "event_id": uuid.uuid4().hex,
                "timestamp": captured_at.isoformat(timespec="seconds"),
                "kind": "transcript_snapshot",
                "runtime": runtime,
                "session_id": session_id,
                "hook_event": hook_event,
                "archive_key": key,
                "archive_path": relative,
                "sha256": digest,
                "bytes": signature["bytes"],
            },
        )
        return "snapshotted"
    except Exception:
        raise
    finally:
        if verified_temp is not None:
            verified_temp.unlink(missing_ok=True)


def capture_hook(runtime: str, payload: dict[str, Any]) -> None:
    if not replay_enabled():
        return
    if payload.get("hook_event_name") == "Stop" and payload.get("stop_hook_active"):
        return
    captured_at = now_local()
    root = archive_root()
    runtime_session_id = session_id(payload.get("session_id"))
    turn_id = optional_string(payload.get("turn_id"), max_length=256)
    hook_event = optional_string(payload.get("hook_event_name"), max_length=128)
    model = optional_string(payload.get("model"), max_length=256)
    prompt = payload.get("prompt")

    if isinstance(prompt, str) and prompt:
        redacted, redactions = redact_prompt(prompt)
        append_event(
            root,
            captured_at,
            {
                "schema": SCHEMA_VERSION,
                "event_id": uuid.uuid4().hex,
                "timestamp": captured_at.isoformat(timespec="seconds"),
                "kind": "user_prompt",
                "runtime": runtime,
                "session_id": runtime_session_id,
                "turn_id": turn_id,
                "hook_event": hook_event,
                "model": model,
                "text": redacted,
                "redactions": redactions,
            },
        )

    # Prompt journaling is the crash boundary. Copying the growing native
    # transcript here adds avoidable latency and I/O; completed-turn hooks
    # reconcile it after the agent has finished instead.
    if hook_event == "UserPromptSubmit":
        return

    if runtime_session_id is None:
        append_event(
            root,
            captured_at,
            {
                "schema": SCHEMA_VERSION,
                "event_id": uuid.uuid4().hex,
                "timestamp": captured_at.isoformat(timespec="seconds"),
                "kind": "transcript_snapshot_unavailable",
                "runtime": runtime,
                "session_id": None,
                "hook_event": hook_event,
                "reason": "session_id_missing",
            },
        )
        return
    source, unavailable_reason = trusted_transcript_path(
        runtime,
        payload.get("transcript_path"),
        cwd=payload.get("cwd"),
    )
    if source is None:
        append_event(
            root,
            captured_at,
            {
                "schema": SCHEMA_VERSION,
                "event_id": uuid.uuid4().hex,
                "timestamp": captured_at.isoformat(timespec="seconds"),
                "kind": "transcript_snapshot_unavailable",
                "runtime": runtime,
                "session_id": runtime_session_id,
                "hook_event": hook_event,
                "reason": unavailable_reason,
            },
        )
        return
    try:
        snapshot_transcript(
            root=root,
            runtime=runtime,
            session_id=runtime_session_id,
            source=source,
            captured_at=captured_at,
            hook_event=hook_event,
            model=model,
        )
    except OSError:
        append_event(
            root,
            captured_at,
            {
                "schema": SCHEMA_VERSION,
                "event_id": uuid.uuid4().hex,
                "timestamp": captured_at.isoformat(timespec="seconds"),
                "kind": "transcript_snapshot_failed",
                "runtime": runtime,
                "session_id": runtime_session_id,
                "hook_event": hook_event,
            },
        )


def command_hook(args: argparse.Namespace) -> int:
    if not replay_enabled():
        return 0
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError, ValueError):
        return 0
    if isinstance(data, dict):
        try:
            capture_hook(args.runtime, data)
        except OSError:
            pass
    return 0


def event_states(
    root: Path, selected_session_id: str | None
) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    events_root = root / "events"
    if not events_root.is_dir():
        return states
    for path in sorted(events_root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            current_session_id = event.get("session_id")
            if not isinstance(current_session_id, str):
                continue
            if (
                selected_session_id is not None
                and current_session_id != selected_session_id
            ):
                continue
            kind = event.get("kind")
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            state: dict[str, Any] | None = None
            if kind == "user_prompt":
                state = {"completeness": "prompt_only"}
            elif kind == "transcript_snapshot_unavailable":
                state = {
                    "completeness": "transcript_unavailable",
                    "reason": event.get("reason"),
                }
            elif kind == "transcript_snapshot_failed":
                state = {"completeness": "snapshot_failed"}
            elif kind == "transcript_snapshot_skipped_sensitive":
                state = {"completeness": "skipped_sensitive"}
            elif kind in {"transcript_snapshot", "transcript_snapshot_reconciled"}:
                state = {"completeness": "current_snapshot"}
            instant = timestamp_instant(timestamp)
            if instant is None:
                continue
            if state is not None and instant >= states.get(current_session_id, {}).get(
                "_instant", float("-inf")
            ):
                state["runtime"] = event.get("runtime")
                state["timestamp"] = timestamp
                state["_instant"] = instant
                states[current_session_id] = state
    return states


def timestamp_instant(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).timestamp()


def inspect_archive(
    root: Path, selected_session_id: str | None
) -> list[dict[str, Any]]:
    states = event_states(root, selected_session_id)
    by_session: dict[str, dict[str, Any]] = {
        item: {"session_id": item, **without_internal_fields(state)}
        for item, state in states.items()
    }
    manifests_root = root / "manifests"
    referenced_transcripts: set[Path] = set()
    for path in sorted(manifests_root.rglob("*.json")):
        manifest = load_manifest(path)
        if manifest is None:
            continue
        current_session_id = manifest.get("session_id")
        if not isinstance(current_session_id, str):
            continue
        if (
            selected_session_id is not None
            and current_session_id != selected_session_id
        ):
            continue
        row: dict[str, Any] = {
            "session_id": current_session_id,
            "runtime": manifest.get("runtime"),
            "archive_key": manifest.get("archive_key"),
            "captured_at": manifest.get("captured_at"),
            "capture_status": manifest.get("capture_status", "unknown"),
            "completeness": manifest.get("capture_status", "unknown"),
        }
        archive_path = manifest.get("archive_path")
        expected_hash = manifest.get("sha256")
        if row["capture_status"] == "current_snapshot":
            candidate = root / archive_path if isinstance(archive_path, str) else None
            try:
                valid_candidate = candidate is not None and is_under(
                    candidate.resolve(strict=True), root.resolve(strict=True)
                )
            except OSError:
                valid_candidate = False
            if not valid_candidate:
                row["completeness"] = "archive_missing"
            else:
                referenced_transcripts.add(candidate.resolve())
            if valid_candidate and (
                not isinstance(expected_hash, str) or not expected_hash
            ):
                row["completeness"] = "hash_metadata_missing"
            elif valid_candidate and file_sha256(candidate) != expected_hash:
                row["completeness"] = "hash_mismatch"
            elif valid_candidate:
                row["completeness"] = "current_snapshot"
                row["archive_path"] = archive_path

        current_state = states.get(current_session_id)
        captured_instant = timestamp_instant(manifest.get("captured_at"))
        if (
            current_state is not None
            and (
                captured_instant is None
                or current_state["_instant"] >= captured_instant
            )
            and current_state["completeness"] != "current_snapshot"
        ):
            row["last_verified_snapshot"] = row["completeness"]
            row["completeness"] = current_state["completeness"]
            if "reason" in current_state:
                row["reason"] = current_state["reason"]
        prior = by_session.get(current_session_id)
        prior_instant = timestamp_instant(prior.get("captured_at")) if prior else None
        if (
            prior is None
            or "captured_at" not in prior
            or captured_instant is None
            or (prior_instant is not None and captured_instant >= prior_instant)
        ):
            by_session[current_session_id] = row

    transcripts_root = root / "transcripts"
    if selected_session_id is None and transcripts_root.is_dir():
        for transcript in transcripts_root.rglob("*.jsonl"):
            try:
                resolved = transcript.resolve(strict=True)
            except OSError:
                continue
            if resolved in referenced_transcripts:
                continue
            runtime = transcript.parent.name
            by_session[f"orphan:{runtime}:{transcript.stem}"] = {
                "session_id": None,
                "runtime": runtime,
                "archive_key": transcript.stem,
                "completeness": "orphaned_archive",
                "archive_path": transcript.relative_to(root).as_posix(),
            }
    return sorted(by_session.values(), key=lambda item: str(item["session_id"]))


def without_internal_fields(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if not key.startswith("_")}


def command_inspect(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser() if args.root else archive_root()
    selected_session_id = optional_string(args.session_id)
    enabled, activation_source = replay_activation()
    result = {
        "schema": SCHEMA_VERSION,
        "archive_root": str(root),
        "activation": {"enabled": enabled, "source": activation_source},
        "sessions": inspect_archive(root, selected_session_id),
    }
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture a private replay snapshot from a runtime hook payload."
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)
    hook = sub.add_parser(
        "hook",
        help="Read a UserPromptSubmit, Stop, or SessionEnd hook payload from stdin.",
    )
    hook.add_argument("--runtime", required=True, choices=RUNTIMES)
    hook.set_defaults(func=command_hook)
    inspect = sub.add_parser(
        "inspect",
        help="List replay archives and verify their transcript hashes.",
    )
    inspect.add_argument("--session-id")
    inspect.add_argument("--root", help="Override the archive root for inspection.")
    inspect.set_defaults(func=command_inspect)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
