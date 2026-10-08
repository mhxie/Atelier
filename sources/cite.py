#!/usr/bin/env python3
"""APA citation formatter using Semantic Scholar API.

Usage:
    python3 cite.py arXiv:1706.03762
    python3 cite.py DOI:10.1145/3292500.3330701
    python3 cite.py arXiv:1706.03762 arXiv:2005.14165
    python3 cite.py --search "Attention Is All You Need"
    python3 cite.py --numbered arXiv:1706.03762 arXiv:2005.14165
"""

import sys
import json
import urllib.request
import urllib.parse
import urllib.error
import re

S2_BASE = "https://api.semanticscholar.org/graph/v1/paper"
S2_FIELDS = "title,authors,year,venue,externalIds,publicationVenue"


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "atelier-cite/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  API error: {e.code} for {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  Request failed: {e}", file=sys.stderr)
        return None


def fetch_s2(paper_id):
    url = f"{S2_BASE}/{urllib.parse.quote(paper_id, safe=':.')}?fields={S2_FIELDS}"
    return fetch_json(url)


def fetch_s2_batch(paper_ids):
    url = f"{S2_BASE}/batch?fields={S2_FIELDS}"
    data = json.dumps({"ids": paper_ids}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "atelier-cite/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  Batch request failed: {e}", file=sys.stderr)
        return None


def search_s2(query, limit=3):
    q = urllib.parse.quote(query)
    url = f"{S2_BASE}/search?query={q}&fields={S2_FIELDS}&limit={limit}"
    data = fetch_json(url)
    return data.get("data", []) if data else []


def format_authors_apa(authors):
    """Format author list in APA style."""
    if not authors:
        return "Unknown"
    names = []
    for a in authors:
        name = a.get("name", "")
        parts = name.rsplit(" ", 1)
        if len(parts) == 2:
            given, family = parts[0], parts[1]
            initials = ". ".join(w[0] for w in given.split() if w) + "."
            names.append(f"{family}, {initials}")
        else:
            names.append(name)

    if len(names) == 1:
        return names[0]
    elif len(names) == 2:
        return f"{names[0]} & {names[1]}"
    elif len(names) <= 20:
        return ", ".join(names[:-1]) + ", & " + names[-1]
    else:
        # APA 7: list first 19, ..., last
        return ", ".join(names[:19]) + ", ... " + names[-1]


def title_case_apa(title):
    """APA sentence case: capitalize first word and after colons."""
    if not title:
        return ""
    # Keep acronyms and proper nouns — don't lowercase aggressively
    # Just ensure first letter is capitalized
    result = title[0].upper() + title[1:]
    # Capitalize after ": "
    result = re.sub(r':\s+([a-z])', lambda m: ': ' + m.group(1).upper(), result)
    return result


def format_apa(paper):
    """Format a Semantic Scholar paper dict as APA citation."""
    if not paper:
        return "[Citation not found]"

    authors = format_authors_apa(paper.get("authors", []))
    # `or`: the S2 API returns an explicit `"year": null` for many preprints;
    # a key-absence default alone would print "(None)".
    year = paper.get("year") or "n.d."
    title = title_case_apa(paper.get("title", "Untitled"))
    venue = paper.get("venue", "")
    ext_ids = paper.get("externalIds", {}) or {}
    pub_venue = paper.get("publicationVenue") or {}
    venue_type = pub_venue.get("type", "")

    doi = ext_ids.get("DOI", "")
    arxiv_id = ext_ids.get("ArXiv", "")

    # Build the citation
    citation = f"{authors} ({year}). {title}."

    if venue and venue_type == "journal":
        citation += f" *{venue}*."
    elif venue:
        citation += f" In *{venue}*."
    elif arxiv_id and not venue:
        citation += f" *arXiv preprint arXiv:{arxiv_id}*."

    if doi:
        citation += f" https://doi.org/{doi}"
    elif arxiv_id:
        citation += f" https://arxiv.org/abs/{arxiv_id}"

    return citation


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    numbered = False

    if "--numbered" in args:
        numbered = True
        args.remove("--numbered")

    if "--search" in args:
        args.remove("--search")
        query = " ".join(args)
        results = search_s2(query)
        if not results:
            print(f"No results for: {query}")
            sys.exit(1)
        print(f"Top {len(results)} results for \"{query}\":\n")
        for i, paper in enumerate(results, 1):
            print(f"  [{i}] {format_apa(paper)}")
            print()
        sys.exit(0)

    # Single or batch lookup
    paper_ids = args

    if len(paper_ids) == 1:
        paper = fetch_s2(paper_ids[0])
        if paper:
            print(format_apa(paper))
        else:
            print(f"[Citation not found: {paper_ids[0]}]")
    else:
        # Batch lookup
        results = fetch_s2_batch(paper_ids)
        if not results:
            print("Batch lookup failed.")
            sys.exit(1)
        for i, paper in enumerate(results, 1):
            citation = format_apa(paper) if paper else f"[Not found: {paper_ids[i-1]}]"
            if numbered:
                print(f"[{i}] {citation}")
            else:
                print(citation)
            if i < len(results):
                print()


if __name__ == "__main__":
    main()
