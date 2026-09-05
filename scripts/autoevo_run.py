#!/usr/bin/env python3
"""Deterministic mechanics of /autoevo-nightly.

The nightly command previously carried ~480 lines of inline bash the model
re-read and re-executed every run. This script owns the mechanical steps;
`.claude/commands/autoevo-nightly.md` keeps the judgment (dispatch, routing,
Curator calls) and calls these subcommands:

  identity        run identity: RUN_TS plus the validated RUN_DATE
  plan            gates + path bindings + rotation + quarantine filter
  outcome         record one dispatch outcome in the outcomes sidecar
  route-bands     trust-band routing of Forgetter rows (thresholds live here)
  tombstone-check both revert-tombstone layers for one candidate cluster
  snapshot        all-or-nothing source snapshots + oldest-mtime target pick
  verify-snapshot refuse an op whose sources changed since their snapshot
  stage-merge     stage a merge op with the staged-set sanity check
  archive-target  derive/validate the archive path for a low-signal op
  merge-op        verify, write the merged body, stage, commit; roll back on failure
  archive-op      verify, git mv, commit; roll back on failure
  stale-op        verify, insert the stale banner, commit, resolve the queue entry
  stale-banner    the banner insertion alone (idempotent)
  finalize        quarantine update, skipped-line insertion, path-limited audit commit
  rollback        restore one failed op's declared paths to pre-op state

Every subcommand prints one JSON object. Errors exit 2 with {"error": ...}.
Filenames (outcomes sidecar, snapshot slugs, quarantine files) are pinned by
`scripts/autoevo_verify.py`; do not rename them here without updating the
verifier and `tests/test_autoevo_run.py`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import atomic_write, tier, vault_root  # noqa: E402
from _git import merge_state, run_git  # noqa: E402
from autoevo_commit import archive_commit, audit_commit, cluster_hash, merge_commit, stale_commit  # noqa: E402
from autoevo_preflight import (  # noqa: E402
    autoevo_sidecar,
    partition_dirty_scope,
    _inside_worktree,
    _status_entries,
    autoevo_scope_prefixes,
)
from autoevo_quarantine import active_scopes  # noqa: E402
import decay_scan  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SESSION_LOCK_MAX_AGE_S = 21600

# Trust-band thresholds: the single numeric source. protocols/autoevo.md
# § Trust bands explains them and harness_lint checks that its table matches;
# forgetter.md and autoevo-nightly.md point here instead of restating them.
BAND_RULES = {
    "redundant-high": {
        "min_peers": 3,
        "min_score": 0.85,
        "tiers": ("wip",),
        "cold_days": 30,
        "mode": "real",
    },
    "redundant-pending": {"min_peers": 3, "min_score": 0.6},
    "low-signal-high": {"conditions": 5, "cold_days": 365},
    "low-signal-pending": {"conditions": 5, "cold_days_min": 90, "cold_days_max": 365},
}


def band_label(band: str) -> str:
    """The `--band` string recorded in commit bodies; rendered from BAND_RULES."""
    if band == "redundant-high":
        r = BAND_RULES[band]
        return (
            f"redundant-high ({r['min_peers']}+ peers >= {r['min_score']}, all in {'/'.join(r['tiers'])}, "
            f"all > {r['cold_days']}d cold, mode={r['mode']})"
        )
    if band == "low-signal-high":
        r = BAND_RULES[band]
        return f"low-signal-high (all {r['conditions']} Forgetter conditions + >{r['cold_days']}d cold)"
    return band
RESEARCH_EXCLUDED_SUBDIRS = ("cache", "images", "raw")
TOMBSTONE_WINDOW = "90 days ago"


def _fail(message: str, code: int = 2) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False))
    return code


def _emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _emit_error(payload: dict, code: int = 2) -> int:
    """An op that failed after side effects: report what was rolled back, exit 2."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run_git(vault, *args, timeout=60)


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
    in_progress = merge_state(vault)
    if in_progress:
        blockers.append(
            {
                "gate": "git_operation_in_progress",
                "detail": (
                    f"Git operation in progress ({', '.join(in_progress)}); a bot commit "
                    "would complete the user's merge, rebase, cherry-pick, or bisect"
                ),
            }
        )
    if blockers and blockers[-1]["gate"] in {
        "git_index_missing", "git_index_lock_present", "git_operation_in_progress",
    }:
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

    outcomes_file = autoevo_sidecar(cache, args.run_ts, "outcomes")
    outcomes_file.write_text("{}", encoding="utf-8")
    skipped_file = autoevo_sidecar(cache, args.run_ts, "quarantine-skipped")
    skipped_file.write_text(
        "".join(line + "\n" for line in skipped_lines), encoding="utf-8"
    )

    _, protected = partition_dirty_scope(
        [path for _, path in _status_entries(vault)], autoevo_scope_prefixes(vault)
    )
    protected_file = autoevo_sidecar(cache, args.run_ts, "protected")
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


