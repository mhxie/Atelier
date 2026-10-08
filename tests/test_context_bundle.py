"""Outcome tests for route selection and the Repomix boundary."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import context_bundle as cb  # noqa: E402
import _paths


class MarkdownSectionTest(unittest.TestCase):
    def test_headings_inside_fences_are_not_sections(self) -> None:
        sections = cb.markdown_sections(
            "## Continuity\ncarry\n\n```md\n## Not A Section\n```\n\n"
            "## Anomalies\nnotice\n"
        )
        self.assertEqual(list(sections), ["continuity", "anomalies"])
        self.assertIn("## Not A Section", sections["continuity"][1])


class ContextSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="atelier-context-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(cb, "ROOT", self.root))
        self.enterContext(patch.object(_paths, "_registry", return_value={"sessions": "sessions"}))
        self.vault = self.root / "vault"
        (self.root / "profile").mkdir()
        for relative in ("sessions", "reflections", "research"):
            (self.vault / relative).mkdir(parents=True, exist_ok=True)
        (self.root / "profile" / "identity.md").write_text(
            "Last built: 2099-01-03\n\n## Identity\nstable\n", encoding="utf-8"
        )
        (self.root / "profile" / "directions.md").write_text(
            "Last built: 2098-12-20\n\n## Current era\nactive\n", encoding="utf-8"
        )
        (self.vault / "sessions" / "2099-01-03-reflection.md").write_text(
            "## Continuity\ncarry\n\n## Anomalies\nnotice\n\n"
            "## Full Text\ndo not stage\n",
            encoding="utf-8",
        )
        (self.vault / "reflections" / "2099-01-02-reflection.md").write_text(
            "## Theme\nreflection body must not stage\n", encoding="utf-8"
        )
        (self.vault / "research" / "source.md").write_text(
            "## Alpha\nalpha only\n\n## Beta\nbeta only\n", encoding="utf-8"
        )

    def selection(
        self, intent: str = "weekly", *sources: str
    ) -> tuple[dict[str, str], str, list[str], int]:
        return cb.select_context(
            vault=self.vault,
            intent_arg=intent,
            source_specs=list(sources),
            effective_date=date(2099, 1, 3),
        )

    def test_route_profiles_latest_continuity_and_explicit_section_are_exact(self) -> None:
        selected, header, warnings, budget = self.selection(
            "weekly", "research/source.md#Beta"
        )
        self.assertEqual(
            set(selected),
            {
                "profile/identity.md",
                "profile/directions.md",
                "sessions/2099-01-03-reflection.md",
                "research/source.md",
            },
        )
        session = selected["sessions/2099-01-03-reflection.md"]
        source = selected["research/source.md"]
        self.assertIn("carry", session)
        self.assertIn("notice", session)
        self.assertNotIn("do not stage", session)
        self.assertEqual(source, "## Beta\n\nbeta only\n")
        self.assertNotIn("reflection body must not stage", header)
        self.assertIn("profile/identity.md: current", header)
        self.assertIn("profile/directions.md: stale", header)
        self.assertTrue(any("directions.md is stale" in warning for warning in warnings))
        self.assertEqual(budget, 8192)

    def test_reading_uses_latest_nonempty_capsule_not_only_latest_reading_log(self) -> None:
        (self.vault / "sessions" / "2099-01-02-reading.md").write_text(
            "## Notes\nnewer log without capsule\n", encoding="utf-8"
        )
        (self.vault / "sessions" / "2099-01-01-reading.md").write_text(
            "## Reading Capsule\ndiscussion-open\n", encoding="utf-8"
        )
        selected, _, _, _ = self.selection("reading")
        self.assertIn("sessions/2099-01-01-reading.md", selected)
        self.assertIn(
            "discussion-open",
            selected["sessions/2099-01-01-reading.md"],
        )

    def test_reflection_branches_preserve_distinct_profile_scopes(self) -> None:
        for intent, needs_directions in (("energy-audit", True), ("explore", False)):
            with self.subTest(intent=intent):
                selected, _, warnings, budget = self.selection(intent)
                expected = {"profile/identity.md", "sessions/2099-01-03-reflection.md"}
                if needs_directions:
                    expected.add("profile/directions.md")
                self.assertEqual(set(selected), expected)
                self.assertEqual(bool(warnings), needs_directions)
                self.assertEqual(budget, 8192)

    def test_missing_required_profile_fails_toward_introspection(self) -> None:
        (self.root / "profile" / "directions.md").unlink()
        with self.assertRaisesRegex(cb.BundleError, r"required profile .*introspect"):
            self.selection("weekly")

    def test_invalid_profile_date_warns_and_configured_sessions_are_selected(self) -> None:
        (self.root / "profile/identity.md").write_text("Last built: 2099-02-30\n")
        sessions = self.vault / "conversation-logs"
        sessions.mkdir()
        (sessions / "2099-01-03-review.md").write_text("## Continuity\nconfigured log\n")
        with patch.object(_paths, "_registry", return_value={"sessions": "conversation-logs"}):
            selected, _, warnings, _ = self.selection()
        self.assertIn("conversation-logs/2099-01-03-review.md", selected)
        self.assertNotIn("sessions/2099-01-03-reflection.md", selected)
        self.assertTrue(any("identity.md has no valid Last built date" in w for w in warnings))

    def test_explicit_source_cannot_escape_vault(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        with self.assertRaisesRegex(cb.BundleError, "escapes the vault"):
            self.selection("weekly", str(outside))

    def test_explicit_vault_profile_cannot_replace_required_repository_profile(self) -> None:
        (self.vault / "profile").mkdir()
        (self.vault / "profile/identity.md").write_text("## Identity\nunrelated source\n")
        for source in ("profile/identity.md", "profile/identity.md#Identity"):
            with self.subTest(source=source), self.assertRaisesRegex(cb.BundleError, "conflicts with required profile"):
                self.selection("weekly", source)

    def test_same_canonical_profile_source_does_not_conflict(self) -> None:
        (self.vault / "profile").mkdir()
        (self.vault / "profile/identity.md").write_text("Last built: 2099-01-03\n## Identity\nshared source\n")
        (self.root / "profile/identity.md").unlink()
        (self.root / "profile/identity.md").symlink_to(self.vault / "profile/identity.md")
        selected, header, _, _ = self.selection("weekly", "profile/identity.md")
        self.assertIn("shared source", selected["profile/identity.md"])
        self.assertIn("profile/identity.md: current", header)

    def test_deprecated_private_overlay_budget_is_not_silently_defaulted(self) -> None:
        registry = self.root / "intents.toml"
        registry.write_text("[intents]\n", encoding="utf-8")
        registry.with_name("intents.local.toml").write_text(
            "[intents.gizmo]\ncontext_budget_bytes = 8192\n", encoding="utf-8"
        )
        merged = {"gizmo": {"profile_reads": [], "context_budget_tokens": 2048}}
        with (
            patch.object(cb, "DEFAULT_INTENTS_PATH", registry),
            patch.object(cb, "load_intents", return_value=merged),
            self.assertRaisesRegex(cb.BundleError, "o200k_base tokens"),
        ):
            cb.resolve_route("gizmo")


class RepomixBoundaryTest(unittest.TestCase):
    def test_controlled_config_has_no_processors_or_ambient_discovery(self) -> None:
        config = cb.repomix_config("header")
        self.assertNotIn("input", config)
        self.assertEqual(config["include"], ["**/*"])
        self.assertFalse(config["ignore"]["useGitignore"])
        self.assertFalse(config["ignore"]["useDotIgnore"])
        self.assertFalse(config["ignore"]["useDefaultPatterns"])
        self.assertTrue(config["security"]["enableSecurityCheck"])
        self.assertFalse(config["output"]["compress"])

    def test_process_environment_is_a_positive_allowlist(self) -> None:
        ambient = {
            "PATH": "/fixture/bin",
            "HOME": "/fixture/home",
            "TMPDIR": "/fixture/tmp",
            "LANG": "en_US.UTF-8",
            "NODE_OPTIONS": "--require=/attacker/hook.cjs",
            "NODE_PATH": "/attacker/modules",
            "REPOMIX_WORKER_PATH": "/attacker/worker.cjs",
            "REPOMIX_REMOTE_TRUST_CONFIG": "true",
            "XDG_CONFIG_HOME": "/attacker/config",
            "NO_COLOR": "0",
            "FORCE_COLOR": "1",
            "UNRELATED_SECRET": "must-not-cross-boundary",
        }
        with patch.dict(os.environ, ambient, clear=True):
            environment = cb.repomix_environment(Path("/controlled/config"))

        self.assertNotIn("PATH", environment)
        self.assertEqual(environment["HOME"], "/fixture/home")
        self.assertEqual(environment["TMPDIR"], "/fixture/tmp")
        self.assertEqual(environment["LANG"], "en_US.UTF-8")
        self.assertEqual(environment["XDG_CONFIG_HOME"], "/controlled/config")
        self.assertEqual(environment["NO_COLOR"], "1")
        self.assertEqual(environment["FORCE_COLOR"], "0")
        self.assertFalse(
            any(name.startswith(("NODE_", "REPOMIX_")) for name in environment)
        )
        self.assertNotIn("UNRELATED_SECRET", environment)

    def test_artifact_must_equal_the_staged_allowlist(self) -> None:
        complete = '<files><file path="profile/identity.md">identity</file></files>\n'
        cb.validate_artifact(complete, ["profile/identity.md"])
        with self.assertRaisesRegex(cb.BundleError, "missing"):
            cb.validate_artifact("<files></files>", ["profile/identity.md"])
        with self.assertRaisesRegex(cb.BundleError, "unexpected"):
            cb.validate_artifact(
                '<files><file path="profile/identity.md">identity</file>'
                '<file path="ambient.md">bad</file></files>',
                ["profile/identity.md"],
            )


if __name__ == "__main__":
    unittest.main()
