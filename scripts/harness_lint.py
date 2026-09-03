#!/usr/bin/env python3
"""
harness_lint.py: portability checks for the Claude Code and Codex harness.

This script checks the repo-level contracts that let Atelier run under both
Claude Code and Codex:

  1. Codex has a root AGENTS.md.
  2. Shared runtime rules point to CLAUDE.md and runtime-adapters.md.
  3. Agent model frontmatter is represented in harness/models.toml.
  4. Capability names referenced by shared protocols are mapped to Codex tools
     in harness/capabilities.toml.
  5. Tracked command specs are represented in harness/commands.toml.
  6. Tracked agent specs are represented in harness/agents.toml.
  7. The harness reference doc exists.
  8. Codex has repo-scoped command skills, native agent adapters, and hooks.
 9. Intent/agent registry coherence: every `intents.<name>.agents[*]`
     resolves to an agent in `harness/agents.toml`; pattern values in
     both registries are drawn from the allowed set; every intent carries
     the one-line `description` the model classifier routes on, and
     `examples` (when present) is a list of strings; `agents.<name>.used_by`
     is consistent with the intents/commands walk; orphans flagged.
 10. Every intent declares an existing procedure and a bounded context budget;
     every `intents.<name>.profile_reads` filename exists at `profile/<name>`.
 11. The native runtime selector has a Codex shipped default, a supported
     Claude choice, and a gitignored local override.

Exit code: 0 if no ERROR-level findings, 1 if any ERROR-level finding.
argparse returns 2 on CLI usage errors.

Fixers (mutating, off by default):
  --fix-used-by  Regenerate `used_by` lists in `harness/agents.toml` from the
                 intents+commands walk. Idempotent. Use after editing
                 `harness/intents.toml` or after a command adds/drops an
                 agent dispatch. Default lint runs are read-only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
from render_runtime_edges import TIER_TO_EFFORT  # noqa: E402  (single owner of the tier map)
from _git import git_paths  # noqa: E402

SEVERITY_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}
MAX_CONTEXT_BUDGET_BYTES = 64 * 1024

# Allowed coordination-pattern values for both `agents.<name>.pattern` (in
# harness/agents.toml) and `intents.<name>.pattern` (in harness/intents.toml).
# Definitions live in protocols/orchestrator.md → "Coordination Patterns".
ALLOWED_PATTERNS = frozenset({
    "orchestrator-subagent",
    "generator-verifier",
    "agent-team",
    "shared-state",
    "solo",
})

# Patterns matching agent-name mentions in command source files. Anchored on
# word boundaries; case-insensitive at usage time. Built dynamically from the
# loaded agent registry so new agents are picked up automatically. Multi-word
# stems are listed before single-word stems so they match greedily.
def _build_agent_name_re(agent_names: list[str]) -> re.Pattern[str]:
    by_len = sorted(set(agent_names), key=lambda s: -len(s))
    parts = [n.replace("-", "[- ]") for n in by_len]
    return re.compile(r"\b(" + "|".join(parts) + r")\b", re.IGNORECASE)

# A line counts as a dispatch context only if it mentions one of these tokens.
# Filters incidental prose mentions (e.g., `readwise reader-list-documents`
# CLI commands, "Reader persona" prose) from the used_by walk; only lines
# that look like real dispatch sites contribute. Per-line scope keeps the
# heuristic local — a CLI fragment one line above a real dispatch will not
# accidentally tag the agent.
DISPATCH_CONTEXT_RE = re.compile(
    r"\b(dispatch(?:es|ed|ing)?|subagent_type|agent|cercle)\b|\*\*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    where: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "where": self.where,
            "message": self.message,
        }


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


def load_claude_agents() -> tuple[dict[str, dict[str, str]], list[Finding]]:
    findings: list[Finding] = []
    agents: dict[str, dict[str, str]] = {}
    agent_dir = ROOT / ".claude" / "agents"
    if not agent_dir.exists():
        return agents, [
            Finding("ERROR", "missing-agent-dir", ".claude/agents", "Claude agent directory is missing")
        ]

    for path in sorted(agent_dir.glob("*.md")):
        fields = parse_agent_frontmatter(path)
        name = fields.get("name")
        if not name:
            findings.append(
                Finding("ERROR", "agent-frontmatter", rel(path), "missing `name` in frontmatter")
            )
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


def load_claude_commands() -> tuple[dict[str, str], list[Finding]]:
    tracked, err = git_list([".claude/commands"])
    if err:
        return {}, [err]
    untracked, err = git_list([".claude/commands"], others=True)
    if err:
        return {}, [err]

    command_paths = sorted(
        p for p in set(tracked) | set(untracked)
        if p.endswith(".md")
        and p.startswith(".claude/commands/")
        and (ROOT / p).is_file()
    )
    commands: dict[str, str] = {}
    findings: list[Finding] = []
    for path in command_paths:
        name = Path(path).stem
        if name in commands:
            findings.append(
                Finding(
                    "ERROR",
                    "command-duplicate",
                    path,
                    f"duplicate command stem `{name}` also appears at `{commands[name]}`",
                )
            )
            continue
        commands[name] = path
    return commands, findings


def check_root_files() -> list[Finding]:
    findings: list[Finding] = []

    agents_path = ROOT / "AGENTS.md"
    claude_path = ROOT / "CLAUDE.md"
    runtime_path = ROOT / "protocols" / "runtime-adapters.md"

    if not agents_path.exists():
        findings.append(
            Finding("ERROR", "missing-agents-md", "AGENTS.md", "Codex root instructions are missing")
        )
    else:
        text = _read(agents_path)
        if "CLAUDE.md" not in text:
            findings.append(
                Finding("ERROR", "agents-contract", "AGENTS.md", "AGENTS.md must point Codex to CLAUDE.md")
            )
        if "protocols/runtime-adapters.md" not in text:
            findings.append(
                Finding(
                    "ERROR",
                    "agents-contract",
                    "AGENTS.md",
                    "AGENTS.md must point Codex to protocols/runtime-adapters.md",
                )
            )

    if not claude_path.exists():
        findings.append(
            Finding("ERROR", "missing-claude-md", "CLAUDE.md", "Claude Code root instructions are missing")
        )
    else:
        size = claude_path.stat().st_size
        if size > 15_000:
            findings.append(
                Finding(
                    "ERROR",
                    "claude-size",
                    "CLAUDE.md",
                    f"CLAUDE.md is {size} bytes; hard ceiling is 15000 bytes",
                )
            )
        elif size > 8_192:
            findings.append(
                Finding(
                    "WARN",
                    "claude-size",
                    "CLAUDE.md",
                    f"CLAUDE.md is {size} bytes; target is under 8192 bytes",
                )
            )
        bold_count = _read(claude_path).count("**")
        if bold_count:
            findings.append(
                Finding(
                    "INFO",
                    "claude-bold",
                    "CLAUDE.md",
                    f"CLAUDE.md contains {bold_count} bold markers",
                )
            )

    if not runtime_path.exists():
        findings.append(
            Finding(
                "ERROR",
                "missing-runtime-adapters",
                rel(runtime_path),
                "runtime adapter protocol is missing",
            )
        )

    return findings


def check_models(agents: dict[str, dict[str, str]]) -> tuple[list[Finding], dict[str, Any]]:
    """Validate harness/models.toml schema + cross-check bindings if present.

    Schema model: `harness/models.toml` (committed) declares model identities
    as `[models.X]` entries (opus, sonnet, deepseek_pro_max, ...). Each entry
    carries a runtime-neutral reasoning tier. Binding values (claude_code,
    codex, direct_api,
    direct_api_base, api_env, direct_api_extras, direct_api_timeout,
    codex_reasoning_effort) live in `profile/models.toml` (gitignored).

    Agent voices membership lives in `harness/agents.toml` as a keyed inline
    table `voices = {native = "X", direct = "Y"}` (or single-leg variants)
    and is validated in `check_agent_registry`. Returns the schema models
    dict so callers can cross-check voices references without re-reading.
    """
    findings: list[Finding] = []
    data, err = _load_toml(ROOT / "harness" / "models.toml")
    if err:
        return [err], {}
    assert data is not None

    models = data.get("models", {})
    if not isinstance(models, dict) or not models:
        findings.append(
            Finding("ERROR", "models-empty", "harness/models.toml", "no model identities declared")
        )
        return findings, {}

    # Forbid the legacy `[profiles.*]` / `[agents.*]` blocks: those belonged
    # to the pre-refactor schema and a re-introduction would silently fork
    # the registry. Hard error, not warn.
    if "profiles" in data:
        findings.append(
            Finding(
                "ERROR",
                "models-legacy-profiles",
                "harness/models.toml",
                "legacy `[profiles.*]` block present; the schema now uses `[models.*]` only",
            )
        )
    if "agents" in data:
        findings.append(
            Finding(
                "ERROR",
                "models-legacy-agent-map",
                "harness/models.toml",
                "legacy `[agents.*]` block present; agent->voices bindings now live in harness/agents.toml",
            )
        )

    binding_keys = {
        "claude_code", "codex", "codex_reasoning_effort",
        "direct_api", "direct_api_base", "direct_api_extras",
        "direct_api_timeout", "api_env",
    }

    for model_name, entry in sorted(models.items()):
        if not isinstance(entry, dict):
            findings.append(
                Finding("ERROR", "models-model-shape", "harness/models.toml", f"model `{model_name}` is not a table")
            )
            continue
        # Binding-shaped keys must NOT appear in the committed schema; they
        # belong in profile/models.toml. Catches accidental leakage of
        # provider names back into the public file.
        leaked = sorted(k for k in entry if k in binding_keys)
        for k in leaked:
            findings.append(
                Finding(
                    "ERROR",
                    "models-binding-in-schema",
                    "harness/models.toml",
                    f"model `{model_name}` has binding key `{k}` (move to profile/models.toml)",
                )
            )
        reasoning_tier = entry.get("reasoning_tier")
        if reasoning_tier not in TIER_TO_EFFORT:
            findings.append(
                Finding(
                    "ERROR",
                    "models-reasoning-tier",
                    "harness/models.toml",
                    f"model `{model_name}` reasoning_tier must be one of the tiers in render_runtime_edges.TIER_TO_EFFORT",
                )
            )

    findings.extend(_check_model_bindings(agents, models))
    return findings, models


def _check_model_bindings(
    agents: dict[str, dict[str, str]],
    models: dict[str, Any],
) -> list[Finding]:
    """Cross-check profile/models.toml bindings against the schema, and
    cross-check `.claude/agents/<name>.md` frontmatter `model:` against the
    role's native voice leg in `harness/agents.toml`.

    Soft check on bindings: if the bindings file is missing, return silently.
    Absence is the expected state on fresh clones (the file is gitignored,
    machine-local). When present, verify every schema model has a binding.
    """
    findings: list[Finding] = []

    # Cross-check Claude frontmatter `model:` against the native voice leg
    # for every role declared in harness/agents.toml. This validates the
    # Claude runtime edge only. Codex model and reasoning configuration is
    # validated separately against each project agent adapter.
    harness_path = ROOT / "harness" / "agents.toml"
    if harness_path.exists():
        h_data, _ = _load_toml(harness_path)
        if h_data is not None:
            registry = h_data.get("agents", {}) or {}
            for agent_name, frontmatter in agents.items():
                entry = registry.get(agent_name)
                if not isinstance(entry, dict):
                    continue
                voices = entry.get("voices")
                if not isinstance(voices, dict):
                    continue
                native_id = voices.get("native")
                fm_model = frontmatter.get("model")
                if native_id and fm_model and native_id != fm_model:
                    bindings_path = ROOT / "profile" / "models.toml"
                    expected_native = native_id
                    if bindings_path.exists():
                        b_data, _ = _load_toml(bindings_path)
                        if b_data is not None:
                            binding = (b_data.get("models", {}) or {}).get(native_id, {}) or {}
                            cc = binding.get("claude_code")
                            if isinstance(cc, str):
                                expected_native = cc
                    if fm_model != expected_native:
                        findings.append(
                            Finding(
                                "WARN",
                                "models-claude-drift",
                                frontmatter.get("path", f".claude/agents/{agent_name}.md"),
                                f"frontmatter model `{fm_model}` differs from native voice `{native_id}` (expected `{expected_native}` per profile/models.toml)",
                            )
                        )

    bindings_path = ROOT / "profile" / "models.toml"
    if not bindings_path.exists():
        return findings
    data, err = _load_toml(bindings_path)
    if err:
        return [err]
    assert data is not None
    binding_models = data.get("models", {}) or {}

    for model_name in sorted(models):
        model_schema = models.get(model_name)
        if isinstance(model_schema, dict) and model_schema.get("binding_optional") is True:
            continue
        if model_name not in binding_models:
            findings.append(
                Finding(
                    "WARN",
                    "models-binding-missing",
                    "profile/models.toml",
                    f"schema model `{model_name}` has no binding entry",
                )
            )
    return findings


def check_capabilities() -> list[Finding]:
    findings: list[Finding] = []
    data, err = _load_toml(ROOT / "harness" / "capabilities.toml")
    if err:
        return [err]
    assert data is not None

    capabilities = data.get("capabilities", {})
    if not isinstance(capabilities, dict) or not capabilities:
        return [
            Finding("ERROR", "capabilities-missing", "harness/capabilities.toml", "no capabilities declared")
        ]

    for cap_name, cap in sorted(capabilities.items()):
        if not isinstance(cap, dict):
            findings.append(
                Finding("ERROR", "capability-shape", "harness/capabilities.toml", f"capability `{cap_name}` is not a table")
            )
            continue
        for key in ("description", "codex"):
            if not cap.get(key):
                findings.append(
                    Finding(
                        "ERROR",
                        "capability-field",
                        "harness/capabilities.toml",
                        f"capability `{cap_name}` is missing `{key}`",
                    )
                )

    return findings


def check_runtime_registry() -> list[Finding]:
    """Validate the native runtime registry and local-selection contract."""
    findings: list[Finding] = []
    path = ROOT / "harness" / "runtimes.toml"
    data, err = _load_toml(path)
    if err:
        return [err]
    assert data is not None

    runtime = data.get("runtime")
    runtimes = data.get("runtimes")
    if not isinstance(runtime, dict) or not isinstance(runtimes, dict):
        return [
            Finding(
                "ERROR",
                "runtime-registry-shape",
                rel(path),
                "registry must define [runtime] and [runtimes.<name>] tables",
            )
        ]

    if runtime.get("default") != "codex":
        findings.append(
            Finding(
                "ERROR",
                "runtime-default",
                rel(path),
                "the shipped runtime default must be `codex`; use the local override for Claude",
            )
        )
    if runtime.get("local_override") != "harness/runtime.local.toml":
        findings.append(
            Finding(
                "ERROR",
                "runtime-local-override",
                rel(path),
                "runtime.local_override must be `harness/runtime.local.toml`",
            )
        )
    if runtime.get("environment_override") != "ATELIER_RUNTIME":
        findings.append(
            Finding(
                "ERROR",
                "runtime-environment-override",
                rel(path),
                "runtime.environment_override must be `ATELIER_RUNTIME`",
            )
        )

    expected_prefixes = {"codex": "$", "claude": "/"}
    # The two shipped runtimes are a required SUBSET; additional runtimes
    # (cursor, grok, ...) may be declared and are validated by the same
    # per-runtime field checks below. Prefixes must stay unique so command
    # routing is unambiguous.
    missing_required = set(expected_prefixes) - set(runtimes)
    if missing_required:
        findings.append(
            Finding(
                "ERROR",
                "runtime-set",
                rel(path),
                f"runtime registry must contain at least {sorted(expected_prefixes)}; missing {sorted(missing_required)}",
            )
        )
    declared_prefixes = [
        str(entry.get("command_prefix", ""))
        for entry in runtimes.values()
        if isinstance(entry, dict)
    ]
    if len(declared_prefixes) != len(set(declared_prefixes)):
        findings.append(
            Finding(
                "ERROR",
                "runtime-prefix-collision",
                rel(path),
                "every declared runtime needs a unique command_prefix",
            )
        )
    required_fields = (
        "label",
        "executable",
        "command_prefix",
        "native_shadow_identity",
        "shell_args",
        "interactive_args",
        "non_interactive_args",
    )
    for name, expected_prefix in (
        (n, expected_prefixes.get(n)) for n in runtimes if n in expected_prefixes
    ):
        entry = runtimes.get(name)
        if not isinstance(entry, dict):
            continue
        for field in required_fields:
            if field not in entry:
                findings.append(
                    Finding(
                        "ERROR",
                        "runtime-field",
                        rel(path),
                        f"runtimes.{name} is missing `{field}`",
                    )
                )
        if entry.get("command_prefix") != expected_prefix:
            findings.append(
                Finding(
                    "ERROR",
                    "runtime-command-prefix",
                    rel(path),
                    f"runtimes.{name}.command_prefix must be `{expected_prefix}`",
                )
            )
        shadow_identity = entry.get("native_shadow_identity")
        if name == "claude" and shadow_identity != "role":
            findings.append(
                Finding(
                    "ERROR",
                    "runtime-shadow-identity",
                    rel(path),
                    "Claude native_shadow_identity must be `role`",
                )
            )
        if name == "codex" and isinstance(shadow_identity, str):
            models_data, models_err = _load_toml(ROOT / "harness" / "models.toml")
            if models_err:
                findings.append(models_err)
            else:
                assert models_data is not None
                models = models_data.get("models", {})
                if not isinstance(models, dict) or shadow_identity not in models:
                    findings.append(
                        Finding(
                            "ERROR",
                            "runtime-shadow-identity",
                            rel(path),
                            f"Codex native_shadow_identity `{shadow_identity}` is not declared in harness/models.toml",
                        )
                    )
        for field, value in entry.items():
            if field.endswith("_args") and (
                not isinstance(value, list)
                or not all(isinstance(item, str) for item in value)
            ):
                findings.append(
                    Finding(
                        "ERROR",
                        "runtime-args-shape",
                        rel(path),
                        f"runtimes.{name}.{field} must be a list of strings",
                    )
                )

    supporting_paths = (
        ROOT / "harness" / "runtime.local.toml.example",
        ROOT / "harness" / "session-replay.toml.example",
        ROOT / "scripts" / "atelier_runtime.py",
    )
    for supporting_path in supporting_paths:
        if not supporting_path.exists():
            findings.append(
                Finding(
                    "ERROR",
                    "runtime-support-file",
                    rel(supporting_path),
                    "runtime selector support file is missing",
                )
            )

    gitignore = _read(ROOT / ".gitignore")
    if "harness/runtime.local.toml" not in gitignore:
        findings.append(
            Finding(
                "ERROR",
                "runtime-local-ignore",
                ".gitignore",
                "the per-user runtime preference must remain gitignored",
            )
        )
    local_path = ROOT / "harness" / "runtime.local.toml"
    if local_path.exists():
        local_data, local_err = _load_toml(local_path)
        if local_err:
            findings.append(local_err)
        else:
            assert local_data is not None
            local_runtime = local_data.get("runtime")
            local_default = local_runtime.get("default") if isinstance(local_runtime, dict) else None
            if local_default not in expected_prefixes:
                findings.append(
                    Finding(
                        "ERROR",
                        "runtime-local-default",
                        rel(local_path),
                        f"local runtime default must be one of {sorted(expected_prefixes)}",
                    )
                )

    replay_config_path = ROOT / "harness" / "session-replay.toml.example"
    replay_data, replay_err = _load_toml(replay_config_path)
    if replay_err:
        findings.append(replay_err)
    else:
        assert replay_data is not None
        unknown_tables = sorted(set(replay_data) - {"session_replay"})
        replay_table = replay_data.get("session_replay")
        unknown_fields = (
            sorted(set(replay_table) - {"enabled"})
            if isinstance(replay_table, dict)
            else []
        )
        if (
            unknown_tables
            or not isinstance(replay_table, dict)
            or not isinstance(replay_table.get("enabled"), bool)
            or unknown_fields
        ):
            findings.append(
                Finding(
                    "ERROR",
                    "session-replay-config-shape",
                    rel(replay_config_path),
                    "replay preference must contain only [session_replay] with boolean `enabled`",
                )
            )
    return findings


def check_agent_registry(
    agents: dict[str, dict[str, str]],
    models: dict[str, Any],
) -> list[Finding]:
    """Validate harness/agents.toml registry shape + voices bindings.

    Each agent declares a `voices` keyed inline table mapping leg name
    (`native`/`direct`/`codex`) to model identity; every model identity
    referenced must exist in `harness/models.toml`. Source paths must match the discovered
    `.claude/agents/*.md` file (one exception: script-driven agents like
    `external-reviewer` declare a non-`.claude/agents/` source and have no
    Claude-side spec). Codex prompt should reference the source path so native
    adapters and sequential fallbacks can route discovery.
    """
    findings: list[Finding] = []
    data, err = _load_toml(ROOT / "harness" / "agents.toml")
    if err:
        return [err]
    assert data is not None

    registry = data.get("agents", {})
    if not isinstance(registry, dict) or not registry:
        return [
            Finding("ERROR", "agents-registry-missing", "harness/agents.toml", "no agents declared")
        ]

    required_fields = (
        "source",
        "voices",
        "status",
        "description",
        "codex_prompt",
    )

    for name, fields in sorted(agents.items()):
        entry = registry.get(name)
        if not isinstance(entry, dict):
            findings.append(
                Finding(
                    "ERROR",
                    "agents-registry-entry-missing",
                    "harness/agents.toml",
                    f"agent `{name}` from `{fields['path']}` has no registry entry",
                )
            )
            continue
        for field in required_fields:
            if not entry.get(field):
                findings.append(
                    Finding(
                        "ERROR",
                        "agents-registry-field",
                        "harness/agents.toml",
                        f"agent `{name}` is missing `{field}`",
                    )
                )
        source = entry.get("source")
        if source != fields["path"]:
            findings.append(
                Finding(
                    "ERROR",
                    "agents-registry-source-drift",
                    "harness/agents.toml",
                    f"agent `{name}` source `{source}` differs from discovered path `{fields['path']}`",
                )
            )
        voices = entry.get("voices")
        if voices is not None:
            if not isinstance(voices, dict) or not voices:
                findings.append(
                    Finding(
                        "ERROR",
                        "agents-voices-shape",
                        "harness/agents.toml",
                        f"agent `{name}` voices must be a non-empty inline table mapping leg name to model identity",
                    )
                )
            else:
                allowed_legs = {"native", "direct", "codex"}
                for leg_name, model_ref in voices.items():
                    if leg_name not in allowed_legs:
                        findings.append(
                            Finding(
                                "ERROR",
                                "agents-voices-leg-name",
                                "harness/agents.toml",
                                f"agent `{name}` voices leg `{leg_name}` not in {sorted(allowed_legs)}",
                            )
                        )
                        continue
                    if not isinstance(model_ref, str):
                        findings.append(
                            Finding(
                                "ERROR",
                                "agents-voices-leg-shape",
                                "harness/agents.toml",
                                f"agent `{name}` voices leg `{leg_name}` value `{model_ref!r}` is not a string",
                            )
                        )
                        continue
                    if model_ref not in models:
                        findings.append(
                            Finding(
                                "ERROR",
                                "agents-voices-unknown-model",
                                "harness/agents.toml",
                                f"agent `{name}` voices leg `{leg_name}` references unknown model `{model_ref}` (not in harness/models.toml)",
                            )
                        )
        kinds = entry.get("kinds")
        if kinds is None:
            findings.append(
                Finding(
                    "ERROR",
                    "agents-kinds-missing",
                    "harness/agents.toml",
                    f"agent `{name}` is missing `kinds` field (must be a non-empty list of 'system' or 'app')",
                )
            )
        elif not isinstance(kinds, list) or not kinds:
            findings.append(
                Finding(
                    "ERROR",
                    "agents-kinds-shape",
                    "harness/agents.toml",
                    f"agent `{name}` kinds must be a non-empty list",
                )
            )
        else:
            for kind in kinds:
                if kind not in ("system", "app"):
                    findings.append(
                        Finding(
                            "ERROR",
                            "agents-kinds-unknown",
                            "harness/agents.toml",
                            f"agent `{name}` kinds value `{kind!r}` not in {{'system', 'app'}}",
                        )
                    )
        rationale = entry.get("dispatch_rationale")
        allowed_rationales = {"context-isolation", "model-tier", "parallelization", "tool-isolation"}
        if rationale is None:
            findings.append(
                Finding(
                    "ERROR",
                    "agents-dispatch-rationale-missing",
                    "harness/agents.toml",
                    f"agent `{name}` is missing `dispatch_rationale` field; declare why a subagent is worth the overhead vs. inline orchestration",
                )
            )
        elif not isinstance(rationale, list) or not rationale:
            findings.append(
                Finding(
                    "ERROR",
                    "agents-dispatch-rationale-shape",
                    "harness/agents.toml",
                    f"agent `{name}` dispatch_rationale must be a non-empty list",
                )
            )
        else:
            for value in rationale:
                if value not in allowed_rationales:
                    findings.append(
                        Finding(
                            "ERROR",
                            "agents-dispatch-rationale-unknown",
                            "harness/agents.toml",
                            f"agent `{name}` dispatch_rationale value `{value!r}` not in {sorted(allowed_rationales)}",
                        )
                    )
        prompt = str(entry.get("codex_prompt", ""))
        if source and str(source) not in prompt:
            findings.append(
                Finding(
                    "WARN",
                    "agents-registry-prompt-source",
                    "harness/agents.toml",
                    f"agent `{name}` Codex prompt does not mention `{source}`",
                )
            )
        if name == "forgetter":
            canonical_marker = "---forgetter-result---"
            legacy_marker = "---begin-result---"
            contract_paths = (
                ROOT / str(source),
                ROOT / "protocols" / "agent-handoff.md",
                ROOT / "protocols" / "orchestrator-actions.md",
                ROOT / "protocols" / "intent-forget.md",
                ROOT / ".claude" / "commands" / "autoevo-nightly.md",
            )
            if canonical_marker not in prompt or legacy_marker in prompt:
                findings.append(
                    Finding(
                        "ERROR",
                        "forgetter-envelope-registry",
                        "harness/agents.toml",
                        "Forgetter Codex prompt must use only `---forgetter-result---` as its opening marker",
                    )
                )
            for contract_path in contract_paths:
                try:
                    contract = contract_path.read_text(encoding="utf-8")
                except OSError as exc:
                    findings.append(
                        Finding(
                            "ERROR",
                            "forgetter-envelope-read",
                            rel(contract_path),
                            f"cannot read Forgetter contract: {exc}",
                        )
                    )
                    continue
                if canonical_marker not in contract or legacy_marker in contract:
                    findings.append(
                        Finding(
                            "ERROR",
                            "forgetter-envelope-drift",
                            rel(contract_path),
                            "Forgetter contract must use only `---forgetter-result---` as its opening marker",
                        )
                    )

    for name, entry in sorted(registry.items()):
        if not isinstance(entry, dict):
            findings.append(
                Finding(
                    "ERROR",
                    "agents-registry-entry-shape",
                    "harness/agents.toml",
                    f"agent `{name}` is not a table",
                )
            )
            continue
        source = str(entry.get("source", ""))
        status = entry.get("status", "")
        is_script_driven = status == "script-driven"
        if name not in agents and not is_script_driven:
            findings.append(
                Finding(
                    "WARN",
                    "agents-registry-entry-extra",
                    "harness/agents.toml",
                    f"registry agent `{name}` has no .claude agent source",
                )
            )
        # Script-driven agents (e.g., external-reviewer → scripts/review.sh)
        # legitimately have a non-.claude/agents/ source. Validate voices
        # voices but skip the Claude-side source-path checks for them.
        if is_script_driven:
            voices = entry.get("voices")
            if isinstance(voices, dict):
                for leg_name, model_ref in voices.items():
                    if isinstance(model_ref, str) and model_ref not in models:
                        findings.append(
                            Finding(
                                "ERROR",
                                "agents-voices-unknown-model",
                                "harness/agents.toml",
                                f"agent `{name}` voices leg `{leg_name}` references unknown model `{model_ref}`",
                            )
                        )
            if source and not (ROOT / source).exists():
                findings.append(
                    Finding(
                        "ERROR",
                        "agents-registry-source-missing",
                        "harness/agents.toml",
                        f"script-driven agent `{name}` source `{source}` does not exist",
                    )
                )
            continue
        if not source.startswith(".claude/agents/") or not source.endswith(".md"):
            findings.append(
                Finding(
                    "ERROR",
                    "agents-registry-source-shape",
                    "harness/agents.toml",
                    f"agent `{name}` source must be a `.claude/agents/*.md` path",
                )
            )
            continue
        source_path = ROOT / source
        if not source_path.exists():
            findings.append(
                Finding(
                    "ERROR",
                    "agents-registry-source-missing",
                    "harness/agents.toml",
                    f"agent `{name}` source `{source}` does not exist",
                )
            )
        if Path(source).stem != name:
            findings.append(
                Finding(
                    "WARN",
                    "agents-registry-name-drift",
                    "harness/agents.toml",
                    f"registry key `{name}` differs from source stem `{Path(source).stem}`",
                )
            )

    return findings


def check_codex_agent_adapters(models: dict[str, Any]) -> list[Finding]:
    """Validate project-scoped Codex agents against the portable registry."""
    findings: list[Finding] = []
    registry_data, err = _load_toml(ROOT / "harness" / "agents.toml")
    if err:
        return [err]
    assert registry_data is not None
    registry = registry_data.get("agents", {})
    if not isinstance(registry, dict):
        return findings

    adapter_dir = ROOT / ".codex" / "agents"
    expected = {
        name: entry
        for name, entry in registry.items()
        if isinstance(entry, dict) and entry.get("status") != "script-driven"
    }
    if not adapter_dir.is_dir():
        return [
            Finding(
                "ERROR",
                "codex-agents-dir-missing",
                rel(adapter_dir),
                "project-scoped Codex agent directory is missing",
            )
        ]

    actual = {path.stem: path for path in adapter_dir.glob("*.toml")}
    for name, entry in sorted(expected.items()):
        path = actual.get(name)
        if path is None:
            findings.append(
                Finding(
                    "ERROR",
                    "codex-agent-missing",
                    rel(adapter_dir / f"{name}.toml"),
                    f"portable agent `{name}` has no native Codex adapter",
                )
            )
            continue
        config, config_err = _load_toml(path)
        if config_err:
            findings.append(config_err)
            continue
        assert config is not None
        if config.get("name") != name:
            findings.append(
                Finding(
                    "ERROR",
                    "codex-agent-name",
                    rel(path),
                    f"adapter name `{config.get('name')}` must match registry key `{name}`",
                )
            )
        expected_description = str(entry.get("description", ""))
        if config.get("description") != expected_description:
            findings.append(
                Finding(
                    "ERROR",
                    "codex-agent-description-drift",
                    rel(path),
                    f"adapter description differs from harness/agents.toml for `{name}`",
                )
            )
        effort_by_tier = TIER_TO_EFFORT
        voices = entry.get("voices")
        native_identity = voices.get("native") if isinstance(voices, dict) else None
        model_entry = models.get(native_identity) if isinstance(native_identity, str) else None
        reasoning_tier = model_entry.get("reasoning_tier") if isinstance(model_entry, dict) else None
        expected_effort = effort_by_tier.get(reasoning_tier)
        if config.get("model_reasoning_effort") != expected_effort:
            findings.append(
                Finding(
                    "ERROR",
                    "codex-agent-effort-drift",
                    rel(path),
                    f"adapter model_reasoning_effort differs from the native voice tier for `{name}`",
                )
            )
        instructions = config.get("developer_instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            findings.append(
                Finding(
                    "ERROR",
                    "codex-agent-instructions",
                    rel(path),
                    "adapter must declare non-empty `developer_instructions`",
                )
            )
            continue
        source = str(entry.get("source", ""))
        for needle in ("AGENTS.md", "CLAUDE.md", source):
            if needle and needle not in instructions:
                findings.append(
                    Finding(
                        "ERROR",
                        "codex-agent-source-routing",
                        rel(path),
                        f"developer_instructions must reference `{needle}`",
                    )
                )
        if name == "forgetter" and (
            "---forgetter-result---" not in instructions
            or "---begin-result---" in instructions
        ):
            findings.append(
                Finding(
                    "ERROR",
                    "codex-forgetter-envelope-drift",
                    rel(path),
                    "native Forgetter adapter must use only `---forgetter-result---` as its opening marker",
                )
            )

    for name, path in sorted(actual.items()):
        if name not in expected:
            findings.append(
                Finding(
                    "ERROR",
                    "codex-agent-unregistered",
                    rel(path),
                    f"native Codex agent `{name}` is not a portable role in harness/agents.toml",
                )
            )
    return findings


def check_codex_hooks() -> list[Finding]:
    """Validate the native Codex lifecycle bridge used by Atelier."""
    path = ROOT / ".codex" / "hooks.json"
    try:
        payload = json.loads(_read(path))
    except FileNotFoundError:
        return [Finding("ERROR", "codex-hooks-missing", rel(path), "native Codex hooks are missing")]
    except json.JSONDecodeError as exc:
        return [Finding("ERROR", "codex-hooks-json", rel(path), str(exc))]

    hooks = payload.get("hooks", {}) if isinstance(payload, dict) else {}
    if not isinstance(hooks, dict):
        return [Finding("ERROR", "codex-hooks-shape", rel(path), "top-level `hooks` must be an object")]

    def commands_for(event: str) -> list[str]:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            return []
        commands: list[str] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks", [])
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if isinstance(handler, dict) and isinstance(handler.get("command"), str):
                    commands.append(handler["command"])
        return commands

    findings: list[Finding] = []
    required = (
        ("SessionStart", ("scripts/cues.py", "--hook", "--runtime codex")),
        ("UserPromptSubmit", ("scripts/session_replay.py", "hook --runtime codex")),
        ("UserPromptSubmit", ("scripts/cues.py", "--touch-lock")),
        ("Stop", ("scripts/session_replay.py", "hook --runtime codex")),
        ("Stop", ("scripts/shadow.py", "gc")),
    )
    for event, needles in required:
        commands = commands_for(event)
        if not any(all(needle in command for needle in needles) for command in commands):
            findings.append(
                Finding(
                    "ERROR",
                    "codex-hook-routing",
                    rel(path),
                    f"{event} must include a command containing {list(needles)}",
                )
            )
    return findings


def check_claude_hooks() -> list[Finding]:
    """Validate the supported Claude lifecycle edge alongside Codex."""
    path = ROOT / ".claude" / "settings.json"
    try:
        payload = json.loads(_read(path))
    except FileNotFoundError:
        return [Finding("ERROR", "claude-hooks-missing", rel(path), "Claude hooks are missing")]
    except json.JSONDecodeError as exc:
        return [Finding("ERROR", "claude-hooks-json", rel(path), str(exc))]

    hooks = payload.get("hooks", {}) if isinstance(payload, dict) else {}
    if not isinstance(hooks, dict):
        return [Finding("ERROR", "claude-hooks-shape", rel(path), "top-level `hooks` must be an object")]

    def commands_for(event: str) -> list[str]:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            return []
        commands: list[str] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks", [])
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if isinstance(handler, dict) and isinstance(handler.get("command"), str):
                    commands.append(handler["command"])
        return commands

    findings: list[Finding] = []
    required = (
        ("SessionStart", ("scripts/cues.py", "--hook", "--runtime claude")),
        ("UserPromptSubmit", ("scripts/session_replay.py", "hook --runtime claude-code")),
        ("UserPromptSubmit", ("scripts/cues.py", "--touch-lock")),
        ("Stop", ("scripts/session_replay.py", "hook --runtime claude-code")),
        ("SessionEnd", ("scripts/session_replay.py", "hook --runtime claude-code")),
        ("SessionEnd", ("scripts/shadow.py", "gc")),
    )
    for event, needles in required:
        commands = commands_for(event)
        if not any(all(needle in command for needle in needles) for command in commands):
            findings.append(
                Finding(
                    "ERROR",
                    "claude-hook-routing",
                    rel(path),
                    f"{event} must include a command containing {list(needles)}",
                )
            )
    return findings


def check_commands(commands: dict[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    data, err = _load_toml(ROOT / "harness" / "commands.toml")
    if err:
        return [err]
    assert data is not None

    command_map = data.get("commands", {})
    if not isinstance(command_map, dict) or not command_map:
        return [
            Finding("ERROR", "commands-missing", "harness/commands.toml", "no commands declared")
        ]

    required_fields = ("source", "category", "status", "description", "codex_prompt")

    for name, path in sorted(commands.items()):
        entry = command_map.get(name)
        if not isinstance(entry, dict):
            findings.append(
                Finding(
                    "ERROR",
                    "commands-entry-missing",
                    "harness/commands.toml",
                    f"command `{name}` from `{path}` has no manifest entry",
                )
            )
            continue
        for field in required_fields:
            if not entry.get(field):
                findings.append(
                    Finding(
                        "ERROR",
                        "commands-field",
                        "harness/commands.toml",
                        f"command `{name}` is missing `{field}`",
                    )
                )
        source = entry.get("source")
        if source != path:
            findings.append(
                Finding(
                    "ERROR",
                    "commands-source-drift",
                    "harness/commands.toml",
                    f"command `{name}` source `{source}` differs from discovered path `{path}`",
                )
            )
        prompt = str(entry.get("codex_prompt", ""))
        if entry.get("status") == "alias":
            pass  # alias prompts intentionally reference the target's source, not their own
        elif source and str(source) not in prompt:
            findings.append(
                Finding(
                    "WARN",
                    "commands-prompt-source",
                    "harness/commands.toml",
                    f"command `{name}` Codex prompt does not mention `{source}`",
                )
            )

    for name, entry in sorted(command_map.items()):
        if not isinstance(entry, dict):
            findings.append(
                Finding(
                    "ERROR",
                    "commands-entry-shape",
                    "harness/commands.toml",
                    f"command `{name}` is not a table",
                )
            )
            continue
        source = str(entry.get("source", ""))
        if name not in commands:
            findings.append(
                Finding(
                    "WARN",
                    "commands-entry-extra",
                    "harness/commands.toml",
                    f"manifest command `{name}` has no tracked .claude command source",
                )
            )
        if not source.startswith(".claude/commands/") or not source.endswith(".md"):
            findings.append(
                Finding(
                    "ERROR",
                    "commands-source-shape",
                    "harness/commands.toml",
                    f"command `{name}` source must be a `.claude/commands/*.md` path",
                )
            )
            continue
        source_path = ROOT / source
        if not source_path.exists():
            findings.append(
                Finding(
                    "ERROR",
                    "commands-source-missing",
                    "harness/commands.toml",
                    f"command `{name}` source `{source}` does not exist",
                )
            )
        if Path(source).stem != name:
            findings.append(
                Finding(
                    "WARN",
                    "commands-name-drift",
                    "harness/commands.toml",
                    f"manifest key `{name}` differs from source stem `{Path(source).stem}`",
                )
            )

    return findings


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
    for needle in ("commands.toml", "agents.toml", "models.toml", "capabilities.toml", "runtimes.toml", ".agents/skills"):
        if needle not in text:
            findings.append(
                Finding(
                    "ERROR",
                    "harness-readme-reference",
                    rel(path),
                    f"harness README must reference `{needle}`",
                )
            )
    return findings


def check_claude_skills(intents: dict[str, dict[str, Any]]) -> list[Finding]:
    """Validate Claude Code entry-hint skills under `.claude/skills/`.

    Skills are non-authoritative entry hints: Claude Code matches the skill's
    frontmatter description against user phrasing semantically (LLM-judged,
    not substring), and on a match the skill forwards into `/hi` where the
    canonical intent router in `harness/intents.toml` decides dispatch.

    Because triggering is semantic, this lint deliberately does NOT enforce
    that the skill's description contain any specific list of phrases — that
    would be substring-thinking against an LLM-judged surface. Coherence
    between a skill's prose description and the intent it exposes is a
    human-curated property, not a mechanical one.

    Structural invariants only:
      - Each `.claude/skills/<name>/SKILL.md` parses, has frontmatter, and
        `name` matches the directory name.
      - The skill's frontmatter declares a non-empty `description`.
      - The description mentions `` `/hi` `` in backtick-wrapped form
        (proves the delegation pattern is documented at the trigger surface
        — the skill must forward into the router rather than dispatch
        directly). Backticks anchor the token so `/history`, `/hire`, etc.
        do not satisfy the check.
      - The skill name corresponds to an existing `intents.<name>` row
        (orphan skills with no router intent are a drift signal).
    """
    findings: list[Finding] = []
    skills_dir = ROOT / ".claude" / "skills"
    if not skills_dir.is_dir():
        return findings

    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_name = skill_dir.name
        skill_path = skill_dir / "SKILL.md"
        if not skill_path.exists():
            findings.append(
                Finding(
                    "ERROR",
                    "claude-skill-missing-file",
                    rel(skill_dir),
                    f"skill directory `{skill_name}` has no SKILL.md",
                )
            )
            continue

        fields = parse_agent_frontmatter(skill_path)
        declared_name = fields.get("name", "").strip()
        if declared_name != skill_name:
            findings.append(
                Finding(
                    "ERROR",
                    "claude-skill-name",
                    rel(skill_path),
                    f"skill frontmatter `name: {declared_name!r}` does not match directory `{skill_name}`",
                )
            )

        description = fields.get("description", "")
        if not description:
            findings.append(
                Finding(
                    "ERROR",
                    "claude-skill-description-missing",
                    rel(skill_path),
                    "skill frontmatter must declare a non-empty `description`",
                )
            )

        if "`/hi`" not in description:
            findings.append(
                Finding(
                    "ERROR",
                    "claude-skill-no-delegation",
                    rel(skill_path),
                    "skill description must mention `` `/hi` `` (backtick-wrapped, delegation pattern: skill forwards into the intent router)",
                )
            )

        if skill_name not in intents:
            findings.append(
                Finding(
                    "ERROR",
                    "claude-skill-orphan",
                    rel(skill_path),
                    f"skill `{skill_name}` has no corresponding `intents.{skill_name}` row in harness/intents.toml",
                )
            )

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
        findings.append(
            Finding("ERROR", "skill-name", rel(path), "skill frontmatter must set `name: atelier`")
        )
    description = fields.get("description", "")
    if not description or "/hi" not in description:
        findings.append(
            Finding(
                "ERROR",
                "skill-description",
                rel(path),
                "skill description must mention Atelier workflow triggers",
            )
        )

    text = _read(path)
    for needle in (
        "harness/commands.toml",
        "harness/agents.toml",
        "harness/runtimes.toml",
        ".claude/commands/",
        "protocols/runtime-adapters.md",
    ):
        if needle not in text:
            findings.append(
                Finding(
                    "ERROR",
                    "skill-reference",
                    rel(path),
                    f"skill must reference `{needle}`",
                )
            )
    metadata_path = path.parent / "agents" / "openai.yaml"
    if not metadata_path.exists():
        findings.append(
            Finding(
                "ERROR",
                "skill-metadata-missing",
                rel(metadata_path),
                "Atelier skill must provide Codex UI metadata",
            )
        )
    else:
        metadata = _read(metadata_path)
        for needle in ("display_name:", "short_description:", "$atelier", "allow_implicit_invocation: true"):
            if needle not in metadata:
                findings.append(
                    Finding(
                        "ERROR",
                        "skill-metadata-field",
                        rel(metadata_path),
                        f"skill metadata must contain `{needle}`",
                    )
                )
    return findings


def check_codex_command_skills() -> list[Finding]:
    """Validate explicit Codex `$command` skills against commands.toml."""
    findings: list[Finding] = []
    data, err = _load_toml(ROOT / "harness" / "commands.toml")
    if err:
        return [err]
    assert data is not None
    commands = data.get("commands", {})
    if not isinstance(commands, dict):
        return findings

    skills_dir = ROOT / ".agents" / "skills"
    user_commands = {
        name
        for name, entry in commands.items()
        if isinstance(entry, dict) and entry.get("user_facing", True) is not False
    }
    actual_skills = {path.parent.name for path in skills_dir.glob("*/SKILL.md")}
    allowed_skills = user_commands | {"atelier"}
    for name in sorted(actual_skills - allowed_skills):
        findings.append(
            Finding(
                "ERROR",
                "codex-command-skill-unregistered",
                rel(skills_dir / name / "SKILL.md"),
                f"Codex skill `{name}` is neither a user-facing command nor the Atelier router",
            )
        )

    for name, entry in sorted(commands.items()):
        if not isinstance(entry, dict) or entry.get("user_facing", True) is False:
            continue
        source = str(entry.get("source", ""))
        skill_path = skills_dir / name / "SKILL.md"
        if not skill_path.exists():
            findings.append(
                Finding(
                    "ERROR",
                    "codex-command-skill-missing",
                    rel(skill_path),
                    f"user-facing command `{name}` must be invokable as `${name}`",
                )
            )
            continue

        fields = parse_agent_frontmatter(skill_path)
        if fields.get("name") != name:
            findings.append(
                Finding(
                    "ERROR",
                    "codex-command-skill-name",
                    rel(skill_path),
                    f"skill frontmatter name must be `{name}`",
                )
            )
        if f"${name}" not in fields.get("description", ""):
            findings.append(
                Finding(
                    "ERROR",
                    "codex-command-skill-description",
                    rel(skill_path),
                    f"skill description must advertise `${name}`",
                )
            )

        text = _read(skill_path)
        for needle in ("AGENTS.md", "CLAUDE.md", source):
            if needle and needle not in text:
                findings.append(
                    Finding(
                        "ERROR",
                        "codex-command-skill-source",
                        rel(skill_path),
                        f"skill must reference `{needle}`",
                    )
                )
        if "scripts/atelier.py" in text or "scripts/intent_coverage.py" in text:
            findings.append(
                Finding(
                    "ERROR",
                    "codex-command-skill-bridge",
                    rel(skill_path),
                    "interactive command skills must read their source directly, not call a Python bridge",
                )
            )

        metadata_path = skill_path.parent / "agents" / "openai.yaml"
        if not metadata_path.exists():
            findings.append(
                Finding(
                    "ERROR",
                    "codex-command-skill-metadata",
                    rel(metadata_path),
                    "command skill must provide Codex UI metadata",
                )
            )
            continue
        metadata = _read(metadata_path)
        for needle in (f"${name}", "allow_implicit_invocation: false"):
            if needle not in metadata:
                findings.append(
                    Finding(
                        "ERROR",
                        "codex-command-skill-metadata",
                        rel(metadata_path),
                        f"command skill metadata must contain `{needle}`",
                    )
                )
    return findings


def check_path_registry_drift() -> list[Finding]:
    """Flag `$OV/<segment>/` literals in committed `.md` whose `<segment>`
    is not registered in `harness/paths.toml`.

    Why this exists: hardcoded `$OV/<x>/` literals scattered across docs
    used to be the single biggest rename antipattern in the harness. A
    tier rename (e.g., `drafts` → `wip`) touched a dozen files and risked
    leaving stale paths in agent prompts. The path registry collapses
    valid tier segments to one source of truth; this check enforces that
    every `$OV/<segment>/` literal in a committed `.md` resolves to a
    registry entry, so drift is caught at lint time and a rename becomes
    a `paths.toml` edit + `scripts/rewrite_paths.py` invocation.

    Scope: walks committed `.md` files under `CLAUDE.md`, `protocols/`,
    `.claude/`, `harness/` (TOML is tracked separately by other checks).
    `paths.local.toml` is gitignored and not consulted here — its entries
    are per-user and not part of the universal contract.
    """
    findings: list[Finding] = []
    paths_toml = ROOT / "harness" / "paths.toml"
    if not paths_toml.is_file():
        return findings  # registry absent; bootstrap state, skip the check
    try:
        with paths_toml.open("rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        findings.append(
            Finding(
                "ERROR",
                "paths-registry-parse",
                rel(paths_toml),
                f"could not parse harness/paths.toml: {exc}",
            )
        )
        return findings

    reg = (data.get("paths") or {})
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
    # Placeholder form documented in CLAUDE.md "Path placeholders": match
    # `<paths.X>` where X is either a simple name or a `wiki_localized.<lang>`
    # dotted reference. Underscores are allowed (canonical names like
    # `daily_notes`); hyphens are not (the registry uses snake_case for
    # logical keys, hyphens only in physical segments).
    placeholder_pat = re.compile(r"<paths\.([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)?)>")
    roots = [
        ROOT / "CLAUDE.md",
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "protocols",
        ROOT / ".claude",
        ROOT / "harness",
        ROOT / "scripts",
        ROOT / "sources",
    ]
    # Scan `.md` (docs read by the model), `.toml` (description / comment
    # fields in commands.toml, intents.toml, etc.), AND `.py` (script
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
            findings.append(
                Finding(
                    "WARN",
                    "paths-registry-drift",
                    rel(path),
                    f"`$OV/{seg}/` referenced {count}x but `{seg}` is not in "
                    f"harness/paths.toml. Templatize via "
                    f"`scripts/rewrite_paths.py --templatize`, or add the "
                    f"segment to the registry.",
                )
            )
        for name, count in sorted(unknown_placeholders.items()):
            findings.append(
                Finding(
                    "WARN",
                    "paths-placeholder-drift",
                    rel(path),
                    f"`<paths.{name}>` referenced {count}x but `{name}` is "
                    f"not in harness/paths.toml. Add to the registry, or "
                    f"fix the placeholder.",
                )
            )
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
    # Only top-level scripts/; `scripts/oneoff/` is gitignored (one-off
    # migration scripts that hardcode private vault content) and excluded
    # from steady-state lint coverage by convention.
    for path in sorted(scripts_dir.glob("*.py")):
        if path.name in skip:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in patterns):
                findings.append(
                    Finding(
                        "ERROR",
                        "scripts-hardcoded-zk",
                        f"{rel(path)}:{lineno}",
                        "use vault_root() from _paths instead of a hardcoded zk literal",
                    )
                )
    return findings


def load_intents() -> tuple[dict[str, dict[str, Any]], list[Finding]]:
    """Load harness/intents.toml; return ({}, [finding]) on missing/invalid.

    The file is required harness state in Wave 1A onward. Missing rows are
    treated as a load failure, not a silent pass — an empty `[intents]`
    table (comment-only file or accidental wipe) means `/hi` would route
    nothing, which is never the intended state.
    """
    findings: list[Finding] = []
    path = ROOT / "harness" / "intents.toml"
    if not path.exists():
        findings.append(
            Finding(
                "ERROR",
                "intents-missing-file",
                "harness/intents.toml",
                "intent registry is missing",
            )
        )
        return {}, findings
    data, err = _load_toml(path)
    if err:
        return {}, [err]
    assert data is not None
    intents = data.get("intents", {})
    if not isinstance(intents, dict):
        return {}, [
            Finding(
                "ERROR",
                "intents-shape",
                "harness/intents.toml",
                "[intents] table missing or not a table",
            )
        ]
    if not intents:
        # File exists, parses, but has no rows. Wave 1A makes the intent
        # registry required harness state; an empty registry is a load
        # failure, not a silent pass.
        findings.append(
            Finding(
                "ERROR",
                "intents-empty-registry",
                "harness/intents.toml",
                "[intents] table is empty (no intent rows declared)",
            )
        )
        return {}, findings
    return intents, findings


def _expected_used_by(
    intents: dict[str, dict[str, Any]],
    commands: dict[str, str],
    harness_agents: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Compute the expected `used_by` list per agent.

    Walks three registries:
      1. `intents.<name>.agents[*]` (harness/intents.toml)
      2. agent-name mentions inside each `commands.<name>` source file
         (via dynamic AGENT_NAME_RE built from the registry)
      3. references to a script-driven agent's `source` path (e.g.,
         `scripts/review.sh` for external-reviewer) inside command sources

    Returns a dict keyed by agent name; values are sorted lists of strings
    of the form `"intents.<name>"` / `"commands.<name>"`. Agents with no
    references map to an empty list (orphan signal).
    """
    expected: dict[str, set[str]] = {}
    agent_names = list((harness_agents or {}).keys())
    name_re = _build_agent_name_re(agent_names) if agent_names else None

    # Map non-.md `source` paths back to the agent name (script-driven agents).
    script_sources: dict[str, str] = {}
    for agent_name, entry in (harness_agents or {}).items():
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if isinstance(source, str) and not source.endswith(".md"):
            script_sources[source] = agent_name

    for intent_name, entry in intents.items():
        if not isinstance(entry, dict):
            continue
        for agent_name in entry.get("agents", []) or []:
            if not isinstance(agent_name, str):
                continue
            expected.setdefault(agent_name, set()).add(f"intents.{intent_name}")

    for command_name, source in commands.items():
        source_path = ROOT / source
        if not source_path.exists():
            continue
        try:
            text = source_path.read_text(encoding="utf-8")
        except OSError:
            continue
        seen: set[str] = set()
        if name_re is not None:
            for line in text.splitlines():
                if not DISPATCH_CONTEXT_RE.search(line):
                    continue
                for match in name_re.finditer(line):
                    stem = match.group(1).lower().replace(" ", "-")
                    seen.add(stem)
        # Script-driven agents: search for references to their source path.
        for script_path, agent_name in script_sources.items():
            if script_path in text:
                seen.add(agent_name)
        for stem in seen:
            expected.setdefault(stem, set()).add(f"commands.{command_name}")

    return {name: sorted(refs) for name, refs in expected.items()}


