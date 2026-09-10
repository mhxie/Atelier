"""Read-only, bounded CLI discovery. No model, config, auth or history reads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time
from typing import Any

from command_timeout import release_process, stop_process_group, track_process, wait_until_deadline


PROBE_SECONDS = 5
MAX_OUTPUT_BYTES = 65_536
PROBES = {
    "codex": {"version": ("--version",), "help": ("--help",), "exec_help": ("exec", "--help")},
    "claude": {"version": ("--version",), "help": ("--help",)},
}
VERSION_PATTERNS = {
    "codex": r"codex-cli ([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})",
    "claude": r"([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}) \(Claude Code\)",
}
COMMANDS = {"codex": ("exec", "resume", "fork", "mcp", "mcp-server", "plugin")}
FLAGS = {
    "codex": {
        "help": ("--worktree", "--sandbox", "--ask-for-approval"),
        "exec_help": ("--json", "--ephemeral", "--output-schema", "--full-auto"),
    },
    "claude": {"help": (
        "--continue", "--resume", "--fork-session", "--worktree", "--agent",
        "--permission-mode", "--output-format", "--settings", "--print",
    )},
}
DECLARED_SUPPORTS = frozenset({"hooks", "subagents", "skills", "choice-ui"})
SURFACE_FIELDS = ("instruction_file", "agent_dir", "skills_dir", "hooks_file")


def _probe(executable: str, args: tuple[str, ...], *, root: Path, env: dict[str, str]) -> tuple[str, str]:
    """Drain the pipe while enforcing both a byte ceiling and a wall deadline."""
    try:
        process = subprocess.Popen(
            [executable, *args], cwd=root, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        return "start-failed", ""
    track_process(process, grace_seconds=0)
    try:
        deadline = time.time() + PROBE_SECONDS
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return "timeout", ""
                if not selector.select(min(remaining, 0.25)):
                    continue
                chunk = os.read(process.stdout.fileno(), min(8192, MAX_OUTPUT_BYTES + 1 - len(output)))
                if not chunk:
                    code = wait_until_deadline(process, deadline)
                    if code:
                        return "nonzero-exit", ""
                    return "observed", output.decode("utf-8")
                output.extend(chunk)
                if len(output) > MAX_OUTPUT_BYTES:
                    return "output-limit", ""
    except subprocess.TimeoutExpired:
        return "timeout", ""
    except (OSError, UnicodeError):
        return "unreadable-output", ""
    finally:
        try:
            stop_process_group(process, grace_seconds=0)
        finally:
            release_process(process)
            process.stdout.close()


def _local_path(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    path = root / relative
    if any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
        return None
    return path


def _reference(root: Path, runtime: str, entry: dict[str, Any]) -> dict[str, Any]:
    expected = f"sources/runtimes/{runtime}.md"
    value = {"path": expected, "status": "unknown", "sha256": None}
    if entry.get("capability_reference") != expected:
        return value
    path = _local_path(root, expected)
    try:
        if path is not None and path.is_file():
            with path.open("rb") as stream:
                data = stream.read(MAX_OUTPUT_BYTES + 1)
            if 0 < len(data) <= MAX_OUTPUT_BYTES:
                value.update(status="present", sha256=hashlib.sha256(data).hexdigest())
    except OSError:
        pass
    return value


def reference_findings(root: Path, registry: dict[str, Any]) -> list[str]:
    """Validate public reference wiring without executing a native CLI."""
    runtimes = registry.get("runtimes", {})
    return [
        name for name in PROBES
        if _reference(root, name, runtimes.get(name, {}))["status"] != "present"
    ]


def _surface_presence(root: Path, entry: dict[str, Any]) -> dict[str, str]:
    result = {}
    for field in SURFACE_FIELDS:
        path = _local_path(root, entry.get(field))
        try:
            result[field] = "unknown" if path is None else "present" if path.exists() else "missing"
        except OSError:
            result[field] = "unknown"
    return result


def _cli_surfaces(runtime: str, results: dict[str, tuple[str, str]]) -> dict[str, str]:
    surfaces = {}
    status, help_text = results["help"]
    section = re.search(r"(?ms)^Commands:\s*\n(.*?)(?=^\S|\Z)", help_text)
    root_help = re.search(rf"(?m)^Usage: {runtime}(?:\s|$)", help_text)
    for command in COMMANDS.get(runtime, ()):
        present = section and re.search(rf"(?m)^\s{{2,}}{re.escape(command)}\s", section[1])
        surfaces[command] = "unknown" if status != "observed" or not section or not root_help else (
            "advertised" if present else "not-advertised"
        )
    for probe, flags in FLAGS[runtime].items():
        status, help_text = results[probe]
        # A success exit with unrelated help text is not feature evidence.
        options = re.search(r"(?ms)^Options:\s*\n(.*?)(?=^\S|\Z)", help_text)
        recognized = re.search(rf"(?m)^Usage: {runtime}(?:\s|$)", help_text) and options
        if probe == "exec_help":
            recognized = recognized and re.search(r"(?m)^Usage: codex exec\b", help_text)
        for flag in flags:
            present = re.search(rf"(?m)^\s+(?:-[A-Za-z?],?\s+)?{re.escape(flag)}(?:[\s=,]|$)", options[1] if options else "")
            surfaces[flag] = "unknown" if status != "observed" or not recognized else (
                "advertised" if present else "not-advertised"
            )
    return surfaces


def snapshot(*, root: Path, registry: dict[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return allowlisted observations, never inferred activation or usability."""
    source_env = os.environ if environ is None else environ
    clean_env = {key: source_env[key] for key in ("PATH", "HOME") if source_env.get(key)}
    clean_env["LANG"] = "C"
    runtimes = {}
    for name, probes in PROBES.items():
        entry = registry.get("runtimes", {}).get(name, {})
        # Registry paths/argv and private inputs cannot select probe commands.
        executable = shutil.which(name, path=source_env.get("PATH", ""))
        if executable:
            executable = str(Path(executable).absolute())
        results = {
            key: _probe(executable, args, root=root, env=clean_env) if executable else ("not-found", "")
            for key, args in probes.items()
        }
        version_status, version_output = results["version"]
        match = re.fullmatch(VERSION_PATTERNS[name], version_output.strip()) if version_status == "observed" else None
        if version_status == "observed" and match is None:
            version_status = "unrecognized-version"
        declared = entry.get("supports", [])
        if not isinstance(declared, list):
            declared = []
        runtimes[name] = {
            "version": match[1] if match else None,
            "version_status": version_status,
            "probe_status": {key: result[0] for key, result in results.items()},
            "cli_surfaces": _cli_surfaces(name, results),
            "baseline": _reference(root, name, entry),
            "declared_supports": sorted(item for item in declared if isinstance(item, str) and item in DECLARED_SUPPORTS),
            "declaration_paths": _surface_presence(root, entry),
            "effective_configuration": "unknown",
            "enabled": "unknown",
            "usable": "unknown",
            "verified_in_use": "unknown",
        }
    return {
        "schema": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "standalone-cli-discovery-only",
        "runtimes": runtimes,
    }
