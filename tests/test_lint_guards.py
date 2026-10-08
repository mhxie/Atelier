"""Outcome tests for harness lint guards."""

from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
import harness_lint as h  # noqa: E402

PUBLIC_CONFIG_PATHS = (
    "harness/models.toml",
    "harness/agents.toml",
    "harness/skills.toml",
    "harness/capabilities.toml",
    "harness/runtimes.toml",
    "harness/intents.toml",
    "harness/paths.toml",
    "routines/registry.toml",
    ".codex/hooks.json",
    ".claude/settings.json",
)


@contextlib.contextmanager
def _lint_root():
    with tempfile.TemporaryDirectory(prefix="atelier-lint-") as tmp:
        root = Path(tmp)
        with patch.object(h, "ROOT", root):
            yield root


def _write(root: Path, name: str, body: str) -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


class RootFileGuardsTest(unittest.TestCase):
    def test_agents_size_boundaries_and_bold_markers(self) -> None:
        with _lint_root() as root:
            _write(root, "protocols/runtime-adapters.md", "runtime contract")
            self.assertEqual([f.code for f in h.check_root_files()], ["missing-agents-md"])
            _write(root, "AGENTS.md", "protocols/runtime-adapters.md")
            self.assertEqual(h.check_root_files(), [])
            for size, severity in ((8192, None), (8193, "WARN"), (15000, "WARN"), (15001, "ERROR")):
                with self.subTest(size=size):
                    _write(root, "AGENTS.md", "protocols/runtime-adapters.md".ljust(size, "x"))
                    findings = h.check_root_files()
                    self.assertEqual([(f.code, f.severity) for f in findings],
                                     [("agents-size", severity)] if severity else [])
            _write(root, "AGENTS.md", "protocols/runtime-adapters.md **bold**")
            self.assertEqual([(f.code, f.severity) for f in h.check_root_files()],
                             [("agents-bold", "INFO")])


class RegistrySchemaGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="atelier-registry-schema-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in (*PUBLIC_CONFIG_PATHS, "harness/registry.schema.json"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / name, target)
        # load_harness_config deliberately resolves the validator from ROOT.
        # Keep a patched-root fixture on the same interpreter as the real suite.
        project_venv = REPO_ROOT / ".venv"
        if project_venv.is_dir():
            (self.root / ".venv").symlink_to(project_venv, target_is_directory=True)
        else:
            python = self.root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.symlink_to(Path(sys.executable))

    def load(self) -> tuple[dict, list[h.Finding]]:
        with patch.object(h, "ROOT", self.root):
            return h.load_harness_config()

    def validate(self, data: dict) -> list[h.Finding]:
        with patch.object(h, "ROOT", self.root):
            return h.validate_harness_config(data)

    def test_all_ten_documents_are_clean_and_each_schema_branch_rejects_bad_data(self) -> None:
        data, findings = self.load()
        self.assertEqual(findings, [])
        self.assertEqual(set(data), set(PUBLIC_CONFIG_PATHS))

        model = next(iter(data["harness/models.toml"]["models"]))
        agent = next(iter(data["harness/agents.toml"]["agents"]))
        skill = next(iter(data["harness/skills.toml"]["skills"]))
        capability = next(iter(data["harness/capabilities.toml"]["capabilities"]))
        intent = next(iter(data["harness/intents.toml"]["intents"]))
        routine = data["routines/registry.toml"]["routine"][0]
        path_name = next(
            name for name, value in data["harness/paths.toml"]["paths"].items()
            if isinstance(value, str)
        )
        temporal = tomllib.loads(
            "date = 1979-05-27\ntime = 07:32:00\ndatetime = 1979-05-27T07:32:00Z\n"
        )
        faults = (
            ("harness/models.toml", ("models", model, "reasoning_tier"), 7),
            ("harness/agents.toml", ("agents", agent, "source"), 7),
            ("harness/skills.toml", ("skills", skill, "source"), 7),
            ("harness/capabilities.toml", ("capabilities", capability, "codex"), []),
            ("harness/runtimes.toml", ("runtime", "default"), "claude"),
            ("harness/intents.toml", ("intents", intent, "context_budget_tokens"), 0),
            ("harness/intents.toml", ("intents", intent, "context_budget_tokens"), 16385),
            ("harness/intents.toml", ("intents", intent, "context_budget_bytes"), 8192),
            ("harness/paths.toml", ("paths", path_name), 7),
            ("routines/registry.toml", ("routine", 0, "runner"), "model"),
            ("routines/registry.toml", ("routine", 0, "name"), routine["name"] + "_bad"),
            (".codex/hooks.json", ("hooks",), []),
            (".claude/settings.json", ("hooks",), []),
            ("harness/models.toml", ("models", model, "reasoning_tier"), temporal["date"]),
            ("harness/intents.toml", ("intents", intent, "description"), temporal["time"]),
            ("harness/intents.toml", ("intents", intent, "procedure"), temporal["datetime"]),
        )
        for name, keys, value in faults:
            with self.subTest(name=name, field=".".join(map(str, keys))):
                broken = copy.deepcopy(data)
                cursor = broken[name]
                for key in keys[:-1]:
                    cursor = cursor[key]
                cursor[keys[-1]] = value
                errors = self.validate(broken)
                self.assertTrue(errors)
                self.assertEqual({finding.code for finding in errors}, {"registry-schema"})
                self.assertTrue(
                    any(finding.where.startswith(f"{name}:$") for finding in errors),
                    errors,
                )

        budget_path = ("harness/intents.toml", "intents", intent, "context_budget_tokens")
        for value, expected_codes in ((8192, []), (8192.0, ["registry-schema"])):
            with self.subTest(context_budget_tokens=value):
                broken = copy.deepcopy(data)
                broken[budget_path[0]][budget_path[1]][budget_path[2]][budget_path[3]] = value
                self.assertEqual(
                    [finding.code for finding in self.validate(broken)],
                    expected_codes,
                )

    def test_unknown_agent_and_intent_metadata_remain_supported(self) -> None:
        data, findings = self.load()
        self.assertEqual(findings, [])
        metadata = {
            "pattern": None,
            "used_by": 42,
            "kinds": [],
            "dispatch_rationale": False,
            **tomllib.loads(
                "metadata_date = 1979-05-27\n"
                "metadata_time = 07:32:00\n"
                "metadata_datetime = 1979-05-27T07:32:00Z\n"
            ),
        }
        agent = next(iter(data["harness/agents.toml"]["agents"].values()))
        intent = next(iter(data["harness/intents.toml"]["intents"].values()))
        agent.update(metadata)
        intent.update(metadata)
        self.assertEqual(self.validate(data), [])

    def test_loader_accumulates_malformed_and_missing_public_documents(self) -> None:
        (self.root / "harness" / "models.toml").write_text("[models.bad\n", encoding="utf-8")
        (self.root / ".codex" / "hooks.json").write_text("{", encoding="utf-8")
        (self.root / "harness" / "capabilities.toml").unlink()
        data, findings = self.load()
        self.assertEqual(data, {})
        self.assertEqual({finding.code for finding in findings}, {"registry-read"})
        self.assertEqual(
            {finding.where for finding in findings},
            {"harness/models.toml", "harness/capabilities.toml", ".codex/hooks.json"},
        )

    def test_schema_and_validator_fail_closed(self) -> None:
        schema_path = self.root / "harness" / "registry.schema.json"
        original = schema_path.read_text(encoding="utf-8")
        for name, replacement in (("missing schema", None), ("malformed schema", "{")):
            with self.subTest(name=name):
                if replacement is None:
                    schema_path.unlink()
                else:
                    schema_path.write_text(replacement, encoding="utf-8")
                data, findings = self.load()
                self.assertEqual(data, {})
                self.assertEqual([finding.code for finding in findings], ["registry-validator"])
                schema_path.write_text(original, encoding="utf-8")

        data, findings = self.load()
        self.assertEqual(findings, [])
        failures = (
            ("missing tool", FileNotFoundError("missing validator")),
            ("malformed output", subprocess.CompletedProcess([], 2, "not-json", "broken")),
            (
                "operational failure",
                subprocess.CompletedProcess([], 2, '{"status":"fail","errors":[]}', "broken"),
            ),
        )
        for name, outcome in failures:
            with self.subTest(name=name), patch.object(h.subprocess, "run") as run:
                if isinstance(outcome, BaseException):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                errors = self.validate(data)
                self.assertEqual([finding.code for finding in errors], ["registry-validator"])


