"""Paper-cache extraction outcomes; independent of the retrieval backend."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import PYTHON, expect

ROOT = Path(__file__).resolve().parent.parent


def check_paper_cache() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    source_doc = (ROOT / "sources" / "local-papers.md").read_text(encoding="utf-8")
    read_skill = (ROOT / "skills" / "read" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    expect("/tmp/" in gitignore.splitlines(), "repo tmp containment rule is missing")
    expect(
        "Never write repo-relative `tmp/`" in instructions,
        "shared scratch boundary is missing from AGENTS.md",
    )
    for document, label in (
        (source_doc, "local paper source doc"),
        (read_skill, "read skill"),
    ):
        expect(
            "scripts/paper_cache.py" in document,
            f"{label} does not route PDF extraction through paper_cache.py",
        )

    with tempfile.TemporaryDirectory(prefix="atelier-paper-cache-") as temp_dir:
        temp = Path(temp_dir)
        vault = temp / "vault"
        papers = vault / "papers"
        papers.mkdir(parents=True)
        (vault / "preprints").mkdir()
        source = papers / "Example Paper.pdf"
        source.write_text("fixture pdf", encoding="utf-8")

        fake_bin = temp / "bin"
        fake_bin.mkdir()
        fake_pdftotext = fake_bin / "pdftotext"
        fake_pdftotext.write_text(
            f"#!{PYTHON}\n"
            "from pathlib import Path\n"
            "import sys\n"
            "source = Path(sys.argv[-2]).read_text(encoding='utf-8')\n"
            "content = '   \\n' if source == 'empty fixture' else 'EXTRACTED\\n' + source\n"
            "Path(sys.argv[-1]).write_text(content, encoding='utf-8')\n",
            encoding="utf-8",
        )
        fake_pdftotext.chmod(0o755)
        env = os.environ | {
            "OV": str(vault),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        }

        def cache(path: Path, *, json_output: bool = True) -> subprocess.CompletedProcess[str]:
            command = [PYTHON, "scripts/paper_cache.py", str(path)]
            if json_output:
                command.append("--json")
            return subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, env=env
            )

        first = cache(source)
        expect(first.returncode == 0, f"paper cache extraction failed: {first.stderr}")
        first_payload = json.loads(first.stdout)
        expect(
            first_payload["status"] == "extracted",
            "paper cache did not extract on miss",
        )
        cache_dir = vault / "cache" / "example-paper"
        expect(
            (cache_dir / "paper.txt").read_text(encoding="utf-8")
            == "EXTRACTED\nfixture pdf",
            "paper cache text output drift",
        )
        expect((cache_dir / "index.md").is_file(), "paper cache index is missing")
        expect(
            (cache_dir / "source.json").is_file(),
            "paper cache source signature is missing",
        )

        second = cache(source)
        expect(second.returncode == 0, f"paper cache reuse failed: {second.stderr}")
        expect(
            json.loads(second.stdout)["status"] == "cached",
            "fresh paper cache was rebuilt",
        )

        colliding = vault / "preprints" / "Example Paper.pdf"
        colliding.write_text("different fixture pdf", encoding="utf-8")
        collision = cache(colliding)
        expect(collision.returncode == 2, "paper cache overwrote a same-slug PDF cache")
        expect(
            (cache_dir / "paper.txt").read_text(encoding="utf-8")
            == "EXTRACTED\nfixture pdf",
            "paper cache collision changed the original extraction",
        )

        empty_source = papers / "Empty.pdf"
        empty_source.write_text("empty fixture", encoding="utf-8")
        empty = cache(empty_source)
        expect(empty.returncode == 2, "paper cache accepted an empty text extraction")
        expect(
            not (vault / "cache" / "empty" / "paper.txt").exists(),
            "paper cache retained an empty extraction",
        )

        outside = temp / "outside.pdf"
        outside.write_text("not canonical", encoding="utf-8")
        rejected = cache(outside, json_output=False)
        expect(
            rejected.returncode == 2, "paper cache accepted a PDF outside the L3 store"
        )


class PaperCacheTest(unittest.TestCase):
    test_paper_cache = staticmethod(check_paper_cache)
