#!/usr/bin/env python3
"""Validated process boundary for Prefect-owned local Atelier routines.

Prefect owns scheduling, run identity, concurrency, retries, state, history,
and logs.  This module owns only what the orchestrator cannot infer: the
private routine declaration, each profile's fixed headless runtime boundary, and a compact
receipt proving that the declared domain artifact exists.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, Iterator
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _models  # noqa: E402
import _node  # noqa: E402
from _paths import atomic_write, tier_segments  # noqa: E402
from command_timeout import release_process, stop_process_group, track_process, wait_until_deadline  # noqa: E402
from runtime import capabilities as runtime_capabilities  # noqa: E402
from observability import usage as observations  # noqa: E402
import routine_receipts as receipts  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SAFE_CYCLE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_MODEL = re.compile(r"^[a-z][a-z0-9_]*$")
LOCAL_SANDBOXES = {"workspace-write", "danger-full-access"}
ATELIER_ACCESS = {"read", "read-write"}
WEB_MODES = {"disabled", "live"}
NETWORK_MODES = {"disabled", "enabled", "unrestricted"}
USER_CONFIG_MODES = {"ignore", "required"}
REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
RUNTIMES = {"codex", "claude"}
# Claude permission rules per routine permission; Claude profiles may declare
# only these. Gmail send, reply, forward, trash, and draft tools are never granted.
CLAUDE_GRANTS = {
    "atelier:read": ("Read", "Glob", "Grep"),
    "vault:read-write": ("Read", "Glob", "Grep", "Edit(/{vault}/**)"),
    "gmail:read": tuple(f"mcp__claude_ai_Gmail__{tool}" for tool in ("search_threads", "get_thread", "get_message", "list_labels")),
}
CLAUDE_DENY = ("Bash", "WebSearch", "WebFetch")
DEFAULT_PREFECT_API_URL = "http://127.0.0.1:4200/api"

SECRET_PATTERNS = (
    re.compile(
        r"(?i)authorization\s*:\s*(?:basic|token|bearer)\s+"
        r"(?!<|\$\{|\{\{|redacted\b|placeholder\b)[A-Za-z0-9._~+/=-]{12,}"
    ),
    re.compile(
        r"(?i)[\"']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
        r"secret[_-]?access[_-]?key|password|passwd|secret)[\"']?\s*[:=]\s*[\"']?"
        r"(?!<|\$\{|\{\{|redacted\b|placeholder\b|example\b|changeme\b)"
        r"[A-Za-z0-9._~+/=-]{12,}"
    ),
    re.compile(
        r"(?m)(?:^|\s)(?:[A-Z][A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD)|"
        r"AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY)\s*=\s*[\"']?"
        r"(?!<|\$\{|\{\{|REDACTED\b|PLACEHOLDER\b|EXAMPLE\b|CHANGEME\b)"
        r"[A-Za-z0-9._~+/=-]{12,}"
    ),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(
        r"(?i)\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,}|"
        r"AIza[A-Za-z0-9_-]{20,})\b"
    ),
    re.compile(r"(?i)https?://[^\s/:@]+:[^\s/@]{8,}@"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
ORIGINAL_PROMPT_MARKER = re.compile(r"(?m)^--- ORIGINAL ROUTINE PROMPT .* ---\s*$")
DRIVE_MENTION = re.compile(r"(?i)\bgoogle[-\s]?drive\b|\bg?drive\s+mcp\b|\bmy\s+drive\b|\bgdrive\b")
LOCAL_INPUT_LOCATION = re.compile(r"(?i)\blocal\s+(?:file\s*system|filesystem|disk)\b")
LOCAL_INPUT_ROOT = re.compile(r"\$\{?OV\}?")


class ConfigurationError(RuntimeError):
    """A routine declaration or execution profile is unsafe or incomplete."""


class ExecutionError(RuntimeError):
    """A child process or its result failed the declared contract."""


class DeferredRun(ExecutionError):
    """A domain preflight intentionally deferred work until a later schedule."""


def _loopback_api_url(value: object, source: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"{source} is not a valid Prefect API URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
        or parsed.path.rstrip("/") != "/api"
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(f"{source} must be a loopback Prefect /api URL")
    return raw.rstrip("/")


@contextmanager
def local_prefect_settings(environ: dict[str, str] | None = None) -> Iterator[str]:
    """Pin Prefect clients to the local API and disable implicit servers."""
    from prefect.settings import (
        PREFECT_API_URL,
        PREFECT_SERVER_EPHEMERAL_ENABLED,
        temporary_settings,
    )

    env = os.environ if environ is None else environ
    candidates = (
        ("ATELIER_PREFECT_API_URL", env.get("ATELIER_PREFECT_API_URL")),
        ("PREFECT_API_URL", env.get("PREFECT_API_URL")),
        ("active Prefect API URL", PREFECT_API_URL.value()),
    )
    validated = [(source, _loopback_api_url(value, source)) for source, value in candidates if value]
    selected = validated[0][1] if validated else DEFAULT_PREFECT_API_URL
    with temporary_settings(
        updates={
            PREFECT_API_URL: selected,
            PREFECT_SERVER_EPHEMERAL_ENABLED: False,
        }
    ):
        yield selected


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    runner: str
    source: str
    cron: tuple[str, ...]
    timezone: str
    retries: int = 0
    retry_delay_seconds: int = 60


@dataclass(frozen=True)
class ModelSpec:
    schedule: ScheduleSpec
    adapter: str
    profile: str
    profile_values: dict[str, Any]
    output_dir: str
    file_pattern: str
    model: str | None = None
    rss_sources: str | None = None
    runtime_snapshot: bool = False

    def payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["schedule"]["cron"] = list(self.schedule.cron)
        return value

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "ModelSpec":
        schedule = dict(value["schedule"])
        schedule["cron"] = tuple(schedule["cron"])
        return cls(schedule=ScheduleSpec(**schedule), **{k: v for k, v in value.items() if k != "schedule"})


@dataclass(frozen=True)
class ProcessSpec:
    schedule: ScheduleSpec
    argv: tuple[str, ...]
    cwd: str
    environment: dict[str, str]
    timeout_seconds: int
    retry_safe: bool

    def payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["schedule"]["cron"] = list(self.schedule.cron)
        value["argv"] = list(self.argv)
        return value

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "ProcessSpec":
        schedule = dict(value["schedule"])
        schedule["cron"] = tuple(schedule["cron"])
        return cls(
            schedule=ScheduleSpec(**schedule),
            argv=tuple(value["argv"]),
            cwd=value["cwd"],
            environment=dict(value["environment"]),
            timeout_seconds=value["timeout_seconds"],
            retry_safe=value["retry_safe"],
        )


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"expected a TOML table in {path}")
    return value


def vault_root(environ: dict[str, str] | None = None) -> Path:
    raw = (environ or os.environ).get("OV", "").strip()
    if not raw:
        raise ConfigurationError("OV is not set")
    vault = Path(raw).expanduser().resolve()
    if not vault.is_dir():
        raise ConfigurationError("OV is not a directory")
    return vault


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ConfigurationError(f"{field} must be a nonempty string array")
    return value


def _safe_relative(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{field} must be a nonempty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigurationError(f"{field} must stay relative")
    return value


def resolve_timezone(value: object) -> str:
    if value == "local":
        try:
            from tzlocal import get_localzone_name

            value = get_localzone_name()
        except Exception as exc:  # noqa: BLE001 - library/platform boundary
            raise ConfigurationError(f"cannot resolve local timezone: {exc}") from exc
    if not isinstance(value, str) or not value:
        raise ConfigurationError("timezone must be an IANA name or 'local'")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"unknown timezone: {value}") from exc
    return value


def _schedule(row: dict[str, Any], *, runner: str, source: str) -> ScheduleSpec:
    name = row.get("name")
    if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
        raise ConfigurationError("routine name is missing or unsafe")
    raw_cron = row.get("cron")
    cron = [raw_cron] if isinstance(raw_cron, str) else raw_cron
    if not isinstance(cron, list) or not cron or any(not isinstance(item, str) or not item.strip() for item in cron):
        raise ConfigurationError(f"{name}: cron must be a string or nonempty string array")
    retries = row.get("retries", 0)
    delay = row.get("retry_delay_seconds", 60)
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 5:
        raise ConfigurationError(f"{name}: retries must be an integer from 0 through 5")
    if isinstance(delay, bool) or not isinstance(delay, int) or not 1 <= delay <= 3600:
        raise ConfigurationError(f"{name}: retry_delay_seconds must be from 1 through 3600")
    return ScheduleSpec(
        name=name,
        runner=runner,
        source=source,
        cron=tuple(item.strip() for item in cron),
        timezone=resolve_timezone(row.get("timezone")),
        retries=retries,
        retry_delay_seconds=delay,
    )


def _load_profiles(root: Path) -> dict[str, dict[str, Any]]:
    document = _load_toml(root / "harness/routine_profiles.toml")
    if document.get("version") != 1 or not isinstance(document.get("profiles"), dict):
        raise ConfigurationError("unsupported or empty routine profile registry")
    return document["profiles"]


def _validate_profile(name: str, profile: object) -> dict[str, Any]:
    if not isinstance(profile, dict) or profile.get("surface") != "local":
        raise ConfigurationError(f"{name}: selected profile is not local")
    checks = {
        "sandbox": LOCAL_SANDBOXES,
        "atelier_access": ATELIER_ACCESS,
        "web_search": WEB_MODES,
        "shell_network": NETWORK_MODES,
        "user_config": USER_CONFIG_MODES,
        "reasoning_effort": REASONING_EFFORTS,
    }
    for field, allowed in checks.items():
        if profile.get(field) not in allowed:
            raise ConfigurationError(f"{name}: profile has invalid {field}")
    timeout = profile.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 30 <= timeout <= 14400:
        raise ConfigurationError(f"{name}: profile has invalid timeout_seconds")
    for field in ("permissions", "required_clis", "required_plugins", "optional_plugins", "allowed_adapters"):
        _string_list(profile.get(field), f"{name}.{field}")
    if profile["required_plugins"] and profile["user_config"] != "required":
        raise ConfigurationError(f"{name}: required plugins need user_config='required'")
    if profile["sandbox"] == "danger-full-access":
        if profile["atelier_access"] != "read-write" or profile["shell_network"] != "unrestricted":
            raise ConfigurationError(f"{name}: danger-full-access needs read-write/unrestricted")
    elif profile["shell_network"] == "unrestricted":
        raise ConfigurationError(f"{name}: unrestricted network needs danger-full-access")
    if profile["atelier_access"] == "read-write" and "atelier:read-write" not in profile["permissions"]:
        raise ConfigurationError(f"{name}: read-write Atelier access is not declared")
    if "fallback_runtime" in profile or "primary_runtime" in profile:
        raise ConfigurationError(f"{name}: runtime selection is unsupported; a profile binds one runtime")
    if profile.get("runtime", "codex") not in RUNTIMES:
        raise ConfigurationError(f"{name}: profile has invalid runtime")
    if profile.get("runtime") == "claude" and (
        (profile["sandbox"], profile["atelier_access"], profile["web_search"], profile["shell_network"])
        != ("workspace-write", "read", "disabled", "disabled")
        or profile["required_clis"] or profile["optional_plugins"] or not set(profile["permissions"]) <= CLAUDE_GRANTS.keys()
    ):
        raise ConfigurationError(f"{name}: Claude profiles are offline, read-only for Atelier, and use mapped permissions")
    return dict(profile)


def model_runtime(spec: "ModelSpec") -> str:
    return spec.profile_values.get("runtime", "codex")


def profile_fingerprint(name: str, profile: dict[str, Any]) -> str:
    encoded = json.dumps({"name": name, **profile}, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _private_rows(vault: Path) -> list[dict[str, Any]]:
    root = tier_segments().get("private_routines", "_tools/routines")
    document = _load_toml(vault / root / "registry.toml")
    if document.get("version") != 1:
        raise ConfigurationError("unsupported private routine registry")
    rows = document.get("routine")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ConfigurationError("private routine registry must contain [[routine]] tables")
    return rows


def _model_spec(row: dict[str, Any], *, root: Path, profiles: dict[str, dict[str, Any]]) -> ModelSpec:
    schedule = _schedule(row, runner="model", source="private")
    if schedule.retries:
        raise ConfigurationError(f"{schedule.name}: model attempts cannot declare retries")
    adapter = row.get("adapter")
    if not isinstance(adapter, str) or not SAFE_NAME.fullmatch(adapter):
        raise ConfigurationError(f"{schedule.name}: adapter is missing or unsafe")
    profile_name = row.get("profile")
    if not isinstance(profile_name, str):
        raise ConfigurationError(f"{schedule.name}: profile is required")
    profile = _validate_profile(profile_name, profiles.get(profile_name))
    if adapter not in profile["allowed_adapters"]:
        raise ConfigurationError(f"{schedule.name}: adapter {adapter!r} is not allowed by {profile_name}")
    rss_sources = row.get("rss_sources")
    runtime_snapshot = row.get("runtime_snapshot", False)
    if not isinstance(runtime_snapshot, bool):
        raise ConfigurationError("runtime_snapshot must be boolean")
    if runtime_snapshot and adapter != "archived-prompt":
        raise ConfigurationError("runtime_snapshot requires the archived-prompt adapter")
    if rss_sources is not None:
        rss_sources = _safe_relative(rss_sources, "rss_sources")
        if Path(rss_sources).suffix != ".toml":
            raise ConfigurationError("rss_sources must name a private TOML file")
        if (adapter != "archived-prompt"
                or profile["web_search"] != "live" or "web:live" not in profile["permissions"]):
            raise ConfigurationError("rss_sources requires the archived-prompt adapter with web:live")
    model = row.get("model")
    if model is not None:
        if not isinstance(model, str) or not SAFE_MODEL.fullmatch(model):
            raise ConfigurationError(f"{schedule.name}: model must be a declared identity name")
        try:
            (_models.claude_binding if profile.get("runtime") == "claude" else _models.codex_binding)(model)
        except _models.ModelError as exc:
            raise ConfigurationError(f"{schedule.name}: {exc}") from exc
    return ModelSpec(
        schedule=schedule,
        adapter=adapter,
        profile=profile_name,
        profile_values={**profile, "profile_fingerprint": profile_fingerprint(profile_name, profile)},
        output_dir=_safe_relative(row.get("output_dir"), f"{schedule.name}.output_dir"),
        file_pattern=_safe_relative(row.get("file_pattern"), f"{schedule.name}.file_pattern"),
        model=model,
        rss_sources=rss_sources,
        runtime_snapshot=runtime_snapshot,
    )


def _public_process_spec(row: dict[str, Any], *, root: Path) -> ProcessSpec:
    schedule = _schedule(row, runner="process", source="public")
    raw_argv = _string_list(row.get("argv"), f"{schedule.name}.argv")
    if not raw_argv:
        raise ConfigurationError(f"{schedule.name}.argv must not be empty")
    first = raw_argv[0]
    if first == "{python}":
        first = sys.executable
    elif first not in {"uv"}:
        relative = _safe_relative(first, f"{schedule.name}.argv[0]")
        executable = (root / relative).resolve()
        try:
            executable.relative_to(root.resolve())
        except ValueError as exc:
            raise ConfigurationError(f"{schedule.name}: program escapes the Atelier root") from exc
        if not executable.is_file():
            raise ConfigurationError(f"{schedule.name}: program is missing: {relative}")
        first = str(executable)
    argv = [first, *raw_argv[1:]]
    raw_environment = row.get("environment", {})
    if not isinstance(raw_environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in raw_environment.items()
    ):
        raise ConfigurationError(f"{schedule.name}: environment must be a string table")
    timeout = row.get("timeout_seconds", 900)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 14400:
        raise ConfigurationError(f"{schedule.name}: timeout_seconds is invalid")
    retry_safe = row.get("retry_safe", False)
    if not isinstance(retry_safe, bool):
        raise ConfigurationError(f"{schedule.name}: retry_safe must be boolean")
    if schedule.retries and not retry_safe:
        raise ConfigurationError(f"{schedule.name}: retries require retry_safe=true")
    return ProcessSpec(
        schedule=schedule,
        argv=tuple(argv),
        cwd=str(root),
        environment=dict(raw_environment),
        timeout_seconds=timeout,
        retry_safe=retry_safe,
    )


def _vault_process_spec(row: dict[str, Any], *, vault: Path) -> ProcessSpec:
    schedule = _schedule(row, runner="process", source="private")
    script = _safe_relative(row.get("script"), f"{schedule.name}.script")
    if Path(script).suffix not in {".py", ".sh"}:
        raise ConfigurationError(f"{schedule.name}: vault script must be .py or .sh")
    raw_args = row.get("args", [])
    if not isinstance(raw_args, list) or any(not isinstance(arg, str) or "\x00" in arg for arg in raw_args):
        raise ConfigurationError(f"{schedule.name}: args must be a string array")
    timeout = row.get("timeout_seconds", 900)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 14400:
        raise ConfigurationError(f"{schedule.name}: timeout_seconds is invalid")
    retry_safe = row.get("retry_safe", False)
    if not isinstance(retry_safe, bool) or (schedule.retries and not retry_safe):
        raise ConfigurationError(f"{schedule.name}: retries require retry_safe=true")
    script_path = (vault / script).resolve()
    try:
        script_path.relative_to(vault)
    except ValueError as exc:
        raise ConfigurationError(f"{schedule.name}: script escapes the vault") from exc
    if not script_path.is_file():
        raise ConfigurationError(f"{schedule.name}: vault script is missing")
    argv = ("uv", "run", "--quiet", str(script_path), *raw_args) if script_path.suffix == ".py" else (
        "/bin/bash", str(script_path), *raw_args
    )
    return ProcessSpec(
        schedule=schedule,
        argv=argv,
        cwd=str(script_path.parent),
        environment={},
        timeout_seconds=timeout,
        retry_safe=retry_safe,
    )


def load_specs(*, root: Path = ROOT, environ: dict[str, str] | None = None) -> tuple[list[ModelSpec], list[ProcessSpec]]:
    """Load every deployment without exposing private parameters to Prefect."""
    vault = vault_root(environ)
    profiles = _load_profiles(root)
    models: list[ModelSpec] = []
    processes: list[ProcessSpec] = []
    for row in _private_rows(vault):
        if row.get("execution") != "local":
            continue
        runner = row.get("runner")
        if runner == "model":
            models.append(_model_spec(row, root=root, profiles=profiles))
        elif runner == "process":
            processes.append(_vault_process_spec(row, vault=vault))
        else:
            raise ConfigurationError(f"{row.get('name', '<missing>')}: unsupported local runner {runner!r}")
    public = _load_toml(root / "routines/registry.toml")
    if public.get("version") != 1 or not isinstance(public.get("routine"), list):
        raise ConfigurationError("unsupported public routine registry")
    if any(not isinstance(row, dict) or row.get("runner") != "process" for row in public["routine"]):
        raise ConfigurationError("public routine rows must declare runner='process'")
    processes.extend(_public_process_spec(row, root=root) for row in public["routine"])
    names = [spec.schedule.name for spec in [*models, *processes]]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigurationError("duplicate Prefect deployment names: " + ", ".join(duplicates))
    if not names:
        raise ConfigurationError("no local Prefect deployments are declared")
    return models, processes


def resolve_model(name: str, *, root: Path = ROOT, environ: dict[str, str] | None = None) -> ModelSpec:
    models, _ = load_specs(root=root, environ=environ)
    matches = [spec for spec in models if spec.schedule.name == name]
    if len(matches) != 1:
        raise ConfigurationError(f"expected exactly one model routine named {name!r}")
    return matches[0]


def resolve_process(
    name: str, source: str, *, root: Path = ROOT, environ: dict[str, str] | None = None
) -> ProcessSpec:
    _, processes = load_specs(root=root, environ=environ)
    matches = [spec for spec in processes if spec.schedule.name == name and spec.schedule.source == source]
    if len(matches) != 1:
        raise ConfigurationError(f"expected exactly one {source} process job named {name!r}")
    return matches[0]


def prompt_findings(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8")
    lines: set[int] = set()
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            lines.add(text.count("\n", 0, match.start()) + 1)
    return sorted(lines)


def validate_archived_prompt(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or not lines[0].startswith("LOCAL EXECUTION OVERRIDE"):
        raise ConfigurationError("archived prompt must begin with LOCAL EXECUTION OVERRIDE")
    marker = ORIGINAL_PROMPT_MARKER.search(text)
    if marker is None:
        raise ConfigurationError("archived prompt has no ORIGINAL ROUTINE PROMPT boundary")
    override, body = text[: marker.start()], text[marker.end() :]
    if DRIVE_MENTION.search(body) and not (LOCAL_INPUT_LOCATION.search(override) and LOCAL_INPUT_ROOT.search(override)):
        raise ConfigurationError("archived prompt references Drive without a local $OV input override")
    findings = prompt_findings(path)
    if findings:
        raise ConfigurationError("archived prompt contains a literal credential at line(s): " + ", ".join(map(str, findings)))


def installed_plugins(runtime: str, *, root: Path, environ: dict[str, str]) -> set[str]:
    """Enabled Codex plugins, or connected Claude connectors as `claude mcp list`
    names them, without inheriting unrelated secrets."""
    clean_env = {key: environ[key] for key in ("HOME", "PATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR") if environ.get(key)}
    argv = ["claude", "mcp", "list"] if runtime == "claude" else ["codex", "plugin", "list"]
    result = execute_process(argv, env=clean_env, cwd=root, seconds=60)
    if result.returncode:
        raise ConfigurationError(f"cannot inspect {runtime} plugins: {screened_log(result.stdout)[:300]}")
    installed: set[str] = set()
    for line in result.stdout.splitlines():
        name, _, state = line.partition(": ")
        columns = line.split()
        if runtime == "claude" and state.rstrip().endswith("✔ Connected"):
            installed.add(name)
        elif runtime == "codex" and len(columns) >= 3 and columns[1:3] == ["installed,", "enabled"]:
            installed.add(columns[0])
    return installed


def prepare_model(name: str, *, root: Path = ROOT, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Safe, retryable preparation. It performs no routine-domain effects."""
    env = dict(os.environ if environ is None else environ)
    spec = resolve_model(name, root=root, environ=env)
    vault = vault_root(env)
    output = (vault / spec.output_dir).resolve()
    try:
        output.relative_to(vault)
    except ValueError as exc:
        raise ConfigurationError(f"{name}: output_dir escapes the vault") from exc
    if spec.adapter != "autoevo":
        output.mkdir(parents=True, exist_ok=True)
    if spec.adapter == "archived-prompt":
        prompt = vault / tier_segments().get("routine_prompts", "_routine_prompts") / f"{name}.md"
        if not prompt.is_file():
            raise ConfigurationError(f"{name}: archived routine prompt is missing")
        validate_archived_prompt(prompt)
    runtime = model_runtime(spec)
    missing = [tool for tool in [runtime, *spec.profile_values["required_clis"]] if shutil.which(tool, path=env.get("PATH")) is None]
    if missing:
        raise ConfigurationError(f"{name}: required CLI not on PATH: " + ", ".join(sorted(set(missing))))
    required_plugins = set(spec.profile_values["required_plugins"])
    if required_plugins:
        missing_plugins = required_plugins - installed_plugins(runtime, root=root, environ=env)
        if missing_plugins:
            label = "Claude connectors" if runtime == "claude" else "Codex plugins"
            raise ConfigurationError(f"{name}: missing required {label}: " + ", ".join(sorted(missing_plugins)))
    if spec.rss_sources:
        config = _rss_config(spec, vault=vault)
        try:
            result = _node.run(
                [root / "scripts/routine_feeds.mjs", "--validate"], cwd=root,
                env=_node.system_env(), timeout=5, input=json.dumps(config),
            )
            valid = json.loads(result.stdout)
            if result.returncode or valid != {"schema": 1, "valid": True, "feeds": len(config["feeds"])}:
                raise ValueError("invalid preflight result")
        except (_node.NodeError, ValueError, TypeError) as exc:
            raise ConfigurationError("RSS input preflight failed; check private config and Node/parser prerequisites") from exc
    return spec.payload()


