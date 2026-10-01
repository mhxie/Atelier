#!/usr/bin/env python3
"""Select and launch Atelier's native Claude Code or Codex surface.

This is a runtime selector, not a prompt bridge. It sends a stable workflow
name to the selected CLI in that runtime's native syntax:

    Codex       $hi
    Claude Code /hi

The committed default lives in `harness/runtimes.toml`. A gitignored
`harness/runtime.local.toml` can persist a user preference, and
`ATELIER_RUNTIME` or `--runtime` can override a single process.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "harness" / "runtimes.toml"
SKILLS_PATH = ROOT / "harness" / "skills.toml"


class RuntimeConfigError(ValueError):
    """Raised when the committed or local runtime configuration is invalid."""


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise RuntimeConfigError(f"missing runtime config: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeConfigError(f"runtime config is not a table: {path}")
    return data


def load_registry() -> dict[str, Any]:
    data = load_toml(REGISTRY_PATH)
    runtime = data.get("runtime")
    runtimes = data.get("runtimes")
    if not isinstance(runtime, dict) or not isinstance(runtimes, dict) or not runtimes:
        raise RuntimeConfigError(
            "harness/runtimes.toml must define [runtime] and at least one [runtimes.<name>]"
        )

    default = runtime.get("default")
    if not isinstance(default, str) or default not in runtimes:
        raise RuntimeConfigError(f"committed runtime default is unknown: {default!r}")

    local_override = runtime.get("local_override")
    environment_override = runtime.get("environment_override")
    if not isinstance(local_override, str) or not local_override:
        raise RuntimeConfigError("runtime.local_override must be a non-empty path")
    if not isinstance(environment_override, str) or not environment_override:
        raise RuntimeConfigError("runtime.environment_override must be a non-empty name")

    required = {
        "label": str,
        "executable": str,
        "command_prefix": str,
        "shell_args": list,
        "interactive_args": list,
        "non_interactive_args": list,
    }
    for name, entry in runtimes.items():
        if not isinstance(entry, dict):
            raise RuntimeConfigError(f"runtimes.{name} must be a table")
        for field, expected_type in required.items():
            value = entry.get(field)
            if not isinstance(value, expected_type):
                raise RuntimeConfigError(
                    f"runtimes.{name}.{field} must be {expected_type.__name__}"
                )
        prefix = entry["command_prefix"]
        if not isinstance(prefix, str) or len(prefix) > 1:
            raise RuntimeConfigError(
                f"runtimes.{name}.command_prefix must be a single character (or empty)"
            )
        for field, value in entry.items():
            if field.endswith("_args"):
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise RuntimeConfigError(
                        f"runtimes.{name}.{field} must be a list of strings"
                    )
    return data


def local_config_path(registry: dict[str, Any]) -> Path:
    relative = Path(str(registry["runtime"]["local_override"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeConfigError("runtime.local_override must stay inside the repository")
    return ROOT / relative


def validate_runtime(name: Any, runtimes: dict[str, Any], source: str) -> str:
    if not isinstance(name, str) or name not in runtimes:
        known = ", ".join(sorted(runtimes))
        raise RuntimeConfigError(f"unknown runtime {name!r} from {source}; expected one of: {known}")
    return name


def resolve_runtime(registry: dict[str, Any]) -> tuple[str, str]:
    runtime_config = registry["runtime"]
    runtimes = registry["runtimes"]
    env_name = runtime_config["environment_override"]
    env_value = os.environ.get(env_name)
    if env_value:
        return validate_runtime(env_value, runtimes, env_name), "environment"

    override_path = local_config_path(registry)
    if override_path.exists():
        override = load_toml(override_path)
        unknown_top = sorted(set(override) - {"runtime"})
        runtime = override.get("runtime")
        if unknown_top or not isinstance(runtime, dict):
            raise RuntimeConfigError(
                f"{override_path} may contain only a [runtime] table"
            )
        unknown_fields = sorted(set(runtime) - {"default"})
        if unknown_fields:
            raise RuntimeConfigError(
                f"unsupported local runtime fields: {', '.join(unknown_fields)}"
            )
        return validate_runtime(runtime.get("default"), runtimes, str(override_path)), "local"

    default = validate_runtime(runtime_config.get("default"), runtimes, str(REGISTRY_PATH))
    return default, "committed"


def load_skills() -> dict[str, Any]:
    data = load_toml(SKILLS_PATH)
    skills = data.get("skills")
    if not isinstance(skills, dict):
        raise RuntimeConfigError("harness/skills.toml has no [skills] table")
    return skills


def normalize_skill(raw: str) -> str:
    name = raw.strip()
    if name.startswith(("$", "/")):
        name = name[1:]
    if not name or any(char.isspace() for char in name):
        raise RuntimeConfigError(f"invalid workflow name: {raw!r}")
    return name


def require_user_skill(raw: str) -> tuple[str, dict[str, Any]]:
    name = normalize_skill(raw)
    entry = load_skills().get(name)
    if not isinstance(entry, dict):
        raise RuntimeConfigError(f"workflow is not registered: {name}")
    if entry.get("user_facing", True) is False:
        raise RuntimeConfigError(
            f"workflow {name!r} is bot-only and cannot be launched through the user selector"
        )
    return name, entry


def expand_args(args: list[str]) -> list[str]:
    root = str(ROOT)
    return [item.replace("{root}", root) for item in args]


def runtime_entry(registry: dict[str, Any], explicit: str | None) -> tuple[str, str, dict[str, Any]]:
    runtimes = registry["runtimes"]
    if explicit is not None:
        name = validate_runtime(explicit, runtimes, "--runtime")
        return name, "command line", runtimes[name]
    name, source = resolve_runtime(registry)
    return name, source, runtimes[name]


def build_workflow_argv(
    entry: dict[str, Any],
    prompt: str,
    *,
    non_interactive: bool,
    resume: bool,
    fork: bool,
) -> list[str]:
    if resume:
        key = "resume_non_interactive_args" if non_interactive else "resume_interactive_args"
    elif fork:
        key = "fork_non_interactive_args" if non_interactive else "fork_interactive_args"
    else:
        key = "non_interactive_args" if non_interactive else "interactive_args"

    args = entry.get(key)
    if not isinstance(args, list):
        raise RuntimeConfigError(f"selected runtime does not support this session mode ({key})")
    return [entry["executable"], *expand_args(args), prompt]


def execute(argv: list[str], *, dry_run: bool, active_runtime: str | None = None, observe: bool = False) -> int:
    env = os.environ.copy()
    if observe:
        from observability.native import launch_options

        try:
            extra, env = launch_options(active_runtime, env)
        except ValueError as exc:
            raise RuntimeConfigError(str(exc)) from exc
        argv = [argv[0], *extra, *argv[1:]]
    if dry_run:
        print(shlex.join(argv))
        return 0
    if shutil.which(argv[0]) is None:
        raise RuntimeConfigError(f"runtime executable not found on PATH: {argv[0]}")
    if active_runtime is not None:
        env["ATELIER_ACTIVE_RUNTIME"] = active_runtime
    return subprocess.run(argv, cwd=ROOT, env=env).returncode


def cmd_status(args: argparse.Namespace) -> int:
    registry = load_registry()
    if args.observations:
        from observability.native import summary

        print(json.dumps(summary(), indent=2, sort_keys=True))
        return 0
    if args.capabilities:
        from runtime.capabilities import snapshot

        print(json.dumps(snapshot(root=ROOT, registry=registry), indent=2, sort_keys=True))
        return 0
    name, source = resolve_runtime(registry)
    local_path = local_config_path(registry)
    runtimes = registry["runtimes"]
    payload = {
        "runtime": name,
        "source": source,
        "committed_default": registry["runtime"]["default"],
        "environment_override": registry["runtime"]["environment_override"],
        "local_override": str(local_path.relative_to(ROOT)),
        "local_override_exists": local_path.exists(),
        "available": {
            runtime_name: {
                "label": entry["label"],
                "executable": entry["executable"],
                "installed": shutil.which(entry["executable"]) is not None,
                "command_prefix": entry["command_prefix"],
            }
            for runtime_name, entry in sorted(runtimes.items())
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"default runtime: {name} ({source})")
    print(f"committed default: {payload['committed_default']}")
    print(f"local override: {payload['local_override']}")
    print(f"environment override: {payload['environment_override']}")
    print("available runtimes:")
    for runtime_name, info in payload["available"].items():
        installed = "installed" if info["installed"] else "not found"
        print(f"  {runtime_name}: {info['label']} ({installed}, prefix {info['command_prefix']})")
    return 0


def cmd_use(args: argparse.Namespace) -> int:
    registry = load_registry()
    validate_runtime(args.runtime, registry["runtimes"], "use")
    path = local_config_path(registry)
    content = (
        "# Local Atelier runtime preference. This file is gitignored.\n\n"
        "[runtime]\n"
        f'default = "{args.runtime}"\n'
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".runtime.local.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    print(f"default runtime set to {args.runtime} in {path.relative_to(ROOT)}")
    env_name = registry["runtime"]["environment_override"]
    if os.environ.get(env_name):
        print(
            f"note: {env_name}={os.environ[env_name]} overrides this preference in the current environment",
            file=sys.stderr,
        )
    return 0


def cmd_shell(args: argparse.Namespace) -> int:
    registry = load_registry()
    name, _, entry = runtime_entry(registry, args.runtime)
    argv = [entry["executable"], *expand_args(entry["shell_args"])]
    return execute(argv, dry_run=args.dry_run, active_runtime=name, observe=args.observe)


def cmd_run(args: argparse.Namespace) -> int:
    registry = load_registry()
    runtime_name, _, runtime = runtime_entry(registry, args.runtime)
    name, skill = require_user_skill(args.skill)
    context = list(args.context)
    if context and context[0] == "--":
        context = context[1:]
    prompt = f"{runtime['command_prefix']}{name}"
    if context:
        prompt = f"{prompt} {' '.join(context).strip()}"

    if (args.resume or args.fork) and not skill.get("resume_friendly", False):
        print(
            f"warning: {name!r} is not marked resume_friendly; prior context may pollute this workflow",
            file=sys.stderr,
        )
    argv = build_workflow_argv(
        runtime,
        prompt,
        non_interactive=args.non_interactive,
        resume=args.resume,
        fork=args.fork,
    )
    return execute(argv, dry_run=args.dry_run, active_runtime=runtime_name, observe=args.observe)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select and launch Atelier's native Codex or Claude Code surface."
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    status = subparsers.add_parser("status", help="Show runtime preference and installation status.")
    status.add_argument("--json", action="store_true")
    status_view = status.add_mutually_exclusive_group()
    status_view.add_argument("--capabilities", action="store_true", help="Probe fixed version/help surfaces and emit JSON; no model launch.")
    status_view.add_argument("--observations", action="store_true", help="Read local opt-in session measurements; JSON output.")
    status.set_defaults(func=cmd_status)

    use = subparsers.add_parser("use", help="Persist the local default runtime.")
    use.add_argument("runtime", choices=_registered_runtime_names())
    use.set_defaults(func=cmd_use)

    shell = subparsers.add_parser("shell", help="Open the selected runtime without a workflow.")
    shell.add_argument("--runtime", choices=_registered_runtime_names())
    shell.add_argument("--dry-run", action="store_true")
    shell.add_argument("--observe", action="store_true", help="Opt this process into local native telemetry.")
    shell.set_defaults(func=cmd_shell)

    run = subparsers.add_parser(
        "run",
        help="Launch a registered workflow natively.",
        description=(
            "Launch a registered workflow natively. Place selector options before "
            "the workflow name; all remaining words become workflow context."
        ),
    )
    run.add_argument("--runtime", choices=_registered_runtime_names())
    run.add_argument("--non-interactive", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--observe", action="store_true", help="Opt this process into local native telemetry.")
    session = run.add_mutually_exclusive_group()
    session.add_argument("--resume", action="store_true")
    session.add_argument("--fork", action="store_true")
    run.add_argument("skill", help="Registered skill name, with or without its runtime prefix.")
    run.add_argument("context", nargs=argparse.REMAINDER, help="Optional workflow context.")
    run.set_defaults(func=cmd_run)
    return parser


def _registered_runtime_names() -> tuple[str, ...]:
    """Runtime names from harness/runtimes.toml; shipped pair on any error."""
    try:
        import tomllib

        with (ROOT / "harness" / "runtimes.toml").open("rb") as handle:
            names = tuple(sorted(tomllib.load(handle).get("runtimes", {})))
        return names or ("claude", "codex")
    except Exception:
        return ("claude", "codex")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RuntimeConfigError as exc:
        print(f"atelier runtime: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
