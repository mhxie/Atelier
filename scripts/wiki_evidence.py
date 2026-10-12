"""Fetch, cache and screen the external evidence wiki anchors cite.

The networked trust routine refreshes this machine-local cache; nightly Autoevo stages passages from it
offline. arXiv resolves through its API and HTML, DOIs through Crossref (with Retraction Watch data).
"""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import html
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

from _paths import atomic_write

FRESH_DAYS = 30
RETRY_DAYS = 7
UNDECIDED_DAYS = 90
MAX_BYTES = 4_000_000
MAX_TEXT = 400_000
MIN_TEXT = 500  # shorter bodies are usually access, consent or error pages, so they are never staged
WIDTH, PER_SENTENCE, MAX_PASSAGES, FULL_CHARS = 2000, 2, 12, 120_000  # section-sized windows keep a fact's qualifications in view
PACE = {"export.arxiv.org": 3.0, "arxiv.org": 1.0, "api.crossref.org": 1.0}  # seconds between calls, per host
WORD_RE = re.compile(r"\w*\d(?:[\w.]*\w)?|\w{3,}")  # numbers count at any length: they are what numeric claims turn on
GITHUB_BLOB_RE = re.compile(r"https://github\.com/([^/]+/[^/]+)/blob/([^#?]+)")
GONE = {404, 410}  # other failures (403, 429, 5xx, no response) are usually blocking or outages, not link rot
RETRACTIONS = {"retraction", "withdrawal", "removal"}
WITHDRAWN_RE = re.compile(r"\b(?:this|the) (?:paper|article|submission|manuscript) (?:has been|is|was) withdrawn|^\s*withdrawn\b", re.I)
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+")
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))
RAW_HOSTS = {"raw.githubusercontent.com", "gist.githubusercontent.com"}
CODE_RE = re.compile(r"\.(?:py|md|txt|json|ya?ml|toml|proto|rs|go|ts|js|c|h|cc|cpp|java|sh)$", re.I)
ALT_RE = re.compile(r"""\balttext=(?:"([^"]*)"|'([^']*)')""", re.I)
HTML_RE = re.compile(r"<(?:!doctype|html|head|body)\b", re.I)
_last_call: dict[str, float] = {}


def _blocks(markup: str, tag: str, replace) -> str:
    """Replace each <tag ...>...</tag> in one linear pass (a lazy regex rescans on unclosed tags)."""
    parts = re.split(rf"(?i)</{tag}\s*>", markup)
    out = []
    for part in parts[:-1]:
        m = re.search(rf"(?i)<{tag}(?=[\s>/])", part)  # the block opens at the first such tag after the previous close
        out.append(part + " " if m is None else f"{part[:m.start()]} {replace(part[m.start():])} ")
    return "".join(out + parts[-1:])


def html_text(markup: str) -> str:
    # A formula becomes its LaTeX alt text: tag-stripped MathML loses roots and fractions.
    for tag in ("math", "mml:math"):  # JATS abstracts prefix MathML
        markup = _blocks(markup, tag, lambda block: next(filter(None, m.groups()), "") if (
            m := ALT_RE.search(block[:block.find(">") + 1])) else "[formula omitted]")
    for tag in ("script", "style", "nav", "header", "footer", "noscript", "svg", "annotation-xml", "annotation"):
        markup = _blocks(markup, tag, lambda block: "")
    markup = re.sub(r"(?i)<(?:p|div|li|br|tr|section|h[1-6]|blockquote|pre)\b", "\n<", markup)
    lines = html.unescape(re.sub(r"<[^<>]*>", " ", markup)).translate(ZERO_WIDTH).splitlines()
    return "\n".join(" ".join(line.split()) for line in lines if line.strip())


class _PublicPeer:
    """Refuse a non-public peer: checking the connected socket, not a DNS answer, also covers rebinding and redirects."""

    def connect(self):
        super().connect()
        if not ipaddress.ip_address(self.sock.getpeername()[0]).is_global:
            self.sock.close()
            raise OSError("evidence fetches reach public addresses only")