_AUTOEVO_SUBJECT = re.compile(r"^\[autoevo:([a-zA-Z0-9-]+)\]")
_REVERTS_SHA = re.compile(r"^This reverts commit ([0-9a-f]{7,40})", re.MULTILINE)
_QUEUE_ENTRY = re.compile(r"^Queue entry: (\S+)", re.MULTILINE)


def cmd_record_undos(args: argparse.Namespace) -> int:
    """Turn `git revert` of an autoevo op into a human `undo` line in the ledger.

    The stale banner tells the user to `git revert` the marked commit, and
    protocols/decision-ledger.md promises the judge learns from its own misses.
    It could not: a revert wrote nothing to the ledger, so the loudest possible
    signal - the judge acted and the human undid it - was the one outcome
    `precedent_stats` never saw. This closes that loop using the walk the
    tombstone check already relies on.

    Idempotent: a subject that already carries an `undo` line is skipped, so the
    nightly can run this every cycle.
    """
    import decisions

    vault = vault_root()
    ledger = Path(args.ledger) if args.ledger else None
    existing = {
        (str(r.get("class")), str(r.get("subject")))
        for r in decisions.load(ledger)
        if r.get("verdict") == "undo"
    }
    today = date.fromisoformat(args.today) if args.today else date.today()
    recorded, skipped = [], []
    log = _git(vault, "log", f"--since={args.since}", '--grep=^Revert "', "--format=%H").stdout.split()
    for sha in log:
        body = _git(vault, "show", "-s", "--format=%B", sha).stdout
        match = _REVERTS_SHA.search(body)
        if not match:
            continue
        original = _git(vault, "show", "-s", "--format=%B", match.group(1)).stdout
        subject = _AUTOEVO_SUBJECT.match(original.strip())
        entry = _QUEUE_ENTRY.search(original)
        if not subject or not entry:
            continue
        cls, subj = f"autoevo/{subject.group(1)}", entry.group(1)
        if (cls, subj) in existing:
            skipped.append(subj)
            continue
        decisions.record_best_effort(
            cls=cls, subject=subj, verdict="undo",
            reason=f"user reverted the autoevo commit ({sha[:7]})",
            features={}, source="revert-scan", by="human",
            ts=f"{today.isoformat()}T00:00:00" if args.today else None, path=ledger,
        )
        existing.add((cls, subj))
        recorded.append(subj)
    return _emit({"recorded": recorded, "already_recorded": skipped, "since": args.since})


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
    result = stage_merge(vault_root(), args.target, list(args.source))
    return _fail(result["error"]) if "error" in result else _emit(result)


# --- archive-target ---------------------------------------------------------


def cmd_archive_target(args: argparse.Namespace) -> int:
    vault = vault_root()
    archive_rel = str(tier("archive").relative_to(vault))
    target_rel = f"{archive_rel}/decayed/{args.run_date}-{_path_slug(args.source)}.md"
    if (vault / target_rel).exists():
        return _fail(f"archive target exists: {target_rel}")
    (vault / archive_rel / "decayed").mkdir(parents=True, exist_ok=True)
    return _emit({"target_rel": target_rel})


# --- stale banner (time-stale-A default) ----------------------------------

STALE_BANNER_TIERS = ("wip", "research")


def _stale_banner_prefixes(vault: Path) -> tuple[str, ...]:
    return tuple(f"{tier(name).relative_to(vault)}/" for name in STALE_BANNER_TIERS)


def stale_banner_text(run_date: str, entry_id: str, phrase: str) -> str:
    phrase = " ".join(str(phrase).split()).replace('"', "'")
    return (
        f'> Stale since {run_date} (autoevo {entry_id}): "{phrase}" passed with no '
        "closure found; the veto window closed. `git revert` the marked commit to undo.\n"
    )


