#!/usr/bin/env python3
"""Check cross-runtime harness contracts, registries, and generated edges.

`run_lints` owns the checks; `--footprint` reports public size and source budgets.
Exit 0 without ERROR findings, 1 with errors; argparse uses 2 for usage errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
from render_runtime_edges import TIER_TO_EFFORT  # noqa: E402  (single owner of the tier map)
from _git import git_paths  # noqa: E402
import component_taxonomy  # noqa: E402

SEVERITY_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}
PUBLIC_CONFIGS = (
    *(f"harness/{name}.toml" for name in ("models", "agents", "skills", "capabilities", "runtimes", "intents", "paths")),
    "routines/registry.toml", ".codex/hooks.json", ".claude/settings.json",
)


import _findings  # noqa: E402
from _findings import Finding, add as _add  # noqa: E402


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_toml(path: Path) -> tuple[dict[str, Any] | None, Finding | None]:
    try:
        return tomllib.loads(_read(path)), None
    except FileNotFoundError:
        return None, Finding("ERROR", "missing-file", rel(path), f"`{rel(path)}` is missing")
    except tomllib.TOMLDecodeError as exc:
        return None, Finding("ERROR", "invalid-toml", rel(path), str(exc))


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def load_harness_config() -> tuple[dict[str, Any], list[Finding]]:
    """Validate one public-config snapshot before any cross-file checks.

    The schema owns structural rules, not permission to read arbitrary files.
    The CLI reads JSON on stdin; private overlays never enter this batch.
    """
    data: dict[str, Any] = {}
    findings: list[Finding] = []
    for name in PUBLIC_CONFIGS:
        try:
            raw = _read(ROOT / name)
            data[name] = tomllib.loads(raw) if name.endswith(".toml") else json.loads(raw)
        except (OSError, ValueError) as exc:
            _add(findings, "ERROR", "registry-read", name, str(exc))
    if findings:
        return {}, findings
    findings = validate_harness_config(data)
    return ({} if findings else data), findings


def validate_harness_config(data: dict[str, Any]) -> list[Finding]:
    """Use the pinned CLI's JSON report, without exposing its Python internals."""
    schema_path = ROOT / "harness" / "registry.schema.json"
    findings: list[Finding] = []
    try:
        python = ROOT / ".venv" / "bin" / "python"
        proc = subprocess.run(
            [str(python) if python.is_file() else sys.executable, "-m", "check_jsonschema",
             "--schemafile", str(schema_path), "--output-format", "json", "--no-cache",
             "--force-filetype", "json", "-"],
            # TOML dates/times have no JSON scalar equivalent. Null rejects
            # them in live typed fields without banning ignored metadata.
            input=json.dumps(data, default=lambda value: None), capture_output=True, text=True, timeout=30,
        )
        if proc.returncode not in (0, 1):
            raise ValueError(f"check-jsonschema exited {proc.returncode}")
        report = json.loads(proc.stdout)
        if not isinstance(report, dict):
            raise ValueError("check-jsonschema returned a non-object report")
        if (proc.returncode == 0 and report.get("status") == "ok"
                and not report.get("errors") and not report.get("parse_errors")):
            # JSON Schema treats 8192.0 as an integer; context_bundle requires
            # an actual int. Keep this runtime representation check here.
            return [Finding("ERROR", "registry-schema",
                            f"harness/intents.toml:$.intents.{name}.context_budget_tokens",
                            "context_budget_tokens must be a TOML integer, not a float")
                    for name, entry in data["harness/intents.toml"]["intents"].items()
                    if type(entry["context_budget_tokens"]) is not int]
        if proc.returncode != 1 or report.get("status") != "fail":
            raise ValueError("check-jsonschema returned an inconsistent status")
        for error in report.get("errors", []):
            path = error["path"]
            if not isinstance(path, str) or not isinstance(error["message"], str):
                raise ValueError("check-jsonschema returned an invalid error record")
            where = f"{rel(schema_path)}:{path}"
            for name in data:
                prefix = f"$['{name}']"
                if path.startswith(prefix):
                    where = f"{name}:${path[len(prefix):]}"
                    break
            _add(findings, "ERROR", "registry-schema", where, error["message"])
        if not findings:
            _add(findings, "ERROR", "registry-validator", rel(schema_path),
                 "check-jsonschema did not produce a successful validation report")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        _add(findings, "ERROR", "registry-validator", rel(schema_path),
             f"cannot validate harness configuration: {exc}; run `uv sync --locked` to install check-jsonschema")
    return findings


FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", re.MULTILINE)


def parse_agent_frontmatter(path: Path) -> dict[str, str]:
    text = _read(path)
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for key, value in FIELD_RE.findall(match.group(1)):
        fields[key] = value
    return fields


def load_canonical_agents() -> tuple[dict[str, dict[str, str]], list[Finding]]:
    findings: list[Finding] = []
    agents: dict[str, dict[str, str]] = {}
    agent_dir = ROOT / "agents"
    if not agent_dir.exists():
        return agents, [
            Finding("ERROR", "missing-agent-dir", "agents", "canonical agent directory is missing")
        ]

    for path in sorted(agent_dir.glob("*.md")):
        fields = parse_agent_frontmatter(path)
        name = fields.get("name")
        if not name:
            _add(findings, "ERROR", "agent-frontmatter", rel(path), "missing `name` in frontmatter")
            continue
        agents[name] = {
            "path": rel(path),
            "model": fields.get("model", ""),
            "tools": fields.get("tools", ""),
        }
    return agents, findings


def git_list(paths: list[str], *, others: bool = False) -> tuple[list[str], Finding | None]:
    args = ["ls-files"]
    if others:
        args.extend(["-o", "--exclude-standard"])
    args.extend(paths)
    try:
        return git_paths(ROOT, *args), None
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return [], Finding("ERROR", "git-ls-files", "git", str(exc))


def load_canonical_skills() -> tuple[dict[str, str], list[Finding]]:
    skills: dict[str, str] = {}
    findings: list[Finding] = []
    for source in sorted((ROOT / "skills").glob("*/SKILL.md")):
        path = rel(source)
        name = source.parent.name
        if name in skills:
            _add(findings, "ERROR", "skill-duplicate", path,
                     f"duplicate skill name `{name}` also appears at `{skills[name]}`")
            continue
        skills[name] = path
    return skills, findings


def check_root_files() -> list[Finding]:
    findings: list[Finding] = []

    agents_path = ROOT / "AGENTS.md"
    runtime_path = ROOT / "protocols" / "runtime-adapters.md"

    if not agents_path.exists():
        _add(findings, "ERROR", "missing-agents-md", "AGENTS.md", "Shared root instructions are missing")
    else:
        text = _read(agents_path)
        if "protocols/runtime-adapters.md" not in text:
            _add(findings, "ERROR", "agents-contract", "AGENTS.md",
                     "AGENTS.md must point to protocols/runtime-adapters.md")

        size = agents_path.stat().st_size
        if size > 15_000:
            _add(findings, "ERROR", "agents-size", "AGENTS.md",
                     f"AGENTS.md is {size} bytes; hard ceiling is 15000 bytes")
        elif size > 8_192:
            _add(findings, "WARN", "agents-size", "AGENTS.md", f"AGENTS.md is {size} bytes; target is under 8192 bytes")
        bold_count = text.count("**")
        if bold_count:
            _add(findings, "INFO", "agents-bold", "AGENTS.md", f"AGENTS.md contains {bold_count} bold markers")

    if not runtime_path.exists():
        _add(findings, "ERROR", "missing-runtime-adapters", rel(runtime_path), "runtime adapter protocol is missing")

    return findings


def check_models(
    agents: dict[str, dict[str, str]],
    models: dict[str, Any],
    registry: dict[str, Any],
) -> tuple[list[Finding], dict[str, Any]]:
    """Check renderer tier references and optional machine-local bindings."""
    findings: list[Finding] = []
    for model_name, entry in sorted(models.items()):
        if entry["reasoning_tier"] not in TIER_TO_EFFORT:
            _add(findings, "ERROR", "models-reasoning-tier", "harness/models.toml",
                     f"model `{model_name}` reasoning_tier must be one of the tiers in render_runtime_edges.TIER_TO_EFFORT")
    findings.extend(_check_model_bindings(agents, models, registry))
    return findings, models


