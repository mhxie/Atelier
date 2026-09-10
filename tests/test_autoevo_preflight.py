"""Read-only preflight outcomes, source protection, and legacy-state refusal.

Every call runs in a subprocess with a disposable vault and canonical-only
path registry, so tests never load the user's private path overrides.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _make_vault(root: Path) -> Path:
    vault = root / "vault"
    for rel in ("wip", "research", "reflections", "agent-findings", "personal", "cache", "_meta"):
        (vault / rel).mkdir(parents=True)
    (vault / ".gitignore").write_text("cache/\n_meta/\n", encoding="utf-8")
    (vault / "wip" / "note.md").write_text("base\n", encoding="utf-8")
    (vault / "personal" / "diary.md").write_text("base\n", encoding="utf-8")
    _git(vault, "init", "-q")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", "base")
    return vault


def _run_py(vault: Path, body: str) -> dict:
    code = PRELUDE + textwrap.dedent(body)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "OV": str(vault.resolve()),
            "GIT_AUTHOR_NAME": "Local User",
            "GIT_AUTHOR_EMAIL": "local@example.com",
            "GIT_COMMITTER_NAME": "Local Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.com",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


PRELUDE = """
import json, sys
sys.path.insert(0, 'scripts')
import autoevo_preflight as ap
import _paths, tomllib
from pathlib import Path
_paths._registry = lambda: tomllib.loads(Path("harness/paths.toml").read_text())["paths"]
ok_probe = lambda: {"hit_count": 0, "detail": ""}
sem_probe = lambda: {"ready": True, "mode": "real", "duration_seconds": 0.01, "detail": ""}
vault = Path(__import__('os').environ['OV'])
"""


class DirtyGateScopeTest(unittest.TestCase):
    def test_publication_rechecks_live_gates_without_repeating_input_probes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            out = _run_py(vault, """
                def must_not_run():
                    raise AssertionError("expensive drafting input probe repeated")
                args = dict(vault=vault, privacy_probe=must_not_run,
                            semantic_probe=must_not_run, publication_boundary=True)
                ready = ap.inspect_preflight(**args)
                (vault / 'cache' / 'atelier-session-lock').write_text('interactive session')
                blocked = ap.inspect_preflight(**args)
                print(json.dumps({'ready': ready['ready'], 'gate': blocked['gate']}))
            """)
            self.assertTrue(out["ready"])
            self.assertEqual(out["gate"], "session_active")

    def test_wrong_or_unknown_default_branch_is_read_only_blocked(self) -> None:
        for setup in (("checkout", "-qb", "feature"), ("checkout", "--detach"), ("branch", "-m", "notes")):
            with self.subTest(setup=setup), tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
                vault = _make_vault(Path(tmp))
                _git(vault, *setup)
                before = {str(path.relative_to(vault)): path.read_bytes()
                          for path in vault.rglob("*") if path.is_file()}
                result = _run_py(vault, "print(json.dumps(ap.inspect_preflight(vault=vault, privacy_probe=ok_probe, semantic_probe=sem_probe)))")
                self.assertFalse(result["ready"])
                self.assertEqual(result["gate"], "git_not_default_branch")
                self.assertEqual(before, {str(path.relative_to(vault)): path.read_bytes()
                                          for path in vault.rglob("*") if path.is_file()})

    def test_out_of_scope_dirt_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            (vault / "personal" / "diary.md").write_text("edited\n", encoding="utf-8")
            out = _run_py(
                vault,
                """
                r = ap.inspect_preflight(vault=vault, lock_path=vault/'cache'/'lock', now=1000,
                                         privacy_probe=ok_probe, semantic_probe=sem_probe)
                print(json.dumps({"ready": r["ready"], "gate": r.get("gate"),
                                  "entries": r["health"]["worktree_entries"],
                                  "in_scope": r["health"]["worktree_entries_in_scope"]}))
                """,
            )
            self.assertTrue(out["ready"], out)
            self.assertEqual(out["entries"], 1)
            self.assertEqual(out["in_scope"], 0)

    def test_in_scope_content_dirt_protects_instead_of_blocking(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            (vault / "wip" / "note.md").write_text("edited\n", encoding="utf-8")
            (vault / "personal" / "diary.md").write_text("edited\n", encoding="utf-8")
            out = _run_py(
                vault,
                """
                r = ap.inspect_preflight(vault=vault, lock_path=vault/'cache'/'lock', now=1000,
                                         privacy_probe=ok_probe, semantic_probe=sem_probe)
                print(json.dumps({"ready": r["ready"], "gate": r.get("gate"), "detail": r.get("detail"),
                                  "in_scope": r["health"]["worktree_entries_in_scope"],
                                  "protected": r["health"].get("protected_paths", [])}))
                """,
            )
            # A note the user is editing makes the file untouchable for the
            # run; it no longer stops the sweep. Blocking on it meant the bot
            # never ran after a work day.
            self.assertTrue(out["ready"], out)
            self.assertIsNone(out["gate"])
            self.assertEqual(out["in_scope"], 1)
            self.assertEqual(out["protected"], ["wip/note.md"])

    def test_rename_out_of_scope_still_protects_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            _git(vault, "mv", "wip/note.md", "personal/note.md")
            out = _run_py(
                vault,
                """
                r = ap.inspect_preflight(vault=vault, lock_path=vault/'cache'/'lock', now=1000,
                                         privacy_probe=ok_probe, semantic_probe=sem_probe)
                print(json.dumps({"ready": r["ready"], "gate": r.get("gate"), "detail": r.get("detail", ""),
                                  "in_scope": r["health"]["worktree_entries_in_scope"]}))
                """,
            )
            self.assertTrue(out["ready"], out)
            self.assertEqual(out["in_scope"], 1)


class ReadOnlyReadinessTest(unittest.TestCase):
    def test_ready_and_blocked_cli_leave_files_and_git_unchanged(self) -> None:
        for blocked in (False, True):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
                vault = _make_vault(Path(tmp))
                if blocked:
                    (vault / ".git" / "index.lock").touch()
                before = {p.relative_to(vault): p.read_bytes() if p.is_file() else None for p in vault.rglob("*")}
                out = _run_py(vault, """
                    ap._default_privacy_probe = ok_probe
                    ap._default_semantic_probe = sem_probe
                    raise SystemExit(ap.main(["--json"]))
                """)
                self.assertEqual(out["ready"], not blocked, out)
                if blocked:
                    self.assertEqual(out["gate"], "git_index_lock_present")
                self.assertEqual(
                    before,
                    {p.relative_to(vault): p.read_bytes() if p.is_file() else None for p in vault.rglob("*")},
                )
                self.assertNotIn("output_file", out)
                self.assertNotIn("owned_audit_recovery", out)

    def test_git_operation_is_preserved_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            marker = vault / ".git" / "MERGE_HEAD"
            marker.write_text("0" * 40 + "\n", encoding="utf-8")
            out = _run_py(vault, """
                print(json.dumps(ap.inspect_preflight(
                    vault=vault, privacy_probe=ok_probe, semantic_probe=sem_probe)))
            """)
            self.assertEqual(out["gate"], "git_operation_in_progress", out)
            self.assertEqual(marker.read_text(), "0" * 40 + "\n")
            self.assertEqual(list((vault / "agent-findings").iterdir()), [])

    def test_privacy_hits_block_without_semantic_or_audit_work(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            out = _run_py(vault, """
                def must_not_run():
                    raise AssertionError("semantic probe ran after privacy failure")
                print(json.dumps(ap.inspect_preflight(
                    vault=vault, privacy_probe=lambda: {"hit_count": 2},
                    semantic_probe=must_not_run)))
            """)
            self.assertEqual(out["gate"], "privacy_hits", out)
            self.assertEqual(out["health"]["privacy_hits"], 2)
            self.assertEqual(list((vault / "agent-findings").iterdir()), [])

    def test_legacy_state_requires_review_without_reading_or_mutating_it(self) -> None:
        for contents in ("{}", "{not json"):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
                vault = _make_vault(Path(tmp))
                state = vault / "cache" / "autoevo-preflight-owned-audit.json"
                state.write_text(contents, encoding="utf-8")
                audit = vault / "agent-findings" / "autoevo-applied-2099-01-02.md"
                audit.write_text("user-owned audit edit\n", encoding="utf-8")
                head = (vault / ".git" / "index").read_bytes()
                out = _run_py(vault, """
                    from unittest.mock import patch
                    original = Path.read_text
                    def guarded(path, *args, **kwargs):
                        if path.name == ap.LEGACY_OWNED_AUDIT_STATE:
                            raise AssertionError("legacy state must not be read")
                        return original(path, *args, **kwargs)
                    with patch.object(Path, "read_text", guarded):
                        raise SystemExit(ap.main(["--json"]))
                """)
                self.assertFalse(out["ready"], out)
                self.assertEqual(out["gate"], "legacy_audit_review_required")
                self.assertIsNone(out["retry_after_epoch"])
                self.assertIn("review and migrate", out["detail"])
                self.assertEqual(state.read_text(), contents)
                self.assertEqual(audit.read_text(), "user-owned audit edit\n")
                self.assertEqual((vault / ".git" / "index").read_bytes(), head)

    def test_broken_legacy_symlink_also_requires_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            state = vault / "cache" / "autoevo-preflight-owned-audit.json"
            state.symlink_to("missing-state")
            out = _run_py(vault, 'raise SystemExit(ap.main(["--json"]))')
            self.assertEqual(out["gate"], "legacy_audit_review_required", out)
            self.assertTrue(state.is_symlink())

    def test_legacy_stat_error_defers_instead_of_ignoring_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            out = _run_py(vault, """
                from unittest.mock import patch
                original = Path.lstat
                def unavailable(path, *args, **kwargs):
                    if path.name == ap.LEGACY_OWNED_AUDIT_STATE:
                        raise OSError("fixture storage unavailable")
                    return original(path, *args, **kwargs)
                with patch.object(Path, "lstat", unavailable):
                    raise SystemExit(ap.main(["--json"]))
            """)
            self.assertEqual(out["gate"], "environment_unavailable", out)
            self.assertIsInstance(out["retry_after_epoch"], int)
            self.assertIn("legacy audit state", out["detail"])

    def test_environment_failure_is_structured_and_deferred(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            out = _run_py(vault, """
                from unittest.mock import patch
                with patch.object(ap, "inspect_preflight", side_effect=ap.PreflightError("fixture timeout")):
                    raise SystemExit(ap.main(["--json"]))
            """)
            self.assertFalse(out["ready"])
            self.assertEqual(out["gate"], "environment_unavailable")
            self.assertIsInstance(out["retry_after_epoch"], int)
            self.assertEqual(list((vault / "agent-findings").iterdir()), [])

    def test_environment_retry_delay_is_stable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            out = _run_py(vault, 'print(json.dumps(ap.environment_blocker(OSError("fixture timeout"), now=1000)))')
            self.assertEqual(out["retry_after_epoch"], 4600)

    def test_obsolete_write_and_identity_flags_are_rejected(self) -> None:
        for flag in ("--record-blocker", "--result-file", "--run-date", "--cycle", "--run-ts", "--dirty-scope"):
            with self.subTest(flag=flag):
                proc = subprocess.run(
                    [sys.executable, "scripts/autoevo_preflight.py", flag],
                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertIn("unrecognized arguments", proc.stderr)


if __name__ == "__main__":
    unittest.main()
