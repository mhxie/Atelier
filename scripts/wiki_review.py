"""Edit-fidelity and source-support review of wiki claims for nightly Autoevo.

The parent stages each pending claim's current text beside its newest
committed non-pending version, and each claim due a source check beside the
cached evidence passages that best match it; the model only returns verdicts.
Writing appends one reviewer or scout record inside the claim's evidence fence.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import itertools
import json
import re
import tempfile

from _git import run_git
import trust
import wiki_evidence
import wiki_trust

CAP = 40
SUPPORT_CAP, STAGE_CHARS = 10, 300_000  # checks per night, and the evidence they may stage in total
QUOTE_RE = re.compile(r'"([^"]{12,})"|\u201c([^\u201d]{12,})\u201d')
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


notes = trust.wiki_paths  # cues.py walks the wiki through this name


def attention(note: trust.WikiNote, as_of: date) -> list[tuple[int, str]]:
    """Claims only a human can settle: a disputing verdict, a doubt only an edit review settled, or evidence that has all expired."""
    rows = []
    for c in note.claims:
        if dispute := c.dispute(as_of):
            rows.append((c.number, f"{dispute.fields['status']} by {dispute.fields['_agent']}"))
        elif doubted(c, as_of):
            rows.append((c.number, "doubt settled only by an edit review"))
        elif c.anchors and not any(a.active_on(as_of) for a in c.anchors):
            rows.append((c.number, "evidence expired"))
    return rows


def doubted(claim: trust.Claim, as_of: date) -> bool:
    """A reader, scout or challenger doubt stands. An edit review judges fidelity, not truth, so it
    must not settle that doubt through Reflect's latest-record rule; only the user does."""
    last = claim.latest(as_of, trust.PASS_AGENTS - {"editor", "reviewer"})
    return last is not None and last.fields["status"] in {"flagged", "inconclusive"}


def edit_due(claim: trust.Claim, as_of: date) -> bool:
    return claim.review(as_of) == "pending" and not doubted(claim, as_of)


def trusted(root: Path, as_of: date) -> set[str]:
    """Origins at or above the trust threshold, so a source check never rests on a source tiers ignore."""
    try:  # weigh the notes the published report weighs: Reflect's local-only folders stay out
        folders = wiki_trust.reflect_target()[1] if root.resolve().is_relative_to(wiki_trust.vault_root().resolve()) else frozenset()
    except SystemExit:
        folders = frozenset()
    notes = [trust.parse_wiki_note(p, as_of) for p in trust.wiki_paths(root) if not (folders and wiki_trust.local_only(p, folders))]
    return {key for key, row in wiki_trust.tiers(notes, as_of)[1].items() if row["trusted"]}


