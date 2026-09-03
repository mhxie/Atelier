#!/usr/bin/env python3
"""Single-owner coordination for Atelier local routines.

Each machine keeps a random identity in the gitignored
``harness/routine_owner.local.toml`` file. The shared vault keeps the active
identity in ``$OV/_meta/routine_owner.toml``. When ``routine_watch.toml`` sets
``coordination.backend = "owner"``, only the matching machine may execute a
local routine.

The shared owner is a cooperative scheduler fence, not an access-control
boundary. Any machine with write access to the vault can explicitly transfer
ownership with ``claim --force --source-stopped`` after quiescing the source
scheduler. Ordinary launchd invocations cannot claim or change ownership.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import tempfile
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_IDENTITY = ROOT / "harness" / "routine_owner.local.toml"
OWNER_BACKEND = "owner"
SUPPORTED_BACKENDS = {"none", "dynamodb", OWNER_BACKEND}


class OwnershipError(RuntimeError):
    """Fail-closed ownership configuration error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ov_path() -> Path:
    raw = os.environ.get("OV")
    if not raw:
        raise OwnershipError("OV is not set")
    # Keep the configured spelling instead of resolving symlinks. On macOS,
    # launchd can block indefinitely while resolving a Google Drive File
    # Provider alias before it has opened any routine file. An absolute lexical
    # path is sufficient for this cooperative ownership fence.
    return Path(os.path.abspath(Path(raw).expanduser()))


def _watch_path() -> Path:
    return _ov_path() / "_meta" / "routine_watch.toml"


def _shared_owner_path() -> Path:
    override = os.environ.get("ATELIER_ROUTINE_OWNER_FILE")
    if override:
        return Path(os.path.abspath(Path(override).expanduser()))
    return _ov_path() / "_meta" / "routine_owner.toml"


def _local_identity_path() -> Path:
    override = os.environ.get("ATELIER_ROUTINE_IDENTITY_FILE")
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_LOCAL_IDENTITY


def _load_toml(path: Path, *, required: bool) -> dict[str, Any] | None:
    if not path.is_file():
        if required:
            raise OwnershipError(f"required file missing: {path}")
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise OwnershipError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OwnershipError(f"expected a TOML table in {path}")
    return data


def configured_backend() -> str:
    """Return the shared coordination backend without environment overrides."""
    data = _load_toml(_watch_path(), required=False)
    if data is None:
        return "none"
    coordination = data.get("coordination", {})
    if not isinstance(coordination, dict):
        raise OwnershipError("routine_watch.toml [coordination] must be a table")
    backend = coordination.get("backend", "none")
    if not isinstance(backend, str):
        raise OwnershipError("coordination.backend must be a string")
    backend = backend.lower()
    if backend not in SUPPORTED_BACKENDS:
        raise OwnershipError(f"unsupported coordination backend: {backend}")
    return backend


def coordination_backend() -> str:
    """Resolve coordination mode, preserving the shared owner fence.

    ``ATELIER_COORDINATION`` remains useful for none/dynamodb diagnostics, but
    it cannot downgrade a shared owner backend to none on another machine.
    """
    configured = configured_backend()
    if configured == OWNER_BACKEND:
        return configured
    explicit = os.environ.get("ATELIER_COORDINATION")
    if not explicit:
        return configured
    explicit = explicit.lower()
    if explicit not in SUPPORTED_BACKENDS:
        raise OwnershipError(f"unsupported ATELIER_COORDINATION value: {explicit}")
    return explicit


def _identity_record(data: dict[str, Any] | None, *, source: str) -> dict[str, Any] | None:
    if data is None:
        return None
    machine_id = data.get("machine_id") if source == "local" else data.get("owner_id")
    label_key = "machine_label" if source == "local" else "owner_label"
    label = data.get(label_key, "unknown")
    if not isinstance(machine_id, str) or not machine_id.strip():
        raise OwnershipError(f"{source} owner identity is missing its ID")
    if not isinstance(label, str) or not label.strip():
        label = "unknown"
    record: dict[str, Any] = {"id": machine_id, "label": label}
    if source == "shared":
        generation = data.get("generation", 1)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise OwnershipError("shared owner identity has an invalid generation")
        record["generation"] = generation
    return record


