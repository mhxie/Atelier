#!/usr/bin/env python3
"""Validate routine support declarations and resolve execution permissions.

Routine names and paths are private policy in ``$OV/_meta/routine_watch.toml``.
This script knows only the public capability-profile schema.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from routine_claim import validate_claim
from routine_prompt_guard import check as credential_lines
from routine_prompt_guard import structure_error

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "harness" / "routine_profiles.toml"
SUPPORT_SURFACES = {
    "local-only": {"local"},
    "hybrid": {"local", "cloud"},
    "cloud-only": {"cloud"},
}
EXECUTION_SURFACE = {"local": "local", "remote": "cloud", "cloud": "cloud"}
LOCAL_SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}
WEB_MODES = {"disabled", "live"}
USER_CONFIG_MODES = {"ignore", "required"}
SHELL_NETWORK_MODES = {"disabled", "enabled", "unrestricted"}
ATELIER_ACCESS_MODES = {"read", "read-write"}
FALLBACK_RUNTIMES = {"claude"}
REASONING_EFFORTS = {"low", "medium", "high"}
# External permission namespaces, and the subset that only reads. An unverified
# read degrades to collecting nothing, so it stays a warning. An unverified
# write is a preflight error: the first run that exercises the capability would
# also be the first time it takes an irreversible action outside $OV, unattended
# and with the prompt allowlist as the only thing bounding it.
EXTERNAL_PERMISSION_NAMESPACES = {"gmail", "readwise", "mail"}
EXTERNAL_READ_PERMISSIONS = {"gmail:read", "readwise:read"}

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SAFE_COMMAND = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._-]*$")
SAFE_PERMISSION = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")


class AuditError(Exception):
    """Configuration or system readiness error."""


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AuditError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"expected TOML table in {path}")
    return value


def _vault_root() -> Path:
    value = os.environ.get("OV", "").strip()
    if not value:
        raise AuditError("OV is not set")
    return Path(value).expanduser()


def _load_profiles() -> dict[str, dict[str, Any]]:
    document = _load_toml(PROFILE_PATH)
    if document.get("version") != 1:
        raise AuditError(f"unsupported routine profile version in {PROFILE_PATH}")
    profiles = document.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise AuditError(f"no profiles declared in {PROFILE_PATH}")
    return profiles


def _load_watch() -> tuple[Path, list[dict[str, Any]]]:
    path = _vault_root() / "_meta" / "routine_watch.toml"
    document = _load_toml(path)
    rows = document.get("routine")
    if not isinstance(rows, list):
        raise AuditError(f"expected [[routine]] rows in {path}")
    return path, rows


def _string_list(value: Any, field: str, profile_name: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise AuditError(
            f"profile {profile_name!r} field {field!r} must be a string array"
        )
    return value


def _profile_fingerprint(name: str, profile: dict[str, Any]) -> str:
    fields = {
        "name": name,
        "sandbox": profile.get("sandbox"),
        "atelier_access": profile.get("atelier_access"),
        "web_search": profile.get("web_search"),
        "shell_network": profile.get("shell_network"),
        "user_config": profile.get("user_config"),
        "timeout_seconds": profile.get("timeout_seconds"),
        "reasoning_effort": profile.get("reasoning_effort"),
        "permissions": profile.get("permissions"),
        "required_clis": profile.get("required_clis"),
        "required_plugins": profile.get("required_plugins"),
        "optional_plugins": profile.get("optional_plugins"),
        "allowed_commands": profile.get("allowed_commands"),
    }
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_profiles(profiles: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            errors.append(f"profile {name!r} must be a table")
            continue
        surface = profile.get("surface")
        if surface not in {"local", "cloud"}:
            errors.append(f"profile {name!r} has invalid surface {surface!r}")
            continue
        if profile.get("web_search") not in WEB_MODES:
            errors.append(f"profile {name!r} has invalid web_search")
        if profile.get("reasoning_effort") not in REASONING_EFFORTS:
            errors.append(f"profile {name!r} has invalid reasoning_effort")
        for field in ("permissions",):
            try:
                values = _string_list(profile.get(field), field, name)
                if any(not SAFE_PERMISSION.fullmatch(value) for value in values):
                    errors.append(f"profile {name!r} has invalid permissions")
            except AuditError as exc:
                errors.append(str(exc))
        if surface == "local":
            if profile.get("sandbox") not in LOCAL_SANDBOXES:
                errors.append(f"profile {name!r} has invalid sandbox")
            shell_network = profile.get("shell_network")
            if shell_network not in SHELL_NETWORK_MODES:
                errors.append(f"profile {name!r} has invalid shell_network")
            elif profile.get("sandbox") == "danger-full-access":
                if shell_network != "unrestricted":
                    errors.append(
                        f"profile {name!r} danger-full-access requires shell_network='unrestricted'"
                    )
            elif shell_network == "unrestricted":
                errors.append(
                    f"profile {name!r} shell_network='unrestricted' requires danger-full-access"
                )
            if profile.get("user_config") not in USER_CONFIG_MODES:
                errors.append(f"profile {name!r} has invalid user_config")
            atelier_access = profile.get("atelier_access")
            if atelier_access not in ATELIER_ACCESS_MODES:
                errors.append(f"profile {name!r} has invalid atelier_access")
            permissions = profile.get("permissions", [])
            if atelier_access == "read" and "atelier:read-write" in permissions:
                errors.append(
                    f"profile {name!r} read-only Atelier access conflicts with permissions"
                )
            if (
                atelier_access == "read-write"
                and "atelier:read-write" not in permissions
            ):
                errors.append(
                    f"profile {name!r} read-write Atelier access is not declared"
                )
            if (
                profile.get("sandbox") == "danger-full-access"
                and atelier_access != "read-write"
            ):
                errors.append(
                    f"profile {name!r} danger-full-access requires read-write Atelier access"
                )
            timeout_seconds = profile.get("timeout_seconds")
            if (
                not isinstance(timeout_seconds, int)
                or isinstance(timeout_seconds, bool)
                or not 30 <= timeout_seconds <= 14400
            ):
                errors.append(f"profile {name!r} has invalid timeout_seconds")
            for field in (
                "required_clis",
                "required_plugins",
                "optional_plugins",
                "allowed_commands",
            ):
                try:
                    values = _string_list(profile.get(field), field, name)
                    if field == "allowed_commands" and (
                        not values
                        or any(not SAFE_COMMAND.fullmatch(value) for value in values)
                    ):
                        errors.append(f"profile {name!r} has invalid allowed_commands")
                except AuditError as exc:
                    errors.append(str(exc))
            if (
                profile.get("required_plugins")
                and profile.get("user_config") == "ignore"
            ):
                errors.append(
                    f"profile {name!r} requires plugins but ignores user config"
                )
            fallback = profile.get("fallback_runtime")
            if fallback is not None:
                # A second runtime re-executes the whole cycle after the first
                # failed. That is only safe for profiles whose every effect is
                # an idempotent vault write: no Codex plugin the fallback cannot
                # load, no shell escape, no external send, no repo commit.
                if fallback not in FALLBACK_RUNTIMES:
                    errors.append(f"profile {name!r} has invalid fallback_runtime")
                if profile.get("required_plugins"):
                    errors.append(
                        f"profile {name!r} fallback_runtime conflicts with required_plugins"
                    )
                if profile.get("user_config") == "required":
                    errors.append(
                        f"profile {name!r} fallback_runtime conflicts with user_config='required'"
                    )
                if profile.get("sandbox") == "danger-full-access":
                    errors.append(
                        f"profile {name!r} fallback_runtime conflicts with danger-full-access"
                    )
                if profile.get("atelier_access") == "read-write":
                    errors.append(
                        f"profile {name!r} fallback_runtime conflicts with atelier read-write"
                    )
                unsafe = sorted(
                    p
                    for p in profile.get("permissions", [])
                    if isinstance(p, str)
                    and (
                        p.endswith(":send-self")
                        or p.endswith(":git-commit")
                        or p.endswith(":create-document")
                    )
                )
                if unsafe:
                    errors.append(
                        f"profile {name!r} fallback_runtime conflicts with external-write permissions: "
                        + ", ".join(unsafe)
                    )
        else:
            for field in ("required_connectors", "optional_connectors"):
                try:
                    _string_list(profile.get(field), field, name)
                except AuditError as exc:
                    errors.append(str(exc))
    return errors


def _routine_record(
    routine: dict[str, Any], profiles: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    name = routine.get("name")
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
        name = str(name or "<missing>")
        errors.append("invalid or missing routine name")

    execution = routine.get("execution", "remote")
    surface = EXECUTION_SURFACE.get(execution)
    if surface is None:
        errors.append(f"invalid execution {execution!r}")
    scheduler = routine.get("scheduler") if surface == "cloud" else None
    if surface == "cloud" and not isinstance(scheduler, str):
        warnings.append("active cloud execution has no scheduler identity")

    support = routine.get("support")
    allowed = SUPPORT_SURFACES.get(support)
    if allowed is None:
        errors.append("missing or invalid support (local-only, hybrid, cloud-only)")
        allowed = set()
    if surface and surface not in allowed:
        errors.append(
            f"execution surface {surface!r} conflicts with support {support!r}"
        )

    local_profile = routine.get("local_profile")
    cloud_profile = routine.get("cloud_profile")
    expected = {
        "local": local_profile,
        "cloud": cloud_profile,
    }
    for candidate_surface in ("local", "cloud"):
        profile_name = expected[candidate_surface]
        declared = candidate_surface in allowed
        if declared and not isinstance(profile_name, str):
            errors.append(f"support {support!r} requires {candidate_surface}_profile")
            continue
        if not declared and profile_name is not None:
            errors.append(
                f"{candidate_surface}_profile conflicts with support {support!r}"
            )
            continue
        if isinstance(profile_name, str):
            profile = profiles.get(profile_name)
            if not isinstance(profile, dict):
                errors.append(f"unknown {candidate_surface}_profile {profile_name!r}")
            elif profile.get("surface") != candidate_surface:
                errors.append(
                    f"profile {profile_name!r} has surface {profile.get('surface')!r}, "
                    f"expected {candidate_surface!r}"
                )

    selected_profile_name = expected.get(surface) if surface else None
    selected_profile = (
        profiles.get(selected_profile_name)
        if isinstance(selected_profile_name, str)
        else None
    )
    if surface == "cloud" and isinstance(selected_profile, dict):
        warnings.append(
            "cloud connector authentication is scheduler-managed and not locally verified"
        )

    support_matrix: dict[str, Any] = {}
    for candidate_surface, profile_name in expected.items():
        profile = profiles.get(profile_name) if isinstance(profile_name, str) else None
        if candidate_surface not in allowed or not isinstance(profile, dict):
            continue
        requirement = {
            "profile": profile_name,
            "permissions": list(profile.get("permissions", [])),
            "web_search": profile.get("web_search"),
            "reasoning_effort": profile.get("reasoning_effort"),
        }
        if candidate_surface == "local":
            requirement.update(
                {
                    "sandbox": profile.get("sandbox"),
                    "atelier_access": profile.get("atelier_access"),
                    "shell_network": profile.get("shell_network"),
                    "user_config": profile.get("user_config"),
                    "timeout_seconds": profile.get("timeout_seconds"),
                    "required_clis": list(profile.get("required_clis", [])),
                    "required_plugins": list(profile.get("required_plugins", [])),
                    "optional_plugins": list(profile.get("optional_plugins", [])),
                    "allowed_commands": list(profile.get("allowed_commands", [])),
                }
            )
        else:
            requirement.update(
                {
                    "required_connectors": list(profile.get("required_connectors", [])),
                    "optional_connectors": list(profile.get("optional_connectors", [])),
                }
            )
        support_matrix[candidate_surface] = requirement

    prompt_archive = _vault_root() / "_routine_prompts" / f"{name}.md"
    cloud_capable = "cloud" in allowed
    prompt_error: str | None = None
    if cloud_capable and prompt_archive.is_file():
        if support == "hybrid":
            prompt_error = structure_error(prompt_archive)
        if prompt_error is None:
            findings = credential_lines(prompt_archive)
            if findings:
                prompt_error = "literal credential detected at line(s): " + ", ".join(
                    str(line) for line in findings
                )
    chatgpt_scheduled = surface == "cloud" and scheduler == "chatgpt-scheduled"
    cloud_migration = {
        "capable": cloud_capable,
        "active": surface == "cloud",
        "current_scheduler": scheduler,
        "chatgpt_scheduled": chatgpt_scheduled,
        "prompt_archive": str(prompt_archive),
        "prompt_archived": prompt_archive.is_file(),
        "prompt_valid": prompt_archive.is_file() and prompt_error is None,
        "management_surface": "chatgpt-web-or-mobile",
        "connector_auth": "scheduler-managed-unverified"
        if cloud_capable
        else "not-applicable",
        "blockers": [],
    }
    if cloud_capable and not prompt_archive.is_file():
        cloud_migration["blockers"].append("private prompt archive missing")
    elif prompt_error is not None:
        cloud_migration["blockers"].append(
            f"private prompt archive invalid: {prompt_error}"
        )
    if cloud_capable and not chatgpt_scheduled:
        cloud_migration["blockers"].append(
            "create and first-run-test task in ChatGPT Scheduled"
        )
    if surface == "cloud" and isinstance(scheduler, str) and not chatgpt_scheduled:
        cloud_migration["blockers"].append(
            f"disable existing {scheduler} trigger after ChatGPT Scheduled first-run"
        )

    return {
        "name": name,
        "label": routine.get("label"),
        "cron": routine.get("cron"),
        "execution": execution,
        "scheduler": scheduler,
        "surface": surface,
        "support": support,
        "local_profile": local_profile,
        "cloud_profile": cloud_profile,
        "selected_profile": selected_profile_name,
        "permissions": list(selected_profile.get("permissions", []))
        if isinstance(selected_profile, dict)
        else [],
        "support_matrix": support_matrix,
        "cloud_migration": cloud_migration,
        "errors": errors,
        "warnings": warnings,
    }


def _installed_codex_plugins() -> tuple[set[str], str | None]:
    try:
        result = subprocess.run(
            ["codex", "plugin", "list"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return set(), str(exc)
    if result.returncode != 0:
        return set(), (result.stderr or result.stdout).strip()
    installed: set[str] = set()
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) >= 3 and columns[1:3] == ["installed,", "enabled"]:
            installed.add(columns[0])
    return installed, None


def _loaded_launchd_labels(labels: set[str]) -> tuple[set[str], str | None]:
    if sys.platform != "darwin" or shutil.which("launchctl") is None:
        return set(), "launchd unavailable on this platform"
    loaded: set[str] = set()
    domain = f"gui/{os.getuid()}"
    for label in labels:
        result = subprocess.run(
            ["launchctl", "print", f"{domain}/{label}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            loaded.add(label)
    return loaded, None


def _plists_by_routine(routine_names: set[str]) -> dict[str, dict[str, Any]]:
    """Map each routine to the parsed plist that invokes it."""
    candidates = list((ROOT / "scripts" / "launchd").glob("*.plist"))
    private_dir = _vault_root() / "_meta" / "launchd"
    if private_dir.is_dir():
        candidates.extend(private_dir.glob("*.plist"))
    found: dict[str, dict[str, Any]] = {}
    for path in candidates:
        try:
            with path.open("rb") as handle:
                plist = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            continue
        label = plist.get("Label")
        arguments = plist.get("ProgramArguments", [])
        if not isinstance(label, str) or not isinstance(arguments, list):
            continue
        invocations: list[list[str]] = [[str(item) for item in arguments]]
        for item in arguments:
            if not isinstance(item, str):
                continue
            try:
                invocations.append(shlex.split(item))
            except ValueError:
                continue
        for invocation in invocations:
            for index, token in enumerate(invocation[:-1]):
                if Path(token).name != "routine_runner.sh":
                    continue
                routine_name = invocation[index + 1]
                if routine_name in routine_names:
                    found[routine_name] = plist
    return found


def _plist_labels_by_routine(routine_names: set[str]) -> dict[str, str]:
    return {
        name: str(plist["Label"])
        for name, plist in _plists_by_routine(routine_names).items()
    }


def plist_recovery(plist: dict[str, Any]) -> str:
    """Classify how a plist recovers a cycle it did not complete.

    This matters more than it looks. `routine_runner.sh` defers a cycle it
    cannot start yet (readiness, contention, a machine that was asleep), and a
    deferred cycle waits for "the next trigger". For a plist that fires once a
    week, the next trigger is a week away, so one deferral costs a full cycle.
    That is the shape of an intermittent hit rate: the scheduler is firing
    exactly as configured and the output still goes missing.

    Two things restore a missed cycle, and the runner's schedule gate makes
    both cheap because it exits immediately for completed, fenced, and
    not-yet-due claims:

      hourly       a StartCalendarInterval with no Hour key is a launchd
                   wildcard firing every hour (also StartInterval, or an
                   explicit list dense enough to cover the day)
      run-at-load  covers login and LaunchAgent reload after a missed event

    Returns "none" when neither is present, which is a standing risk rather
    than a failure: nothing is broken until a cycle is missed.
    """
    marks: list[str] = []
    interval = plist.get("StartCalendarInterval")
    if isinstance(interval, dict) and "Hour" not in interval:
        marks.append("hourly")
    elif isinstance(interval, list) and len(interval) >= 12:
        marks.append("hourly")
    elif plist.get("StartInterval"):
        marks.append("hourly")
    if plist.get("RunAtLoad") is True:
        marks.append("run-at-load")
    return "+".join(marks) if marks else "none"


def _background_evidence(
    records: list[dict[str, Any]], profiles: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    runs_root = _vault_root() / "_meta" / "routine_runs"
    smokes_root = _vault_root() / "_meta" / "routine_profile_smokes"
    permission_smokes_root = _vault_root() / "_meta" / "routine_permission_smokes"
    machine = os.uname().nodename
    latest_completed: dict[str, Any] | None = None
    latest_profile_smoke: dict[str, Any] | None = None
    real_run_profiles: set[str] = set()
    profile_smoke_profiles: set[str] = set()
    for record in records:
        routine_dir = runs_root / record["name"]
        if not routine_dir.is_dir():
            continue
        for path in routine_dir.glob("*.toml"):
            try:
                claim = _load_toml(path)
                validate_claim(
                    claim,
                    routine=record["name"],
                    cycle=path.stem,
                    allow_legacy_owner_generation=True,
                )
            except (AuditError, ValueError):
                continue
            if (
                claim.get("machine") != machine
                or claim.get("status") != "completed"
                or claim.get("contract_version") != 2
                or claim.get("runtime") != "codex"
                or not isinstance(claim.get("profile"), str)
            ):
                continue
            profile = claim.get("profile")
            current_profile = (
                profiles.get(profile) if isinstance(profile, str) else None
            )
            if not isinstance(current_profile, dict) or claim.get(
                "profile_fingerprint"
            ) != _profile_fingerprint(profile, current_profile):
                continue
            if isinstance(profile, str):
                real_run_profiles.add(profile)
            completed_at = claim.get("completed_at")
            candidate = {
                "routine": record["name"],
                "profile": profile,
                "runtime": claim.get("runtime"),
                "completed_at": completed_at,
                "claim": str(path),
            }
            if latest_completed is None or str(completed_at or "") > str(
                latest_completed.get("completed_at") or ""
            ):
                latest_completed = candidate

    if smokes_root.is_dir():
        for path in smokes_root.glob("*/*.toml"):
            try:
                claim = _load_toml(path)
            except AuditError:
                continue
            if (
                claim.get("machine") != machine
                or claim.get("status") != "completed"
                or claim.get("kind") != "runtime-envelope"
                or claim.get("contract_version") != 2
                or claim.get("runtime") != "codex"
                or claim.get("connector_access") != "not-exercised"
                or claim.get("approval_policy") != "never"
                or not isinstance(claim.get("profile"), str)
            ):
                continue
            launcher = claim.get("launcher")
            if not isinstance(launcher, str) or not launcher.startswith(
                "com.atelier.profile-smoke."
            ):
                continue
            profile = claim["profile"]
            current_profile = profiles.get(profile)
            if not isinstance(current_profile, dict) or claim.get(
                "profile_fingerprint"
            ) != _profile_fingerprint(profile, current_profile):
                continue
            profile_smoke_profiles.add(profile)
            completed_at = claim.get("completed_at")
            candidate = {
                "routine": claim.get("routine"),
                "profile": profile,
                "runtime": "codex",
                "completed_at": completed_at,
                "claim": str(path),
                "connector_access": "not-exercised",
                "approval_policy": "never",
                "launcher": launcher,
            }
            if latest_profile_smoke is None or str(completed_at or "") > str(
                latest_profile_smoke.get("completed_at") or ""
            ):
                latest_profile_smoke = candidate
    required_profiles = {
        record["selected_profile"]
        for record in records
        if isinstance(record.get("selected_profile"), str)
    }
    external_permissions_required: dict[str, set[str]] = {}
    for record in records:
        profile = record.get("selected_profile")
        if not isinstance(profile, str):
            continue
        permissions = [
            value
            for value in record.get("permissions", [])
            if isinstance(value, str)
            and value.split(":", 1)[0] in EXTERNAL_PERMISSION_NAMESPACES
        ]
        if permissions:
            external_permissions_required.setdefault(profile, set()).update(permissions)

    external_permissions_exercised: dict[str, set[str]] = {}
    latest_permission_smoke: dict[str, Any] | None = None
    record_profiles = {
        record["name"]: record.get("selected_profile")
        for record in records
        if isinstance(record.get("name"), str)
    }
    now = datetime.now(timezone.utc)
    if permission_smokes_root.is_dir():
        for path in permission_smokes_root.glob("*/*.toml"):
            try:
                claim = _load_toml(path)
            except AuditError:
                continue
            profile = claim.get("profile")
            permission = claim.get("permission")
            routine = claim.get("routine")
            current_profile = (
                profiles.get(profile) if isinstance(profile, str) else None
            )
            if (
                claim.get("machine") != machine
                or claim.get("status") != "completed"
                or claim.get("kind") != "external-permission"
                or claim.get("contract_version") != 1
                or claim.get("runtime") != "codex"
                or claim.get("approval_policy") != "never"
                or claim.get("user_authorized") is not True
                or claim.get("verification") != "model-reported"
                or not isinstance(profile, str)
                or not isinstance(permission, str)
                or not isinstance(routine, str)
                or record_profiles.get(routine) != profile
                or permission not in external_permissions_required.get(profile, set())
                or not isinstance(current_profile, dict)
                or claim.get("sandbox") != current_profile.get("sandbox")
                or claim.get("web_search") != current_profile.get("web_search")
                or claim.get("shell_network") != current_profile.get("shell_network")
                or claim.get("user_config") != current_profile.get("user_config")
                or claim.get("atelier_access") != current_profile.get("atelier_access")
                or claim.get("profile_fingerprint")
                != _profile_fingerprint(profile, current_profile)
            ):
                continue
            launcher = claim.get("launcher")
            if not isinstance(launcher, str) or not launcher.startswith(
                "com.atelier.permission-smoke."
            ):
                continue
            completed_at = claim.get("completed_at")
            if not isinstance(completed_at, str):
                continue
            try:
                completed = datetime.fromisoformat(completed_at).astimezone(
                    timezone.utc
                )
            except ValueError:
                continue
            if completed > now + timedelta(hours=1) or now - completed > timedelta(
                days=30
            ):
                continue
            expected_mutation = {
                "gmail:read": "read-only",
                "readwise:read": "read-only",
                "readwise:create-document": "idempotent-test-write",
                # Sending cannot be read-only or idempotent. It is verifiable
                # only by actually sending, so the class is bounded instead:
                # one message, to the authenticated account itself, from an
                # explicitly authorized smoke. That is the honest floor for a
                # capability an unattended routine will otherwise first
                # exercise on real content.
                "gmail:send-self": "self-directed-write",
                "mail:send-self": "self-directed-write",
            }.get(permission)
            if claim.get("mutation_mode") != expected_mutation:
                continue
            external_permissions_exercised.setdefault(profile, set()).add(permission)
            candidate = {
                "routine": routine,
                "profile": profile,
                "permission": permission,
                "runtime": "codex",
                "completed_at": completed_at,
                "claim": str(path),
                "launcher": launcher,
                "mutation_mode": expected_mutation,
                "verification": "model-reported",
            }
            if latest_permission_smoke is None or completed_at > str(
                latest_permission_smoke.get("completed_at") or ""
            ):
                latest_permission_smoke = candidate

    external_permissions_unverified = {
        profile: sorted(required - external_permissions_exercised.get(profile, set()))
        for profile, required in external_permissions_required.items()
        if required - external_permissions_exercised.get(profile, set())
    }
    runtime_verified_profiles = real_run_profiles | profile_smoke_profiles
    return {
        "verified": latest_completed is not None,
        "all_profiles_verified": required_profiles <= runtime_verified_profiles,
        "all_runtime_profiles_verified": required_profiles <= runtime_verified_profiles,
        "machine": machine,
        "required_profiles": sorted(required_profiles),
        "verified_profiles": sorted(runtime_verified_profiles),
        "real_run_profiles": sorted(real_run_profiles),
        "profile_smoke_profiles": sorted(profile_smoke_profiles),
        "unverified_profiles": sorted(required_profiles - runtime_verified_profiles),
        "external_permissions_required": {
            profile: sorted(permissions)
            for profile, permissions in sorted(external_permissions_required.items())
        },
        "external_permissions_exercised": {
            profile: sorted(permissions)
            for profile, permissions in sorted(external_permissions_exercised.items())
        },
        "external_permissions_unverified": external_permissions_unverified,
        "latest_completed": latest_completed,
        "latest_profile_smoke": latest_profile_smoke,
        "latest_permission_smoke": latest_permission_smoke,
    }


def _split_unverified_external(
    unverified: dict[str, list[str]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Separate unverified external permissions into reads and writes.

    Anything outside EXTERNAL_READ_PERMISSIONS counts as a write, so a new verb
    is treated as dangerous until someone classifies it. The alternative default
    would let an unrecognized capability through as a warning, which is the
    failure this split exists to prevent.
    """
    reads: dict[str, list[str]] = {}
    writes: dict[str, list[str]] = {}
    for profile, permissions in unverified.items():
        profile_reads = [p for p in permissions if p in EXTERNAL_READ_PERMISSIONS]
        profile_writes = [p for p in permissions if p not in EXTERNAL_READ_PERMISSIONS]
        if profile_reads:
            reads[profile] = profile_reads
        if profile_writes:
            writes[profile] = profile_writes
    return reads, writes