def _check_model_bindings(
    agents: dict[str, dict[str, str]],
    models: dict[str, Any],
    registry: dict[str, Any],
) -> list[Finding]:
    """A fresh clone need not have private bindings; validate them if present."""
    findings: list[Finding] = []
    bindings_path = ROOT / "profile" / "models.toml"
    binding_models = {}
    if bindings_path.exists():
        data, err = _load_toml(bindings_path)
        if err:
            return [err]
        binding_models = data.get("models", {}) or {}
        for name, entry in sorted(models.items()):
            if entry.get("binding_optional") is not True and name not in binding_models:
                _add(findings, "WARN", "models-binding-missing", "profile/models.toml",
                     f"schema model `{name}` has no binding entry")
    for name, frontmatter in agents.items():
        entry = registry.get(name)
        if entry is None:
            continue
        native_id = entry["voices"].get("native")
        fm_model = frontmatter.get("model")
        if native_id and fm_model and native_id != fm_model:
            binding = binding_models.get(native_id, {}) or {}
            cc = binding.get("claude_code")
            expected_native = cc if isinstance(cc, str) else native_id
            if fm_model != expected_native:
                _add(findings, "WARN", "models-claude-drift", frontmatter.get("path", f"agents/{name}.md"),
                     f"frontmatter model `{fm_model}` differs from native voice `{native_id}` (expected `{expected_native}` per profile/models.toml)")
    return findings


def check_runtime_registry(data: dict[str, Any]) -> list[Finding]:
    """Validate the native runtime registry and local-selection contract."""
    from runtime.capabilities import reference_findings

    findings: list[Finding] = []
    path = ROOT / "harness" / "runtimes.toml"
    for runtime in reference_findings(ROOT, data):
        _add(findings, "ERROR", "runtime-capability-reference", rel(path),
             f"{runtime} needs its bounded public capability reference")
    runtimes = data["runtimes"]
    declared_prefixes = [entry["command_prefix"] for entry in runtimes.values()]
    if len(declared_prefixes) != len(set(declared_prefixes)):
        _add(findings, "ERROR", "runtime-prefix-collision", rel(path),
                 "every declared runtime needs a unique command_prefix")
    supporting_paths = (
        ROOT / "harness" / "runtime.local.toml.example",
        ROOT / "scripts" / "atelier_runtime.py",
    )
    for supporting_path in supporting_paths:
        if not supporting_path.exists():
            _add(findings, "ERROR", "runtime-support-file", rel(supporting_path),
                     "runtime selector support file is missing")

    gitignore = _read(ROOT / ".gitignore")
    if "harness/runtime.local.toml" not in gitignore:
        _add(findings, "ERROR", "runtime-local-ignore", ".gitignore",
                 "the per-user runtime preference must remain gitignored")
    local_path = ROOT / "harness" / "runtime.local.toml"
    if local_path.exists():
        local_data, local_err = _load_toml(local_path)
        if local_err:
            findings.append(local_err)
        else:
            assert local_data is not None
            local_runtime = local_data.get("runtime")
            local_default = local_runtime.get("default") if isinstance(local_runtime, dict) else None
            if local_default not in ("codex", "claude"):
                _add(findings, "ERROR", "runtime-local-default", rel(local_path),
                         "local runtime default must be one of ['claude', 'codex']")

    return findings


def check_agent_registry(
    agents: dict[str, dict[str, str]],
    models: dict[str, Any],
    registry: dict[str, Any],
) -> list[Finding]:
    """Resolve voices and sources; retain the shared Forgetter envelope guard."""
    findings: list[Finding] = []
    for name, fields in sorted(agents.items()):
        entry = registry.get(name)
        if entry is None:
            _add(findings, "ERROR", "agents-registry-entry-missing", "harness/agents.toml",
                     f"agent `{name}` from `{fields['path']}` has no registry entry")
            continue
        source = entry["source"]
        if source != fields["path"]:
            _add(findings, "ERROR", "agents-registry-source-drift", "harness/agents.toml",
                     f"agent `{name}` source `{source}` differs from discovered path `{fields['path']}`")
        if name == "forgetter":
            canonical_marker = "---forgetter-result---"
            legacy_marker = "---begin-result---"
            contract_paths = (
                ROOT / str(source),
                ROOT / "protocols" / "agent-handoff.md",
                ROOT / "protocols" / "intent-forget.md",
                ROOT / "routines" / "_adapters" / "autoevo" / "PROCEDURE.md",
            )
            for contract_path in contract_paths:
                try:
                    contract = contract_path.read_text(encoding="utf-8")
                except OSError as exc:
                    _add(findings, "ERROR", "forgetter-envelope-read", rel(contract_path),
                             f"cannot read Forgetter contract: {exc}")
                    continue
                if canonical_marker not in contract or legacy_marker in contract:
                    _add(findings, "ERROR", "forgetter-envelope-drift", rel(contract_path),
                             "Forgetter contract must use only `---forgetter-result---` as its opening marker")

    for name, entry in sorted(registry.items()):
        source = entry["source"]
        is_script_driven = entry["status"] == "script-driven"
        if name not in agents and not is_script_driven:
            _add(findings, "WARN", "agents-registry-entry-extra", "harness/agents.toml",
                     f"registry agent `{name}` has no canonical agent source")
        for leg, model_ref in entry["voices"].items():
            if model_ref not in models:
                _add(findings, "ERROR", "agents-voices-unknown-model", "harness/agents.toml",
                     f"agent `{name}` voices leg `{leg}` references unknown model `{model_ref}`")
        if not (ROOT / source).exists():
            _add(findings, "ERROR", "agents-registry-source-missing", "harness/agents.toml",
                     f"agent `{name}` source `{source}` does not exist")
        if not is_script_driven and Path(source).stem != name:
            _add(findings, "WARN", "agents-registry-name-drift", "harness/agents.toml",
                     f"registry key `{name}` differs from source stem `{Path(source).stem}`")

    return findings


def check_runtime_edges(config: dict[str, Any]) -> list[Finding]:
    """Compare generated bytes; source/schema checks remain independent above.

    Template semantics have independent fixtures in tests/test_render_edges.py.
    Inventory the native surfaces separately: renderer output alone cannot
    reveal an obsolete or unregistered adapter left on disk.
    """
    import render_runtime_edges as edges

    registries = {name: config[f"harness/{name}.toml"] for name in ("agents", "skills", "models")}
    for name in edges.HAND_WRITTEN_SKILLS & registries["skills"]["skills"].keys():
        return [Finding("ERROR", "skill-reserved", "harness/skills.toml",
                        f"public skill `{name}` collides with a handwritten runtime skill")]
    try:
        expected = {ROOT / path.relative_to(edges.ROOT): content.encode("utf-8")
                    for path, content in edges.render_all(
                        registries["agents"], registries["skills"], registries["models"]
                    ).items()}
    except (SystemExit, AttributeError, KeyError, TypeError, ValueError) as exc:
        return [Finding("ERROR", "runtime-edge-render", "harness/", str(exc))]

    skills = ROOT / ".agents" / "skills"
    actual = set((ROOT / ".codex" / "agents").glob("*.toml"))
    actual.update((ROOT / ".claude" / "agents").glob("*.md"))
    actual.update((ROOT / ".claude" / "commands").glob("*.md"))
    for pattern in ("*/SKILL.md", "*/agents/openai.yaml"):
        actual.update(path for path in skills.glob(pattern)
                      if path.relative_to(skills).parts[0] not in edges.HAND_WRITTEN_SKILLS)
    findings = [Finding("ERROR", "runtime-edge-unregistered", rel(path),
                        "unexpected generated edge; remove it or register its owner")
                for path in sorted(actual - expected.keys())]
    for path, content in sorted(expected.items()):
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            _add(findings, "ERROR", "runtime-edge-missing", rel(path),
                     "generated edge is missing; re-render the registries")
        except OSError as exc:
            _add(findings, "ERROR", "runtime-edge-read", rel(path), str(exc))
        else:
            if current != content:
                _add(findings, "ERROR", "runtime-edge-drift", rel(path),
                         "generated edge differs; run render_runtime_edges.py --runtime all --apply")
            elif path.suffix == ".toml":
                try:
                    adapter = tomllib.loads(current.decode("utf-8"))
                except tomllib.TOMLDecodeError as exc:
                    _add(findings, "ERROR", "invalid-toml", rel(path), str(exc))
                else:
                    row = registries["agents"]["agents"][path.stem]
                    if (adapter.get("name") != path.stem
                            or adapter.get("description") != str(row.get("description", ""))):
                        _add(findings, "ERROR", "runtime-edge-values", rel(path),
                                 "parsed name/description differs from the registry")
    return findings


