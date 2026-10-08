"""zk_audit vault-layout checks: Git work tree placement and raw_store links."""

from __future__ import annotations

import os
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

os.environ.setdefault("OV", tempfile.mkdtemp())
import zk_audit as za  # noqa: E402


class KnowledgeLevelsSyncTest(unittest.TestCase):
    def test_export_rejects_symlinked_parent_or_target_without_changing_external_file(self):
        for symlink_parent in (False, True):
            with self.subTest(parent=symlink_parent), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root, outside = base / "vault", base / "outside"
                root.mkdir()
                outside.mkdir()
                sentinel = outside / "knowledge-levels.json"
                sentinel.write_text("keep this")
                if symlink_parent:
                    (root / ".reflect").symlink_to(outside, target_is_directory=True)
                else:
                    (root / ".reflect").mkdir()
                    (root / ".reflect" / sentinel.name).symlink_to(sentinel)
                with mock.patch.object(za, "OV", root), mock.patch("sys.stderr"):
                    self.assertEqual(za.main(["--sync-reflect-levels"]), 2)
                self.assertEqual(sentinel.read_text(), "keep this")

    def test_export_only_replaces_derived_sidecar_then_audit_detects_path_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / "Example.md"
            note.write_text("# Example\nUntouched.\n")
            with mock.patch.object(za, "OV", root), mock.patch.object(za, "run_audit", return_value=za.Report(vault=str(root))), \
                 mock.patch("sys.stdout"):
                self.assertEqual(za.main(["--sync-reflect-levels", "--json"]), 0)
            sidecar = root / ".reflect" / "knowledge-levels.json"
            self.assertEqual(json.loads(sidecar.read_text()), za.knowledge_levels(root))
            self.assertEqual(note.read_text(), "# Example\nUntouched.\n")
            with mock.patch.object(za, "OV", root), mock.patch.object(za, "raw_store", return_value=None), \
                 mock.patch.object(za, "backup_limit", return_value=None):
                self.assertFalse(any("knowledge levels" in finding.detail for finding in za.run_audit().layout))
                sidecar.write_text('{"version": 0}')
                self.assertTrue(any("knowledge levels" in finding.detail for finding in za.run_audit().layout))


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

    def test_archive_tier_is_one_root_link_whose_inner_folders_need_none(self) -> None:
        (self.store / "archive" / "x" / "raw").mkdir(parents=True)
        (self.vault / "archive").mkdir()
        flagged = {f.where.rstrip("/").removeprefix(str(self.vault) + "/"): f.detail
                   for f in za.check_layout(self.vault, self.store)}
        self.assertIn("in Git, not raw_store", flagged["archive"])
        (self.vault / "archive").rmdir()
        self.assertIn(self.vault / "archive", za.fix_links(self.vault, self.store))
        self.assertEqual(self.flagged(self.store), ["c/raw", "d/raw"])
        (self.vault / "a" / "archive").mkdir()
        self.assertEqual(self.flagged(self.store), ["a/archive", "c/raw", "d/raw"])


class ArchiveLinkTest(unittest.TestCase):
    def test_reflect_notes_enter_the_archive_link_but_no_other(self) -> None:
        import _reflect

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root, store = Path(tmp.name) / "vault", Path(tmp.name) / "store"
        for rel in ("archive/a/Old.md", "archive/a/secure/Hidden.md", "archive/a/raw/Dump.md", "elsewhere/Stray.md"):
            (store / rel).parent.mkdir(parents=True, exist_ok=True)
            (store / rel).write_text("+ [ ] open\n", encoding="utf-8")
        root.mkdir()
        (root / "archive").symlink_to(store / "archive")
        (root / "linked").symlink_to(store / "elsewhere")
        self.assertEqual([n.path.as_posix() for n in _reflect.notes(root)], ["archive/a/Old.md"])
        self.assertEqual([Path(f.where).name for f in za.check_archive_tasks(root)], ["Old.md"])
        (store / "archive/a/Empty.md").write_text("", encoding="utf-8")
        self.assertEqual(za.check_root_orphans(root)[2], 1)


