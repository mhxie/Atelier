#!/usr/bin/env python3
"""Prepare isolated Autoevo proposals; the trusted writer applies plain-file operations."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from datetime import date, timedelta
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid

from _git import hash_objects, run_git, tree_blobs
from _paths import atomic_write, tier_segments
import autoevo_pending as pending
import autoevo_preflight as preflight
import autoevo_quarantine as quarantine
import autoevo_verify as evidence
import decay_scan
import decisions
import precedent

ROOT = Path(__file__).resolve().parents[1]
WORKING_TIERS = ("wip", "research", "reflections")
RESEARCH_EXCLUDED_SUBDIRS = ("cache", "images", "raw")
NOTE_KINDS = evidence.NOTE_KINDS
RESULT_STATUSES = ("prepared", "publishing", "needs_review", "failed", "complete")
TOMBSTONE_DAYS = 90
# A note touched this recently may still be mid-edit or not yet committed by Reflect.
RECENT_EDIT_SECONDS = 2 * 60 * 60
# When most in-scope notes share one mtime window, a clone or checkout reset
# them, and mtime no longer says how long a note sat untouched.
MTIME_RESET_SHARE = 0.8
MTIME_RESET_WINDOW_SECONDS = 10 * 60
CONFLICT_MARKER = re.compile(rb"^(?:<<<<<<<(?: |\r?$)|=======\r?$|>>>>>>>(?: |\r?$))", re.MULTILINE)


def _text(vault: Path, *args: str) -> str:
    result = run_git(vault, *args, timeout=120)
    if result.returncode:
        raise evidence.VerificationError(f"git {args[0]} failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def _segment(name: str) -> str:
    return evidence.relative(tier_segments().get(name, name))


def _prefixes(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_segment(name) + "/" for name in names)


def _protected(rel: str, paths: list[str]) -> bool:
    return any(rel == path or path.endswith("/") and rel.startswith(path) for path in paths)


def _file(vault: Path, rel: str) -> Path:
    path = vault / evidence.relative(rel)
    if path.resolve() != path.absolute() or path.is_symlink():
        raise evidence.VerificationError(f"symlink path refused: {rel}")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise evidence.VerificationError(f"non-regular or hard-linked path refused: {rel}")
    return path


def _hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _text_hash(body: str | None) -> str | None:
    return None if body is None else hashlib.sha256(body.encode()).hexdigest()


def _assert_hashes(vault: Path, hashes: dict[str, str | None]) -> None:
    for rel, expected in hashes.items():
        if _hash(_file(vault, rel)) != expected:
            raise evidence.VerificationError(f"source or decision state changed: {rel}")


def _pending(queue: Path, ledger: Path, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, pending.__file__, "--queue", str(queue), "--ledger", str(ledger), *args],
        capture_output=True, text=True, timeout=120,
    )
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        detail = result.stderr.strip()[-500:] or f"exit {result.returncode}"
        raise evidence.VerificationError(f"pending helper did not produce JSON: {detail}") from exc
    if result.returncode or value.get("error") or value.get("invalid"):
        raise evidence.VerificationError(str(value.get("error") or value.get("invalid") or "domain helper failed"))
    return value


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


def _age_days(vault: Path, rel: str, today: date) -> int | None:
    path = vault / rel
    try:
        mtime = date.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None
    return (today - mtime).days


def _norm_rel(rel: str) -> str:
    """Collapse traversal before tier containment checks, without filesystem access."""
    return PurePosixPath(os.path.normpath(str(rel).strip().lstrip("/"))).as_posix()


def _under_tiers(vault: Path, rel: str, tiers: tuple[str, ...]) -> bool:
    prefixes = _prefixes(tiers)
    return _norm_rel(rel).startswith(prefixes)


def route_row(vault: Path, row: dict, today: date, age_guard: str | None = None) -> tuple[str, str, str]:
    """(bucket, band, reason); age_guard disables mtime-based eligibility."""
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
        if row.get("mode") == "qmd":
            if len(set(peers) - {candidate}) < rule["min_peers"]:
                return "invalid", category, "QMD candidate needs three distinct non-self peers"
            return "pending", "redundant", "QMD scores are uncalibrated; content review and human approval required"
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
            if age_guard or age <= rule["cold_days"]:
                failures.append(age_guard or f"{rel} touched within {rule['cold_days']}d")
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
        if age_guard:
            return "invalid", category, age_guard
        if age > rule["cold_days"] and confidence == "high":
            return "auto_apply", "low-signal-high", f"{age}d cold, all conditions hold"
        pending = BAND_RULES["low-signal-pending"]
        if age >= pending["cold_days_min"]:
            return "pending", "low-signal", f"{age}d cold (confidence {confidence})"
        return "invalid", category, f"{age}d cold is under the {pending['cold_days_min']}d pending floor"
    return "invalid", category or "(none)", "unknown category"



def stale_banner_text(run_date: str, entry_id: str, phrase: str) -> str:
    phrase = " ".join(str(phrase).split()).replace('"', "'")
    return (
        f'> Stale since {run_date} (autoevo {entry_id}): "{phrase}" passed with no '
        "closure found; the veto window closed. `git restore` the note's prior version to undo.\n"
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


def cluster_hash(sources: list[str]) -> str:
    """First 12 hex chars of sha1 over the sorted unique source paths, one per LF-terminated line."""
    return hashlib.sha1(("\n".join(sorted(set(sources))) + "\n").encode("utf-8")).hexdigest()[:12]


def _recent_operations(vault: Path, today: date) -> list[tuple[dict, str]]:
    """(operation, content state) for note operations receipted in the tombstone window."""
    found = []
    for receipt in sorted(evidence.record_path(vault, today.isoformat()).parent.glob("*.json")):
        try:
            cycle = date.fromisoformat(receipt.stem)
        except ValueError:
            continue
        if today - timedelta(days=TOMBSTONE_DAYS) <= cycle < today:
            found += [(op, evidence.operation_state(vault, op)) for op in evidence.read_record(receipt).get("operations", [])
                      if isinstance(op, dict) and op.get("kind") in NOTE_KINDS]
    return found


def _cluster(operation: dict) -> str:
    """The cluster an operation changed: its pre-existing note paths, never state files."""
    meta = _segment("meta") + "/"
    return cluster_hash([rel for rel, (before, _) in evidence.operation_paths(operation).items()
                         if before is not None and not rel.startswith(meta)])


def tombstone_reason(vault: Path, sources: list[str], today: date) -> str | None:
    cluster = cluster_hash(sources)
    if any(state == "reverted" and _cluster(op) == cluster for op, state in _recent_operations(vault, today)):
        return f"user reverted cluster {cluster}"
    path = _file(vault, f"{_segment('meta')}/autoevo_tombstones.toml")
    if path.is_file():
        for row in tomllib.loads(path.read_text()).get("tombstone", []):
            if row.get("cluster_hash") == cluster and (not row.get("expires_at") or str(row["expires_at"]) >= today.isoformat()):
                return f"explicit tombstone: {row.get('reason', cluster)}"
    return None


def record_undos(vault: Path, ledger: Path, today: date) -> None:
    """A restored stale banner is the user's veto of that queue entry's default."""
    existing = {(row.get("class"), row.get("subject")) for row in decisions.load(ledger) if row.get("verdict") == "undo"}
    for op, state in _recent_operations(vault, today):
        key = (f"autoevo/{op.get('category')}", op.get("entry"))
        if state == "reverted" and op.get("entry") and key not in existing:
            decisions.record_best_effort(cls=key[0], subject=key[1], verdict="undo",
                reason=f"user restored the notes autoevo changed ({op['candidate_id'][:12]})", features={},
                source="revert-scan", by="human", ts=f"{today.isoformat()}T00:00:00", path=ledger)
            existing.add(key)


def _revived_archives(vault: Path, today: date, run_id: str) -> list[dict]:
    """Queue archived sources that a sync merge brought back beside their archive copy."""
    entries = []
    for op, state in _recent_operations(vault, today):
        peers = sorted(evidence.operation_paths(op)) if state == "superseded" and op["kind"] == "low-signal-high" else []
        if peers and all((vault / rel).is_file() for rel in peers):
            entries.append({"id": f"{run_id}-{evidence.digest(['revived', peers])[:12]}", "category": "redundant",
                            "peers": peers, "proposed_action": "keep either the revived note or its archive copy",
                            "evidence_summary": f"archived by autoevo {op['candidate_id'][:12]}, then revived by a sync merge",
                            "proposed_at": today.isoformat(), "status": "pending"})
    return entries


def _mtime_reset(stamps: list[float]) -> str | None:
    """Why ages are unreliable when most in-scope notes share one mtime window."""
    stamps = sorted(stamps)
    peak = max((bisect_right(stamps, stamp + MTIME_RESET_WINDOW_SECONDS) - index for index, stamp in enumerate(stamps)), default=0)
    if not stamps or peak < MTIME_RESET_SHARE * len(stamps):
        return None
    return (f"mtime_reset: {peak} of {len(stamps)} in-scope tracked notes share one "
            f"{MTIME_RESET_WINDOW_SECONDS // 60}-minute mtime window; age-based bands are off this run")


def prepare_workspace(vault: Path, workspace: Path, cycle: str, readiness: dict, *, now: float | None = None) -> dict:
    """Snapshot eligible committed sources; the parent retains the authoritative plan."""
    now = time.time() if now is None else now
    today = date.fromisoformat(cycle)
    meta = _segment("meta")
    state_files = [f"{meta}/{name}" for name in ("autoevo_pending.toml", "autoevo_quarantine.toml", "autoevo_tombstones.toml", "decisions.jsonl")]
    state_hashes = {rel: _hash(_file(vault, rel)) for rel in state_files}
    quarantined = set(quarantine.active_scopes(state_path=vault / state_files[1], today=today))
    research = vault / _segment("research")
    children = sorted(path for path in research.iterdir() if path.is_dir() and not path.is_symlink()
                      and not path.name.startswith(".") and path.name not in RESEARCH_EXCLUDED_SUBDIRS) if research.is_dir() else []
    live = [path for path in children if str(path) not in quarantined]
    selected = live[(today.day - 1) % len(live)] if live else None
    scopes = [vault / _segment("wip"), *([selected] if selected else []), vault / _segment("reflections")]
    skipped = [f"scope_quarantined: {path.relative_to(vault)}" for path in children if str(path) in quarantined]
    dispatches = []
    for path in scopes:
        rel = evidence.relative(path.relative_to(vault).as_posix())
        if str(path) in quarantined:
            skipped.append(f"scope_quarantined: {rel}")
        else:
            dispatches.append({"scope": rel, "max_candidates": 15 if path == selected else 12, "time_budget_s": 240})
    defaults = [row for row in pending.load(vault / state_files[0])["pending"]
                if row.get("status") == "pending" and row.get("default_action")
                and pending._parse_date(row.get("default_at")) is not None
                and pending._parse_date(row["default_at"]) <= today]
    peers = [peer for row in defaults for peer in row.get("peers", [])
             if isinstance(peer, str) and _norm_rel(peer) == peer and peer.startswith(_prefixes(WORKING_TIERS))]
    head = tree_blobs(vault, *(row["scope"] for row in dispatches), *peers)
    in_scope = {rel for rel in head if rel.endswith(".md") and any(rel.startswith(row["scope"] + "/") for row in dispatches)}
    files, protected, stamps, sources = {}, [], [], {}
    for rel in sorted(in_scope.union(peer for peer in peers if peer in head)):
        try:
            source = _file(vault, rel)
        except evidence.VerificationError:
            protected.append(rel)
            continue
        if source.is_file():
            files[rel] = source
    for (rel, source), blob in zip(files.items(), hash_objects(vault, list(files))):
        before = source.stat()
        stamps += [before.st_mtime] if rel in in_scope else []
        content = source.read_bytes()
        if blob != head[rel] or CONFLICT_MARKER.search(content) or now - before.st_mtime < RECENT_EDIT_SECONDS:
            protected.append(rel)
            continue
        destination = workspace / "sources" / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        shutil.copystat(source, destination)
        sources[rel] = {"before_sha256": hashlib.sha256(content).hexdigest(), "before_blob": blob,
                        "mtime_ns": before.st_mtime_ns, "snapshot": str(destination)}
    _assert_hashes(vault, state_hashes)
    age_guard = _mtime_reset(stamps)
    notes = [] if selected else ["research_rotation_empty: no eligible research subdirectory"]
    return {"schema_version": evidence.VERSION, "cycle_id": cycle, "run_id": f"{today:%Y%m%d}-{uuid.uuid4().hex[:12]}",
            "base_head": _text(vault, "rev-parse", "HEAD"), "dispatches": dispatches, "quarantine_skipped": skipped,
            "retrieval_mode": readiness.get("health", {}).get("semantic_mode", "unavailable"),
            "protected_paths": sorted(protected), "source_files": sources, "state_files": state_hashes, "defaults": defaults,
            "age_guard": age_guard, "notes": notes + [age_guard] if age_guard else notes}


def _curator_problem(row: dict, plan: dict, vault: Path, band: str) -> str | None:
    curator = row.get("curator", {})
    if curator.get("completion_status") != "complete" or curator.get("auto_apply_safe") is not True:
        return "Curator did not return a complete safe proposal"
    if curator.get("mode") != "auto-apply" or curator.get("band") != band or curator.get("remaining_work") or curator.get("gaps"):
        return "Curator mode, band, or remaining work disagrees with the operation"
    sources = sorted(set([row["candidate"], *row.get("peers", [])])) if band == "redundant-high" else [row["candidate"]]
    if any(rel not in plan["source_files"] for rel in sources):
        return "a source was not in the trusted clean snapshot"
    if curator.get("snapshot_paths") != [plan["source_files"][rel]["snapshot"] for rel in sources]:
        return "Curator did not use the exact trusted snapshots"
    if tombstone_reason(vault, sources, date.fromisoformat(plan["cycle_id"])):
        return "cluster is tombstoned"
    if band == "redundant-high":
        if curator.get("operation") not in {"compact", "merge"}:
            return "Curator operation is not a merge"
        target = min(sources, key=lambda rel: (plan["source_files"][rel]["mtime_ns"], rel))
        if curator.get("target_path") != target:
            return "merge target is not the oldest source"
        body = curator.get("proposed_content")
        integrity = curator.get("content_integrity", {})
        if not isinstance(body, str) or not body.strip() or len(body.encode()) > 15_000:
            return "merge body is empty or needs splitting"
        if any(integrity.get(key) is not True for key in ("verbatim_preserved", "structures_preserved", "images_preserved", "checklist_passed")):
            return "Curator content-preservation evidence is incomplete"
        if curator.get("media_inventory") != curator.get("media_output_count") or not isinstance(curator.get("media_inventory"), dict):
            return "Curator media inventory does not match"
        for rel in sources:
            original = _file(vault, rel).read_text(encoding="utf-8")
            if any(line.strip() and line not in body for line in original.splitlines()):
                return "a source line is absent from the merge"
    elif (curator.get("operation") != "archive" or curator.get("source_path") != sources[0]
          or curator.get("proposed_content") != _file(vault, sources[0]).read_bytes().decode("utf-8")):
        return "Curator archive does not preserve the original source"
    return None


def route_proposal(vault: Path, proposal: dict, plan: dict) -> tuple[list[dict], list[dict], list[str]]:
    auto, queued, notes = [], [], []
    today = date.fromisoformat(plan["cycle_id"])
    for sweep in proposal["sweeps"]:
        for row in sweep["findings"]:
            subject = row.get("contradicting_peer", "") if row["category"] == "contradicted" else row["candidate"]
            if not subject.startswith(sweep["scope"] + "/") or _protected(row["candidate"], plan["protected_paths"]):
                notes.append(f"out-of-scope or protected finding skipped: {row['candidate']}")
                continue
            bucket, band, reason = route_row(vault, row, today, plan.get("age_guard"))
            if row["category"] == "redundant" and bucket == "auto_apply" and plan["retrieval_mode"] != "real":
                bucket, band, reason = "pending", "redundant", "trusted retrieval mode is not calibrated; model scores cannot authorize a merge"
            if bucket == "invalid":
                notes.append(f"route_invalid: {row['candidate']}: {reason}")
                continue
            if bucket == "probe":
                probe = row.get("probe", {})
                if probe.get("completion_status") == "complete" and not probe.get("remaining_work") and not probe.get("gaps") and probe.get("verdict") == "rhetorical":
                    notes.append(f"rhetorical contradiction: {probe.get('rationale', '')}")
                    continue
            if bucket == "auto_apply":
                problem = _curator_problem(row, plan, vault, band)
                if not problem:
                    auto.append({"row": row, "band": band})
                    continue
                reason = problem
            peers = sorted(set([row["candidate"], *row.get("peers", [])]))
            entry_id = f"{plan['run_id']}-{evidence.digest([row['category'], peers])[:12]}"
            queued.append({"id": entry_id, "category": row["category"], "peers": peers,
                           "proposed_action": row["proposed_action"], "evidence_summary": f"{row['evidence']}; {reason}",
                           "proposed_at": plan["cycle_id"], "last_surfaced": plan["cycle_id"], "surface_count": 0, "status": "pending"})
    return sorted(auto, key=lambda op: op["band"] != "redundant-high"), queued, notes


def _shadow(vault: Path, directory: Path, state: dict[str, str | None]) -> tuple[Path, Path]:
    _assert_hashes(vault, state)
    for rel in state:
        source = _file(vault, rel)
        destination = directory / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            destination.write_bytes(source.read_bytes())
    meta = _segment("meta")
    return directory / meta / "autoevo_pending.toml", directory / meta / "decisions.jsonl"


def _append(queue: Path, ledger: Path, entries: list[dict], directory: Path, cycle: str) -> dict:
    path = directory / "entries.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return _pending(queue, ledger, "append", "--entries", str(path), "--today", cycle)


def _bundles(queue: Path, ledger: Path, today: date) -> dict:
    args = precedent.build_parser().parse_args(["autoevo"])
    return {str(row["id"]): precedent.build_bundle(cls=f"autoevo/{row['category']}", subject=str(row["id"]),
                features=decisions.autoevo_features(row), ledger=ledger, today=today, k=args.k)
            for row in pending.load(queue)["pending"] if row.get("status") == "pending" and not row.get("default_action")}


def preview(vault: Path, proposal: dict, plan: dict, directory: Path) -> dict:
    evidence.validate_proposal(proposal, plan)
    auto, entries, notes = route_proposal(vault, proposal, plan)
    directory.mkdir(parents=True, exist_ok=True)
    queue, ledger = _shadow(vault, directory, plan["state_files"])
    appended = _append(queue, ledger, entries, directory, plan["cycle_id"])
    bundles = _bundles(queue, ledger, date.fromisoformat(plan["cycle_id"]))
    return {"auto_apply": [{"candidate": op["row"]["candidate"], "band": op["band"]} for op in auto],
            "pending": appended["appended"], "notes": notes,
            "bundles": {key: {"bundle_sha256": evidence.digest(bundle), "prompt": precedent.JUDGE_SYSTEM + "\n\n" + bundle["prompt"]}
                        for key, bundle in bundles.items()}}


def _lint(vault: Path) -> dict:
    result = subprocess.run([sys.executable, str(ROOT / "scripts/lint.py"), "--json"], cwd=ROOT,
                            env={**os.environ, "OV": str(vault)}, capture_output=True, text=True, timeout=600)
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise evidence.VerificationError("lint did not produce JSON") from exc
    if result.returncode not in {0, 1} or any(type(value.get("counts", {}).get(key)) is not int for key in ("error", "warn", "info")):
        raise evidence.VerificationError("lint did not complete")
    return value


def _error_set(lint: dict) -> set[str]:
    return {evidence.digest(row) for row in lint.get("findings", []) if row.get("severity") == "ERROR"}


def _stale(vault: Path, rel: str, expected: str | None, *, note: bool) -> str | None:
    """Why the planned bytes or a note's recoverable HEAD blob no longer match."""
    try:
        path = _file(vault, rel)
        data = path.read_bytes() if path.is_file() else None
        if (None if data is None else hashlib.sha256(data).hexdigest()) != expected:
            return f"{rel} differs from its planned bytes"
        if note and data is not None and (CONFLICT_MARKER.search(data) or tree_blobs(vault, rel).get(rel) != hash_objects(vault, [rel])[0]):
            return f"{rel} has conflict markers or is not committed at HEAD"
    except (OSError, RuntimeError, evidence.VerificationError) as exc:
        return f"{rel} cannot be checked: {exc}"
    return None