def insert_stale_banner(text: str, banner: str) -> str:
    """Place the banner after frontmatter and a leading H1, else at the top."""
    lines = text.splitlines(keepends=True)
    index = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                index = i + 1
                break
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and lines[index].startswith("# "):
        index += 1
    head, tail = lines[:index], lines[index:]
    if head and not head[-1].endswith("\n"):
        head[-1] += "\n"
    block = banner if not tail or tail[0].strip() == "" else banner + "\n"
    if head and head[-1].strip():
        block = "\n" + block
    return "".join(head) + block + "".join(tail)


def cmd_stale_banner(args: argparse.Namespace) -> int:
    vault = vault_root()
    rel = _norm_rel(args.source)
    if not rel.startswith(_stale_banner_prefixes(vault)):
        return _fail(f"stale-banner refused outside {'/'.join(STALE_BANNER_TIERS)}: {rel}")
    path = vault / rel
    if not path.is_file():
        return _fail(f"source missing: {rel}")
    text = path.read_text(encoding="utf-8")
    marker = f"(autoevo {args.entry_id})"
    if marker in text:
        return _emit({"changed": False, "reason": "banner already present", "source": rel})
    updated = insert_stale_banner(text, stale_banner_text(args.run_date, args.entry_id, args.phrase))
    path.write_text(updated, encoding="utf-8")
    return _emit({"changed": True, "source": rel, "bytes_added": len(updated) - len(text)})


# --- identity ---------------------------------------------------------------


def cmd_identity(args: argparse.Namespace) -> int:
    """RUN_TS plus the RUN_DATE an unattended cycle is allowed to claim."""
    run_ts = time.strftime("%Y%m%d-%H%M%S")
    profile = os.environ.get("ATELIER_ROUTINE_PROFILE", "").strip()
    cycle = os.environ.get("ATELIER_ROUTINE_CYCLE", "").strip()
    if profile and not cycle:
        return _fail("unattended invocation omitted ATELIER_ROUTINE_CYCLE")
    if cycle:
        from routine_claim import validate_cycle_id

        try:
            run_date = validate_cycle_id(cycle)
        except (ValueError, SystemExit) as exc:
            return _fail(f"invalid ATELIER_ROUTINE_CYCLE: {exc}")
    else:
        run_date = date.today().isoformat()
    return _emit({"run_ts": run_ts, "run_date": run_date, "unattended": bool(profile)})


# --- route-bands ------------------------------------------------------------


def _age_days(vault: Path, rel: str, today: date) -> int | None:
    path = vault / rel
    try:
        mtime = date.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None
    return (today - mtime).days


def _norm_rel(rel: str) -> str:
    """Vault-relative path with `.` and `..` collapsed, POSIX separators.

    A lexical prefix test accepts `wip/../wiki/X.md`, which every later
    filesystem and git call normalizes into the protected tier. Collapse the
    traversal first so containment is decided on the path that is actually
    touched. Purely lexical: no filesystem access, so it cannot be defeated by
    a missing file and cannot break on a network-mounted vault.
    """
    return PurePosixPath(os.path.normpath(str(rel).strip().lstrip("/"))).as_posix()


def _under_tiers(vault: Path, rel: str, tiers: tuple[str, ...]) -> bool:
    prefixes = tuple(f"{tier(name).relative_to(vault)}/" for name in tiers)
    return _norm_rel(rel).startswith(prefixes)


