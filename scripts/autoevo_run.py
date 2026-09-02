#!/usr/bin/env python3
"""Deterministic mechanics of /autoevo-nightly.

The nightly command previously carried ~480 lines of inline bash the model
re-read and re-executed every run. This script owns the mechanical steps;
`.claude/commands/autoevo-nightly.md` keeps the judgment (dispatch, routing,
Curator calls) and calls these subcommands:

  plan            gates + path bindings + rotation + quarantine filter
  outcome         record one dispatch outcome in the outcomes sidecar
  tombstone-check both revert-tombstone layers for one candidate cluster
  snapshot        all-or-nothing source snapshots + oldest-mtime target pick
  stage-merge     stage a merge op with the staged-set sanity check
  archive-target  derive/validate the archive path for a low-signal op
  rollback        restore one failed op's declared paths to pre-op state

Every subcommand prints one JSON object. Errors exit 2 with {"error": ...}.
Filenames (outcomes sidecar, snapshot slugs, quarantine files) are pinned by
`scripts/autoevo_verify.py`; do not rename them here without updating the
verifier and `tests/test_autoevo_run.py`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import tier, vault_root  # noqa: E402
from autoevo_commit import cluster_hash  # noqa: E402
from autoevo_preflight import (  # noqa: E402
    partition_dirty_scope,
    _inside_worktree,
    _status_entries,
    autoevo_scope_prefixes,
)
from autoevo_quarantine import active_scopes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SESSION_LOCK_MAX_AGE_S = 21600
RESEARCH_EXCLUDED_SUBDIRS = ("cache", "images", "raw")
TOMBSTONE_WINDOW = "90 days ago"


def _fail(message: str, code: int = 2) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False))
    return code


def _emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _path_slug(rel: str) -> str:
    """wip/foo.md -> wip-foo (collision-safe across same-basename notes)."""
    slug = rel[:-3] if rel.endswith(".md") else rel
    return slug.replace("/", "-")


def _snapshot_path(cache: Path, run_ts: str, rel: str) -> Path:
    return cache / f"autoevo-{run_ts}-{_path_slug(rel)}.md"


# --- plan -------------------------------------------------------------------


def _gate_blockers(vault: Path, cache: Path, now: float) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []

    lock = cache / "atelier-session-lock"
    if lock.exists():
        age = max(0, int(now - lock.stat().st_mtime))
        if age < SESSION_LOCK_MAX_AGE_S:
            blockers.append(
                {
                    "gate": "session_lock_fresh",
                    "detail": f"session-active lock fresh (age {age}s < {SESSION_LOCK_MAX_AGE_S}s)",
                }
            )

    if not _inside_worktree(vault):
        blockers.append(
            {
                "gate": "git_not_worktree",
                "detail": "$OV is not a git work tree (no recovery surface)",
            }
        )
        return blockers  # index/status checks are meaningless without a repo

    index = _git(vault, "rev-parse", "--git-path", "index").stdout.strip()
    index_path = Path(index) if index.startswith("/") else vault / index
    lock_path = index_path.with_name(index_path.name + ".lock")
    if not index_path.is_file():
        blockers.append(
            {
                "gate": "git_index_missing",
                "detail": "Git index missing; refuse status-based classification",
            }
        )
    if lock_path.exists():
        blockers.append(
            {
                "gate": "git_index_lock_present",
                "detail": "Git index.lock present; never delete or replace it",
            }
        )
    if blockers and blockers[-1]["gate"] in {"git_index_missing", "git_index_lock_present"}:
        return blockers

    prefixes = autoevo_scope_prefixes(vault)
    blocking, _protected = partition_dirty_scope(
        [path for _, path in _status_entries(vault)], prefixes
    )
    if blocking:
        blockers.append(
            {
                "gate": "dirty_autoevo_state",
                "detail": (
                    f"{len(blocking)} Git status entries in autoevo state "
                    "(_meta/autoevo_*.toml); the queue condition is unknown"
                ),
            }
        )

    zettelm = vault / "zettelm"
    if zettelm.is_dir():
        zm = _git(zettelm, "status", "--porcelain")
        entries = [line for line in zm.stdout.splitlines() if line.strip()]
        if zm.returncode == 0 and entries:
            blockers.append(
                {
                    "gate": "zettelm_dirty",
                    "detail": f"dirty zettelm submodule ({len(entries)} entries)",
                }
            )

    privacy = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "privacy_check.py"), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    try:
        doc = json.loads(privacy.stdout)
    except json.JSONDecodeError:
        blockers.append(
            {
                "gate": "privacy_gate_error",
                "detail": f"privacy_check emitted no JSON: {privacy.stderr.strip()[:160]}",
            }
        )
    else:
        hits = doc.get("hit_count", len(doc.get("hits", [])))
        if not doc.get("zk_missing") and not doc.get("vacuous_gate") and hits:
            blockers.append(
                {
                    "gate": "privacy_hits",
                    "detail": f"privacy_check found {hits} hits",
                }
            )
    return blockers


def cmd_plan(args: argparse.Namespace) -> int:
    vault = vault_root()
    run_date = date.fromisoformat(args.run_date)
    cache, archive, findings = tier("cache"), tier("archive"), tier("agent_findings")
    findings_rel = str(findings.relative_to(vault))

    blockers = _gate_blockers(vault, cache, time.time())
    payload: dict = {
        "run_ts": args.run_ts,
        "run_date": args.run_date,
        "paths": {
            "cache": str(cache),
            "archive": str(archive),
            "findings": str(findings),
            "findings_rel": findings_rel,
            "audit_rel": f"{findings_rel}/autoevo-applied-{args.run_date}.md",
            "quarantine_state": str(vault / "_meta" / "autoevo_quarantine.toml"),
        },
    }
    if blockers:
        payload["gate"] = {"status": "blocked", "blockers": blockers}
        return _emit(payload)
    payload["gate"] = {"status": "ready", "blockers": []}

    quarantined = set(
        active_scopes(
            state_path=vault / "_meta" / "autoevo_quarantine.toml", today=run_date
        )
    )
    notes: list[str] = []
    skipped_lines: list[str] = []

    research_dir = tier("research")
    subdirs = []
    if research_dir.is_dir():
        for child in sorted(research_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name in RESEARCH_EXCLUDED_SUBDIRS:
                continue
            subdirs.append(str(child))
    live_subdirs = []
    for scope in subdirs:
        if scope in quarantined:
            skipped_lines.append(
                f"scope_quarantined: scope={scope} (research-tier rotation)"
            )
        else:
            live_subdirs.append(scope)

    research_tonight = ""
    if not subdirs:
        notes.append(f"research_rotation_empty: no eligible subdirs in {research_dir}")
    elif not live_subdirs:
        notes.append("research_all_quarantined")
    else:
        dom = run_date.day
        research_tonight = live_subdirs[(dom - 1) % len(live_subdirs)]
        notes.append(
            f"research rotation: night {dom} -> {Path(research_tonight).name} "
            f"(of {len(live_subdirs)} live subdirs; full sweep every {len(live_subdirs)} nights)"
        )

    dispatches = []
    for scope, slug, cap in (
        (str(tier("wip")), "wip", 12),
        (research_tonight, Path(research_tonight).name if research_tonight else "", 15),
        (str(tier("reflections")), "reflections", 12),
    ):
        if not scope:
            continue
        if scope in quarantined:
            label = "wip" if slug == "wip" else "reflections" if slug == "reflections" else slug
            skipped_lines.append(f"scope_quarantined: scope={scope} ({label})")
            continue
        dispatches.append(
            {
                "scope": scope,
                "slug": slug,
                "max_candidates": cap,
                "time_budget_s": 240,
            }
        )

    outcomes_file = cache / f"autoevo-{args.run_ts}-outcomes.json"
    outcomes_file.write_text("{}", encoding="utf-8")
    skipped_file = cache / f"autoevo-{args.run_ts}-quarantine-skipped.txt"
    skipped_file.write_text(
        "".join(line + "\n" for line in skipped_lines), encoding="utf-8"
    )

    _, protected = partition_dirty_scope(
        [path for _, path in _status_entries(vault)], autoevo_scope_prefixes(vault)
    )
    protected_file = cache / f"autoevo-{args.run_ts}-protected.txt"
    protected_file.write_text(
        "".join(line + "\n" for line in protected), encoding="utf-8"
    )
    if protected:
        notes.append(
            f"protected_dirty: {len(protected)} in-scope paths carry uncommitted "
            "user edits and are untouchable this run"
        )

    payload.update(
        {
            "protected_paths": protected,
            "protected_file": str(protected_file),
            "dispatches": dispatches,
            "quarantine_skipped": skipped_lines,
            "quarantine_skipped_file": str(skipped_file),
            "outcomes_file": str(outcomes_file),
            "notes": notes,
        }
    )
    return _emit(payload)


# --- outcome ----------------------------------------------------------------


def cmd_outcome(args: argparse.Namespace) -> int:
    path = Path(args.file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(f"cannot read outcomes sidecar: {exc}")
    if args.result not in {"envelope_returned", "forgetter_no_envelope"}:
        return _fail(f"invalid outcome: {args.result}")
    data[args.scope.rstrip("/")] = args.result
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return _emit({"recorded": args.scope.rstrip("/"), "result": args.result})


# --- tombstone-check --------------------------------------------------------


def cmd_tombstone_check(args: argparse.Namespace) -> int:
    vault = vault_root()
    chash = cluster_hash(args.source)
    today = date.fromisoformat(args.today)

    # Layer A: a prior [autoevo:*] commit for this cluster that the user
    # reverted. `git revert` records the FULL original sha in the revert
    # body, so match against %B, not the subject line.
    shas = _git(
        vault, "log", f"--since={TOMBSTONE_WINDOW}", "--grep=^\\[autoevo:", "--format=%H"
    ).stdout.split()
    for sha in shas:
        body = _git(vault, "show", "-s", "--format=%b", sha).stdout
        original = next(
            (
                line.split(":", 1)[1].strip()
                for line in body.splitlines()
                if line.startswith("cluster_hash:")
            ),
            "",
        )
        if original != chash:
            continue
        reverts = _git(
            vault,
            "log",
            f"--since={TOMBSTONE_WINDOW}",
            '--grep=^Revert "',
            "--format=%H %B",
        ).stdout
        if sha in reverts:
            short = _git(vault, "rev-parse", "--short=7", sha).stdout.strip()
            return _emit(
                {
                    "skip": True,
                    "cluster_hash": chash,
                    "reason": f"tombstoned cluster - user reverted {short}",
                }
            )

    # Layer B: explicit TOML tombstones.
    tomb_file = vault / "_meta" / "autoevo_tombstones.toml"
    if tomb_file.is_file():
        import tomllib

        try:
            data = tomllib.loads(tomb_file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return _fail(f"cannot read tombstones: {exc}")
        for entry in data.get("tombstone", []):
            if entry.get("cluster_hash") != chash:
                continue
            expires = str(entry.get("expires_at", "") or "")
            if expires and expires < today.isoformat():
                continue
            return _emit(
                {
                    "skip": True,
                    "cluster_hash": chash,
                    "reason": f"explicit tombstone: {entry.get('reason', 'no reason given')}",
                }
            )
    return _emit({"skip": False, "cluster_hash": chash})


# --- snapshot ---------------------------------------------------------------


def cmd_snapshot(args: argparse.Namespace) -> int:
    vault = vault_root()
    cache = tier("cache")
    snapshots: list[str] = []
    stats: list[tuple[float, str]] = []
    for rel in args.source:
        src = vault / rel
        if not src.is_file():
            return _fail(f"source missing on disk: {rel}")
        snap = _snapshot_path(cache, args.run_ts, rel)
        try:
            snap.write_bytes(src.read_bytes())
        except OSError as exc:
            return _fail(f"snapshot failed for {rel}: {exc}")
        snapshots.append(str(snap))
        stats.append((src.stat().st_mtime, rel))
    # Oldest mtime wins the surviving slug (preserves inbound wikilinks).
    target_rel = min(stats)[1] if stats else ""
    return _emit({"snapshots": snapshots, "target_rel": target_rel})


# --- stage-merge ------------------------------------------------------------


def cmd_stage_merge(args: argparse.Namespace) -> int:
    vault = vault_root()
    added = _git(vault, "add", "--", args.target)
    if added.returncode != 0:
        return _fail(f"git add failed: {added.stderr.strip()}")
    for rel in args.source:
        if rel == args.target:
            continue  # the survivor stays
        removed = _git(vault, "rm", "--", rel)
        if removed.returncode != 0:
            return _fail(f"git rm failed for {rel}: {removed.stderr.strip()}")
    staged = set(
        _git(vault, "diff", "--cached", "--name-only").stdout.splitlines()
    )
    expected = set(args.source) | {args.target}
    if staged != expected:
        _git(vault, "restore", "--staged", "--", *sorted(expected))
        return _fail(
            "staged paths diverged from expected: "
            f"staged={sorted(staged)} expected={sorted(expected)}"
        )
    return _emit({"staged": sorted(staged)})


# --- archive-target ---------------------------------------------------------


def cmd_archive_target(args: argparse.Namespace) -> int:
    vault = vault_root()
    archive_rel = str(tier("archive").relative_to(vault))
    target_rel = f"{archive_rel}/decayed/{args.run_date}-{_path_slug(args.source)}.md"
    if (vault / target_rel).exists():
        return _fail(f"archive target exists: {target_rel}")
    (vault / archive_rel / "decayed").mkdir(parents=True, exist_ok=True)
    return _emit({"target_rel": target_rel})


# --- rollback ---------------------------------------------------------------


def cmd_rollback(args: argparse.Namespace) -> int:
    vault = vault_root()
    cache = tier("cache")
    _git(vault, "restore", "--staged", "--", *args.paths)
    restored, removed, recovered = [], [], []
    for rel in args.paths:
        in_head = (
            _git(vault, "cat-file", "-e", f"HEAD:{rel}").returncode == 0
        )
        if in_head:
            _git(vault, "restore", "--worktree", "--", rel)
            restored.append(rel)
        elif (vault / rel).is_file():
            # Created by the failed op; never existed at HEAD.
            (vault / rel).unlink()
            removed.append(rel)
    for rel in args.source:
        if (vault / rel).is_file():
            continue
        snap = _snapshot_path(cache, args.run_ts, rel)
        if snap.is_file():
            (vault / rel).write_bytes(snap.read_bytes())
            recovered.append(rel)
    return _emit(
        {"restored": restored, "removed": removed, "recovered_from_snapshot": recovered}
    )


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument("--run-ts", required=True)
    plan.add_argument("--run-date", required=True)
    plan.set_defaults(func=cmd_plan)

    outcome = sub.add_parser("outcome")
    outcome.add_argument("--file", required=True)
    outcome.add_argument("--scope", required=True)
    outcome.add_argument(
        "--result", required=True, help="envelope_returned | forgetter_no_envelope"
    )
    outcome.set_defaults(func=cmd_outcome)

    tomb = sub.add_parser("tombstone-check")
    tomb.add_argument("--source", action="append", required=True)
    tomb.add_argument("--today", required=True)
    tomb.set_defaults(func=cmd_tombstone_check)

    snap = sub.add_parser("snapshot")
    snap.add_argument("--run-ts", required=True)
    snap.add_argument("--source", action="append", required=True)
    snap.set_defaults(func=cmd_snapshot)

    stage = sub.add_parser("stage-merge")
    stage.add_argument("--target", required=True)
    stage.add_argument("--source", action="append", required=True)
    stage.set_defaults(func=cmd_stage_merge)

    arch = sub.add_parser("archive-target")
    arch.add_argument("--source", required=True)
    arch.add_argument("--run-date", required=True)
    arch.set_defaults(func=cmd_archive_target)

    roll = sub.add_parser("rollback")
    roll.add_argument("--run-ts", required=True)
    roll.add_argument("--paths", nargs="+", required=True)
    roll.add_argument("--source", action="append", default=[])
    roll.set_defaults(func=cmd_rollback)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # surface, never swallow (headless caller)
        return _fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
