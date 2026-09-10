"""Safety checks for retirement cleanup of legacy full-payload API logs."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import invocation_log_gc as gc


class InvocationLogGcTest(unittest.TestCase):
    def test_only_old_regular_jsonl_children_are_removed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-log-gc-") as tmp:
            root = Path(tmp)
            old = root / "old.jsonl"
            fresh = root / "fresh.jsonl"
            boundary = root / "boundary.jsonl"
            other = root / "old.txt"
            target = root / "target"
            nested = root / "nested.jsonl"
            for path in (old, fresh, boundary, other, target):
                path.write_text(path.name, encoding="utf-8")
            nested.mkdir()
            link = root / "link.jsonl"
            link.symlink_to(target)
            now = 2_000_000_000.0
            old_time = now - 91 * 86400
            for path in (old, other, target, nested):
                os.utime(path, (old_time, old_time), follow_symlinks=False)
            os.utime(fresh, (now, now))
            cutoff = now - 90 * 86400
            os.utime(boundary, (cutoff, cutoff))

            self.assertEqual(gc.rotate(root, 90, now=now), 1)
            self.assertFalse(old.exists())
            for path in (fresh, boundary, other, target, nested, link):
                self.assertTrue(path.exists(), path)

    def test_nonpositive_retention_refuses_to_delete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-log-gc-") as tmp:
            old = Path(tmp) / "old.jsonl"
            old.write_text("private", encoding="utf-8")
            for days in (0, -1):
                with self.subTest(days=days):
                    self.assertEqual(gc.rotate(Path(tmp), days, now=2_000_000_000.0), 0)
                    self.assertTrue(old.exists())

    def test_symlink_root_and_permission_failure_are_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-log-gc-") as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            old = real / "old.jsonl"
            old.write_text("private", encoding="utf-8")
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            self.assertEqual(gc.rotate(alias, 90, now=2_000_000_000.0), 0)
            self.assertTrue(old.exists())
            with mock.patch.object(gc.os, "open", side_effect=PermissionError):
                self.assertEqual(gc.rotate(real, 90, now=2_000_000_000.0), 0)
            self.assertTrue(old.exists())

    def test_main_uses_only_the_fixed_path_and_default_retention(self) -> None:
        with mock.patch.object(gc, "rotate", return_value=0) as rotate:
            self.assertEqual(gc.main([]), 0)
        rotate.assert_called_once_with(gc.LOG_DIR, gc.RETENTION_DAYS)

    def test_scan_failure_is_best_effort(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-log-gc-") as tmp, mock.patch.object(
            gc.os, "scandir", side_effect=PermissionError
        ):
            self.assertEqual(gc.rotate(Path(tmp), 90), 0)

    def test_entry_stat_and_unlink_failures_do_not_block_other_entries(self) -> None:
        denied_stat = mock.Mock()
        denied_stat.name = "denied-stat.jsonl"
        denied_stat.stat.side_effect = PermissionError
        denied_unlink = mock.Mock()
        denied_unlink.name = "denied-unlink.jsonl"
        denied_unlink.stat.return_value = mock.Mock(st_mode=0o100600, st_mtime=1)
        removable = mock.Mock()
        removable.name = "removable.jsonl"
        removable.stat.return_value = mock.Mock(st_mode=0o100600, st_mtime=1)
        scan = mock.MagicMock()
        scan.__enter__.return_value = [denied_stat, denied_unlink, removable]
        with mock.patch.object(gc.os, "open", return_value=10), mock.patch.object(
            gc.os, "scandir", return_value=scan
        ), mock.patch.object(
            gc.os, "unlink", side_effect=[PermissionError, None]
        ) as unlink, mock.patch.object(gc.os, "close") as close:
            self.assertEqual(gc.rotate(Path("/fixture"), 90, now=2_000_000_000.0), 1)
        self.assertEqual(unlink.call_count, 2)
        close.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main()
