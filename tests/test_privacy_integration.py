"""Privacy scanner integration tests."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import privacy_check  # noqa: E402

from tests.support import (  # noqa: E402
    expect,
)


def check_privacy_scanner() -> None:
    """Catch staged-only leaks and the boundary cases that previously escaped."""
    privacy_role = (ROOT / "agents" / "privacy-reviewer.md").read_text(
        encoding="utf-8"
    )
    push_skill = (ROOT / "skills" / "push" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    allowlist = privacy_check.load_allowlist()
    expect(
        "atelier-mbp" in allowlist, "approved public machine example is not allowlisted"
    )
    expect(
        "scripts/privacy_allowlist.txt" in privacy_role,
        "native semantic privacy role does not honor deliberate public opt-outs",
    )
    expect(
        "--- PRIVACY ALLOWLIST ---" in push_skill
        and "cat scripts/privacy_allowlist.txt" in push_skill,
        "direct semantic privacy prompt does not receive deliberate public opt-outs",
    )
    expect(
        'git log -p "$RANGE"' in push_skill,
        "direct semantic privacy prompt reads the net diff, missing a name that "
        "was added and later removed but still ships in history",
    )

    with tempfile.TemporaryDirectory(prefix="atelier-privacy-") as temp_dir:
        repo = Path(temp_dir)

        def git(*args: str) -> None:
            result = subprocess.run(
                ["git", *args],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            expect(
                result.returncode == 0,
                f"privacy fixture git {' '.join(args)} failed: {result.stderr}",
            )

        git("init", "-q")
        (repo / "README.md").write_text("public fixture\n", encoding="utf-8")
        git("add", "README.md")
        git(
            "-c",
            "user.name=Atelier Smoke",
            "-c",
            "user.email=smoke@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "base",
        )

        candidate = repo / "Private-Example-Person.md"
        candidate.write_text("PRIVATE EXAMPLE PERSON\n", encoding="utf-8")
        git("add", candidate.name)
        candidate.write_text("clean working copy\n", encoding="utf-8")

        files = privacy_check.tracked_files(repo)
        expect(
            candidate.name in files, "newly staged privacy fixture is not public-bound"
        )
        sources = privacy_check.content_sources(files, repo)
        sources.extend(privacy_check.path_sources(files))
        hits = privacy_check.scan(["Private Example Person"], sources)
        expect(
            any(
                hit["file"] == candidate.name and hit["source"] == "index"
                for hit in hits
            ),
            "privacy scanner missed a case-insensitive staged-only leak",
        )
        expect(
            not any(hit["source"] == "worktree" for hit in hits),
            "clean worktree fixture should not report a worktree leak",
        )
        expect(
            any(hit["source"] == "path" for hit in hits),
            "privacy scanner missed a filename-only leak",
        )

    boundary_sources = [
        ("inside.md", "worktree", "a masterpiece"),
        ("exact.md", "worktree", "中文Aster中文"),
    ]
    boundary_hits = privacy_check.scan_slugs({"aster"}, boundary_sources)
    expect(
        len(boundary_hits) == 1 and boundary_hits[0]["file"] == "exact.md",
        "private slug boundary matching drift",
    )
    candidates = privacy_check._wikilink_candidates(
        "people/Example-Person.md#Background"
    )
    expect(
        {"Example-Person", "Example Person"} <= candidates,
        "path-qualified wikilink normalization drift",
    )



class PrivacyIntegrationTest(unittest.TestCase):
    test_staged_path_and_boundary_detection = staticmethod(check_privacy_scanner)