def _format_permission_map(by_profile: dict[str, list[str]]) -> str:
    return ", ".join(
        f"{profile} ({', '.join(permissions)})"
        for profile, permissions in sorted(by_profile.items())
    )


def _system_checks(
    records: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    runtime_override: str | None = None,
    smoke_permission: str | None = None,
) -> dict[str, Any]:
    local_records = [record for record in records if record["surface"] == "local"]
    required_clis: set[str] = set()
    required_plugins: set[str] = set()
    optional_plugins: set[str] = set()
    for record in local_records:
        profile = profiles.get(record.get("selected_profile"), {})
        required_clis.update(profile.get("required_clis", []))
        required_plugins.update(profile.get("required_plugins", []))
        optional_plugins.update(profile.get("optional_plugins", []))

    runtime_error = None
    if runtime_override is not None:
        runtime = runtime_override
    else:
        runtime = "codex"
    if runtime:
        required_clis.add(runtime)
    if local_records and sys.platform == "darwin":
        required_clis.add("caffeinate")
    cli_status = {name: shutil.which(name) for name in sorted(required_clis)}
    plugins, plugin_error = (
        _installed_codex_plugins()
        if required_plugins or optional_plugins
        else (set(), None)
    )
    local_names = {record["name"] for record in local_records}
    plists = _plists_by_routine(local_names)
    plist_labels = {name: str(plist["Label"]) for name, plist in plists.items()}
    loaded_labels, launchd_error = _loaded_launchd_labels(set(plist_labels.values()))
    launchd = {
        name: {
            "label": plist_labels.get(name),
            "loaded": bool(plist_labels.get(name) in loaded_labels),
            "recovery": plist_recovery(plists[name]) if name in plists else None,
        }
        for name in sorted(local_names)
    }
    background = _background_evidence(local_records, profiles)

    owner_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "routine_owner.py"),
            "status",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    owner = None
    owner_error = None
    if owner_result.returncode == 0:
        try:
            owner = json.loads(owner_result.stdout)
        except json.JSONDecodeError as exc:
            owner_error = str(exc)
    else:
        owner_error = (owner_result.stderr or owner_result.stdout).strip()

    errors: list[str] = []
    warnings: list[str] = []
    missing_clis = [name for name, path in cli_status.items() if path is None]
    missing_plugins = sorted(required_plugins - plugins)
    missing_plists = [name for name, value in launchd.items() if not value["label"]]
    unloaded = [
        name
        for name, value in launchd.items()
        if value["label"] and not value["loaded"]
    ]
    if missing_clis:
        errors.append(f"missing required CLIs: {', '.join(missing_clis)}")
    if plugin_error:
        errors.append(f"cannot inspect Codex plugins: {plugin_error}")
    elif missing_plugins:
        errors.append(f"missing required Codex plugins: {', '.join(missing_plugins)}")
    if runtime != "codex":
        errors.append(
            f"unattended local routines require codex; selected runtime is {runtime or 'unresolved'}"
        )
    if runtime_error:
        errors.append(f"runtime resolution failed: {runtime_error}")
    if owner_error:
        errors.append(f"owner status failed: {owner_error}")
    elif not owner or not owner.get("eligible"):
        errors.append("this machine is not the active local routine owner")
    if missing_plists:
        errors.append(
            f"local routines without launchd plists: {', '.join(missing_plists)}"
        )
    no_recovery = sorted(
        name for name, value in launchd.items() if value["recovery"] == "none"
    )
    if no_recovery:
        # A warning, not an error: nothing is broken until a cycle is missed,
        # and every one of these routines works on a machine that happens to be
        # awake. It earns a line because the cost is invisible at the moment it
        # is paid -- the scheduler fires as configured and the output is simply
        # never produced.
        warnings.append(
            f"launchd jobs with no way to recover a missed cycle ({len(no_recovery)}): "
            + ", ".join(no_recovery)
        )
    if unloaded:
        errors.append(f"local routine launchd jobs not loaded: {', '.join(unloaded)}")
    unavailable_optional = sorted(optional_plugins - plugins)
    if unavailable_optional:
        warnings.append(
            f"optional Codex plugins unavailable: {', '.join(unavailable_optional)}"
        )
    if launchd_error:
        errors.append(f"cannot inspect launchd: {launchd_error}")
    if not background["verified"]:
        warnings.append(
            "no completed launchd claim from this machine; background macOS file permissions remain unverified"
        )
    elif background["unverified_profiles"]:
        warnings.append(
            "background runtime smoke missing capability profiles: "
            + ", ".join(background["unverified_profiles"])
        )
    unverified_external = background["external_permissions_unverified"]
    if unverified_external:
        unverified_reads, unverified_writes = _split_unverified_external(
            unverified_external
        )
        if unverified_reads:
            warnings.append(
                "external read permissions not exercised or stale: "
                + _format_permission_map(unverified_reads)
            )
        if smoke_permission:
            # The run that verifies a permission cannot require that permission
            # to already be verified. Without this the gate deadlocks: the smoke
            # preflights through this same resolve, and a write capability is
            # unverified until precisely the smoke that is being blocked runs.
            #
            # Exactly one permission is exempted, and only on the smoke path.
            # routine_runner.sh never passes it, so real execution still fails
            # closed on an unverified write.
            for profile, permissions in list(unverified_writes.items()):
                remaining = [p for p in permissions if p != smoke_permission]
                if remaining:
                    unverified_writes[profile] = remaining
                else:
                    del unverified_writes[profile]
            warnings.append(
                f"verifying {smoke_permission}; its unverified state is exempt for this run"
            )
        if unverified_writes:
            # Fail closed. A write capability that has never been exercised
            # must not first be exercised by an unattended run.
            errors.append(
                "external write permissions not exercised or stale: "
                + _format_permission_map(unverified_writes)
            )

    return {
        "ready": not errors,
        "runtime": runtime,
        "owner": owner,
        "clis": cli_status,
        "plugins": {
            "installed_enabled": sorted(plugins),
            "required": sorted(required_plugins),
            "optional": sorted(optional_plugins),
        },
        "launchd": launchd,
        "background": background,
        "errors": errors,
        "warnings": warnings,
    }