class HarnessLintCliContractTest(unittest.TestCase):
    def test_json_envelope_and_exit_codes(self) -> None:
        cases = (
            ([], 0, {"error": 0, "warn": 0, "info": 0}),
            ([h.Finding("WARN", "advisory", "harness/x", "message")], 0,
             {"error": 0, "warn": 1, "info": 0}),
            ([h.Finding("ERROR", "broken", "harness/x", "message")], 1,
             {"error": 1, "warn": 0, "info": 0}),
        )
        for findings, expected_exit, counts in cases:
            with self.subTest(expected_exit=expected_exit), patch.object(h, "run_lints", return_value=findings):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exit_code = h.main(["--json"])
                payload = json.loads(output.getvalue())
                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(payload["counts"], counts)
                self.assertEqual(
                    [set(finding) for finding in payload["findings"]],
                    [{"severity", "code", "where", "message"} for _ in findings],
                )

    def test_unknown_argument_uses_argparse_exit_two(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            h.main(["--unknown"])
        self.assertEqual(raised.exception.code, 2)

    def test_schema_failure_gates_domain_checks(self) -> None:
        failure = h.Finding("ERROR", "registry-schema", "harness/models.toml:$", "bad")
        with patch.object(h, "load_harness_config", return_value=({}, [failure])), \
             patch.object(h, "load_canonical_agents", side_effect=AssertionError("domain checks ran")):
            self.assertEqual(h.run_lints(), [failure])


class ComponentTaxonomyGuardTest(unittest.TestCase):
    def test_public_and_private_kinds_are_rooted(self) -> None:
        with _lint_root() as root, tempfile.TemporaryDirectory(prefix="atelier-components-") as tmp:
            vault = Path(tmp)
            _write(root, "skills/sample/SKILL.md", "---\nname: sample\n---\n")
            _write(root, "agents/sample.md", "---\nname: sample\n---\n")
            private_skill = vault / "_tools/skills/private-sample"
            private_skill.mkdir(parents=True)
            (private_skill / "SKILL.md").write_text("---\nname: private-sample\n---\n")
            (vault / "_tools/agents").mkdir(parents=True)
            private_routines = vault / "_tools/routines"
            private_routines.mkdir(parents=True)
            registry = private_routines / "registry.toml"
            registry.write_text(
                'version = 1\n[[routine]]\nname = "sample"\nrunner = "model"\n', encoding="utf-8"
            )
            private_tool = vault / "_tools/tools/sample"
            private_tool.mkdir(parents=True)
            (private_tool / "README.md").write_text("tool\n", encoding="utf-8")
            arguments = (
                {"sample": "skills/sample/SKILL.md"},
                {"sample": {"path": "agents/sample.md"}},
                {"routine": [{"name": "public-sample"}]},
                {f"private_{kind}": f"_tools/{kind}" for kind in ("skills", "agents", "routines", "tools")},
            )
            with patch.dict(os.environ, {"OV": str(vault)}):
                self.assertEqual(h.check_component_taxonomy(*arguments), [])
                _write(root, "skills/unregistered/SKILL.md", "---\nname: unregistered\n---\n")
                self.assertIn("component-skill-unregistered", [f.code for f in h.check_component_taxonomy(*arguments)])


class FlatTierGlobGuardTest(unittest.TestCase):
    def test_top_level_exemptions_do_not_skip_same_named_nested_modules(self) -> None:
        with _lint_root() as root:
            for prefix in ("scripts", "scripts/runtime"):
                _write(root, f"{prefix}/_paths.py", 'p = Path("zk/inbox")\n')
                _write(root, f"{prefix}/fission.py", 'x = tier("reflections").glob("*.md")\n')
            paths = [f.where for f in h.check_scripts_zk_paths() + h._flat_tier_glob_findings()]
        self.assertEqual(paths, ["scripts/runtime/_paths.py:1", "scripts/runtime/fission.py:1"])

    def test_nested_packages_are_checked_without_reading_private_oneoffs(self) -> None:
        with _lint_root() as root:
            body = 'p = Path("zk/inbox")\nx = tier("reflections").glob("*.md")\n'
            _write(root, "scripts/runtime/nested.py", body)
            _write(root, "scripts/oneoff/private.py", body)
            alias = root / "scripts/runtime/link.py"
            alias.symlink_to(root / "scripts/oneoff/private.py")
            paths = [f.where for f in h.check_scripts_zk_paths() + h._flat_tier_glob_findings()]
        self.assertEqual(paths, ["scripts/runtime/nested.py:1", "scripts/runtime/nested.py:2"])

    def test_python_alias_and_shell_shapes_are_rejected(self) -> None:
        with _lint_root() as root:
            _write(root, "scripts/victim.py",
                   'weeklies = sorted(weekly_dir.glob("*-weekly.md"))\n'
                   'for f in sorted(REFLECTIONS_DIR.glob("*.md")): pass\n'
                   'x = tier("reflections").glob("*.md")\n'
                   'refl = tier("reflections")\n'
                   'reviews = sorted(refl.glob("*-growth-review.md"))\n'
                   'safe = WIKI_DIR.rglob("*.md")\n')
            _write(root, "protocols/victim.md",
                   'ls "$OV"/reflections/*-review.md\n'
                   'find "$OV/reflections" -name "*-weekly.md"\n')
            findings = h.check_flat_tier_globs()
        self.assertEqual(
            [(f.code, f.where) for f in findings],
            [("flat-tier-glob", where) for where in (
                "scripts/victim.py:1",
                "scripts/victim.py:2",
                "scripts/victim.py:3",
                "scripts/victim.py:5",
                "protocols/victim.md:1",
            )],
        )


class IntentAgentsInProcedureGuardTest(unittest.TestCase):
    def test_declared_agent_missing_from_procedure_is_an_error(self) -> None:
        with _lint_root() as root:
            _write(root, "procs/good.md", "Dispatch the **Thinker** then the Scribe.\n")
            _write(root, "procs/bad.md", "Runs retrieval inline; no dispatch.\n")
            findings = h.check_intents_agents_in_procedure({
                "ok": {"agents": ["thinker", "scribe"], "procedure": "procs/good.md"},
                "stale": {"agents": ["researcher"], "procedure": "procs/bad.md"},
                "none": {"agents": [], "procedure": "procs/bad.md"},
            })
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].code, "intent-agent-not-in-procedure")
        self.assertIn("intents.stale", findings[0].where)
        self.assertEqual(findings[0].severity, "ERROR")


