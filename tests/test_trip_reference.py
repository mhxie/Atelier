from __future__ import annotations

import concurrent.futures
from contextlib import contextmanager
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


import trip_reference
import _paths


HEADING = "## Visit Log"
ANCHOR = "- 2026-08-01: arrival"
REFERENCE = "- 2026-08-02: [Meal History](../meal-history.md)"


class TripReferenceTests(unittest.TestCase):
    @contextmanager
    def vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("OV")
            os.environ["OV"] = temporary
            _paths.vault_root.cache_clear()
            _paths._registry.cache_clear()
            with patch.object(trip_reference, "tier", return_value=Path(temporary) / "cache"):
                try:
                    yield Path(temporary)
                finally:
                    if previous is None:
                        os.environ.pop("OV", None)
                    else:
                        os.environ["OV"] = previous
                    _paths.vault_root.cache_clear()
                    _paths._registry.cache_clear()

    def isolated_cli(self, vault: Path) -> Path:
        repository = vault / "cli-repository"
        scripts = repository / "scripts"
        harness = repository / "harness"
        scripts.mkdir(parents=True)
        harness.mkdir()
        source_root = Path(__file__).resolve().parents[1]
        shutil.copy2(source_root / "scripts" / "trip_reference.py", scripts)
        shutil.copy2(source_root / "scripts" / "_paths.py", scripts)
        shutil.copy2(source_root / "harness" / "paths.toml", harness)
        return scripts / "trip_reference.py"

    def make_note(self, directory: Path) -> Path:
        note = directory / "trip.md"
        note.write_text(
            "# Trip\n\n## Visit Log\n- 2026-08-01: arrival\n\n## Notes\n- keep\n",
            encoding="utf-8",
        )
        return note

    def invoke(self, note: Path, expected_hash: str) -> dict[str, str]:
        return trip_reference.insert_trip_reference(
            note, HEADING, expected_hash, ANCHOR, "after", REFERENCE
        )

    def test_concurrent_cli_processes_insert_once(self) -> None:
        with self.vault() as vault:
            note = self.make_note(vault)
            expected_hash = trip_reference.section_sha256(note.read_text(), HEADING)
            self.assertIsNotNone(expected_hash)
            command = [
                sys.executable,
                str(self.isolated_cli(vault)),
                "--trip-note",
                str(note),
                "--section-heading",
                HEADING,
                "--section-sha256",
                expected_hash,
                "--anchor",
                ANCHOR,
                "--position",
                "after",
                "--reference",
                REFERENCE,
            ]
            environment = {**os.environ, "OV": str(vault)}
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                processes = list(
                    executor.map(
                        lambda _: subprocess.run(
                            command, capture_output=True, text=True, env=environment
                        ),
                        range(2),
                    )
                )
            self.assertTrue(all(process.returncode == 0 for process in processes))
            results = [json.loads(process.stdout) for process in processes]
            self.assertEqual({result["status"] for result in results}, {"inserted", "already_present"})
            self.assertEqual(note.read_text(encoding="utf-8").count(REFERENCE), 1)
            expected_lock = trip_reference.lock_file_for(note.resolve())
            self.assertTrue(expected_lock.is_file())
            self.assertTrue(expected_lock.is_relative_to(vault.resolve() / "cache"))

    def test_already_present(self) -> None:
        with self.vault() as vault:
            note = self.make_note(vault)
            expected_hash = trip_reference.section_sha256(note.read_text(), HEADING)
            self.assertEqual(self.invoke(note, expected_hash)["status"], "inserted")
            self.assertEqual(self.invoke(note, expected_hash)["status"], "already_present")

    def test_section_drift(self) -> None:
        with self.vault() as vault:
            note = self.make_note(vault)
            expected_hash = trip_reference.section_sha256(note.read_text(), HEADING)
            note.write_text(note.read_text(encoding="utf-8").replace("arrival", "changed"), encoding="utf-8")
            self.assertEqual(self.invoke(note, expected_hash)["status"], "drift")

    def test_missing_anchor(self) -> None:
        with self.vault() as vault:
            note = self.make_note(vault)
            expected_hash = trip_reference.section_sha256(note.read_text(), HEADING)
            self.assertEqual(
                trip_reference.insert_trip_reference(
                    note,
                    HEADING,
                    expected_hash,
                    "- missing",
                    "after",
                    REFERENCE,
                )["status"],
                "anchor_missing",
            )

    def test_eof_anchor_and_crlf_are_preserved(self):
        with self.vault() as vault:
            for ending in ("\n", "\r\n"):
                with self.subTest(ending=repr(ending)):
                    note = vault / "trip.md"
                    before = HEADING + ending + ANCHOR
                    note.write_bytes(before.encode())
                    expected_hash = trip_reference.section_sha256(before, HEADING)
                    self.assertEqual(self.invoke(note, expected_hash)["status"], "inserted")
                    self.assertEqual(note.read_bytes(), (before + ending + REFERENCE + ending).encode())

    def test_quoted_example_is_not_an_existing_reference(self):
        with self.vault() as vault:
            note = self.make_note(vault)
            before = note.read_text().replace(ANCHOR, f"> Example: {REFERENCE}\n{ANCHOR}")
            note.write_text(before)
            expected_hash = trip_reference.section_sha256(before, HEADING)
            self.assertEqual(self.invoke(note, expected_hash)["status"], "inserted")
            self.assertEqual(note.read_text(), before.replace(ANCHOR, ANCHOR + "\n" + REFERENCE))


if __name__ == "__main__":
    unittest.main()