def check_intents_registry(
    intents: dict[str, dict[str, Any]],
    claude_agents: dict[str, Any],
    harness_agents: dict[str, Any],
) -> list[Finding]:
    """Validate intent rows: agent references resolve, pattern values valid,
    and every row carries the `description` the classifier routes on.

    Routing is model judgment over `description`, so there is no substring
    or priority machinery to lint. Retired keys (`patterns`, `priority`)
    are flagged so a stale overlay or merge does not silently reintroduce
    them.

    Agent references must resolve against BOTH registries:
      - `claude_agents` from `load_claude_agents()` (`.claude/agents/*.md`):
        canonical for Claude Code subagent dispatch.
      - `harness_agents` from `harness/agents.toml` `[agents]` table:
        canonical for Codex parity (native `.codex/agents/` adapters expose
        this registry and route back to the .claude role brief).

    A row is fully valid only when both registries know the agent. Missing
    in either registry is an ERROR with a distinct code so operators can
    see which surface is broken.
    """
    findings: list[Finding] = []
    if not intents:
        return findings

    for intent_name, entry in sorted(intents.items()):
        if not isinstance(entry, dict):
            findings.append(
                Finding(
                    "ERROR",
                    "intents-entry-shape",
                    "harness/intents.toml",
                    f"intent `{intent_name}` is not a table",
                )
            )
            continue
        # (a) agent references valid in BOTH registries
        agents_field = entry.get("agents", []) or []
        if not isinstance(agents_field, list):
            findings.append(
                Finding(
                    "ERROR",
                    "intents-agent-shape",
                    "harness/intents.toml",
                    f"intent `{intent_name}` `agents` must be a list",
                )
            )
        else:
            for agent_name in agents_field:
                if not isinstance(agent_name, str):
                    findings.append(
                        Finding(
                            "ERROR",
                            "intents-agent-missing-claude",
                            "harness/intents.toml",
                            f"intent `{intent_name}` has non-string agent entry `{agent_name!r}`",
                        )
                    )
                    continue
                if agent_name not in claude_agents:
                    findings.append(
                        Finding(
                            "ERROR",
                            "intents-agent-missing-claude",
                            "harness/intents.toml",
                            f"intent `{intent_name}` references agent `{agent_name}` not in .claude/agents/ (Claude Code dispatch will fail)",
                        )
                    )
                if agent_name not in harness_agents:
                    findings.append(
                        Finding(
                            "ERROR",
                            "intents-agent-missing-harness",
                            "harness/intents.toml",
                            f"intent `{intent_name}` references agent `{agent_name}` not in harness/agents.toml (Codex parity broken)",
                        )
                    )
        # (c) intent pattern value valid
        pattern_value = entry.get("pattern")
        if pattern_value is None:
            findings.append(
                Finding(
                    "ERROR",
                    "intents-pattern-missing",
                    "harness/intents.toml",
                    f"intent `{intent_name}` is missing required `pattern` field",
                )
            )
        elif not isinstance(pattern_value, str):
            findings.append(
                Finding(
                    "ERROR",
                    "intents-pattern-invalid",
                    "harness/intents.toml",
                    f"intent `{intent_name}` pattern must be a string, got {type(pattern_value).__name__}",
                )
            )
        elif pattern_value not in ALLOWED_PATTERNS:
            findings.append(
                Finding(
                    "ERROR",
                    "intents-pattern-invalid",
                    "harness/intents.toml",
                    f"intent `{intent_name}` pattern=`{pattern_value}` not in {sorted(ALLOWED_PATTERNS)}",
                )
            )

    # (d) classifier contract: description required; examples optional list[str];
    # retired substring-router keys must not come back.
    for intent_name, entry in sorted(intents.items()):
        if not isinstance(entry, dict):
            continue
        description = entry.get("description")
        if not isinstance(description, str) or not description.strip():
            findings.append(
                Finding(
                    "ERROR",
                    "intents-description-missing",
                    "harness/intents.toml",
                    f"intent `{intent_name}` needs a non-empty one-line `description` (the classifier routes on it)",
                )
            )
        elif "\n" in description.strip():
            findings.append(
                Finding(
                    "WARN",
                    "intents-description-multiline",
                    "harness/intents.toml",
                    f"intent `{intent_name}` description should be one line",
                )
            )
        examples = entry.get("examples")
        if examples is not None and (
            not isinstance(examples, list) or any(not isinstance(e, str) for e in examples)
        ):
            findings.append(
                Finding(
                    "ERROR",
                    "intents-examples-shape",
                    "harness/intents.toml",
                    f"intent `{intent_name}` `examples` must be a list of strings",
                )
            )
        for retired in ("patterns", "priority"):
            if retired in entry:
                findings.append(
                    Finding(
                        "ERROR",
                        "intents-retired-key",
                        "harness/intents.toml",
                        f"intent `{intent_name}` still declares `{retired}`; the substring router is gone, use `description` / `examples`",
                    )
                )

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
            findings.append(
                Finding(
                    "ERROR",
                    "autoevo-band-drift",
                    rel(protocol_path),
                    f"§ Trust bands does not state `{needle}` as scripts/autoevo_run.py BAND_RULES defines it",
                )
            )
    restating = (
        ROOT / ".claude" / "commands" / "autoevo-nightly.md",
        ROOT / ".claude" / "agents" / "forgetter.md",
    )
    markers = (f"≥ {high['min_score']}", f">= {high['min_score']}", f"> {low['cold_days']}d", f"{low['cold_days']}d ago")
    for path in restating:
        try:
            text = _read(path)
        except FileNotFoundError:
            continue
        hits = [m for m in markers if m in text]
        if hits:
            findings.append(
                Finding(
                    "ERROR",
                    "autoevo-band-restated",
                    rel(path),
                    f"restates trust-band thresholds {hits}; point at protocols/autoevo.md § Trust bands instead",
                )
            )
    return findings