def _plist_fire_dates(plist: dict[str, Any], start: date, end: date) -> set[date]:
    """Local dates in [start, end] on which this plist's calendar entries fire."""
    interval = plist.get("StartCalendarInterval")
    entries = (
        interval
        if isinstance(interval, list)
        else ([interval] if isinstance(interval, dict) else [])
    )
    fires: set[date] = set()
    day = start
    while day <= end:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if "Weekday" in entry and (day.weekday() + 1) % 7 != entry["Weekday"]:
                continue
            if "Day" in entry and day.day != entry["Day"]:
                continue
            if "Month" in entry and day.month != entry["Month"]:
                continue
            fires.add(day)
            break
        day += timedelta(days=1)
    return fires


def schedule_disagreements(
    routines: list[dict[str, Any]],
    plists: dict[str, dict[str, Any]],
    *,
    today: date | None = None,
) -> dict[str, list[str]]:
    """Routines whose plist fires on days their declared cron does not claim.

    This is the precondition for letting the declared cron gate execution. Once
    `routine_claim.select_scheduled_cycle` consults the cron, a plist firing on
    a day the cron does not declare means the invocation is skipped and that
    run is simply lost.

    Only that direction is reported. The opposite -- a cron declaring a day the
    plist never fires on -- costs nothing, because nothing runs on that day
    today either.

    Plists that can recover a missed cycle are exempt, and the exemption is the
    point rather than a loophole. This check existed to prove the cron gate was
    safe to wire; once wired, an hourly plist firing on an unclaimed day is the
    intended design and the selector skips it. Flagging that would report the
    fix as the fault. What still matters is a single-shot plist, where the plist
    remains the effective schedule and a disagreement really does lose a run.
    """
    import cron_spec

    end = (today or date.today()) - timedelta(days=1)
    start = end - timedelta(days=89)
    now = datetime.combine(end, datetime.min.time()).astimezone() + timedelta(
        hours=23
    )
    findings: dict[str, list[str]] = {}
    for row in routines:
        name = str(row.get("name") or "")
        plist = plists.get(name)
        if not name or plist is None:
            continue
        if plist_recovery(plist) != "none":
            continue
        cron = row.get("cron")
        if not isinstance(cron, str) or not cron_spec.is_evaluable(cron):
            continue
        declared = {
            day
            for day in cron_spec.scheduled_dates(cron, start, now)
            if start <= day <= end
        }
        fires = _plist_fire_dates(plist, start, end)
        unclaimed = sorted(fires - declared)
        if unclaimed:
            findings[name] = [day.isoformat() for day in unclaimed[:5]]
    return findings


