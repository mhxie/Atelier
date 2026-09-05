"""Contract guards for the Reviewer's system modes.

Why this exists: on 2026-09-03 a System Diff Review dispatched with a plain
"do not modify" instruction reverted two scripts under review and began
re-applying the diff hunk by hunk; the working tree had to be restored from
backups and both reviews re-run. reviewer.md granted Bash and said nothing
about writes. The rule now lives in the agent definition and a PreToolUse
guard denies the git moves mechanically; these tests keep both there and
keep the rest of the system-mode contract consistent with itself.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / ".claude" / "agents" / "reviewer.md"
CATALOG = ROOT / "protocols" / "antipatterns.md"
sys.path.insert(0, str(ROOT / "scripts"))

import readonly_bash_guard  # noqa: E402

RULE = "Both system modes are read-only"
PROSE_VERBS = ("stash", "checkout", "restore", "apply", "reset", "edit")
SCORED_DIMENSIONS = ["Contract integrity", "Wiring correctness", "Bug absence", "Claim fidelity"]


def _spec() -> tuple[str, str]:
    text = SPEC.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, "reviewer.md has no frontmatter"
    return match.group(1), text[match.end() :]


class ReviewerReadOnlyContractTest(unittest.TestCase):
    def test_system_modes_declare_read_only_and_the_baseline_commands(self) -> None:
        _, body = _spec()
        diff_mode = body.index("## System Diff Review Mode")
        rule = body.index(RULE, diff_mode)
        self.assertLess(rule - diff_mode, 400)
        window = body[rule : rule + 400]
        for verb in PROSE_VERBS:
            self.assertIn(verb, window)
        self.assertIn("git show HEAD:<path>", window)
        self.assertIn("git show <base>:<path>", window)
        self.assertIn("has no baseline", window)

    def test_rule_is_stated_once_and_holistic_mode_follows_it(self) -> None:
        _, body = _spec()
        self.assertEqual(body.count(RULE), 1)
        self.assertGreater(body.index("## System Holistic Review Mode"), body.index(RULE))

    def test_git_guard_is_wired_for_bash_and_covers_the_prose_verbs(self) -> None:
        frontmatter, _ = _spec()
        self.assertIn("PreToolUse", frontmatter)
        self.assertIn("matcher: Bash", frontmatter)
        self.assertIn("scripts/readonly_bash_guard.py", frontmatter)
        self.assertTrue((ROOT / "scripts" / "readonly_bash_guard.py").is_file())
        git_verbs = set(PROSE_VERBS) - {"edit"}
        self.assertTrue(git_verbs <= readonly_bash_guard.BLOCKED_GIT, git_verbs - readonly_bash_guard.BLOCKED_GIT)
        # "edit" is enforced as a write into the repository
        self.assertTrue(readonly_bash_guard.blocked_subcommands("sed -i 's/a/b/' scripts/x.py", repo="/repo"))
        self.assertTrue(readonly_bash_guard.blocked_subcommands("cat > scripts/x.py <<'EOF'\nx\nEOF", repo="/repo"))

    def test_guard_denies_when_it_cannot_run(self) -> None:
        """A hook that exits non-zero is a non-blocking error, and the
        command runs: the hook command must deny when python3 cannot run
        the guard, and the guard must parse on the oldest python3 a machine
        resolves (macOS ships 3.9)."""
        frontmatter, _ = _spec()
        self.assertIn("|| printf", frontmatter)
        self.assertIn('"permissionDecision":"deny"', frontmatter)
        ast.parse((ROOT / "scripts" / "readonly_bash_guard.py").read_text(), feature_version=(3, 9))
        folded = re.search(r"(?m)^\s*command: >-\n((?:^ {12}.*\n)+)", frontmatter)
        self.assertIsNotNone(folded, "the hook command is a folded block scalar")
        command = " ".join(line.strip() for line in folded.group(1).splitlines())
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git log -1"}})

        def hook(project: str, path: str) -> dict:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                input=payload,
                capture_output=True,
                text=True,
                timeout=30,
                env={"PATH": path, "CLAUDE_PROJECT_DIR": project, "HOME": os.environ.get("HOME", "/")},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)["hookSpecificOutput"]

        # no python3 on PATH: the shell fallback denies
        self.assertEqual(hook(str(ROOT), "/nonexistent")["permissionDecision"], "deny")
        # a guard python3 cannot import: the fallback denies too
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / "scripts" / "readonly_bash_guard.py").write_text("def (\n")
            self.assertEqual(hook(tmp, os.environ.get("PATH", "/usr/bin:/bin"))["permissionDecision"], "deny")


class AgentHookLintTest(unittest.TestCase):
    """harness_lint covers agent-frontmatter hooks: a missing script or an
    unsupported event is an ERROR, and the live tree is clean."""

    def test_live_tree_is_clean(self) -> None:
        import harness_lint

        self.assertEqual(harness_lint.check_agent_hooks(), [])

    def test_missing_script_and_bad_event_are_errors(self) -> None:
        import tempfile

        import harness_lint

        with tempfile.TemporaryDirectory() as tmp:
            agent = Path(tmp) / "probe.md"
            agent.write_text(
                "---\nname: probe\nhooks:\n  SessionStart:\n    - matcher: Bash\n      hooks:\n"
                "        - type: command\n          command: python3 scripts/does-not-exist.py\n---\n\nbody\n",
                encoding="utf-8",
            )
            codes = sorted(f.code for f in harness_lint.check_agent_hooks(Path(tmp)))
        self.assertEqual(codes, ["agent-hook-event", "agent-hook-script"])

    def test_event_indentation_is_read_from_the_block(self) -> None:
        import harness_lint

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "x.md").write_text(
                "---\nname: x\nhooks:\n    BadEvent:\n        - matcher: Bash\n          hooks:\n"
                "            - type: command\n              command: python3 scripts/readonly_bash_guard.py\n---\n\nbody\n"
            )
            codes = sorted(f.code for f in harness_lint.check_agent_hooks(Path(tmp)))
        self.assertEqual(codes, ["agent-hook-event"])

    def test_block_scalar_commands_are_checked(self) -> None:
        import tempfile

        import harness_lint

        with tempfile.TemporaryDirectory() as tmp:
            agent = Path(tmp) / "probe.md"
            agent.write_text(
                "---\nname: probe\nhooks:\n  PreToolUse:\n    - matcher: Bash\n      hooks:\n"
                "        - type: command\n          command: |\n            python3 scripts/does-not-exist.py\n---\n\nbody\n",
                encoding="utf-8",
            )
            codes = sorted(f.code for f in harness_lint.check_agent_hooks(Path(tmp)))
        self.assertEqual(codes, ["agent-hook-script"])

    def test_block_scalar_commands_span_all_their_lines(self) -> None:
        import tempfile

        import harness_lint

        with tempfile.TemporaryDirectory() as tmp:
            agent = Path(tmp) / "probe.md"
            agent.write_text(
                "---\nname: probe\nhooks:\n  PreToolUse:\n    - matcher: Bash\n      hooks:\n"
                "        - type: command\n          command: |\n            python3\n            scripts/does-not-exist.py\n---\n\nbody\n",
                encoding="utf-8",
            )
            codes = sorted(f.code for f in harness_lint.check_agent_hooks(Path(tmp)))
        self.assertEqual(codes, ["agent-hook-script"])

    def test_hooks_block_ends_at_the_next_top_level_key(self) -> None:
        import tempfile

        import harness_lint

        with tempfile.TemporaryDirectory() as tmp:
            agent = Path(tmp) / "probe.md"
            agent.write_text(
                "---\nname: probe\nhooks:\n  PreToolUse:\n    - matcher: Bash\n      hooks:\n"
                "        - type: command\n          command: python3 scripts/readonly_bash_guard.py\n"
                "extra:\n  Nested:\n    command: python3 scripts/does-not-exist.py\n---\n\nbody\n",
                encoding="utf-8",
            )
            self.assertEqual(harness_lint.check_agent_hooks(Path(tmp)), [])


class SystemModeScoringContractTest(unittest.TestCase):
    def test_diff_table_scores_exactly_the_four_dimensions(self) -> None:
        _, body = _spec()
        section = body[body.index("## System Diff Review Mode") : body.index("## System Holistic Review Mode")]
        rows = re.findall(r"(?m)^\| ([A-Z][a-z]+ [a-z]+) \| .+ \|$", section)
        self.assertEqual(rows, SCORED_DIMENSIONS)
        self.assertIn(
            "equal-weighted average of the 4 system dimensions (" + ", ".join(SCORED_DIMENSIONS) + ")",
            body,
        )

    def test_antipattern_template_does_not_enumerate_the_catalog(self) -> None:
        _, body = _spec()
        entries = re.findall(r"(?m)^### (\d+)\. (.+?)\s*$", CATALOG.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(entries), 9)
        for number, title in entries:
            self.assertNotIn(f"{number}. {title}:", body)
        self.assertIn("one line for each `protocols/antipatterns.md` entry", body)


if __name__ == "__main__":
    unittest.main()