def check_intents_procedures(
    intents: dict[str, dict[str, Any]],
) -> list[Finding]:
    """Verify each intent owns a safe procedure path and context budget."""
    findings: list[Finding] = []
    if not intents:
        return findings

    for intent_name, entry in sorted(intents.items()):
        if not isinstance(entry, dict):
            continue
        mode = entry.get("mode")
        if not isinstance(mode, str) or not mode.strip():
            findings.append(
                Finding(
                    "ERROR",
                    "intents-mode-missing",
                    "harness/intents.toml",
                    f"intent `{intent_name}` has no `mode` field",
                )
            )
            continue

        procedure = entry.get("procedure")
        if not isinstance(procedure, str) or not procedure.strip():
            findings.append(
                Finding(
                    "ERROR",
                    "intents-procedure-missing",
                    "harness/intents.toml",
                    f"intent `{intent_name}` has no `procedure` path",
                )
            )
        else:
            procedure_path = Path(procedure)
            resolved = (ROOT / procedure_path).resolve()
            if procedure_path.is_absolute() or not resolved.is_relative_to(ROOT):
                findings.append(
                    Finding(
                        "ERROR",
                        "intents-procedure-path",
                        "harness/intents.toml",
                        f"intent `{intent_name}` procedure escapes the repository: `{procedure}`",
                    )
                )
            elif not resolved.is_file():
                findings.append(
                    Finding(
                        "ERROR",
                        "intents-procedure-missing",
                        "harness/intents.toml",
                        f"intent `{intent_name}` procedure does not exist: `{procedure}`",
                    )
                )

        budget = entry.get("context_budget_bytes")
        if (
            isinstance(budget, bool)
            or not isinstance(budget, int)
            or not 1 <= budget <= MAX_CONTEXT_BUDGET_BYTES
        ):
            findings.append(
                Finding(
                    "ERROR",
                    "intents-context-budget",
                    "harness/intents.toml",
                    f"intent `{intent_name}` context_budget_bytes must be an integer from 1 to {MAX_CONTEXT_BUDGET_BYTES}",
                )
            )

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
                findings.append(
                    Finding(
                        "ERROR",
                        "intent-agent-not-in-procedure",
                        f"harness/intents.toml:intents.{name}",
                        f"declares agent '{agent}' but {procedure} never mentions it; "
                        "drop it from `agents` or add the dispatch to the procedure",
                    )
                )
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
    if not intents:
        return findings
    profile_dir = ROOT / "profile"
    if not profile_dir.exists():
        return findings
    for intent_name, entry in sorted(intents.items()):
        if not isinstance(entry, dict):
            continue
        reads = entry.get("profile_reads", []) or []
        if not isinstance(reads, list):
            findings.append(
                Finding(
                    "ERROR",
                    "intents-profile-reads-shape",
                    "harness/intents.toml",
                    f"intent `{intent_name}` `profile_reads` must be a list",
                )
            )
            continue
        for fname in reads:
            if not isinstance(fname, str):
                findings.append(
                    Finding(
                        "ERROR",
                        "intents-profile-reads-shape",
                        "harness/intents.toml",
                        f"intent `{intent_name}` `profile_reads` has non-string entry `{fname!r}`",
                    )
                )
                continue
            target = profile_dir / fname
            if not target.exists():
                findings.append(
                    Finding(
                        "ERROR",
                        "intents-profile-reads-missing",
                        "harness/intents.toml",
                        f"intent `{intent_name}` references `profile/{fname}` which does not exist",
                    )
                )
    return findings


