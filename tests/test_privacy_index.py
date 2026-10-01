"""Privacy index provenance, exclusions, cache, and CLI tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
import privacy_index as pi  # noqa: E402


def _vault(tmp: str) -> Path:
    vault = Path(tmp) / "vault"
    for d in (
        "research/quantum-widgets/raw", "research/quantum-widgets/audit-log",
        "research/quantum", "research/papers", "inbox/digest", "travel/trips",
        "wip", "people", "_meta", "_tools/skills/gizmo-tracker", "_tools/agents",
        "_tools/tools/meteor-console", "_tools/routines/nebula-scan", "profile", "cache",
    ):
        (vault / d).mkdir(parents=True)
    (vault / "wip" / "Quarterly Plan Draft.md").write_text("---\ntitle: Quarterly Plan Draft\npeople: [Ada Lovelace, Grace Hopper]\n---\nSee [[Charles Babbage]].\n", encoding="utf-8")
    (vault / "_tools" / "routines" / "registry.toml").write_text(
        'version = 1\n[[routine]]\nname = "orbital-scan"\nrunner = "model"\nlabel = "orbital mechanics scan"\noutput_dir = "research/quantum-widgets/agent-findings"\nfile_pattern = "*.md"\n', encoding="utf-8")
    (vault / "_tools" / "agents" / "stellar-guide.md").write_text("---\nname: stellar-guide\n---\n", encoding="utf-8")
    (vault / "profile" / "identity.md").write_text("# Me\n\n**Acme Rocketry** employs me. I studied at **Miskatonic University**.\n", encoding="utf-8")
    return vault


class IndexBuildTest(unittest.TestCase):
    def test_sources_land_with_provenance_and_generic_words_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(tmp)
            data = pi.build(vault, allowlist=set())
            terms = data["terms"]
            self.assertIn("quantum-widgets", terms)
            self.assertIn("dir", terms["quantum-widgets"]["kinds"])
            self.assertIn("research/quantum-widgets", data["paths"])
            self.assertNotIn("research", data["paths"], "public tier segments are never paths")
            self.assertIn("research/quantum", data["paths"], "a single word under a taxonomy tier is still a name")
            self.assertNotIn("inbox/digest", data["paths"], "a single word under a schema tier is schema")
            self.assertNotIn("travel/trips", data["paths"], "a dictionary word under a non-taxonomy tier is schema")
            self.assertNotIn("profile", data["paths"], "root-level single word is schema")
            self.assertTrue(any("path-shape" in r for r in pi.explain(data, "quantum")["reasons"]))
            self.assertNotIn("papers", terms, "public tier segment")
            self.assertNotIn("cache", terms)
            self.assertNotIn("raw", terms, "short generic token")
            if pi.DICTIONARY.is_file():
                self.assertNotIn("audit-log", terms, "a compound of dictionary words is schema-shaped")
                self.assertNotIn("research/quantum-widgets/audit-log", data["paths"], "deep dictionary compound is schema")
            self.assertIn("Quarterly Plan Draft", terms)
            self.assertIn("Charles Babbage", terms)
            self.assertIn("Ada Lovelace", terms)
            self.assertEqual(terms["Ada Lovelace"]["kinds"], ["frontmatter"])
            self.assertIn("orbital-scan", terms)
            self.assertIn("orbital mechanics scan", terms)
            self.assertNotIn("research/quantum-widgets/agent-findings", data["paths"], "public segment tail")
            self.assertIn("gizmo-tracker", terms)
            self.assertEqual(terms["gizmo-tracker"]["sources"], ["private skill directory"])
            self.assertEqual(terms["meteor-console"]["sources"], ["private tool directory"])
            self.assertEqual(terms["nebula-scan"]["sources"], ["private routine directory"])
            self.assertEqual(terms["stellar-guide"]["sources"], ["private agent source"])
            self.assertNotIn("Acme Rocketry", terms, "profile prose is not an index source")
            self.assertNotIn("research/papers", data["paths"], "a public tier segment is never a path")
            self.assertNotIn("quantum-widgets/raw", " ".join(data["paths"]).replace("research/", ""), "raw is not name-like")

    def test_raw_links_are_read_and_secure_notes_lend_only_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(tmp)
            store = Path(tmp) / "store"
            (store / "wip" / "secure").mkdir(parents=True)
            (store / "travel" / "raw" / "okonkwo-ledger").mkdir(parents=True)
            (store / "travel" / "raw" / "Ferry Desk Notes.md").write_text("Ask [[Rosalind Franklin]].\n", encoding="utf-8")
            (store / "wip" / "secure" / "Hidden Orchard Notes.md").write_text(
                "---\npeople: [Grace Kelly]\n---\nAsk [[Marie Curie]].\n", encoding="utf-8")
            (vault / "wip" / "secure").symlink_to(store / "wip" / "secure")
            (vault / "travel" / "raw").symlink_to(store / "travel" / "raw")
            (vault / "secure").mkdir()
            (vault / "secure" / "Quiet Harbor Notes.md").write_text("---\npeople: [Lise Meitner]\n---\n", encoding="utf-8")
            terms = pi.build(vault, allowlist=set())["terms"]
            for name in ("Hidden Orchard Notes", "okonkwo-ledger", "Rosalind Franklin", "Quiet Harbor Notes"):
                self.assertIn(name, terms)
            for name in ("Grace Kelly", "Marie Curie", "Lise Meitner"):
                self.assertNotIn(name, terms, "secure note contents are never read")

    def test_explain_reports_provenance_or_the_reason_for_absence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = pi.build(_vault(tmp), allowlist=set())
            hit = pi.explain(data, "QUANTUM-WIDGETS")
            self.assertTrue(hit["indexed"])
            self.assertEqual(hit["term"], "quantum-widgets")
            miss = pi.explain(data, "research")
            self.assertFalse(miss["indexed"])
            self.assertTrue(any("public tier" in r for r in miss["reasons"]))
            single = pi.explain(data, "gardening")
            self.assertFalse(single["indexed"])
            self.assertTrue(any("plain single word" in r for r in single["reasons"]))

    def test_load_or_build_caches_and_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(tmp)
            first = pi.load_or_build(vault, allowlist=set())
            path = pi.index_path(vault)
            self.assertTrue(path.is_file())
            (vault / "research" / "new-lab-notes").mkdir()
            cached = pi.load_or_build(vault, allowlist=set())
            self.assertEqual(cached["built"], first["built"])
            self.assertNotIn("research/new-lab-notes", cached["paths"])
            forced = pi.load_or_build(vault, force=True, allowlist=set())
            self.assertIn("research/new-lab-notes", forced["paths"])

    def test_cli_build_and_why(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(tmp)
            env = {**os.environ, "OV": str(vault)}
            proc = subprocess.run([sys.executable, "scripts/privacy_index.py", "build", "--json"], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertGreater(json.loads(proc.stdout)["terms"], 5)
            proc = subprocess.run([sys.executable, "scripts/privacy_index.py", "why", "orbital-scan"], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
            self.assertIn("private routine registry name", proc.stdout)


if __name__ == "__main__":
    unittest.main()
