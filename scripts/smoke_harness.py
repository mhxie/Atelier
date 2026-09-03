"""smoke_harness.py: registry, Codex edge, and runtime-selector smoke checks.

Split out of harness_smoke.py; harness_smoke.py re-exports every name so callers and tests are unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path

from smoke_common import (  # noqa: E402
    PYTHON,
    ROOT,
    expect,
    run,
)


def check_harness_lint() -> None:
    payload = json.loads(run(["scripts/harness_lint.py", "--json"]))
    counts = payload["counts"]
    expect(
        counts.get("error", 0) == 0,
        f"harness_lint.py reports {counts.get('error', 0)} error(s)",
    )
    expect(
        counts.get("warn", 0) == 0,
        f"harness_lint.py reports {counts.get('warn', 0)} warn(s)",
    )
    error_or_warn = [
        f for f in payload["findings"] if f.get("severity") in ("ERROR", "WARN")
    ]
    expect(
        error_or_warn == [],
        f"harness_lint.py returned {len(error_or_warn)} error/warn finding(s)",
    )

def check_codex_command_skills() -> None:
    with (ROOT / "harness" / "commands.toml").open("rb") as handle:
        commands = tomllib.load(handle)["commands"]
    expected = {
        name: entry
        for name, entry in commands.items()
        if isinstance(entry, dict) and entry.get("user_facing", True) is not False
    }
    expect(len(expected) >= 10, "expected user-facing portable commands")
    actual = {
        path.parent.name
        for path in (ROOT / ".agents" / "skills").glob("*/SKILL.md")
        if path.parent.name != "atelier"
    }
    expect(
        actual == set(expected),
        "native Codex command skills drifted from user-facing registry rows",
    )
    for name, entry in expected.items():
        skill_dir = ROOT / ".agents" / "skills" / name
        skill_path = skill_dir / "SKILL.md"
        metadata_path = skill_dir / "agents" / "openai.yaml"
        expect(skill_path.exists(), f"missing Codex command skill `${name}`")
        expect(metadata_path.exists(), f"missing Codex metadata for `${name}`")
        skill = skill_path.read_text(encoding="utf-8")
        metadata = metadata_path.read_text(encoding="utf-8")
        expect(
            str(entry["source"]) in skill,
            f"`${name}` does not point to its command source",
        )
        expect(
            "scripts/atelier.py" not in skill,
            f"`${name}` still calls the retired bridge",
        )
        expect(
            f"${name}" in metadata, f"`${name}` metadata lacks its explicit invocation"
        )
        expect(
            "allow_implicit_invocation: false" in metadata,
            f"`${name}` must remain explicit-only",
        )

def check_codex_native_agents() -> None:
    with (ROOT / "harness" / "agents.toml").open("rb") as handle:
        agents = tomllib.load(handle)["agents"]
    with (ROOT / "harness" / "models.toml").open("rb") as handle:
        models = tomllib.load(handle)["models"]
    expected = {
        name
        for name, entry in agents.items()
        if isinstance(entry, dict) and entry.get("status") != "script-driven"
    }
    actual = {path.stem for path in (ROOT / ".codex" / "agents").glob("*.toml")}
    expect(
        actual == expected,
        "native Codex agent adapters drifted from harness/agents.toml",
    )
    from render_runtime_edges import TIER_TO_EFFORT as effort_by_tier
    for name in expected:
        with (ROOT / ".codex" / "agents" / f"{name}.toml").open("rb") as handle:
            adapter = tomllib.load(handle)
        native_identity = agents[name]["voices"]["native"]
        reasoning_tier = models[native_identity]["reasoning_tier"]
        expect(
            adapter.get("model_reasoning_effort") == effort_by_tier[reasoning_tier],
            f"native Codex agent `{name}` reasoning effort drift",
        )
        if name == "forgetter":
            instructions = str(adapter.get("developer_instructions", ""))
            expect(
                "---forgetter-result---" in instructions
                and "---begin-result---" not in instructions,
                "native Forgetter adapter has a conflicting envelope marker",
            )

def check_runtime_selector() -> None:
    status = json.loads(run(["scripts/atelier_runtime.py", "status", "--json"]))
    expect(
        status["committed_default"] == "codex", "shipped runtime default must be Codex"
    )
    expect(
        set(status["available"]) == {"claude", "codex"},
        "runtime registry must expose both CLIs",
    )

    codex = run(
        [
            "scripts/atelier_runtime.py",
            "run",
            "--runtime",
            "codex",
            "--dry-run",
            "hi",
            "smoke",
        ]
    ).strip()
    expect(
        "codex -C" in codex and "'$hi smoke'" in codex, "Codex selector command drift"
    )

    claude = run(
        [
            "scripts/atelier_runtime.py",
            "run",
            "--runtime",
            "claude",
            "--non-interactive",
            "--dry-run",
            "lint",
        ]
    ).strip()
    expect(claude == "claude -p /lint", "Claude selector command drift")

    overridden = json.loads(
        run(
            ["scripts/atelier_runtime.py", "resolve", "--json"],
            env_overrides={"ATELIER_RUNTIME": "claude"},
        )
    )
    expect(
        overridden == {"runtime": "claude", "source": "environment"},
        "runtime env override drift",
    )

    codex_native = run(
        [
            "scripts/shadow.py",
            "native-model",
            "--agent",
            "thinker",
            "--runtime",
            "codex",
        ]
    ).strip()
    claude_native = run(
        [
            "scripts/shadow.py",
            "native-model",
            "--agent",
            "thinker",
            "--runtime",
            "claude",
        ]
    ).strip()
    expect(codex_native == "codex_native", "Codex native shadow identity drift")
    expect(claude_native == "opus", "Claude native shadow identity drift")

    neutral_env = os.environ.copy()
    for key in (
        "ATELIER_ACTIVE_RUNTIME",
        "CODEX_THREAD_ID",
        "CLAUDECODE",
        "CLAUDE_PROJECT_DIR",
    ):
        neutral_env.pop(key, None)
    neutral = subprocess.run(
        [PYTHON, "scripts/shadow.py", "native-model", "--agent", "thinker"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=neutral_env,
    )
    expect(
        neutral.returncode == 0, f"neutral native model lookup failed: {neutral.stderr}"
    )
    expected_neutral = "codex_native" if status["runtime"] == "codex" else "opus"
    expect(
        neutral.stdout.strip() == expected_neutral,
        "native model lookup must honor the selected runtime outside a live session",
    )

def check_runtime_cue_syntax() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-cue-runtime-") as temp_dir:
        (Path(temp_dir) / "reflections").mkdir()
        codex = json.loads(
            run(
                ["scripts/cues.py", "--only", "weekly", "--json", "--runtime", "codex"],
                env_overrides={"OV": temp_dir},
            )
        )
        claude = json.loads(
            run(
                [
                    "scripts/cues.py",
                    "--only",
                    "weekly",
                    "--json",
                    "--runtime",
                    "claude",
                ],
                env_overrides={"OV": temp_dir},
            )
        )
        expect(
            len(codex) == 1 and "`$weekly`" in codex[0]["message"],
            "Codex cue syntax drift",
        )
        expect(
            len(claude) == 1 and "`/weekly`" in claude[0]["message"],
            "Claude cue syntax drift",
        )