def check_agent_pattern_and_used_by(
    intents: dict[str, dict[str, Any]],
    commands: dict[str, str],
) -> list[Finding]:
    """Validate `pattern` and `used_by` on every agent in harness/agents.toml.

    Three checks: (b) pattern in allowed set, (d) used_by drift relative
    to walked expectation, (e) orphan (empty used_by) WARN.
    """
    findings: list[Finding] = []
    data, err = _load_toml(ROOT / "harness" / "agents.toml")
    if err:
        return [err]
    assert data is not None
    registry = data.get("agents", {})
    if not isinstance(registry, dict):
        return findings

    expected = _expected_used_by(intents, commands, registry)

    for name, entry in sorted(registry.items()):
        if not isinstance(entry, dict):
            continue
        # (b) pattern value valid
        pattern_value = entry.get("pattern")
        if pattern_value is None:
            findings.append(
                Finding(
                    "ERROR",
                    "agents-pattern-invalid",
                    "harness/agents.toml",
                    f"agent `{name}` is missing `pattern` field",
                )
            )
        elif pattern_value not in ALLOWED_PATTERNS:
            findings.append(
                Finding(
                    "ERROR",
                    "agents-pattern-invalid",
                    "harness/agents.toml",
                    f"agent `{name}` pattern=`{pattern_value}` not in {sorted(ALLOWED_PATTERNS)}",
                )
            )
        # (d) used_by drift
        stored = entry.get("used_by")
        if not isinstance(stored, list):
            findings.append(
                Finding(
                    "ERROR",
                    "agents-used-by-shape",
                    "harness/agents.toml",
                    f"agent `{name}` is missing `used_by` field (must be a list)",
                )
            )
            stored_set: set[str] = set()
        else:
            stored_set = {s for s in stored if isinstance(s, str)}
        expected_set = set(expected.get(name, []))
        missing = sorted(expected_set - stored_set)
        extra = sorted(stored_set - expected_set)
        if missing or extra:
            parts: list[str] = []
            if missing:
                parts.append(f"missing {missing}")
            if extra:
                parts.append(f"extra {extra}")
            findings.append(
                Finding(
                    "WARN",
                    "agents-used-by-drift",
                    "harness/agents.toml",
                    f"agent `{name}` used_by drift: " + "; ".join(parts) +
                    " (run `python3 scripts/harness_lint.py --fix-used-by`)",
                )
            )
        # (e) orphan
        if not stored_set and not expected_set:
            findings.append(
                Finding(
                    "WARN",
                    "agents-orphan",
                    "harness/agents.toml",
                    f"agent `{name}` has no callers (no intent or command dispatches it)",
                )
            )

    return findings


