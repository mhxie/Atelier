"""Claim trust tiers, source reputation and the trust report Reflect renders.

Atelier owns these rules; Reflect only reads the report. Tier rules are pinned
by tests/fixtures/wiki-claim-trust.json. Source weights use a fixed scale, so a
threshold means the same thing however large the wiki grows.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import atomic_write, tier_segments, vault_root  # noqa: E402
import trust  # noqa: E402

VERSION = 1
ORDER = ("needs-work", "supported", "solid")
PRIMARY_TYPES = {"s2", "arxiv", "doi", "isbn"}
ADVERSARIAL = {"challenger", "scout"}
CODE_HOSTS = {"github.com", "gist.github.com", "raw.githubusercontent.com", "gitlab.com"}
ARXIV_PATH_RE = re.compile(r"^/(?:abs|pdf|html)/(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?/?$", re.I)
PRIORS = {"primary": 1.0, "reference": 0.6, "web": 0.3}
BREADTH_CAP = 20  # wiki entries; more citing entries no longer raise a weight
REFERENCE_RE = re.compile(r"^(code:|host:(docs\.|.+\.edu(\.|$)|(.+\.)?apache\.org$))")
THRESHOLD = 0.15
PRIVATE_RE = re.compile(r"\A---\r?\n(?:(?!---).*\r?\n)*?private:\s*true\s*\r?\n")
SCALE = 1 + math.log1p(BREADTH_CAP)


def origin(atype: str, aid: str) -> str:
    """The independent origin a source counts toward: paper by id, code host by owner, page by host."""
    if atype not in {"url", "gist"}:
        return f"{atype}:{aid.lower()}"
    parts = urlsplit(aid)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not parts.scheme or not host:
        return f"{atype}:{aid}"
    if host == "arxiv.org" and (match := ARXIV_PATH_RE.match(parts.path)):
        return f"arxiv:{match[1].lower()}"
    if host in {"doi.org", "dx.doi.org"} and parts.path[1:]:
        return f"doi:{unquote(parts.path[1:]).lower()}"
    if host in CODE_HOSTS:
        return f"code:{next((s for s in parts.path.split('/') if s), '').lower()}"
    return f"host:{host}"


def _origin(anchor: trust.Marker) -> str:
    return origin(anchor.fields["_anchor_type"], anchor.fields["_anchor_id"])


def prior(anchor: trust.Marker) -> str:
    """The writer's `kind` when stated, else papers and books are primary; docs, code, .edu and Apache sites are reference."""
    if kind := anchor.fields.get("kind"):
        return "primary" if kind == "primary" else "web"
    key = _origin(anchor)
    if key.split(":", 1)[0] in PRIMARY_TYPES:
        return "primary"
    return "reference" if REFERENCE_RE.match(key) else "web"