def check_hooks(payload: dict[str, Any], runtime: str) -> list[Finding]:
    """Keep required lifecycle commands wired after hook shape validation."""
    path = ".codex/hooks.json" if runtime == "codex" else ".claude/settings.json"
    findings: list[Finding] = []
    required = (
        ("SessionStart", ("scripts/cues.py", "--hook", f"--runtime {runtime}")),
        *((event, ("scripts/autoevo_preflight.py", "--touch-lock"))
          for event in ("UserPromptSubmit", "PostToolUse", "Stop")),
    )
    for event, needles in required:
        commands = [handler["command"] for group in payload["hooks"].get(event, [])
                    for handler in group["hooks"] if "command" in handler]
        if not any(all(needle in command for needle in needles) for command in commands):
            _add(findings, "ERROR", f"{runtime}-hook-routing", path,
                     f"{event} must include a command containing {list(needles)}")
    return findings


# the agent-frontmatter hook events Claude Code has been seen to fire for a
# subagent's own tool calls (the Reviewer's Bash guard); add an event here
# only with a live check behind it
AGENT_HOOK_EVENTS = ("PreToolUse",)


def check_agent_hooks(agent_dir: Path | None = None) -> list[Finding]:
    """Agent-frontmatter hooks (Claude Code only) name supported events and
    scripts that exist."""
    findings: list[Finding] = []
    for path in sorted((agent_dir or ROOT / "agents").glob("*.md")):
        match = FRONTMATTER_RE.match(_read(path))
        if not match:
            continue
        frontmatter = match.group(1)
        start = re.search(r"(?m)^hooks:\s*$", frontmatter)
        if start is None:
            continue
        block = frontmatter[start.end() :]
        next_key = re.search(r"(?m)^[A-Za-z_][A-Za-z0-9_-]*:", block)
        if next_key:
            block = block[: next_key.start()]
        nested = re.search(r"(?m)^([ \t]+)[A-Za-z]", block)  # the events' own indentation, whatever the file uses
        events = re.findall(r"(?m)^" + re.escape(nested.group(1)) + r"([A-Za-z]+):", block) if nested else []
        for event in events:
            if event not in AGENT_HOOK_EVENTS:
                _add(findings, "ERROR", "agent-hook-event", rel(path), f"unsupported agent hook event `{event}`")
        commands = [
            value for value in re.findall(r"(?m)^\s*command:\s*(.+?)\s*$", block) if value not in ("|", ">", "|-", ">-")
        ]
        for scalar in re.finditer(r"(?m)^(\s*)command:\s*[|>]-?\s*$", block):
            indent = len(scalar.group(1))
            lines: list[str] = []
            for line in block[scalar.end() :].split("\n")[1:]:
                if line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                lines.append(line.strip())
            commands.append(" ".join(part for part in lines if part))
        if not commands:
            _add(findings, "ERROR", "agent-hook-command", rel(path), "hooks block declares no command")
        for command in commands:
            for script in re.findall(r"scripts/[\w./-]+", command):
                if not (ROOT / script).is_file():
                    _add(findings, "ERROR", "agent-hook-script", rel(path),
                             f"hook command names a missing script `{script}`")
    return findings


