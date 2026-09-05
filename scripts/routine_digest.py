"""routine_digest.py: Roll up scheduled-routine outputs into one morning document.

Why this exists: scheduled routines write dated reports into a dozen
directories under $OV. `cues.py check_routine_outputs` only says "N new outputs,
go read them"; nothing turned that pile into something readable away from a
terminal, and review debt had reached hundreds of unread files.

This script is the deterministic half of the digest. It selects which routine
outputs fall in a window, extracts a bounded, quotable projection of each one,
renders one semantic HTML document, and writes it into $OV as the run's
canonical artifact. The judgment half -- the cross-source overview prose -- is
written by the `/digest` command and injected through `--overview` as
structured JSON, so the renderer stays deterministic and testable.

An optional private `$OV/_meta/digest_updates.toml` also declares append-only
Markdown ledgers whose rows are mandatory status updates. New rows are rendered
deterministically above the overview: daily delivery uses a write cursor so a
late-day change reaches the next morning exactly once, while weekly delivery
uses the report's seven-day window. The cursor records delivery only and is
separate from `routine_acks.json`, which means the user reviewed the material.

Per `protocols/remote-routines.md`, the artifact under $OV is the source of
truth and the morning email is a presentation of it. Delivery is deterministic
and lives in `mail`, not in the model: the recipient comes from private config,
the body is the artifact verbatim, and nothing between the two can redirect a
message. That is not only a safety preference. The Codex Gmail plugin marks
`send_email` as requiring approval, and unattended routines run under
`approval_policy = "never"`, so a model-sent message is not possible there at
all (verified 2026-08-31).

Nothing generated here is ever pushed into Readwise: Reader stores originals,
and the digest links into it rather than adding to it.

The document's first screen is the action surface from `daily_brief.py --json`,
passed in through `--brief`. It renders above the overview because it is the
only part read before the day's deep work; anything placed above it spends that
attention. Without `--brief` the document is intel only, which is the right
shape for the weekly roll-up.

Stdlib-only on purpose. The digest is summary-depth, so no source body is
converted wholesale; only a bounded inline-markdown subset (links, bold, code)
appears in overview bullets. That removes any need for a markdown dependency.

Subcommands:
    collect     select routine outputs in a window -> manifest JSON
    render      manifest (+ optional brief and overview) -> HTML on stdout
    write       same render, into the routine's declared $OV output directory
    mail        send an artifact to the account in $OV/_meta/mail.toml
    ack         advance $OV/_meta/routine_acks.json past the digested files

Typical flow (the `/digest` command automates the middle step; `$PY` is the
interpreter from `scripts/find_python.sh`, never `uv run`, because the routine
sandbox cannot sync uv and these scripts are stdlib-only):

    $PY scripts/routine_digest.py collect --mode daily --json > m.json
    $PY scripts/daily_brief.py --json --out brief.json      # daily only
    # ... model reads m.json and brief.json, writes overview.json ...
    $PY scripts/routine_digest.py write --manifest m.json         --brief brief.json --overview overview.json
    $PY scripts/routine_digest.py mail --html <artifact> --subject "<title>"
    $PY scripts/routine_digest.py ack --manifest m.json

`write` finds the digest's own registry row by its `include = false` mark;
`--routine <name>` is only needed when several rows are excluded.

Registry: `$OV/_meta/routine_watch.toml` is the source of truth for which
routines exist, where they write, and which filenames belong to them. Per-routine
digest policy is optional and lives in the same rows, so this script stays
vault-agnostic:

    [[routine]]
    name = "..."
    output_dir = "..."
    file_pattern = "*.md"
    digest = { lane = "Finance", include = true }

`include = false` drops a routine from every digest. The only routine excluded
by default is the public harness maintenance job `autoevo-nightly`, whose output
is applied-decay bookkeeping rather than gathered intel; `--include-maintenance`
overrides that.

Window semantics: `--mode daily` covers one effective day, `--mode weekly` the
7 days ending on it. Before 03:00 local the effective day is the previous date,
matching the day boundary the rest of the harness uses. The default daily
selection also carries back files dated the previous day that no earlier daily
digest delivered (a routine that finished after that morning's run); `write`
records delivered paths in `_meta/digest_update_state.json`, and the manifest
marks carried sources so the document can label them. `--days` or `--since`
disables the carry. `--unacked` ignores the window entirely and takes
everything past each directory's ack, for clearing backlog.

Exit codes: 0 on success. An empty window is a success that writes nothing, by
design: a digest with no content is worse than an absent one, and the routine
reports the empty window instead of mailing a hollow document. 1 on a real
failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PathsError, atomic_write, fmt, vault_root  # noqa: E402
from routine_collect import (  # noqa: E402,F401  (re-exported public surface)
    OVERVIEW_SCHEMA,
    BRIEF_SCHEMA,
    CONTEXT_SCHEMA,
    DIGEST_UPDATES_CONFIG,
    DIGEST_UPDATES_STATE,
    LANE_ORDER,
    _DATE_IN_NAME,
    _FRONTMATTER,
    _FENCED_BLOCK,
    _META_LINE,
    _SOURCE_URL,
    _LIST_LINK,
    _TABLE_LINK,
    _BARE_TABLE_URL,
    _H1,
    _ANY_HEADING,
    _PAYLOAD_HEADINGS,
    _ANY_MD_LINK,
    _GENERIC_HEADINGS,
    UNIT_EXCERPT_CHARS,
    MAX_UNITS_PER_FILE,
    DigestUpdateSource,
    load_update_sources,
    load_update_state,
    _markdown_cells,
    _markdown_table,
    collect_digest_updates,
    advance_update_state,
    effective_date,
    resolve_window,
    _parse_date,
    file_date,
    parse_frontmatter,
    _looks_like_meta_block,
    split_units,
    _parse_meta_lines,
    extract_headline,
    _META_TAILS,
    _INLINE_NOTE_LEAD,
    _strip_meta_tail,
    _BOLD_RUN,
    _item_note,
    extract_items,
    extract_excerpt,
    strip_inline_markup,
    _split_sections,
    _prose_lines,
    Source,
    collect,
    collect_health,
    _build_source,
    _unit_dict,
    _group_lanes,
    _source_dict,
    load_overview,
    load_context,
    load_retrospect,
    load_brief,
    DAILY_CARRY_DAYS,
    DELIVERED_RETENTION_DAYS,
    load_delivered_state,
    strip_frontmatter,
)
from routine_digest_core import (  # noqa: E402,F401  (re-exported public surface)
    MANIFEST_SCHEMA,
    MAINTENANCE_ROUTINES,
    DEFAULT_EXCERPT_CHARS,
    DEFAULT_MAX_ITEMS,
    DEFAULT_MAX_FILES,
    GMAIL_CLIP_BYTES,
    LANE_BY_SEGMENT,
    RESEARCH_LANE,
    _CARD,
    _S_ITEM,
    DEFAULT_ROUTINE_LINES,
    Routine,
    load_routines,
    default_lane,
    load_acks,
    humanize_slug,
    _vault_relative,
    source_anchor,
    iter_sources,
    _within_days,
    deep_read_lane_gap,
    DEEP_TARGET_MINUTES,
    artifact_name,
    resolve_output_dir,
    manifest_names_by_dir,
    hidden_by_ack,
)
from routine_mail import (  # noqa: E402,F401  (re-exported public surface)
    MAIL_CONFIG,
    load_mail_config,
    KEYCHAIN_TIMEOUT_SECONDS,
    _refuse_vault_secret,
    smtp_password,
    build_message,
    mail,
)
from routine_render import (  # noqa: E402,F401  (re-exported public surface)
    _INLINE_LINK,
    _INLINE_CODE,
    _INLINE_BOLD,
    _FONT,
    _MONO,
    _SERIF,
    _INK,
    _MUTED,
    _FAINT,
    _RULE,
    _ACCENT,
    _LINK,
    _CHIP,
    _URGENT,
    _AMBER,
    _OK,
    _BAR_TRACK,
    _S_WRAP,
    _S_MAST,
    _S_H1,
    _S_H1_DATE,
    _S_MAST_SIDE,
    _S_WEATHER,
    _S_WEATHER_SUB,
    _S_META,
    _S_CARD,
    _S_CARD_H,
    _S_CARD_H_N,
    _S_GROUP_HOT,
    _S_GROUP_COOL,
    _S_LEDGER,
    _S_LEDGER_DUE,
    _S_LEDGER_ITEM,
    _S_LEDGER_HEAD,
    _S_UL,
    _S_TRACE,
    _S_LI,
    _S_H2,
    _S_H2_N,
    _S_LEAD,
    _S_P,
    _S_SMALL,
    _S_CODE,
    _S_LINK,
    _S_SRC_LINK,
    _S_INDEX_H,
    _S_INDEX_UL,
    _S_INDEX_LI,
    _S_NOTE,
    _S_STAT_TABLE,
    _S_STAT_CELL,
    _S_STAT_CELL_FIRST,
    _S_STAT_NUM,
    _S_STAT_LABEL,
    _S_QUOTA_TABLE,
    _S_QUOTA_CELL,
    _S_QUOTA_CELL_NEXT,
    _S_QUOTA_NAME,
    _S_QUOTA_SUB,
    _S_QUOTA_BAR,
    _S_QUOTA_SEG,
    _S_QUOTA_LEFT,
    _S_QUOTA_META,
    _S_QUOTA_SNAP,
    _S_FOLD,
    _S_FOLD_RULE,
    _S_FOLD_TEXT,
    _S_COST,
    _S_DEEP_ITEM,
    _S_DEEP_HEAD,
    _S_DEEP_BODY,
    _S_RETRO_META,
    _S_MD_P,
    _S_MD_H2,
    _S_MD_H3,
    _S_MD_UL,
    _S_MD_LI,
    _S_MD_TABLE,
    _S_MD_TH,
    _S_MD_TD,
    _S_ART,
    _S_ART_FIRST,
    _S_ART_TITLE,
    _S_ART_META,
    _S_ART_WHY,
    _S_ART_ABSTRACT,
    _S_LAB_TABLE,
    _S_LAB_NAME,
    _S_LAB_CAT,
    _S_LAB_TEXT,
    _S_LAB_NOTE,
    _S_DR_GROUP,
    _S_DR_ITEM,
    _S_DR_TITLE,
    _S_DR_UL,
    _S_DR_LI,
    _S_DR_WHY,
    _S_RULE,
    _S_COLOPHON,
    _INDEX_ITEM_CAP,
    _INDEX_EXCERPT_CAP,
    inline_html,
    _CJK_PER_MINUTE,
    _WORDS_PER_MINUTE,
    _CJK,
    _TAGS,
    reading_minutes,
    digest_title,
    render,
    _cost_badge,
    _render_masthead,
    _HEADING_OVERDUE,
    _FIRST_INT,
    _brief_counts,
    WEIGHT_STALE_DAYS,
    DECISION_SECTION,
    SIGNAL_SECTION,
    _decision_count,
    decision_shape_missing,
    normalize_decisions,
    _render_decision_bullet,
    _trace_label,
    feed_note_gap,
    _countdown_repeated,
    check_html,
    _S_HINT,
    _S_FLAG,
    _S_DEC_CARD,
    _S_DEC_KEY,
    _TRACE_STEM_CHARS,
    _render_signals,
    _fleet_bits,
    _QUOTA_COLOUR,
    _render_quota,
    _due_cell,
    _render_brief,
    _render_updates,
    _render_feed_health,
    _render_routine_briefs,
    _render_articles,
    _articles_badge,
    _render_frontier,
    _render_deep_read_curated,
    _render_feed_items,
    _deep_read_badge,
    _render_section,
    _render_bullet,
    DEEP_PER_SOURCE_CHARS,
    DEEP_TOTAL_BYTES,
    _MD_HEADING,
    _MD_LIST,
    _MD_TABLE_ROW,
    _MD_TABLE_RULE,
    _MD_IMAGE,
    markdown_to_html,
    _render_deep_read,
    _render_retrospect,
    _render_source,
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
    atomic_write(out, html_text)
    advance_update_state(ov, manifest)
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

def text_report(manifest: dict[str, Any]) -> str:
    counts = manifest.get("counts", {})
    window = manifest.get("window", {})
    lines = [
        f"{digest_title(manifest)}",
        f"selection={manifest.get('selection')} window={window.get('since')}..{window.get('until')} "
        f"routines={counts.get('routines', 0)} files={counts.get('files', 0)} "
        f"updates={counts.get('updates', 0)} bytes={counts.get('bytes', 0)}",
    ]
    for lane in manifest.get("lanes", []):
        lines.append(f"\n{lane.get('lane')} ({lane.get('files')})")
        for source in lane.get("sources", []):
            marks = []
            if source.get("units"):
                marks.append(f"{len(source['units'])} units")
            if source.get("items"):
                marks.append(f"{len(source['items'])} items")
            suffix = f" [{', '.join(marks)}]" if marks else ""
            headline = source.get("headline") or "(no headline)"
            lines.append(f"  {source.get('date')}  {source.get('label')}: {headline}{suffix}")
    if manifest.get("updates"):
        lines.append("\nStatus updates")
        for update in manifest["updates"]:
            fields = " · ".join(
                f"{key}: {value}" for key, value in (update.get("values") or {}).items()
            )
            lines.append(f"  {update.get('date')}  {update.get('label')}: {fields}")
    for warning in manifest.get("update_warnings") or []:
        lines.append(f"warning: {warning}")
    if manifest.get("skipped_routines"):
        lines.append(f"\nexcluded: {', '.join(manifest['skipped_routines'])}")
    if manifest.get("truncated"):
        lines.append("truncated by --max-files")
    return "\n".join(lines)

def _load_manifest(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"manifest unreadable: {exc!r}") from exc
    if data.get("schema") != MANIFEST_SCHEMA:
        raise SystemExit(f"manifest schema {data.get('schema')} unsupported")
    return data

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Roll up scheduled-routine outputs into a Readwise document.",
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
    p_collect.add_argument(
        "--feeds",
        help="RSS/Atom items JSON ({\"items\": [...]}) from any fetcher; its channel counts ride along in the manifest.",
    )
    p_collect.add_argument("--json", action="store_true", help="JSON manifest instead of a report.")
    p_collect.add_argument("--out", help="Write output to a file instead of stdout.")

    p_render = sub.add_parser("render", help="Render a manifest to HTML.")
    p_render.add_argument("--manifest", required=True, help="Manifest path, or - for stdin.")
    p_render.add_argument("--overview", help="Overview JSON written by the /digest command.")
    p_render.add_argument("--brief", help="Action-surface JSON from daily_brief.py --json.")
    p_render.add_argument("--context", help="Masthead JSON from daily_context.py --json.")
    p_render.add_argument(
        "--retrospect",
        help=(
            "Picks from retrospect.py --json. Only entries a reviewer marked "
            "reviewed are rendered; the rest are dropped here as well as at draw time."
        ),
    )
    p_render.add_argument("--out", help="Write HTML to a file instead of stdout.")

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
            "routine_watch.toml row carrying digest = { include = false }."
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
        if args.feeds:
            try:
                manifest["feeds"] = json.loads(Path(args.feeds).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                print(f"feed payload unreadable: {exc!r}", file=sys.stderr)
        payload = (
            json.dumps(manifest, indent=2, ensure_ascii=False)
            if args.json
            else text_report(manifest)
        )
        if args.out:
            Path(args.out).write_text(payload + "\n", encoding="utf-8")
            print(
                f"wrote {args.out} ({manifest['counts']['files']} files, "
                f"{manifest['counts'].get('updates', 0)} updates)"
            )
        else:
            print(payload)
        return 0

    if args.cmd == "render":
        manifest = _load_manifest(args.manifest)
        overview = load_overview(Path(args.overview) if args.overview else None)
        brief = load_brief(Path(args.brief) if args.brief else None)
        picks = load_retrospect(Path(args.retrospect) if args.retrospect else None)
        context = load_context(Path(args.context) if args.context else None)
        document = render(manifest, overview, brief, picks, context)
        if args.out:
            Path(args.out).write_text(document, encoding="utf-8")
            print(f"wrote {args.out} ({len(document.encode('utf-8')) / 1024:.0f} KB)")
        else:
            sys.stdout.write(document)
        return 0

    if args.cmd == "write":
        manifest = _load_manifest(args.manifest)
        counts = manifest.get("counts", {})
        if not counts.get("files") and not counts.get("updates"):
            print("empty window; nothing written")
            return 0
        overview = load_overview(Path(args.overview) if args.overview else None)
        brief = load_brief(Path(args.brief) if args.brief else None)
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