class DecisionRecordPathGuardTest(unittest.TestCase):
    def test_dated_reflection_output_is_rejected(self) -> None:
        with _lint_root() as root:
            _write(root, "skills/decision/SKILL.md",
                   "<paths.reflections>/YYYY-MM-DD-decision-<slugified-topic>.md\n")
            _write(root, "protocols/session-continuity.md", "<paths.gtd>/decisions/*.md\n")
            findings = h.check_decision_record_contract()
        self.assertIn(("decision-record-path", "skills/decision/SKILL.md"),
                      [(f.code, f.where) for f in findings])


class HotPathCeilingGuardTest(unittest.TestCase):
    def test_oversized_hot_path_file_is_flagged(self) -> None:
        with _lint_root() as root:
            big = _write(root, "big.md", "x" * 2048)
            self.assertIn("hot-path-ceiling", [f.code for f in h.check_hot_path_ceilings({str(big): 1024})])

    def test_real_hot_path_files_are_under_ceiling(self) -> None:
        self.assertEqual(h.check_hot_path_ceilings(), [])


class LegacyFramingGuardTest(unittest.TestCase):
    """The present-tense rule shipped without an executor; these two phrasings
    survived in the repo until a hand audit found them."""

    REGRESSIONS = (
        "`/lint` used to flag this and was wrong about it.",
        "It is now a draft the system produces on its own; the human",
    )

    def test_system_biography_phrasings_are_flagged(self) -> None:
        for body in self.REGRESSIONS:
            with self.subTest(body=body), _lint_root() as root:
                _write(root, "doc.md", body)
                findings = h.check_legacy_framing(roots=[str(root)])
                self.assertEqual(["legacy-framing"], [f.code for f in findings])
                self.assertIn("doc.md:1", findings[0].where)

    def test_rule_owning_docs_may_quote_the_phrasings(self) -> None:
        with _lint_root() as root:
            _write(root, "protocols/repo-conventions.md", '"earlier versions used to be X"')
            self.assertEqual(h.check_legacy_framing(roots=["protocols"]), [])

    def test_live_uses_of_the_broad_detect_list_are_not_flagged(self) -> None:
        """The original prose detect list was written for a human reading a
        diff. Gating on it verbatim would fire on these."""
        with _lint_root() as root:
            _write(root, "doc.md", "\n".join((
                "| Catalog rating (legacy field) = 3 | +3 |",
                "| **Decay** | Previously active theme going quiet |",
                "zettelm no longer holds the file",
                'It is a "previously on..." anchor, not a summary.',
                "Refuse to frame later iterations as better than earlier versions",
                "For v1:",
            )))
            self.assertEqual(h.check_legacy_framing(roots=[str(root)]), [])

    def test_repo_prose_surface_is_clean(self) -> None:
        self.assertEqual(h.check_legacy_framing(), [])