def check_skills(skills: dict[str, str], skill_map: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for name, path in sorted(skills.items()):
        entry = skill_map.get(name)
        if entry is None:
            _add(findings, "ERROR", "skills-entry-missing", "harness/skills.toml",
                     f"skill `{name}` from `{path}` has no registry entry")
            continue
        source = entry["source"]
        if source != path:
            _add(findings, "ERROR", "skills-source-drift", "harness/skills.toml",
                     f"skill `{name}` source `{source}` differs from discovered path `{path}`")

    for name, entry in sorted(skill_map.items()):
        source = entry["source"]
        if name not in skills:
            _add(findings, "WARN", "skills-entry-extra", "harness/skills.toml",
                     f"registry skill `{name}` has no canonical source")
        source_path = ROOT / source
        if not source_path.exists():
            _add(findings, "ERROR", "skills-source-missing", "harness/skills.toml",
                     f"skill `{name}` source `{source}` does not exist")
        if Path(source).parent.name != name:
            _add(findings, "WARN", "skills-name-drift", "harness/skills.toml",
                     f"registry key `{name}` differs from source directory `{Path(source).parent.name}`")

    return findings


def check_component_taxonomy(
    skills: dict[str, str],
    agents: dict[str, dict[str, str]],
    public_routines: dict[str, Any],
    paths: dict[str, Any],
) -> list[Finding]:
    ov_raw = os.environ.get("OV")
    return component_taxonomy.check(
        ROOT, skills, agents, public_routines, paths,
        vault=Path(ov_raw).expanduser().resolve() if ov_raw else None,
    )


def check_harness_readme() -> list[Finding]:
    path = ROOT / "harness" / "README.md"
    if not path.exists():
        return [
            Finding(
                "ERROR",
                "harness-readme-missing",
                rel(path),
                "portable harness reference is missing",
            )
        ]
    text = _read(path)
    findings: list[Finding] = []
    for needle in ("skills.toml", "agents.toml", "models.toml", "capabilities.toml", "runtimes.toml", "routines/registry.toml", ".agents/skills"):
        if needle not in text:
            _add(findings, "ERROR", "harness-readme-reference", rel(path), f"harness README must reference `{needle}`")
    return findings


def check_claude_skills(intents: dict[str, dict[str, Any]]) -> list[Finding]:
    """Validate Claude entry-hint structure, not semantic trigger quality.

    The entry-hint contract lives in `protocols/runtime-adapters.md`.
    Exact backtick-wrapped `/hi` identifies the declared delegation target;
    a prefix such as `/history` must not pass.
    """
    findings: list[Finding] = []
    skills_dir = ROOT / ".claude" / "skills"
    if not skills_dir.is_dir():
        return findings

    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_name = skill_dir.name
        skill_path = skill_dir / "SKILL.md"
        if not skill_path.exists():
            _add(findings, "ERROR", "claude-skill-missing-file", rel(skill_dir),
                     f"skill directory `{skill_name}` has no SKILL.md")
            continue

        fields = parse_agent_frontmatter(skill_path)
        declared_name = fields.get("name", "").strip()
        if declared_name != skill_name:
            _add(findings, "ERROR", "claude-skill-name", rel(skill_path),
                     f"skill frontmatter `name: {declared_name!r}` does not match directory `{skill_name}`")

        description = fields.get("description", "")
        if not description:
            _add(findings, "ERROR", "claude-skill-description-missing", rel(skill_path),
                     "skill frontmatter must declare a non-empty `description`")

        if "`/hi`" not in description:
            _add(findings, "ERROR", "claude-skill-no-delegation", rel(skill_path),
                     "skill description must mention `` `/hi` `` (backtick-wrapped, delegation pattern: skill forwards into the intent router)")

        if skill_name not in intents:
            _add(findings, "ERROR", "claude-skill-orphan", rel(skill_path),
                     f"skill `{skill_name}` has no corresponding `intents.{skill_name}` row in harness/intents.toml")

    return findings


def check_atelier_skill() -> list[Finding]:
    findings: list[Finding] = []
    path = ROOT / ".agents" / "skills" / "atelier" / "SKILL.md"
    if not path.exists():
        return [
            Finding(
                "ERROR",
                "skill-missing",
                rel(path),
                "repo-scoped Codex skill for Atelier workflows is missing",
            )
        ]

    fields = parse_agent_frontmatter(path)
    if fields.get("name") != "atelier":
        _add(findings, "ERROR", "skill-name", rel(path), "skill frontmatter must set `name: atelier`")
    description = fields.get("description", "")
    if not description or "/hi" not in description:
        _add(findings, "ERROR", "skill-description", rel(path),
                 "skill description must mention Atelier workflow triggers")

    text = _read(path)
    for needle in (
        "harness/skills.toml",
        "harness/agents.toml",
        "harness/runtimes.toml",
        "skills/",
        "protocols/runtime-adapters.md",
        "protocols/repo-conventions.md",
    ):
        if needle not in text:
            _add(findings, "ERROR", "skill-reference", rel(path), f"skill must reference `{needle}`")
    metadata_path = path.parent / "agents" / "openai.yaml"
    if not metadata_path.exists():
        _add(findings, "ERROR", "skill-metadata-missing", rel(metadata_path),
                 "Atelier skill must provide Codex UI metadata")
    else:
        metadata = _read(metadata_path)
        for needle in ("display_name:", "short_description:", "$atelier", "allow_implicit_invocation: true"):
            if needle not in metadata:
                _add(findings, "ERROR", "skill-metadata-field", rel(metadata_path),
                         f"skill metadata must contain `{needle}`")
    return findings




def check_path_registry_drift(reg: dict[str, Any]) -> list[Finding]:
    """Reject vault-path references absent from the canonical path registry.

    Scan public tracked and non-ignored untracked Markdown, TOML, and Python;
    private overlays are not part of the canonical registry contract.
    """
    findings: list[Finding] = []
    # Canonical segments: every scalar value in [paths], plus the values
    # of [paths.wiki_localized]. Map segment string → canonical name for
    # the remediation hint.
    valid_segments: dict[str, str] = {}
    for k, v in reg.items():
        if isinstance(v, str):
            valid_segments[v] = k
    for k, v in (reg.get("wiki_localized") or {}).items():
        if isinstance(v, str):
            valid_segments.setdefault(v, f"wiki_localized.{k}")

    # Allow-list: legacy migrations or examples that explicitly need a
    # bare segment. Keep empty unless a real exception emerges.
    segment_allowlist: set[str] = set()

    # Valid logical names: top-level keys in [paths] plus the dotted form
    # `wiki_localized.<lang>` for shadow wikis.
    valid_names: set[str] = set()
    for k, v in reg.items():
        if isinstance(v, str):
            valid_names.add(k)
    for k in (reg.get("wiki_localized") or {}).keys():
        valid_names.add(f"wiki_localized.{k}")

    literal_pat = re.compile(r"\$OV/([A-Za-z_][A-Za-z0-9_-]*)/?")
    # Placeholder form documented in AGENTS.md "Always-on invariants": match
    # `<paths.X>` where X is either a simple name or a `wiki_localized.<lang>`
    # dotted reference. Underscores are allowed (canonical names like
    # `daily_notes`); hyphens are not (the registry uses snake_case for
    # logical keys, hyphens only in physical segments).
    placeholder_pat = re.compile(r"<paths\.([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)?)>")
    roots = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "protocols",
        ROOT / "skills",
        ROOT / "agents",
        ROOT / "routines",
        ROOT / "harness",
        ROOT / "scripts",
        ROOT / "sources",
    ]
    # Scan `.md` (docs read by the model), `.toml` (description / comment
    # fields in skills.toml, intents.toml, etc.), AND `.py` (script
    # docstrings + comments). A stale `$OV/<seg>/` literal anywhere is the
    # same drift class — silent rename-breakage when the registry moves.
    # Scope via git (tracked + untracked-but-not-ignored), matching the
    # docstring's committed-file claim: a filesystem rglob also swept
    # gitignored local-only content (scripts/oneoff/, _results_* scratch),
    # where a private `$OV/<seg>/` literal would fail the gate AND leak the
    # private segment name into the lint report.
    root_args = [str(r.relative_to(ROOT)) for r in roots]
    tracked, t_err = git_list(root_args)
    if t_err:
        findings.append(t_err)
        return findings
    untracked, u_err = git_list(root_args, others=True)
    if u_err:
        findings.append(u_err)
        return findings
    scan_files = sorted(
        ROOT / p
        for p in set(tracked) | set(untracked)
        if p.endswith((".md", ".toml", ".py"))
    )
    py_comment_re = re.compile(r"^\s*#")
    for path in scan_files:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # For .py files, strip pure-comment lines so explanatory text like
        # `# accidentally rewrite $OV/wipfoo/` does not register as drift.
        # Inline trailing comments are kept; mid-line `#` is rare and the
        # cost of one false positive there is low.
        if path.suffix == ".py":
            text = "\n".join(
                line for line in raw.splitlines() if not py_comment_re.match(line)
            )
        else:
            text = raw
        unknown_literals: dict[str, int] = {}
        for m in literal_pat.finditer(text):
            seg = m.group(1)
            if seg in valid_segments or seg in segment_allowlist:
                continue
            unknown_literals[seg] = unknown_literals.get(seg, 0) + 1
        unknown_placeholders: dict[str, int] = {}
        for m in placeholder_pat.finditer(text):
            name = m.group(1)
            if name in valid_names:
                continue
            # Allow any `wiki_localized.<lang>` since specific language
            # codes live in per-user paths.local.toml; the canonical
            # registry only declares the parent table.
            if name.startswith("wiki_localized."):
                continue
            unknown_placeholders[name] = unknown_placeholders.get(name, 0) + 1
        for seg, count in sorted(unknown_literals.items()):
            _add(findings, "WARN", "paths-registry-drift", rel(path),
                     f"`$OV/{seg}/` referenced {count}x but `{seg}` is not in "
                    f"harness/paths.toml. Templatize the literal to "
                    f"`<paths.{seg}>`, or add the segment to the registry.")
        for name, count in sorted(unknown_placeholders.items()):
            _add(findings, "WARN", "paths-placeholder-drift", rel(path),
                     f"`<paths.{name}>` referenced {count}x but `{name}` is "
                    f"not in harness/paths.toml. Add to the registry, or "
                    f"fix the placeholder.")
    return findings


def check_scripts_zk_paths() -> list[Finding]:
    """Flag hardcoded `"zk"` literals (path or string-default) in scripts/.

    Vault-rooted paths must go through `scripts/_paths.vault_root()` so
    they fail loud when $OV is unset and never silently create stray
    relative `zk/` directories. Two patterns are flagged:

      - `Path("zk/...")` literal (the original failure mode)
      - bare-string `"zk"` or `["zk"]` defaults (the failure mode that
        bit semantic.py — wrapped in `walk_markdown` it became a relative
        path resolved against the script's cwd)

    The only allowed mentions are in `_paths.py` (the helper's own
    docstring explains the antipattern) and `harness_lint.py` (this
    check's own remediation message).
    """
    findings: list[Finding] = []
    scripts_dir = ROOT / "scripts"
    if not scripts_dir.is_dir():
        return findings
    skip = {"_paths.py", "harness_lint.py"}
    # Patterns covering the four common forms of the antipattern:
    #   (1) Path("zk/...")         — Path constructor with literal
    #   (2) = "zk"                 — bare-string assignment (excludes
    #       `==` comparisons via the `=` in the lookbehind class)
    #   (3) ["zk"]                 — list/dict literal
    #   (4) / "zk"                 — operator-form path construction
    #       (e.g., `(REPO_ROOT / "zk").resolve()`); this is the form
    #       that lived for months in fission/relink/wikilink_to_md and
    #       6 oneoff/ scripts before the lint caught it.
    patterns = [
        re.compile(r'Path\("zk/'),
        re.compile(r'(?<![\w.=])= "zk"(?![\w/])'),
        re.compile(r'\["zk"\]'),
        re.compile(r'/ "zk"(?![\w/])'),
    ]
    # Include packages; private one-off migrations stay outside this gate.
    for path in sorted(scripts_dir.rglob("*.py")):
        if path.relative_to(scripts_dir).as_posix() in skip or path.is_symlink() or path.relative_to(scripts_dir).parts[0] == "oneoff":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in patterns):
                _add(findings, "ERROR", "scripts-hardcoded-zk", f"{rel(path)}:{lineno}",
                         "use vault_root() from _paths instead of a hardcoded zk literal")
    return findings


