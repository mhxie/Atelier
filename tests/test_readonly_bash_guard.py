"""Read-only Bash allowlist, denial, and hook serialization tests."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import readonly_bash_guard as guard  # noqa: E402

# real reviewer workflows: every one of these must survive the guard
ALLOWED = (
    "git status --short",
    "git diff HEAD -- scripts/x.py",
    "git show HEAD:scripts/x.py",
    "git log --oneline -5",
    "git ls-files --others --exclude-standard -z",
    'git -C "${CLAUDE_PROJECT_DIR:-.}" --no-pager diff --stat',
    "git --no-pager show HEAD:scripts/x.py",
    "git blame -L 10,20 scripts/x.py",
    "git rev-parse --abbrev-ref HEAD",
    "git branch",
    "git branch --show-current",
    "git branch -a --merged",
    "git tag -l",
    "git tag -l 'v*'",
    "git tag -n3",
    "git stash list",
    "git remote -v",
    "git config --get user.name",
    "git config --get-regexp remote",
    "git worktree list",
    "git reflog show",
    'rg -n "pattern" scripts',
    "grep -rn x .",
    # a quoted metacharacter is a character, not a shell operator
    'rg -n "def (foo|bar)" scripts',
    "rg 'x{2}' scripts",
    'grep -E "(a|b)" scripts/x.py',
    "rg 'foo$' scripts",
    'echo "a;b"',
    "cat scripts/x.py",
    "sed -n '12,40p' scripts/x.py",
    "sed -n '5p' scripts/x.py",
    "head -50 scripts/x.py | tail -20",
    "wc -l scripts/x.py",
    "ls -la scripts",
    "diff scripts/a.py scripts/b.py",
    "find scripts -name '*.py'",
    "command -v uv",
    "python3 -m unittest tests.test_cues",
    "python3 -m unittest discover -s tests -t .",
    'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/harness_smoke.py"',
    ".venv/bin/python scripts/harness_smoke.py",
    ".venv/bin/python3 -m unittest tests.test_x",
    "uv run python3 -m unittest tests.test_x",
    "uv run --offline --no-sync python -m unittest tests.test_x",
    "uv run python3 scripts/harness_smoke.py",
    "uv run python3 scripts/harness_lint.py --json",
    "uv run scripts/privacy_check.py --json",
    "uv run scripts/intent_coverage.py catalog",
    "uv run scripts/decay_scan.py --redundant --scope wip",
    'uv run scripts/semantic.py query "career" --top 5 --format json',
    "uvx --offline ruff check scripts/x.py",
    "uvx ruff check scripts tests",
    "ruff check scripts",
    "cat scripts/x.py | head -5 | wc -l",
    "python3 -m unittest tests.test_x 2>&1",
    "uv run python3 scripts/harness_smoke.py 2>/dev/null",
    "git log -1 </dev/null",
    "git status --short && git log -1",
    "git status --short ; git log -1",
    "git diff --stat || git status --short",
)

# adversarial: one case per rule the grammar states, and per verb it refuses
DENIED = (
    # rule 1, shell shapes the guard will not read
    "cat scripts/x.py > /tmp/out",
    "cat scripts/x.py >> /tmp/out",
    "cat < scripts/x.py",
    "git log 2>err.txt",
    "git log |& tee /tmp/log",
    "grep -c x <script.sh",
    "tr a-z A-Z < scripts/a.py > /tmp/out",
    "cat <<'EOF'\nx\nEOF",
    "wc -l <<<'git reset --hard'",
    "echo $(git reset --hard)",
    "echo `git reset --hard`",
    "git show $(git rev-parse HEAD):scripts/x.py",
    "(cd /tmp && rm x)",
    "{ git log -1; git status; }",
    "echo {a,b}",
    "git log -1 &",
    "git log ! -x",
    "git log \\\n -1",
    "cat 'a' > b",
    "rg 'x' | tee f",
    # quoting makes a character literal; it does not launder a substitution or an expansion
    'rg "$(id)" .',
    'rg "`id`" .',
    'rg "${HOME}" .',
    'rg "$HOME" .',
    # rule 4, expansions and environment prefixes
    "X=1 git status",
    "GIT_PAGER=cat git log -1",
    "git log $REV",
    'git diff "$ARGS"',
    'git show "$REV":scripts/x.py',
    "echo ${HOME/x/y}",
    "echo $HOME",
    "cat ${OV}/notes.md",
    # rule 5, the executable itself
    "/usr/bin/git status",
    "./scripts/harness_lint.py",
    "/usr/bin/python3 -m unittest tests.test_x",
    "python3.13 scripts/harness_lint.py --json",
    "git",
    # python forms
    "python3 -c 1",
    'python3 -c "print(1)"',
    "python3 -",
    "python3 -m pip install x",
    "python3 scripts/decisions.py",
    "python3 scripts/harness_lint.py --fix-used-by",
    "uv run scripts/privacy_check.py --rebuild-index",
    "python3 scripts/intent_coverage.py intent-log --input x",
    "uv run scripts/semantic.py index --if-stale",
    "python3 scripts/semantic.py index",
    # uv, uvx, ruff
    "uv lock",
    "uv sync",
    "uv run python3 -c 1",
    "uv run bash -c x",
    "uv run scripts/lint.py --json",
    "uv run --no-sync scripts/harness_lint.py --json",
    "uvx ruff check --fix .",
    "uvx ruff format scripts",
    "uvx --offline pytest",
    "ruff format scripts",
    "ruff check --fix scripts",
    # find and sed
    "find scripts -exec cat +",
    "find scripts -execdir cat +",
    "find scripts -delete",
    "find scripts -fprint /tmp/out",
    "sed -i 's/a/b/' scripts/x.py",
    "sed -n 'w /tmp/out' scripts/x.py",
    "sed -n 's/a/b/p' scripts/x.py",
    "sed -e '1p' scripts/x.py",
    "sed -n '1,$p' scripts/x.py",
    "sed -n '1,2p'",
    # git global options and subcommands
    "git -c core.pager=cat log -1",
    "git --git-dir /tmp/.git log -1",
    "git --exec-path=/tmp log -1",
    "git config user.name x",
    "git config --unset user.name",
    "git branch new-branch",
    "git branch -d tmp",
    "git stash",
    "git stash push -m x",
    "git tag v1",
    "git tag -a v1",
    "git remote add origin url",
    "git worktree add /tmp/w",
    "git reflog expire --all",
    "git commit -m x",
    "git push origin main",
    "git checkout main",
    "git restore scripts/x.py",
    "git reset --hard HEAD~1",
    "git rebase main",
    "git merge main",
    "git apply /tmp/p.diff",
    "git diff --output=/tmp/p",
    "git log -O /tmp/order",
    "git grep --open-files-in-pager x",
    "git diff --ext-diff",
    # a chain is only as allowed as its weakest segment
    "git log -1 && rm -rf /tmp/x",
    "git status ; git commit -m x",
    "cat scripts/x.py | rm -f /tmp/y",
    "git log -1 |",
    "; git log -1",
)

# the verbs the policy names as never allowed; none of them takes an argument that helps
OFF_ALLOWLIST = (
    "awk perl ruby node sh bash zsh eval exec source . xargs tee mktemp touch cp mv rm chmod chown "
    "ln mkdir rmdir tar zip unzip curl wget ssh scp rsync open vim vi nano emacs code timeout time "
    "nice env caffeinate sudo cd export codex claude gh brew npm npx pip pipx make just"
).split()


# a reader whose own flags write or exec: allowlisting the verb is not enough
WRITING_READER_FORMS = (
    "rg --pre rm pattern /etc/hosts",
    "rg --pre=/bin/sh pattern /etc/hosts",
    "rg --hostname-bin /bin/sh pattern /etc/hosts",
    "sort --o=/tmp/victim.txt /etc/hosts",
    "sort --out=/tmp/victim.txt /etc/hosts",
    "sort --o /tmp/victim.txt /etc/hosts",
    "sort --co=/bin/sh /etc/hosts",
    "sort --t=/tmp /etc/hosts",
    "file -C -m /tmp/m",
    "file --compile -m /tmp/m",
    "sort -o /tmp/victim.txt /etc/hosts",
    "sort --output=/tmp/victim.txt /etc/hosts",
    "sort --output /tmp/victim.txt /etc/hosts",
    "sort -no /tmp/victim.txt /etc/hosts",
    "sort --compress-program=/bin/sh /etc/hosts",
    "sort -T /tmp /etc/hosts",
    "uniq /etc/hosts /tmp/victim.txt",
    "uniq -c /etc/hosts /tmp/victim.txt",
    "uniq -f 2 /etc/hosts /tmp/victim.txt",
)

# the reading shapes of those same verbs, which must survive the narrower rule
READING_READER_FORMS = (
    "rg pattern /etc/hosts",
    "rg -n --no-heading pattern scripts/",
    "sort --check /etc/hosts",
    "sort --reverse /etc/hosts",
    "rg --pretty pattern /etc/hosts",
    "file --brief /etc/hosts",
    "sort /etc/hosts",
    "sort -u /etc/hosts",
    "sort -rn /etc/hosts",
    "sort -k2,3 -t: /etc/passwd",
    "uniq /etc/hosts",
    "uniq -c",
    "uniq -f 2 /etc/hosts",
    "uniq -",
    "sort /etc/hosts | uniq -c",
)


class WritingReaderFlagTest(unittest.TestCase):
    """A reading verb stays reading only until a flag turns an argument into a destination.

    getopt_long accepts any unambiguous abbreviation, so a rule matching whole
    flag names alone lets `--o=FILE` through to `--output`.
    """

    def test_flags_that_write_or_exec_are_denied(self) -> None:
        for command in WRITING_READER_FORMS:
            with self.subTest(command=command):
                reason = guard.check(command)
                self.assertIsNotNone(reason, f"{command} truncates or execs but was allowed")
                self.assertTrue(reason.startswith("read-only agent: "), reason)

    def test_the_reading_shapes_still_pass(self) -> None:
        for command in READING_READER_FORMS:
            with self.subTest(command=command):
                self.assertIsNone(guard.check(command))


class AllowlistTest(unittest.TestCase):
    def test_reviewer_workflows_pass(self) -> None:
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(guard.check(command))

    def test_everything_the_grammar_does_not_describe_is_denied(self) -> None:
        for command in DENIED:
            with self.subTest(command=command):
                reason = guard.check(command)
                self.assertIsNotNone(reason)
                self.assertTrue(reason.startswith("read-only agent: "), reason)

    def test_named_verbs_are_off_the_allowlist(self) -> None:
        for name in OFF_ALLOWLIST:
            with self.subTest(name=name):
                self.assertIsNotNone(guard.check(f"{name} scripts/x.py"))


class ReasonTest(unittest.TestCase):
    def test_a_reason_says_what_to_run_instead(self) -> None:
        cases = [
            ("python3 -c 1", "-m unittest"),
            ("git reset --hard", "git show <rev>:<path>"),
            ("sed -i 's/a/b/' scripts/x.py", "sed -n"),
            ("uvx ruff check --fix .", "--fix"),
            ("cat x > /tmp/out", "2>&1"),
            ("X=1 git status", "environment assignment"),
            ("/usr/bin/git status", "path-prefixed"),
            ("node -e 1", "not on the read-only allowlist"),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertIn(expected, guard.check(command))


class FailClosedTest(unittest.TestCase):
    def test_payloads_the_guard_cannot_interpret_are_denied(self) -> None:
        payloads = [
            None,
            [],
            {},
            {"tool_input": None},
            {"tool_name": "Bash", "tool_input": {}},
            {"tool_name": "Bash", "tool_input": {"command": ["git", "status"]}},
            {"tool_name": "Bash", "tool_input": {"command": 3}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                decision = guard.decide(payload)
                self.assertIsNotNone(decision)
                reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("no Bash command to inspect", reason)

    def test_a_guard_that_raises_denies(self) -> None:
        def boom(tokens: list[str]) -> None:
            raise RuntimeError("boom")

        original = guard._segment
        guard._segment = boom
        sys.stdin = io.StringIO(json.dumps({"tool_input": {"command": "git status"}}))
        try:
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(guard.main(), 0)
        finally:
            guard._segment = original
            sys.stdin = sys.__stdin__
        decision = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("RuntimeError", decision["permissionDecisionReason"])


class MainTest(unittest.TestCase):
    def _run(self, stdin: str) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "readonly_bash_guard.py")],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout

    def _payload(self, command: object, tool: str = "Bash") -> str:
        return json.dumps({"tool_name": tool, "tool_input": {"command": command}})

    def test_deny_is_one_json_object_with_exactly_the_contract_keys(self) -> None:
        code, out = self._run(self._payload("git reset --hard"))
        self.assertEqual(code, 0)
        decision = json.loads(out)
        self.assertEqual(set(decision), {"hookSpecificOutput"})
        output = decision["hookSpecificOutput"]
        self.assertEqual(set(output), {"hookEventName", "permissionDecision", "permissionDecisionReason"})
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertEqual(out.strip(), out)

    def test_allow_is_silent(self) -> None:
        for command in ("git show HEAD:scripts/x.py", "rg -n x scripts"):
            with self.subTest(command=command):
                self.assertEqual(self._run(self._payload(command)), (0, ""))

    def test_another_tool_is_silent(self) -> None:
        self.assertEqual(self._run(json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}})), (0, ""))

    def test_unreadable_stdin_is_denied_with_exit_zero(self) -> None:
        for stdin in ("not json", "", "[", "null"):
            with self.subTest(stdin=stdin):
                code, out = self._run(stdin)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

if __name__ == "__main__":
    unittest.main()
