"""Generated-byte drift plus independent runtime-template contracts.

The lint reuses the renderer, so expected fields, policies, and effort
mappings below do not come from that implementation. Mutations use a
synthetic repository, never private profiles or overlays.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
import harness_lint as lint  # noqa: E402
import render_runtime_edges as edges  # noqa: E402


class RenderCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="atelier-render-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for module in (lint, edges):
            patched = patch.object(module, "ROOT", self.root)
            patched.start()
            self.addCleanup(patched.stop)
        self.agents = {"agents": {name: {
            "source": f".claude/agents/{name}.md", "voices": {"native": "sample"},
            "description": f"{name} role.", "status": "portable-adapted",
        } for name in ("sample", "forgetter")}}
        self.agents["agents"]["external"] = {
            "source": "scripts/sample_leg.sh", "status": "script-driven",
            "voices": {"direct": "sample"}, "description": "External review.",
        }
        self.commands = {"commands": {name: {
            "source": f".claude/commands/{name}.md", "description": f"{name} command.",
            "user_facing": name == "sample",
        } for name in ("sample", "hidden", "atelier")}}
        self.models = {"models": {"sample": {"reasoning_tier": "xdeep"}}}
        for name, document in (("agents", self.agents), ("commands", self.commands)):
            lines = []
            for key, row in document[name].items():
                lines.append(f"[{name}.{key}]")
                for field, value in row.items():
                    rendered = ("{ " + ", ".join(f'{k} = "{v}"' for k, v in value.items()) + " }"
                                if isinstance(value, dict) else
                                str(value).lower() if isinstance(value, bool) else f'"{value}"')
                    lines.append(f"{field} = {rendered}")
            self.write(f"harness/{name}.toml", "\n".join(lines))
        self.write("harness/models.toml", '[models.sample]\nreasoning_tier = "xdeep"\n')

    def write(self, path: str | Path, text: str) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def rendered(self) -> dict[Path, str]:
        return edges.render_codex(self.agents, self.commands, self.models)

    def findings(self) -> list[lint.Finding]:
        return lint.check_codex_edges({
            "harness/agents.toml": self.agents,
            "harness/commands.toml": self.commands,
            "harness/models.toml": self.models,
        })

    def assert_template_contract(self, files: dict[Path, str], effort: str = "xhigh") -> None:
        expected = {self.root / path for path in (
            ".codex/agents/sample.toml", ".codex/agents/forgetter.toml",
            ".agents/skills/sample/SKILL.md", ".agents/skills/sample/agents/openai.yaml",
        )}
        self.assertEqual(set(files), expected)  # no script-driven/bot-only/handwritten edges
        for name in ("sample", "forgetter"):
            adapter = tomllib.loads(files[self.root / f".codex/agents/{name}.toml"])
            self.assertEqual(adapter["name"], name)
            self.assertEqual(adapter["description"], f"{name} role.")
            self.assertEqual(adapter["model_reasoning_effort"], effort)
            self.assertNotIn("model", adapter)  # inherit the selected native model
            for needle in ("AGENTS.md", "CLAUDE.md", f".claude/agents/{name}.md",
                           "role's write boundary", "never take orchestrator-owned write, approval, or commit actions"):
                self.assertIn(needle, adapter["developer_instructions"])
        forgetter = files[self.root / ".codex/agents/forgetter.toml"]
        for marker in ("---forgetter-result---", "---end-result---"):
            self.assertIn(marker, forgetter)
        self.assertNotIn("---begin-result---", forgetter)
        skill = files[self.root / ".agents/skills/sample/SKILL.md"]
        for needle in ("name: sample", "$sample", "AGENTS.md", "CLAUDE.md",
                       ".claude/commands/sample.md", "Do not start a nested Codex process"):
            self.assertIn(needle, skill)
        for retired in ("scripts/atelier.py", "scripts/intent_coverage.py"):
            self.assertNotIn(retired, skill)
        metadata = files[self.root / ".agents/skills/sample/agents/openai.yaml"]
        self.assertIn("$sample", metadata)
        self.assertIn("allow_implicit_invocation: false", metadata)
        self.assertNotIn("allow_implicit_invocation: true", metadata)

    def test_template_semantics_and_all_effort_mappings(self) -> None:
        for tier, effort in (("light", "low"), ("balanced", "medium"),
                             ("deep", "high"), ("xdeep", "xhigh")):
            with self.subTest(tier=tier):
                self.models["models"]["sample"]["reasoning_tier"] = tier
                self.assert_template_contract(self.rendered(), effort)

    def test_missing_drifted_unreadable_and_unexpected_edges_fail(self) -> None:
        files = self.rendered()
        for path, text in files.items():
            self.write(path, text)
        self.write(".agents/skills/atelier/SKILL.md", "handwritten")
        self.assertEqual(self.findings(), [])
        for path, text in files.items():
            with self.subTest(path=path):
                path.unlink()
                self.assertEqual([f.code for f in self.findings()], ["codex-edge-missing"])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(edges.main(["--runtime", "codex", "--check"]), 1)
                self.write(path, "invalid or hand-edited\n")
                self.assertEqual([f.code for f in self.findings()], ["codex-edge-drift"])
                self.write(path, text)
        for path in (".codex/agents/obsolete.toml", ".agents/skills/obsolete/SKILL.md",
                     ".agents/skills/orphan/agents/openai.yaml"):
            extra = self.write(path, "unregistered")
            self.assertEqual([f.code for f in self.findings()], ["codex-edge-unregistered"])
            extra.unlink()
        with patch.object(Path, "read_bytes", side_effect=PermissionError("unreadable")):
            self.assertEqual({f.code for f in self.findings()}, {"codex-edge-read"})

    def test_reasoning_tier_errors_remain_independent_of_renderer(self) -> None:
        self.models["models"]["sample"]["reasoning_tier"] = "invalid"
        self.assertEqual([f.code for f in self.findings()], ["codex-edge-render"])
        findings, _ = lint.check_models(
            {},
            self.models["models"],
            self.agents["agents"],
        )
        self.assertIn("models-reasoning-tier", [f.code for f in findings])

    def test_self_consistent_generated_native_toml_must_roundtrip(self) -> None:
        path = self.root / "harness/agents.toml"
        original = path.read_text()
        for description, code in ((r"Role \q", "invalid-toml"), (r"Role \tools", "codex-edge-values")):
            with self.subTest(description=description):
                self.write(path, original.replace('description = "sample role."',
                                                   f"description = '{description}'"))
                self.agents["agents"]["sample"]["description"] = description
                for adapter_path, text in self.rendered().items():
                    self.write(adapter_path, text)
                self.assertEqual([(f.code, f.where) for f in self.findings()],
                                 [(code, ".codex/agents/sample.toml")])

    def test_user_facing_command_cannot_claim_handwritten_skill(self) -> None:
        self.commands["commands"]["atelier"]["user_facing"] = True
        self.assert_template_contract(self.rendered())  # rendering never overwrites handwritten skills
        with patch.object(edges, "render_codex", side_effect=AssertionError("rendered reserved command")):
            self.assertEqual([f.code for f in self.findings()], ["codex-command-reserved"])

    def test_committed_codex_edge_is_render_clean(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/render_runtime_edges.py", "--runtime", "codex", "--check"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("byte-for-byte", result.stdout)
        commands = tomllib.loads((REPO_ROOT / "harness/commands.toml").read_text())["commands"]
        self.assertGreaterEqual(sum(row.get("user_facing", True) is not False
                                    for row in commands.values()), 10)


if __name__ == "__main__":
    unittest.main()
