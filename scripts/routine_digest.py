"""Collect routine outputs as JSON, then write, check, mail, or acknowledge a digest.

Workflow and setup: `skills/digest/SKILL.md`. Shared persistence and delivery
rules: `protocols/remote-routines.md`. See `--help` for subcommands and flags.

Daily collection covers the effective day plus undelivered files from yesterday;
before 03:00 the effective day is yesterday. `--days`/`--since` disable carry,
while `--unacked` selects the review backlog. Weekly mode uses a seven-day window.
Daily status ledgers use a delivery cursor distinct from `routine_acks.json`:
written is not reviewed. The workflow requires user approval for `ack`.

`write` takes model-authored overview JSON and optional brief/context inputs,
renders once, and writes the canonical artifact under the declared vault output.
It selects the registry's unique `digest.include = false` row unless a routine
or destination is specified. No artifact is written without files, updates, or brief groups.
`mail` sends that artifact verbatim to the private configured recipient.
Reader keeps originals; the digest links to them and never creates Reader items.

Rendering requires preinstalled markdown-it-py and local MJML/Node packages;
it never installs or fetches. Collect/check/mail/ack need neither render dependency.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PathsError, atomic_write, fmt, vault_root  # noqa: E402
from routine_collect import (  # noqa: E402
    DIGEST_UPDATES_STATE,
    prepare_update_state,
    collect,
    load_overview,
    load_context,
    load_retrospect,
    load_brief,
)
from routine_digest_core import (  # noqa: E402
    MANIFEST_SCHEMA,
    DEFAULT_EXCERPT_CHARS,
    DEFAULT_MAX_ITEMS,
    DEFAULT_MAX_FILES,
    GMAIL_CLIP_BYTES,
    load_acks,
    deep_read_lane_gap,
    artifact_name,
    resolve_output_dir,
    manifest_names_by_dir,
    hidden_by_ack,
)
from routine_mail import mail  # noqa: E402
from routine_render import (  # noqa: E402
    render,
    check_html,
)


# `check` exit code when the artifact carries findings. Distinct from 1, which
# stays the code for a check that could not run (unreadable artifact, unset
# $OV, broken registry), so /lint can downgrade findings to WARN without also
# swallowing execution failures.
CHECK_FINDINGS_EXIT = 3

def write(
    ov: Path,
    html_text: str,
    manifest: dict[str, Any],
    *,
    routine_name: str = "",
    out: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Write the rendered document into $OV as the run's canonical artifact."""
    if out is None:
        routine = resolve_output_dir(ov, routine_name)
        out = ov / routine.output_dir / artifact_name(manifest)

    size = len(html_text.encode("utf-8"))
    if size > GMAIL_CLIP_BYTES:
        print(
            f"warning: {size / 1024:.0f} KB document; Gmail clips past "
            f"{GMAIL_CLIP_BYTES // 1000} KB and the source index will fold behind "
            "'View entire message'",
            file=sys.stderr,
        )
    if dry_run:
        print(f"would write {fmt(out)} ({size / 1024:.0f} KB)")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    state_path = ov / DIGEST_UPDATES_STATE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        exists = out.exists()
        if exists and out.read_bytes() != html_text.encode("utf-8"):
            raise SystemExit("existing digest differs; keep it or choose a new --out path")
        payload = prepare_update_state(ov, manifest)
        if not exists:
            atomic_write(out, html_text)
        if payload is not None:
            atomic_write(state_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {fmt(out)} ({size / 1024:.0f} KB)")
    return 0

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
        description="Collect routine outputs as JSON and write the digest artifact.",
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

    p_write = sub.add_parser(
        "write", help="Render into the routine's declared $OV output directory."
    )
    p_write.add_argument("--manifest", required=True, help="Manifest path, or - for stdin.")
    p_write.add_argument("--overview", help="Overview JSON written by the /digest command.")
    p_write.add_argument("--brief", help="Action-surface JSON from daily_brief.py --json.")
    p_write.add_argument("--context", help="Masthead JSON from daily_context.py --json.")
    p_write.add_argument(
        "--retrospect",
        help=(
            "Picks from retrospect.py --json. Only entries a reviewer marked "
            "reviewed are rendered; the rest are dropped here as well as at draw time."
        ),
    )
    p_write.add_argument(
        "--routine",
        help=(
            "Routine whose output_dir receives the artifact. Defaults to the one "
            "private routine row carrying digest = { include = false }."
        ),
    )
    p_write.add_argument("--out", help="Explicit destination, overriding --routine.")
    p_write.add_argument("--dry-run", action="store_true", help="Report the path, write nothing.")

    p_mail = sub.add_parser(
        "mail", help="Send an artifact to the configured account, and nowhere else."
    )
    p_mail.add_argument("--html", required=True, help="Rendered artifact to send.")
    p_mail.add_argument("--subject", required=True, help="Message subject.")
    p_mail.add_argument("--dry-run", action="store_true", help="Report, send nothing.")

    p_ack = sub.add_parser("ack", help="Advance routine_acks.json past digested files.")
    p_ack.add_argument("--manifest", required=True)
    p_ack.add_argument("--dry-run", action="store_true")

    p_check = sub.add_parser(
        "check",
        help=(
            "Assert the rendered invariants on a digest artifact. Exit "
            f"{CHECK_FINDINGS_EXIT} on findings, 1 on an execution failure, 0 when clean."
        ),
    )
    p_check.add_argument(
        "--html",
        help="Artifact to check. Default: the newest *-digest.html in the digest routine's output_dir.",
    )
    p_check.add_argument("--routine", help="Digest routine name when the registry excludes several.")

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
        overview = load_overview(Path(args.overview) if args.overview else None)
        picks = load_retrospect(Path(args.retrospect) if args.retrospect else None)
        context = load_context(Path(args.context) if args.context else None)
        gap = deep_read_lane_gap(overview.get("deep_read"), manifest)
        if gap:
            print(f"warning: {gap}", file=sys.stderr)
        document = render(manifest, overview, brief, picks, context)
        # The artifact checks itself before it is written: a finding here is
        # a report on the inputs (or the renderer), never a reason to skip
        # the morning's document.
        for finding in check_html(document):
            print(f"check: {finding}", file=sys.stderr)
        return write(
            ov,
            document,
            manifest,
            routine_name=args.routine or "",
            out=Path(args.out) if args.out else None,
            dry_run=args.dry_run,
        )

    if args.cmd == "mail":
        document = Path(args.html).read_text(encoding="utf-8")
        return mail(ov, document, args.subject, dry_run=args.dry_run)

    if args.cmd == "check":
        if args.html:
            target = Path(args.html)
        else:
            routine = resolve_output_dir(ov, args.routine or "")
            candidates = list((ov / routine.output_dir).glob("*-digest.html"))
            if not candidates:
                # Nothing to check is not "clean": the check could not run.
                print(f"no *-digest.html under {fmt(ov / routine.output_dir)}", file=sys.stderr)
                return 1
            # Newest by modification time: a name sort would rank a same-day
            # weekly above the daily and miss an older file rendered again.
            target = max(candidates, key=lambda path: path.stat().st_mtime)
        try:
            document = target.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"artifact unreadable: {exc!r}", file=sys.stderr)
            return 1
        findings = check_html(document)
        for finding in findings:
            print(f"check: {finding}")
        print(f"{fmt(target)}: {len(findings)} finding(s)")
        # Findings are advisory and get their own code, so a caller can tell
        # "the document has a problem" from "the check could not run".
        return CHECK_FINDINGS_EXIT if findings else 0

    if args.cmd == "ack":
        manifest = _load_manifest(args.manifest)
        return ack(ov, manifest, dry_run=args.dry_run)

    return 1

if __name__ == "__main__":
    sys.exit(main())
