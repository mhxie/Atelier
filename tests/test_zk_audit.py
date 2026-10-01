"""zk_audit vault-layout checks: Git work tree placement and raw_store links."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("OV", tempfile.mkdtemp())
import zk_audit as za  # noqa: E402


class LayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.vault, self.store = self.base / "vault", self.base / "store"
        (self.vault / ".git").mkdir(parents=True)
        for rel in ("a/raw", "b/secure", "c/raw", "cache"):
            (self.store / rel).mkdir(parents=True)
        (self.vault / "a").mkdir()
        (self.vault / "a" / "raw").symlink_to(self.store / "a" / "raw")
        (self.vault / "c" / "raw").mkdir(parents=True)
        (self.vault / "d").mkdir()
        (self.vault / "d" / "raw").symlink_to(self.store / "d" / "raw")
        (self.vault / "cache").symlink_to(self.store / "cache")

    def flagged(self, store: Path | None) -> list[str]:
        return sorted(f.where.rstrip("/").removeprefix(str(self.vault) + "/")
                      for f in za.check_layout(self.vault, store))

    def test_reports_missing_real_and_misdirected_folders_only(self) -> None:
        self.assertEqual(self.flagged(self.store), ["b/secure", "c/raw", "d/raw"])

    def test_fix_links_only_creates_missing_links(self) -> None:
        made = za.fix_links(self.vault, self.store)
        self.assertEqual(made, [self.vault / "b" / "secure"])
        self.assertEqual(os.readlink(self.vault / "b" / "secure"), str(self.store / "b" / "secure"))
        self.assertEqual(self.flagged(self.store), ["c/raw", "d/raw"])
        self.assertTrue((self.vault / "c" / "raw").is_dir() and not (self.vault / "c" / "raw").is_symlink())

    def test_git_placement_and_unmounted_store(self) -> None:
        (self.vault / ".git").rmdir()
        self.assertEqual(self.flagged(None), [str(self.vault)])
        self.assertEqual(za.fix_links(self.vault, self.store), [])
        stale = self.base / "gone"
        self.assertEqual([f.where for f in za.check_layout(stale, self.store)], [str(stale)])
        self.assertFalse(stale.exists())
        synced = self.base / "Library" / "CloudStorage" / "vault"
        (synced / ".git").mkdir(parents=True)
        self.assertIn("file-sync", za.check_layout(synced, None)[0].detail)
        self.assertIn("unmounted", za.check_layout(synced, self.base / "missing")[-1].detail)


class DuplicateTitleTest(unittest.TestCase):
    def test_lists_shared_fallback_titles_and_counts_archive_copies(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        notes = {
            "a/README.md": "## Body\n", "b/README.md": "```\n# fenced\n```\n",
            "c/README.md": '---\ntitle: "c README"\n---\n', "_meta/README.md": "", ".x/README.md": "",
            "d/Index.md": "# Index\n", "e/index.md": "", "f/Ideas.md": "", "g/🧠 Ideas.md": "",
            "h/Solo.md": "", "archive/h/Solo.md": "", "daily/2099-01-01.md": "", "i/2099-01-01.md": "",
            "j/Named.md": "# Same\n", "k/Other.md": "# Same\n", "m/secure/README.md": "", "m/raw/README.md": "",
        }
        for rel, text in notes.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        (root / "l").mkdir()
        (root / "l" / "README.md").symlink_to(root / "a" / "README.md")
        (root / ".reflectignore").write_text("# hidden\n_meta/\n", encoding="utf-8")
        found, archived = za.check_duplicate_titles(root)
        self.assertEqual([(f.where, f.detail.count(", ") + 1) for f in found], [("ideas", 2), ("index", 2), ("readme", 2)])
        self.assertEqual(archived, 1)


if __name__ == "__main__":
    unittest.main()
