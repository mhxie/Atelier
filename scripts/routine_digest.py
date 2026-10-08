"""Collect routine outputs as JSON, then write the digest note or acknowledge files.

Workflow and setup: `skills/digest/SKILL.md`. Shared persistence and delivery
rules: `protocols/remote-routines.md`. See `--help` for subcommands and flags.

Daily collection covers the effective day plus undelivered files from yesterday;
before 03:00 the effective day is yesterday. `--days`/`--since` disable carry,
while `--unacked` selects the review backlog. Weekly mode uses a seven-day window.
Daily status ledgers use a delivery cursor distinct from `routine_acks.json`:
written is not reviewed, and a same-day recollect replays the day's rows.
The workflow requires user approval for `ack`.

`write` renders one Reflect-native note at `<paths.digest>/YYYY-MM/` from the
manifest, the model-authored overview, and optional brief/context inputs. It
creates the note when absent and replaces it only while it still holds what
the harness last wrote there, or with `--replace` after approval. `morning` is
the scheduled, model-free writer: it creates the effective day's note and never
replaces one. No note is written without files, updates, or brief groups.
Reader keeps originals; the note links to them and never creates Reader items.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PathsError, atomic_write, date_in_text, fmt, retry_transient, vault_root  # noqa: E402
from _reflect import TitleIndex, nonnative  # noqa: E402
from routine_collect import (  # noqa: E402
    DELIVERED_RETENTION_DAYS,
    DIGEST_UPDATES_STATE,
    TODO_REMINDER_LIMIT,
    _load_state_payload,
    prepare_update_state,
    collect,
    effective_date,
    load_overview,
    load_context,
    load_brief,
)
from routine_digest_core import (  # noqa: E402
    MANIFEST_SCHEMA,
    DEFAULT_EXCERPT_CHARS,
    DEFAULT_MAX_ITEMS,
    DEFAULT_MAX_FILES,
    load_acks,
    deep_read_lane_gap,
    note_path,
    manifest_names_by_dir,
    hidden_by_ack,
)
from digest_note import render  # noqa: E402


# `write` exit code when it refuses: the note changed since the harness wrote it
# (a Reflect or phone edit, a retitled link), or the render differs from the
# approved preview. 1 stays the code for a failure.
REFUSED_EXIT = 3
# Frontmatter Reflect owns; it survives every re-render.
REFLECT_KEYS = ("id", "pinned", "private", "aliases", "gist", "ignoredContacts")
ERROR_LOG = "_meta/digest_last_error.txt"


def _sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _when(stamp: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp)).astimezone()
    except ValueError:
        return None


def _carry_frontmatter(current: str, text: str) -> str:
    """Reflect's own keys (pin, privacy, aliases, id) move into the new note."""
    head, sep, _ = current[4:].partition("\n---\n") if current.startswith("---\n") else ("", "", "")
    kept, keep = [], False
    for line in head.splitlines() if sep else []:
        continued = line[:1].isspace() or line == "-" or line.startswith("- ")  # YAML lists may sit at column 0
        keep = keep if continued else line.split(":", 1)[0].strip() in REFLECT_KEYS
        kept += [line] if keep else []
    return text.replace("\n---\n", "\n" + "\n".join(kept) + "\n---\n", 1) if kept else text