def _apply(vault: Path, record: dict, path: Path, kind: str, after: dict[str, str | None],
           expected: dict[str, str | None], **fields: str) -> bool:
    """Receipt atomic file writes; skip changed sources before their first write."""
    note, plan = kind in NOTE_KINDS, record["plan"]
    scope = {"queue": [*plan["state_files"]], "audit": [f"{_segment('agent_findings')}/"]}.get(
        kind, [*plan["source_files"], f"{_segment('archive')}/decayed/"])
    if stray := [rel for rel in after if not _protected(_norm_rel(rel), scope)]:
        raise evidence.VerificationError(f"{kind} may not write {stray}")
    after = {rel: body for rel, body in after.items() if _text_hash(body) != expected[rel]}
    if not after:
        return True
    order = sorted(after, key=lambda rel: (after[rel] is None, rel))
    _ready(vault)
    if problem := next(filter(None, (_stale(vault, rel, expected[rel], note=note) for rel in order)), None):
        if not note:
            raise evidence.VerificationError(f"source or decision state changed: {problem}")
        record["notes"].append(f"skipped {kind}: {problem}")
        return False
    existing = [rel for rel in order if expected[rel] is not None]
    before_blobs = dict(zip(existing, hash_objects(vault, existing)))
    operation = {"kind": kind, "candidate_id": evidence.digest([record["run_id"], len(record["operations"]), kind, order]),
                 **fields, "state": "applying", "paths": {
                     rel: {"before_blob": before_blobs.get(rel), "before_sha256": expected[rel],
                           "after_blob": None, "after_sha256": _text_hash(after[rel])} for rel in order}}
    record["operations"].append(operation)
    evidence.write_record(path, record)
    for rel in order:
        if problem := _stale(vault, rel, expected[rel], note=note):
            raise evidence.VerificationError(f"{kind} stopped between writes: {problem}")
        if after[rel] is None:
            _file(vault, rel).unlink()
        else:
            atomic_write(_file(vault, rel), after[rel], newline="")
    written = [rel for rel in order if after[rel] is not None]
    for rel, blob in zip(written, hash_objects(vault, written)):
        operation["paths"][rel]["after_blob"] = blob
    operation["state"] = "applied"
    evidence.write_record(path, record)
    return True


