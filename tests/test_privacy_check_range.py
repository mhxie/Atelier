"""privacy_check --range: a name that lived in an intermediate commit still fails the gate,
and the path rule catches a private directory named in prose."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class OtherRepoGateTest(unittest.TestCase):
    """--repo reads that repository's history; a range the gate cannot read never passes."""

    def _repo(self, tmp: str, text: str | None) -> tuple[Path, str]:
        repo = Path(tmp) / "fork"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "doc.md").write_text("clean\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "base")
        base = _git(repo, "rev-parse", "HEAD").strip()
        if text is None:
            _git(repo, "rm", "-q", "doc.md")
        else:
            (repo / "doc.md").write_text(text, encoding="utf-8")
            _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "change")
        return repo, base

    def _main(self, *argv: str) -> tuple[int, str]:
        index = {"terms": {"Charles Babbage": {"kinds": ["stem"], "sources": ["note filename"]}}, "paths": [], "counts": {}}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as ov, mock.patch.dict(os.environ, {"OV": ov}), \
                mock.patch.object(pc, "_discover_private_dirs", return_value=["wip"]), \
                mock.patch("privacy_index.load_or_build", return_value=index), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                return pc.main(["--json", *argv]), out.getvalue()
            except SystemExit as exc:
                return int(exc.code), out.getvalue()

    def test_repo_flag_scans_the_other_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = self._repo(tmp, "mentions Charles Babbage here\n")
            rc, out = self._main("--repo", str(repo), "--range", f"{base}..HEAD")
            self.assertEqual((rc, json.loads(out)["hit_count"]), (1, 1))
            self.assertEqual(self._main("--range", f"{base}..HEAD")[0], 2, "this checkout lacks the fork's commits")

    def test_commits_without_readable_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base = self._repo(tmp, None)
            rc, out = self._main("--repo", str(repo), "--range", f"{base}..HEAD")
            self.assertEqual((rc, json.loads(out)["action"]), (2, "abort"))
            self.assertEqual(self._main("--repo", str(repo), "--range", f"{base}..HEAD", "--allow-empty-ov")[0], 0)
            self.assertEqual(self._main("--repo", str(repo), "--range", "HEAD..HEAD")[0], 0, "an empty range has nothing to ship")


if __name__ == "__main__":
    unittest.main()