def _adapter_contract(spec: ModelSpec, root: Path) -> str:
    adapters = _load_toml(root / "routines/registry.toml").get("adapters", {})
    row = adapters.get(spec.adapter) if isinstance(adapters, dict) else None
    if not isinstance(row, dict) or not isinstance(row.get("procedure"), str):
        raise ConfigurationError(f"{spec.schedule.name}: adapter {spec.adapter!r} is not registered")
    source = root / row["procedure"]
    if not source.is_file():
        raise ConfigurationError(f"{spec.schedule.name}: adapter procedure is missing")
    return row["procedure"]


def adapter_prompt(spec: ModelSpec, *, root: Path) -> str:
    source = _adapter_contract(spec, root)
    profile = spec.profile_values
    permissions = ",".join(profile["permissions"])
    if spec.adapter == "autoevo":
        return (
            f"Read {root}/AGENTS.md, then {root / source} completely. "
            "This unattended Autoevo invocation authorizes candidate drafting only. The vault and Atelier are "
            "read-only; only AUTOEVO_WORKSPACE is writable. Do not run a publisher, commit, alter live state, "
            "inspect scheduler state, use network/connectors, or follow unrelated session cues. "
            "Use AUTOEVO_WORKSPACE/plan.json and sources/ for this cycle. Read schema.json and write "
            "proposal.json matching it. The parent retains the trusted plan, validates proposals, rebuilds "
            "decision state and publishes allowed operations after this process exits. Preview helpers may "
            "write only inside AUTOEVO_WORKSPACE. Final JSON is a transport acknowledgment matching the "
            "supplied schema: routine=autoevo-nightly, output_file=proposal.json, outcome=delivered only "
            "when the candidate file is ready (not a claim of live publication), plus summary and skipped_inputs."
        )
    return (
        "This is an unattended local Atelier routine, not an interactive user workflow. "
        f"Routine identity: `{spec.schedule.name}`. Prefect has selected adapter `{spec.adapter}` and completed "
        "the safe local preflight with profile "
        f"`{spec.profile}` (sandbox={profile['sandbox']}, atelier_access={profile['atelier_access']}, "
        f"web={profile['web_search']}, shell_network={profile['shell_network']}, user_config={profile['user_config']}). "
        f"Effective action permission allowlist: `{permissions}`. Treat it as a strict model-level allowlist: skip "
        "every connector, CLI, web, or filesystem action not listed, even if an optional integration is installed. "
        "This is not a shell-level connector ACL. "
        f"Read `{root}/AGENTS.md` first, then read `{root / source}` completely and execute "
        f"it in this process{' using the Codex adaptation table' if model_runtime(spec) == 'codex' else ''}. Treat the Atelier repository as read-only unless "
        "atelier_access is read-write. Do not inspect scheduler state or the private routine registry; Prefect owns "
        "scheduling and run state. Load only files required by the adapter and archived prompt after the mandatory "
        "session-start reads. The scheduled invocation authorizes only autonomous effects explicitly allowed by the "
        "adapter contract. Do not ask for interactive input. Ignore unrelated SessionStart cues. Stop safely if the "
        "procedure requires authority it does not grant. The final response must contain only JSON matching the supplied "
        "schema. Set outcome to delivered only after writing the canonical output artifact, noop only for an intentional "
        "documented no-op that still writes its audit artifact, or failed if no valid artifact was produced. Report the "
        "canonical artifact path in output_file."
    )


