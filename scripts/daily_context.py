#!/usr/bin/env python3
"""daily_context.py: masthead context for the daily digest, deterministic.

Two facts that belong at the top of the morning document but that no routine
produces: what the sky will do where the user will actually be, and how much of
each coding harness's weekly window is still there. Both are read here so the
digest procedure stays a judgment layer and never a data-fetching one.

Quota is read from files the harnesses already leave behind, not from an API:

- Claude Code: the newest snapshot claude-hud writes under
  ``~/.cache/claude-hud/usage-*.json`` (``percent`` used, ``resetsAtEpoch``,
  ``model``). It only exists when a statusline has rendered recently, so the
  snapshot time is carried into the output and shown next to the number.
- Codex: the last ``rate_limits`` event in the newest rollout under
  ``~/.codex/sessions``. Same caveat, same treatment.

A passive snapshot can be a day old. That is acceptable for a weekly window,
which is why the digest shows the snapshot time rather than hiding it, and why
this script never opens a network connection for quota.

Weather is the one network call. The place comes from ``--place`` when the
digest procedure could read the calendar, otherwise from the private
``$OV/_meta/digest.toml`` (``[weather] place = "..."``), which is what an
unattended run uses. No place, no weather. Open-Meteo needs no key. A failed
fetch becomes a warning, never a missing document.

``--offline`` skips the weather fetch even when a place is configured, so a
run whose action allowlist grants no web access can still carry the quota
half, which never touches the network. The skip is reported as a warning
when a place was configured, so the reader knows why the masthead is bare.

Usage:
    daily_context.py [--place "Lisbon"] [--offline] [--date YYYY-MM-DD] --json [--out F]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PathsError, vault_root  # noqa: E402

CONTEXT_SCHEMA = 1  # must match routine_digest.CONTEXT_SCHEMA
DIGEST_CONFIG = "_meta/digest.toml"

HTTP_TIMEOUT = 12
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Remaining share of the window, not the used share: the number the reader
# acts on is how much is left, so that is the one that carries the colour.
QUOTA_GREEN_ABOVE = 40
QUOTA_AMBER_ABOVE = 20

# WMO weather interpretation codes, the subset Open-Meteo emits, in Chinese.
_WMO = {
    0: "晴",
    1: "大致晴",
    2: "少云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "毛毛雨",
    56: "冻雨",
    57: "冻雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "阵雪",
    95: "雷暴",
    96: "雷暴冰雹",
    99: "雷暴冰雹",
}

_CODEX_RATE_LIMITS = re.compile(
    r'"rate_limits":\s*(\{\s*"limit_id".*?"plan_type":\s*"[^"]*"[^}]*\})'
)


# ---------------------------------------------------------------- quota


def quota_level(left_percent: int) -> str:
    """'ok' above 40 % left, 'low' down to 20 %, 'critical' below that."""
    if left_percent > QUOTA_GREEN_ABOVE:
        return "ok"
    if left_percent > QUOTA_AMBER_ABOVE:
        return "low"
    return "critical"


def relative_reset(reset_epoch: float, now: float) -> str:
    """'1 天 23 小时后重置': a countdown, because a weekday and clock time make
    the reader do the subtraction the document should have done."""
    seconds = max(0, int(reset_epoch - now))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    if days:
        return f"{days} 天 {hours} 小时后重置"
    if hours:
        return f"{hours} 小时后重置"
    return f"{rem // 60} 分钟后重置"


def _quota_entry(
    name: str,
    window: str,
    used_percent: float,
    reset_epoch: float,
    snapshot_epoch: float,
    now: float,
) -> dict[str, Any]:
    used = max(0, min(100, int(round(used_percent))))
    left = 100 - used
    return {
        "name": name,
        "window": window,
        "used_percent": used,
        "left_percent": left,
        "level": quota_level(left),
        "reset_epoch": int(reset_epoch),
        "reset_relative": relative_reset(reset_epoch, now),
        "snapshot_epoch": int(snapshot_epoch),
        "snapshot_age_hours": round(max(0.0, now - snapshot_epoch) / 3600, 1),
    }


def read_claude_quota(cache_dir: Path, now: float) -> dict[str, Any] | None:
    """Newest claude-hud usage snapshot, or None when no statusline has run."""
    paths = sorted(glob.glob(str(cache_dir / "usage-*.json")), key=os.path.getmtime)
    if not paths:
        return None
    path = paths[-1]
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "percent" not in data or "resetsAtEpoch" not in data:
        return None
    fetched = float(data.get("fetchedAtMs") or 0) / 1000 or os.path.getmtime(path)
    model = str(data.get("model") or "").strip()
    window = f"{model} · 7d" if model else "7d"
    return _quota_entry(
        "Claude Code", window, float(data["percent"]), float(data["resetsAtEpoch"]), fetched, now
    )


def _find_key(obj: Any, key: str, depth: int = 0) -> Any:
    """First value under `key` anywhere in a nested JSON object."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], dict):
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_key(value, key, depth + 1)
            if found is not None:
                return found
    return None


