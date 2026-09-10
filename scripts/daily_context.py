#!/usr/bin/env python3
"""Digest weather and timestamped quota; setup lives in the digest command.

--refresh-quota uses CodexBar OAuth. --offline forbids network; scheduled
weather is always off. Missing context is non-fatal.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PathsError, atomic_write, tier_segments, vault_root  # noqa: E402

CONTEXT_SCHEMA = 1  # must match routine_collect.CONTEXT_SCHEMA
DIGEST_CONFIG = "_meta/digest.toml"

HTTP_TIMEOUT = 12
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

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

# ---------------------------------------------------------------- quota


def quota_level(left_percent: int) -> str:
    """'ok' above 40 % left, 'low' down to 20 %, 'critical' below that."""
    if left_percent > 40:
        return "ok"
    if left_percent > 20:
        return "low"
    return "critical"


def relative_reset(reset_epoch: float, now: float) -> str:
    """Chinese reset countdown, rounded down to whole minutes."""
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
    values = (used_percent, reset_epoch, snapshot_epoch)
    if (name not in {"Codex", "Claude Code"} or not isinstance(window, str)
            or not window[:-1].isdigit() or window[-1:] not in {"m", "h", "d"}
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
            or not 0 <= used_percent <= 100 or not 0 < snapshot_epoch <= now + 300
            or reset_epoch <= max(now, snapshot_epoch)):
        raise ValueError("invalid or expired quota window")
    used = int(round(used_percent))
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


def _epoch(value: str) -> float:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("quota timestamp has no timezone")
    return stamp.timestamp()


def _codexbar_rows() -> list[dict[str, Any]]:
    """One bounded OAuth read; never inherit GUI hooks or token overrides."""
    env = {k: v for k, v in os.environ.items() if k in {
        "HOME", "PATH", "LANG", "TMPDIR", "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
    }}
    env["CODEXBAR_CONFIG"] = str(Path(__file__).resolve().parents[1] / "harness/codexbar.json")
    proc = subprocess.Popen(
        ["codexbar", "usage", "--provider", "both", "--source", "oauth", "--format", "json", "--json-only"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, start_new_session=True,
    )
    try:
        stdout, _ = proc.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    data = json.loads(stdout)  # A nonzero exit can still contain one good provider.
    rows = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("error"):
            continue
        name = {"codex": "Codex", "claude": "Claude Code"}.get(item.get("provider"))
        usage = item.get("usage")
        if not name or not isinstance(usage, dict):
            continue
        for key in ("primary", "secondary"):
            window = usage.get(key)
            if not isinstance(window, dict):
                continue
            try:
                minutes = window["windowMinutes"]
                if type(minutes) is not int or minutes <= 0:
                    continue
                label = f"{minutes // 1440}d" if minutes % 1440 == 0 else (
                    f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m")
                rows.append(dict(name=name, window=label, used_percent=window["usedPercent"],
                                 reset_epoch=_epoch(window["resetsAt"]), snapshot_epoch=_epoch(usage["updatedAt"])))
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                continue
    return rows


def read_quota(cache: Path | None, now: float, refresh: bool) -> tuple[list[dict[str, Any]], list[str]]:
    warnings = []
    try:
        rows = _codexbar_rows() if refresh else json.loads(cache.read_text()) if cache else []
    except (OSError, TypeError, ValueError, OverflowError, subprocess.SubprocessError):
        rows = []
    entries, clean = [], []
    for row in rows if isinstance(rows, list) else []:
        try:
            entry = _quota_entry(now=now, **row)
        except (TypeError, ValueError, OverflowError):
            warnings.append("quota: invalid or expired window omitted")
            continue
        entries.append(entry)
        clean.append(row)
    if refresh and cache:
        mask = os.umask(0o077)
        try:
            if cache.exists():
                cache.chmod(0o600)
            atomic_write(cache, json.dumps(clean) + "\n")
        except OSError:
            warnings.append("quota: cache write unavailable")
        finally:
            os.umask(mask)
    for name in ("Codex", "Claude Code"):
        if not any(entry["name"] == name for entry in entries):
            warnings.append(f"{name} quota: unavailable")
    return entries, warnings


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
    code = int(daily["weather_code"][0])
    tmin = round(float(daily["temperature_2m_min"][0]))
    tmax = round(float(daily["temperature_2m_max"][0]))
    pop = int(daily["precipitation_probability_max"][0])
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
    quota_cache: Path | None = None,
    weather_fetcher=fetch_weather,
    ov: Path | None = None,
    offline: bool = False,
    refresh_quota: bool = False,
    no_weather: bool = False,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    if quota_cache is None:
        try:
            quota_cache = (ov or vault_root()) / tier_segments()["cache"] / "digest-quota.json"
        except PathsError:
            pass
    allowed = "quota:read" in os.environ.get("ATELIER_ROUTINE_PERMISSIONS", "").split(",")
    scheduled = "ATELIER_ROUTINE_PROFILE" in os.environ
    no_weather = no_weather or scheduled
    denied = offline or (scheduled and not allowed)
    quota, warnings = read_quota(quota_cache, now, refresh_quota and not denied)
    if refresh_quota and denied:
        warnings.append("quota refresh skipped: offline or routine permission denied")

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
    if place and (offline or no_weather):
        warnings.append(f"weather skipped for {place!r}: {'--offline' if offline else '--no-weather'}")
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
        help="No network: cached quota only, no weather fetch.",
    )
    parser.add_argument("--refresh-quota", action="store_true", help="Refresh quota through CodexBar OAuth.")
    parser.add_argument("--no-weather", action="store_true", help="Skip weather independently of quota refresh.")
    parser.add_argument("--json", action="store_true", help="JSON instead of a text report.")
    parser.add_argument("--out", help="Write to a file instead of stdout.")
    args = parser.parse_args(argv)

    day = date.fromisoformat(args.date) if args.date else datetime.now().date()
    context = build(
        day, place=args.place, region=args.region, country=args.country, offline=args.offline,
        refresh_quota=args.refresh_quota, no_weather=args.no_weather,
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