def check_intents_registry(
    intents: dict[str, dict[str, Any]],
    claude_agents: dict[str, Any],
    harness_agents: dict[str, Any],
) -> list[Finding]:
    """Resolve declared agents on both runtimes; keep the one-line prose hint."""
    findings: list[Finding] = []
    for intent_name, entry in sorted(intents.items()):
        for agent_name in entry.get("agents", []):
            if agent_name not in claude_agents:
                _add(findings, "ERROR", "intents-agent-missing-claude", "harness/intents.toml",
                     f"intent `{intent_name}` references agent `{agent_name}` without a canonical source")
            if agent_name not in harness_agents:
                _add(findings, "ERROR", "intents-agent-missing-harness", "harness/intents.toml",
                     f"intent `{intent_name}` references agent `{agent_name}` not in harness/agents.toml (Codex parity broken)")
        if "\n" in entry["description"].strip():
            _add(findings, "WARN", "intents-description-multiline", "harness/intents.toml",
                     f"intent `{intent_name}` description should be one line")
    return findings


def check_intents_overlay() -> list[Finding]:
    """The gitignored `intents.local.toml`, when present, must merge cleanly.

    Private rows that fail validation are skipped at load time; this surfaces
    them as WARN so a typo in a private procedure path does not silently
    drop a route.
    """
    overlay = ROOT / "harness" / "intents.local.toml"
    if not overlay.is_file():
        return []
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from intent_coverage import merge_overlay, load_table
    except ImportError as exc:
        return [Finding("WARN", "intents-overlay-unchecked", rel(overlay), f"cannot import router: {exc}")]
    try:
        canonical = {k: v for k, v in load_table(ROOT / "harness" / "intents.toml", "intents").items() if isinstance(v, dict)}
    except SystemExit as exc:
        return [Finding("WARN", "intents-overlay-unchecked", rel(overlay), str(exc))]
    _merged, problems = merge_overlay(canonical, overlay)
    return [Finding("WARN", "intents-overlay-row", rel(overlay), problem) for problem in problems]