def runtime_env(spec: ModelSpec, *, root: Path, vault: Path, cycle: str, environ: dict[str, str]) -> dict[str, str]:
    for key in ("HOME", "PATH"):
        if not environ.get(key):
            raise ConfigurationError(f"{key} is required")
    profile = spec.profile_values
    env = {
        "HOME": environ["HOME"],
        "PATH": environ["PATH"],
        "OV": str(vault),
        "TMPDIR": environ.get("TMPDIR") or "/tmp",
        "LANG": environ.get("LANG") or "en_US.UTF-8",
        "ATELIER_ACTIVE_RUNTIME": model_runtime(spec),
        "ATELIER_ROOT": str(root),
        "ATELIER_ROUTINE_PROFILE": spec.profile,
        "ATELIER_ROUTINE_CYCLE": cycle,
        "ATELIER_ROUTINE_PERMISSIONS": ",".join(profile["permissions"]),
        "ATELIER_SKIP_LOCK_TOUCH": "1",
        "ATELIER_PYTHON": environ.get("ATELIER_PYTHON") or sys.executable,
        "ZDOTDIR": str(root / "harness/routine-shell"),
    }
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CODEX_CA_CERTIFICATE", "SSL_CERT_FILE", "DRY_RUN"):
        if environ.get(key):
            env[key] = environ[key]
    return env


