#!/usr/bin/env python3
"""feed_fetch.py: Fetch declared RSS/Atom channels into one bounded JSON payload.

Why this exists: the news routine declares 38 feeds and reached 1 of them on a
good day and 0 on most. Measured over six consecutive runs: 1/38, 0/31, 0/6,
0/20, 1/37, 1/38. Every day it still emitted twelve items, so it looked healthy
from the outside; the items came from ad-hoc web searches, not from the declared
channels. A routine that fires, produces output, and reports success while its
actual mechanism is dead is the most expensive kind of broken.

The cause was capability, not logic. That routine runs a model with web *search*
but no shell network, so its only way to reach a feed was the model's native
fetch, which does not parse XML. Fetching is not a judgement task and should
never have been the model's job.

So: the wrapper fetches, the model judges. Same split as the Readwise prefetch.

Stdlib only (`urllib` + `xml.etree`), because this runs where `uv` cannot write
its cache and no third-party parser is installed. That rules out `feedparser`
and means handling both RSS 2.0 and Atom by hand, which is a smaller problem
than it sounds: both are flat lists of entries with a title, a link, and a date.

Reachability is reported, not assumed. The point of this script is to make a
dead channel countable, so `--json` always carries per-feed status including the
failures, and the caller can put a real number in its report instead of a number
that was never measured.

Feed list lives in `$OV/_meta/feeds.toml`, not in a routine prompt. A URL is
data: keeping it in prose meant it could not be linted, counted, or fixed
without editing an archived prompt.

    # $OV/_meta/feeds.toml
    [[feed]]
    url = "https://example.com/feed/"
    label = "Example"
    tags = ["ai"]

Usage:
    uv run scripts/feed_fetch.py --json --out items.json
    uv run scripts/feed_fetch.py --days 3 --max-per-feed 5

Exit codes: 0 whenever the config could be read, including when every feed
failed. A fleet-wide outage is a reportable state and the caller needs the
report, not a non-zero exit that hides it.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import PathsError, fmt, vault_root  # noqa: E402

FEEDS_RELPATH = "_meta/feeds.toml"
HEALTH_RELPATH = "_meta/feed_health.json"

# A feed that fails this many consecutive runs, and that healing could not
# repair, stops being fetched. Five is roughly a working week: long enough to
# ride out an outage, short enough that a permanently moved feed does not spend
# a month dragging the reachability number down and hiding real regressions.
RETIRE_AFTER_FAILURES = 5

# Autodiscovery: <link rel="alternate" type="application/rss+xml" href="...">
_ALT_LINK = re.compile(
    r"<link[^>]+type=[\"']application/(?:rss|atom)\+xml[\"'][^>]*>", re.I
)
_HREF = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)

DEFAULT_DAYS = 2
DEFAULT_MAX_PER_FEED = 6
DEFAULT_TIMEOUT = 15
MAX_WORKERS = 8
SUMMARY_CHARS = 400

# Some publishers reject the stdlib default agent outright.
USER_AGENT = "Mozilla/5.0 (compatible; atelier-feed-fetch/1)"

_TAG = re.compile(r"\{[^}]*\}")
_HTML_TAG = re.compile(r"<[^>]+>")


def load_feeds(ov: Path) -> list[dict[str, str]]:
    path = ov / FEEDS_RELPATH
    if not path.is_file():
        raise SystemExit(
            f"feed list missing: {fmt(path)}; declare channels there rather than "
            "in a routine prompt"
        )
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise SystemExit(f"feed list unreadable: {exc!r}") from exc
    feeds = []
    for row in document.get("feed", []):
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        feeds.append(
            {
                "url": url,
                "label": str(row.get("label") or url),
                "tags": row.get("tags") or [],
            }
        )
    return feeds


def load_health(ov: Path) -> dict[str, dict[str, Any]]:
    path = ov / HEALTH_RELPATH
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_health(ov: Path, health: dict[str, dict[str, Any]]) -> None:
    (ov / HEALTH_RELPATH).write_text(
        json.dumps(health, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def heal_candidates(url: str) -> list[str]:
    """Cheap repairs to try before giving up on a URL.

    These cover what actually breaks in practice: a site moving to HTTPS, and a
    publisher renaming the path between the three conventional spellings. They
    are tried in order and the first that parses wins.
    """
    candidates: list[str] = []
    if url.startswith("http://"):
        candidates.append("https://" + url[len("http://"):])
    stem = url.rstrip("/")
    for suffix in ("/feed", "/rss", "/feed.xml", "/atom.xml", "/index.xml"):
        if stem.endswith(suffix):
            base = stem[: -len(suffix)]
            candidates.extend(
                base + other for other in ("/feed", "/rss", "/feed.xml", "/index.xml")
                if other != suffix
            )
            break
    else:
        candidates.extend(stem + suffix for suffix in ("/feed", "/rss", "/feed.xml"))
    return [c for c in dict.fromkeys(candidates) if c != url][:4]


def discover_feed(url: str, *, timeout: int) -> str:
    """Ask the site itself where its feed moved to.

    Autodiscovery is the only repair that survives a genuine restructure: the
    page still advertises its feed even when every conventional path is gone.
    """
    root = re.match(r"(https?://[^/]+)", url)
    if not root:
        return ""
    request = urllib.request.Request(root.group(1), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            html = response.read(200_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    for tag in _ALT_LINK.findall(html):
        href = _HREF.search(tag)
        if not href:
            continue
        candidate = href.group(1)
        if candidate.startswith("/"):
            candidate = root.group(1) + candidate
        if candidate.startswith(("http://", "https://")):
            return candidate
    return ""


def attempt_heal(feed: dict[str, Any], *, timeout: int) -> dict[str, Any] | None:
    """Try to find a working URL for a broken feed. None when nothing worked."""
    for candidate in heal_candidates(feed["url"]):
        result = fetch_one({**feed, "url": candidate}, timeout=timeout)
        if result["ok"]:
            return result
    discovered = discover_feed(feed["url"], timeout=timeout)
    if discovered and discovered != feed["url"]:
        result = fetch_one({**feed, "url": discovered}, timeout=timeout)
        if result["ok"]:
            return result
    return None


def _strip(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", _HTML_TAG.sub(" ", value)).strip()


def _tag(element: ElementTree.Element) -> str:
    return _TAG.sub("", element.tag).lower()


def _parse_when(raw: str) -> datetime | None:
    """RSS uses RFC 822 dates, Atom uses ISO 8601. Try both, give up quietly."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError, IndexError):
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """Entries from an RSS 2.0 or Atom document.

    Both formats are handled by walking for `item` and `entry` elements rather
    than by branching on the root tag: real feeds in the wild mix namespaces and
    wrappers, and a structural walk survives that where a strict reader does not.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    entries: list[dict[str, Any]] = []
    for element in root.iter():
        if _tag(element) not in {"item", "entry"}:
            continue
        title = link = when = summary = ""
        for child in element:
            name = _tag(child)
            if name == "title" and not title:
                title = _strip(child.text)
            elif name == "link" and not link:
                link = (child.get("href") or child.text or "").strip()
            elif name in {"pubdate", "published", "updated", "date"} and not when:
                when = (child.text or "").strip()
            elif name in {"description", "summary", "content"} and not summary:
                summary = _strip(child.text)
        if not title or not link:
            continue
        entries.append(
            {
                "title": title,
                "url": link,
                "published": when,
                "summary": summary[:SUMMARY_CHARS],
            }
        )
    return entries


# A feed larger than this is not a feed; bounded like discover_feed so one
# runaway channel cannot hold the whole collect in memory.
MAX_FEED_BYTES = 2_000_000


def fetch_one(feed: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    """Fetch and parse one feed. Never raises: a dead channel is a datum."""
    request = urllib.request.Request(feed["url"], headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_FEED_BYTES)
    # http.client errors (IncompleteRead, LineTooLong, RemoteDisconnected on
    # some paths) are not OSError subclasses; one truncated feed used to escape
    # the thread pool and abort every other channel with a traceback.
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        return {**feed, "ok": False, "error": type(exc).__name__, "entries": []}
    try:
        text = payload.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - decode fallback
        return {**feed, "ok": False, "error": "decode", "entries": []}
    entries = parse_feed(text)
    if not entries:
        return {**feed, "ok": False, "error": "unparseable-or-empty", "entries": []}
    return {**feed, "ok": True, "error": "", "entries": entries}


def collect(
    ov: Path,
    *,
    days: int = DEFAULT_DAYS,
    max_per_feed: int = DEFAULT_MAX_PER_FEED,
    timeout: int = DEFAULT_TIMEOUT,
    now: datetime | None = None,
    heal: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    declared = load_feeds(ov)
    health = load_health(ov)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # A retired feed is not fetched and does not count against reachability.
    # Leaving it in the denominator would keep the number permanently depressed
    # and hide the next real regression behind a known-dead channel.
    feeds = [f for f in declared if health.get(f["url"], {}).get("status") != "retired"]
    retired_before = [f for f in declared if f not in feeds]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(lambda f: fetch_one(f, timeout=timeout), feeds))

    if heal:
        # Healing is serial and only for what failed: it costs extra requests,
        # and doing it inside the parallel pass would multiply load on sites that
        # are already refusing us.
        repaired: list[dict[str, Any]] = []
        for index, result in enumerate(results):
            if result["ok"]:
                continue
            fixed = attempt_heal(result, timeout=timeout)
            if fixed:
                results[index] = fixed
                repaired.append({"label": fixed["label"], "was": result["url"],
                                 "now": fixed["url"]})
        healed = repaired
    else:
        healed = []

    items: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    retired_now: list[dict[str, str]] = []
    reached = 0
    for result in results:
        entry = dict(health.get(result["url"]) or {})
        if not result["ok"]:
            strikes = int(entry.get("consecutive_failures") or 0) + 1
            entry.update(
                {
                    "consecutive_failures": strikes,
                    "last_error": result["error"],
                    "last_checked": now.date().isoformat(),
                }
            )
            if strikes >= RETIRE_AFTER_FAILURES:
                entry["status"] = "retired"
                entry["retired_on"] = now.date().isoformat()
                retired_now.append({"label": result["label"], "error": result["error"]})
            health[result["url"]] = entry
            failures.append(
                {
                    "label": result["label"],
                    "error": result["error"],
                    "strikes": strikes,
                }
            )
            continue
        health[result["url"]] = {
            "consecutive_failures": 0,
            "last_ok": now.date().isoformat(),
            "status": "ok",
        }
        reached += 1
        kept = 0
        for entry in result["entries"]:
            when = _parse_when(entry["published"])
            # An undated entry is kept: plenty of feeds omit dates, and dropping
            # them would silently narrow the channel to nothing.
            if when is not None and when < cutoff:
                continue
            items.append({**entry, "source": result["label"], "tags": result["tags"]})
            kept += 1
            if kept >= max_per_feed:
                break

    seen: set[str] = set()
    deduped = []
    for item in items:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        deduped.append(item)

    for repair in healed:
        health.pop(repair["was"], None)
        health[repair["now"]] = {
            "consecutive_failures": 0,
            "last_ok": now.date().isoformat(),
            "status": "ok",
            "healed_from": repair["was"],
        }
    if persist:
        save_health(ov, health)

    return {
        "schema": 1,
        "generated": now.isoformat(timespec="seconds"),
        "window_days": days,
        "channels": {
            "declared": len(feeds),
            "reached": reached,
            "retired": len(retired_before) + len(retired_now),
        },
        "healed": healed,
        "retired": retired_now,
        "failures": failures,
        "items": deduped,
    }


def text_report(payload: dict[str, Any]) -> str:
    channels = payload["channels"]
    lines = [
        f"channels {channels['reached']}/{channels['declared']} · "
        f"{len(payload['items'])} items · window {payload['window_days']}d"
        + (f" · {channels['retired']} retired" if channels.get("retired") else "")
    ]
    for repair in payload.get("healed") or []:
        lines.append(f"  healed {repair['label']}: {repair['was']} -> {repair['now']}")
    for gone in payload.get("retired") or []:
        lines.append(f"  retired {gone['label']} after {RETIRE_AFTER_FAILURES} failures")
    for item in payload["items"][:20]:
        lines.append(f"  [{item['source']}] {item['title'][:80]}")
    if payload["failures"]:
        lines.append("")
        lines.append(f"unreachable ({len(payload['failures'])}):")
        for failure in payload["failures"][:12]:
            lines.append(
                f"  {failure['label']}: {failure['error']} "
                f"({failure.get('strikes', 1)}/{RETIRE_AFTER_FAILURES})"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch declared RSS/Atom channels.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--max-per-feed", type=int, default=DEFAULT_MAX_PER_FEED)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out")
    parser.add_argument(
        "--no-heal", action="store_true", help="Skip repair attempts on failures."
    )
    parser.add_argument(
        "--no-persist", action="store_true", help="Do not update feed_health.json."
    )
    args = parser.parse_args(argv)

    try:
        ov = vault_root()
    except PathsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload = collect(
        ov,
        days=max(1, args.days),
        max_per_feed=max(1, args.max_per_feed),
        timeout=max(1, args.timeout),
        heal=not args.no_heal,
        persist=not args.no_persist,
    )
    rendered = (
        json.dumps(payload, indent=2, ensure_ascii=False)
        if args.json
        else text_report(payload)
    )
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
        channels = payload["channels"]
        print(
            f"wrote {args.out} ({channels['reached']}/{channels['declared']} channels, "
            f"{len(payload['items'])} items)"
        )
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