def claim_trust(claim: trust.Claim, as_of: date, cited: list[str], weights: dict[str, float] | None = None) -> dict:
    """One claim's tier, overlays and reasons; `weights` drops sources under the threshold."""
    origins: dict[str, bool] = {}
    reasons = []
    for anchor in (a for a in claim.anchors if a.active_on(as_of)):
        key = _origin(anchor)
        if weights is not None and weights.get(key, 0) < THRESHOLD:
            reasons.append({"kind": "untrusted-source", "text": f"{key} is below the trust threshold",
                            "data": {"origin": key, "weight": round(weights.get(key, 0), 3)}})
            continue
        origins[key] = origins.get(key, False) or prior(anchor) == "primary"
    primary = sum(origins.values())
    reasons.insert(0, {"kind": "evidence", "text": f"{len(origins)} independent sources, {primary} primary",
                       "data": {"origins": len(origins), "primary": primary}})
    strong = len(origins) >= 2 and primary >= 1
    tier = "supported" if strong or primary or len(origins) >= 2 else "needs-work"
    if best := max(cited, key=ORDER.index, default=None):
        reasons.append({"kind": "citation", "text": f"cites a {best} claim", "data": {"tier": best}})
        tier = max(tier, "supported", key=ORDER.index) if best != "needs-work" else tier
    passes = [((p.valid_at, i), p) for i, p in enumerate(claim.passes) if p.active_on(as_of)]
    edit = max((x for x in passes if x[1].fields["_agent"] == "editor"), default=None, key=lambda x: x[0])
    last = max((x for x in passes if x[1].fields["_agent"] in {"editor", "reviewer"}), default=None, key=lambda x: x[0])
    review = max((x for x in passes if x[1].fields["_agent"] in ADVERSARIAL and x[1].fields["status"] == "verified"
                  and (edit is None or x[0] > edit[0])), default=None, key=lambda x: x[0])
    overlays = []
    if strong and review:
        tier = "solid"
        reasons.append({"kind": "adversarial-review", "text": f"verified by {review[1].fields['_agent']}",
                        "data": {"agent": review[1].fields["_agent"], "at": review[1].fields["valid_at"]}})
    if dispute := claim.dispute(as_of):
        tier = "needs-work"
        overlays.append("disputed")
        reasons.append({"kind": "disputed", "text": f"{dispute.fields['status']} by {dispute.fields['_agent']}",
                        "data": {"agent": dispute.fields["_agent"], "status": dispute.fields["status"], "at": dispute.fields["valid_at"]}})
    if last is not None and last is edit:
        overlays.append("edited")
        reasons.append({"kind": "edited", "text": "text changed after its last review", "data": {"at": edit[1].fields["valid_at"]}})
    return {"tier": tier, "overlays": overlays, "reasons": reasons, "sources": sorted(origins)}


def sources(notes: list[trust.WikiNote], as_of: date) -> dict[str, dict]:
    """Per origin: weight = prior x breadth x undisputed share, on a fixed [0, 1] scale."""
    uses: dict[str, dict] = {}
    for note in notes:
        for claim in note.claims:
            for anchor in (a for a in claim.anchors if a.active_on(as_of)):
                row = uses.setdefault(_origin(anchor), {"prior": "web", "notes": set(), "claims": {}, "url": None})
                row["url"] = row["url"] or (anchor.fields["_anchor_id"] if anchor.fields["_anchor_type"] in {"url", "gist"} else None)
                row["prior"] = max(row["prior"], prior(anchor), key=lambda name: PRIORS[name])
                row["notes"].add(note.path)
                row["claims"][claim.key] = claim.dispute(as_of) is not None
    result = {}
    for key, row in sorted(uses.items()):
        notes_n, disputed = len(row["notes"]), sum(row["claims"].values())
        weight = PRIORS[row["prior"]] * (1 + math.log1p(min(notes_n, BREADTH_CAP))) * (1 - disputed / len(row["claims"])) / SCALE
        kind, ident = key.split(":", 1)
        url = row["url"] or {"arxiv": f"https://arxiv.org/abs/{ident}", "doi": f"https://doi.org/{ident}"}.get(kind)
        result[key] = {"label": ident if kind in {"host", "code"} else key, **({"url": url} if url else {}),
                       "weight": round(weight, 4), "trusted": weight >= THRESHOLD, "reasons": [
            {"kind": "prior", "text": f"{row['prior']} source", "data": {"prior": row["prior"], "value": PRIORS[row["prior"]]}},
            {"kind": "breadth", "text": f"cited by {notes_n} wiki entries", "data": {"entries": notes_n}},
            {"kind": "disputes", "text": f"{disputed} of {len(row['claims'])} supported claims disputed",
             "data": {"disputed": disputed, "claims": len(row["claims"])}}]}
    return result


def claim_hash(data: bytes, claim: trust.Claim) -> str | None:
    """SHA-256 of the claim's bytes between its markers with CRLF and CR as LF; legacy claims get none."""
    if claim.range_utf8 is None:
        return None
    text = data[claim.range_utf8[0]:claim.range_utf8[1]].replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(text).hexdigest()


