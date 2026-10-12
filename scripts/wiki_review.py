"""Edit-fidelity and source-support review of wiki claims, and wiki growth, for nightly Autoevo.

The parent stages each pending claim's current text beside its newest
committed non-pending version, and each claim due a source check beside the
cached evidence passages that best match it; the model only returns verdicts.
Writing appends one reviewer or scout record inside the claim's evidence fence.
Growth stages working notes your thinking keeps linking beside their cached
sources; the parent writes a drafted entry only when every claim passes `grown`.
"""

from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path
import itertools
import json
import re
import tempfile

from _git import run_git
import _paths
from _paths import tier_segments, vault_root
from _reflect import TitleIndex, read_head
import trust
import wiki_evidence
import wiki_trust

CAP = 40
SUPPORT_CAP, STAGE_CHARS = 10, 300_000  # checks per night, and the evidence they may stage in total
QUOTE_RE = re.compile(r'"([^"]{12,})"|\u201c([^\u201d]{12,})\u201d')
FENCE_RE = re.compile(r"^```anchors[^\n]*\n.*?^```[ \t]*$", re.M | re.S)
GROW_MODE = "dry-run"  # off; dry-run, where drafts and their checks land only in the receipt; or write
GROW_TIERS, GROW_CAP, GROW_SOURCES, GROW_CHARS = ("wip", "research", "preprints"), 2, 4, 20_000  # per night, per entry, per source
GROW_CLAIMS, GROW_COVERAGE = range(3, 13), 0.6  # claims an entry carries, and the share of its prose they cover
LINK_RE = re.compile(r"https?://[^\s<>()\[\]\"'`]+|\barXiv:\s?\d{4}\.\d{4,5}(?:v\d+)?", re.I)


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


def trusted(root: Path, as_of: date, extra: tuple[trust.WikiNote, ...] = ()) -> set[str]:
    """Origins at or above the trust threshold, so a source check never rests on a source tiers ignore."""
    try:  # weigh the notes the published report weighs: Reflect's local-only folders stay out
        folders = wiki_trust.reflect_target()[1] if root.resolve().is_relative_to(wiki_trust.vault_root().resolve()) else frozenset()
    except SystemExit:
        folders = frozenset()
    notes = [trust.parse_wiki_note(p, as_of) for p in trust.wiki_paths(root) if not (folders and wiki_trust.local_only(p, folders))]
    return {key for key, row in wiki_trust.tiers([*notes, *extra], as_of)[1].items() if row["trusted"]}


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


def grow_key(rel: str, text: str) -> str:
    """A drafted note rests, like an undecided check, until its text changes; a dry run rests apart from writing."""
    return f"grow-{GROW_MODE}:{rel}:{hashlib.sha256(text.encode()).hexdigest()}"


def grow_candidates(vault: Path, as_of: date) -> list[str]:
    """Growth-tier working notes your thinking keeps linking that no wiki entry cites and that link a source, most
    linked first, leaving out private and local-only notes and drafts resting until their note changes."""
    from staleness import promotion_candidates

    tiers, found = tuple(tier_segments().get(name, name) + "/" for name in GROW_TIERS), []
    try:  # links are counted across the registry's vault only, and an unreadable note there must not stop the night
        ranked = promotion_candidates(as_of) if GROW_MODE != "off" and vault.resolve() == vault_root() else {}
        folders = wiki_trust.reflect_target()[1]  # a local-only note must not reach the synced wiki
    except (OSError, SystemExit):
        ranked = {}
    for path, _ in sorted(ranked.items(), key=lambda item: (-item[1], item[0].as_posix())):
        rel = path.relative_to(vault).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if (rel.startswith(tiers) and not wiki_trust.PRIVATE_RE.match(text) and not wiki_trust.local_only(path, folders)
                and grow_keys(text) and not wiki_evidence.undecided(grow_key(rel, text), as_of)):
            found.append(rel)
    return found


def grow_keys(text: str) -> list[str]:
    """Evidence keys of the external sources a note links, first mention first."""
    keys = []
    for link in (link for link in LINK_RE.findall(text) if "?" not in link):  # query strings carry tokens and tracking
        ident = "arxiv:" + link.split(":", 1)[1].strip() if link.lower().startswith("arxiv:") else "url:" + link.rstrip(".,;:!?")
        marker, _ = trust._parse_marker("@anchor", ident, [f"valid_at: {date.today()}"], 0, ident)
        if marker and (key := wiki_trust.evidence_key(marker)) not in keys:
            keys.append(key)
    return keys


def grow_sources(as_of: date) -> set[str]:
    """The sources the networked trust routine fetches, so a later growth run can stage them."""
    return {key for rel in grow_candidates(vault_root(), as_of)[:2 * GROW_CAP]
            for key in grow_keys((vault_root() / rel).read_text(encoding="utf-8", errors="replace"))[:2 * GROW_SOURCES]}


