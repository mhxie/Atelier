"""Regression tests for bucket-aware tier readers.

Glitch (2026-08-22): `reflections/` was split into `YYYY-MM/` buckets by
`scripts/fission.py`, but several readers still used non-recursive
`tier("reflections").glob(...)`. `cues.py` then raised a hard "never ran
weekly" cue every session although weekly files existed, and
`todos.py digest` found no prior reflection. `harness_smoke.py` did not catch
it because its fixture wrote reflections at the tier root.

Guard: every reader must go through `_paths.tier_files` (or `rglob`), and
the fixtures here place files inside buckets so a regression to a flat glob
fails loudly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _make_vault(root: Path, weekly_date: str) -> Path:
    vault = root / "vault"
    (vault / "reflections" / weekly_date[:7]).mkdir(parents=True)
    (vault / "reflections" / weekly_date[:7] / f"{weekly_date}-weekly.md").write_text(
        "## Energy\nfine\n", encoding="utf-8"
    )
    (vault / "reflections" / weekly_date[:7] / f"{weekly_date}-reflection.md").write_text(
        "## Theme\nt\n\n## Next Action\n- [ ] do the thing\n", encoding="utf-8"
    )
    # Directories the cue runner expects to be able to probe.
    for rel in ("daily", "gtd", "wiki", "cache", "_meta", "sessions"):
        (vault / rel).mkdir(parents=True, exist_ok=True)
    return vault


class TierFilesTest(unittest.TestCase):
    def test_tier_files_recurses_into_buckets(self) -> None:
        # Run in a subprocess: `_paths` caches $OV process-wide and importing
        # it in-process would leak the fixture vault into later test modules.
        snippet = (
            "import sys; sys.path.insert(0, 'scripts'); import _paths, json; "
            "print(json.dumps({"
            "'weekly': [p.name for p in _paths.tier_files('reflections', '*-weekly.md')], "
            "'last': _paths.tier_files('reflections', '*.md')[-1].name, "
            "'empty': [p.name for p in _paths.tier_files('sessions', '*.md')]}))"
        )
        with tempfile.TemporaryDirectory(prefix="atelier-paths-") as tmp:
            vault = _make_vault(Path(tmp), "2099-01-05")
            proc = subprocess.run(
                [sys.executable, "-c", snippet],
                cwd=REPO_ROOT,
                env={**os.environ, "OV": str(vault)},
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["weekly"], ["2099-01-05-weekly.md"])
            self.assertEqual(out["last"], "2099-01-05-weekly.md")
            self.assertEqual(out["empty"], [])


class StalenessBucketedScanTest(unittest.TestCase):
    """staleness.py was the third reader to go flat-glob blind (2026-08-23)."""

    def test_staleness_scores_bucketed_notes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-staleness-") as tmp:
            vault = Path(tmp) / "vault"
            (vault / "reflections" / "2099-01").mkdir(parents=True)
            (vault / "reflections" / "2099-01" / "2099-01-05-reflection.md").write_text(
                "# Old Thought\nbody\n", encoding="utf-8"
            )
            for rel in ("wiki", "daily", "wip", "gtd", "preprints", "agent-findings"):
                (vault / rel).mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [sys.executable, "scripts/staleness.py", "--json"],
                cwd=REPO_ROOT,
                env={**os.environ, "OV": str(vault)},
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("2099-01-05-reflection.md", proc.stdout)


class BucketedReadersTest(unittest.TestCase):
    """Drive the real CLIs against a bucketed fixture vault."""

    def _run(self, vault: Path, *argv: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "OV": str(vault), "ATELIER_SKIP_LOCK_TOUCH": "1"}
        return subprocess.run(
            [sys.executable, *argv],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_weekly_cue_sees_bucketed_weekly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            today = date.today().isoformat()
            vault = _make_vault(Path(tmp), today)
            proc = self._run(vault, "scripts/cues.py", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout or "{}")
            cues = payload.get("cues", payload) if isinstance(payload, dict) else payload
            keys = [c.get("key") for c in cues] if isinstance(cues, list) else list(cues)
            self.assertNotIn("weekly", keys, f"weekly cue fired despite bucketed weekly file: {proc.stdout}")

    def test_todos_digest_finds_bucketed_reflection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-todos-") as tmp:
            vault = _make_vault(Path(tmp), "2099-01-05")
            proc = self._run(vault, "scripts/todos.py", "digest")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("No prior reflection", proc.stdout)
            self.assertIn("do the thing", proc.stdout)


# Glitch (2026-08-27/28): the nightly sweep aborted mid-plan reading a tracked
# `_meta/*.toml` with `OSError: [Errno 11] Resource deadlock avoided`, and two
# routine cycles failed lock acquisition with the same errno. The vault sits on
# a Google Drive File Provider mount that invents EDEADLK while it materializes
# a file; the same path reads cleanly moments later.

import errno as _errno  # noqa: E402
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
import _paths  # noqa: E402


class KnowledgeLevelsTest(unittest.TestCase):
    def test_export_uses_remapped_paths_and_localized_shadows_without_reading_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = {**_paths._registry(), "wiki": "knowledge/wiki", "papers": str(root / "reading"),
                        "wiki_localized": {"zh": "knowledge/zh"}, "archive": str(root.parent / "offline")}
            with mock.patch.object(_paths, "_registry", return_value=registry):
                payload = _paths.knowledge_levels(root)
            self.assertEqual(payload["version"], 1)
            self.assertEqual([level["level"] for level in payload["levels"]], [1, 2, 3, 4])
            rules = {rule["path"]: rule for rule in payload["rules"]}
            self.assertEqual(rules["knowledge/wiki"], {"path": "knowledge/wiki", "match": "tree", "level": 4})
            self.assertEqual(rules["knowledge/zh"]["role"], "shadow")
            self.assertEqual(rules["reading"]["level"], 3)
            self.assertEqual(rules["preprints"]["level"], 3)
            self.assertEqual(rules["raw"], {"path": "raw", "match": "segment", "level": 1})
            self.assertNotIn("wiki", rules)
            self.assertNotIn("archive", rules)
            self.assertNotIn("_meta", rules)
            self.assertEqual(list(root.iterdir()), [])

    def test_export_rejects_ambiguous_or_escaping_paths(self):
        for segment in (".", "", "../outside", "a/../wiki", "a\\wiki", "C:/wiki"):
            with self.subTest(segment=segment), mock.patch.object(_paths, "_registry", return_value={
                **_paths._registry(), "wiki": segment,
            }), self.assertRaises(_paths.PathsError):
                _paths.knowledge_levels(Path("/vault"))

    def test_export_rejects_conflicting_remaps(self):
        for overlay in ({"wiki": "research"}, {"wiki_localized": {"zh": "wiki"}}):
            with self.subTest(overlay=overlay), mock.patch.object(_paths, "_registry", return_value={
                **_paths._registry(), **overlay,
            }), self.assertRaisesRegex(_paths.PathsError, "conflicting knowledge levels"):
                _paths.knowledge_levels(Path("/vault"))


class TransientMountRetryTests(unittest.TestCase):
    def test_transient_mount_error_is_retried_then_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise OSError(_errno.EDEADLK, "Resource deadlock avoided")
            return "materialized"

        self.assertEqual(
            _paths.retry_transient(flaky, delay=0, what="test read"),
            "materialized",
        )
        self.assertEqual(len(calls), 3)

    def test_persistent_transient_error_still_raises(self):
        def always():
            raise OSError(_errno.EDEADLK, "Resource deadlock avoided")

        with self.assertRaises(OSError) as caught:
            _paths.retry_transient(always, attempts=2, delay=0, what="test read")
        self.assertEqual(caught.exception.errno, _errno.EDEADLK)

    def test_unrelated_oserror_is_not_retried(self):
        calls = []

        def missing():
            calls.append(1)
            raise FileNotFoundError(_errno.ENOENT, "No such file")

        with self.assertRaises(FileNotFoundError):
            _paths.retry_transient(missing, delay=0, what="test read")
        self.assertEqual(
            len(calls), 1,
            "widening the retry beyond the mount's errno hides real bugs",
        )


class AtomicWriteTests(unittest.TestCase):
    def test_threads_publish_complete_values_without_colliding_or_changing_permissions(self):
        barrier = Barrier(2)
        replace = os.replace
        def synchronized(source, target):
            barrier.wait(timeout=5)
            return replace(source, target)
        with tempfile.TemporaryDirectory(prefix="atelier-atomic-") as directory:
            target = Path(directory) / "result.json"
            target.write_text("original")
            target.chmod(0o600)
            with mock.patch.object(_paths.os, "replace", side_effect=synchronized), ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda value: _paths.atomic_write(target, value), ("first\n", "second\n")))
            self.assertIn(target.read_text(), {"first\n", "second\n"})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_failed_replacement_preserves_original_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory(prefix="atelier-atomic-") as directory:
            target = Path(directory) / "result.json"
            target.write_text("original")
            with mock.patch.object(_paths.os, "replace", side_effect=OSError("fixture failure")), self.assertRaises(OSError):
                _paths.atomic_write(target, "replacement")
            self.assertEqual(target.read_text(), "original")
            self.assertEqual(list(Path(directory).iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