def _latest_file_date(directory: Path, pattern: str) -> str:
    """Newest ISO date appearing in a filename under `directory`."""
    if not directory.is_dir():
        return ""
    dates = [
        match.group(0)
        for path in directory.glob(pattern or "*.md")
        if (match := re.search(r"\d{4}-\d{2}-\d{2}", path.name))
    ]
    return max(dates) if dates else ""


def _latest_claim(runs_dir: Path) -> tuple[str, str, str]:
    """(cycle, status, why) of the newest claim for one routine.

    `why` is the claim's own account of a failure. It is the difference between
    knowing a routine failed and knowing what to fix, and it exists on the claim
    precisely because the fuller transcript goes to a log that does not survive.
    """
    if not runs_dir.is_dir():
        return "", "", ""
    newest = ""
    status = ""
    why = ""
    for path in runs_dir.glob("*.toml"):
        if path.stem <= newest:
            continue
        try:
            claim = _load_toml(path)
        except Exception:
            continue
        newest = path.stem
        status = str(claim.get("status") or "")
        detail = str(claim.get("error_detail") or "")
        error = str(claim.get("error") or "")
        why = f"{error}: {detail}" if error and detail else (detail or error)
    return newest, status, why


def _latest_failure(failures_dir: Path) -> tuple[str, str]:
    """(recorded_at, phase) of the newest failure diagnostic for one routine.

    These files are written by `routine_runner.sh` for failures that happen
    before a claim exists (owner probe, capability preflight, lock acquire), so
    they are the only record of that entire class. Nothing read them before
    this command.
    """
    if not failures_dir.is_dir():
        return "", ""
    newest_path = None
    for path in failures_dir.glob("*.toml"):
        if newest_path is None or path.name > newest_path.name:
            newest_path = path
    if newest_path is None:
        return "", ""
    try:
        record = _load_toml(newest_path)
    except Exception:
        return newest_path.stem, "unreadable"
    return str(record.get("recorded_at") or "")[:10], str(record.get("phase") or "")


