#!/usr/bin/env python3
"""Refresh the derived reminder cache consumed by ``daily_brief.py``.

This is deterministic scheduled work, not a model routine.  AniList supplies
the user's library, exact same-day airing schedules, and explicitly configured
follow-up metadata.  Concert reminders are derived from private vault policy
and the discovery cache.  The command preserves every top-level cache key it
does not own and writes atomically so readers never observe a partial refresh.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import atomic_write, fmt, tier_segments, vault_root  # noqa: E402

CACHE_NAME = "hi-tracking.json"
ANILIST_CONFIG_NAME = "anilist.toml"
CONCERT_CONFIG_NAME = "concerts.toml"
MUSIC_DISCOVERY_NAME = "music-event-discovery.json"
ANILIST_ENDPOINT = "https://graphql.anilist.co"
REQUEST_TIMEOUT_SECONDS = 10
DISCOVERY_MODE_LABELS = {
    "familiar": "熟悉",
    "adjacent": "邻接",
    "counter-profile": "反画像",
}

QueryFn = Callable[[str, dict[str, object]], dict[str, Any]]


def _tier_for(ov: Path, name: str) -> Path:
    """Resolve one registry tier against an explicit vault root."""
    segment = tier_segments().get(name)
    if not isinstance(segment, str):
        raise ValueError(f"path registry has no {name!r} tier")
    path = Path(segment).expanduser()
    return path.resolve() if path.is_absolute() else ov / path


def _title(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    for field in ("userPreferred", "english", "romaji", "native"):
        candidate = value.get(field)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _anilist_query(query: str, variables: dict[str, object]) -> dict[str, Any]:
    request = Request(
        ANILIST_ENDPOINT,
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "atelier-tracking/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("AniList returned a non-object response")
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        message = first.get("message") if isinstance(first, dict) else first
        raise ValueError(f"AniList query failed: {message}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("AniList returned no data")
    return data


def _load_config(ov: Path) -> dict[str, Any]:
    path = _tier_for(ov, "meta") / ANILIST_CONFIG_NAME
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a TOML object")
    return value


def _library(config: dict[str, Any], query_fn: QueryFn) -> tuple[list[dict[str, Any]], list[int]]:
    anime = config.get("anime")
    if not isinstance(anime, dict):
        raise ValueError("anilist.toml has no [anime] table")
    username = anime.get("username")
    if not isinstance(username, str) or not username.strip():
        raise ValueError("AniList username missing")

    data = query_fn(
        """
        query ($name: String) {
          MediaListCollection(userName: $name, type: ANIME) {
            lists {
              entries {
                status
                progress
                media {
                  id
                  title { userPreferred english romaji native }
                }
              }
            }
          }
        }
        """,
        {"name": username.strip()},
    )
    collection = data.get("MediaListCollection")
    lists = collection.get("lists") if isinstance(collection, dict) else None
    if not isinstance(lists, list):
        raise ValueError("AniList library response has no lists")

    library: list[dict[str, Any]] = []
    current_ids: list[int] = []
    for group in lists:
        entries = group.get("entries") if isinstance(group, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            media = entry.get("media")
            media_id = media.get("id") if isinstance(media, dict) else None
            title = _title(media.get("title")) if isinstance(media, dict) else None
            if not isinstance(media_id, int) or title is None:
                continue
            status = entry.get("status")
            library.append(
                {
                    "id": media_id,
                    "title": title,
                    "status": status,
                    "progress": entry.get("progress"),
                }
            )
            if status == "CURRENT":
                current_ids.append(media_id)

    library.sort(key=lambda item: (str(item.get("status")), str(item.get("title"))))
    return library, sorted(set(current_ids))


def _same_day_airings(
    current_ids: list[int],
    local_now: datetime,
    query_fn: QueryFn,
) -> list[str]:
    if not current_ids:
        return []
    start = datetime.combine(local_now.date(), datetime.min.time(), local_now.tzinfo)
    end = datetime.combine(local_now.date(), datetime.max.time(), local_now.tzinfo)
    data = query_fn(
        """
        query ($ids: [Int], $start: Int, $end: Int) {
          Page(page: 1, perPage: 50) {
            airingSchedules(
              mediaId_in: $ids,
              airingAt_greater: $start,
              airingAt_lesser: $end,
              sort: TIME
            ) {
              episode
              airingAt
              mediaId
              media { title { userPreferred english romaji native } }
            }
          }
        }
        """,
        {
            "ids": current_ids,
            "start": int(start.timestamp()) - 1,
            "end": int(end.timestamp()) + 1,
        },
    )
    page = data.get("Page")
    rows = page.get("airingSchedules") if isinstance(page, dict) else None
    if not isinstance(rows, list):
        raise ValueError("AniList airing response has no schedules")

    updates: list[tuple[int, str]] = []
    current_set = set(current_ids)
    for row in rows:
        if not isinstance(row, dict) or row.get("mediaId") not in current_set:
            continue
        episode = row.get("episode")
        airing_at = row.get("airingAt")
        media = row.get("media")
        title = _title(media.get("title")) if isinstance(media, dict) else None
        if not isinstance(episode, int) or not isinstance(airing_at, int) or title is None:
            continue
        airing = datetime.fromtimestamp(airing_at, local_now.tzinfo)
        if airing.date() != local_now.date():
            continue
        state = "已更新" if airing <= local_now else "将更新"
        updates.append(
            (
                airing_at,
                f"{title} Ep.{episode} {airing:%H:%M} {airing:%Z} {state}",
            )
        )
    updates.sort(key=lambda item: (item[0], item[1]))
    return [text for _, text in updates]


def _anime_updates(
    ov: Path,
    local_now: datetime,
    query_fn: QueryFn = _anilist_query,
) -> dict[str, Any]:
    config = _load_config(ov)
    anime = config["anime"]
    zone_name = anime.get("timezone") if isinstance(anime, dict) else None
    if isinstance(zone_name, str) and zone_name.strip():
        local_now = local_now.astimezone(ZoneInfo(zone_name.strip()))
    library, current_ids = _library(config, query_fn)
    return {
        "date": local_now.date().isoformat(),
        "last_success_at": local_now.isoformat(),
        "failed_at": None,
        "error": None,
        "updates": _same_day_airings(current_ids, local_now, query_fn),
        "library": library,
    }


def _start_date(value: object) -> str | None:
    if not isinstance(value, dict) or not isinstance(value.get("year"), int):
        return None
    parts = [str(value["year"])]
    for field in ("month", "day"):
        part = value.get(field)
        if not isinstance(part, int):
            break
        parts.append(f"{part:02d}")
    return "-".join(parts)


def _followup_updates(
    ov: Path,
    local_now: datetime,
    previous: object,
    query_fn: QueryFn = _anilist_query,
) -> dict[str, Any]:
    config = _load_config(ov)
    rows = config.get("followup", [])
    ids = [row.get("media_id") for row in rows if isinstance(row, dict)]
    ids = sorted({media_id for media_id in ids if isinstance(media_id, int)})
    today = local_now.date().isoformat()
    if not ids:
        return {
            "date": today,
            "last_success_at": local_now.isoformat(),
            "failed_at": None,
            "error": None,
            "items": [],
            "updates": [],
        }

    data = query_fn(
        """
        query ($ids: [Int]) {
          Page(page: 1, perPage: 50) {
            media(id_in: $ids, type: ANIME) {
              id
              status
              startDate { year month day }
              title { userPreferred english romaji native }
            }
          }
        }
        """,
        {"ids": ids},
    )
    page = data.get("Page")
    media_rows = page.get("media") if isinstance(page, dict) else None
    if not isinstance(media_rows, list):
        raise ValueError("AniList follow-up response has no media")

    prior_items = previous.get("items", []) if isinstance(previous, dict) else []
    if not isinstance(prior_items, list):
        prior_items = []
    prior_by_id = {
        item["id"]: item
        for item in prior_items
        if isinstance(item, dict) and isinstance(item.get("id"), int)
    }
    updates: list[str] = []
    if isinstance(previous, dict) and previous.get("date") == today:
        prior_updates = previous.get("updates", [])
        if isinstance(prior_updates, list):
            updates.extend(str(item) for item in prior_updates if str(item).strip())

    items: list[dict[str, Any]] = []
    for media in media_rows:
        if not isinstance(media, dict) or not isinstance(media.get("id"), int):
            continue
        title = _title(media.get("title"))
        if title is None:
            continue
        item = {
            "id": media["id"],
            "title": title,
            "status": media.get("status"),
            "start_date": _start_date(media.get("startDate")),
        }
        items.append(item)
        prior = prior_by_id.get(item["id"])
        if prior is None:
            continue
        if prior.get("start_date") != item["start_date"]:
            before = prior.get("start_date") or "未定"
            after = item["start_date"] or "未定"
            updates.append(f"{title} 档期更新：{before} → {after}")
        elif prior.get("status") != item["status"]:
            updates.append(f"{title} 状态更新：{prior.get('status')} → {item['status']}")
    items.sort(key=lambda item: (str(item.get("title")), item["id"]))
    return {
        "date": today,
        "last_success_at": local_now.isoformat(),
        "failed_at": None,
        "error": None,
        "items": items,
        "updates": list(dict.fromkeys(updates)),
    }


def _concert_reminders(ov: Path, local_now: datetime) -> dict[str, Any]:
    meta = _tier_for(ov, "meta")
    cache_dir = _tier_for(ov, "cache")
    config_path = meta / CONCERT_CONFIG_NAME
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    concerts = config.get("concerts", {})
    if not isinstance(concerts, dict):
        raise ValueError("concert list invalid")
    items = concerts.get("items", [])
    remind_days = concerts.get("remind_days", 14)
    if not isinstance(items, list) or not isinstance(remind_days, int):
        raise ValueError("concert list invalid")

    try:
        discovered = json.loads(
            (cache_dir / MUSIC_DISCOVERY_NAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        discovered = {}
    candidates = discovered.get("candidates", []) if isinstance(discovered, dict) else []
    source_items: list[tuple[object, bool]] = []
    if isinstance(candidates, list):
        source_items.extend((item, True) for item in candidates)
    source_items.extend((item, False) for item in items)

    merged: dict[tuple[str, date, str], tuple[dict[str, Any], bool]] = {}
    for raw_item, is_discovery in source_items:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        artist = item.get("artist")
        try:
            concert_date = date.fromisoformat(str(item.get("date")))
        except ValueError:
            continue
        sale_date = None
        if item.get("sale_date"):
            try:
                sale_date = date.fromisoformat(str(item["sale_date"]))
            except ValueError:
                sale_date = None
        if not isinstance(artist, str) or not artist.strip():
            continue
        venue = item.get("venue")
        identity = (
            artist.strip(),
            concert_date,
            venue.strip() if isinstance(venue, str) else "",
        )
        item["artist"] = artist.strip()
        item["date"] = concert_date
        item["sale_date"] = sale_date
        if identity not in merged:
            merged[identity] = (item, is_discovery)
            continue
        existing, existing_is_discovery = merged[identity]
        if is_discovery:
            for key, value in item.items():
                if existing.get(key) in (None, "") and value not in (None, ""):
                    existing[key] = value
        else:
            existing.update(
                {key: value for key, value in item.items() if value not in (None, "")}
            )
            existing_is_discovery = False
        merged[identity] = (existing, existing_is_discovery)

    today = local_now.date()
    reminders: list[str] = []
    for item, is_discovery in merged.values():
        if item.get("status") in {"tickets_bought", "not_interested", "pass"}:
            continue
        artist = item["artist"]
        concert_date = item["date"]
        sale_date = item.get("sale_date")
        venue = item.get("venue")
        place = f"{artist} · {venue}" if isinstance(venue, str) and venue else artist
        context: list[str] = []
        mode_label = DISCOVERY_MODE_LABELS.get(str(item.get("discovery_mode")))
        if mode_label:
            context.append(mode_label)
        why_now = item.get("why_now")
        if isinstance(why_now, str) and why_now.strip():
            context.append(why_now.strip())
        if context:
            place += f" [{' · '.join(context)}]"
        if sale_date == today:
            reminders.append(
                f"{place} 今日开售（{concert_date.month}/{concert_date.day}），要买票吗？"
            )
        elif is_discovery and item.get("discovered_on") == today.isoformat():
            reminders.append(
                f"新候选：{place}（{concert_date.month}/{concert_date.day}），要看票吗？"
            )
        elif (concert_date - today).days == remind_days:
            reminders.append(
                f"{place} 距离演出 {remind_days} 天，尚未购票；要买票吗？"
            )
    return {
        "date": today.isoformat(),
        "last_success_at": local_now.isoformat(),
        "failed_at": None,
        "error": None,
        "reminders": reminders,
    }


def _failed(previous: object, local_now: datetime, exc: Exception) -> dict[str, Any]:
    section = dict(previous) if isinstance(previous, dict) else {}
    section["failed_at"] = local_now.isoformat()
    section["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    return section


def refresh(
    ov: Path,
    now: datetime | None = None,
    *,
    query_fn: QueryFn = _anilist_query,
) -> dict[str, Any]:
    """Refresh owned cache sections and preserve the last success on failure."""
    ov = ov.expanduser().resolve()
    cache_path = _tier_for(ov, "cache") / CACHE_NAME
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}
    if not isinstance(cache, dict):
        cache = {}

    local_now = now or datetime.now().astimezone()
    errors: list[str] = []
    successes: list[str] = []

    try:
        cache["anime"] = _anime_updates(ov, local_now, query_fn)
        successes.append("anime")
    except (
        HTTPError,
        URLError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
        ZoneInfoNotFoundError,
    ) as exc:
        cache["anime"] = _failed(cache.get("anime"), local_now, exc)
        errors.append(f"anime: {exc}")

    try:
        cache["followups"] = _followup_updates(
            ov, local_now, cache.get("followups"), query_fn
        )
        successes.append("followups")
    except (
        HTTPError,
        URLError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as exc:
        cache["followups"] = _failed(cache.get("followups"), local_now, exc)
        errors.append(f"followups: {exc}")

    try:
        cache["concerts"] = _concert_reminders(ov, local_now)
        successes.append("concerts")
    except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        cache["concerts"] = _failed(cache.get("concerts"), local_now, exc)
        errors.append(f"concerts: {exc}")

    cache["schema"] = 1
    if successes:
        cache["refreshed_at"] = local_now.isoformat()
    atomic_write(cache_path, json.dumps(cache, ensure_ascii=False, indent=2) + "\n")
    return {
        "cache": fmt(cache_path),
        "successes": successes,
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ov",
        type=Path,
        help="Vault root override; defaults to the OV environment variable.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.ov is not None:
        os.environ["OV"] = str(args.ov.expanduser().resolve())
    try:
        result = refresh(vault_root())
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"refreshed {result['cache']}")
        for error in result["errors"]:
            print(f"warning: {error}", file=sys.stderr)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
