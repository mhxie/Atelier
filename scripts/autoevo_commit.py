#!/usr/bin/env python3
"""Sole committer for autoevo's destructive-op, queue, and audit commits.

The four `git commit --only` heredocs previously lived inline in
`.claude/commands/autoevo-nightly.md` and were executed by the model, guarded
only by a substring check. Commit choreography is mechanical: message
templates, `cluster_hash`, path-limited staging, and bot identity
belong in one audited script (the same sole-writer posture as
`autoevo_pending.py`). The command doc now calls these subcommands; message
shapes are pinned by tests and consumed downstream by the revert-tombstone
walk (`cluster_hash:` line) and `git log --grep='^\\[autoevo:'`.

Every subcommand prints one JSON object: {"sha": ..., "cluster_hash": ...?}
on success, {"error": ...} with exit 1 on failure. Nothing here repairs git
state; a failed commit is reported, never retried.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import vault_root  # noqa: E402

BOT_NAME = "Atelier Autoevo Bot"
BOT_EMAIL = "noreply@atelier.local"


def cluster_hash(sources: list[str]) -> str:
    """First 12 hex chars of sha1 over the sorted unique source paths.

    Matches protocols/autoevo.md § Revert tombstones (one path per line,
    LF-terminated) so hashes stay stable across re-runs and machines.
    """
    body = "\n".join(sorted(set(sources))) + "\n"
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]


def _git(
    vault: Path, *args: str, bot_identity: bool = False
) -> subprocess.CompletedProcess[str]:
    env = None
    if bot_identity:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": BOT_NAME,
            "GIT_AUTHOR_EMAIL": BOT_EMAIL,
            "GIT_COMMITTER_NAME": BOT_NAME,
            "GIT_COMMITTER_EMAIL": BOT_EMAIL,
        }
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )


def _protected_paths() -> frozenset[str]:
    """Vault-relative paths this run must not stage.

    The dirty-tree gate no longer stops a sweep because the user was editing a
    note, so the protection moved here: the plan records the in-scope paths that
    carried uncommitted edits at claim time, and no autoevo commit may touch
    them. Deterministic, at the one choke point every autoevo commit goes
    through, rather than a rule the model is asked to remember.
    """
    raw = os.environ.get("AUTOEVO_PROTECTED_FILE", "").strip()
    if not raw:
        return frozenset()
    try:
        content = Path(raw).read_text(encoding="utf-8")
    except OSError:
        # Fail closed: a declared protection list that cannot be read must not
        # silently degrade into no protection at all.
        raise SystemExit(f"ERROR: cannot read AUTOEVO_PROTECTED_FILE: {raw}")
    return frozenset(line.strip() for line in content.splitlines() if line.strip())


def _commit(vault: Path, message: str, paths: list[str], *, force_add: list[str] | None = None) -> dict:
    protected = _protected_paths()
    if protected:
        collisions = sorted(protected.intersection(paths + list(force_add or [])))
        if collisions:
            return {
                "error": (
                    "refusing to stage paths with uncommitted user edits: "
                    + ", ".join(collisions[:5])
                )
            }
    add = _git(vault, "add", "--", *paths)
    if add.returncode != 0:
        return {"error": f"git add failed: {add.stderr.strip()[:300]}"}
    for path in force_add or []:
        forced = _git(vault, "add", "-f", "--", path)
        if forced.returncode != 0:
            return {"error": f"git add -f {path} failed: {forced.stderr.strip()[:300]}"}
    commit = _git(
        vault,
        "commit",
        "--only",
        "-m",
        message,
        "--",
        *paths,
        *(force_add or []),
        bot_identity=True,
    )
    if commit.returncode != 0:
        return {"error": f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()[:300]}"}
    sha = _git(vault, "rev-parse", "HEAD").stdout.strip()
    return {"sha": sha}


def cmd_merge(args: argparse.Namespace) -> int:
    vault = vault_root()
    chash = cluster_hash(args.source)
    source_lines = "\n".join(f"- {line}" for line in args.source_evidence or args.source)
    message = (
        f"[autoevo:redundant] {args.scope}: merge {len(args.source)} notes into {args.target_slug}\n\n"
        f"Source notes:\n{source_lines}\n\n"
        f"Auto-band: {args.band}\n"
        f"cluster_hash: {chash}\n"
        "Revert: git revert <future sha>"
    )
    result = _commit(vault, message, args.paths)
    if "sha" in result:
        result["cluster_hash"] = chash
    print(json.dumps(result, sort_keys=True))
    return 0 if "sha" in result else 1


def cmd_archive(args: argparse.Namespace) -> int:
    vault = vault_root()
    message = (
        f"[autoevo:low-signal] archive: {args.slug} after {args.days_inactive} days inactive\n\n"
        f"{args.evidence}\n"
        f"Moved: {args.source} -> {args.target}\n\n"
        f"Auto-band: {args.band}"
    )
    result = _commit(vault, message, [args.source, args.target])
    print(json.dumps(result, sort_keys=True))
    return 0 if "sha" in result else 1


def cmd_queue(args: argparse.Namespace) -> int:
    vault = vault_root()
    message = (
        f"[autoevo:queue] _meta: {args.summary}\n\n"
        f"{args.detail}"
    )
    result = _commit(vault, message, [args.queue_path, *(args.extra_path or [])])
    print(json.dumps(result, sort_keys=True))
    return 0 if "sha" in result else 1


def cmd_audit(args: argparse.Namespace) -> int:
    vault = vault_root()
    message = (
        f"[autoevo:audit] agent-findings: record nightly run {args.run_date}\n\n"
        f"Auto-applied: {args.auto}, Pending: {args.pending}, Errors: {args.errors}, Quarantined: {args.quarantined}"
    )
    result = _commit(vault, message, args.paths, force_add=args.force_add or [])
    print(json.dumps(result, sort_keys=True))
    return 0 if "sha" in result else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("merge", help="redundant-merge op commit")
    p.add_argument("--scope", required=True, help="relative dir under $OV, e.g. wip")
    p.add_argument("--target-slug", required=True)
    p.add_argument("--band", required=True, help='e.g. "redundant-high (3+ peers >= 0.85, ...)"')
    p.add_argument("--source", action="append", required=True, help="source relative path (repeat)")
    p.add_argument("--source-evidence", action="append", help="evidence line per source (repeat, optional)")
    p.add_argument("--paths", nargs="+", required=True, help="all paths this op touched")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("archive", help="low-signal archive op commit (after git mv)")
    p.add_argument("--slug", required=True)
    p.add_argument("--days-inactive", required=True)
    p.add_argument("--evidence", required=True, help="e.g. 'words: N, links_in: 0, tags: 0, mtime: DATE'")
    p.add_argument("--source", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--band", required=True)
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("queue", help="pending-queue state commit")
    p.add_argument("--summary", required=True, help='subject tail, e.g. "append N pending findings from DATE sweep"')
    p.add_argument("--detail", required=True, help='body line, e.g. "Categories: redundant=n, ..."')
    p.add_argument("--queue-path", default="_meta/autoevo_pending.toml")
    p.add_argument(
        "--extra-path", action="append",
        help="additional path committed with the queue file (repeat), e.g. the day's audit log",
    )
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("audit", help="nightly audit-log commit")
    p.add_argument("--run-date", required=True)
    p.add_argument("--auto", required=True)
    p.add_argument("--pending", required=True)
    p.add_argument("--errors", required=True)
    p.add_argument("--quarantined", required=True)
    p.add_argument("--paths", nargs="+", required=True)
    p.add_argument("--force-add", nargs="*", help="bot-owned whitelist-ignored state files")
    p.set_defaults(func=cmd_audit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