def _health() -> tuple[dict[str, Any], int]:
    """One table answering "what is actually broken", from all four sources.

    Diagnosis previously meant correlating routine_watch.toml, the output
    directories, the claim files, and the failure diagnostics by hand, per
    routine. With ~20 routines that is the wrong amount of work to do at the
    moment something has already gone wrong.
    """
    import cues  # local import: only this subcommand needs it

    ov = _vault_root()
    _watch_path, routines = _load_watch()
    runs_root = ov / "_meta" / "routine_runs"
    failures_root = ov / "_meta" / "routine_failures"
    # Derive execution surface exactly as the audit does. A local heuristic here
    # would disagree with `audit --check-system` about which routines need a
    # plist at all, and two tools contradicting each other about the same
    # routine is worse than either being silent.
    profiles = _load_profiles()
    local_names: set[str] = set()
    for row in routines:
        try:
            record = _routine_record(row, profiles)
        except Exception:
            continue
        if record.get("surface") == "local" and record.get("name"):
            local_names.add(str(record["name"]))
    plists = _plists_by_routine(local_names)

    rows: list[dict[str, Any]] = []
    for row in routines:
        name = str(row.get("name") or "")
        if not name:
            continue
        cron = str(row.get("cron") or "")
        output_dir = str(row.get("output_dir") or "")
        cycle, status, why = _latest_claim(runs_root / name)
        failed_at, failed_phase = _latest_failure(failures_root / name)
        plist = plists.get(name)
        rows.append(
            {
                "routine": name,
                "cadence_days": cues._estimate_cadence_days(cron) if cron else None,
                "last_output": (
                    _latest_file_date(ov / output_dir, str(row.get("file_pattern") or ""))
                    if output_dir
                    else ""
                ),
                "last_cycle": cycle,
                "last_status": status,
                "last_error": why,
                "last_failure": failed_at,
                "failure_phase": failed_phase,
                "recovery": plist_recovery(plist) if plist else ("" if name not in local_names else "no-plist"),
            }
        )
    rows.sort(key=lambda r: r["routine"])
    no_recovery = [r["routine"] for r in rows if r["recovery"] == "none"]
    disagreements = schedule_disagreements(routines, plists)
    return (
        {
            "ok": not no_recovery,
            "counts": {
                "routines": len(rows),
                "no_recovery": len(no_recovery),
                "with_failure_diagnostic": sum(1 for r in rows if r["last_failure"]),
                "schedule_disagreements": len(disagreements),
            },
            "schedule_disagreements": disagreements,
            "rows": rows,
        },
        0,
    )