def check_autoevo_band_sync() -> list[Finding]:
    """Trust-band thresholds exist once, in autoevo_run.BAND_RULES.

    protocols/autoevo.md must render the same numbers (it is the explanation),
    and no other prose surface may restate them; the nightly command and the
    Forgetter brief point at the protocol instead.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from autoevo_run import BAND_RULES
    except ImportError as exc:
        return [Finding("WARN", "autoevo-band-unchecked", "scripts/autoevo_run.py", f"cannot import BAND_RULES: {exc}")]
    findings: list[Finding] = []
    protocol_path = ROOT / "protocols" / "autoevo.md"
    try:
        protocol = _read(protocol_path)
    except FileNotFoundError:
        return [Finding("ERROR", "autoevo-band-protocol-missing", rel(protocol_path), "protocols/autoevo.md missing")]
    high, low = BAND_RULES["redundant-high"], BAND_RULES["low-signal-high"]
    expected = (
        f"{high['min_peers']}+ peers ≥ {high['min_score']}",
        f"untouched > {high['cold_days']}d",
        f"mode `{high['mode']}`",
        f"All {low['conditions']} Forgetter conditions",
        f"untouched > {low['cold_days']}d",
    )
    for needle in expected:
        if needle not in protocol:
            _add(findings, "ERROR", "autoevo-band-drift", rel(protocol_path),
                     f"§ Trust bands does not state `{needle}` as scripts/autoevo_run.py BAND_RULES defines it",)
    restating = (
        ROOT / "routines" / "_adapters" / "autoevo" / "PROCEDURE.md",
        ROOT / "agents" / "forgetter.md",
    )
    markers = (f"≥ {high['min_score']}", f">= {high['min_score']}", f"> {low['cold_days']}d", f"{low['cold_days']}d ago")
    for path in restating:
        try:
            text = _read(path)
        except FileNotFoundError:
            continue
        hits = [m for m in markers if m in text]
        if hits:
            _add(findings, "ERROR", "autoevo-band-restated", rel(path),
                     f"restates trust-band thresholds {hits}; point at protocols/autoevo.md § Trust bands instead",)
    return findings


def check_intents_procedures(
    intents: dict[str, dict[str, Any]],
) -> list[Finding]:
    """Resolve each declared procedure without allowing a repository escape."""
    findings: list[Finding] = []
    for intent_name, entry in sorted(intents.items()):
        procedure = entry["procedure"]
        procedure_path = Path(procedure)
        resolved = (ROOT / procedure_path).resolve()
        if procedure_path.is_absolute() or not resolved.is_relative_to(ROOT):
            _add(findings, "ERROR", "intents-procedure-path", "harness/intents.toml",
                 f"intent `{intent_name}` procedure escapes the repository: `{procedure}`")
        elif not resolved.is_file():
            _add(findings, "ERROR", "intents-procedure-missing", "harness/intents.toml",
                 f"intent `{intent_name}` procedure does not exist: `{procedure}`")
    return findings


def check_intents_agents_in_procedure(intents: dict[str, dict[str, Any]]) -> list[Finding]:
    """Every agent an intent row declares must be dispatchable from its procedure.

    2026-08-22: `intents.reflection` declared five parallel agents that
    `daily-reflection.md` never dispatches; `/hi` batched them anyway
    ("when parallel = true, dispatch the declared initial agents"), so each
    reflection bootstrapped 3 to 5 idle subagents. The registry row is the
    routing announcement; if the procedure does not mention the role, the row
    is advertising work that will not happen (or, worse, causing it).
    """
    findings: list[Finding] = []
    for name, row in sorted(intents.items()):
        agents = row.get("agents") or []
        procedure = row.get("procedure")
        if not agents or not isinstance(procedure, str):
            continue
        path = ROOT / procedure
        if not path.is_file():
            continue  # reported by check_intents_procedures
        text = _read(path).lower()
        for agent in agents:
            if not isinstance(agent, str):
                continue
            if not re.search(r"\b" + re.escape(agent.lower()) + r"\b", text):
                _add(findings, "ERROR", "intent-agent-not-in-procedure", f"harness/intents.toml:intents.{name}",
                         f"declares agent '{agent}' but {procedure} never mentions it; "
                        "drop it from `agents` or add the dispatch to the procedure")
    return findings


def check_intents_profile_reads(
    intents: dict[str, dict[str, Any]],
) -> list[Finding]:
    """Verify every `profile_reads` filename exists at `profile/<name>`.

    A renamed `profile/identity.md` would silently degrade routing context —
    the orchestrator's pre-read step would fail open. ERROR rather than WARN
    because silent degradation of a routing precondition is harder to debug
    than a noisy false positive — except on a fresh clone where `profile/` is
    gitignored and absent, in which case the existence check is skipped (the
    user has not yet run `/introspect` to populate it).
    """
    findings: list[Finding] = []
    profile_dir = ROOT / "profile"
    if not profile_dir.exists():
        return findings
    for intent_name, entry in sorted(intents.items()):
        for fname in entry.get("profile_reads", []):
            target = profile_dir / fname
            if not target.exists():
                _add(findings, "ERROR", "intents-profile-reads-missing", "harness/intents.toml",
                         f"intent `{intent_name}` references `profile/{fname}` which does not exist")
    return findings


MAX_DOC_INDIRECTION_DEPTH = 4
DOC_LINT_ROOTS = ("skills/", "agents/", "routines/", "protocols/", "harness/", "scripts/")
DOC_LINT_TOPS = ("AGENTS.md", "README.md")

# Instruction-level cross-document references. We only count refs that LOOK
# like an instruction to read another file (markdown link, explicit "see/per/
# follow/refer X.md", or arrow `-> X.md`). Bare backticked path mentions in
# prose (footnotes, cross-references, "this is documented alongside X.md")
# are NOT counted; they are passive mentions, not redirections. The goal is
# to catch instruction chains a reader has to follow ("read this, which says
# read that, which says read that"), not every textual mention.
DOC_REF_PATTERNS = [
    re.compile(r"\[[^\]]+\]\(([\w./-]+\.md)\)"),          # markdown link
    re.compile(r"(?:see|per|read|follow|refer to)\s+`?([\w./-]+\.md)`?", re.IGNORECASE),
    re.compile(r"(?:->|→)\s*`?([\w./-]+\.md)`?"),          # arrow pointer
]


def _doc_files() -> list[Path]:
    """Committed .md files we walk for indirection-depth checks."""
    files: list[Path] = []
    for top in DOC_LINT_TOPS:
        p = ROOT / top
        if p.exists():
            files.append(p)
    for root in DOC_LINT_ROOTS:
        base = ROOT / root
        if not base.exists():
            continue
        for p in base.rglob("*.md"):
            files.append(p)
    return files


def _build_doc_graph() -> dict[Path, set[Path]]:
    """Map each committed .md file to the set of other .md files it references."""
    files = _doc_files()
    file_set = {f.resolve() for f in files}
    graph: dict[Path, set[Path]] = {f.resolve(): set() for f in files}
    for src in files:
        try:
            text = src.read_text(encoding="utf-8")
        except OSError:
            continue
        src_dir = src.parent
        for pat in DOC_REF_PATTERNS:
            for match in pat.finditer(text):
                ref = match.group(1)
                if ref.startswith("$OV/") or ref.startswith("//"):
                    continue
                for candidate in (ROOT / ref, src_dir / ref):
                    resolved = candidate.resolve()
                    if resolved in file_set and resolved != src.resolve():
                        graph[src.resolve()].add(resolved)
                        break
    return graph


def check_doc_indirection_depth() -> list[Finding]:
    """Forbid indirection chains deeper than MAX_DOC_INDIRECTION_DEPTH hops
    or any cycle in the cross-document reference graph. A "hop" is one
    `.md` file referencing another. Three hops max: `a.md -> b.md -> c.md`
    is allowed; `a.md -> b.md -> c.md -> d.md` is not.
    """
    findings: list[Finding] = []
    graph = _build_doc_graph()

    def find_long_path_or_cycle(start: Path) -> tuple[list[Path] | None, list[Path] | None]:
        """DFS from start. Return (long_path, cycle_path)."""
        stack: list[tuple[Path, list[Path]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for nxt in graph.get(node, set()):
                if nxt in path:
                    cycle = path[path.index(nxt):] + [nxt]
                    return None, cycle
                new_path = path + [nxt]
                if len(new_path) > MAX_DOC_INDIRECTION_DEPTH:
                    return new_path, None
                stack.append((nxt, new_path))
        return None, None

    flagged: set[tuple[str, ...]] = set()
    for start in sorted(graph.keys()):
        long_path, cycle = find_long_path_or_cycle(start)
        if cycle is not None:
            key = tuple(p.relative_to(ROOT).as_posix() for p in cycle)
            if key in flagged:
                continue
            flagged.add(key)
            _add(findings, "ERROR", "doc-indirection-cycle", key[0],
                     f"cross-document reference cycle: {' -> '.join(key)}")
        elif long_path is not None:
            key = tuple(p.relative_to(ROOT).as_posix() for p in long_path)
            if key in flagged:
                continue
            flagged.add(key)
            _add(findings, "ERROR", "doc-indirection-depth", key[0],
                     f"cross-document indirection chain too deep ({len(key)} hops, max {MAX_DOC_INDIRECTION_DEPTH}): {' -> '.join(key)}")
    return findings


def check_skills_intent_coverage(skill_map: dict[str, Any], intents: dict[str, Any]) -> list[Finding]:
    """Require public skills to be routed, aliased, or direct-only."""
    findings: list[Finding] = []
    routed_sources = {entry["procedure"] for entry in intents.values()}

    for name, entry in sorted(skill_map.items()):
        if entry.get("status") == "alias":
            continue
        if entry.get("direct_only") is True:
            continue
        # hi is the routing hub, not a routable mode itself.
        if name == "hi":
            continue
        source = str(entry.get("source", ""))
        if source and source in routed_sources:
            continue
        _add(findings, "WARN", "skills-routing-undeclared", "harness/skills.toml",
                 f"skill `{name}` has no intent procedure and no `direct_only = true`")
    return findings


def check_decision_record_contract() -> list[Finding]:
    """Keep durable decisions on stable, topic-addressed paths."""
    contracts = {
        "skills/decision/SKILL.md": "<paths.gtd>/decisions/<slugified-topic>.md",
        "protocols/session-continuity.md": "<paths.gtd>/decisions/*.md",
    }
    forbidden = "<paths.reflections>/YYYY-MM-DD-decision-"
    findings: list[Finding] = []
    for rel, required in contracts.items():
        text = _read(ROOT / rel)
        if required not in text or forbidden in text:
            _add(findings, "ERROR", "decision-record-path", rel,
                     f"decision records must use stable `{required}` paths, never dated reflection filenames")
    return findings


def check_workflow_contract_owners() -> list[Finding]:
    """Keep routing and migrated recovery rules in their existing owners."""
    contracts = {
        "protocols/orchestrator.md": ("`harness/intents.toml` selects the procedure",),
        "skills/read/SKILL.md": ("Start with one **Reader**, or one **Scholar**",),
        "protocols/intent-capture.md": (
            "`/dine` Intent C", "Never infer trip association",
            "ask the user once for a default GTD filename",
            "Do not pass an empty `target_file`",
            "propose `<paths.wip>/<short-slug>.md` and confirm with the user before dispatch",
            "route it back to the user", "rather than retrying with a guess",
        ),
    }
    findings: list[Finding] = []
    for path, required in contracts.items():
        try:
            body = " ".join(_read(ROOT / path).split())
        except OSError:
            body = ""
        if any(needle not in body for needle in required):
            _add(findings, "ERROR", "workflow-contract-owner", path,
                     "canonical routing, recovery, or retest contract is missing")
    return findings


def check_skill_frontmatter(skills: dict[str, str], skill_map: dict[str, Any]) -> list[Finding]:
    """Every canonical skill carries matching name and description frontmatter.

    The generated runtime edges derive their descriptions from the registry.
    """
    findings: list[Finding] = []
    for name, path in sorted(skills.items()):
        fpath = ROOT / path
        try:
            lines = fpath.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        desc: str | None = None
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                if line.startswith("description:"):
                    desc = line[len("description:"):].strip()
        if desc is None:
            _add(findings, "WARN", "skill-frontmatter", path,
                     "missing `description:` frontmatter")
            continue
        fields = parse_agent_frontmatter(fpath)
        if fields.get("name") != name:
            _add(findings, "ERROR", "skill-frontmatter-name", path,
                     f"frontmatter name must be `{name}`")
        entry = skill_map.get(name)
        if entry is not None:
            toml_desc = entry["description"].strip()
            if toml_desc and desc.strip("\"'") != toml_desc:
                _add(findings, "WARN", "skill-frontmatter-drift", path,
                         f"frontmatter description differs from the "
                        f"harness/skills.toml entry for `{name}` — "
                        "mirror the registry prose (or update both)")
    return findings


def check_reader_scholar_sync() -> list[Finding]:
    """Scholar reuses Reader's behavior while keeping its own runtime identity."""
    reader_path = ROOT / "agents" / "reader.md"
    scholar_path = ROOT / "agents" / "scholar.md"
    try:
        reader = FRONTMATTER_RE.sub("", _read(reader_path), count=1)
        scholar = FRONTMATTER_RE.sub("", _read(scholar_path), count=1)
    except OSError as exc:
        return [Finding("ERROR", "reader-scholar-sync", "agents/scholar.md",
                        f"cannot read the shared reading contract: {exc}")]
    if (not reader.partition("## Shared reading contract")[2].strip()
            or "agents/reader.md" not in scholar
            or "own frontmatter" not in scholar.lower()
            or re.search(r"(?m)^## (Reading Lenses|(?:Reading )?Workflow|How You Work|Output Format)\b", scholar)):
        return [Finding("ERROR", "reader-scholar-sync", "agents/scholar.md",
                        "Scholar must read Reader's shared behavior, preserve its own frontmatter, "
                        "and not duplicate the Reading Lenses, Workflow, or Output Format body")]
    return []