def fix_used_by() -> int:
    """Rewrite `used_by` lists in harness/agents.toml from the walk.

    Read-modify-write of the agents.toml file. Idempotent: re-running with
    no upstream changes is a no-op. Preserves all other formatting verbatim
    by editing only the `used_by = [...]` block.
    """
    # Refuse to regenerate from partial data. Either load failure (missing
    # intents.toml, invalid TOML, missing command tree) would silently drop
    # references on the rewrite, corrupting the file we are trying to repair.
    intents, intent_findings = load_intents()
    intent_errors = [f for f in intent_findings if f.severity == "ERROR"]
    if intent_errors:
        for f in intent_errors:
            sys.stderr.write(f"harness_lint --fix-used-by: aborting: {f.where}: {f.message}\n")
        sys.stderr.write("harness_lint --fix-used-by: fix the intent registry and retry\n")
        return 2
    commands, command_findings = load_claude_commands()
    command_errors = [f for f in command_findings if f.severity == "ERROR"]
    if command_errors:
        for f in command_errors:
            sys.stderr.write(f"harness_lint --fix-used-by: aborting: {f.where}: {f.message}\n")
        sys.stderr.write("harness_lint --fix-used-by: fix the command registry and retry\n")
        return 2
    agents_path = ROOT / "harness" / "agents.toml"
    if not agents_path.exists():
        sys.stderr.write("harness_lint: harness/agents.toml not found\n")
        return 1
    data, err = _load_toml(agents_path)
    if err:
        sys.stderr.write(f"harness_lint: {err.message}\n")
        return 1
    assert data is not None
    registry = data.get("agents", {})
    if not isinstance(registry, dict):
        sys.stderr.write("harness_lint: harness/agents.toml has no [agents] table\n")
        return 1
    expected = _expected_used_by(intents, commands, registry)

    text = agents_path.read_text(encoding="utf-8")
    new_text = text
    changed_agents: list[str] = []
    # Rewrite each agent's used_by block in place. We match the table header
    # and the existing used_by = [...] block (multi-line), and replace the
    # block contents only — preserving all other fields and ordering.
    for name in sorted(registry.keys()):
        refs = expected.get(name, [])
        formatted = _format_used_by_block(refs)
        # Pattern: `[agents.<name>]` ... `used_by = [...]` (multi-line).
        # Walk past intermediate array fields like `kinds = [...]` and
        # `voices = { ... }` by matching any character that is NOT the start of
        # the next `[agents.` table header. The earlier `[^\[]*?` form forbade
        # ALL `[` characters, so the regex never matched any agent that had an
        # array field before `used_by`.
        section_re = re.compile(
            rf"(\[agents\.{re.escape(name)}\](?:(?!\[agents\.).)*?)(used_by\s*=\s*\[[^\]]*\])",
            re.DOTALL,
        )

        def _sub(match: re.Match[str], formatted: str = formatted) -> str:
            return match.group(1) + formatted

        new_section, count = section_re.subn(_sub, new_text, count=1)
        if count == 0:
            sys.stderr.write(
                f"harness_lint: --fix-used-by: could not locate `used_by` block for `{name}` (skipped)\n"
            )
            continue
        if new_section != new_text:
            new_text = new_section
            changed_agents.append(name)

    if new_text == text:
        print("harness_lint --fix-used-by: no changes")
        return 0
    agents_path.write_text(new_text, encoding="utf-8")
    print(f"harness_lint --fix-used-by: updated {len(changed_agents)} agent(s): {', '.join(changed_agents)}")
    return 0