def route_row(vault: Path, row: dict, today: date) -> tuple[str, str, str]:
    """(bucket, band, reason) for one Forgetter row.

    bucket: auto_apply | pending | probe | invalid. The band is the label the
    op records; the reason explains a downgrade so the audit can show it.
    """
    category = str(row.get("category", ""))
    confidence = str(row.get("confidence", "medium") or "medium")
    candidate = str(row.get("candidate", "")).strip()
    if category == "contradicted":
        return "probe", "contradicted", "Challenger decides genuine vs rhetorical"
    if category in ("time-stale-A", "time-stale-B", "time-stale"):
        return "pending", category, "intent-laden; never auto-applied"
    if category == "redundant":
        rule = BAND_RULES["redundant-high"]
        peers = [str(p) for p in row.get("peers", []) if str(p).strip()]
        scores = []
        for value in row.get("scores", []):
            try:
                score = float(value)
            except (TypeError, ValueError):
                return "invalid", category, f"non-numeric retrieval score {value!r}"
            if not math.isfinite(score):
                # NaN compares False against the threshold, so it would pass the
                # min_score check rather than fail it.
                return "invalid", category, f"non-finite retrieval score {value!r}"
            scores.append(score)
        if not candidate or not peers:
            return "invalid", category, "redundant row needs candidate and peers"
        failures = []
        if confidence != "high":
            failures.append(f"confidence {confidence}")
        if len(peers) < rule["min_peers"]:
            failures.append(f"{len(peers)} peers < {rule['min_peers']}")
        if len(scores) < len(peers) or any(sc < rule["min_score"] for sc in scores[: len(peers)]):
            failures.append(f"a peer scores below {rule['min_score']}")
        if str(row.get("mode", "")) != rule["mode"]:
            failures.append(f"mode {row.get('mode')!r} is not {rule['mode']}")
        for rel in [candidate, *peers]:
            if not _under_tiers(vault, rel, rule["tiers"]):
                failures.append(f"{rel} outside {'/'.join(rule['tiers'])}")
                break
        for rel in [candidate, *peers]:
            age = _age_days(vault, rel, today)
            if age is None:
                failures.append(f"{rel} missing on disk")
                break
            if age <= rule["cold_days"]:
                failures.append(f"{rel} touched within {rule['cold_days']}d")
                break
        if failures:
            pending_rule = BAND_RULES["redundant-pending"]
            if len(peers) >= pending_rule["min_peers"] and scores and min(scores[: len(peers)]) >= pending_rule["min_score"]:
                return "pending", "redundant", "; ".join(failures)
            return "invalid", category, "below the pending floor: " + "; ".join(failures)
        return "auto_apply", "redundant-high", "all auto-apply preconditions verified"
    if category == "low-signal":
        rule = BAND_RULES["low-signal-high"]
        try:
            met = int(row.get("conditions_met", 0))
        except (TypeError, ValueError):
            return "invalid", category, "conditions_met is not an integer"
        if not candidate:
            return "invalid", category, "low-signal row needs candidate"
        if met < rule["conditions"]:
            return "invalid", category, f"{met}/{rule['conditions']} conditions is no flag"
        age = _age_days(vault, candidate, today)
        if age is None:
            return "invalid", category, f"{candidate} missing on disk"
        # `conditions_met` is the dispatched agent's own count. Trusting it meant
        # a caller-supplied 5 was enough to reach an automatic archive, with
        # nothing checking the note's words, tags, inbound links, or tier. The
        # rule lives in decay_scan; recompute it from disk before auto-applying.
        failures = decay_scan.low_signal_content_failures(vault, candidate)
        if failures:
            return "invalid", category, f"conditions do not hold on disk: {'; '.join(failures)}"
        if age > rule["cold_days"] and confidence == "high":
            return "auto_apply", "low-signal-high", f"{age}d cold, all conditions hold"
        pending = BAND_RULES["low-signal-pending"]
        if age >= pending["cold_days_min"]:
            return "pending", "low-signal", f"{age}d cold (confidence {confidence})"
        return "invalid", category, f"{age}d cold is under the {pending['cold_days_min']}d pending floor"
    return "invalid", category or "(none)", "unknown category"