# Tiers that have undergone directory fission (`scripts/fission.py`,
# protocols/repo-conventions.md 32-entry rule). A non-recursive glob or a
# flat shell `ls` over one of these returns nothing or a partial listing.
# 2026-08-22: `reflections/` buckets blinded the weekly cue and the TODO
# digest; a flat `ls "$OV"/wiki/*.md` in civ.md counted 1 of 90 entries.
def _bucketed_tiers() -> tuple[str, ...]:
    """Tiers declared fission-eligible in protocols/repo-conventions.md.

    Read from the "Per-tier split axes" table so a newly fissioned tier is
    guarded the moment the convention records it; only rows naming a bare
    tier (`reflections/`, not `research/<area>/labs/`) count.
    """
    path = ROOT / "protocols" / "repo-conventions.md"
    found: list[str] = []
    if path.is_file():
        for line in _read(path).splitlines():
            m = re.match(r"^\|\s*`([a-z0-9-]+)/`", line)
            if m and m.group(1) not in found:
                found.append(m.group(1))
    if not found and path.is_file():
        # The table exists but the parser matched zero rows: the guard would
        # silently narrow to the fallback trio. Surface it as a finding via a
        # sentinel the check function reports (import-time, so no Finding yet).
        global _BUCKETED_PARSE_FAILED
        _BUCKETED_PARSE_FAILED = True
    for fallback in ("reflections", "agent-findings", "wiki"):
        if fallback not in found:
            found.append(fallback)
    return tuple(found)


_BUCKETED_PARSE_FAILED = False


BUCKETED_TIERS = _bucketed_tiers()
_FLAT_TIER_LS_RE = re.compile(
    r"""ls\s+(?:-\w+\s+)*["']?\$\{?OV\}?["']?/(?:%s)/[^\s|;)]*\*""" % "|".join(BUCKETED_TIERS)
)
_FLAT_TIER_PY_RES = [
    # tier("reflections").glob(...)
    re.compile(r'tier\(\s*["\'][a-z_]+["\']\s*\)\.glob\('),  # any registry tier may fission
    # <anything>_dir.glob(...) / REFLECTIONS_DIR.glob(...) where the name
    # says which tier it points at.
    re.compile(r'\b\w*(?:reflect|finding|wiki|weekly|people|archive|daily)\w*\.glob\(', re.IGNORECASE),
]


_TIER_ALIAS_RE = re.compile(
    r'(\w+)\s*=\s*tier\(\s*["\']([a-z_]+)["\']\s*\)'
)


def check_flat_tier_globs() -> list[Finding]:
    if _BUCKETED_PARSE_FAILED:
        return [
            Finding(
                "ERROR",
                "bucketed-tier-parse",
                "protocols/repo-conventions.md",
                "the per-tier split-axes table parsed to zero rows; the flat-glob guard silently narrowed to its fallback trio",
            )
        ] + _flat_tier_glob_findings()
    return _flat_tier_glob_findings()


def _flat_tier_glob_findings() -> list[Finding]:
    """Flag non-recursive reads over bucketed tiers in scripts and docs.

    Also tracks per-file aliases (`refl = tier("reflections")` followed by
    `refl.glob(...)`), which the line-level regexes cannot see.
    """
    findings: list[Finding] = []
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        if (path.relative_to(ROOT / "scripts").as_posix() in {"harness_lint.py", "fission.py"} or path.is_symlink()
                or path.relative_to(ROOT / "scripts").parts[0] == "oneoff"):
            continue
        text = _read(path)
        bucketed = {t.replace("-", "_") for t in BUCKETED_TIERS}
        aliases = {
            m.group(1)
            for m in _TIER_ALIAS_RE.finditer(text)
            if m.group(2).replace("-", "_") in bucketed
        }
        alias_rx = (
            re.compile(r"\b(?:%s)\.glob\(" % "|".join(re.escape(a) for a in sorted(aliases)))
            if aliases
            else None
        )
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(rx.search(line) for rx in _FLAT_TIER_PY_RES) or (
                alias_rx and alias_rx.search(line)
            ):
                _add(findings, "ERROR", "flat-tier-glob", f"{path.relative_to(ROOT)}:{lineno}",
                         "non-recursive glob over a bucketed tier; use _paths.tier_files() or rglob")
    for path in _doc_files():
        if path.name == "repo-conventions.md":
            continue
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(_read(path).splitlines(), start=1):
            if _FLAT_TIER_LS_RE.search(line):
                _add(findings, "ERROR", "flat-tier-glob", f"{rel}:{lineno}",
                         "flat `ls` over a bucketed tier; use `find \"$OV/<tier>\" -name ...`")
    return findings


# Frozen from the measured implementation total and largest file plus the
# review allowance. Lower after verified cuts; raising requires user approval.
SOURCE_GROWTH_REVIEW_LINES = 50
SOURCE_LINE_CEILING = 31_779
SOURCE_FILE_LINE_CEILING = 1_654


def source_footprint() -> tuple[dict, list[Finding]]:
    """Current public text, not staged blobs, runtime tokens, or vault content.

    Tests are the offline suite; deployed diagnostics remain implementation.
    """
    groups = {kind: {"files": 0, "lines": 0, "bytes": 0}
              for kind in ("implementation/config", "tests", "prose")}
    findings: list[Finding] = []
    try:
        names = set(git_paths(ROOT, "ls-files", "--cached", "--others", "--exclude-standard"))
        if not names:
            raise ValueError("no public inventory; cannot establish a footprint")
        for name in sorted(names):
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("git returned a path outside the repository")
            if (name in {"uv.lock", "package-lock.json"} or name.startswith(("profile/", ".codex/agents/"))
                    or name.endswith(".local.toml")
                    or (name.startswith(".agents/skills/")
                        and not name.startswith(".agents/skills/atelier/"))):
                continue
            path = ROOT / relative
            # Do not read a symlink target, including through a linked directory.
            if any((ROOT.joinpath(*relative.parts[:i])).is_symlink()
                   for i in range(1, len(relative.parts) + 1)):
                continue
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue  # tracked worktree deletion
            if not stat.S_ISREG(mode):
                continue
            data = path.read_bytes()
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if b"\0" in data:
                continue  # binary asset, not owned text
            kind = ("tests" if name.startswith("tests/") or name == "scripts/harness_smoke.py" else
                    "prose" if path.suffix == ".md" else "implementation/config")
            lines = len(data.splitlines())
            if kind == "implementation/config" and lines > SOURCE_FILE_LINE_CEILING:
                _add(findings, "ERROR", "source-budget", name,
                     f"{lines} lines exceeds the {SOURCE_FILE_LINE_CEILING}-line file ceiling")
            for key, value in zip(("files", "lines", "bytes"),
                                  (1, lines, len(data))):
                groups[kind][key] += value
        total = {key: sum(group[key] for group in groups.values())
                 for key in ("files", "lines", "bytes")}
        implementation = groups["implementation/config"]["lines"]
        if implementation > SOURCE_LINE_CEILING:
            _add(findings, "ERROR", "source-budget", "implementation/config",
                 f"{implementation} lines exceeds the frozen {SOURCE_LINE_CEILING}-line ceiling; "
                 "subtract or seek an approved budget, never silently rebaseline")
        return {"total": total, "by_kind": groups, "budgets": {
            "growth_review_lines": SOURCE_GROWTH_REVIEW_LINES,
            "implementation/config_lines": SOURCE_LINE_CEILING,
            "implementation/config_file_lines": SOURCE_FILE_LINE_CEILING,
        }}, findings
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return {}, [Finding("ERROR", "source-inventory", ".", str(exc))]


# Instruction budget, split by what the text serves. Harness plumbing competes
# with knowledge work for the same reader attention, so they do not share a
# pool: plumbing is capped at its measured size and may only shrink, while the
# note-facing surface (autoevo, wiki schema, tiers, the agents and skills
# that act on $OV) carries the headroom. A file absent from PROSE_PLUMBING
# counts as note-facing, so a new knowledge doc never lands in the frozen half
# by accident.
PROSE_BUDGET_ROOTS = ("protocols", "agents", "skills", "routines/_adapters")
PROSE_PLUMBING = frozenset({
    "protocols/agent-handoff.md", "protocols/atelier.md", "protocols/hi-menu.md",
    "protocols/intent-capture.md", "protocols/intent-coverage.md",
    "protocols/intent-forget.md", "protocols/intent-general.md",
    "protocols/intent-meeting.md", "protocols/orchestrator.md",
    "protocols/components.md", "protocols/README.md",
    "protocols/remote-routines.md", "protocols/repo-conventions.md",
    "protocols/runtime-adapters.md", "protocols/session-continuity.md",
    "protocols/session-log.md",
    "agents/reviewer.md", "agents/precedent-judge.md", "agents/privacy-reviewer.md",
    "skills/hi/SKILL.md", "skills/lint/SKILL.md", "skills/push/SKILL.md",
    "skills/reflect/SKILL.md", "routines/_adapters/archived-prompt/PROCEDURE.md",
    "skills/triage/SKILL.md",
})
PROSE_PLUMBING_CEILING = 143_000   # frozen at the 2026-10-03 measurement, rounded up to the
                                   # next 1k; lower it after a cut, never raise it