def stage_growth(vault: Path, snapshots: dict[str, Path]) -> dict:
    """The wiki folders an entry may join, the shadow copies it needs, and up to GROW_CAP candidates with cached sources."""
    root, rows = vault / tier_segments().get("wiki", "wiki"), []
    for rel, snapshot in snapshots.items():
        text = snapshot.read_text(encoding="utf-8", errors="replace")
        found = [{"source": key, "title": (wiki_evidence.load(key) or {}).get("title", ""), "text": body[:GROW_CHARS]}
                 for key in grow_keys(text) if len(body := wiki_evidence.text(key)) >= wiki_evidence.MIN_TEXT]
        if found:
            rows.append({"source": rel, "title": read_head(snapshot)[0] or Path(rel).stem, "snapshot": str(snapshot),
                         "rest": grow_key(rel, text), "evidence": found[:GROW_SOURCES]})
    return {"mode": GROW_MODE, "wiki": root.relative_to(vault).as_posix(), "candidates": rows[:GROW_CAP],
            "folders": sorted({p.parent.relative_to(vault).as_posix() for p in trust.wiki_paths(root) if p.parent != root}),
            "shadows": {code: segment for code, segment in _paths._registry().get("wiki_localized", {}).items() if isinstance(segment, str)}}


def _bare(text: str) -> int:
    """Characters of prose, without comments, headings, link definitions or whitespace."""
    for pattern in (r"(?s)<!--.*?-->", r"(?s)\A---\n.*?\n---\n", r"(?m)^#+ .*$", r"(?m)^\[[^\]\n]+\]:.*$"):
        text = re.sub(pattern, "", text)
    return len("".join(text.split()))


def grown(row: dict, plan: dict, vault: Path, as_of: date) -> tuple[str | None, dict[str, str]]:
    """Why a drafted entry may not be written, else its files: a new note in an existing wiki folder whose
    every claim anchors a staged source, quotes it verbatim, and has MiniCheck support from that source."""
    growth = plan["wiki_growth"]
    staged = next((c for c in growth["candidates"] if c["source"] == row["source"]), None)
    note, folder = parse(row["content"]), row["path"].rpartition("/")[0]
    if (staged is None or folder not in growth["folders"] or row["path"] != f"{folder}/{note.title}.md"
            or note.title.lower() == "index" or row["content"].startswith("---")):
        return "the path is not a new entry named for its H1, without frontmatter, in an existing wiki folder", {}
    titles = TitleIndex(vault, skip_unreadable=True)
    if (vault / row["path"]).exists() or titles.resolve(note.title) is not None:
        return "an entry or note already has this title", {}
    if note.parse_errors or note.provenance or len(note.claims) not in GROW_CLAIMS or any(
            c.range_utf8 is None or c.passes or c.cites or any(set(a.fields) - {"valid_at"} - {k for k in a.fields if k[0] == "_"}
                                                                 for a in c.anchors) for c in note.claims):
        return f"the draft needs {GROW_CLAIMS.start}-{GROW_CLAIMS.stop - 1} clean range claims with plain anchors and no records or citations", {}
    body, _, log = row["content"].partition("\n## Revision Log")
    if len(log.split("\n- ")) != 2 or not re.search(rf"\[\[{re.escape(staged['title'])}(?:\||\]\])", log):
        return "the Revision Log is not one line linking the source note", {}
    texts = {e["source"]: " ".join(e["text"].split()) for e in staged["evidence"]}
    claims = {c.number: (c, claim_text(note, row["content"], c.number) or "") for c in note.claims}
    if sum(_bare(text) for _, text in claims.values()) < GROW_COVERAGE * _bare(FENCE_RE.sub("", body)):
        return f"claims cover less than {GROW_COVERAGE:.0%} of the prose", {}
    with tempfile.TemporaryDirectory(prefix="atelier-wiki-growth-") as directory:  # weights read each note's file
        (draft := Path(directory) / "draft.md").write_text(row["content"], encoding="utf-8")
        origins = trusted(vault / growth["wiki"], as_of, (trust.parse_wiki_note(draft, as_of),))
    cited = {}
    for number, (claim, text) in claims.items():
        cited[number] = [k for a in claim.anchors if a.active_on(as_of) and (k := wiki_trust.evidence_key(a)) in texts
                         and wiki_trust.origin(a.fields["_anchor_type"], a.fields["_anchor_id"]) in origins]
        quote = " ".join(str(row["quotes"].get(f"c{number}", "")).split())
        if not cited[number]:
            return f"c{number} cites no staged source above the trust threshold", {}
        if len(quote) < 12 or not any(quote in texts[k] for k in cited[number]):
            return f"c{number} does not quote its source verbatim", {}
    files = {row["path"]: row["content"]}
    for code, segment in growth["shadows"].items():
        shadow = parse(copy := row["translations"].get(code, ""))
        head = re.match(r"---\n(.*?)\n---\n", copy, re.S)
        title = re.search(r'(?m)^title:\s*"?(.*?)"?\s*$', head[1]) if head else None
        if (shadow.parse_errors or sorted(c.number for c in shadow.claims) != sorted(claims) or not title or "aliases:" in head[1]
                or "lang:" not in head[1] or title[1] == note.title or titles.resolve(title[1]) is not None
                or FENCE_RE.findall(copy) != FENCE_RE.findall(row["content"])):
            return f"the {code} copy does not mirror the entry's claims and evidence", {}
        files[segment + row["path"][len(growth["wiki"]):]] = copy
    for number, (_, text) in claims.items():  # the slow check last
        passages = [p for k in cited[number] for p in wiki_evidence.passages(wiki_evidence.text(k), text)]
        if (supported := wiki_evidence.minicheck(text, passages)) is not True:
            return f"c{number}: MiniCheck {'is unavailable' if supported is None else 'finds no support'}", {}
    return None, files