class SourceBudgetGuardTest(unittest.TestCase):
    def test_current_tracked_and_untracked_public_text_is_counted(self) -> None:
        with _lint_root() as root:
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            _write(root, "source.py", "staged\n")
            deleted = _write(root, "gone.py", "deleted\n")
            subprocess.run(["git", "-C", str(root), "add", "source.py", "gone.py"], check=True)
            deleted.unlink()
            for name, body in {
                ".gitignore": "ignored/\n", "source.py": "one\ntwo\n",
                "new.toml": "key = 1\n", "tests/test_unit.py": "test\n",
                "protocols/rule.md": "note\n", "ignored/private.py": "not counted\n",
                "profile/private.py": "not counted\n", "uv.lock": "not counted\n",
                ".agents/skills/generated/SKILL.md": "not counted\n", "asset.bin": "\0",
            }.items():
                _write(root, name, body)
            (root / "alias.py").symlink_to(root / "ignored/private.py")
            footprint, findings = h.source_footprint()
        self.assertEqual(findings, [])
        self.assertEqual(footprint["total"]["lines"], 6)
        self.assertEqual({k: v["lines"] for k, v in footprint["by_kind"].items()},
                         {"implementation/config": 4, "tests": 1, "prose": 1})

    def test_total_and_file_boundaries_fail_the_footprint_cli(self) -> None:
        with _lint_root() as root, patch.object(h, "git_paths", return_value=["a.py", "b.toml"]):
            for name in ("a.py", "b.toml"):
                _write(root, name, "one\ntwo\n")
            for total_limit, file_limit, locations in (
                (4, 2, []), (3, 2, ["implementation/config"]), (4, 1, ["a.py", "b.toml"]),
            ):
                with self.subTest(total=total_limit, file=file_limit), \
                     patch.object(h, "SOURCE_LINE_CEILING", total_limit), \
                     patch.object(h, "SOURCE_FILE_LINE_CEILING", file_limit), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(h.main(["--footprint"]), int(bool(locations)))
                report = json.loads(output.getvalue())
                self.assertEqual([f["where"] for f in report["findings"]], locations)
                self.assertTrue(all(f["severity"] == "ERROR" for f in report["findings"]))
                self.assertEqual(report["budgets"]["implementation/config_lines"], total_limit)

    def test_inventory_failure_is_not_a_zero_sized_success(self) -> None:
        with patch.object(h, "git_paths", side_effect=RuntimeError("inventory unavailable")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(h.main(["--footprint"]), 1)
        report = json.loads(output.getvalue())
        self.assertNotIn("total", report)
        self.assertEqual(report["findings"][0]["code"], "source-inventory")

    def test_normal_lint_includes_source_budget_findings(self) -> None:
        failure = h.Finding("ERROR", "source-budget", "implementation/config", "over ceiling")
        with patch.object(h, "source_footprint", return_value=({}, [failure])):
            self.assertIn(failure, h.run_lints())


class ProseBudgetGuardTest(unittest.TestCase):
    def test_canonical_skill_procedures_are_inside_the_budget(self) -> None:
        self.assertIn("skills", h.PROSE_BUDGET_ROOTS)

    def test_a_new_doc_counts_as_note_facing_not_plumbing(self) -> None:
        """The frozen half is an explicit list, so an unlisted file must land
        in the half that still has headroom, never in the one that cannot grow."""
        with _lint_root() as root:
            _write(root, "protocols/brand-new.md", "x" * 64)
            with patch.object(h, "PROSE_PLUMBING_CEILING", 0), patch.object(h, "PROSE_NOTES_WARN", 8192), \
                 patch.object(h, "PROSE_NOTES_ERROR", 16384):
                self.assertEqual([], h.check_prose_budget(roots=("protocols",)))

    def test_repo_is_under_budget_and_the_budget_still_binds(self) -> None:
        self.assertEqual(h.check_prose_budget(), [])
        plumbing = notes = 0
        for root in h.PROSE_BUDGET_ROOTS:
            for path in (h.ROOT / root).rglob("*.md"):
                rel = str(path.relative_to(h.ROOT))
                if rel in h.PROSE_PLUMBING:
                    plumbing += path.stat().st_size
                else:
                    notes += path.stat().st_size
        # Plumbing is frozen: the ceiling stays within 2% of the real surface, so
        # it still binds without forcing a re-baseline on every byte trimmed.
        self.assertLess(h.PROSE_PLUMBING_CEILING, plumbing * 1.02)
        # Notes keep headroom, but not so much that the next growth pass sails through.
        self.assertLess(h.PROSE_NOTES_WARN, notes * 1.25)
        self.assertLess(h.PROSE_NOTES_WARN, h.PROSE_NOTES_ERROR)

    def test_boundaries(self) -> None:
        with _lint_root() as root:
            _write(root, "protocols/big.md", "x" * 4096)
            with patch.object(h, "PROSE_PLUMBING_CEILING", 8192), \
                 patch.object(h, "PROSE_NOTES_WARN", 1024), patch.object(h, "PROSE_NOTES_ERROR", 8192):
                self.assertEqual(["WARN"], [f.severity for f in h.check_prose_budget(roots=("protocols",))])
            with patch.object(h, "PROSE_PLUMBING_CEILING", 8192), \
                 patch.object(h, "PROSE_NOTES_WARN", 512), patch.object(h, "PROSE_NOTES_ERROR", 1024):
                self.assertEqual(["ERROR"], [f.severity for f in h.check_prose_budget(roots=("protocols",))])
            with patch.object(h, "PROSE_PLUMBING_CEILING", 8192), \
                 patch.object(h, "PROSE_NOTES_WARN", 8192), patch.object(h, "PROSE_NOTES_ERROR", 16384):
                self.assertEqual([], h.check_prose_budget(roots=("protocols",)))

    def test_a_listed_plumbing_file_cannot_grow_past_its_frozen_ceiling(self) -> None:
        with _lint_root() as root:
            _write(root, "protocols/README.md", "x" * 4096)
            with patch.object(h, "PROSE_PLUMBING_CEILING", 1024), \
                 patch.object(h, "PROSE_NOTES_WARN", 8192), patch.object(h, "PROSE_NOTES_ERROR", 16384):
                findings = h.check_prose_budget(roots=("protocols",))
            self.assertEqual(["ERROR"], [f.severity for f in findings])
            self.assertIn("plumbing", findings[0].message)


class AnnotationRemovalGuardTest(unittest.TestCase):
    def test_runtime_rendering_routing_and_context_ignore_annotations(self) -> None:
        from render_runtime_edges import render_codex
        from intent_coverage import catalog_rows
        import context_bundle as cb

        agents, intents, skills, models = (
            tomllib.loads((REPO_ROOT / 'harness' / f'{name}.toml').read_text())
            for name in ('agents', 'intents', 'skills', 'models')
        )
        metadata = {'pattern': 'obsolete', 'used_by': ['obsolete'], 'kinds': ['obsolete'],
                    'dispatch_rationale': ['obsolete']}
        annotated_agents, annotated_intents = copy.deepcopy(agents), copy.deepcopy(intents)
        for row in annotated_agents['agents'].values():
            row.update(metadata)
        for row in annotated_intents['intents'].values():
            row.update(metadata)

        def routes(rows):
            with (patch.object(cb, 'load_intents', return_value=rows),
                  patch.object(cb, 'DEFAULT_INTENTS_PATH', Path('/fixture/intents.toml'))):
                return [cb.resolve_route(intent_arg=name) for name in rows]

        self.assertTrue(all(not set(row).intersection(metadata) for row in agents['agents'].values()))
        self.assertTrue(all('pattern' not in row for row in intents['intents'].values()))
        self.assertEqual(render_codex(agents, skills, models), render_codex(annotated_agents, skills, models))
        self.assertEqual(catalog_rows(intents['intents']), catalog_rows(annotated_intents['intents']))
        self.assertEqual(routes(intents['intents']), routes(annotated_intents['intents']))

    def test_metadata_is_optional_but_live_registry_errors_still_fail(self) -> None:
        with _lint_root() as root:
            source = 'agents/sample.md'
            _write(root, source, 'Sample role.')
            row = {'source': source, 'voices': {'native': 'known'}, 'status': 'canonical',
                   'description': 'Sample role.'}

            def check_agent(entry):
                return [f.code for f in h.check_agent_registry(
                    {'sample': {'path': source}}, {'known': {}}, {'sample': entry}
                )]

            intent = {'description': 'Sample request.', 'agents': ['sample']}
            metadata = {'pattern': None, 'used_by': 42, 'kinds': [], 'dispatch_rationale': False}
            self.assertEqual(check_agent(row), [])
            self.assertEqual(check_agent({**row, **metadata}), [])
            self.assertIn('agents-voices-unknown-model', check_agent({**row, 'voices': {'native': 'unknown'}}))
            self.assertEqual(h.check_intents_registry({'sample': {**intent, **metadata}}, {'sample': {}}, {'sample': {}}), [])
            errors = [f.code for f in h.check_intents_registry({'sample': intent}, {}, {})]
            self.assertIn('intents-agent-missing-claude', errors)
            self.assertIn('intents-agent-missing-harness', errors)


class WorkflowContractOwnerGuardTest(unittest.TestCase):
    def test_missing_migrated_boundaries_fail(self) -> None:
        paths = ('protocols/orchestrator.md', 'skills/read/SKILL.md', 'protocols/intent-capture.md')

        def assert_broken():
            self.assertEqual([f.code for f in h.check_workflow_contract_owners()], ['workflow-contract-owner'])

        with _lint_root() as root:
            for name in paths:
                _write(root, name, (REPO_ROOT / name).read_text())
            self.assertEqual(h.check_workflow_contract_owners(), [])
            mutations = (
                ('protocols/orchestrator.md', '`harness/intents.toml` selects the procedure'),
                ('skills/read/SKILL.md', 'Start with one **Reader**, or one **Scholar**'),
                ('protocols/intent-capture.md', '`/dine` Intent C'),
                ('protocols/intent-capture.md', 'ask the user once for a default GTD filename'),
                ('protocols/intent-capture.md', 'Do not pass an empty `target_file`'),
                ('protocols/intent-capture.md', 'confirm with the user before dispatch'),
                ('protocols/intent-capture.md', 'rather than retrying with a guess'),
            )
            for name, needle in mutations:
                with self.subTest(boundary=needle):
                    target = root / name
                    original = target.read_text()
                    self.assertIn(needle, original)
                    target.write_text(original.replace(needle, 'REMOVED'))
                    try:
                        assert_broken()
                    finally:
                        target.write_text(original)


class SharedReadingContractGuardTest(unittest.TestCase):
    def test_missing_shared_source_or_reintroduced_duplicate_body_fails(self) -> None:
        body = '## Shared reading contract\n## Reading Lenses\nOne shared body.\n## How You Work\nRead.\n## Output Format\nBrief.\n'
        adapter = "Read `agents/reader.md`; preserve this role's own frontmatter.\n"
        with _lint_root() as root:
            reader = _write(root, 'agents/reader.md', '---\nname: reader\n---\n' + body)
            for name, value in (('clean', adapter), ('missing_pointer', 'Preserve own frontmatter.'),
                                ('missing_identity', 'Read agents/reader.md.'), ('duplicate', adapter + body)):
                with self.subTest(case=name):
                    scholar = _write(root, 'agents/scholar.md', '---\nname: scholar\n---\n' + value)
                    expected = [] if name == 'clean' else ['reader-scholar-sync']
                    self.assertEqual([f.code for f in h.check_reader_scholar_sync()], expected)
            scholar.write_text(adapter)
            reader.write_text('Empty shared source.')
            self.assertEqual([f.code for f in h.check_reader_scholar_sync()], ['reader-scholar-sync'])
            reader.unlink()
            self.assertEqual([f.code for f in h.check_reader_scholar_sync()], ['reader-scholar-sync'])


if __name__ == "__main__":
    unittest.main()