def codex_argv(spec: ModelSpec, *, root: Path, vault: Path, cwd: Path, output: Path) -> list[str]:
    profile = spec.profile_values
    # Provider ids live in gitignored bindings, so they resolve here at
    # execution time and never enter the spec payload Prefect persists.
    model_argv: list[str] = []
    effort = profile["reasoning_effort"]
    if spec.model:
        provider_model, declared_effort = _models.codex_binding(spec.model)
        if not provider_model:
            raise ConfigurationError(
                f"{spec.schedule.name}: model {spec.model!r} has no codex binding in profile/models.toml"
            )
        model_argv = ["-m", provider_model]
        if declared_effort:
            effort = declared_effort
    argv = ["codex", *model_argv, "-c", 'approval_policy="never"', "-c", f'model_reasoning_effort="{effort}"']
    argv += ["--search"] if profile["web_search"] == "live" else ["-c", 'web_search="disabled"']
    if profile["sandbox"] == "workspace-write":
        network = str(profile["shell_network"] == "enabled").lower()
        argv += ["-c", f"sandbox_workspace_write.network_access={network}"]
    if spec.adapter == "autoevo":
        if any(profile[key] != value for key, value in {
            "sandbox": "workspace-write", "atelier_access": "read", "shell_network": "disabled",
            "user_config": "ignore", "web_search": "disabled",
        }.items()) or profile["required_plugins"] or profile["optional_plugins"]:
            raise ConfigurationError("Autoevo requires isolated, offline, staging-only model permissions")
        argv += ["-c", "sandbox_workspace_write.writable_roots=[]",
                 "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                 "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true"]
        # Managed profile allowlists take precedence over legacy sandbox flags.
        workspace = cwd.resolve()
        filesystem = {":root": "read", str(workspace): "write",
                      **{str(workspace / name): "read" for name in (".git", ".codex", ".agents")}}
        rules = ",".join(f"{json.dumps(path)}={json.dumps(access)}" for path, access in filesystem.items())
        argv += ["-c", 'default_permissions="atelier-autoevo-draft"',
                 "-c", f"permissions.atelier-autoevo-draft.filesystem={{{rules}}}",
                 "-c", "permissions.atelier-autoevo-draft.network.enabled=false"]
    argv += ["--ask-for-approval", "never", "exec"]
    if profile["user_config"] == "ignore":
        argv.append("--ignore-user-config")
    argv += [
        "--sandbox",
        profile["sandbox"],
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--output-schema",
        str(root / "harness/routine_result.schema.json"),
        "--output-last-message",
        str(output),
        "--dangerously-bypass-hook-trust" if profile["atelier_access"] == "read-write" else "--skip-git-repo-check",
        "-C",
        str(root if profile["atelier_access"] == "read-write" else cwd),
    ]
    if spec.adapter != "autoevo":
        argv += ["--add-dir", str(vault)]
    return argv


def claude_argv(spec: ModelSpec, *, root: Path, vault: Path) -> list[str]:
    """Headless Claude Code bounded by an explicit allowlist. No user or project
    settings load, so the user's own allow rules and hooks never apply."""
    profile = spec.profile_values
    allow = dict.fromkeys(rule.format(vault=vault) for key in profile["permissions"] for rule in CLAUDE_GRANTS[key])
    settings = {"permissions": {"allow": list(allow), "deny": list(CLAUDE_DENY)}}
    # Claude's validator rejects the draft 2020-12 $schema URI; the body is portable.
    schema = json.loads((root / "harness/routine_result.schema.json").read_text(encoding="utf-8"))
    argv = ["claude", "-p", "--output-format", "json", "--no-session-persistence",
            "--json-schema", json.dumps({k: v for k, v in schema.items() if k != "$schema"}),
            "--permission-mode", "dontAsk", "--setting-sources", "project", "--settings", json.dumps(settings),
            "--effort", profile["reasoning_effort"], "--add-dir", str(vault)]
    if spec.model:
        model = _models.claude_binding(spec.model)
        if not model:
            raise ConfigurationError(f"{spec.schedule.name}: model {spec.model!r} has no claude_code binding in profile/models.toml")
        argv += ["--model", model]
    return argv


def write_claude_result(stdout: str, destination: Path) -> None:
    """Keep only the schema-bound structured output from Claude's final result line."""
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type") == "result":
            if not value.get("is_error") and isinstance(value.get("structured_output"), dict):
                destination.write_text(json.dumps(value["structured_output"]), encoding="utf-8")
            return


def execute_process(
    argv: list[str] | tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    seconds: float | None = None,
    input_text: str | None = None,
    usage: observations.Usage | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a process group with file-backed I/O and kill it at the boundary."""
    if seconds is not None and seconds <= 0:
        raise ValueError("seconds must be positive")
    with ExitStack() as stack:
        output = stack.enter_context(tempfile.TemporaryFile())
        stdin = subprocess.DEVNULL
        if input_text is not None:
            stdin = stack.enter_context(tempfile.TemporaryFile())
            stdin.write(input_text.encode())
            stdin.seek(0)
        process = None
        reader = None
        ready, stopping = threading.Event(), threading.Event()
        if usage is not None:
            def read_model():
                ready.wait()
                if process is not None:
                    usage.drain(process.stdout, stopping)
            try:
                reader = threading.Thread(target=read_model, daemon=True)
                reader.start()
            except RuntimeError:
                reader = None
                usage.gaps += 1
        try:
            process = subprocess.Popen(
                argv, env=env, cwd=cwd, stdin=stdin, start_new_session=True,
                stdout=subprocess.PIPE if reader else subprocess.DEVNULL if usage is not None else output,
                stderr=subprocess.DEVNULL if usage is not None else output,
            )
        except OSError as exc:
            ready.set()
            if usage is None:
                output.write(f"ERROR: cannot start {argv[0]}: {exc}\n".encode())
            returncode = 127
        else:
            ready.set()
            track_process(process)
            try:
                returncode = process.wait() if seconds is None else wait_until_deadline(process, time.time() + seconds)
            except subprocess.TimeoutExpired:
                stop_process_group(process)
                if usage is None:
                    output.write(f"ERROR: command timed out after {seconds:g}s: {argv[0]}\n".encode())
                returncode = 124
            except BaseException:
                stop_process_group(process)
                raise
            finally:
                if reader is not None:
                    stopping.set()
                    reader.join(timeout=1)
                    if reader.is_alive():
                        usage.gaps += 1
                release_process(process)
        output.seek(0)
        return subprocess.CompletedProcess(list(argv), returncode, output.read().decode(errors="replace"))


def execute_observed_model(argv, *, flow_run_id: str, **kwargs):
    usage = observations.Usage()
    returncode = None
    try:
        result = execute_process(argv, usage=usage, **kwargs)
        returncode = result.returncode
        return result
    finally:
        observations.emit(usage, returncode, flow_run_id)


def screened_log(text: str) -> str:
    flagged: set[int] = set()
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            flagged.add(text.count("\n", 0, match.start()) + 1)
    return "\n".join(
        "[line withheld: credential screening]" if number in flagged else line
        for number, line in enumerate(text.splitlines(), 1)
    )


# CodexBar never refreshes an expired Claude OAuth token, and a sandboxed
# refresh could rotate it without persisting it; Claude's own CLI does it here.
PRE_SANDBOX_REFRESHES = {
    "readwise:read": ("readwise", "--refresh", "--version"),
    "quota:read": ("claude", "auth", "status", "--json"),
}


def warm_before_sandbox(spec: ModelSpec, *, env: dict[str, str], root: Path) -> None:
    """Best-effort tool cache and credential refreshes; output is discarded."""
    for permission, argv in PRE_SANDBOX_REFRESHES.items():
        if permission in spec.profile_values["permissions"] and shutil.which(argv[0], path=env.get("PATH")):
            result = execute_process(list(argv), env=env, cwd=root, seconds=30, input_text="")
            ok = result.returncode == 0
            print(f"{'' if ok else 'warning: '}{argv[0]} refresh {'done' if ok else 'failed'} before sandbox entry", flush=True)


def _rss_config(spec: ModelSpec, *, vault: Path) -> dict[str, Any]:
    """Read bounded private declarations, without exposing values in errors."""
    relative = _safe_relative(spec.rss_sources, "rss_sources")
    path = (vault / relative).resolve()
    try:
        path.relative_to(vault)
        if path.suffix != ".toml" or not path.is_file() or path.stat().st_size > 131072:
            raise ValueError("not a bounded TOML file")
        with path.open("rb") as stream:
            raw = stream.read(131073)
        if len(raw) > 131072:
            raise ValueError("file grew beyond limit")
        value = tomllib.loads(raw.decode("utf-8"))
        if (set(value) != {"version", "feed"} or type(value["version"]) is not int
                or value["version"] != 1 or not isinstance(value["feed"], list)):
            raise ValueError("invalid configuration shape")
    except (OSError, ValueError) as exc:
        raise ConfigurationError("RSS source config must be bounded version-1 TOML inside the vault") from exc
    return {"schema": 1, "feeds": value["feed"]}


def stage_rss_inputs(spec: ModelSpec, *, root: Path, vault: Path, destination: Path) -> None:
    """One bounded fetch before the model; unavailable inputs remain explicit."""
    declared = None
    try:
        config = _rss_config(spec, vault=vault)
        declared = len(config["feeds"])
        result = _node.run(
            [root / "scripts/routine_feeds.mjs"], cwd=root, env=_node.system_env(),
            timeout=25, input=json.dumps(config),
        )
        if result.returncode or len(result.stdout.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("collector failed or exceeded its output limit")
        value = json.loads(result.stdout)
        if (not isinstance(value, dict) or value.get("schema") != 1
                or value.get("status") not in {"ok", "degraded"}
                or not isinstance(value.get("collected_at"), str)
                or not isinstance(value.get("feeds"), list) or len(value["feeds"]) != declared
                or not isinstance(value.get("gaps"), list)
                or not isinstance(value.get("counts"), dict)
                or value["counts"].get("declared") != declared
                or any(type(value["counts"].get(key)) is not int for key in ("attempted", "reached", "usable", "items"))
                or value.get("summary_provenance") != "feed content, not article full text"):
            raise ValueError("invalid collector envelope")
    except (ConfigurationError, _node.NodeError, ValueError, TypeError):
        value = {
            "schema": 1, "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "degraded",
            "counts": {"declared": declared, "attempted": None, "reached": None, "usable": None, "items": 0},
            "feeds": [],
            "gaps": [{"source": "collector", "code": "collector_failed",
                      "message": "RSS collector unavailable; fetch coverage is unknown. No retry was attempted."}],
            "summary_provenance": "feed content, not article full text",
        }
    atomic_write(destination, json.dumps(value, ensure_ascii=False) + "\n")


def _model_result(path: Path, spec: ModelSpec, *, vault: Path, started_at: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError("model produced no valid result envelope") from exc
    if not isinstance(value, dict) or value.get("routine") != spec.schedule.name:
        raise ExecutionError("model result routine does not match")
    if value.get("outcome") not in {"delivered", "noop"}:
        raise ExecutionError("model did not report a successful domain outcome")
    if not isinstance(value.get("summary"), str):
        raise ExecutionError("model result summary is invalid")
    skipped = value.get("skipped_inputs")
    if not isinstance(skipped, list) or any(not isinstance(item, str) for item in skipped):
        raise ExecutionError("model result skipped_inputs is invalid")
    raw_output = value.get("output_file")
    if not isinstance(raw_output, str) or not raw_output.strip():
        raise ExecutionError("successful model result omitted output_file")
    raw_output = raw_output.strip()[4:] if raw_output.strip().startswith("$OV/") else raw_output.strip()
    output = Path(raw_output).expanduser()
    try:
        if output.is_absolute():
            try:
                relative = output.relative_to(vault)
            except ValueError:
                relative = output.resolve().relative_to(vault.resolve())
        else:
            relative = output
        if ".." in relative.parts:
            raise ValueError("reported output_file is unsafe")
        value["output_file"] = relative.as_posix()
        output = receipts.artifact_path(value["output_file"], vault=vault,
                                        output_dir=spec.output_dir, file_pattern=spec.file_pattern)
        value["artifact_sha256"] = receipts.content_hash(
            output, not_before=datetime.fromisoformat(started_at).timestamp() - 2.0
        )
        value["verification_scope"] = receipts.VERIFICATION_SCOPE
    except (OSError, ValueError) as exc:
        raise ExecutionError(f"artifact attestation failed: {exc}") from exc
    return value


def receipt_path(vault: Path, routine: str, cycle: str) -> Path:
    if not SAFE_NAME.fullmatch(routine) or not SAFE_CYCLE.fullmatch(cycle):
        raise ConfigurationError("unsafe receipt identity")
    meta = tier_segments().get("meta", "_meta")
    return vault / meta / "routine_receipts" / routine / f"{cycle}.toml"


def validate_cycle_id(value: str) -> str:
    if not SAFE_CYCLE.fullmatch(value):
        raise ValueError("cycle must use YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("cycle is not a calendar date") from exc
    return value


def write_receipt(path: Path, fields: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (str, bool, int, float, list)):
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}\n")
        else:
            raise ExecutionError(f"receipt field {key!r} has unsupported type")
    atomic_write(path, "".join(lines))


def prior_delivery(spec: ModelSpec, *, cycle: str, vault: Path) -> dict[str, Any] | None:
    """Return an already verified cycle, or refuse an ambiguous prior attempt."""
    path = receipt_path(vault, spec.schedule.name, cycle)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        receipt = receipts.read(path, routine=spec.schedule.name, cycle=cycle, vault=vault,
                                output_dir=spec.output_dir, file_pattern=spec.file_pattern)
    except (OSError, ValueError) as exc:
        raise ExecutionError(f"existing domain receipt is not verified; effects review is required: {exc}") from exc
    verification = receipt.get("verification")
    if verification == "blocked":
        return None
    if verification != "passed":
        raise ExecutionError(
            f"existing domain receipt verification is {verification or 'absent'}; effects review is required"
        )
    print(f"routine cycle already verified: {spec.schedule.name} {cycle}", flush=True)
    return receipt


def execute_autoevo(spec: ModelSpec, *, vault: Path, root: Path, cycle: str, flow_run_id: str,
                    environ: dict[str, str]) -> dict[str, Any]:
    """Draft in an OS-fenced workspace, then accept in the trusted parent."""
    import autoevo_preflight
    import autoevo_run
    import autoevo_verify
    import semantic

    existing = autoevo_run.prior_result(vault, cycle)
    if existing is not None:
        return existing
    readiness = autoevo_preflight.inspect_preflight(vault=vault)
    if not readiness["ready"]:
        raise DeferredRun(f"autoevo deferred by {readiness['gate']}: {readiness['detail']}")
    with tempfile.TemporaryDirectory(prefix="atelier-autoevo-") as name:
        workspace = Path(name).resolve()
        plan = autoevo_run.prepare_workspace(vault, workspace, cycle, readiness)
        (workspace / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        (workspace / "schema.json").write_text(json.dumps(autoevo_verify.proposal_schema(plan)), encoding="utf-8")
        (workspace / "tmp").mkdir()
        env = runtime_env(spec, root=root, vault=vault, cycle=cycle, environ=environ)
        env.update(semantic.prepare_query_copy(vault, workspace / "qmd"))
        env.update(AUTOEVO_WORKSPACE=str(workspace), TMPDIR=str(workspace / "tmp"), PYTHONDONTWRITEBYTECODE="1")
        argv = codex_argv(spec, root=root, vault=vault, cwd=workspace, output=workspace / "result.json")
        result = execute_observed_model(
            [*argv, adapter_prompt(spec, root=root)], flow_run_id=flow_run_id,
            env=env, cwd=workspace, seconds=spec.profile_values["timeout_seconds"],
        )
        if result.returncode:
            raise ExecutionError(f"Autoevo candidate process exited {result.returncode}; no publication attempted")
        candidate = workspace / "proposal.json"
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_nlink != 1 or candidate.stat().st_size > 8_000_000:
            raise ExecutionError("Autoevo produced no bounded regular candidate file")
        proposal = json.loads(candidate.read_text(encoding="utf-8"))
    record = autoevo_run.accept_proposal(vault, proposal, plan, flow_run_id=flow_run_id)
    if record["status"] != "complete":
        raise ExecutionError(f"Autoevo domain result is {record['status']}; inspect the structured result")
    return record


def execute_model(
    payload: dict[str, Any],
    *,
    cycle: str,
    flow_run_id: str,
    root: Path = ROOT,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute one non-retryable model attempt and write only domain evidence."""
    validate_cycle_id(cycle)
    spec = ModelSpec.from_payload(payload)
    env_source = dict(os.environ if environ is None else environ)
    vault = vault_root(env_source)
    if spec.adapter == "autoevo":
        return execute_autoevo(spec, vault=vault, root=root, cycle=cycle, flow_run_id=flow_run_id, environ=env_source)
    existing = prior_delivery(spec, cycle=cycle, vault=vault)
    if existing is not None:
        return existing
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    started_epoch = time.time()
    print(f"routine starting: {spec.schedule.name} cycle={cycle} flow_run={flow_run_id}", flush=True)
    with tempfile.TemporaryDirectory(prefix="atelier-prefect-model-") as temporary_name:
        temporary = Path(temporary_name)
        result_file = temporary / "result.json"
        cwd = root if spec.profile_values["atelier_access"] == "read-write" else temporary / "cwd"
        cwd.mkdir(exist_ok=True)
        env = runtime_env(spec, root=root, vault=vault, cycle=cycle, environ=env_source)
        warm_before_sandbox(spec, env=env, root=root)
        if spec.rss_sources:
            inputs = temporary / "inputs.json"
            stage_rss_inputs(spec, root=root, vault=vault, destination=inputs)
            env["ATELIER_ROUTINE_INPUTS"] = str(inputs)
        if spec.runtime_snapshot:
            snapshot = runtime_capabilities.snapshot(
                root=root, registry=_load_toml(root / "harness/runtimes.toml"), environ=env_source,
            )
            snapshot_file = temporary / "runtime-snapshot.json"
            snapshot_file.write_text(json.dumps({**snapshot, "cycle_id": cycle}), encoding="utf-8")
            env["ATELIER_RUNTIME_SNAPSHOT"] = str(snapshot_file)
        runtime = model_runtime(spec)
        if runtime == "claude":
            argv = claude_argv(spec, root=root, vault=vault)
        else:
            argv = codex_argv(spec, root=root, vault=vault, cwd=cwd, output=result_file)
        prefix = []
        if env_source.get("ATELIER_SKIP_CAFFEINATE") != "1" and shutil.which("caffeinate", path=env_source.get("PATH")):
            prefix = ["caffeinate", "-i", "-s"]
        prompt = adapter_prompt(spec, root=root)
        path = receipt_path(vault, spec.schedule.name, cycle)
        pending_receipt = {
            "contract_version": receipts.VERSION,
            "routine": spec.schedule.name,
            "cycle_id": cycle,
            "prefect_flow_run_id": flow_run_id,
            "profile": spec.profile,
            "profile_fingerprint": spec.profile_values["profile_fingerprint"],
            "runtime": runtime,
            "started_at": started_at,
            "result_summary": "model attempt started; domain outcome is not yet verified",
            "verification": "pending",
        }
        write_receipt(path, pending_receipt)
        seconds = spec.profile_values["timeout_seconds"]
        if runtime == "claude":
            # The prompt goes on stdin so no variadic option can consume it.
            result = execute_process([*prefix, *argv], env=env, cwd=cwd, seconds=seconds, input_text=prompt)
            if not result.returncode:
                write_claude_result(result.stdout, result_file)
        else:
            result = execute_observed_model([*prefix, *argv, prompt], flow_run_id=flow_run_id, env=env, cwd=cwd, seconds=seconds)
        if result.returncode:
            detail = (f": {screened_log(result.stdout)[-300:]}" if runtime == "claude"
                      else "; inspect Prefect state and observation coverage")
            raise ExecutionError(f"{runtime} exited {result.returncode}{detail}")
        outcome = _model_result(result_file, spec, vault=vault, started_at=started_at)
    receipt = {
        "contract_version": receipts.VERSION,
        "routine": spec.schedule.name,
        "cycle_id": cycle,
        "prefect_flow_run_id": flow_run_id,
        "profile": spec.profile,
        "profile_fingerprint": spec.profile_values["profile_fingerprint"],
        "runtime": runtime,
        "started_at": started_at,
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_seconds": int(time.time() - started_epoch),
        "outcome": outcome["outcome"],
        "output_file": outcome["output_file"],
        "artifact_sha256": outcome["artifact_sha256"],
        "verification_scope": outcome["verification_scope"],
        "result_summary": outcome["summary"][:500],
        "skipped_inputs": outcome["skipped_inputs"],
        "verification": "blocked" if outcome["outcome"] == "noop" and outcome["skipped_inputs"] else "passed",
    }
    write_receipt(path, receipt)
    print(f"routine artifact {receipt['verification']}: {receipt['output_file']}", flush=True)
    return receipt


def execute_process_job(
    name: str,
    source: str,
    *,
    root: Path = ROOT,
    environ: dict[str, str] | None = None,
) -> None:
    spec = resolve_process(name, source, root=root, environ=environ)
    env = dict(os.environ if environ is None else environ)
    env.update(spec.environment)
    prefix: list[str] = []
    if env.get("ATELIER_SKIP_CAFFEINATE") != "1" and shutil.which("caffeinate", path=env.get("PATH")):
        prefix = ["caffeinate", "-i", "-s"]
    result = execute_process([*prefix, *spec.argv], env=env, cwd=Path(spec.cwd), seconds=spec.timeout_seconds)
    if result.stdout:
        print(result.stdout.rstrip(), flush=True)
    if result.returncode:
        raise ExecutionError(f"process job exited {result.returncode}")