PROSE_NOTES_WARN = 390_000
PROSE_NOTES_ERROR = 420_000


def check_prose_budget(roots: tuple[str, ...] | None = None) -> list[Finding]:
    """Bound the canonical workflow and role prose loaded by runtimes."""
    plumbing = notes = 0
    for root in (roots or PROSE_BUDGET_ROOTS):
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            size = path.stat().st_size
            if str(path.relative_to(ROOT)) in PROSE_PLUMBING:
                plumbing += size
            else:
                notes += size
    findings: list[Finding] = []
    if plumbing > PROSE_PLUMBING_CEILING:
        _add(findings, "ERROR", "prose-budget", "harness plumbing prose",
             f"{plumbing} bytes exceeds the frozen {PROSE_PLUMBING_CEILING}-byte plumbing ceiling; "
             "plumbing may only shrink, so subtract instead of raising it")
    if notes > PROSE_NOTES_ERROR:
        _add(findings, "ERROR", "prose-budget", "note-facing prose",
             f"{notes} bytes exceeds the {PROSE_NOTES_ERROR}-byte ceiling; subtract before adding")
    elif notes > PROSE_NOTES_WARN:
        _add(findings, "WARN", "prose-budget", "note-facing prose",
             f"{notes} bytes exceeds the {PROSE_NOTES_WARN}-byte budget; plan a pruning pass")
    return findings


# Hot-path files are re-read on every scheduled run, so their bytes bill
# recurrently. Ceilings hold the compression wins; raising one requires
# subtracting elsewhere on the same hot path.
HOT_PATH_CEILINGS = {
    "protocols/wiki-schema.md": 25600,
    "routines/_adapters/autoevo/PROCEDURE.md": 24576,
    "agents/forgetter.md": 15360,
    "agents/curator.md": 20480,
}


def check_hot_path_ceilings(ceilings: dict[str, int] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for rel, ceiling in (ceilings or HOT_PATH_CEILINGS).items():
        path = ROOT / rel
        if not path.is_file():
            _add(findings, "ERROR", "hot-path-ceiling", rel, "hot-path file missing")
            continue
        size = path.stat().st_size
        if size > ceiling:
            _add(findings, "ERROR", "hot-path-ceiling", rel,
                     f"{size} bytes exceeds the {ceiling}-byte nightly hot-path ceiling; subtract before adding")
    return findings


# A living doc states how the system works, not how it used to. The detect list
# is deliberately narrow: "legacy", "deprecated", "previously", and "no longer"
# all have live uses here (a runtime pointer at `dine.md`, the Decay row in
# `synthesizer.md`, the "previously on..." anchor in `session-continuity.md`),
# so what is left is the subset that only ever describes system biography.
# "earlier version" is absent because `protocols/epistemic-hygiene.md` uses it
# for earlier versions of a user's claim, which is not system biography. The
# blessed alternative to a scattered carve-out is a named deferred-work
# section, as `wiki-schema.md` does with "Open v2 Items".
LEGACY_FRAMING = re.compile(
    r"(used to (?:be|flag|have|live|run|sit|point|call|write|require)"
    r"|formerly (?:known|called|named|a|the)"
    r"|in v1 we"
    r"|it is now (?:a|an|the))",
    re.IGNORECASE,
)

# Docs that restate the rule quote the banned phrasings to define them.
LEGACY_FRAMING_EXEMPT = frozenset({"protocols/repo-conventions.md"})


def check_legacy_framing(roots: list[str] | None = None) -> list[Finding]:
    """Enforce the present-tense rule above over the routed prose surface."""
    findings: list[Finding] = []
    scope = roots if roots is not None else [*PROSE_BUDGET_ROOTS, "harness"]
    paths: list[Path] = []
    for root in scope:
        base = ROOT / root
        if base.is_dir():
            paths.extend(sorted(base.rglob("*.md")))
    if roots is None:
        paths.extend(ROOT / name for name in ("AGENTS.md", "README.md"))
    for path in paths:
        if not path.is_file() or rel(path) in LEGACY_FRAMING_EXEMPT:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            match = LEGACY_FRAMING.search(line)
            if match:
                _add(findings, "WARN", "legacy-framing", f"{rel(path)}:{lineno}",
                     f"\"{match.group(0)}\" narrates past system state; "
                     "state the current rule in present tense")
    return findings


def run_lints() -> list[Finding]:
    config, findings = load_harness_config()
    if findings:
        return sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 99), f.code, f.where, f.message))
    model_registry = config["harness/models.toml"]["models"]
    agent_registry = config["harness/agents.toml"]["agents"]
    skill_registry = config["harness/skills.toml"]["skills"]
    intents = config["harness/intents.toml"]["intents"]
    findings.extend(check_root_files())
    agents, agent_findings = load_canonical_agents()
    findings.extend(agent_findings)
    skills, skill_findings = load_canonical_skills()
    findings.extend(skill_findings)
    model_findings, models = check_models(agents, model_registry, agent_registry)
    findings.extend(model_findings)
    findings.extend(check_runtime_registry(config["harness/runtimes.toml"]))
    findings.extend(check_agent_registry(agents, models, agent_registry))
    findings.extend(check_runtime_edges(config))
    findings.extend(check_hooks(config[".codex/hooks.json"], "codex"))
    findings.extend(check_hooks(config[".claude/settings.json"], "claude"))
    findings.extend(check_agent_hooks())
    findings.extend(check_skills(skills, skill_registry))
    findings.extend(check_skill_frontmatter(skills, skill_registry))
    findings.extend(check_component_taxonomy(
        skills, agents, config["routines/registry.toml"], config["harness/paths.toml"]["paths"]
    ))
    findings.extend(check_reader_scholar_sync())
    findings.extend(check_harness_readme())
    findings.extend(check_atelier_skill())
    findings.extend(check_scripts_zk_paths())
    findings.extend(check_path_registry_drift(config["harness/paths.toml"]["paths"]))
    findings.extend(check_skills_intent_coverage(skill_registry, intents))
    findings.extend(check_decision_record_contract())
    findings.extend(check_workflow_contract_owners())
    findings.extend(check_intents_registry(intents, agents, agent_registry))
    findings.extend(check_intents_overlay())
    findings.extend(check_autoevo_band_sync())
    findings.extend(check_intents_procedures(intents))
    findings.extend(check_intents_agents_in_procedure(intents))
    findings.extend(check_intents_profile_reads(intents))
    findings.extend(check_claude_skills(intents))
    findings.extend(check_doc_indirection_depth())
    findings.extend(check_flat_tier_globs())
    findings.extend(source_footprint()[1])
    findings.extend(check_prose_budget())
    findings.extend(check_hot_path_ceilings())
    findings.extend(check_legacy_framing())
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 99), f.code, f.where, f.message))
    return findings


def format_table(findings: list[Finding]) -> str:
    return _findings.format_table(
        findings,
        empty="harness_lint: clean (no findings)\n",
        label="harness lint report",
        separator=": ",
    )


def format_json(findings: list[Finding]) -> str:
    return _findings.format_json(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/harness_lint.py",
        description="Check Claude Code and Codex harness portability.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument("--footprint", action="store_true",
                        help="Report public owned-text size and its budget only; no private profile/vault lint.")
    args = parser.parse_args(argv)

    if args.footprint:
        footprint, findings = source_footprint()
        print(json.dumps({**footprint,
                          "excludes": ["ignored/private files", "generated adapters", "uv.lock", "package-lock.json",
                                       "symlinks", "binary assets"],
                          "findings": [finding.to_dict() for finding in findings]}, indent=2))
        return int(any(finding.severity == "ERROR" for finding in findings))

    findings = run_lints()
    if args.json:
        sys.stdout.write(format_json(findings))
    else:
        sys.stdout.write(format_table(findings))
    return 1 if any(f.severity == "ERROR" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
