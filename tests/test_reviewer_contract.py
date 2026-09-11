"""Exercise the configured Reviewer hook; prose quality belongs to review/evals."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "agents/reviewer.md"

import harness_lint  # noqa: E402


class ReviewerHookTests(unittest.TestCase):
    def test_configured_hook_allows_reads_and_fails_closed(self):
        frontmatter = harness_lint.FRONTMATTER_RE.match(SPEC.read_text()).group(1)
        self.assertRegex(frontmatter, r"PreToolUse:\s*\n\s*- matcher: Bash\b")
        folded = re.search(r"(?m)^( *)command: >-\n((?:\1 +[^\n]*\n)+)", frontmatter)
        self.assertIsNotNone(folded)
        command = " ".join(line.strip() for line in folded.group(2).splitlines())
        guard = ROOT / "scripts/readonly_bash_guard.py"
        ast.parse(guard.read_text(), feature_version=(3, 9))

        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp)
            (broken / "scripts").mkdir()
            (broken / "scripts/readonly_bash_guard.py").write_text("def (\n")
            search_path = os.environ.get("PATH", "/usr/bin:/bin")
            cases = [
                ("read", ROOT, search_path, "git log -1", False),
                ("no interpreter", ROOT, "/nonexistent", "git log -1", True),
                ("broken guard", broken, search_path, "git log -1", True),
                *((verb, ROOT, search_path, f"git {verb}", True)
                  for verb in ("stash", "checkout", "restore", "apply", "reset")),
                ("edit", ROOT, search_path, "sed -i 's/a/b/' scripts/x.py", True),
                ("redirect", ROOT, search_path, "cat > scripts/x.py <<'EOF'\nx\nEOF", True),
            ]
            for name, project, path, payload, denied in cases:
                with self.subTest(name=name):
                    result = subprocess.run(
                        ["/bin/sh", "-c", command], capture_output=True, text=True, timeout=30,
                        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": payload}}),
                        env={"PATH": path, "CLAUDE_PROJECT_DIR": str(project)}, cwd=ROOT,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if denied:
                        output = json.loads(result.stdout)["hookSpecificOutput"]
                        self.assertEqual(output["permissionDecision"], "deny")
                    else:
                        self.assertEqual(result.stdout, "")


class AgentHookLintTest(unittest.TestCase):
    def test_hook_schema_and_command_boundaries(self):
        self.assertEqual(harness_lint.check_agent_hooks(), [])
        valid = "scripts/readonly_bash_guard.py"
        missing = "scripts/does-not-exist.py"
        cases = [
            ("missing script and event", "SessionStart", 2, f"python3 {missing}", "",
             ["agent-hook-event", "agent-hook-script"]),
            ("event indentation", "BadEvent", 4, f"python3 {valid}", "", ["agent-hook-event"]),
            ("literal command", "PreToolUse", 2, f"|\n            python3 {missing}", "", ["agent-hook-script"]),
            ("multiline command", "PreToolUse", 2, f"|\n            python3\n            {missing}", "",
             ["agent-hook-script"]),
            ("next top-level key", "PreToolUse", 2, f"python3 {valid}",
             f"extra:\n  Nested:\n    command: python3 {missing}\n", []),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name, event, indent, command, extra, expected in cases:
                with self.subTest(name=name):
                    (directory / "probe.md").write_text(
                        f"---\nname: probe\nhooks:\n{' ' * indent}{event}:\n"
                        f"{' ' * (indent + 2)}- matcher: Bash\n{' ' * (indent + 4)}hooks:\n"
                        f"{' ' * (indent + 6)}- type: command\n{' ' * (indent + 8)}command: {command}\n"
                        f"{extra}---\n\nbody\n"
                    )
                    self.assertEqual(sorted(f.code for f in harness_lint.check_agent_hooks(directory)), expected)


if __name__ == "__main__":
    unittest.main()
