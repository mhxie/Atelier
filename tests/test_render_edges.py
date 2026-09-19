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

import yaml

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
            "source": f"agents/{name}.md", "voices": {"native": "sample"},
            "description": f"{name} role.", "status": "canonical",
        } for name in ("sample", "forgetter")}}
        self.agents["agents"]["external"] = {
            "source": "scripts/sample_leg.sh", "status": "script-driven",
            "voices": {"direct": "sample"}, "description": "External review.",
        }
        self.skills = {"skills": {"sample": {
            "source": "skills/sample/SKILL.md", "description": "sample skill.",
            "status": "canonical",
        }}}
        self.models = {"models": {"sample": {"reasoning_tier": "xdeep"}}}
        for name, document in (("agents", self.agents), ("skills", self.skills)):
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
        for name in ("sample", "forgetter"):
            self.write(f"agents/{name}.md", (
                f"---\nname: {name}\ndescription: {name} role.\ntools: Read\nmodel: sample\n---\n\n"
                + ("---forgetter-result---\n---end-result---\n" if name == "forgetter" else "Role body.\n")
            ))
        self.write("skills/sample/SKILL.md", "---\nname: sample\ndescription: sample skill.\n---\n\nWorkflow.\n")

    def write(self, path: str | Path, text: str) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def rendered(self) -> dict[Path, str]:
        return edges.render_all(self.agents, self.skills, self.models)

    def findings(self) -> list[lint.Finding]:
        return lint.check_runtime_edges({
            "harness/agents.toml": self.agents,
            "harness/skills.toml": self.skills,
            "harness/models.toml": self.models,
        })

    def assert_template_contract(self, files: dict[Path, str], effort: str = "xhigh") -> None:
        expected = {self.root / path for path in (
            ".codex/agents/sample.toml", ".codex/agents/forgetter.toml",
            ".agents/skills/sample/SKILL.md", ".agents/skills/sample/agents/openai.yaml",
            ".claude/agents/sample.md", ".claude/agents/forgetter.md",
            ".claude/commands/sample.md",
        )}
        self.assertTrue(all("CLAUDE.md" not in text for text in files.values()))
        self.assertIn("Read `AGENTS.md`", files[self.root / ".claude/agents/sample.md"])
        self.assertEqual(set(files), expected)  # no script-driven/bot-only/handwritten edges
        for name in ("sample", "forgetter"):
            adapter = tomllib.loads(files[self.root / f".codex/agents/{name}.toml"])
            self.assertEqual(adapter["name"], name)
            self.assertEqual(adapter["description"], f"{name} role.")
            self.assertEqual(adapter["model_reasoning_effort"], effort)
            self.assertNotIn("model", adapter)  # inherit the selected native model
            for needle in ("AGENTS.md", f"agents/{name}.md",
                           "role's write boundary", "never take orchestrator-owned write, approval, or commit actions"):
                self.assertIn(needle, adapter["developer_instructions"])
        forgetter = files[self.root / ".codex/agents/forgetter.toml"]
        for marker in ("---forgetter-result---", "---end-result---"):
            self.assertIn(marker, forgetter)
        self.assertNotIn("---begin-result---", forgetter)
        skill = files[self.root / ".agents/skills/sample/SKILL.md"]
        for needle in ("name: sample", "$sample", "AGENTS.md",
                       "skills/sample/SKILL.md", "Do not start a nested Codex process"):
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

    def test_skill_descriptions_roundtrip_yaml(self) -> None:
        for description in ('Daily digest: JSON collection.', 'A "quoted" path: C:\\tools',
                            'first line\nsecond line', 'true', '中文说明'):
            with self.subTest(description=description):
                self.skills["skills"]["sample"]["description"] = description
                files = self.rendered()
                for relative in (".claude/commands/sample.md", ".agents/skills/sample/SKILL.md"):
                    parsed = yaml.safe_load(files[self.root / relative].split("---\n", 2)[1])
                    self.assertIsInstance(parsed["description"], str)
                    self.assertTrue(parsed["description"].endswith(description))

    def test_missing_drifted_unreadable_and_unexpected_edges_fail(self) -> None:
        files = self.rendered()
        for path, text in files.items():
            self.write(path, text)
        self.write(".agents/skills/atelier/SKILL.md", "handwritten")
        self.assertEqual(self.findings(), [])
        for path, text in files.items():
            with self.subTest(path=path):
                path.unlink()
                self.assertEqual([f.code for f in self.findings()], ["runtime-edge-missing"])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(edges.main(["--runtime", "all", "--check"]), 1)
                self.write(path, "invalid or hand-edited\n")
                self.assertEqual([f.code for f in self.findings()], ["runtime-edge-drift"])
                self.write(path, text)
        for path in (".codex/agents/obsolete.toml", ".agents/skills/obsolete/SKILL.md",
                     ".agents/skills/orphan/agents/openai.yaml", ".claude/commands/obsolete.md",
                     ".claude/agents/obsolete.md"):
            extra = self.write(path, "unregistered")
            self.assertEqual([f.code for f in self.findings()], ["runtime-edge-unregistered"])
            extra.unlink()
        with patch.object(Path, "read_bytes", side_effect=PermissionError("unreadable")):
            self.assertEqual({f.code for f in self.findings()}, {"runtime-edge-read"})

    def test_reasoning_tier_errors_remain_independent_of_renderer(self) -> None:
        self.models["models"]["sample"]["reasoning_tier"] = "invalid"
        self.assertEqual([f.code for f in self.findings()], ["runtime-edge-render"])
        findings, _ = lint.check_models(
            {},
            self.models["models"],
            self.agents["agents"],
        )
        self.assertIn("models-reasoning-tier", [f.code for f in findings])

    def test_self_consistent_generated_native_toml_must_roundtrip(self) -> None:
        path = self.root / "harness/agents.toml"
        original = path.read_text()
        for description, code in ((r"Role \q", "invalid-toml"), (r"Role \tools", "runtime-edge-values")):
            with self.subTest(description=description):
                self.write(path, original.replace('description = "sample role."',
                                                   f"description = '{description}'"))
                self.agents["agents"]["sample"]["description"] = description
                for adapter_path, text in self.rendered().items():
                    self.write(adapter_path, text)
                self.assertEqual([(f.code, f.where) for f in self.findings()],
                                 [(code, ".codex/agents/sample.toml")])

    def test_registry_cannot_claim_handwritten_skill(self) -> None:
        self.skills["skills"]["atelier"] = {
            "source": "skills/atelier/SKILL.md", "description": "reserved", "status": "canonical"
        }
        self.assertEqual([f.code for f in self.findings()], ["skill-reserved"])

    def test_committed_codex_edge_is_render_clean(self) -> None:
        self.assertEqual((REPO_ROOT / "CLAUDE.md").read_text(), "@AGENTS.md\n")
        result = subprocess.run(
            [sys.executable, "scripts/render_runtime_edges.py", "--runtime", "all", "--check"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("byte-for-byte", result.stdout)
        skills = tomllib.loads((REPO_ROOT / "harness/skills.toml").read_text())["skills"]
        self.assertGreaterEqual(len(skills), 10)


if __name__ == "__main__":
    unittest.main()