def _health_text(payload: dict[str, Any]) -> str:
    rows = payload["rows"]
    if not rows:
        return "no routines declared"
    width = max(len(r["routine"]) for r in rows)
    lines = [
        f"{'routine'.ljust(width)}  {'cad':>4}  {'last output':11}  "
        f"{'last claim':22}  {'last failure':22}  recovery"
    ]
    for r in rows:
        cadence = f"{r['cadence_days']}d" if r["cadence_days"] else "-"
        claim = f"{r['last_status'] or '-'} {r['last_cycle'] or ''}".strip()
        failure = (
            f"{r['failure_phase']} {r['last_failure']}".strip()
            if r["last_failure"]
            else "-"
        )
        lines.append(
            f"{r['routine'].ljust(width)}  {cadence:>4}  {r['last_output'] or '-':11}  "
            f"{claim[:22]:22}  {failure[:22]:22}  {r['recovery'] or '-'}"
        )
    counts = payload["counts"]
    lines.append("")
    lines.append(
        f"{counts['routines']} routines; {counts['no_recovery']} cannot recover a "
        f"missed cycle; {counts['with_failure_diagnostic']} have a failure diagnostic on record"
    )
    failing = [r for r in rows if r["last_status"] == "failed"]
    if failing:
        lines.append("")
        lines.append("latest claim failed:")
        for r in failing:
            why = r["last_error"] or "(claim records no reason)"
            lines.append(f"  {r['routine']} {r['last_cycle']}")
            lines.append(f"      {why[:160]}")

    disagreements = payload.get("schedule_disagreements") or {}
    if disagreements:
        lines.append("")
        lines.append(
            "plist fires on days the declared cron does not claim "
            "(these runs would be lost once the cron gates execution):"
        )
        for name, days in sorted(disagreements.items()):
            lines.append(f"  {name}: {', '.join(days)}")
    return "\n".join(lines)