def _record(state: dict[str, Any], rel: str, text: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Remember the bytes the harness wrote at `rel`, for the next compare; keep two weeks."""
    notes = state.get("notes") if isinstance(state.get("notes"), dict) else {}
    floor = date.fromisoformat(str(manifest["window"]["until"])[:10]) - timedelta(days=DELIVERED_RETENTION_DAYS)
    kept = {key: value for key, value in notes.items() if (date_in_text(Path(key).name) or floor) >= floor}
    kept[rel] = {"sha256": _sha(text), "generated": str(manifest.get("generated") or "")}
    return {**state, "notes": dict(sorted(kept.items()))}


def _titles(ov: Path) -> TitleIndex | None:
    """The vault's title index; None (notes cited by path) when the mount refuses."""
    try:
        return retry_transient(lambda: TitleIndex(ov), what="title index")
    except OSError as exc:
        print(f"warning: title index unavailable ({exc.errno}); notes cited by path", file=sys.stderr)
        return None


def write(
    ov: Path,
    text: str,
    manifest: dict[str, Any],
    *,
    out: Path | None = None,
    dry_run: bool = False,
    brief: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    create_only: bool = False,
    replace: str = "",
    expect: str = "",
) -> int:
    """Publish the note: compare, then the note, then delivery state, under one lock.

    Previews (`out`, `dry_run`) record nothing. A note is created when absent;
    otherwise it is replaced only while its bytes hash to what the harness
    last wrote there, or to `replace`, the hash the user approved replacing,
    and never from an older collection. `create_only` (the scheduled run)
    leaves any existing note alone. `expect` ties the write to the approved
    preview's hash.
    """
    for kind, count in nonnative(text).items():
        print(f"warning: {count} non-native {kind} in the note", file=sys.stderr)
    if expect and _sha(text) != expect:
        print("the note differs from the approved preview; preview it again", file=sys.stderr)
        return REFUSED_EXIT
    target = note_path(ov, manifest)
    rel = target.relative_to(ov).as_posix()
    size = f"{len(text.encode('utf-8')) / 1024:.0f} KB"
    if out is not None or dry_run:
        if out is not None:
            if out.resolve().is_relative_to(ov.resolve()):
                raise SystemExit("--out previews stay outside $OV")
            atomic_write(out, text)
        print(f"{'previewed ' + str(out) if out else 'would write $OV/' + rel} ({size}; sha256 {_sha(text)})")
        return 0

    state_path = ov / DIGEST_UPDATES_STATE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = target.read_bytes() if target.exists() else None
        if current is not None and create_only:
            print(f"kept $OV/{rel}; it already exists")
            return 0
        old: str | None = None
        if current is not None:
            payload = _load_state_payload(ov)[0]
            notes, replay = payload.get("notes"), payload.get("replay")
            record = notes.get(rel) if isinstance(notes, dict) and isinstance(notes.get(rel), dict) else {}
            with contextlib.suppress(UnicodeDecodeError):
                old = current.decode("utf-8")
            text = _carry_frontmatter(old or "", text)
            if old != text:
                if old is None:
                    print(f"$OV/{rel} is not UTF-8 text; inspect it or move it aside, then write again",
                          file=sys.stderr)
                    return REFUSED_EXIT
                if _sha(current) not in (record.get("sha256"), replace):
                    print(f"$OV/{rel} changed since the harness wrote it (sha256 {_sha(current)}); show the "
                          f"user, then rerun with --replace {_sha(current)} only after approval", file=sys.stderr)
                    return REFUSED_EXIT
                newer, ours = _when(record.get("generated")), _when(manifest.get("generated"))
                if newer and ours and ours < newer:
                    raise SystemExit(f"$OV/{rel} was written from a newer collection; recollect before writing")
                latest = str(replay.get("day") or "") if isinstance(replay, dict) else ""
                until = str(manifest["window"]["until"])[:10]
                if manifest.get("mode") == "daily" and latest > until:
                    # Its status updates moved on with the cursor, so a rebuild would silently drop them.
                    raise SystemExit(f"$OV/{rel} predates the {latest} note; its status updates cannot be "
                                     "replayed, so it stays as written")
        state = prepare_update_state(ov, manifest)
        if manifest.get("mode") == "daily":
            day = str(manifest["window"]["until"])[:10]
            if brief and brief.get("date") != day:
                raise SystemExit("brief date differs from digest; rebuild the brief")
            if context and context.get("date", day) != day:
                raise SystemExit("context date differs from digest; rebuild the context")
            reminders = state["todo_reminders"]
            for group in (brief or {}).get("groups", []):
                for item in group.get("items", []) if group.get("kind") == "todo_now" else []:
                    if item.get("reminder_id") and item.get("days_left", 0) < 0:
                        shown = reminders.setdefault(item["reminder_id"], [])
                        if day not in shown:
                            if len(shown) >= TODO_REMINDER_LIMIT:
                                raise SystemExit("TODO reminder limit reached; rebuild the brief")
                            shown.append(day)
        if current is None:
            atomic_write(target, text)
        elif old != text:
            try:
                atomic_write(target, text, expected_text=old)
            except ValueError:
                print(f"$OV/{rel} changed during the write; nothing recorded", file=sys.stderr)
                return REFUSED_EXIT
        atomic_write(state_path, json.dumps(_record(state, rel, text, manifest), indent=2, ensure_ascii=False) + "\n")
    files, updates = ((manifest.get("counts") or {}).get(key, 0) for key in ("files", "updates"))
    print(f"{'unchanged' if old == text else 'wrote'} $OV/{rel} ({size}; {files} files, {updates} updates)")
    return 0


def morning(ov: Path, *, refresh_quota: bool = False, no_weather: bool = False, now: datetime | None = None) -> int:
    """The scheduled, model-free note: one effective day, created only when absent.

    Prints one JSON line and nothing private, because process output reaches the
    Prefect log and a world-readable launchd log unscreened: helper warnings go
    into the note's 输入缺口 and a failure's detail into `_meta/digest_last_error.txt`.
    """
    import daily_brief
    import daily_context

    day = effective_date(now)
    target = note_path(ov, {"mode": "daily", "window": {"until": day.isoformat()}})
    rel = target.relative_to(ov).as_posix()
    summary: dict[str, Any] = {"status": "exists", "note": f"$OV/{rel}"}
    if target.exists():  # a late run, a retry, or /digest came first; before any quota refresh
        notes = _load_state_payload(ov)[0].get("notes")
        if not (isinstance(notes, dict) and rel in notes):  # a note whose state write never landed
            summary["status"] = "unrecorded"
        print(json.dumps(summary, ensure_ascii=False))
        return 1 if summary["status"] == "unrecorded" else 0
    log, stage = io.StringIO(), "collect"
    try:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            manifest = collect(ov, mode="daily", until=day.isoformat())
            stage = "brief"
            brief = daily_brief.build(ov, day)
            counts = manifest["counts"]
            summary.update(status="empty", files=counts["files"], updates=counts["updates"], groups=len(brief["groups"]))
            if counts["files"] or counts["updates"] or brief["groups"]:
                stage = "context"
                context = daily_context.build(day, place=None, ov=ov, refresh_quota=refresh_quota, no_weather=no_weather)
                stage = "render"
                titles = _titles(ov)
                manifest["context_warnings"] += [line for line in log.getvalue().splitlines() if line.strip()]
                text = render(manifest, None, brief, context, titles=titles)
                stage = "write"
                write(ov, text, manifest, brief=brief, context=context, create_only=True)
                written = target.read_bytes() == text.encode("utf-8")
                summary.update(status="wrote" if written else "exists", nonnative=sum(nonnative(text).values()))
        (ov / ERROR_LOG).unlink(missing_ok=True)
    except (SystemExit, Exception) as exc:  # noqa: BLE001 - the message may name private paths
        with contextlib.suppress(OSError):
            atomic_write(ov / ERROR_LOG, f"{datetime.now().isoformat(timespec='seconds')} {stage}: {exc!r}\n"
                                         f"{traceback.format_exc()}\n{log.getvalue()}")
        summary.update(status="failed", stage=stage, error=type(exc).__name__)
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if summary["status"] == "failed" else 0


def ack(ov: Path, manifest: dict[str, Any], *, dry_run: bool = False) -> int:
    """Advance routine_acks.json past every file this digest covered.

    Same key space and comparison as `cues.py check_routine_outputs`
    ({output_dir: last_acked_filename}, string compare), so acking here is what
    clears the session-start review-debt cue. Never moves an ack backwards.
    """
    targets = manifest.get("acks") or {}
    if not targets:
        print("nothing to ack (empty manifest)")
        return 0
    current = load_acks(ov)
    updated = dict(current)
    changes: list[str] = []
    shown = manifest_names_by_dir(manifest)
    for output_dir, name in sorted(targets.items()):
        before = current.get(output_dir, "")
        if name > before:
            updated[output_dir] = name
            changes.append(f"  {output_dir}: {before or '∅'} → {name}")
            hidden = hidden_by_ack(ov, output_dir, before, name, shown.get(output_dir, set()))
            for label, count in hidden:
                changes.append(
                    f"    warning: also marks {count} unshown {label} file(s) in "
                    f"{output_dir} as reviewed (shared directory, ack is per directory)"
                )
    if not changes:
        print("acks already current")
        return 0
    print("\n".join(changes))
    if dry_run:
        print("(dry run; no write)")
        return 0
    path = ov / "_meta" / "routine_acks.json"
    atomic_write(path, json.dumps(updated, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"updated {fmt(path)} ({len(changes)} directories)")
    return 0

def _load_manifest(path: str) -> dict[str, Any]:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"manifest unreadable: {exc!r}") from exc
    if not isinstance(data, dict):
        raise SystemExit("manifest must be a JSON object")
    if data.get("schema") != MANIFEST_SCHEMA:
        raise SystemExit(f"manifest schema {data.get('schema')} unsupported")
    return data

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect routine outputs as JSON and write the digest note.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_collect = sub.add_parser("collect", help="Select routine outputs in a window.")
    p_collect.add_argument("--mode", choices=["daily", "weekly"], default="weekly")
    p_collect.add_argument("--days", type=int, help="Override window length in days.")
    p_collect.add_argument("--since", help="Window start (YYYY-MM-DD).")
    p_collect.add_argument("--until", help="Window end (YYYY-MM-DD); default effective today.")
    p_collect.add_argument(
        "--unacked", action="store_true", help="Ignore the window; take everything past each ack."
    )
    p_collect.add_argument(
        "--include-maintenance", action="store_true", help="Include harness-maintenance routines."
    )
    p_collect.add_argument("--excerpt-chars", type=int, default=DEFAULT_EXCERPT_CHARS)
    p_collect.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    p_collect.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    p_collect.add_argument("--json", action="store_true", required=True, help="Emit a JSON manifest.")
    p_collect.add_argument("--out", help="Write output to a file instead of stdout.")

    p_write = sub.add_parser("write", help="Render the note into <paths.digest>/YYYY-MM/.")
    p_write.add_argument("--manifest", required=True, help="Manifest path, or - for stdin.")
    p_write.add_argument("--overview", help="Overview JSON written by the /digest command.")
    p_write.add_argument("--brief", help="Action-surface JSON from daily_brief.py --json.")
    p_write.add_argument("--context", help="Masthead JSON from daily_context.py --json.")
    p_write.add_argument("--out", help="Preview outside $OV; records nothing.")
    p_write.add_argument("--dry-run", action="store_true", help="Report the path and hash, write nothing.")
    p_write.add_argument("--expect", default="", help="sha256 of the approved preview; refuse a different render.")
    p_write.add_argument("--replace", default="", help="sha256 of a changed note the user approved replacing.")

    p_morning = sub.add_parser("morning", help="Scheduled: create the effective day's note unless it exists.")
    p_morning.add_argument("--refresh-quota", action="store_true", help="Refresh quota through CodexBar first.")
    p_morning.add_argument("--no-weather", action="store_true", help="Skip the weather fetch.")

    p_ack = sub.add_parser("ack", help="Advance routine_acks.json past digested files.")
    p_ack.add_argument("--manifest", required=True)
    p_ack.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)

    try:
        ov = vault_root()
    except PathsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.cmd == "collect":
        manifest = collect(
            ov,
            mode=args.mode,
            days=args.days,
            since=args.since,
            until=args.until,
            unacked=args.unacked,
            include_maintenance=args.include_maintenance,
            excerpt_chars=args.excerpt_chars,
            max_items=args.max_items,
            max_files=args.max_files,
        )
        payload = json.dumps(manifest, indent=2, ensure_ascii=False)
        if args.out:
            Path(args.out).write_text(payload + "\n", encoding="utf-8")
            print(
                f"wrote {args.out} ({manifest['counts']['files']} files, "
                f"{manifest['counts'].get('updates', 0)} updates)"
            )
        else:
            print(payload)
        return 0

    if args.cmd == "write":
        manifest = _load_manifest(args.manifest)
        brief = load_brief(Path(args.brief) if args.brief else None)
        counts = manifest.get("counts", {})
        if not counts.get("files") and not counts.get("updates") and not brief.get("groups"):
            print("empty window; nothing written")
            return 0
        overview = load_overview(Path(args.overview)) if args.overview else None
        context = load_context(Path(args.context) if args.context else None)
        gap = deep_read_lane_gap((overview or {}).get("deep_read"), manifest)
        if gap:
            print(f"warning: {gap}", file=sys.stderr)
        text = render(manifest, overview, brief, context=context, titles=_titles(ov))
        return write(
            ov,
            text,
            manifest,
            out=Path(args.out) if args.out else None,
            dry_run=args.dry_run,
            brief=brief,
            context=context,
            replace=args.replace,
            expect=args.expect,
        )

    if args.cmd == "morning":
        return morning(ov, refresh_quota=args.refresh_quota, no_weather=args.no_weather)

    if args.cmd == "ack":
        manifest = _load_manifest(args.manifest)
        return ack(ov, manifest, dry_run=args.dry_run)

    return 1

if __name__ == "__main__":
    sys.exit(main())