def _format_used_by_block(refs: list[str]) -> str:
    """Format a `used_by = [...]` TOML block (matches existing style)."""
    if not refs:
        return "used_by = []"
    lines = ["used_by = ["]
    for ref in refs:
        lines.append(f'    "{ref}",')
    lines.append("]")
    return "\n".join(lines)


MAX_DOC_INDIRECTION_DEPTH = 4
DOC_LINT_ROOTS = (".claude/", "protocols/", "harness/", "scripts/")
DOC_LINT_TOPS = ("AGENTS.md", "CLAUDE.md", "README.md")

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
            findings.append(
                Finding(
                    "ERROR",
                    "doc-indirection-cycle",
                    key[0],
                    f"cross-document reference cycle: {' -> '.join(key)}",
                )
            )
        elif long_path is not None:
            key = tuple(p.relative_to(ROOT).as_posix() for p in long_path)
            if key in flagged:
                continue
            flagged.add(key)
            findings.append(
                Finding(
                    "ERROR",
                    "doc-indirection-depth",
                    key[0],
                    f"cross-document indirection chain too deep ({len(key)} hops, max {MAX_DOC_INDIRECTION_DEPTH}): {' -> '.join(key)}",
                )
            )
    return findings


def check_commands_intent_coverage() -> list[Finding]:
    """Require public commands to be routed, aliased, or direct-only."""
    findings: list[Finding] = []
    data, err = _load_toml(ROOT / "harness" / "commands.toml")
    if err:
        return [err]
    assert data is not None
    command_map = data.get("commands", {}) or {}
    intents, intent_err = _load_toml(ROOT / "harness" / "intents.toml")
    if intent_err:
        return [intent_err]
    assert intents is not None
    routed_sources = {
        str(entry.get("procedure", ""))
        for entry in (intents.get("intents", {}) or {}).values()
        if isinstance(entry, dict)
    }

    for name, entry in sorted(command_map.items()):
        if not isinstance(entry, dict):
            continue
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
        findings.append(
            Finding(
                "WARN",
                "commands-routing-undeclared",
                "harness/commands.toml",
                f"command `{name}` has no intent procedure and no `direct_only = true`",
            )
        )
    return findings


