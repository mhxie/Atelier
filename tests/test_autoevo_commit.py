"""Trusted Autoevo publication tests."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import autoevo_commit as publisher  # noqa: E402


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )


class AutoevoCommitTest(unittest.TestCase):
    def test_cluster_hash_is_order_insensitive(self) -> None:
        snippet = (
            "import sys; sys.path.insert(0, 'scripts'); import autoevo_commit as a; "
            "print(a.cluster_hash(['wip/b.md', 'wip/a.md']) == a.cluster_hash(['wip/a.md', 'wip/b.md']))"
        )
        proc = subprocess.run([sys.executable, "-c", snippet], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "True", proc.stderr)


class TrustedPublisherTest(unittest.TestCase):
    def test_default_branch_guard_rechecks_same_head_checkout(self) -> None:
        for switch in (("checkout", "-qb", "feature"), ("checkout", "--detach")):
            with self.subTest(switch=switch), tempfile.TemporaryDirectory() as tmp:
                vault, head = self._vault(tmp)
                with self.assertRaises(publisher.PublicationError) as raised:
                    self._publish(vault, head, {"wip/a.md": self._change("a\n", "A\n")},
                                  "branch-guard", recheck=lambda: _git(vault, *switch))
                self.assertFalse(raised.exception.publication_started)
                self.assertEqual((vault / "wip/a.md").read_text(), "a\n")
                self.assertEqual(_git(vault, "rev-parse", "HEAD").stdout.strip(), head)

    def test_declared_remote_default_accepts_nonstandard_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, head = self._vault(tmp)
            _git(vault, "branch", "-m", "notes")
            _git(vault, "update-ref", "refs/remotes/origin/notes", head)
            _git(vault, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/notes")
            result = self._publish(vault, head, {"wip/a.md": self._change("a\n", "A\n")}, "named-default")
            self.assertEqual(result["parent"], head)

    def _vault(self, tmp: str) -> tuple[Path, str]:
        vault = Path(tmp) / "vault"
        (vault / "wip").mkdir(parents=True)
        for name, body in {
            "a.md": "a\n",
            "b.md": "b\n",
            "staged.md": "staged base\n",
            "unstaged.md": "unstaged base\n",
        }.items():
            (vault / "wip" / name).write_text(body, encoding="utf-8")
        _git(vault, "init", "-q")
        _git(vault, "add", "-A")
        _git(vault, "commit", "-q", "-m", "base")
        return vault, _git(vault, "rev-parse", "HEAD").stdout.strip()

    @staticmethod
    def _change(before: str | None, after: str | None) -> dict[str, object]:
        return {
            "before_sha256": _sha256(before) if before is not None else None,
            "after": after,
            "mode": 0o644,
        }

    def _publish(
        self,
        vault: Path,
        head: str,
        changes: dict[str, dict[str, object]],
        candidate_id: str,
        **kwargs: object,
    ) -> dict[str, object]:
        return publisher.publish_changes(
            vault,
            changes=changes,
            message="[autoevo:fixture] publish one accepted operation",
            candidate_id=candidate_id,
            expected_head=head,
            allowed_prefixes=("wip/",),
            protected_paths=set(),
            **kwargs,
        )

    def test_publish_is_path_limited_and_preserves_unrelated_dirt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, head = self._vault(tmp)
            (vault / "wip/staged.md").write_text("user staged\n", encoding="utf-8")
            _git(vault, "add", "wip/staged.md")
            (vault / "wip/unstaged.md").write_text("user unstaged\n", encoding="utf-8")
            changes = {
                "wip/a.md": self._change("a\n", "A published\n"),
                "wip/b.md": self._change("b\n", None),
                "wip/new.md": self._change(None, "new\n"),
            }

            evidence = self._publish(vault, head, changes, "cycle-1-op-1")

            self.assertEqual(evidence["parent"], head)
            self.assertEqual(evidence["paths"], sorted(changes))
            self.assertEqual(
                sorted(_git(vault, "show", "--name-only", "--format=", "HEAD").stdout.split()),
                sorted(changes),
            )
            self.assertEqual(_git(vault, "diff", "--cached", "--name-only").stdout.split(), ["wip/staged.md"])
            self.assertEqual(_git(vault, "diff", "--name-only").stdout.split(), ["wip/unstaged.md"])
            body = _git(vault, "show", "-s", "--format=%B", "HEAD").stdout
            self.assertEqual(body.splitlines().count("Autoevo-candidate: cycle-1-op-1"), 1)

    def test_archive_publication_creates_a_missing_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, head = self._vault(tmp)
            target = "archive/decayed/2099-a.md"
            changes = {
                "wip/a.md": self._change("a\n", None),
                target: self._change(None, "a\n"),
            }

            evidence = publisher.publish_changes(
                vault,
                changes=changes,
                message="[autoevo:low-signal] archive missing-parent fixture",
                candidate_id="cycle-1-op-archive",
                expected_head=head,
                allowed_prefixes=("wip/a.md", "archive/decayed/"),
                protected_paths=set(),
            )

            self.assertEqual(evidence["paths"], sorted(changes))
            self.assertFalse((vault / "wip/a.md").exists())
            self.assertEqual((vault / target).read_text(encoding="utf-8"), "a\n")

    def test_private_modes_survive_publication_and_git_reconciliation(self) -> None:
        for mode, git_mode in ((0o600, "100644"), (0o700, "100755")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                vault, _ = self._vault(tmp)
                path = vault / "wip/a.md"
                path.chmod(mode)
                _git(vault, "add", "--", "wip/a.md")
                _git(vault, "commit", "--allow-empty", "-qm", "private source mode")
                head = _git(vault, "rev-parse", "HEAD").stdout.strip()
                changes = {"wip/a.md": {**self._change("a\n", "private update\n"), "mode": mode}}
                proof = self._publish(vault, head, changes, "private-mode")
                self.assertEqual(path.stat().st_mode & 0o777, mode)
                self.assertTrue(_git(vault, "ls-tree", "HEAD", "--", "wip/a.md").stdout.startswith(git_mode))
                self.assertEqual(publisher.reconcile_commit(vault, candidate_id="private-mode", expected_head=head,
                                                           changes=changes), proof)

    def test_path_validation_rejects_git_metadata_controls_and_protected_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, head = self._vault(tmp)
            for candidate_id, relative in (
                ("cycle-1-op-git", "wip/.git/config"),
                ("cycle-1-op-control", "wip/bad\x1fname.md"),
            ):
                with self.subTest(relative=relative), self.assertRaises(publisher.PublicationError) as raised:
                    self._publish(vault, head, {relative: self._change(None, "unsafe\n")}, candidate_id)
                self.assertFalse(raised.exception.publication_started)

            with self.assertRaisesRegex(publisher.PublicationError, "candidate path is protected") as protected:
                publisher.publish_changes(
                    vault,
                    changes={"wip/a.md": self._change("a\n", "agent\n")},
                    message="[autoevo:fixture] protected directory",
                    candidate_id="cycle-1-op-protected",
                    expected_head=head,
                    allowed_prefixes=("wip/",),
                    protected_paths={"wip/"},
                )
            self.assertFalse(protected.exception.publication_started)

    def test_source_drift_target_collision_and_source_symlink_reject_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, head = self._vault(tmp)
            (vault / "wip/a.md").write_text("user drift\n", encoding="utf-8")
            with self.assertRaises(publisher.PublicationError) as drift:
                self._publish(vault, head, {"wip/a.md": self._change("a\n", "agent\n")}, "cycle-2-op-1")
            self.assertFalse(drift.exception.publication_started)
            self.assertIn("source changed", str(drift.exception))
            self.assertEqual(_git(vault, "rev-parse", "HEAD").stdout.strip(), head)

            _git(vault, "restore", "wip/a.md")
            (vault / "wip/new.md").write_text("user file\n", encoding="utf-8")
            with self.assertRaises(publisher.PublicationError) as collision:
                self._publish(vault, head, {"wip/new.md": self._change(None, "agent\n")}, "cycle-2-op-2")
            self.assertFalse(collision.exception.publication_started)
            self.assertIn("target already exists", str(collision.exception))

            (vault / "wip/new.md").unlink()
            (vault / "wip/a.md").unlink()
            (vault / "wip/a.md").symlink_to(vault / "wip/b.md")
            with self.assertRaises(publisher.PublicationError) as linked:
                self._publish(vault, head, {"wip/a.md": self._change("a\n", "agent\n")}, "cycle-2-op-3")
            self.assertFalse(linked.exception.publication_started)
            self.assertIn("not a regular file", str(linked.exception))

    def test_hardlinked_source_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, head = self._vault(tmp)
            outside = Path(tmp) / "outside.md"
            outside.write_text("a\n", encoding="utf-8")
            (vault / "wip/a.md").unlink()
            os.link(outside, vault / "wip/a.md")
            with self.assertRaises(publisher.PublicationError) as raised:
                self._publish(vault, head, {"wip/a.md": self._change("a\n", "agent\n")}, "cycle-3-op-1")
            self.assertFalse(raised.exception.publication_started)
            self.assertIn("hard links", str(raised.exception))

    def test_failure_after_first_write_is_ambiguous_and_never_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, head = self._vault(tmp)
            changes = {
                "wip/a.md": self._change("a\n", "A published\n"),
                "wip/b.md": self._change("b\n", "B published\n"),
            }
            original = publisher._write_file
            calls = 0

            def fail_second(root: Path, change: publisher._CandidateChange) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic second-write failure")
                original(root, change)

            with mock.patch.object(publisher, "_write_file", side_effect=fail_second):
                with self.assertRaises(publisher.PublicationError) as raised:
                    self._publish(vault, head, changes, "cycle-4-op-1")
            self.assertTrue(raised.exception.publication_started)
            self.assertEqual((vault / "wip/a.md").read_text(encoding="utf-8"), "A published\n")
            self.assertEqual((vault / "wip/b.md").read_text(encoding="utf-8"), "b\n")
            self.assertEqual(_git(vault, "rev-parse", "HEAD").stdout.strip(), head)

    def test_hook_failure_is_ambiguous_and_never_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, head = self._vault(tmp)
            hook = vault / ".git/hooks/pre-commit"
            hook.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
            hook.chmod(0o755)
            with self.assertRaises(publisher.PublicationError) as raised:
                self._publish(vault, head, {"wip/a.md": self._change("a\n", "A published\n")}, "cycle-5-op-1")
            self.assertTrue(raised.exception.publication_started)
            self.assertIn("git commit failed", str(raised.exception))
            self.assertEqual((vault / "wip/a.md").read_text(encoding="utf-8"), "A published\n")
            self.assertEqual(_git(vault, "rev-parse", "HEAD").stdout.strip(), head)

    def test_reconciliation_proves_commit_and_per_op_revert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, head = self._vault(tmp)
            changes = {"wip/a.md": self._change("a\n", "A published\n")}
            evidence = self._publish(vault, head, changes, "cycle-6-op-1")
            self.assertEqual(
                publisher.reconcile_commit(
                    vault,
                    candidate_id="cycle-6-op-1",
                    expected_head=head,
                    changes=changes,
                ),
                evidence,
            )
            with self.assertRaisesRegex(publisher.PublicationError, "commit evidence does not match"):
                publisher.reconcile_commit(
                    vault,
                    candidate_id="cycle-6-op-1",
                    expected_head=head,
                    changes={"wip/a.md": self._change("a\n", "different bytes\n")},
                )
            _git(vault, "revert", "--no-edit", str(evidence["sha"]))
            self.assertEqual((vault / "wip/a.md").read_text(encoding="utf-8"), "a\n")

    def test_force_add_is_exact_and_only_for_ignored_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, head = self._vault(tmp)
            (vault / ".gitignore").write_text("_meta/\n", encoding="utf-8")
            _git(vault, "add", ".gitignore")
            _git(vault, "commit", "-q", "-m", "ignore metadata")
            head = _git(vault, "rev-parse", "HEAD").stdout.strip()
            (vault / "_meta").mkdir()
            state = vault / "_meta/autoevo_pending.toml"
            state.write_text("version = 1\n", encoding="utf-8")
            changes = {
                "_meta/autoevo_pending.toml": self._change("version = 1\n", "version = 1\n# updated\n")
            }
            evidence = publisher.publish_changes(
                vault,
                changes=changes,
                message="[autoevo:queue] update accepted operation",
                candidate_id="cycle-7-op-1",
                expected_head=head,
                allowed_prefixes=("_meta/autoevo_pending.toml",),
                protected_paths=set(),
                force_add={"_meta/autoevo_pending.toml"},
            )
            self.assertEqual(evidence["force_added"], ["_meta/autoevo_pending.toml"])
            self.assertEqual(
                _git(vault, "show", "--name-only", "--format=", "HEAD").stdout.split(),
                ["_meta/autoevo_pending.toml"],
            )
            next_head = str(evidence["sha"])
            next_changes = {
                "_meta/autoevo_pending.toml": self._change(
                    "version = 1\n# updated\n",
                    "version = 1\n# updated twice\n",
                )
            }
            second = publisher.publish_changes(
                vault,
                changes=next_changes,
                message="[autoevo:queue] update tracked ignored metadata",
                candidate_id="cycle-7-op-2",
                expected_head=next_head,
                allowed_prefixes=("_meta/autoevo_pending.toml",),
                protected_paths=set(),
                force_add={"_meta/autoevo_pending.toml"},
            )
            self.assertEqual(second["parent"], next_head)


if __name__ == "__main__":
    unittest.main()