def local_identity(*, required: bool = False) -> dict[str, Any] | None:
    return _identity_record(
        _load_toml(_local_identity_path(), required=required),
        source="local",
    )


def shared_owner(*, required: bool = False) -> dict[str, Any] | None:
    return _identity_record(
        _load_toml(_shared_owner_path(), required=required),
        source="shared",
    )


def ownership_status() -> dict[str, Any]:
    backend = coordination_backend()
    if backend != OWNER_BACKEND:
        return {
            "coordination": backend,
            "enforced": False,
            "eligible": True,
        }

    owner = shared_owner(required=True)
    assert owner is not None
    identity = local_identity(required=False)
    eligible = identity is not None and identity["id"] == owner["id"]
    reason = "owner_match" if eligible else (
        "machine_identity_missing" if identity is None else "owned_by_another_machine"
    )
    return {
        "coordination": OWNER_BACKEND,
        "enforced": True,
        "eligible": eligible,
        "reason": reason,
        "owner_label": owner["label"],
        "generation": owner["generation"],
        "machine_label": identity["label"] if identity else None,
    }


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def ensure_local_identity(label: str | None = None) -> dict[str, Any]:
    existing = local_identity(required=False)
    if existing is not None:
        return existing
    machine_label = label or platform.node() or "unknown"
    machine_id = str(uuid.uuid4())
    content = (
        "version = 1\n"
        f"machine_id = {json.dumps(machine_id)}\n"
        f"machine_label = {json.dumps(machine_label)}\n"
        f"created_at = {json.dumps(_utc_now())}\n"
    )
    _atomic_write(_local_identity_path(), content)
    return {"id": machine_id, "label": machine_label}


def _shared_owner_content(identity: dict[str, Any], generation: int) -> str:
    return (
        "version = 2\n"
        f"owner_id = {json.dumps(identity['id'])}\n"
        f"owner_label = {json.dumps(identity['label'])}\n"
        f"generation = {generation}\n"
        f"transferred_at = {json.dumps(_utc_now())}\n"
    )


def _write_shared_owner(identity: dict[str, Any], generation: int) -> None:
    _atomic_write(_shared_owner_path(), _shared_owner_content(identity, generation))


def _owner_backend_content() -> tuple[Path, str]:
    path = _watch_path()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OwnershipError(f"cannot read {path}: {exc}") from exc

    section = re.search(
        r"(?ms)^\[coordination\]\s*$.*?(?=^\[[^\n]+\]\s*$|\Z)",
        content,
    )
    if section is None:
        raise OwnershipError("routine_watch.toml has no [coordination] table")
    block = section.group(0)
    replacement, count = re.subn(
        r"(?m)^(\s*)backend\s*=.*$",
        rf'\g<1>backend = "{OWNER_BACKEND}"  # transfer only after stopping source: scripts/routine_owner.py claim --force --source-stopped',
        block,
        count=1,
    )
    if count != 1:
        raise OwnershipError("[coordination] must contain exactly one quoted backend value")
    updated = content[: section.start()] + replacement + content[section.end() :]
    return path, updated


def _active_running_claims(stale_after_hours: float | None = None) -> list[Path]:
    """Claims still marked running.

    A claim can stay `running` forever after `kill -9` or power loss before the
    runner's EXIT trap; `stale_after_hours` lets an operator treat claims whose
    file has not been touched for that long as abandoned when transferring
    ownership. The default (None) keeps every running claim blocking.
    """
    runs_root = _ov_path() / "_meta" / "routine_runs"
    if not runs_root.is_dir():
        return []
    active: list[Path] = []
    for path in runs_root.glob("*/*.toml"):
        claim = _load_toml(path, required=True)
        if not claim or claim.get("status") != "running":
            continue
        if stale_after_hours is not None:
            try:
                age_hours = (time.time() - path.stat().st_mtime) / 3600
            except OSError:
                age_hours = 0.0
            if age_hours >= stale_after_hours:
                continue
        active.append(path)
    return active


