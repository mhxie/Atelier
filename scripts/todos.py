#!/usr/bin/env python3
"""Aggregate open GTD checkboxes and reflection Next Actions.

GTD markers carry explicit state; DONE/KILLED prefixes close unboxed reflection
actions. Optional due date, priority, and area metadata drive list, stale, and
digest views. Missing explicit priority is derived from due date and git age.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from functools import lru_cache
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import atomic_write, tier, tier_files, vault_root  # type: ignore[import-not-found]  # noqa: E402
from _reflect import TitleIndex  # type: ignore[import-not-found]  # noqa: E402

GTD_DIR = tier("gtd")
DAILY_NOTES_DIR = tier("daily_notes")

CHECKBOX_RE = re.compile(r"^\s*[+\-*]\s*\[([ xX~/])\]\s+(.*)$")
LIST_ITEM_RE = re.compile(r"^(\s*)(?:[-*+]\s+|\d+\.\s+)(.*)$")
SUBSECTION_RE = re.compile(r"^\s*\*\*([^*]+?)\*\*\s*[:：]?\s*$")
# Daily notes are `YYYY-MM-DD.md`; reflections are `YYYY-MM-DD-<slug>.md`.
# Accept either: stem ends with the date, or date is followed by `-` then slug.
FILENAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-|\.)")

INLINE_META = {
    "due": re.compile(r"\bdue:(\d{4}-\d{2}-\d{2})\b"),
    "priority": re.compile(r"\bpriority:(P[0-3])\b"),
    "area": re.compile(r"\barea:(#[\w\-]+)\b"),
}

# Reflect reads a `+ [ ]` task's first [[YYYY-MM-DD]] (aliased or not) as its due date; `due:` still wins.
REFLECT_DUE_RE = re.compile(r"\[\[(\d{4}-\d{2}-\d{2})(?:\|[^\]\n]*)?\]\]")

STATE_MAP = {" ": "open", "x": "done", "X": "done", "~": "killed", "/": "wip"}
# Reflect knows only open and done, so a struck-through done task is cancelled; `[~]` and `[/]` are legacy.
STRUCK_RE = re.compile(r"^~~(.*)~~$")

NEXT_ACTION_HEADERS = ("## Next Action", "## Next Actions")
SKIP_SUBSECTIONS = ("不做", "不要做", "Parked", "Skip", "Don't")

# Closure-language patterns. Group 1 captures the phrase mentioning the target.
# Chinese-only by design: English `done`/`finished` patterns produced too many
# false positives against quoted English text in reading reflections. The
# primary closure mechanism is the user editing [ ] -> [x] in source files;
# this scan is a best-effort secondary signal for daily-note mentions.
# `了?` after each verb consumes the perfective particle so it does not bleed
# into the captured noun (e.g. `已完成了申请` -> capture `申请`, not `了申请`).
CLOSURE_PATTERNS = [
    re.compile(r"已完成了?\s*([^\s。，,；;、/]+)"),
    re.compile(r"完成了\s*([^\s。，,；;、/]+)"),
    re.compile(r"搞定了?\s*([^\s。，,；;、/]+)"),
    re.compile(r"做完了?\s*([^\s。，,；;、/]+)"),
    re.compile(r"已经?做了?\s*([^\s。，,；;、/]+)"),
]

CLOSURE_STOP_WORDS = {
    "了", "的", "和", "也", "都", "就", "会", "也是", "还", "可", "可以",
    "一", "二", "三", "几", "些", "些", "这", "那", "其",
}


@dataclass
class Todo:
    text: str
    source: str
    line: int
    state: str
    section: str | None = None
    due: str | None = None
    priority: str | None = None
    area: str | None = None
    age_days: int = -1  # -1 = not loaded
    sot: str | None = None  # owner link `path#anchor`, removed from text
    sot_error: str | None = None

    def computed_priority(self) -> str:
        if self.priority:
            return self.priority
        if self.due:
            try:
                d = date.fromisoformat(self.due)
                days = (d - date.today()).days
                if days < 0:
                    return "P0"
                if days <= 7:
                    return "P1"
            except ValueError:
                pass
        if self.age_days >= 30:
            return "P3"
        return "P2"

    def short_source(self) -> str:
        return Path(self.source).name


def extract_metadata(todo: Todo, text: str, reflect_task: bool = False) -> None:
    for key, regex in INLINE_META.items():
        m = regex.search(text)
        if m:
            setattr(todo, key, m.group(1))
    if reflect_task and todo.due is None and (m := REFLECT_DUE_RE.search(text)):
        todo.due = m.group(1)


def filename_date(path: Path) -> date | None:
    m = FILENAME_DATE_RE.match(path.name)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def line_age_days(path: Path, line_no: int) -> int:
    """Days since the item was created.

    Reflections are write-once; we derive age from the YYYY-MM-DD filename prefix,
    which is the canonical creation date and beats file mtime (Google Drive sync
    invalidates mtime). Git blame is also unreliable here: zk/ is a Drive symlink
    not tracked by the atelier repo. GTD files are continuously edited, so we
    approximate their line-age with file mtime.
    """
    if "reflections" in path.parts:
        d = filename_date(path)
        if d is not None:
            return max(0, (date.today() - d).days)
    try:
        out = subprocess.run(
            [
                "git",
                "blame",
                "--line-porcelain",
                "-L",
                f"{line_no},{line_no}",
                "--",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        timeout=10,
    ).stdout
        for ln in out.splitlines():
            if ln.startswith("author-time "):
                ts = int(ln.split()[1])
                return max(0, (datetime.now() - datetime.fromtimestamp(ts)).days)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        pass
    try:
        mtime = path.stat().st_mtime
        return max(0, (datetime.now() - datetime.fromtimestamp(mtime)).days)
    except OSError:
        return 0


def scan_gtd_file(path: Path) -> list[Todo]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[Todo] = []
    for i, line in enumerate(lines, start=1):
        m = CHECKBOX_RE.match(line)
        if not m:
            continue
        state_char, content = m.groups()
        state, text = checkbox_state(state_char, content)
        todo = Todo(
            text=text,
            source=str(path),
            line=i,
            state=state,
        )
        extract_metadata(todo, content, reflect_task=line.lstrip().startswith("+"))
        out.append(todo)
    return out


def scan_reflection_next_actions(path: Path) -> list[Todo]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    in_section = False
    in_skip_sub = False
    current_sub: str | None = None
    out: list[Todo] = []

    for i, line in enumerate(lines, start=1):
        if line.startswith("## "):
            if any(line.startswith(h) for h in NEXT_ACTION_HEADERS):
                in_section = True
                in_skip_sub = False
                current_sub = None
                continue
            if in_section:
                break
            continue

        if not in_section:
            continue

        sub_m = SUBSECTION_RE.match(line)
        if sub_m:
            sub_label = sub_m.group(1).strip()
            current_sub = sub_label
            in_skip_sub = any(skip in sub_label for skip in SKIP_SUBSECTIONS)
            continue

        if in_skip_sub:
            continue

        list_m = LIST_ITEM_RE.match(line)
        if not list_m:
            continue
        indent, content = list_m.groups()
        if indent:
            continue  # indented sub-bullets are detail under a parent item
        text_part = content.strip()
        if not text_part:
            continue
        # Reflection bullets have no checkbox; DONE/KILLED are their closure markers.
        if (
            text_part.startswith("DONE ")
            or text_part.startswith("DONE:")
            or text_part.startswith("KILLED ")
            or text_part.startswith("KILLED:")
        ):
            continue
        todo = Todo(
            text=text_part,
            source=str(path),
            line=i,
            state="open",
            section=current_sub,
        )
        extract_metadata(todo, text_part)
        out.append(todo)

    return out


SOT_REF_RE = re.compile(r'\[\[([^\[\]|#\n]+)#([^\[\]|\n]+)\|sot\]\]')
SOT_STATES = {"☐": "open", "📅": "open", "✅": "done", "🚫": "killed"}
SOT_MARKERS = {"open": " ", "done": "x", "killed": "x"}


def checkbox_state(marker: str, content: str) -> tuple[str, str]:
    """(state, text) of a checkbox; a done task struck through is cancelled."""
    state, text = STATE_MAP.get(marker, "open"), content.strip()
    if state == "done" and (struck := STRUCK_RE.match(text)):
        return "killed", struck[1].strip()
    return state, text


def render_checkbox(line: str, match: re.Match, state: str, text: str) -> str:
    """`line` rewritten to `state` the way Reflect shows it, keeping the rest of the line."""
    body = f"~~{text}~~" if state == "killed" else text
    tail = match[2][len(match[2].rstrip()):]
    return (line[:match.start(1)] + SOT_MARKERS[state] + line[match.end(1):match.start(2)]
            + body + tail + line[match.end(2):])


@lru_cache(maxsize=4)
def _titles(root: Path) -> TitleIndex:
    return TitleIndex(root)


def resolve_sot(todo: Todo, snapshots: dict[Path, str]) -> str:
    """Resolve an opted-in ledger row, never infer completion from prose."""
    if '[sot](' in todo.text or any(row.startswith('^') for _, row in SOT_REF_RE.findall(todo.text)):
        raise ValueError(f'{todo.source}:{todo.line}: legacy SoT link; rewrite as '
                         '[[<owner note>#<row>|sot]], <row> being the owner row\'s first cell')
    if '|sot]]' not in todo.text:
        return todo.state
    refs = SOT_REF_RE.findall(todo.text)
    if len(refs) != 1 or todo.text.count('|sot]]') != 1:
        raise ValueError(f'{todo.source}:{todo.line}: expected one [[<owner note>#<row>|sot]]')
    title, anchor = refs[0]
    rel = _titles(vault_root()).resolve(title)
    if rel is None:
        raise ValueError(f'{todo.source}:{todo.line}: missing or ambiguous SoT owner: {title}')
    path = (vault_root() / rel).resolve()
    if (path.is_relative_to(GTD_DIR.resolve())
            or path.is_relative_to(DAILY_NOTES_DIR.resolve()) or path.suffix != '.md'):
        raise ValueError(f'{todo.source}:{todo.line}: invalid SoT owner: {title}')
    if path not in snapshots:
        snapshots[path] = path.read_bytes().decode('utf-8')
    fenced = None
    rows = []
    for line in snapshots[path].splitlines():
        fence = re.match(r'^\s*(`{3,}|~{3,})', line)
        if fence:
            if fenced is None:
                fenced = fence[1]
            elif (fence[1][0] == fenced[0] and len(fence[1]) >= len(fenced)
                  and not line[fence.end():].strip()):
                fenced = None
        elif fenced is None and line.startswith('|'):
            cells = [cell.strip() for cell in re.split(r'(?<!\\)\|', line)]
            if len(cells) > 1 and cells[1] == anchor.strip():
                rows.append(cells)
    if len(rows) != 1:
        raise ValueError(f'{path}#{anchor}: expected exactly one owner row, outside code fences, '
                         'whose first cell is the link fragment')
    cells = rows[0]
    if len(cells) < 5 or cells[3] not in SOT_STATES:
        raise ValueError(f'{path}#{anchor}: expected ledger Status in third column')
    return SOT_STATES[cells[3]]


def linked_state(saved: str, owner: str) -> str:
    """The owner decides, except that an open owner keeps an in-progress marker."""
    return saved if (saved, owner) == ("wip", "open") else owner


def link_todos(todos: list[Todo]) -> None:
    """Derive linked status; a bad link keeps its saved marker and is reported."""
    snapshots: dict[Path, str] = {}
    for todo in todos:
        refs = SOT_REF_RE.findall(todo.text)
        todo.sot = "#".join(refs[0]) if refs else None
        try:
            todo.state = linked_state(todo.state, resolve_sot(todo, snapshots))
        except (OSError, ValueError) as exc:
            todo.sot_error = str(exc)
            print(f"WARN: {todo.short_source()}:{todo.line}: {exc}", file=sys.stderr)
        todo.text = SOT_REF_RE.sub("", todo.text).strip()


def sot_changes(paths: list[Path]) -> tuple[dict[Path, str], dict[Path, str], list[str]]:
    snapshots: dict[Path, str] = {}
    updates: dict[Path, str] = {}
    findings: list[str] = []
    for path in paths:
        snapshots[path] = path.read_bytes().decode('utf-8')
        lines = snapshots[path].splitlines(keepends=True)
        for index, line in enumerate(lines):
            match = CHECKBOX_RE.match(line)
            if not match:
                continue
            state, text = checkbox_state(match[1], match[2])
            todo = Todo(text, str(path), index + 1, state)
            expected = linked_state(state, resolve_sot(todo, snapshots))
            if state != expected:
                findings.append(f'{path}:{index + 1}: {state} -> {expected}')
                lines[index] = render_checkbox(line, match, expected, text)
        updated = ''.join(lines)
        if updated != snapshots[path]:
            updates[path] = updated
    return snapshots, updates, findings


def cmd_sot(args: argparse.Namespace) -> int:
    paths = sorted(GTD_DIR.glob('*.md'))
    if args.cmd == 'sync':
        path = (GTD_DIR / args.file).resolve()
        if path.parent != GTD_DIR.resolve() or path.suffix != '.md':
            raise ValueError('--file must name a Markdown file directly under GTD')
        paths = [path]
    snapshots, updates, findings = sot_changes(paths)
    for finding in findings:
        print(finding)
    if args.cmd == 'check':
        print(f'{len(findings)} SoT status mismatch(es); only explicit links checked.')
        return int(bool(findings))
    if not args.apply:
        print(f'Preview: {len(findings)} marker change(s); no files written.')
        return 0
    # One target file per apply: atomic replacement, with source drift checks.
    for path, before in snapshots.items():
        if path.read_bytes().decode('utf-8') != before:
            raise ValueError(f'{path}: changed since planning; rerun sync')
    for path, updated in updates.items():
        atomic_write(path, updated, expected_text=snapshots[path])
        if path.read_bytes().decode('utf-8') != updated:
            raise ValueError(f'{path}: verification failed; inspect before retrying')
    print(f'Applied and verified {len(findings)} marker change(s).')
    return 0


def collect_all_todos(load_age: bool = True) -> list[Todo]:
    todos: list[Todo] = []
    if GTD_DIR.exists():
        for f in sorted(GTD_DIR.glob("*.md")):
            todos.extend(scan_gtd_file(f))
    for f in tier_files("reflections", "*.md"):
        todos.extend(scan_reflection_next_actions(f))
    link_todos(todos)
    if load_age:
        for t in todos:
            t.age_days = line_age_days(Path(t.source), t.line)
    return todos


def collect_open_todos(load_age: bool = True) -> list[Todo]:
    return [t for t in collect_all_todos(load_age) if t.state == "open"]


def detect_closure_candidates(
    open_todos: list[Todo], since_days: int = 14
) -> list[tuple[Todo, str, str]]:
    """Return (todo, closure_phrase, source_path). De-duplicates on (todo, phrase).

    Scans daily notes only. Reflection bodies are too narrative-heavy and produce
    excessive false positives.
    """
    cutoff = date.today() - timedelta(days=since_days)
    sources: list[Path] = []
    if DAILY_NOTES_DIR.exists():
        for f in DAILY_NOTES_DIR.rglob("*.md"):
            d = filename_date(f)
            if d is not None and d >= cutoff:
                sources.append(f)

    seen: set[tuple[str, int, str]] = set()
    out: list[tuple[Todo, str, str]] = []
    for src_path in sources:
        try:
            text = src_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for pattern in CLOSURE_PATTERNS:
            for m in pattern.finditer(text):
                phrase = m.group(1).strip()
                if len(phrase) < 2:
                    continue
                for todo in open_todos:
                    if _phrase_matches_todo(phrase, todo.text):
                        key = (todo.source, todo.line, phrase)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append((todo, phrase, str(src_path)))
    return out


def _is_substantive(s: str) -> bool:
    """A phrase or token is substantive enough to base a closure match on."""
    if s in CLOSURE_STOP_WORDS:
        return False
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    if cjk >= 2:
        return True
    if len(s) >= 4 and all(c.isalnum() or c in "-_" for c in s):
        return True
    return False


def _phrase_matches_todo(phrase: str, todo_text: str) -> bool:
    if not _is_substantive(phrase):
        return False
    if phrase in todo_text:
        return True
    tokens = [t for t in re.split(r"[\s/、,，·]+", phrase) if _is_substantive(t)]
    return any(t in todo_text for t in tokens)


def find_last_reflection() -> Path | None:
    files = tier_files("reflections", "*-reflection*.md")
    return files[-1] if files else None


def cmd_list(args: argparse.Namespace) -> int:
    todos = collect_open_todos(load_age=True)
    if args.area:
        todos = [t for t in todos if t.area == args.area]

    if args.json:
        payload = []
        for t in todos:
            d = asdict(t)
            d["computed_priority"] = t.computed_priority()
            payload.append(d)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    groups: dict[str, list[Todo]] = {"P0": [], "P1": [], "P2": [], "P3": []}
    for t in todos:
        groups[t.computed_priority()].append(t)

    if not todos:
        print("No open TODOs.")
        return 0

    visible = ("P0", "P1", "P2") if not args.include_stale else ("P0", "P1", "P2", "P3")
    skipped_stale = len(groups["P3"]) if not args.include_stale else 0

    for prio in visible:
        items = groups[prio]
        if not items:
            continue
        label = {
            "P0": "P0  OVERDUE / PINNED",
            "P1": "P1  DUE SOON / HIGH",
            "P2": "P2  NORMAL",
            "P3": "P3  STALE (kill candidate)",
        }[prio]
        print(f"\n{label}")
        print("─" * 56)
        for t in sorted(items, key=lambda x: (x.due or "9999", -x.age_days)):
            tags = []
            if t.due:
                tags.append(f"due:{t.due}")
            if t.area:
                tags.append(t.area)
            if t.section:
                tags.append(f"§{t.section}")
            if t.age_days >= 30 and prio != "P3":
                tags.append(f"{t.age_days}d")
            tag_str = "  " + " ".join(tags) if tags else ""
            print(f"  {t.short_source()}:{t.line}{tag_str}")
            text = t.text if len(t.text) <= 110 else t.text[:107] + "..."
            print(f"    {text}")
    if skipped_stale:
        print(
            f"\n({skipped_stale} stale items hidden; run with --include-stale "
            f"or `scripts/todos.py stale`)"
        )
    print()
    return 0


def cmd_stale(args: argparse.Namespace) -> int:
    todos = collect_open_todos(load_age=True)
    stale = [t for t in todos if t.age_days >= args.days]
    stale.sort(key=lambda t: -t.age_days)
    if not stale:
        print(f"No open TODOs older than {args.days} days.")
        return 0
    print(f"Stale TODOs (>= {args.days} days, oldest first)")
    print("─" * 56)
    for t in stale:
        print(f"  {t.age_days:4d}d  {t.short_source()}:{t.line}")
        text = t.text if len(t.text) <= 100 else t.text[:97] + "..."
        print(f"         {text}")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    """Concise output for /hi Step 0."""
    last_ref = find_last_reflection()
    todos = collect_open_todos(load_age=True)
    cands = detect_closure_candidates(todos, since_days=args.days)

    print("## Digest")
    if last_ref:
        last_actions = scan_reflection_next_actions(last_ref)
        link_todos(last_actions)
        last_actions = [t for t in last_actions if t.state == "open"]
        print(f"\nLast reflection: {last_ref.name}")
        if last_actions:
            print(f"Next Actions ({len(last_actions)}):")
            for t in last_actions[:5]:
                section = f" [§{t.section}]" if t.section else ""
                text = t.text if len(t.text) <= 100 else t.text[:97] + "..."
                print(f"  - {text}{section}")
            if len(last_actions) > 5:
                print(f"  ... ({len(last_actions) - 5} more)")
    else:
        print("\nNo prior reflection found.")

    if cands:
        print(f"\nClosure candidates (mentions in last {args.days} days):")
        seen: set[tuple[str, int]] = set()
        for todo, phrase, _ in cands:
            key = (todo.source, todo.line)
            if key in seen:
                continue
            seen.add(key)
            text = todo.text if len(todo.text) <= 80 else todo.text[:77] + "..."
            print(f"  - {text}")
            print(f"    ({todo.short_source()}:{todo.line}; phrase \"{phrase}\")")

    stale = sorted(
        [t for t in todos if t.age_days >= 30], key=lambda t: -t.age_days
    )[:3]
    if stale:
        print("\nStale (oldest 3, kill or promote?):")
        for t in stale:
            text = t.text if len(t.text) <= 80 else t.text[:77] + "..."
            print(f"  - {t.age_days}d  {text}  ({t.short_source()}:{t.line})")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/todos.py",
        description="Aggregate and surface open TODOs from gtd/ and reflections/.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List open TODOs by computed priority.")
    p_list.add_argument("--area", help="Filter by area tag, e.g. #capacity")
    p_list.add_argument(
        "--include-stale",
        action="store_true",
        help="Include P3 stale items (>=30d). Default hides them.",
    )
    p_list.add_argument("--json", action="store_true", help="JSON output.")
    p_list.set_defaults(func=cmd_list)

    p_stale = sub.add_parser("stale", help="List TODOs older than N days.")
    p_stale.add_argument("--days", type=int, default=30)
    p_stale.set_defaults(func=cmd_stale)

    p_digest = sub.add_parser(
        "digest", help="Concise digest for /hi Step 0."
    )
    p_digest.add_argument("--days", type=int, default=7)
    p_digest.set_defaults(func=cmd_digest)

    p_check = sub.add_parser("check", help="Check explicit SoT links and marker drift.")
    p_check.set_defaults(func=cmd_sot)
    p_sync = sub.add_parser("sync", help="Preview derived GTD markers; apply one file.")
    p_sync.add_argument("--file", required=True, help="Filename under GTD.")
    p_sync.add_argument("--apply", action="store_true", help="Write verified marker changes.")
    p_sync.set_defaults(func=cmd_sot)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