def _audit(check_system: bool) -> tuple[dict[str, Any], int]:
    profiles = _load_profiles()
    profile_errors = _validate_profiles(profiles)
    watch_path, routines = _load_watch()
    records = [_routine_record(row, profiles) for row in routines]
    errors = list(profile_errors)
    for record in records:
        errors.extend(f"{record['name']}: {message}" for message in record["errors"])
    system = (
        _system_checks(records, profiles)
        if check_system and not profile_errors
        else None
    )
    if system:
        errors.extend(system["errors"])
    counts = {
        "routines": len(records),
        "local_only": sum(record["support"] == "local-only" for record in records),
        "hybrid": sum(record["support"] == "hybrid" for record in records),
        "cloud_only": sum(record["support"] == "cloud-only" for record in records),
        "execution_local": sum(record["surface"] == "local" for record in records),
        "execution_cloud": sum(record["surface"] == "cloud" for record in records),
        "cloud_capable": sum(
            record["cloud_migration"]["capable"] for record in records
        ),
        "cloud_prompt_archived": sum(
            record["cloud_migration"]["capable"]
            and record["cloud_migration"]["prompt_archived"]
            for record in records
        ),
    }
    payload = {
        "ok": not errors,
        "watch_path": str(watch_path),
        "profiles_path": str(PROFILE_PATH),
        "counts": counts,
        "errors": errors,
        "routines": records,
    }
    if system is not None:
        payload["system"] = system
    return payload, 0 if not errors else 2