_HTTP = type("_HTTP", (_PublicPeer, http.client.HTTPConnection), {})
_HTTPS = type("_HTTPS", (_PublicPeer, http.client.HTTPSConnection), {})


class _Handler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler, urllib.request.HTTPRedirectHandler):
    def http_open(self, req):
        return self.do_open(_HTTP, req)

    def https_open(self, req):
        return self.do_open(_HTTPS, req, context=self._context)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme not in {"http", "https"}:
            raise urllib.error.URLError("evidence redirects stay on http or https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Handler)
_LOCAL, _MINICHECK_DOWN = urllib.request.build_opener(urllib.request.ProxyHandler({})), []
MINICHECK = "http://127.0.0.1:11434/api/generate", "bespoke-minicheck"  # local Ollama; the LLM-AggreFact WiCE leader  # direct, so the peer check sees the host


def _get(url: str) -> tuple[int, str, str]:
    """(HTTP status or 0 when unreachable, final URL, body text)."""
    try:
        host = urlsplit(url).hostname or ""
        time.sleep(max(0.0, _last_call.get(host, 0.0) + PACE.get(host, 0.0) - time.monotonic()))
        _last_call[host] = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": "atelier-wiki-evidence/1"})
        with _OPENER.open(request, timeout=15) as response:
            return response.status, response.geturl(), response.read(MAX_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, url, ""
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return 0, url, ""


def fetch(ident: str, get=_get) -> dict | None:
    """One evidence record: status, title, retraction and text. Books and S2 ids have nothing to fetch."""
    kind, _, value = ident.partition(":")
    if kind == "arxiv":
        # Metadata from the latest version, where arXiv records a withdrawal; text from the cited one.
        api, latest = "https://export.arxiv.org/api/query?id_list=", re.sub(r"v[0-9]+$", "", value)
        status, _, feed = get(api + quote(latest))
        entry = re.search(r"<entry>(.*?)</entry>", feed, re.S)
        checked = status == 200 and bool(entry) and "/api/errors" not in entry[1]  # the withdrawal check itself succeeded

        def field(name: str, entry=entry) -> str:
            match = entry and re.search(rf"<{name}[^>]*>(.*?)</{name}>", entry[1], re.S)
            return html_text(match[1]) if match else ""
        full_status, _, page = get(f"https://arxiv.org/html/{quote(value)}")
        text = html_text(page) if full_status == 200 else ""
        cited_status, _, cited_feed = (status, "", feed) if value == latest else get(api + quote(value))
        cited = re.search(r"<entry>(.*?)</entry>", cited_feed, re.S)
        if len(text) < MIN_TEXT:  # some papers have only a stub page: use the cited version's abstract
            missing = cited_status == 200 and (not cited or "/api/errors" in cited[1])  # the cited version does not exist
            text, status = field("summary", cited), 404 if missing else status if cited_status == 200 else cited_status
        row = {"status": 404 if status == 200 and (not entry or "/api/errors" in entry[1]) else status, "title": field("title", cited or entry), "text": text,
               "retracted": bool(WITHDRAWN_RE.search(field("arxiv:comment")) or WITHDRAWN_RE.search(field("summary")[:300])), "checked": checked}
    elif kind == "doi":
        status, _, body = get(f"https://api.crossref.org/v1/works/{quote(value, safe='/')}")
        try:
            work = json.loads(body)["message"] if status == 200 else {}
            title = " ".join(work.get("title") or [])
            row = {"status": 0 if status == 404 else status, "title": title, "text": "\n".join(filter(None, [title, html_text(work.get("abstract", ""))])),
                   "retracted": any(update.get("type") in RETRACTIONS for update in work.get("updated-by", [])), "checked": status == 200}
        except (ValueError, KeyError, TypeError, AttributeError):  # a portal or truncated body fails; a Crossref 404 (DataCite) is not rot
            row = {"status": 0, "title": "", "text": "", "retracted": False}
    elif kind in {"url", "gist"} and urlsplit(value).scheme in {"http", "https"}:
        blob = GITHUB_BLOB_RE.match(value)  # GitHub refuses scripted page views but serves the raw file
        target = f"https://raw.githubusercontent.com/{blob[1]}/{blob[2]}" if blob else value
        status, final, body = get(target)
        body = "" if body.startswith("%PDF") else body
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        markup = urlsplit(final).hostname not in RAW_HOSTS and not CODE_RE.search(urlsplit(final).path) and HTML_RE.search(body[:4096])
        row = {"status": status, "title": html_text(title[1]) if title else "",
               "text": html_text(body) if markup else body.strip()}  # raw files and code keep `<` and `>`
    else:
        return None
    row["text"] = row["text"][:MAX_TEXT]
    return row


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "atelier" / "wiki-evidence"


def _path(ident: str, suffix: str) -> Path:
    return cache_dir() / f"{hashlib.sha256(ident.encode()).hexdigest()[:32]}{suffix}"


def _read(path: Path, parse=json.loads, default=None):
    try:
        return parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load(ident: str) -> dict | None:
    return _read(_path(ident, ".json"))


def minicheck(claim: str, texts: list[str]) -> bool | None:
    """Whether the local MiniCheck model finds the claim supported in any 20k-character chunk; None when unavailable."""
    doc = "\n\n".join(texts)
    if _MINICHECK_DOWN and time.time() - _MINICHECK_DOWN[-1] < 900:  # wall clock: monotonic time stops while macOS sleeps
        return None
    try:
        for i in range(0, max(len(doc), 1), 20_000):
            body = json.dumps({"model": MINICHECK[1], "prompt": f"Document: {doc[i:i + 20_000]}\nClaim: {claim}", "stream": False,
                               "options": {"num_predict": 1, "temperature": 0, "num_ctx": 8192}}).encode()
            with _LOCAL.open(urllib.request.Request(MINICHECK[0], data=body), timeout=60) as response:
                if json.load(response)["response"].strip().lower().startswith("yes"):
                    return True
        return False
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
        _MINICHECK_DOWN.append(time.time())  # skip MiniCheck for 15 minutes, so a hung server costs one timeout a run
        return None


def withdrawn(origin: str, as_of: date) -> bool:
    return (since := _read(_path(origin, ".retracted"), default=False)) is True or isinstance(since, str) and since <= as_of.isoformat()


def text(ident: str) -> str:
    return _read(_path(ident, ".txt"), str, "")


def undecided(claim: str, today: date) -> bool:
    """Whether this exact claim and evidence had an undecided source check in the last UNDECIDED_DAYS."""
    seen = _read(cache_dir() / "undecided.json", default={}).get(hashlib.sha256(claim.encode()).hexdigest(), "")
    return seen > (today - timedelta(days=UNDECIDED_DAYS)).isoformat()


def remember_undecided(claims: list[str], today: date) -> None:
    """Rest undecided checks so they do not take every night's slots; new text or evidence ends the rest."""
    if claims:
        rows = {k: v for k, v in _read(cache_dir() / "undecided.json", default={}).items() if v > (today - timedelta(days=UNDECIDED_DAYS)).isoformat()}
        rows |= {hashlib.sha256(c.encode()).hexdigest(): today.isoformat() for c in claims}
        atomic_write(cache_dir() / "undecided.json", json.dumps(rows, sort_keys=True) + "\n")


def refresh(idents: set[str], today: date, seconds: float, get=_get) -> dict[str, int]:
    """Fetch missing or stale records, oldest first, until the time budget runs out.

    A failed fetch changes only its status and its count of consecutive 404/410 answers:
    earlier text, title and retraction survive an outage, and it is retried after RETRY_DAYS.
    """
    deadline, counts = time.monotonic() + seconds, {"fetched": 0, "fresh": 0, "failed": 0, "retracted": 0, "remaining": 0}
    rows = sorted(((load(i) or {}, i) for i in idents), key=lambda pair: (pair[0].get("checked_at", ""), pair[1]))
    for old, ident in rows:
        wait = FRESH_DAYS if old.get("status") == 200 else RETRY_DAYS
        if old.get("checked_at", "") > (today - timedelta(days=wait)).isoformat():
            counts["fresh"] += 1
        elif time.monotonic() >= deadline:
            counts["remaining"] += 1
        else:  # mark the attempt first, so a fetch killed mid-way cannot stall every later night
            atomic_write(_path(ident, ".json"), json.dumps({**old, "status": 0, "ident": ident, "checked_at": today.isoformat()}) + "\n")
            if (row := fetch(ident, get)) is None:
                continue
            failed = row["status"] != 200
            fresh_text = not failed and bool(row["text"]) and len(row["text"]) >= len(text(ident)) // 4  # else a soft 404: keep old text
            if fresh_text:
                atomic_write(_path(ident, ".txt"), row["text"])
            new = {**{k: v for k, v in row.items() if k != "text"}, **({"title": old["title"]} if "title" in old and (failed or not fresh_text) else {}),
                   "retracted": bool(row["retracted"] if row.get("checked") else row.get("retracted") or old.get("retracted")),
                   "gone": old.get("gone", 0) + 1 if row["status"] in GONE else 0, "ident": ident,
                   "checked_at": today.isoformat()}
            new["retracted_since"] = (old.get("retracted_since") or today.isoformat()) if new["retracted"] else None
            atomic_write(_path(ident, ".json"), json.dumps(new, ensure_ascii=False) + "\n")
            if ident.startswith("arxiv:") and row.get("checked"):  # only a completed check sets or clears the work's flag
                work = _path(re.sub(r"v[0-9]+$", "", ident), ".retracted")  # keep the earliest date the work was seen withdrawn
                atomic_write(work, json.dumps(min(d for d in (_read(work, default=False), new["retracted_since"]) if isinstance(d, str)) if new["retracted"] else False))
            counts["fetched"] += 1
            counts["failed"] += failed
            counts["retracted"] += bool(new.get("retracted"))
    return counts


def _tokens(text: str) -> set[str]:
    """Words of three or more letters, plus character pairs from CJK runs, which have no spaces."""
    lowered = text.lower()
    return set(WORD_RE.findall(CJK_RE.sub(" ", lowered))) | {run[i:i + 2] for run in CJK_RE.findall(lowered) for i in range(len(run) - 1)}


def passages(body: str, claim: str) -> list[str]:
    """For each sentence or clause of `claim`, the PER_SENTENCE windows of `body` sharing its rarest words;
    up to MAX_PASSAGES, in source order.

    Matching sentence by sentence keeps every fact of a long claim in view, as atomic-fact checkers do.
    """
    if len(body) < MIN_TEXT:
        return []
    windows, current = [], ""
    pieces = (long[i:i + WIDTH] for long in re.split(r"(?<=[.!?])\s+|(?<=[。！？])|\n+", body) for i in range(0, len(long), WIDTH))
    for piece in pieces:
        if current and len(current) + len(piece) > WIDTH:
            windows.append(current)
            current = ""
        current = f"{current} {piece}".strip()
    windows += [current] if current else []
    sets = [_tokens(w) for w in windows]
    chosen: dict[int, float] = {}
    for sentence in re.split(r"(?<=[.!?;])\s+|(?<=[。！？；])", claim):  # clauses approximate atomic facts
        query = _tokens(sentence)
        df = {word: sum(word in s for s in sets) for word in query}
        scored = sorted(((sum(math.log1p(len(sets) / df[w]) for w in query & s), i) for i, s in enumerate(sets)), reverse=True)
        for score, i in scored[:PER_SENTENCE]:
            if score > 0:
                chosen[i] = max(chosen.get(i, 0), score)
    best = sorted(sorted(chosen, key=chosen.get, reverse=True)[:MAX_PASSAGES])
    return [windows[i] for i in best]