def check_decision_record_contract() -> list[Finding]:
    """Keep durable decisions on stable, topic-addressed paths."""
    contracts = {
        ".claude/commands/decision.md": "<paths.gtd>/decisions/<slugified-topic>.md",
        "protocols/session-continuity.md": "<paths.gtd>/decisions/*.md",
    }
    forbidden = "<paths.reflections>/YYYY-MM-DD-decision-"
    findings: list[Finding] = []
    for rel, required in contracts.items():
        text = _read(ROOT / rel)
        if required not in text or forbidden in text:
            findings.append(
                Finding(
                    "ERROR",
                    "decision-record-path",
                    rel,
                    f"decision records must use stable `{required}` paths, never dated reflection filenames",
                )
            )
    return findings


def check_shadow_group_start() -> list[Finding]:
    """Known multi-leg call sites MUST invoke `shadow.py group-start` so the
    shadow-log correlation pipeline has data to aggregate. Without the call,
    legs land in the per-call log but cannot be correlated; the report
    degrades to "0 groups found" silently.

    Per protocols/backend-taxonomy.md § shadow_logs and the shadow-log
    design doc, the multi-leg call sites are enumerated below. Each MUST
    contain the substring `shadow.py group-start`. Site files that don't
    yet exist (the design names them but the user has not yet instrumented
    them) emit INFO; existing files without the invocation emit ERROR.

    Sites with a runtime-native project-agent leg must also use
    `shadow.py native-model --agent <role>`. This prevents Codex agent output
    from being mislabeled with a Claude model identity and cost row.
    """
    sites = {
        ".claude/commands/system-review.md": "privacy-reviewer",
        ".claude/commands/decision.md": "thinker",
        "scripts/review.sh": None,
    }
    findings: list[Finding] = []
    for rel, native_role in sites.items():
        path = ROOT / rel
        if not path.exists():
            findings.append(
                Finding("INFO", "shadow-site-missing", rel,
                        f"multi-leg call site {rel} not found; skipping group-start check")
            )
            continue
        text = _read(path)
        # Flexible match: shadow.py followed by group-start within the same
        # line region (quotes / variable interpolation between is fine).
        if not re.search(r"shadow\.py[\"' \\$\w/]*\s+group-start", text):
            findings.append(
                Finding("ERROR", "shadow-group-start-missing", rel,
                        f"{rel} dispatches multi-leg but does not invoke `shadow.py group-start`; "
                        "shadow-log report will silently degrade. Add a group-start "
                        "invocation at flow entry (see protocols/shadow-log.md).")
            )
        if native_role is not None and not re.search(
            rf"shadow\.py\s+native-model\s+--agent\s+{re.escape(native_role)}\b",
            text,
        ):
            findings.append(
                Finding(
                    "ERROR",
                    "shadow-native-model-missing",
                    rel,
                    f"{rel} must resolve `{native_role}` through `shadow.py native-model` before native-leg correlation",
                )
            )
    return findings


def check_command_frontmatter(commands: dict[str, str]) -> list[Finding]:
    """Every `.claude/commands/*.md` carries `description:` frontmatter that
    mirrors its `harness/commands.toml` entry.

    The Claude Code runtime reads the file frontmatter for the slash-command
    list; Codex and intent dispatch read the registry. Without this check the
    two surfaces drift independently (the original failure mode: no
    frontmatter at all, so the runtime degraded descriptions to heading
    text like `/dine — Purpose`).
    """
    findings: list[Finding] = []
    data, err = _load_toml(ROOT / "harness" / "commands.toml")
    if err:
        return []  # parse failure already reported by check_commands
    command_map = (data or {}).get("commands", {}) or {}

    for name, path in sorted(commands.items()):
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
            findings.append(
                Finding(
                    "WARN",
                    "command-frontmatter",
                    path,
                    "missing `description:` frontmatter — the runtime degrades "
                    "the slash-command description to heading text",
                )
            )
            continue
        entry = command_map.get(name)
        if isinstance(entry, dict):
            toml_desc = str(entry.get("description", "")).strip()
            if toml_desc and desc.strip("\"'") != toml_desc:
                findings.append(
                    Finding(
                        "WARN",
                        "command-frontmatter-drift",
                        path,
                        f"frontmatter description differs from the "
                        f"harness/commands.toml entry for `{name}` — "
                        "mirror the registry prose (or update both)",
                    )
                )
        elif entry is not None:
            findings.append(
                Finding(
                    "INFO",
                    "command-frontmatter-uncheckable",
                    path,
                    f"harness/commands.toml entry for `{name}` is not a "
                    f"table, so the frontmatter drift check cannot compare "
                    "descriptions — convert the entry to `[commands."
                    f"{name}]` table form to restore drift coverage",
                )
            )
    return findings