def claim_here(
    *, force: bool, source_stopped: bool, label: str | None, stale_running_hours: float | None = None,
) -> dict[str, Any]:
    backend_before = configured_backend()
    watch_path, updated_watch = _owner_backend_content()
    identity = ensure_local_identity(label)
    previous = shared_owner(required=False)
    transferring = previous is not None and previous["id"] != identity["id"]
    if transferring and not force:
        raise OwnershipError(
            f"routines are owned by {previous['label']}; rerun with --force to transfer"
        )
    if transferring and not source_stopped:
        raise OwnershipError(
            "cross-machine transfer requires --source-stopped after unloading the old scheduler"
        )
    ownership_change = previous is None or transferring or backend_before != OWNER_BACKEND
    active = _active_running_claims(stale_running_hours)
    if ownership_change and active:
        raise OwnershipError(
            "cannot transfer ownership while a routine claim is running: "
            + ", ".join(str(path) for path in active)
            + " (pass --stale-running-hours N to ignore claims untouched for N hours)"
        )

    previous_generation = previous.get("generation", 1) if previous else 0
    generation = previous_generation + 1 if ownership_change else previous_generation
    owner_path = _shared_owner_path()
    previous_owner_content = owner_path.read_text(encoding="utf-8") if owner_path.is_file() else None

    # Publish a complete owner record before enabling the shared fence so no
    # runner can observe backend=owner with a missing owner file.
    _write_shared_owner(identity, generation)
    try:
        _atomic_write(watch_path, updated_watch)
    except OSError as exc:
        if previous_owner_content is not None:
            _atomic_write(owner_path, previous_owner_content)
        elif owner_path.exists():
            owner_path.unlink()
        raise OwnershipError(f"cannot enable owner backend: {exc}") from exc
    return {
        "claimed": True,
        "owner_label": identity["label"],
        "generation": generation,
        "transferred": transferring,
    }


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the single owner of local routines.")
    sub = parser.add_subparsers(dest="command", required=True)

    status_parser = sub.add_parser("status", help="Show whether this machine owns local routines.")
    status_parser.add_argument("--json", action="store_true")

    check_parser = sub.add_parser("check", help="Exit 0 for owner, 1 for another machine, 2 for error.")
    check_parser.add_argument("--json", action="store_true")

    claim_parser = sub.add_parser("claim", help="Transfer local-routine ownership to this machine.")
    claim_parser.add_argument("--force", action="store_true", help="Replace a different current owner.")
    claim_parser.add_argument(
        "--source-stopped",
        action="store_true",
        help="Assert the previous machine's routine plists are unloaded before transfer.",
    )
    claim_parser.add_argument("--label", help="Human-readable machine label; defaults to hostname.")
    claim_parser.add_argument(
        "--stale-running-hours",
        type=float,
        default=None,
        help="Treat `running` claims whose file is older than this as abandoned (after kill -9 or power loss).",
    )
    claim_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "claim":
            payload = claim_here(
                force=args.force,
                source_stopped=args.source_stopped,
                label=args.label,
                stale_running_hours=args.stale_running_hours,
            )
            _emit(payload, as_json=args.json)
            return 0

        payload = ownership_status()
        _emit(payload, as_json=args.json)
        if args.command == "check" and payload["enforced"] and not payload["eligible"]:
            return 1
        return 0
    except OwnershipError as exc:
        payload = {"error": str(exc)}
        _emit(payload, as_json=getattr(args, "json", False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