def _rate_limits_from_line(line: str) -> dict[str, Any] | None:
    """Parse the whole line as JSON and walk to `rate_limits`; the regex is a
    fallback for a line that is not one complete JSON object."""
    try:
        found = _find_key(json.loads(line), "rate_limits")
        if found is not None:
            return found
    except json.JSONDecodeError:
        pass
    match = _CODEX_RATE_LIMITS.search(line)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def read_codex_quota(sessions_dir: Path, now: float) -> dict[str, Any] | None:
    """Last rate_limits event in the newest rollout, or None."""
    paths = sorted(
        glob.glob(str(sessions_dir / "*" / "*" / "*" / "rollout-*.jsonl")),
        key=os.path.getmtime,
    )
    for path in reversed(paths[-5:]):
        last: dict[str, Any] | None = None
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"rate_limits"' not in line:
                        continue
                    parsed = _rate_limits_from_line(line)
                    if parsed is not None:
                        last = parsed
        except OSError:
            continue
        primary = (last or {}).get("primary") or {}
        if not primary or "used_percent" not in primary or "resets_at" not in primary:
            continue
        minutes = int(primary.get("window_minutes") or 0)
        window = f"{minutes // 1440}d" if minutes >= 1440 else f"{minutes}m"
        plan = str(last.get("plan_type") or "").strip()
        label = f"{plan} · {window}" if plan else window
        return _quota_entry(
            "Codex",
            label,
            float(primary["used_percent"]),
            float(primary["resets_at"]),
            os.path.getmtime(path),
            now,
        )
    return None


# ---------------------------------------------------------------- weather


def _get_json(url: str, params: dict[str, Any]) -> Any:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}", headers={"User-Agent": "atelier-daily-context/1"}
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def pick_location(results: list[dict[str, Any]], region: str | None, country: str | None) -> dict[str, Any] | None:
    """The most populous candidate that matches the optional region and
    country. The geocoder's own first result is not population-ordered: a
    bare "Mountain View" comes back as the Arkansas town ahead of the
    California city, and the forecast for the wrong one is worse than none."""
    def ok(item: dict[str, Any]) -> bool:
        if region and str(item.get("admin1") or "").lower() != region.lower():
            return False
        if country and str(item.get("country_code") or "").lower() != country.lower():
            return False
        return True

    candidates = [r for r in results if isinstance(r, dict) and ok(r)]
    if not candidates:
        return None
    return max(candidates, key=lambda r: int(r.get("population") or 0))


def geocode(place: str, region: str | None = None, country: str | None = None) -> dict[str, Any]:
    data = _get_json(GEOCODE_URL, {"name": place, "count": 10, "language": "en"})
    top = pick_location(data.get("results") or [], region, country)
    if top is None:
        raise LookupError(f"no geocoding result for {place!r} (region={region!r}, country={country!r})")
    return {
        "name": str(top.get("name") or place),
        "region": str(top.get("admin1") or ""),
        "latitude": float(top["latitude"]),
        "longitude": float(top["longitude"]),
        "timezone": str(top.get("timezone") or "auto"),
    }


def summarize_forecast(daily: dict[str, Any], hourly: dict[str, Any], place: str) -> dict[str, Any]:
    """Reduce one day's forecast to the four numbers a masthead has room for."""
    code = int((daily.get("weather_code") or [0])[0])
    tmin = round(float((daily.get("temperature_2m_min") or [0])[0]))
    tmax = round(float((daily.get("temperature_2m_max") or [0])[0]))
    pop = int((daily.get("precipitation_probability_max") or [0])[0])
    hours: list[dict[str, Any]] = []
    for stamp, temp in zip(hourly.get("time") or [], hourly.get("temperature_2m") or []):
        hour = int(str(stamp)[11:13])
        if hour in (9, 12, 18):
            hours.append({"hour": hour, "temp": round(float(temp))})
    return {
        "place": place,
        "tmin": tmin,
        "tmax": tmax,
        "summary": _WMO.get(code, "未知"),
        "precip_probability": pop,
        "hours": hours,
    }


