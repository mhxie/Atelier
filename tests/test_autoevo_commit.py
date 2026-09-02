"""Pin the autoevo commit shapes now that a script owns them.

Downstream consumers: the revert-tombstone walk greps `cluster_hash:` from
commit bodies; `git log --grep='^\\[autoevo:'` is the operational index; bot
authorship attributes automated changes. `--only` isolation is the safety
property the scoped dirty gate relies on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )


def _run(vault: Path, *argv: str, protected_file: str | None = None) -> dict:
    proc = subprocess.run(
        [sys.executable, "scripts/autoevo_commit.py", *argv],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            **({"AUTOEVO_PROTECTED_FILE": protected_file} if protected_file else {}),
            "OV": str(vault),
            "GIT_AUTHOR_NAME": "Local User",
            "GIT_AUTHOR_EMAIL": "local@example.com",
            "GIT_COMMITTER_NAME": "Local Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.com",
        },
        capture_output=True, text=True, timeout=120,
    )
    payload = json.loads(proc.stdout)
    payload["_exit"] = proc.returncode
    return payload


class AutoevoCommitTest(unittest.TestCase):
    def _vault(self, tmp: str) -> Path:
        vault = Path(tmp) / "vault"
        (vault / "wip").mkdir(parents=True)
        (vault / "wip" / "a.md").write_text("a\n", encoding="utf-8")
        (vault / "wip" / "b.md").write_text("b\n", encoding="utf-8")
        (vault / "wip" / "target.md").write_text("merged\n", encoding="utf-8")
        _git(vault, "init", "-q")
        _git(vault, "add", "-A")
        _git(vault, "commit", "-q", "-m", "base")
        return vault

    def test_merge_commit_shape_and_only_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = self._vault(tmp)
            (vault / "wip" / "a.md").unlink()
            (vault / "wip" / "target.md").write_text("merged a+b\n", encoding="utf-8")
            # A stray user edit that must NOT enter the bot commit.
            (vault / "wip" / "b.md").write_text("user edit in progress\n", encoding="utf-8")
            out = _run(
                vault, "merge", "--scope", "wip", "--target-slug", "target",
                "--band", "redundant-high (3+ peers >= 0.85, all > 30d cold, mode=real, floor=0.6)",
                "--source", "wip/a.md",
                "--source-evidence", "wip/a.md (retrieval 0.91, mtime 2025-12-01)",
                "--paths", "wip/a.md", "wip/target.md",
            )
            self.assertEqual(out["_exit"], 0, out)
            body = _git(vault, "log", "-1", "--format=%B").stdout
            self.assertIn("[autoevo:redundant] wip: merge 1 notes into target", body)
            self.assertIn(f"cluster_hash: {out['cluster_hash']}", body)
            self.assertNotIn("Co-Authored-By:", body)
            self.assertEqual(
                _git(vault, "log", "-1", "--format=%an <%ae>").stdout.strip(),
                "Atelier Autoevo Bot <noreply@atelier.local>",
            )
            self.assertEqual(
                _git(vault, "log", "-1", "--format=%cn <%ce>").stdout.strip(),
                "Atelier Autoevo Bot <noreply@atelier.local>",
            )
            changed = _git(vault, "show", "--name-only", "--format=", "HEAD").stdout.split()
            self.assertIn("wip/target.md", changed)
            self.assertNotIn("wip/b.md", changed, "--only isolation breached")

    def test_cluster_hash_is_order_insensitive(self) -> None:
        snippet = (
            "import sys; sys.path.insert(0, 'scripts'); import autoevo_commit as a; "
            "print(a.cluster_hash(['wip/b.md', 'wip/a.md']) == a.cluster_hash(['wip/a.md', 'wip/b.md']))"
        )
        proc = subprocess.run([sys.executable, "-c", snippet], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "True", proc.stderr)

    def test_audit_commit_with_force_added_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = self._vault(tmp)
            (vault / ".gitignore").write_text("_meta/\n", encoding="utf-8")
            _git(vault, "add", ".gitignore")
            _git(vault, "commit", "-q", "-m", "ignore")
            (vault / "agent-findings").mkdir()
            (vault / "agent-findings" / "autoevo-applied-2099-01-02.md").write_text("## Autoevo Run\n", encoding="utf-8")
            (vault / "_meta").mkdir()
            (vault / "_meta" / "autoevo_quarantine.toml").write_text("version = 1\n", encoding="utf-8")
            out = _run(
                vault, "audit", "--run-date", "2099-01-02", "--auto", "0", "--pending", "2",
                "--errors", "0", "--quarantined", "1",
                "--paths", "agent-findings/autoevo-applied-2099-01-02.md",
                "--force-add", "_meta/autoevo_quarantine.toml",
            )
            self.assertEqual(out["_exit"], 0, out)
            body = _git(vault, "log", "-1", "--format=%B").stdout
            self.assertIn("[autoevo:audit] agent-findings: record nightly run 2099-01-02", body)
            self.assertIn("Auto-applied: 0, Pending: 2, Errors: 0, Quarantined: 1", body)
            changed = _git(vault, "show", "--name-only", "--format=", "HEAD").stdout.split()
            self.assertIn("_meta/autoevo_quarantine.toml", changed)

    def test_failed_commit_reports_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = self._vault(tmp)  # clean tree: commit --only with no changes fails
            out = _run(vault, "queue", "--summary", "append 0", "--detail", "Categories: none")
            self.assertEqual(out["_exit"], 1)
            self.assertIn("error", out)


class QueueExtraPathTest(unittest.TestCase):
    def test_queue_commit_includes_extra_paths_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            (vault / "_meta").mkdir(parents=True)
            (vault / "agent-findings").mkdir()
            (vault / "wip").mkdir()
            queue = vault / "_meta" / "autoevo_pending.toml"
            queue.write_text("# queue\n", encoding="utf-8")
            audit = vault / "agent-findings" / "autoevo-applied-2099-01-02.md"
            audit.write_text("## Autoevo Run\n", encoding="utf-8")
            _git(vault, "init", "-q")
            _git(vault, "add", "-A")
            _git(vault, "commit", "-q", "-m", "base")
            queue.write_text("# queue\n# dismissed\n", encoding="utf-8")
            audit.write_text("## Autoevo Run\n### Auto-dismissed\n", encoding="utf-8")
            # A stray edit that must NOT be swept into the queue commit.
            (vault / "wip" / "stray.md").write_text("s\n", encoding="utf-8")
            out = _run(
                vault, "queue",
                "--summary", "auto-dismiss 1 stale pending entries",
                "--detail", "Categories: redundant=1",
                "--extra-path", "agent-findings/autoevo-applied-2099-01-02.md",
            )
            self.assertEqual(out["_exit"], 0, out)
            changed = _git(vault, "show", "--name-only", "--format=", "HEAD").stdout.split()
            self.assertEqual(
                sorted(changed),
                ["_meta/autoevo_pending.toml", "agent-findings/autoevo-applied-2099-01-02.md"],
            )
            subject = _git(vault, "log", "-1", "--format=%s").stdout.strip()
            self.assertEqual(subject, "[autoevo:queue] _meta: auto-dismiss 1 stale pending entries")


if __name__ == "__main__":
    unittest.main()


# Glitch (2026-08-29): the dirty-tree gate blocked every sweep that followed a
# work day, so it was narrowed to autoevo's own state. The protection for the
# user's in-progress notes moved to this choke point, and must hold here or a
# sweep can delete uncommitted work that git cannot recover.


class ProtectedPathTest(unittest.TestCase):
    def _vault(self, tmp: str) -> Path:
        vault = Path(tmp) / "vault"
        (vault / "wip").mkdir(parents=True)
        (vault / "wip" / "a.md").write_text("a\n", encoding="utf-8")
        (vault / "wip" / "target.md").write_text("t\n", encoding="utf-8")
        _git(vault, "init", "-q")
        _git(vault, "add", "-A")
        _git(vault, "commit", "-q", "-m", "base")
        return vault

    def test_protected_path_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = self._vault(tmp)
            protected = Path(tmp) / "protected.txt"
            protected.write_text("wip/a.md\n", encoding="utf-8")
            head = _git(vault, "rev-parse", "HEAD").stdout.strip()
            out = _run(
                vault, "audit", "--run-date", "2099-01-01", "--auto", "0",
                "--pending", "0", "--errors", "0", "--quarantined", "0",
                "--paths", "wip/a.md",
                protected_file=str(protected),
            )
            self.assertIn("error", out)
            self.assertIn("uncommitted user edits", out["error"])
            self.assertEqual(
                _git(vault, "rev-parse", "HEAD").stdout.strip(), head,
                "a refused commit must not advance HEAD",
            )

    def test_unprotected_path_still_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = self._vault(tmp)
            protected = Path(tmp) / "protected.txt"
            protected.write_text("wip/other.md\n", encoding="utf-8")
            (vault / "wip" / "a.md").write_text("changed\n", encoding="utf-8")
            out = _run(
                vault, "audit", "--run-date", "2099-01-01", "--auto", "0",
                "--pending", "0", "--errors", "0", "--quarantined", "0",
                "--paths", "wip/a.md",
                protected_file=str(protected),
            )
            self.assertIn("sha", out, out)

    def test_unreadable_protection_list_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = self._vault(tmp)
            (vault / "wip" / "a.md").write_text("changed\n", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "scripts/autoevo_commit.py", "audit",
                 "--run-date", "2099-01-01", "--auto", "0", "--pending", "0",
                 "--errors", "0", "--quarantined", "0", "--paths", "wip/a.md"],
                cwd=REPO_ROOT,
                env={**os.environ, "OV": str(vault),
                     "AUTOEVO_PROTECTED_FILE": str(Path(tmp) / "missing.txt")},
                capture_output=True, text=True, timeout=120,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("cannot read AUTOEVO_PROTECTED_FILE", proc.stderr)