def _ready(vault: Path) -> None:
    status = preflight.inspect_preflight(vault=vault, publication_boundary=True)
    if not status["ready"]:
        raise evidence.VerificationError(f"publication deferred: {status['gate']}: {status['detail']}")


def _report(vault: Path, record: dict, path: Path) -> None:
    if evidence.needs_report(record):
        rel = f"{_segment('agent_findings')}/autoevo-applied-{record['cycle_id']}.md"
        record["output_file"] = rel
        _apply(vault, record, path, "audit", {rel: evidence.render_report(record)}, {rel: _hash(_file(vault, rel))})


def accept_proposal(vault: Path, proposal: dict, plan: dict, *, flow_run_id: str, lint_check=None) -> dict:
    """Only the trusted Prefect process calls this; the model cannot write the vault."""
    proposal = evidence.validate_proposal(proposal, plan)
    _ready(vault)
    _assert_hashes(vault, plan["state_files"])
    check_lint = lint_check or _lint
    before_lint = check_lint(vault)
    cycle, today = plan["cycle_id"], date.fromisoformat(plan["cycle_id"])
    path = evidence.record_path(vault, cycle)
    if path.exists():
        raise evidence.VerificationError("cycle already has a result; do not overwrite or replay it")
    record = {"schema_version": evidence.VERSION, "cycle_id": cycle, "run_id": plan["run_id"], "prefect_flow_run_id": flow_run_id,
              "status": "prepared", "plan": plan, "proposal": proposal, "operations": [], "notes": [],
              "errors": evidence.coverage_errors(proposal, plan), "pending": [], "triage": [], "reports": {},
              "output_file": path.relative_to(vault).as_posix()}
    auto, entries, notes = route_proposal(vault, proposal, plan)
    record["notes"] = notes
    expected_state = dict(plan["state_files"])
    with tempfile.TemporaryDirectory(prefix="atelier-autoevo-accept-") as temporary:
        shadow = Path(temporary)
        queue, ledger = _shadow(vault, shadow, expected_state)
        evidence.write_record(path, record)
        try:
            if not record["errors"]:
                for op in auto:
                    row, band = op["row"], op["band"]
                    sources = sorted(set([row["candidate"], *row.get("peers", [])])) if band == "redundant-high" else [row["candidate"]]
                    if route_row(vault, row, today, plan.get("age_guard"))[:2] != ("auto_apply", band) or _curator_problem(row, plan, vault, band):
                        record["notes"].append(f"skipped {band}: {row['candidate']} no longer satisfies its band")
                        continue
                    if band == "redundant-high":
                        target = row["curator"]["target_path"]
                        after = {rel: row["curator"]["proposed_content"] if rel == target else None for rel in sources}
                    else:
                        target = f"{_segment('archive')}/decayed/{cycle}-{sources[0][:-3].replace('/', '-')}.md"
                        after = {sources[0]: None, target: _file(vault, sources[0]).read_bytes().decode("utf-8")}
                    _apply(vault, record, path, band, after,
                           {rel: plan["source_files"][rel]["before_sha256"] if rel in sources else None for rel in after})
                for entry in plan["defaults"]:
                    if entry.get("default_action") != "stale-banner":
                        continue
                    sources = list(entry.get("peers", []))
                    live = next((item for item in pending.load(vault / _segment("meta") / "autoevo_pending.toml")["pending"] if item.get("id") == entry["id"]), None)
                    if (pending.default_for(entry) != "stale-banner" or live != entry or tombstone_reason(vault, sources, today)
                            or any(rel not in plan["source_files"] for rel in sources)):
                        record["notes"].append(f"default skipped: {entry['id']} lacks eligible sources or was changed, vetoed, deferred, or tombstoned")
                        continue
                    after = {}
                    for source in sources:
                        original = _file(vault, source).read_text(encoding="utf-8")
                        after[source] = original if f"(autoevo {entry['id']})" in original else insert_stale_banner(original, stale_banner_text(cycle, entry["id"], entry["evidence_summary"]))
                    if _apply(vault, record, path, "stale-banner", after, {rel: plan["source_files"][rel]["before_sha256"] for rel in sources},
                              entry=entry["id"], category=entry["category"]):
                        _pending(queue, ledger, "resolve", "--id", entry["id"], "--status", "applied", "--reason", "default after veto window",
                                 "--today", cycle, "--source", "nightly", "--by", "rule")
            else:
                for op in auto:
                    row = op["row"]
                    peers = sorted(set([row["candidate"], *row.get("peers", [])]))
                    entries.append({"id": f"{plan['run_id']}-{evidence.digest([row['category'], peers])[:12]}", "category": row["category"],
                                    "peers": peers, "proposed_action": row["proposed_action"], "evidence_summary": row["evidence"] + "; incomplete sweep",
                                    "proposed_at": cycle, "status": "pending"})
            _assert_hashes(vault, expected_state)
            record["pending"] = _append(queue, ledger, entries + _revived_archives(vault, today, plan["run_id"]), shadow, cycle)["appended"]
            _pending(queue, ledger, "veto-expired", "--today", cycle, "--apply-dismissals")
            bundles = _bundles(queue, ledger, today)
            gate_args = precedent._gate_kwargs(precedent.build_parser().parse_args(["autoevo"]))
            for entry_id, supplied in proposal["judgments"].items():
                bundle = bundles.get(entry_id)
                if bundle is None or supplied["bundle_sha256"] != evidence.digest(bundle) or not isinstance(supplied["judgment"].get("cited", []), list):
                    record["notes"].append(f"stale or unknown precedent judgment: {entry_id}")
                    continue
                judgment = precedent.gate(bundle, supplied["judgment"], **gate_args)
                if judgment["default"]:
                    entry = next(row for row in pending.load(queue)["pending"] if row["id"] == entry_id)
                    action = "dismiss" if judgment["verdict"] == "dismiss" else pending.default_for(entry)
                    if action:
                        _pending(queue, ledger, "set-default", "--id", entry_id, "--action", action, "--today", cycle,
                                  "--reason", f"precedent ({len(judgment['cited'])} cited): {judgment['reason']}", "--by", "precedent", "--source", "nightly")
                        record["pending"].append(entry_id)
            record_undos(vault, ledger, today)
            record["triage"] = [entry for entry in pending.load(queue)["pending"] if entry["id"] in record["pending"]]
            quarantine.update_state(outcomes={str(vault / row["scope"]): row["outcome"] for row in proposal["sweeps"]},
                                    state_path=shadow / _segment("meta") / "autoevo_quarantine.toml", today=today)
            _apply(vault, record, path, "queue", {rel: (shadow / rel).read_text(encoding="utf-8") if (shadow / rel).is_file() else None
                                                  for rel in expected_state}, expected_state)
            after_lint = check_lint(vault)
            record["lint"] = {"counts": after_lint["counts"], "new_errors": sorted(_error_set(after_lint) - _error_set(before_lint))}
            if record["lint"]["new_errors"]:
                record["errors"].append("written operations introduced lint errors; review the receipted operations")
            _report(vault, record, path)
            record["status"] = "failed" if record["errors"] else "complete"
            evidence.write_record(path, record)
        except BaseException as exc:
            if record["status"] not in {"needs_review", "failed"}:
                record["status"] = "needs_review" if any(op["state"] == "applying" for op in record["operations"]) else "failed"
                record["errors"].append(f"{type(exc).__name__}: {exc}")
                evidence.write_record(path, record)
            if not any(op["kind"] == "audit" for op in record["operations"]):
                try:
                    _report(vault, record, path)
                except Exception as report_error:
                    record["errors"].append(f"review report deferred: {report_error}")
                    evidence.write_record(path, record)
            raise
    if record["status"] == "complete":
        evidence.verify_cycle(vault=vault, cycle=cycle)
    return record


