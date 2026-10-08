"""Deterministic checks for canonical component kind and visibility roots."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from _findings import Finding, add


_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", re.MULTILINE)


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _name(path: Path) -> str | None:
    try:
        match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if not match:
        return None
    return dict(_FIELD.findall(match.group(1))).get("name")


def _private_roots(
    root: Path, vault: Path, paths: dict[str, Any], findings: list[Finding]
) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for kind in ("skills", "agents", "routines", "tools"):
        key = f"private_{kind}"
        value = paths.get(key)
        if not isinstance(value, str):
            add(findings, "ERROR", "component-private-root", "harness/paths.toml",
                f"paths.{key} must be declared")
            continue
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            add(findings, "ERROR", "component-private-root", "harness/paths.toml",
                f"paths.{key} must stay relative to the vault")
            continue
        roots[kind] = vault / candidate
    return roots


def check(
    root: Path,
    skills: dict[str, str],
    agents: dict[str, dict[str, str]],
    public_routines: dict[str, Any],
    paths: dict[str, Any],
    *,
    vault: Path | None,
) -> list[Finding]:
    """Enforce kind by canonical root and visibility by source boundary."""
    findings: list[Finding] = []
    routine_names = [
        row.get("name") for row in public_routines.get("routine", []) if isinstance(row, dict)
    ]
    if len(routine_names) != len(set(routine_names)):
        add(findings, "ERROR", "component-routine-duplicate", "routines/registry.toml",
            "public routine names must be unique")

    public_shapes = (
        (root / "skills", "*/SKILL.md", set(skills.values()), "skill"),
        (root / "agents", "*.md", {row["path"] for row in agents.values()}, "agent"),
    )
    for base, pattern, declared, kind in public_shapes:
        actual = {_rel(root, path) for path in base.glob(pattern) if path.is_file()}
        for path in sorted(actual - declared):
            add(findings, "ERROR", f"component-{kind}-unregistered", path,
                f"public {kind} source is outside its registry")
        for path in sorted(declared - actual):
            add(findings, "ERROR", f"component-{kind}-missing", path,
                f"registered public {kind} source is missing")

    tool_root = root / "tools"
    if tool_root.is_dir():
        for child in sorted(path for path in tool_root.iterdir() if path.is_dir() and not path.name.startswith(".")):
            if not (child / "README.md").is_file() or (child / "SKILL.md").exists():
                add(findings, "ERROR", "component-tool-shape", _rel(root, child),
                    "public tool packages need README.md and must not contain SKILL.md")
    for base in (root / "tools", root / "routines", root / "agents"):
        if base.is_dir():
            for source in base.rglob("SKILL.md"):
                add(findings, "ERROR", "component-skill-root", _rel(root, source),
                    "SKILL.md is valid only under a skill root")

    if vault is None:
        return findings
    roots = _private_roots(root, vault, paths, findings)

    private_skills = roots.get("skills")
    if private_skills and private_skills.is_dir():
        for child in sorted(path for path in private_skills.iterdir() if path.is_dir() and not path.name.startswith(".")):
            if _name(child / "SKILL.md") != child.name:
                add(findings, "ERROR", "component-private-skill-shape", _rel(root, child),
                    "private skill needs SKILL.md with a matching name")
    private_agents = roots.get("agents")
    if private_agents and private_agents.is_dir():
        for source in sorted(private_agents.glob("*.md")):
            if _name(source) != source.stem:
                add(findings, "ERROR", "component-private-agent-shape", _rel(root, source),
                    "private agent frontmatter name must match its filename")
    private_tools = roots.get("tools")
    if private_tools and private_tools.is_dir():
        for child in sorted(path for path in private_tools.iterdir() if path.is_dir() and not path.name.startswith(".")):
            if not (child / "README.md").is_file() or (child / "SKILL.md").exists():
                add(findings, "ERROR", "component-private-tool-shape", _rel(root, child),
                    "private tool packages need README.md and must not contain SKILL.md")

    private_routines = roots.get("routines")
    if private_routines and private_routines.is_dir():
        registry_path = private_routines / "registry.toml"
        try:
            registry = tomllib.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            add(findings, "ERROR", "component-private-routine-registry", _rel(root, registry_path), str(exc))
            registry = None
        if registry is not None:
            rows = registry.get("routine")
            if registry.get("version") != 1 or not isinstance(rows, list):
                add(findings, "ERROR", "component-private-routine-registry", _rel(root, registry_path),
                    "private routine registry needs version=1 and [[routine]] rows")
            else:
                names: list[str] = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    names.append(str(row.get("name", "")))
                    if row.get("runner") not in {"model", "process"}:
                        add(findings, "ERROR", "component-private-routine-runner", _rel(root, registry_path),
                            "each private routine must declare runner=model|process")
                if len(names) != len(set(names)):
                    add(findings, "ERROR", "component-private-routine-duplicate", _rel(root, registry_path),
                        "private routine names must be unique")
        for child in sorted(path for path in private_routines.iterdir() if path.is_dir() and not path.name.startswith("_")):
            if not (child / "README.md").is_file() or (child / "SKILL.md").exists():
                add(findings, "ERROR", "component-private-routine-shape", _rel(root, child),
                    "private routine packages need README.md and must not contain SKILL.md")
    return findings