def cmd_route_bands(args: argparse.Namespace) -> int:
    vault = vault_root()
    today = date.fromisoformat(args.today)
    try:
        rows = json.loads(Path(args.findings).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(f"cannot read findings: {exc}")
    if not isinstance(rows, list):
        return _fail("findings must be a JSON list of rows")
    buckets: dict[str, list[dict]] = {"auto_apply": [], "pending": [], "probe": [], "invalid": []}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            buckets["invalid"].append({"index": index, "reason": "row is not an object"})
            continue
        bucket, band, reason = route_row(vault, row, today)
        entry = dict(row)
        entry.update({"index": index, "band": band, "route_reason": reason})
        if bucket == "auto_apply":
            entry["band_label"] = band_label(band)
        buckets[bucket].append(entry)
    return _emit({"today": args.today, "rules": BAND_RULES, **buckets})


# --- verify-snapshot + ops ---------------------------------------------------


def snapshot_mismatch(vault: Path, cache: Path, run_ts: str, rel: str) -> str | None:
    """Why the on-disk file no longer matches its step-4.1 snapshot, or None."""
    snap = _snapshot_path(cache, run_ts, rel)
    src = vault / rel
    if not snap.is_file():
        return f"no snapshot for {rel} (run 4.1 first)"
    if not src.is_file():
        return f"{rel} vanished since its snapshot"
    if snap.read_bytes() != src.read_bytes():
        return f"{rel} changed since its snapshot (user edit mid-run); refusing to touch it"
    return None


def cmd_verify_snapshot(args: argparse.Namespace) -> int:
    vault, cache = vault_root(), tier("cache")
    for rel in args.source:
        problem = snapshot_mismatch(vault, cache, args.run_ts, rel)
        if problem:
            return _fail(problem)
    return _emit({"verified": list(args.source)})


def _rollback(vault: Path, cache: Path, run_ts: str, paths: list[str], sources: list[str]) -> dict:
    _git(vault, "restore", "--staged", "--", *paths)
    restored, removed, recovered = [], [], []
    for rel in paths:
        if _git(vault, "cat-file", "-e", f"HEAD:{rel}").returncode == 0:
            _git(vault, "restore", "--worktree", "--", rel)
            restored.append(rel)
        elif (vault / rel).is_file():
            (vault / rel).unlink()
            removed.append(rel)
    for rel in sources:
        if (vault / rel).is_file():
            continue
        snap = _snapshot_path(cache, run_ts, rel)
        if snap.is_file():
            (vault / rel).write_bytes(snap.read_bytes())
            recovered.append(rel)
    return {"restored": restored, "removed": removed, "recovered_from_snapshot": recovered}


def stage_merge(vault: Path, target: str, sources: list[str]) -> dict:
    added = _git(vault, "add", "--", target)
    if added.returncode != 0:
        return {"error": f"git add failed: {added.stderr.strip()}"}
    for rel in sources:
        if rel == target:
            continue
        removed = _git(vault, "rm", "--", rel)
        if removed.returncode != 0:
            return {"error": f"git rm failed for {rel}: {removed.stderr.strip()}"}
    staged = set(_git(vault, "diff", "--cached", "--name-only").stdout.splitlines())
    expected = set(sources) | {target}
    if staged != expected:
        _git(vault, "restore", "--staged", "--", *sorted(expected))
        return {"error": f"staged paths diverged from expected: staged={sorted(staged)} expected={sorted(expected)}"}
    return {"staged": sorted(staged)}


def cmd_merge_op(args: argparse.Namespace) -> int:
    """Redundant auto-merge, end to end, after Curator returned auto_apply_safe."""
    vault, cache = vault_root(), tier("cache")
    sources = list(args.source)
    if args.target not in sources:
        return _fail("target must be one of the sources (the surviving slug)")
    for rel in sources:
        problem = snapshot_mismatch(vault, cache, args.run_ts, rel)
        if problem:
            return _fail(problem)
    try:
        body = Path(args.body).read_text(encoding="utf-8")
    except OSError as exc:
        return _fail(f"cannot read merged body: {exc}")
    if not body.strip():
        return _fail("merged body is empty")
    atomic_write(vault / args.target, body)
    paths = [args.target, *sources]
    staged = stage_merge(vault, args.target, sources)
    if "error" in staged:
        rolled = _rollback(vault, cache, args.run_ts, paths, sources)
        return _emit_error({"error": staged["error"], "rolled_back": rolled})
    result = merge_commit(
        vault, scope=args.scope, target_slug=args.target_slug, band=args.band or band_label("redundant-high"),
        sources=sources, paths=paths, source_evidence=args.source_evidence,
    )
    if "sha" not in result:
        rolled = _rollback(vault, cache, args.run_ts, paths, sources)
        return _emit_error({"error": result.get("error", "commit failed"), "rolled_back": rolled})
    return _emit({**result, "staged": staged["staged"], "target": args.target})


def cmd_archive_op(args: argparse.Namespace) -> int:
    """Low-signal auto-archive, end to end, after Curator returned auto_apply_safe."""
    vault, cache = vault_root(), tier("cache")
    problem = snapshot_mismatch(vault, cache, args.run_ts, args.source)
    if problem:
        return _fail(problem)
    if (vault / args.target).exists():
        return _fail(f"archive target exists: {args.target}")
    (vault / args.target).parent.mkdir(parents=True, exist_ok=True)
    moved = _git(vault, "mv", "--", args.source, args.target)
    if moved.returncode != 0:
        return _fail(f"git mv failed: {moved.stderr.strip()}")
    result = archive_commit(
        vault, slug=args.slug, days_inactive=args.days_inactive, evidence=args.evidence,
        source=args.source, target=args.target, band=args.band or band_label("low-signal-high"),
    )
    if "sha" not in result:
        rolled = _rollback(vault, cache, args.run_ts, [args.source, args.target], [args.source])
        return _emit_error({"error": result.get("error", "commit failed"), "rolled_back": rolled})
    return _emit({**result, "source": args.source, "target": args.target})


def cmd_stale_op(args: argparse.Namespace) -> int:
    """time-stale-A default after the veto window: banner, commit, resolve."""
    import autoevo_pending
    from autoevo_pending import load as load_queue

    vault, cache = vault_root(), tier("cache")
    rel = _norm_rel(args.source)
    if not rel.startswith(_stale_banner_prefixes(vault)):
        return _fail(f"stale-op refused outside {'/'.join(STALE_BANNER_TIERS)}: {rel}")
    problem = snapshot_mismatch(vault, cache, args.run_ts, rel)
    if problem:
        return _fail(problem)
    queue = Path(args.queue) if args.queue else autoevo_pending.queue_path()
    # The window can close between `veto-expired` listing this entry and this op
    # running: a user who applied, skipped, or deferred it in /autoevo-review in
    # between has already decided. Checking after the write meant acting on a
    # vetoed entry, committing the banner, and still reporting success with the
    # refusal nested inside.
    entry = next((e for e in load_queue(queue)["pending"] if e.get("id") == args.entry_id), None)
    if entry is None:
        return _fail(f"stale-op refused: no queue entry {args.entry_id}")
    if entry.get("status") != "pending":
        return _fail(f"stale-op refused: {args.entry_id} is {entry.get('status')}, not pending")
    path = vault / rel
    text = path.read_text(encoding="utf-8")
    changed = f"(autoevo {args.entry_id})" not in text
    sha = None
    if changed:
        atomic_write(path, insert_stale_banner(text, stale_banner_text(args.run_date, args.entry_id, args.phrase)))
        result = stale_commit(
            vault, slug=args.slug or _path_slug(rel), source=rel, phrase=args.phrase, entry_id=args.entry_id,
            proposed_at=args.proposed_at, default_at=args.default_at,
        )
        if "sha" not in result:
            rolled = _rollback(vault, cache, args.run_ts, [rel], [rel])
            return _emit_error({"error": result.get("error", "commit failed"), "rolled_back": rolled})
        sha = result["sha"]
    resolved = autoevo_pending.resolve_entry(
        queue, args.entry_id, "applied", "default after veto window",
        today=date.fromisoformat(args.run_date), source="nightly", by="rule",
        ledger=Path(args.ledger) if args.ledger else None, explicit_today=True,
    )
    if "error" in resolved:
        # The banner is committed at this point. A nested error under a success
        # envelope is how this stayed invisible; surface it as the failure it is.
        return _emit_error({"error": resolved["error"], "changed": changed, "sha": sha, "source": rel})
    return _emit({"changed": changed, "sha": sha, "source": rel, "resolved": resolved})


def cmd_finalize(args: argparse.Namespace) -> int:
    """Step 7 mechanics: quarantine counters, skipped lines, path-limited audit commit."""
    vault, cache = vault_root(), tier("cache")
    audit_path = vault / args.audit_rel
    if not audit_path.is_file():
        return _fail(f"audit log missing: {args.audit_rel}")
    state = vault / "_meta" / "autoevo_quarantine.toml"
    count_file = autoevo_sidecar(cache, args.run_ts, "quarantine-count")
    helper = ROOT / "scripts" / "autoevo_quarantine.py"
    steps = [
        ["update", "--outcomes", args.outcomes, "--state", str(state), "--count-file", str(count_file), "--today", args.run_date],
        ["insert-skipped", "--audit", str(audit_path), "--skipped-lines", args.quarantine_skipped],
    ]
    for step in steps:
        proc = subprocess.run([sys.executable, str(helper), *step], cwd=ROOT, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return _fail(f"autoevo_quarantine.py {step[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    try:
        quarantined = int(count_file.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        quarantined = 0
    try:
        reports = json.loads(Path(args.reports).read_text(encoding="utf-8")) if args.reports else []
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(f"cannot read reports list: {exc}")
    missing = [rel for rel in reports if not (vault / rel).is_file()]
    if missing:
        return _fail(f"registered decay reports missing on disk: {missing}")
    paths = [args.audit_rel, *reports]
    result = audit_commit(
        vault, run_date=args.run_date, auto=args.auto, pending=args.pending, errors=args.errors,
        quarantined=str(quarantined), paths=paths,
        force_add=["_meta/autoevo_quarantine.toml"] if state.is_file() else [],
    )
    if "sha" not in result:
        return _emit_error({"error": result.get("error", "audit commit failed"), "quarantined": quarantined, "paths": paths})
    return _emit({**result, "quarantined": quarantined, "paths": paths})


# --- rollback ---------------------------------------------------------------


def cmd_rollback(args: argparse.Namespace) -> int:
    return _emit(_rollback(vault_root(), tier("cache"), args.run_ts, list(args.paths), list(args.source)))


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    identity = sub.add_parser("identity")
    identity.set_defaults(func=cmd_identity)

    route = sub.add_parser("route-bands")
    route.add_argument("--findings", required=True, help="JSON list of Forgetter rows")
    route.add_argument("--today", required=True)
    route.set_defaults(func=cmd_route_bands)

    verify = sub.add_parser("verify-snapshot")
    verify.add_argument("--run-ts", required=True)
    verify.add_argument("--source", action="append", required=True)
    verify.set_defaults(func=cmd_verify_snapshot)

    mop = sub.add_parser("merge-op")
    mop.add_argument("--run-ts", required=True)
    mop.add_argument("--target", required=True, help="surviving relative path (one of --source)")
    mop.add_argument("--source", action="append", required=True)
    mop.add_argument("--body", required=True, help="file holding Curator's merged body")
    mop.add_argument("--scope", required=True)
    mop.add_argument("--target-slug", required=True)
    mop.add_argument("--band", default=None)
    mop.add_argument("--source-evidence", action="append", default=None)
    mop.set_defaults(func=cmd_merge_op)

    aop = sub.add_parser("archive-op")
    aop.add_argument("--run-ts", required=True)
    aop.add_argument("--source", required=True)
    aop.add_argument("--target", required=True)
    aop.add_argument("--slug", required=True)
    aop.add_argument("--days-inactive", required=True)
    aop.add_argument("--evidence", required=True)
    aop.add_argument("--band", default=None)
    aop.set_defaults(func=cmd_archive_op)

    sop = sub.add_parser("stale-op")
    sop.add_argument("--run-ts", required=True)
    sop.add_argument("--run-date", required=True)
    sop.add_argument("--entry-id", required=True)
    sop.add_argument("--source", required=True)
    sop.add_argument("--phrase", required=True)
    sop.add_argument("--proposed-at", required=True)
    sop.add_argument("--default-at", required=True)
    sop.add_argument("--slug", default=None)
    sop.add_argument("--queue", default=None)
    sop.add_argument("--ledger", default=None)
    sop.set_defaults(func=cmd_stale_op)

    fin = sub.add_parser("finalize")
    fin.add_argument("--run-ts", required=True)
    fin.add_argument("--run-date", required=True)
    fin.add_argument("--audit-rel", required=True)
    fin.add_argument("--outcomes", required=True)
    fin.add_argument("--quarantine-skipped", required=True)
    fin.add_argument("--reports", default=None, help="JSON list of decay report relative paths")
    fin.add_argument("--auto", required=True)
    fin.add_argument("--pending", required=True)
    fin.add_argument("--errors", required=True)
    fin.set_defaults(func=cmd_finalize)

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

    undos = sub.add_parser("record-undos")
    undos.add_argument("--since", default=TOMBSTONE_WINDOW)
    undos.add_argument("--today", default=None)
    undos.add_argument("--ledger", default=None, help="Override the decision ledger path (tests).")
    undos.set_defaults(func=cmd_record_undos)

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

    stale = sub.add_parser("stale-banner")
    stale.add_argument("--source", required=True, help="relative path under $OV")
    stale.add_argument("--phrase", required=True, help="the dated phrase Forgetter flagged")
    stale.add_argument("--entry-id", required=True)
    stale.add_argument("--run-date", required=True)
    stale.set_defaults(func=cmd_stale_banner)

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