def fetch_weather(place: str, day: date, region: str | None = None, country: str | None = None) -> dict[str, Any]:
    location = geocode(place, region, country)
    data = _get_json(
        FORECAST_URL,
        {
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "hourly": "temperature_2m",
            "timezone": location["timezone"],
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
        },
    )
    summary = summarize_forecast(data.get("daily") or {}, data.get("hourly") or {}, location["name"])
    summary["date"] = day.isoformat()
    summary["region"] = location["region"]
    return summary


def place_from_config(ov: Path | None) -> dict[str, str] | None:
    """`[weather] place`, with optional `region` and `country`, from the
    private digest config; None when unset."""
    if ov is None:
        try:
            ov = vault_root()
        except PathsError:
            return None
    path = ov / DIGEST_CONFIG
    if not path.is_file():
        return None
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    weather = data.get("weather") if isinstance(data, dict) else None
    if not isinstance(weather, dict) or not str(weather.get("place") or "").strip():
        return None
    out = {"place": str(weather["place"]).strip()}
    for key in ("region", "country"):
        if str(weather.get(key) or "").strip():
            out[key] = str(weather[key]).strip()
    return out


# ---------------------------------------------------------------- build


def build(
    day: date,
    *,
    place: str | None,
    region: str | None = None,
    country: str | None = None,
    now: float | None = None,
    claude_cache: Path | None = None,
    codex_sessions: Path | None = None,
    weather_fetcher=fetch_weather,
    ov: Path | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    home = Path.home()
    claude_cache = claude_cache or home / ".cache" / "claude-hud"
    codex_sessions = codex_sessions or home / ".codex" / "sessions"
    warnings: list[str] = []

    quota = []
    claude = read_claude_quota(claude_cache, now)
    if claude:
        quota.append(claude)
    else:
        warnings.append("claude quota: no claude-hud snapshot found")
    codex = read_codex_quota(codex_sessions, now)
    if codex:
        quota.append(codex)
    else:
        warnings.append("codex quota: no rate_limits event found in recent sessions")

    weather: dict[str, Any] | None = None
    place_source = "argument" if place else ""
    region_arg, country_arg = region, country
    if not place:
        configured = place_from_config(ov)
        if configured:
            place = configured["place"]
            region_arg = region_arg or configured.get("region")
            country_arg = country_arg or configured.get("country")
            place_source = "config"
    if place and offline:
        warnings.append(f"weather skipped for {place!r}: --offline (no web access in this run)")
    elif place:
        try:
            weather = weather_fetcher(place, day, region_arg, country_arg)
            if weather is not None:
                weather["place_source"] = place_source
        except Exception as exc:  # network, geocoding, shape: all one outcome
            warnings.append(f"weather unavailable for {place!r}: {exc!r}")

    return {
        "schema": CONTEXT_SCHEMA,
        "date": day.isoformat(),
        "generated_epoch": int(now),
        "weather": weather,
        "quota": quota,
        "warnings": warnings,
    }


def text_view(context: dict[str, Any]) -> str:
    lines = [f"context for {context['date']}"]
    weather = context.get("weather")
    if weather:
        lines.append(
            f"weather: {weather['place']} {weather['tmin']}–{weather['tmax']}°C "
            f"{weather['summary']} 降水 {weather['precip_probability']}%"
        )
    for entry in context.get("quota") or []:
        lines.append(
            f"quota: {entry['name']} ({entry['window']}) 剩 {entry['left_percent']}% "
            f"[{entry['level']}] {entry['reset_relative']} · 快照 {entry['snapshot_age_hours']}h 前"
        )
    for warning in context.get("warnings") or []:
        lines.append(f"! {warning}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--place", help="Where the day is spent; enables the weather fetch.")
    parser.add_argument("--region", help="State or province to disambiguate the place.")
    parser.add_argument("--country", help="Two-letter country code to disambiguate the place.")
    parser.add_argument("--date", help="Forecast date YYYY-MM-DD (default today).")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Never fetch weather; quota only. For runs without web access.",
    )
    parser.add_argument("--json", action="store_true", help="JSON instead of a text report.")
    parser.add_argument("--out", help="Write to a file instead of stdout.")
    args = parser.parse_args(argv)

    day = date.fromisoformat(args.date) if args.date else datetime.now().date()
    context = build(
        day, place=args.place, region=args.region, country=args.country, offline=args.offline
    )
    payload = json.dumps(context, ensure_ascii=False, indent=2) if args.json else text_view(context)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(payload)
    for warning in context["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
