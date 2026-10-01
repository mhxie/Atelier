"""Explicit owner links keep task views and saved markers consistent."""
from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import todos


class TodoSotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.gtd = self.root / 'gtd'
        self.gtd.mkdir()
        self.owner = self.root / 'finance' / 'Example Ledger.md'
        self.owner.parent.mkdir()
        self.task = self.gtd / '2099Q1.md'
        self.ref = '[sot](<../finance/Example Ledger.md#benefit-a>)'
        self.set_owner('✅')
        self.task.write_text(f'+ [ ] Claim benefit {self.ref} due:2099-01-01\n+ [ ] Ordinary task\n')
        for target, value in [('GTD_DIR', self.gtd), ('DAILY_NOTES_DIR', self.root / 'daily-notes')]:
            mock = patch.object(todos, target, value)
            mock.start()
            self.addCleanup(mock.stop)
        for target, value in [('vault_root', self.root), ('tier_files', [])]:
            mock = patch.object(todos, target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def set_owner(self, state):
        self.owner.write_text('| Credit | Face | Status | Evidence |\n'
                              '|---|---|---|---|\n'
                              f'| Benefit <a id="benefit-a"></a> | $10 | {state} | confirmed |\n')

    def run_sync(self, apply=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return todos.cmd_sot(argparse.Namespace(cmd='sync', file=self.task.name, apply=apply))

    def test_reads_derive_live_status_without_writing(self):
        before = self.task.read_bytes()
        tasks = todos.collect_all_todos(load_age=False)
        self.assertEqual([t.state for t in tasks], ['done', 'open'])
        self.assertNotIn('[sot]', tasks[0].text)
        self.assertEqual(len(todos.collect_open_todos(load_age=False)), 1)
        self.assertEqual(self.task.read_bytes(), before)
        self.set_owner('📅')
        self.assertEqual(len(todos.collect_open_todos(load_age=False)), 2)

    def test_check_fails_on_drift_and_preview_is_read_only(self):
        before = self.task.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(todos.cmd_sot(argparse.Namespace(cmd='check')), 1)
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.task.read_bytes(), before)

    def test_apply_changes_only_marker_and_is_idempotent(self):
        before, owner = self.task.read_text(), self.owner.read_bytes()
        self.run_sync(apply=True)
        expected = before.replace('+ [ ] Claim', '+ [x] Claim')
        self.assertEqual(self.task.read_text(), expected)
        self.assertEqual(self.owner.read_bytes(), owner)
        self.run_sync(apply=True)
        self.assertEqual(self.task.read_text(), expected)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(todos.cmd_sot(argparse.Namespace(cmd='check')), 0)

    def test_apply_preserves_crlf(self):
        before = self.task.read_bytes().replace(b'\n', b'\r\n')
        self.task.write_bytes(before)
        self.run_sync(apply=True)
        self.assertEqual(self.task.read_bytes(), before.replace(b'+ [ ] Claim', b'+ [x] Claim'))

    def test_reopens_derived_completion_but_preserves_explicit_cancellation(self):
        source = self.ref.replace('[sot]', '[source]')
        self.task.write_text(f'+ [x] Claim {self.ref}\n+ [~] Cancelled {source}\n')
        self.set_owner('☐')
        self.run_sync(apply=True)
        self.assertEqual(self.task.read_text(), f'+ [ ] Claim {self.ref}\n+ [~] Cancelled {source}\n')

    def test_derived_cancellation_can_reopen(self):
        self.set_owner('🚫')
        self.run_sync(apply=True)
        self.assertTrue(self.task.read_text().startswith('+ [~]'))
        self.set_owner('☐')
        self.run_sync(apply=True)
        self.assertTrue(self.task.read_text().startswith('+ [ ]'))

    def test_bad_link_keeps_its_marker_and_spares_other_tasks(self):
        self.task.write_text('+ [ ] Broken [sot](<../finance/Missing.md#benefit-a>)\n'
                             f'+ [ ] Claim {self.ref}\n')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            tasks = todos.collect_all_todos(load_age=False)
        self.assertEqual([(t.text, t.state) for t in tasks], [('Broken', 'open'), ('Claim', 'done')])
        self.assertEqual(tasks[1].sot, '../finance/Example Ledger.md#benefit-a')
        self.assertIn('Missing.md', tasks[0].sot_error)
        self.assertIn('2099Q1.md:1', err.getvalue())
        with self.assertRaises((ValueError, OSError)):
            self.run_sync()

    def test_in_progress_marker_survives_an_open_owner(self):
        self.set_owner('☐')
        self.task.write_text(f'+ [/] Claim {self.ref}\n')
        before = self.task.read_bytes()
        self.assertEqual(self.run_sync(apply=True), 0)
        self.assertEqual(self.task.read_bytes(), before)
        self.assertEqual(todos.collect_all_todos(load_age=False)[0].state, 'wip')
        self.set_owner('✅')
        self.run_sync(apply=True)
        self.assertTrue(self.task.read_text().startswith('+ [x]'))

    def test_bad_owners_block_all_writes(self):
        good = self.owner.read_text()
        for bad in [good + good, good.replace('benefit-a', 'benefit-b'),
                    good.replace('✅', 'unknown'), '<a id="benefit-a"></a>\n',
                    '```markdown\n' + good + '```\n',
                    '```markdown\n```python\n' + good + '```\n']:
            with self.subTest(owner=bad):
                self.owner.write_text(bad)
                before = self.task.read_bytes()
                with self.assertRaises(ValueError):
                    self.run_sync(apply=True)
                self.assertEqual(self.task.read_bytes(), before)

    def test_missing_and_malformed_links_block_writes(self):
        for ref in ['[sot](<../finance/Missing.md#benefit-a>)',
                    '[sot](broken)', self.ref + ' ' + self.ref,
                    '[sot](<../../outside.md#benefit-a>)',
                    '[sot](<2099Q1.md#benefit-a>)',
                    '[sot](<../daily-notes/example.md#benefit-a>)']:
            with self.subTest(ref=ref):
                self.task.write_text(f'+ [ ] Claim {ref}\n')
                before = self.task.read_bytes()
                with self.assertRaises((ValueError, OSError)):
                    self.run_sync(apply=True)
                self.assertEqual(self.task.read_bytes(), before)

    def test_symlinks_cannot_escape_owner_or_target_boundaries(self):
        outside = self.root.parent / 'outside.md'
        self.owner.unlink()
        self.owner.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.run_sync(apply=True)
        for filename in ['../finance/Example Ledger.md', '../daily-notes/example.md']:
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                todos.cmd_sot(argparse.Namespace(cmd='sync', file=filename, apply=True))

    def test_owner_drift_prevents_apply(self):
        plan = todos.sot_changes([self.task])
        before = self.task.read_bytes()
        self.set_owner('☐')
        with patch.object(todos, 'sot_changes', return_value=plan):
            with self.assertRaisesRegex(ValueError, 'changed since planning'):
                self.run_sync(apply=True)
        self.assertEqual(self.task.read_bytes(), before)

    def test_target_drift_prevents_apply(self):
        plan = todos.sot_changes([self.task])
        self.task.write_text(self.task.read_text() + 'User addition\n')
        before = self.task.read_bytes()
        with patch.object(todos, 'sot_changes', return_value=plan):
            with self.assertRaisesRegex(ValueError, 'changed since planning'):
                self.run_sync(apply=True)
        self.assertEqual(self.task.read_bytes(), before)

    def test_edit_after_planning_check_survives(self):
        original_write = todos.atomic_write
        expected = self.task.read_text() + 'Concurrent user addition\n'

        def intervening_write(path, text, **kwargs):
            path.write_text(expected)
            original_write(path, text, **kwargs)

        with patch.object(todos, 'atomic_write', side_effect=intervening_write):
            with self.assertRaisesRegex(ValueError, 'changed before replacement'):
                self.run_sync(apply=True)
        self.assertEqual(self.task.read_text(), expected)
        self.assertEqual(list(self.gtd.glob('.*.tmp')), [])

    def test_daily_brief_uses_derived_state(self):
        from datetime import date
        import daily_brief
        warnings = []
        self.assertEqual(daily_brief.load_todos(self.root, date(2099, 1, 1), warnings), [])
        self.assertEqual(warnings, [])
        self.set_owner('☐')
        groups = daily_brief.load_todos(self.root, date(2099, 1, 1), warnings)
        self.assertTrue(groups)
        self.assertNotIn('[sot]', str(groups))

    def test_digest_latest_reflection_uses_owner(self):
        reflection = self.root / 'reflections' / '2099-01-01-reflection.md'
        reflection.parent.mkdir()
        reflection.write_text(f'## Next Actions\n- Claim benefit {self.ref}\n')
        out = io.StringIO()
        with patch.object(todos, 'find_last_reflection', return_value=reflection), \
                patch.object(todos, 'collect_open_todos', return_value=[]), \
                contextlib.redirect_stdout(out):
            todos.cmd_digest(argparse.Namespace(days=7))
        self.assertNotIn('Claim benefit', out.getvalue())


if __name__ == '__main__':
    unittest.main()