def report(notes: list[trust.WikiNote], as_of: date) -> dict:
    trust._resolve_cites(notes)
    source_rows = sources([n for n in notes if n.integrity_ok()], as_of)
    weights = {key: row["weight"] for key, row in source_rows.items()}
    claims = {c.key: (n, c) for n in notes for c in n.claims}
    cited = {key: [f"{c.fields['_cite_path']}#C{c.fields['_cite_claim_number']}" if c.fields.get("_cite_claim_number")
                   else f"{c.fields['_cite_path']}" for c in claim.cites
                   if c.active_on(as_of) and "_cite_path" in c.fields and not c.fields.get("_provenance_only")]
             for key, (_note, claim) in claims.items()}
    whole = {n.path.as_posix(): [c.key for c in n.claims] for n in notes}
    results = {key: {"tier": "needs-work"} for key in claims}
    for _ in range(len(claims) + 1):  # citations only raise toward Supported, so this settles
        previous = {key: row["tier"] for key, row in results.items()}
        for key, (note, claim) in claims.items():
            targets = [t for ref in cited[key] for t in (whole.get(ref) or [ref]) if t in previous]
            results[key] = claim_trust(claim, as_of, [previous[t] for t in targets], weights) if note.integrity_ok() else {
                "tier": "needs-work", "overlays": [], "reasons": [{"kind": "integrity", "text": note.parse_errors[0]}]}
        if {key: row["tier"] for key, row in results.items()} == previous:
            break
    out = {}
    for note in notes:
        data = note.path.read_bytes()
        if PRIVATE_RE.match(data.decode("utf-8")):
            continue  # the report syncs wherever the graph does
        entries = {f"c{c.number}": {key: value for key, value in {
            **results[c.key], "evaluated_at": as_of.isoformat(), "text_sha256": claim_hash(data, c)}.items() if value != []}
            for c in note.claims if c.range_utf8}
        out[unicodedata.normalize("NFC", note.path.relative_to(vault_root()).as_posix())] = {"claims": entries}
    return {"format": "reflect-wiki-trust", "version": VERSION, "harness": {"name": "atelier"},
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_threshold": THRESHOLD, "notes": out, "sources": source_rows}


def reflect_target() -> tuple[Path, frozenset[str]]:
    """Where Reflect reads the report, and the folder names whose notes it must leave out.

    In a Reflect graph: what `reflect trust-report --json` prints (the user may move the
    report in Reflect's Settings; local-only folders never leave the device, but the report
    syncs and is committed), else Reflect's default `.harness/wiki-trust.json` and no
    folders when the CLI is missing or older. Exit 3 means Reflect cannot tell which
    folders are local-only, or the path links out of the graph, so nothing is written.
    Elsewhere `<meta>/wiki-trust.json`.
    """
    root = vault_root()
    if not (root / ".reflect").is_dir():
        return root / tier_segments().get("meta", "_meta") / "wiki-trust.json", frozenset()
    if cli := shutil.which("reflect"):
        try:
            done = subprocess.run([cli, "--graph", str(root), "trust-report", "--json"],
                                  capture_output=True, text=True, timeout=30, check=True)
            answer = json.loads(done.stdout)
            folders = frozenset(name.lower() for name in answer.get("localOnlyFolders", []))
            return Path(answer["absolutePath"]), folders
        except subprocess.CalledProcessError as err:
            if err.returncode == 3:
                raise SystemExit(f"reflect refused to name a safe report path: {err.stderr.strip()}") from err
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            pass
    return root / ".harness" / "wiki-trust.json", frozenset()


def local_only(path: Path, folders: frozenset[str]) -> bool:
    """Whether a note lies in a local-only folder: any folder in its graph path has one of
    the names. Reflect compares ASCII case-insensitively; lowercasing all letters only
    leaves out more."""
    return any(part.lower() in folders for part in path.relative_to(vault_root()).parts[:-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute the wiki trust report Reflect reads.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--write", action="store_true", help="write the report where Reflect reads it (see reflect_target) instead of stdout")
    args = parser.parse_args(argv)
    path, folders = reflect_target()
    notes = [note for note in trust.load_wiki(args.as_of) if not local_only(note.path, folders)]
    payload = json.dumps(report(notes, args.as_of), ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if not args.write:
        sys.stdout.write(payload)
        return 0
    atomic_write(path, payload)
    print(json.dumps({"output_file": path.relative_to(vault_root()).as_posix(), "bytes": len(payload.encode())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
