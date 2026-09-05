"""smoke_regressions.py: privacy scanner, public regression tests, and ruff smoke checks.

Split out of harness_smoke.py; harness_smoke.py re-exports every name so callers and tests are unchanged.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
import privacy_check

from smoke_common import (  # noqa: E402
    PYTHON,
    ROOT,
    expect,
)


def check_privacy_scanner() -> None:
    """Catch staged-only leaks and the boundary cases that previously escaped."""
    privacy_role = (ROOT / ".claude" / "agents" / "privacy-reviewer.md").read_text(
        encoding="utf-8"
    )
    system_review = (ROOT / ".claude" / "commands" / "system-review.md").read_text(
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
        "--- PRIVACY ALLOWLIST ---" in system_review
        and "cat scripts/privacy_allowlist.txt" in system_review,
        "direct semantic privacy prompt does not receive deliberate public opt-outs",
    )
    expect(
        "git ls-files --others --exclude-standard -z" in system_review,
        "direct semantic privacy prompt word-splits untracked filenames",
    )
    expect(
        'cat "./$f"' in system_review,
        "direct semantic privacy prompt treats dash-prefixed filenames as options",
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

def check_public_regression_tests() -> None:
    result = subprocess.run(
        [
            PYTHON,
            "-m",
            "unittest",
            "tests.test_session_log",
            "tests.test_session_replay",
            "tests.test_signal_facts",
            "tests.test_paths",
            "tests.test_autoevo_preflight",
            "tests.test_autoevo_pending",
            "tests.test_cues",
            "tests.test_lint_guards",
            "tests.test_session_replay_default",
            "tests.test_render_edges",
            "tests.test_autoevo_commit",
            "tests.test_autoevo_run",
            "tests.test_shadow_group",
            "tests.test_privacy_action",
            "tests.test_intent_routing",
            "tests.test_intent_overlay",
            "tests.test_session_stats",
            "tests.test_decay_scan",
            "tests.test_dine_rank",
            "tests.test_dining_audit",
            "tests.test_daily_brief",
            "tests.test_refresh_tracking",
            "tests.test_triage_command",
            "tests.test_reviewer_contract",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(
        result.returncode == 0,
        "public session-log/replay/signal regression tests failed\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
    )

def check_ruff_strict_core() -> None:
    """The committed ruff gate: correctness classes only, always clean.

    Soft-skips when uvx/ruff is unavailable (offline launchd runs must not
    fail on a missing linter); any lint FINDING is a real failure.
    """
    import shutil

    if shutil.which("uvx") is None:
        print("  note: uvx unavailable; ruff strict-core check skipped")
        return
    result = subprocess.run(
        ["uvx", "ruff", "check", "scripts", "tests", "--select", "F,E4,E7,E9,EXE001"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode not in (0, 1):
        print(f"  note: ruff unavailable (exit {result.returncode}); check skipped")
        return
    expect(
        result.returncode == 0,
        f"ruff strict-core violations:\n{result.stdout}",
    )