class LargeFileTest(unittest.TestCase):
    def test_moves_oversized_files_into_a_raw_link_and_repoints_links(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root, store = Path(tmp.name) / "vault", Path(tmp.name) / "store"
        files = {
            "p/big file.pdf": "x" * 200, "p/small.pdf": "x", ".git/pack": "x" * 200, "q/raw/kept.pdf": "x" * 200,
            "p/Note.md": "![](big%20file.pdf) [root](p/big%20file.pdf) `![](big file.pdf)` ![](small.pdf)\n",
            "r/Other.md": "[see](../p/big%20file.pdf#page=2) [other](big%20file.pdf)\n",
        }
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        self.assertEqual([f.where for f in za.check_large_files(root, 150)], [str(root / "p/big file.pdf")])
        self.assertEqual(za.check_large_files(root, None), [])
        moved = za.fix_large(root, store, 150)
        self.assertEqual(moved, [(Path("p/big file.pdf"), Path("p/raw/big file.pdf"))])
        self.assertEqual(os.readlink(root / "p/raw"), str(store / "p/raw"))
        self.assertTrue((store / "p/raw/big file.pdf").is_file())
        self.assertEqual((root / "p/Note.md").read_text(encoding="utf-8"),
                         "![](raw/big%20file.pdf) [root](raw/big%20file.pdf) `![](big file.pdf)` ![](small.pdf)\n")
        self.assertEqual((root / "r/Other.md").read_text(encoding="utf-8"),
                         "[see](../p/raw/big%20file.pdf) [other](big%20file.pdf)\n")
        self.assertEqual(za.check_large_files(root, 150), [])

    def test_backup_limit_reads_this_graphs_reflect_setting(self) -> None:
        import json
        from unittest.mock import patch

        import _reflect

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings, root = Path(tmp.name) / "settings.json", Path(tmp.name)
        with patch.object(_reflect, "SETTINGS", settings):
            self.assertIsNone(_reflect.backup_limit(root))
            for value, expected in ((32, 32 * 2**20), (96, None), (True, None), ("32", None)):
                settings.write_text(json.dumps({"backupMaxFileMiB": {str(root): value}}), encoding="utf-8")
                self.assertEqual(_reflect.backup_limit(root), expected, value)


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
        self.assertEqual([(f.where, f.detail.count(", ") + 1) for f in found], [("ideas", 2), ("index", 2), ("readme", 2), ("same", 2)])
        self.assertEqual(archived, 1)


class TitleFormatTest(unittest.TestCase):
    def test_localized_h1_can_omit_language_suffix_without_changing_link_identity(self):
        from _reflect import read_head, title_issue

        source = '---\ntitle: "Example (中文)"\nlang: zh-CN\n---\n\n# Example\n'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Example.md"
            path.write_text(source, encoding="utf-8")
            title, aliases = read_head(path)
            self.assertEqual((title, aliases), ("Example (中文)", ()))
            self.assertIsNone(title_issue(source, title))
            for replacement in ("", "lang: false\n", "lang: []\n"):
                self.assertIsNotNone(title_issue(source.replace("lang: zh-CN\n", replacement), title))

    def test_quoted_unicode_titles_and_aliases_agree_with_the_rendered_h1(self) -> None:
        from _reflect import read_head, title_issue

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Quoted.md"
            path.write_text('---\ntitle: "Amdahl\\u2019s Law"\naliases: ["\\u00b5s", \'It\'\'s fine\']\n---\n# Amdahl’s Law\n', encoding="utf-8")
            title, aliases = read_head(path)
            self.assertEqual(title, "Amdahl’s Law")
            self.assertEqual(aliases, ("µs", "It's fine"))
            self.assertIsNone(title_issue(path.read_text(), title))

    def test_reports_missing_late_and_conflicting_titles_without_reading_private_notes(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        notes = {
            "research/Missing.md": "## Background\n",
            "research/Late.md": "Preface.\n\n# Late\n",
            "research/Metadata.md": '---\ntitle: "Metadata"\n---\n## Body\n',
            "research/Conflict.md": "---\ntitle: Canonical\n---\n# Other\n",
            "research/Good.md": '---\ntitle: "Good"\n---\n\n# Good\n',
            "research/Fenced.md": "```\n# Code\n```\n",
            "research/Indented.md": "    # Code\n",
            "archive/Old.md": "## Old\n",
            "daily/2099-01-01.md": "User-authored daily text.\n",
            "_meta/State.md": "Operational state.\n",
            "secure/Private.md": "Do not read.\n",
            "raw/Raw.md": "Do not read.\n",
        }
        for rel, text in notes.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        (root / ".reflectignore").write_text("_meta/\n", encoding="utf-8")
        original_open = Path.open

        # Secure/raw/ignored paths must stay closed during catalog discovery too.
        def guarded_catalog_open(path, *args, **kwargs):
            self.assertFalse({"secure", "raw", "_meta"}.intersection(path.relative_to(root).parts))
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_catalog_open):
            found, archived = za.check_title_format(root)
        self.assertEqual(archived, 1)
        self.assertEqual({Path(f.where).stem for f in found}, {"Missing", "Late", "Metadata", "Conflict", "Fenced", "Indented"})
        self.assertEqual(next(f.detail for f in found if Path(f.where).stem == "Conflict"), "authored title differs from opening H1")
        report = za.Report(vault=str(root), title_format=found, title_format_archive_count=archived)
        self.assertEqual(report.total(), 7)
        self.assertEqual(report.to_dict()["categories"]["title_format_archive_count"], 1)
        self.assertIn("[11] Opening H1 titles (6)", za.format_human(report))


class ArchiveTaskTest(unittest.TestCase):
    def test_flags_open_round_tasks_in_archive_only(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        notes = {
            "archive/a/Old.md": "# Old\n+ [ ] open\n  + [ ] nested\n+ [x] done\n```\n+ [ ] fenced\n```\n",
            "archive/b/Demoted.md": "# Demoted\n- [ ] square\n+ [~] killed\n",
            "gtd/2099Q4.md": "# 2099 Q4\n+ [ ] live\n",
            "archive/secure/Hidden.md": "+ [ ] private\n",
        }
        for rel, text in notes.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        found = za.check_archive_tasks(root)
        self.assertEqual([(Path(f.where).name, f.detail) for f in found], [("Old.md", "2 open `+ [ ]`")])



class ReflectSyntaxTest(unittest.TestCase):
    def test_claim_comments_are_native_only_when_ranges_are_valid(self):
        import _reflect

        good = "# Title\n\n<!-- claim:c1 -->\n\n中文 😀 first.<!-- /claim:c1 --> and <!-- claim:c4 -->second.<!-- /claim:c4 -->\n"
        self.assertEqual(_reflect.nonnative(good), {})
        self.assertEqual(_reflect.nonnative("# Title\n\n`<!-- claim:c1 -->`\n"), {})
        bad = "# Title\n\nBefore <!-- claim:c1 -->unclosed.\n"
        self.assertIn("claim_range", _reflect.nonnative(bad))
        self.assertIn("html", _reflect.nonnative(bad))
        self.assertIn("claim_range", _reflect.nonnative("# Title\n\n<!-- claim:c1 -->**swallowed**<!-- /claim:c1 -->\n"))

    def test_counts_unrendered_syntax_and_aggregates_daily_and_inbox(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        wrapped = "This sentence is long enough to count as hard-wrapped prose in a\nnote body that continues on the next line here.\n"
        notes = {
            "research/Native.md": (
                "# Native\n\nOne line paragraph.\n\n+ [ ] task\n+ [x] ~~gone~~\n- [ ] check\n<!-- hidden -->\n"
                '![a](x.png) <!-- {"width":1} -->\n```\n^c1 <br> + [~] x\n```\n[[Native]] [[#Top|top]]\n'
            ),
            "research/Old.md": (
                "---\ntags: [a]\n---\n# Old\n\nclaim ^c1\n+ [~] killed\n+ plain\n"
                "x <br> &amp; [h](#top) [n](Native.md)\n> [!NOTE] hi\nfoot[^1] %%c%%\nkey:: v\n\n---\nslug: s\n---\n\n" + wrapped
            ),
            "daily/2099-01-01.md": "+ [~] mine\n",
            "inbox/clip.md": "<br>\n",
        }
        for rel, text in notes.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        found, kinds, raw = za.check_reflect_syntax(root)
        self.assertEqual([Path(f.where).name for f in found], ["Old.md"])
        self.assertEqual(raw, 2)
        expected = ("block_id callout dataview entity footnote frontmatter_tags hard_wrap heading_link html "
                    "md_note_link obsidian_comment plus_bullet task_marker yaml_block")
        self.assertEqual(kinds, dict.fromkeys(expected.split(), 1))

    def test_joins_wrapped_lines_without_spaces_between_cjk(self) -> None:
        import _reflect

        lines = ["这是一个很长的中文句子，用来测试硬换行的段落在合并时不会插入多余的空格，因为中文之间没有空格分隔，所以需要",
                 "直接拼接成一行。"]
        self.assertEqual(_reflect.join_wrapped(lines, 0, 2), lines[0] + lines[1])

if __name__ == "__main__":
    unittest.main()