def prior_result(vault: Path, cycle: str) -> dict | None:
    """Verify prior effects and return this cycle's result without replay."""
    path = evidence.record_path(vault, cycle)
    for legacy in sorted(path.parent.glob("*.toml")):
        try:
            receipt = tomllib.loads(_file(vault, legacy.relative_to(vault).as_posix()).read_text())
        except (OSError, ValueError) as exc:
            raise evidence.VerificationError(f"legacy receipt requires effects review: {legacy.name}") from exc
        if (legacy.stem == cycle or receipt.get("contract_version") != 3
                or receipt.get("routine") != "autoevo-nightly" or receipt.get("cycle_id") != legacy.stem
                or receipt.get("verification") not in {"passed", "blocked"}):
            raise evidence.VerificationError(f"legacy receipt requires effects review: {legacy.name}")
    for previous in sorted(path.parent.glob("*.json")):
        record = evidence.read_record(previous)
        if (record.get("cycle_id") != previous.stem or record.get("status") not in RESULT_STATUSES
                or not isinstance(record.get("operations"), list) or not isinstance(record.get("errors"), list)
                or (record["status"] == "failed" and not record["errors"])):
            raise evidence.VerificationError(f"unrecognized Autoevo result needs review: {previous.name}")
        if record["status"] == "complete":
            evidence.verify_cycle(vault=vault, cycle=record["cycle_id"])
        else:
            evidence.verify_operations(vault, record)
        if previous == path:
            if record["status"] == "complete":
                return record
            raise evidence.VerificationError(f"this cycle already has a {record['status']} result; no automatic replay")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preview_parser = sub.add_parser("preview", help="Read-only policy/bundle preview; writes only its explicit scratch directory")
    preview_parser.add_argument("--proposal", type=Path, required=True)
    preview_parser.add_argument("--plan", type=Path, required=True)
    preview_parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        from _paths import vault_root

        result = preview(vault_root(), json.loads(args.proposal.read_text()), json.loads(args.plan.read_text()), args.directory)
    except (OSError, ValueError, evidence.VerificationError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
