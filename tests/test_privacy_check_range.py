"""privacy_check --range: a name that lived in an intermediate commit still fails the gate,
and the path rule catches a private directory named in prose."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import privacy_check as pc  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}).stdout


class PathRuleTest(unittest.TestCase):
    def test_private_directory_prefix_in_prose_is_a_hit(self) -> None:
        sources = [("docs/a.md", "worktree", "Read `research/quantum-widgets/agent-findings/` when stale.\nSee research/ for tiers.\n")]
        hits = pc.scan_vault_paths(["research/quantum-widgets", "research/quantum-widgets/raw"], sources)
        self.assertEqual([(h["line"], h["private_title"], h["rule"]) for h in hits], [(1, "research/quantum-widgets", "vault-path")])
        self.assertEqual(pc.scan_vault_paths(["research/quantum-widgets"], [("x.md", "worktree", "wip/notes.md and research/papers\n")]), [])


class HistoryRangeTest(unittest.TestCase):
    def test_range_scan_sees_an_intermediate_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _git(repo, "init", "-q")
            (repo / "doc.md").write_text("clean\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD").strip()
            (repo / "doc.md").write_text("mentions Charles Babbage here\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "leak")
            (repo / "doc.md").write_text("clean again\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "scrub")
            sources = pc.range_sources(f"{base}..HEAD", repo)
            self.assertEqual(sorted({s for _, s, _ in sources if s.startswith("history")}).__len__(), 2)
            hits = pc.scan(["Charles Babbage"], sources)
            self.assertEqual(len(hits), 1)
            self.assertTrue(hits[0]["source"].startswith("history:"))
            self.assertEqual(hits[0]["file"], "doc.md")


if __name__ == "__main__":
    unittest.main()
