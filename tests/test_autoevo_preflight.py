"""Regression tests for the autoevo deterministic preflight.

Glitches (2026-08-22):

1. The dirty-tree gate counted every Git status entry in the vault although
   the bot stages explicit paths and commits with `--only`. On a Drive-synced
   vault that is dirty by design, 73 of 103 attempts were blocked and the
   bot never ran. The gate now inspects only autoevo's own scopes.
2. A Drive File Provider read error (`[Errno 11] Resource deadlock avoided`)
   while reading the owned-audit state file was classified as "requires
   review", which made the runner exit 2 and mark the claim `failed`
   (human retry approval needed). Storage and timeout errors are transient
   and must defer with an hourly retry instead.

The tests run every preflight call in a subprocess so `_paths`'s
process-wide $OV cache never leaks into other test modules.
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
from pathlib import Path
ok_probe = lambda: {"hits": 0, "detail": ""}
sem_probe = lambda: {"ready": True, "mode": "real", "duration_seconds": 0.01, "detail": ""}
vault = Path(__import__('os').environ['OV'])
"""


class DirtyGateScopeTest(unittest.TestCase):
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

    def test_dirty_scope_cli_counts_only_scoped_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            (vault / "personal" / "diary.md").write_text("edited\n", encoding="utf-8")
            (vault / "research" / "new.md").write_text("draft\n", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "scripts/autoevo_preflight.py", "--dirty-scope"],
                cwd=REPO_ROOT,
                env={**os.environ, "OV": str(vault)},
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "1")


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


class TransientErrorsDeferTest(unittest.TestCase):
    def test_recovered_audit_is_bot_authored_without_coauthor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            audit = vault / "agent-findings" / "autoevo-applied-2099-01-02.md"
            audit.write_text("## Autoevo Run\n", encoding="utf-8")
            out = _run_py(
                vault,
                """
                r = ap._commit_audit(vault, vault/'agent-findings'/'autoevo-applied-2099-01-02.md', '2099-01-02')
                author = ap._git(vault, 'log', '-1', '--format=%an <%ae>').stdout.strip()
                committer = ap._git(vault, 'log', '-1', '--format=%cn <%ce>').stdout.strip()
                body = ap._git(vault, 'log', '-1', '--format=%B').stdout
                print(json.dumps({'returncode': r.returncode, 'author': author,
                                  'committer': committer, 'body': body}))
                """,
            )
            self.assertEqual(out["returncode"], 0, out)
            self.assertEqual(
                out["author"], "Atelier Autoevo Bot <noreply@atelier.local>"
            )
            self.assertEqual(
                out["committer"], "Atelier Autoevo Bot <noreply@atelier.local>"
            )
            self.assertNotIn("Co-Authored-By:", out["body"])

    def test_unreadable_owned_state_defers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            state = vault / "cache" / "autoevo-preflight-owned-audit.json"
            state.write_text("{}", encoding="utf-8")
            state.chmod(0)
            try:
                out = _run_py(vault, "print(json.dumps(ap.recover_owned_audit()))")
            finally:
                state.chmod(0o600)
            self.assertEqual(out["status"], "deferred", out)
            self.assertIn("unreadable", out["detail"])

    def test_corrupt_owned_state_still_requires_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            state = vault / "cache" / "autoevo-preflight-owned-audit.json"
            state.write_text("{not json", encoding="utf-8")
            out = _run_py(vault, "print(json.dumps(ap.recover_owned_audit()))")
            self.assertEqual(out["status"], "invalid", out)

    def test_environment_blocker_defers_and_records_audit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            out = _run_py(
                vault,
                """
                r = ap.environment_blocker(ap.PreflightError("cannot run git: timed out"), now=1000)
                rec = ap.record_blocker(r, run_date="2099-01-02", run_ts="t1", cycle="2099-01-02")
                audit = Path(rec["output_file"]) if Path(rec["output_file"]).is_absolute() else vault / rec["output_file"]
                print(json.dumps({"ready": r["ready"], "gate": r["gate"],
                                  "retry": r["retry_after_epoch"],
                                  "audit_commit": rec["audit_commit"],
                                  "audit_text": audit.read_text()}))
                """,
            )
            self.assertFalse(out["ready"])
            self.assertEqual(out["gate"], "environment_unavailable")
            self.assertEqual(out["retry"], 1000 + 3600)
            self.assertIn(out["audit_commit"], {"committed", "deferred"})
            self.assertIn("environment_unavailable: preflight could not inspect the vault", out["audit_text"])

    def test_deferred_recovery_blocks_the_cycle(self) -> None:
        """An unreadable owned-audit state must defer the run, not let it start."""
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            state = vault / "cache" / "autoevo-preflight-owned-audit.json"
            state.write_text("{}", encoding="utf-8")
            state.chmod(0)
            try:
                proc = subprocess.run(
                    [sys.executable, "scripts/autoevo_preflight.py", "--json", "--run-date", "2099-01-02", "--cycle", "2099-01-02"],
                    cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)}, capture_output=True, text=True, timeout=120,
                )
            finally:
                state.chmod(0o600)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertFalse(out["ready"], out)
            self.assertEqual(out["gate"], "audit_recovery_deferred")
            self.assertIsInstance(out["retry_after_epoch"], int)

    def test_lfs_timeout_is_health_not_crash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-preflight-") as tmp:
            vault = _make_vault(Path(tmp))
            out = _run_py(
                vault,
                """
                def boom(*a, **k):
                    raise ap.PreflightError("cannot run git: timed out after 45 seconds")
                ap._git = boom
                print(json.dumps(ap._lfs_health(vault)))
                """,
            )
            self.assertFalse(out["available"])
            self.assertIn("timed out", out["detail"])


if __name__ == "__main__":
    unittest.main()