def _resolve(
    name: str,
    surface: str,
    check_system: bool,
    output_format: str,
    runtime: str | None,
    command: str | None,
    smoke_permission: str | None = None,
) -> int:
    if not SAFE_NAME.fullmatch(name):
        raise AuditError(f"invalid routine name: {name}")
    profiles = _load_profiles()
    profile_errors = _validate_profiles(profiles)
    if profile_errors:
        raise AuditError("; ".join(profile_errors))
    _, routines = _load_watch()
    matches = [row for row in routines if row.get("name") == name]
    if len(matches) != 1:
        raise AuditError(
            f"expected exactly one routine named {name!r}, found {len(matches)}"
        )
    record = _routine_record(matches[0], profiles)
    if record["errors"]:
        raise AuditError("; ".join(record["errors"]))
    if surface not in SUPPORT_SURFACES[record["support"]]:
        raise AuditError(f"routine {name!r} does not support {surface} execution")
    profile_name = matches[0].get(f"{surface}_profile")
    profile = profiles[profile_name]
    command_name = command.split(" ", 1)[0] if command else None
    if surface == "local" and command_name is not None:
        if not SAFE_COMMAND.fullmatch(command_name):
            raise AuditError(f"invalid scheduled command: {command_name}")
        if command_name not in profile.get("allowed_commands", []):
            raise AuditError(
                f"command {command_name!r} is not allowed by local profile {profile_name!r}"
            )
    payload = {
        "routine": name,
        "support": record["support"],
        "surface": surface,
        "profile": profile_name,
        **profile,
    }
    if check_system:
        selected_record = dict(record)
        selected_record["surface"] = surface
        selected_record["selected_profile"] = profile_name
        system = _system_checks(
            [selected_record],
            profiles,
            runtime_override=runtime,
            smoke_permission=smoke_permission,
        )
        payload["system"] = system
        if system["errors"]:
            if output_format == "json":
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print("; ".join(system["errors"]), file=sys.stderr)
            return 2
    if output_format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if surface != "local":
            raise AuditError("TSV resolution is only defined for local profiles")
        fields = (
            str(profile_name),
            str(profile["sandbox"]),
            str(profile["atelier_access"]),
            str(profile["web_search"]),
            str(profile["shell_network"]),
            str(profile["user_config"]),
            str(profile["timeout_seconds"]),
            str(profile["reasoning_effort"]),
            _profile_fingerprint(str(profile_name), profile),
            ",".join(profile["permissions"]),
            str(profile.get("fallback_runtime") or "none"),
        )
        if any("\t" in field or "\n" in field for field in fields):
            raise AuditError("profile metadata contains unsafe whitespace")
        print("\t".join(fields))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit_parser = subparsers.add_parser("audit", help="audit every routine")
    audit_parser.add_argument("--check-system", action="store_true")
    audit_parser.add_argument("--json", action="store_true")
    health_parser = subparsers.add_parser(
        "health", help="one table: schedule, output, claim, failure, recovery"
    )
    health_parser.add_argument("--json", action="store_true")
    resolve_parser = subparsers.add_parser(
        "resolve", help="resolve one routine profile"
    )
    resolve_parser.add_argument("routine")
    resolve_parser.add_argument("--surface", choices=("local", "cloud"), required=True)
    resolve_parser.add_argument("--check-system", action="store_true")
    resolve_parser.add_argument("--runtime", choices=("codex", "claude"))
    resolve_parser.add_argument("--command")
    resolve_parser.add_argument("--format", choices=("json", "tsv"), default="json")
    resolve_parser.add_argument(
        "--smoke-permission",
        help=(
            "Exempt exactly this external permission from the unverified-write "
            "gate. Only routine_permission_smoke.sh passes it, because a smoke "
            "cannot require the verification it exists to produce. Never passed "
            "by routine_runner.sh, so real execution still fails closed."
        ),
    )
    args = parser.parse_args()
    try:
        if args.command == "audit":
            payload, exit_code = _audit(args.check_system)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"routine audit: {'ok' if payload['ok'] else 'failed'}; "
                    f"{payload['counts']['routines']} routine(s)"
                )
                for message in payload["errors"]:
                    print(f"ERROR: {message}", file=sys.stderr)
            return exit_code
        if args.command == "health":
            payload, exit_code = _health()
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(_health_text(payload))
            return exit_code
        return _resolve(
            args.routine,
            args.surface,
            args.check_system,
            args.format,
            args.runtime,
            args.command,
            args.smoke_permission,
        )
    except AuditError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