def support_due(note: trust.WikiNote, text: str, claim: trust.Claim, as_of: date, origins: set[str]) -> list[dict]:
    """The evidence to stage for a reviewed, undoubted article claim with no source check since its last
    edit: matched passages first, then, once those left it undecided, each source's full text."""
    last = claim.latest(as_of, {"editor", *wiki_trust.ADVERSARIAL})
    if (claim.range_utf8 is None or claim.review(as_of) == "pending" or doubted(claim, as_of) or claim.dispute(as_of)
            or (last is not None and last.fields["_agent"] != "editor")):
        return []
    current = claim_text(note, text, claim.number) or ""
    found = evidence(claim, current, as_of, origins)[:4]  # four sources keep any one check well inside STAGE_CHARS
    if found and wiki_evidence.undecided(rest_key(current, found), as_of):  # escalate, as checkers do for low confidence
        found = [{**e, "passages": [wiki_evidence.text(e["source"])[:wiki_evidence.FULL_CHARS // len(found)]]} for e in found]
    return [] if not found or wiki_evidence.undecided(rest_key(current, found), as_of) else found


def rest_key(current: str, found: list[dict]) -> str:
    """An undecided check rests until the claim text or its staged evidence changes."""
    return current + json.dumps(found, sort_keys=True, ensure_ascii=False)


def evidence(claim: trust.Claim, text: str, as_of: date, origins: set[str]) -> list[dict]:
    """Per cached trusted source, the passages that best match the claim's text."""
    found = {}
    for anchor in (a for a in claim.anchors if a.active_on(as_of) and wiki_trust.origin(a.fields["_anchor_type"], a.fields["_anchor_id"]) in origins):
        key = wiki_trust.evidence_key(anchor)
        if key not in found and (passages := wiki_evidence.passages(wiki_evidence.text(key), text)):
            found[key] = {"source": key, "title": (wiki_evidence.load(key) or {}).get("title", ""), "passages": passages}
    return list(found.values())


def candidates(vault: Path, root: Path, as_of: date, cap: int = CAP, eligible=lambda rel: True) -> list[str]:
    """Whole wiki notes in path order: with pending claims up to `cap`, then with claims due a source check up to SUPPORT_CAP."""
    chosen, total, checks, origins = [], 0, 0, trusted(root, as_of)
    for path in trust.wiki_paths(root):
        note = trust.parse_wiki_note(path, as_of)
        count = sum(edit_due(c, as_of) for c in note.claims)
        if note.parse_errors or not eligible(path.relative_to(vault).as_posix()):  # unstageable notes would waste the caps
            continue
        if count and (not total or total + count <= cap):
            total += count
        elif not count and checks < SUPPORT_CAP and (due := sum(bool(support_due(note, text, c, as_of, origins))
                                                                for text in [path.read_text(encoding="utf-8")] for c in note.claims)):
            checks += due
        else:
            continue
        chosen.append(path.relative_to(vault).as_posix())
    return chosen


def stage(vault: Path, root: Path, snapshots: dict[str, Path], as_of: date) -> list[dict]:
    """Current and previous text for every pending, undoubted claim of each snapshotted note, then
    current text and evidence passages for claims due a source check, up to SUPPORT_CAP."""
    claims, checks, origins = [], [], trusted(root, as_of)
    for rel, snapshot in snapshots.items():
        text = snapshot.read_text(encoding="utf-8")
        note = parse(text)
        before = dict.fromkeys(c.number for c in note.claims if edit_due(c, as_of))
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
        claims += [{"path": rel, "title": note.title, "claim": number, "mode": "edit", "previous": previous,
                    "current": claim_text(note, text, number)} for number, previous in before.items()]
        checks += [{"path": rel, "title": note.title, "claim": c.number, "mode": "support",
                    "current": claim_text(note, text, c.number), "evidence": found}
                   for c in note.claims if (found := support_due(note, text, c, as_of, origins))]
    sizes = itertools.accumulate(sum(len(p) for e in row["evidence"] for p in e["passages"]) for row in checks)
    return claims + [row for row, size in zip(checks[:SUPPORT_CAP], sizes) if size <= STAGE_CHARS]


def decided(row: dict, claim: dict) -> bool:
    """A source verdict counts only when its reason quotes one staged passage verbatim up to whitespace
    (a `flagged` may quote an arXiv or DOI title instead). This stops paraphrase and invented quotes;
    it shows the words exist, not that they support the claim. The offline model is the injection guard."""
    texts = [p for e in claim["evidence"] for p in e["passages"]] + [  # a page's title can drift; a paper's names the work
        e["title"] for e in claim["evidence"] if row["verdict"] == "flagged" and e["source"].split(":")[0] in {"arxiv", "doi"}]
    staged = "\n".join(" ".join(t.split()) for t in texts)  # a quote cannot straddle two passages
    quotes = [" ".join((a or b).split()) for a, b in QUOTE_RE.findall(row["reason"])]
    return row["verdict"] != "inconclusive" and any(len(q) >= 12 and q in staged for q in quotes)


def support_status(row: dict, claim: dict) -> str | None:
    """Scout's quoted verdict, else verified when MiniCheck (asked after Scout, so it cannot anchor it) finds unflagged support."""
    if decided(row, claim) or row["verdict"] == "flagged":
        return row["verdict"] if decided(row, claim) else None
    if "minicheck" not in claim:
        claim["minicheck"] = wiki_evidence.minicheck(claim["current"], [p for e in claim["evidence"] for p in e["passages"]])
    return "verified" if claim["minicheck"] else None


def verdicts(proposal: dict, plan: dict) -> dict[str, dict[int, tuple[str, str, str]]]:
    """Group (agent, status) by note. An edit without previous text cannot be verified;
    an undecided source check records nothing, so missing evidence never disputes a claim."""
    staged = {(row["path"], row["claim"]): row for row in plan.get("wiki_review", {}).get("claims", [])}
    grouped: dict[str, dict[int, tuple[str, str]]] = {}
    for row in proposal.get("wiki_reviews", []):
        claim, verdict = staged[(row["path"], row["claim"])], row["verdict"]
        if claim.get("mode") == "support":
            if status := support_status(row, claim):
                grouped.setdefault(row["path"], {})[row["claim"]] = ("scout", status, "" if decided(row, claim) else "#minicheck")
        else:
            unverifiable = verdict == "verified" and claim["previous"] is None
            grouped.setdefault(row["path"], {})[row["claim"]] = ("reviewer", "inconclusive" if unverifiable else verdict, "")
    return grouped


def undecided(proposal: dict, plan: dict) -> list[dict]:
    """Staged source checks the model left undecided, did not back with a quote, or skipped while returning
    others, with its reason; a dispatch that returned none rests nothing (the queue moves even without MiniCheck)."""
    returned = {(row["path"], row["claim"]): row for row in proposal.get("wiki_reviews", [])}
    if not any(row.get("mode") == "support" and (row["path"], row["claim"]) in returned for row in plan.get("wiki_review", {}).get("claims", [])):
        return []
    return [{**claim, "reason": returned.get(key, {}).get("reason", "not returned")}
            for claim in plan.get("wiki_review", {}).get("claims", []) if claim.get("mode") == "support"
            for key in [(claim["path"], claim["claim"])] if key not in returned or not support_status(returned[key], claim)]


def append(text: str, results: dict[int, tuple[str, str]], cycle: str, ref: str) -> str:
    """Insert one record after each claim's last pass (or anchor) line; nothing else changes."""
    lines = text.splitlines(keepends=True)
    claims = [c for c in parse(text).claims if c.number in results and (c.passes or c.anchors)]
    for claim in sorted(claims, key=lambda c: -max(m.line_no for m in [*c.passes, *c.anchors])):
        at = max(m.line_no for m in [*claim.passes, *claim.anchors])
        ending = lines[at - 1][len(lines[at - 1].rstrip("\r\n")):] or "\n"
        agent, status, note = results[claim.number]  # note "#minicheck" marks a verdict MiniCheck decided
        lines.insert(at, f"@pass: {agent} | status: {status} | at: {cycle} | ref: {ref}{note}{ending}")
    return "".join(lines)


def safe(before: str, after: str, results: dict[int, tuple[str, str]], cycle: str) -> bool:
    """The edit keeps the parse clean and leaves each returned record as its claim's latest of its kind."""
    old, new, day = parse(before), parse(after), date.fromisoformat(cycle)
    claims = {c.number: c for c in new.claims}
    latest = {n: claims[n].latest(day, {"editor", "reviewer"} if agent == "reviewer" else {"editor", *wiki_trust.ADVERSARIAL})
              for n, (agent, *_rest) in results.items() if n in claims}
    return len(new.parse_errors) == len(old.parse_errors) and all(
        (m := latest.get(n)) is not None and (m.fields["_agent"], m.fields["status"]) == result[:2] for n, result in results.items())
