"""Edit-fidelity review of pending wiki claims for nightly Autoevo.

The parent stages each pending claim's current text beside its newest
committed non-pending version; the model only returns verdicts. Writing
appends one reviewer record inside the claim's own evidence fence.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import re
import tempfile

from _git import run_git
import trust

VERDICTS = ("verified", "flagged", "inconclusive")
CAP = 40
FENCE_RE = re.compile(r"^```anchors[^\n]*\n.*?^```[ \t]*$", re.M | re.S)


def parse(text: str) -> trust.WikiNote:
    with tempfile.TemporaryDirectory(prefix="atelier-wiki-review-") as directory:
        path = Path(directory) / "note.md"
        path.write_text(text, encoding="utf-8")
        return trust.parse_wiki_note(path, date.today())


def claim_text(note: trust.WikiNote, text: str, number: int) -> str | None:
    """Claim prose without evidence fences or legacy citation lines."""
    claim = next((c for c in note.claims if c.number == number), None)
    if claim is None:
        return None
    body = FENCE_RE.sub("", text[slice(*claim.source_range)])
    body = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("@cite:")).strip()
    return body if claim.range_utf8 is not None else f"{claim.title}\n{body}".strip()


def notes(root: Path):
    """Primary wiki entries in path order, without folder indexes, secure notes or symlinks."""
    for path in sorted(root.rglob("*.md")):
        if path.name != "index.md" and "secure" not in path.relative_to(root).parts and not path.is_symlink():
            yield path


def attention(note: trust.WikiNote, as_of: date) -> list[tuple[int, str]]:
    """Claims only a human can settle: a disputing verdict, or evidence that has all expired."""
    rows = []
    for c in note.claims:
        if dispute := c.dispute(as_of):
            rows.append((c.number, f"{dispute.fields['status']} by {dispute.fields['_agent']}"))
        elif c.anchors and not any(a.active_on(as_of) for a in c.anchors):
            rows.append((c.number, "evidence expired"))
    return rows


def candidates(vault: Path, root: Path, as_of: date, cap: int = CAP) -> list[str]:
    """Whole wiki notes with pending claims, in path order, up to `cap` claims."""
    chosen, total = [], 0
    for path in notes(root):
        note = trust.parse_wiki_note(path, as_of)
        count = sum(c.review(as_of) == "pending" for c in note.claims)
        if count and not note.parse_errors and (not chosen or total + count <= cap):
            chosen.append(path.relative_to(vault).as_posix())
            total += count
    return chosen


def stage(vault: Path, snapshots: dict[str, Path], as_of: date) -> list[dict]:
    """Current and previous text for every pending claim of each snapshotted note."""
    claims = []
    for rel, snapshot in snapshots.items():
        text = snapshot.read_text(encoding="utf-8")
        note = parse(text)
        before = dict.fromkeys(c.number for c in note.claims if c.review(as_of) == "pending")
        commit = None
        for line in run_git(vault, "log", "--follow", "--format=@%H", "--name-only", "--", rel).stdout.splitlines():
            if line.startswith("@"):
                commit = line[1:]
            elif line.strip() and None in before.values():
                old = run_git(vault, "show", f"{commit}:{line}").stdout
                version = parse(old)
                for claim in version.claims:
                    if before.get(claim.number, "") is None and claim.review(as_of) != "pending":
                        before[claim.number] = claim_text(version, old, claim.number)
        claims += [{"path": rel, "title": note.title, "claim": number, "previous": previous,
                    "current": claim_text(note, text, number)} for number, previous in before.items()]
    return claims


def verdicts(proposal: dict, plan: dict) -> dict[str, dict[int, str]]:
    """Group returned verdicts by note; a claim without previous text cannot be verified."""
    staged = {(row["path"], row["claim"]): row for row in plan.get("wiki_review", {}).get("claims", [])}
    grouped: dict[str, dict[int, str]] = {}
    for row in proposal.get("wiki_reviews", []):
        unverifiable = row["verdict"] == "verified" and staged[(row["path"], row["claim"])]["previous"] is None
        grouped.setdefault(row["path"], {})[row["claim"]] = "inconclusive" if unverifiable else row["verdict"]
    return grouped


def append(text: str, results: dict[int, str], cycle: str, ref: str) -> str:
    """Insert one reviewer record after each claim's last pass line; nothing else changes."""
    lines = text.splitlines(keepends=True)
    claims = [c for c in parse(text).claims if c.number in results and c.passes]
    for claim in sorted(claims, key=lambda c: -max(p.line_no for p in c.passes)):
        at = max(p.line_no for p in claim.passes)
        ending = lines[at - 1][len(lines[at - 1].rstrip("\r\n")):] or "\n"
        lines.insert(at, f"@pass: reviewer | status: {results[claim.number]} | at: {cycle} | ref: {ref}{ending}")
    return "".join(lines)


def safe(before: str, after: str, results: dict[int, str], cycle: str) -> bool:
    """The edit keeps the parse clean and resolves each claim exactly as returned."""
    old, new = parse(before), parse(after)
    states = {c.number: c.review(date.fromisoformat(cycle)) for c in new.claims}
    return len(new.parse_errors) == len(old.parse_errors) and all(states.get(n) == v for n, v in results.items())