def check_reader_scholar_sync() -> list[Finding]:
    """Guard the deliberate Reader/Scholar near-duplication.

    `.claude/agents/scholar.md` is `.claude/agents/reader.md` modulo a
    role-name substitution (`Readers`->`Scholars`, `Reader`->`Scholar`,
    word-bounded, case-sensitive — lowercase `reader` is shared vocabulary
    like the `---reader-brief---` sentinel and must NOT differ) plus exactly
    one intentionally divergent body line: the `You are the ...` role intro.
    The two files drifted silently before this check existed; any further
    divergence must be a conscious edit to BOTH files (or to this check).
    """
    findings: list[Finding] = []
    reader_p = ROOT / ".claude" / "agents" / "reader.md"
    scholar_p = ROOT / ".claude" / "agents" / "scholar.md"
    if not reader_p.is_file() or not scholar_p.is_file():
        return findings  # absence is the agent registry checks' problem

    def body_lines(path: Path) -> list[str]:
        lines = path.read_text(encoding="utf-8").splitlines()
        if lines and lines[0].strip() == "---":
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    return lines[i + 1:]
        return lines

    r_body = body_lines(reader_p)
    s_body = body_lines(scholar_p)
    if len(r_body) != len(s_body):
        findings.append(
            Finding(
                "WARN",
                "reader-scholar-sync",
                ".claude/agents/scholar.md",
                f"body line counts differ (reader {len(r_body)} vs scholar "
                f"{len(s_body)}) — the files must stay line-aligned modulo "
                "the role-name substitution; re-sync them",
            )
        )
        return findings

    def subst(line: str) -> str:
        line = re.sub(r"\bReaders\b", "Scholars", line)
        return re.sub(r"\bReader\b", "Scholar", line)

    mismatches = []
    for i, (rl, sl) in enumerate(zip(r_body, s_body), start=1):
        if rl.startswith("You are the ") and sl.startswith("You are the "):
            continue  # the one allowed divergent body line (role intro)
        if subst(rl) != sl:
            mismatches.append(i)
    if mismatches:
        shown = ", ".join(str(i) for i in mismatches[:5])
        more = "" if len(mismatches) <= 5 else f" (+{len(mismatches) - 5} more)"
        findings.append(
            Finding(
                "WARN",
                "reader-scholar-sync",
                ".claude/agents/scholar.md",
                f"{len(mismatches)} body line(s) diverge from reader.md beyond "
                f"the role-name substitution: body line(s) {shown}{more} — "
                "re-sync the drifted wording in both files",
            )
        )
    return findings


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
    for path in sorted((ROOT / "scripts").glob("*.py")):
        if path.name in {"harness_lint.py", "fission.py"}:
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
                findings.append(
                    Finding(
                        "ERROR",
                        "flat-tier-glob",
                        f"scripts/{path.name}:{lineno}",
                        "non-recursive glob over a bucketed tier; use _paths.tier_files() or rglob",
                    )
                )
    for path in _doc_files():
        if path.name == "repo-conventions.md":
            continue
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(_read(path).splitlines(), start=1):
            if _FLAT_TIER_LS_RE.search(line):
                findings.append(
                    Finding(
                        "ERROR",
                        "flat-tier-glob",
                        f"{rel}:{lineno}",
                        "flat `ls` over a bucketed tier; use `find \"$OV/<tier>\" -name ...`",
                    )
                )
    return findings


# model_costs.toml identity -> pricing.toml provider slot for the same model.
PRICE_SURFACE_MAP = {
    "opus": ("anthropic", "flagship"),
    "sonnet": ("anthropic", "standard"),
    "codex_gpt55_max": ("openai", "standard"),
    "deepseek_pro_max": ("deepseek", "flagship"),
    "deepseek_pro": ("deepseek", "flagship"),
    "deepseek_flash": ("deepseek", "standard"),
}


def check_price_surfaces_agree() -> list[Finding]:
    """The two pricing files state the same volatile facts; they must match."""
    findings: list[Finding] = []
    costs_data, err1 = _load_toml(ROOT / "harness" / "model_costs.toml")
    catalog, err2 = _load_toml(ROOT / "scripts" / "pricing.toml")
    for err in (err1, err2):
        if err:
            findings.append(err)
    if not costs_data or not catalog:
        return findings
    costs = costs_data.get("costs", {})
    providers = catalog.get("providers", {})
    for identity, (provider, slot) in PRICE_SURFACE_MAP.items():
        row = costs.get(identity)
        entry = providers.get(provider, {}).get(slot)
        if not isinstance(row, dict) or not isinstance(entry, dict):
            continue
        for cost_key, catalog_key in (("input_per_1m_usd", "input"), ("output_per_1m_usd", "output")):
            a, b = row.get(cost_key), entry.get(catalog_key)
            if a is not None and b is not None and float(a) != float(b):
                findings.append(
                    Finding(
                        "ERROR",
                        "price-surface-drift",
                        f"harness/model_costs.toml:costs.{identity}",
                        f"{cost_key}={a} disagrees with scripts/pricing.toml providers.{provider}.{slot}.{catalog_key}={b}",
                    )
                )
    return findings


# Prose-budget ratchet. Baseline measured 2026-08-24. Raising these numbers is
# allowed only in a commit whose message justifies the growth (subtract before
# adding); the point is that growth becomes a decision, not a drift.
PROSE_BUDGET_WARN = 556968
PROSE_BUDGET_ERROR = 596164


def check_prose_budget() -> list[Finding]:
    """protocols/ + .claude/agents/ grew 2.3x in four months unnoticed."""
    total = 0
    for root in ("protocols", ".claude/agents"):
        base = ROOT / root
        if base.is_dir():
            total += sum(p.stat().st_size for p in base.rglob("*.md"))
    if total > PROSE_BUDGET_ERROR:
        return [
            Finding(
                "ERROR",
                "prose-budget",
                "protocols/ + .claude/agents/",
                f"{total} bytes exceeds the {PROSE_BUDGET_ERROR}-byte ceiling; subtract before adding",
            )
        ]
    if total > PROSE_BUDGET_WARN:
        return [
            Finding(
                "WARN",
                "prose-budget",
                "protocols/ + .claude/agents/",
                f"{total} bytes exceeds the {PROSE_BUDGET_WARN}-byte budget; plan a pruning pass",
            )
        ]
    return []


# Hot-path files are re-read on every scheduled run (nightly trio) or on
# every harness edit (evolution.md), so their bytes bill recurrently. Ceilings hold the 2026-08 compression wins;
# raising one requires subtracting elsewhere on the same hot path.
HOT_PATH_CEILINGS = {
    "protocols/evolution.md": 4096,
    ".claude/commands/autoevo-nightly.md": 24576,
    ".claude/agents/forgetter.md": 15360,
    ".claude/agents/curator.md": 20480,
}


def check_hot_path_ceilings(ceilings: dict[str, int] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for rel, ceiling in (ceilings or HOT_PATH_CEILINGS).items():
        path = ROOT / rel
        if not path.is_file():
            findings.append(
                Finding("ERROR", "hot-path-ceiling", rel, "hot-path file missing")
            )
            continue
        size = path.stat().st_size
        if size > ceiling:
            findings.append(
                Finding(
                    "ERROR",
                    "hot-path-ceiling",
                    rel,
                    f"{size} bytes exceeds the {ceiling}-byte nightly hot-path ceiling; subtract before adding",
                )
            )
    return findings


BANNED_BOT_TRAILER = "Co-Authored-By: Atelier Autoevo Bot"


def check_bot_trailer_banned(roots: list[str] | None = None) -> list[Finding]:
    """Bot identity moved from co-author trailer to GIT_AUTHOR/COMMITTER env
    (protocols/autoevo.md § Per-op commit policy). A prompt-layer commit
    template reintroducing the trailer would attribute bot ops to the user;
    scripts/autoevo_commit.py is the sole committer."""
    findings: list[Finding] = []
    for root in roots or (".claude/commands", ".claude/agents", "protocols"):
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if BANNED_BOT_TRAILER in path.read_text(encoding="utf-8", errors="ignore"):
                findings.append(
                    Finding(
                        "ERROR",
                        "bot-trailer-banned",
                        rel(path),
                        "dead Co-Authored-By bot trailer; route the commit through scripts/autoevo_commit.py",
                    )
                )
    return findings


def run_lints() -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_root_files())
    agents, agent_findings = load_claude_agents()
    findings.extend(agent_findings)
    commands, command_findings = load_claude_commands()
    findings.extend(command_findings)
    model_findings, models = check_models(agents)
    findings.extend(model_findings)
    findings.extend(check_capabilities())
    findings.extend(check_runtime_registry())
    findings.extend(check_agent_registry(agents, models))
    findings.extend(check_codex_agent_adapters(models))
    findings.extend(check_codex_hooks())
    findings.extend(check_claude_hooks())
    findings.extend(check_commands(commands))
    findings.extend(check_command_frontmatter(commands))
    findings.extend(check_reader_scholar_sync())
    findings.extend(check_harness_readme())
    findings.extend(check_atelier_skill())
    findings.extend(check_codex_command_skills())
    findings.extend(check_scripts_zk_paths())
    findings.extend(check_path_registry_drift())
    intents, intent_findings = load_intents()
    findings.extend(intent_findings)
    findings.extend(check_commands_intent_coverage())
    findings.extend(check_decision_record_contract())
    # Resolve intent agent references against both registries:
    #   - `agents` is from load_claude_agents() (.claude/agents/*.md filesystem
    #     walk); canonical for Claude Code subagent dispatch.
    #   - `harness_agents_data` is from harness/agents.toml; canonical for
    #     Codex parity. A broken reference in either is an error.
    harness_agents_raw, _ = _load_toml(ROOT / "harness" / "agents.toml")
    harness_agents_data = (harness_agents_raw or {}).get("agents", {}) or {}
    findings.extend(check_intents_registry(intents, agents, harness_agents_data))
    findings.extend(check_intents_overlay())
    findings.extend(check_autoevo_band_sync())
    findings.extend(check_intents_procedures(intents))
    findings.extend(check_intents_agents_in_procedure(intents))
    findings.extend(check_intents_profile_reads(intents))
    findings.extend(check_claude_skills(intents))
    findings.extend(check_agent_pattern_and_used_by(intents, commands))
    findings.extend(check_doc_indirection_depth())
    findings.extend(check_shadow_group_start())
    findings.extend(check_flat_tier_globs())
    findings.extend(check_price_surfaces_agree())
    findings.extend(check_prose_budget())
    findings.extend(check_hot_path_ceilings())
    findings.extend(check_bot_trailer_banned())
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 99), f.code, f.where, f.message))
    return findings


def format_table(findings: list[Finding]) -> str:
    if not findings:
        return "harness_lint: clean (no findings)\n"

    counts = {"ERROR": 0, "WARN": 0, "INFO": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1

    lines = [
        f"harness lint report: {counts['ERROR']} error, {counts['WARN']} warn, {counts['INFO']} info",
        "",
    ]
    for finding in findings:
        lines.append(f"[{finding.severity:5s}] {finding.code}")
        lines.append(f"    where:   {finding.where}")
        lines.append(f"    message: {finding.message}")
        lines.append("")
    return "\n".join(lines)


def format_json(findings: list[Finding]) -> str:
    payload = {
        "counts": {
            "error": sum(1 for f in findings if f.severity == "ERROR"),
            "warn": sum(1 for f in findings if f.severity == "WARN"),
            "info": sum(1 for f in findings if f.severity == "INFO"),
        },
        "findings": [f.to_dict() for f in findings],
    }
    return json.dumps(payload, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/harness_lint.py",
        description="Check Claude Code and Codex harness portability.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument(
        "--fix-used-by",
        action="store_true",
        help="Regenerate `used_by` lists in harness/agents.toml from the intents/commands walk, then exit. Mutating; off by default.",
    )
    args = parser.parse_args(argv)

    if args.fix_used_by:
        return fix_used_by()

    findings = run_lints()
    if args.json:
        sys.stdout.write(format_json(findings))
    else:
        sys.stdout.write(format_table(findings))
    return 1 if any(f.severity == "ERROR" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
