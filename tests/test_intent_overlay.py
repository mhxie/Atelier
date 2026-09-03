"""The gitignored intents overlay extends `examples` without inventing rows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_with_overlay(overlay_text: str, extra_keys: tuple[str, ...] = ()) -> dict:
    with tempfile.TemporaryDirectory(prefix="atelier-overlay-") as tmp:
        root = Path(tmp)
        shutil.copytree(REPO_ROOT / "harness", root / "harness")
        (root / "harness" / "intents.local.toml").write_text(overlay_text, encoding="utf-8")
        snippet = (
            "import sys, json\n"
            "sys.path.insert(0, 'scripts')\n"
            "import intent_coverage as ic\n"
            f"ic.ROOT = __import__('pathlib').Path({str(root)!r})\n"
            "intents = ic.load_intents()\n"
            f"extra = {list(extra_keys)!r}\n"
            "print(json.dumps({'count': len(intents),\n"
            "                  'weekly_examples': intents['weekly'].get('examples'),\n"
            "                  'weekly_description': intents['weekly'].get('description'),\n"
            "                  'invented': 'invented' in intents,\n"
            "                  'rows': {k: intents.get(k) for k in extra},\n"
            "                  'catalog': ic.render_catalog(ic.catalog_rows(intents), examples=False)}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=REPO_ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])


class IntentOverlayTest(unittest.TestCase):
    def test_overlay_extends_examples_and_cannot_invent_rows(self) -> None:
        out = _load_with_overlay(
            textwrap.dedent(
                """
                [intents.weekly]
                examples = ["zzz weekly probe phrase"]
                description = "must not override"

                [intents.invented]
                examples = ["should never appear"]
                """
            )
        )
        self.assertIn("zzz weekly probe phrase", out["weekly_examples"])
        self.assertNotEqual(out["weekly_description"], "must not override")
        self.assertFalse(out["invented"])

    def test_private_rows_need_description_and_an_existing_procedure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-private-") as tmp:
            proc = Path(tmp) / "gizmo" / "SKILL.md"
            proc.parent.mkdir(parents=True)
            proc.write_text("# gizmo\n", encoding="utf-8")
            out = _load_with_overlay(
                textwrap.dedent(
                    f"""
                    [intents.gizmo]
                    description = "Track warranty expiries for household gadgets."
                    procedure = {str(proc)!r}
                    examples = ["gizmo"]

                    [intents.ghost]
                    description = "no procedure"

                    [intents.broken]
                    procedure = {str(proc)!r}
                    """
                ),
                extra_keys=("gizmo", "ghost", "broken"),
            )
            self.assertTrue(out["rows"]["gizmo"]["private"])
            self.assertEqual(out["rows"]["gizmo"]["procedure"], str(proc))
            self.assertEqual(out["rows"]["gizmo"]["mode"], "private-feature")
            self.assertEqual(out["rows"]["gizmo"]["profile_reads"], [])
            self.assertIsNone(out["rows"]["ghost"])
            self.assertIsNone(out["rows"]["broken"])
            self.assertIn("(private)", out["catalog"])

    def test_legacy_patterns_key_is_read_as_examples(self) -> None:
        out = _load_with_overlay('[intents.weekly]\npatterns = ["legacy phrase"]\n')
        self.assertIn("legacy phrase", out["weekly_examples"])

    def test_broken_overlay_never_breaks_routing(self) -> None:
        out = _load_with_overlay("[broken")
        self.assertGreater(out["count"], 10)


if __name__ == "__main__":
    unittest.main()
