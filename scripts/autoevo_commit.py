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
from _git import BOT_EMAIL, BOT_NAME, run_git  # noqa: E402,F401  (identity re-exported for callers)


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
    return run_git(vault, *args, timeout=120, bot_identity=bot_identity)


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
    # Stage what still exists; a path already removed by `git rm` / `git mv`
    # is neither on disk nor in the index and would make `git add` fail, while
    # a path deleted on disk but still tracked needs `-A` to stage the deletion.
    present = [p for p in paths if (vault / p).exists()]
    tracked_gone = [
        p for p in paths
        if not (vault / p).exists()
        and _git(vault, "ls-files", "--error-unmatch", "--", p).returncode == 0
    ]
    if present:
        add = _git(vault, "add", "--", *present)
        if add.returncode != 0:
            return {"error": f"git add failed: {add.stderr.strip()[:300]}"}
    if tracked_gone:
        gone = _git(vault, "add", "-A", "--", *tracked_gone)
        if gone.returncode != 0:
            return {"error": f"git add -A failed: {gone.stderr.strip()[:300]}"}
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


def merge_commit(
    vault: Path, *, scope: str, target_slug: str, band: str, sources: list[str],
    paths: list[str], source_evidence: list[str] | None = None,
) -> dict:
    chash = cluster_hash(sources)
    source_lines = "\n".join(f"- {line}" for line in source_evidence or sources)
    message = (
        f"[autoevo:redundant] {scope}: merge {len(sources)} notes into {target_slug}\n\n"
        f"Source notes:\n{source_lines}\n\n"
        f"Auto-band: {band}\n"
        f"cluster_hash: {chash}\n"
        "Revert: git revert <future sha>"
    )
    result = _commit(vault, message, paths)
    if "sha" in result:
        result["cluster_hash"] = chash
    return result


def archive_commit(
    vault: Path, *, slug: str, days_inactive: str, evidence: str, source: str, target: str, band: str,
) -> dict:
    message = (
        f"[autoevo:low-signal] archive: {slug} after {days_inactive} days inactive\n\n"
        f"{evidence}\n"
        f"Moved: {source} -> {target}\n\n"
        f"Auto-band: {band}"
    )
    return _commit(vault, message, [source, target])


def stale_commit(
    vault: Path, *, slug: str, source: str, phrase: str, entry_id: str,
    proposed_at: str, default_at: str, veto_days: str = "14",
) -> dict:
    """time-stale-A default op: the note now carries a stale banner."""
    chash = cluster_hash([source])
    message = (
        f"[autoevo:time-stale-A] stale-banner: {slug} after {veto_days}d veto window\n\n"
        f"Dated phrase: {phrase}\n"
        f"Queue entry: {entry_id} (proposed {proposed_at}, default fired {default_at})\n"
        f"Path: {source}\n\n"
        f"Auto-band: time-stale-A-default (no veto in /autoevo-review by {default_at})\n"
        f"cluster_hash: {chash}\n"
        "Revert: git revert <future sha>"
    )
    result = _commit(vault, message, [source])
    if "sha" in result:
        result["cluster_hash"] = chash
    return result


def queue_commit(vault: Path, *, summary: str, detail: str, queue_path: str, extra_paths: list[str] | None = None) -> dict:
    message = f"[autoevo:queue] _meta: {summary}\n\n{detail}"
    return _commit(vault, message, [queue_path, *(extra_paths or [])])


def audit_commit(
    vault: Path, *, run_date: str, auto: str, pending: str, errors: str, quarantined: str,
    paths: list[str], force_add: list[str] | None = None,
) -> dict:
    message = (
        f"[autoevo:audit] agent-findings: record nightly run {run_date}\n\n"
        f"Auto-applied: {auto}, Pending: {pending}, Errors: {errors}, Quarantined: {quarantined}"
    )
    return _commit(vault, message, paths, force_add=force_add or [])


def _print_result(result: dict) -> int:
    print(json.dumps(result, sort_keys=True))
    return 0 if "sha" in result else 1


def cmd_merge(args: argparse.Namespace) -> int:
    return _print_result(merge_commit(
        vault_root(), scope=args.scope, target_slug=args.target_slug, band=args.band,
        sources=args.source, paths=args.paths, source_evidence=args.source_evidence,
    ))


def cmd_archive(args: argparse.Namespace) -> int:
    return _print_result(archive_commit(
        vault_root(), slug=args.slug, days_inactive=args.days_inactive, evidence=args.evidence,
        source=args.source, target=args.target, band=args.band,
    ))


def cmd_stale(args: argparse.Namespace) -> int:
    return _print_result(stale_commit(
        vault_root(), slug=args.slug, source=args.source, phrase=args.phrase, entry_id=args.entry_id,
        proposed_at=args.proposed_at, default_at=args.default_at, veto_days=args.veto_days,
    ))


def cmd_queue(args: argparse.Namespace) -> int:
    return _print_result(queue_commit(
        vault_root(), summary=args.summary, detail=args.detail, queue_path=args.queue_path,
        extra_paths=args.extra_path,
    ))


def cmd_audit(args: argparse.Namespace) -> int:
    return _print_result(audit_commit(
        vault_root(), run_date=args.run_date, auto=args.auto, pending=args.pending, errors=args.errors,
        quarantined=args.quarantined, paths=args.paths, force_add=args.force_add,
    ))


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

    p = sub.add_parser("stale", help="time-stale-A default op commit (after stale-banner)")
    p.add_argument("--slug", required=True)
    p.add_argument("--source", required=True, help="relative path of the banner-bearing note")
    p.add_argument("--phrase", required=True, help="the dated phrase Forgetter flagged")
    p.add_argument("--entry-id", required=True)
    p.add_argument("--proposed-at", required=True)
    p.add_argument("--default-at", required=True)
    p.add_argument("--veto-days", default="14")
    p.set_defaults(func=cmd_stale)

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
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # keep the one-JSON-object contract for headless callers
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
