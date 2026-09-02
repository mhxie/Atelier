#!/usr/bin/env python3
"""Audit the private dining registry and canonical meal-history table."""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import unquote

REQUIRED_ROLES = (
    "Regional dining catalog",
    "Meal-history tracker",
    "Credit-perks catalog",
    "Benefits tracker",
    "Prepaid-balance tracker",
)
EXPECTED_COLUMNS = (
    "Date",
    "Restaurant",
    "City",
    "类型",
    "⭐",
    "评分",
    "再去",
    "健康",
    "人数",
    "总额",
    "人均",
    "Platform",
    "Credit",
    "必点·备注",
)
ESTABLISHMENT_COLUMNS = ("餐厅", "分店", "地址", "状态", "核验日", "来源")
LIFECYCLE_STATUSES = {"active", "closed", "moved", "unknown"}
UNKNOWN = {"", "—", "-"}
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
MONEY_RE = re.compile(r"^(~)?([$¥])([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)$")
PROFILE_ROLE_RE = re.compile(r"^[A-Za-z][A-Za-z -]+$")
LINK_RE = re.compile(r"\[[^\]]+\]\((?:<([^>]+)>|([^)]+))\)")
PENDING_MARKERS = ("待确认", "TBD", "UNKNOWN")
# profile/diet.md "Capture tiers" owns when 再去 is required. It is asked only
# while the log cannot already answer it: a 正餐 row with fewer than
# REVISIT_SETTLED_AFTER prior visits. Returning again IS the revisit answer.
BEVERAGE_TYPES = {"奶茶", "咖啡"}
REVISIT_SETTLED_AFTER = 2


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    path: str
    detail: str
    row: int | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "detail": self.detail,
        }
        if self.row is not None:
            payload["row"] = self.row
        return payload


def _display_path(path: Path, vault: Path) -> str:
    try:
        return f"$OV/{path.resolve().relative_to(vault.resolve()).as_posix()}"
    except ValueError:
        return path.as_posix()


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in stripped[1:-1]:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _section(text: str, heading: str) -> str | None:
    marker = f"## {heading}"
    if marker not in text:
        return None
    remainder = text.split(marker, 1)[1]
    next_heading = re.search(r"^## ", remainder, flags=re.MULTILINE)
    return remainder[: next_heading.start()] if next_heading else remainder


def _parse_catalog_paths(
    profile_path: Path, vault: Path
) -> tuple[dict[str, Path], list[Finding]]:
    findings: list[Finding] = []
    display = _display_path(profile_path, vault)
    if not profile_path.is_file():
        return {}, [
            Finding(
                "error",
                "profile_missing",
                display,
                "private dining profile does not exist",
            )
        ]

    text = profile_path.read_text(encoding="utf-8")
    section = _section(text, "Catalog files")
    if section is None:
        return {}, [
            Finding(
                "error",
                "catalog_section_missing",
                display,
                "profile has no 'Catalog files' section",
            )
        ]

    mappings: dict[str, Path] = {}
    for line_number, line in enumerate(section.splitlines(), start=1):
        cells = _split_markdown_row(line)
        if len(cells) < 2:
            continue
        role = cells[0].strip()
        raw_path = cells[1].strip().strip("`")
        if role in {"Role", "---"} or not PROFILE_ROLE_RE.fullmatch(role):
            continue
        if not raw_path:
            findings.append(
                Finding(
                    "error",
                    "catalog_path_empty",
                    display,
                    f"catalog role {role!r} has an empty path",
                    line_number,
                )
            )
            continue
        candidate = Path(raw_path).expanduser()
        resolved = candidate if candidate.is_absolute() else vault / candidate
        if role in mappings:
            findings.append(
                Finding(
                    "error",
                    "catalog_role_duplicate",
                    display,
                    f"catalog role {role!r} is declared more than once",
                    line_number,
                )
            )
            continue
        mappings[role] = resolved.resolve()

    for role in REQUIRED_ROLES:
        if role not in mappings:
            findings.append(
                Finding(
                    "error",
                    "catalog_role_missing",
                    display,
                    f"required catalog role {role!r} is not mapped",
                )
            )
            continue
        target = mappings[role]
        if not target.is_file():
            findings.append(
                Finding(
                    "error",
                    "catalog_file_missing",
                    _display_path(target, vault),
                    f"mapped file for {role!r} does not exist",
                )
            )

    duplicate_paths: dict[Path, list[str]] = {}
    for role, target in mappings.items():
        duplicate_paths.setdefault(target, []).append(role)
    for target, roles in duplicate_paths.items():
        if len(roles) > 1:
            findings.append(
                Finding(
                    "error",
                    "catalog_path_reused",
                    _display_path(target, vault),
                    f"one file is mapped to multiple roles: {', '.join(sorted(roles))}",
                )
            )
    return mappings, findings


def _health_vocabulary(profile_path: Path) -> set[str]:
    if not profile_path.is_file():
        return set()
    section = _section(
        profile_path.read_text(encoding="utf-8"), "Full health-flag taxonomy"
    )
    if section is None:
        return set()
    return {
        match.group(1).strip()
        for match in re.finditer(r"^- `([^`]+)`\s", section, flags=re.MULTILINE)
    }


def _parse_money(value: str) -> tuple[Decimal | None, bool, str | None]:
    if value in UNKNOWN:
        return None, False, None
    match = MONEY_RE.fullmatch(value)
    if not match:
        raise ValueError(value)
    try:
        return (
            Decimal(match.group(3).replace(",", "")),
            bool(match.group(1)),
            match.group(2),
        )
    except InvalidOperation as exc:
        raise ValueError(value) from exc


def _parse_party(value: str) -> int | None:
    if value in UNKNOWN:
        return None
    if not value.isdigit() or int(value) <= 0:
        raise ValueError(value)
    return int(value)


def _audit_meal_history(
    path: Path, profile_path: Path, vault: Path
) -> tuple[list[Finding], dict[str, int]]:
    findings: list[Finding] = []
    stats = {"rows": 0, "dated_rows": 0, "health_flags": 0}
    display = _display_path(path, vault)
    if not path.is_file():
        return [
            Finding("error", "meal_history_missing", display, "meal history is missing")
        ], stats

    lines = path.read_text(encoding="utf-8").splitlines()
    header_indexes = [
        index
        for index, line in enumerate(lines)
        if tuple(_split_markdown_row(line)) == EXPECTED_COLUMNS
    ]
    if not header_indexes:
        return [
            Finding(
                "error",
                "meal_table_missing",
                display,
                "meal history has no table with the canonical schema",
            )
        ], stats
    if len(header_indexes) > 1:
        return [
            Finding(
                "error",
                "meal_table_ambiguous",
                display,
                "meal history has more than one table with the canonical schema",
            )
        ], stats

    header_index = header_indexes[0]
    table_rows: list[tuple[int, list[str]]] = []
    for index in range(header_index, len(lines)):
        line = lines[index]
        if not line.strip().startswith("|"):
            break
        table_rows.append((index + 1, _split_markdown_row(line)))
    if len(table_rows) < 2:
        return [
            Finding(
                "error",
                "meal_table_empty",
                display,
                "canonical meal-history table has no separator row",
            )
        ], stats

    header_line, header = table_rows[0]
    if tuple(header) != EXPECTED_COLUMNS:
        findings.append(
            Finding(
                "error",
                "schema_mismatch",
                display,
                f"expected {len(EXPECTED_COLUMNS)} canonical columns, got {header!r}",
                header_line,
            )
        )

    health_vocabulary = _health_vocabulary(profile_path)
    if not health_vocabulary:
        findings.append(
            Finding(
                "error",
                "health_taxonomy_missing",
                _display_path(profile_path, vault),
                "profile has no parseable health-flag taxonomy",
            )
        )

    previous_date: date | None = None
    seen: set[tuple[date, str]] = set()
    prior_visits: dict[str, int] = {}
    for line_number, cells in table_rows[2:]:
        if not cells or all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        stats["rows"] += 1
        if len(cells) != len(EXPECTED_COLUMNS):
            findings.append(
                Finding(
                    "error",
                    "row_width",
                    display,
                    f"expected {len(EXPECTED_COLUMNS)} columns, got {len(cells)}",
                    line_number,
                )
            )
            continue

        row = dict(zip(EXPECTED_COLUMNS, cells, strict=True))
        if any(
            marker.casefold() in row["Restaurant"].casefold()
            for marker in PENDING_MARKERS
        ):
            findings.append(
                Finding(
                    "error",
                    "restaurant_pending",
                    display,
                    f"canonical row still has a placeholder restaurant: {row['Restaurant']!r}",
                    line_number,
                )
            )
        score_text = row["评分"].replace("*", "").strip()
        if score_text not in UNKNOWN:
            try:
                score = int(score_text)
            except ValueError:
                score = 0
            if not 1 <= score <= 10:
                findings.append(
                    Finding(
                        "error",
                        "score_invalid",
                        display,
                        f"score must be an integer from 1 to 10 or dash: {row['评分']!r}",
                        line_number,
                    )
                )
        if row["再去"] not in {*UNKNOWN, "Y", "N", "Maybe"}:
            findings.append(
                Finding(
                    "error",
                    "revisit_invalid",
                    display,
                    f"revisit must be Y, N, Maybe, or dash: {row['再去']!r}",
                    line_number,
                )
            )
        restaurant_key = _plain_restaurant(row["Restaurant"]).strip()
        seen_before = prior_visits.get(restaurant_key, 0)
        prior_visits[restaurant_key] = seen_before + 1
        revisit_required = (
            row["类型"].strip() not in BEVERAGE_TYPES
            and seen_before < REVISIT_SETTLED_AFTER
        )
        capture_fields_missing = score_text in UNKNOWN or (
            revisit_required and row["再去"] in UNKNOWN
        )
        if capture_fields_missing and any(
            marker.casefold() in row["必点·备注"].casefold()
            for marker in PENDING_MARKERS
        ):
            findings.append(
                Finding(
                    "error",
                    "capture_pending",
                    display,
                    "canonical row explicitly records unresolved capture fields",
                    line_number,
                )
            )
        date_match = DATE_RE.search(row["Date"])
        if not date_match:
            findings.append(
                Finding(
                    "error",
                    "date_missing",
                    display,
                    f"row has no ISO event date: {row['Date']!r}",
                    line_number,
                )
            )
            continue
        try:
            event_date = date.fromisoformat(date_match.group(1))
        except ValueError:
            findings.append(
                Finding(
                    "error",
                    "date_invalid",
                    display,
                    f"row has an invalid event date: {date_match.group(1)!r}",
                    line_number,
                )
            )
            continue
        stats["dated_rows"] += 1
        if previous_date is not None and event_date < previous_date:
            findings.append(
                Finding(
                    "error",
                    "date_order",
                    display,
                    f"{event_date.isoformat()} appears after {previous_date.isoformat()}",
                    line_number,
                )
            )
        previous_date = event_date

        identity = (event_date, re.sub(r"\[|\]|\([^)]*\)", "", row["Restaurant"]))
        if identity in seen:
            findings.append(
                Finding(
                    "warning",
                    "possible_duplicate",
                    display,
                    f"duplicate date and restaurant: {event_date} {row['Restaurant']}",
                    line_number,
                )
            )
        seen.add(identity)

        if row["健康"] not in UNKNOWN:
            flags = [flag.strip() for flag in row["健康"].split("·") if flag.strip()]
            stats["health_flags"] += len(flags)
            for flag in flags:
                if flag not in health_vocabulary:
                    findings.append(
                        Finding(
                            "error",
                            "health_flag_unknown",
                            display,
                            f"health flag {flag!r} is absent from profile taxonomy",
                            line_number,
                        )
                    )

        try:
            party = _parse_party(row["人数"])
        except ValueError:
            findings.append(
                Finding(
                    "error",
                    "party_invalid",
                    display,
                    f"party size must be a positive integer or dash: {row['人数']!r}",
                    line_number,
                )
            )
            party = None
        try:
            total, total_approximate, total_currency = _parse_money(row["总额"])
        except ValueError:
            findings.append(
                Finding(
                    "error",
                    "total_invalid",
                    display,
                    f"total must be $N, ~$N, ¥N, ~¥N, or dash: {row['总额']!r}",
                    line_number,
                )
            )
            total, total_approximate, total_currency = None, False, None
        try:
            per_person, per_person_approximate, per_person_currency = _parse_money(
                row["人均"]
            )
        except ValueError:
            findings.append(
                Finding(
                    "error",
                    "per_person_invalid",
                    display,
                    f"per-person must be $N, ~$N, ¥N, ~¥N, or dash: {row['人均']!r}",
                    line_number,
                )
            )
            per_person, per_person_approximate, per_person_currency = None, False, None

        if party is not None and total is not None:
            currency = total_currency or "$"
            expected = (total / Decimal(party)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if per_person is None:
                findings.append(
                    Finding(
                        "error",
                        "per_person_missing",
                        display,
                        f"party and total imply a per-person value of {currency}{expected}",
                        line_number,
                    )
                )
            elif per_person_currency != total_currency:
                findings.append(
                    Finding(
                        "error",
                        "currency_mismatch",
                        display,
                        "total and per-person values use different currencies",
                        line_number,
                    )
                )
            elif abs(per_person - expected) > Decimal("0.01"):
                findings.append(
                    Finding(
                        "error",
                        "per_person_mismatch",
                        display,
                        f"stored {currency}{per_person} does not match "
                        f"{currency}{total} / {party} = {currency}{expected}",
                        line_number,
                    )
                )
            if total_approximate and not per_person_approximate:
                findings.append(
                    Finding(
                        "warning",
                        "approximation_lost",
                        display,
                        "approximate total produced a non-approximate per-person value",
                        line_number,
                    )
                )

    return findings, stats


def _audit_establishment_registry(
    path: Path, vault: Path
) -> tuple[list[Finding], list[dict[str, str]]]:
    findings: list[Finding] = []
    establishments: list[dict[str, str]] = []
    display = _display_path(path, vault)
    if not path.is_file():
        return findings, establishments

    lines = path.read_text(encoding="utf-8").splitlines()
    header_indexes = [
        index
        for index, line in enumerate(lines)
        if tuple(_split_markdown_row(line)) == ESTABLISHMENT_COLUMNS
    ]
    if not header_indexes:
        return [
            Finding(
                "warning",
                "establishment_registry_missing",
                display,
                "regional dining catalog has no canonical establishment registry",
            )
        ], establishments
    if len(header_indexes) > 1:
        return [
            Finding(
                "error",
                "establishment_registry_ambiguous",
                display,
                "regional dining catalog has more than one establishment registry",
            )
        ], establishments

    seen: set[tuple[str, str]] = set()
    for index in range(header_indexes[0] + 2, len(lines)):
        cells = _split_markdown_row(lines[index])
        if not cells:
            break
        line_number = index + 1
        if len(cells) != len(ESTABLISHMENT_COLUMNS):
            findings.append(
                Finding(
                    "error",
                    "establishment_row_width",
                    display,
                    f"expected {len(ESTABLISHMENT_COLUMNS)} columns, got {len(cells)}",
                    line_number,
                )
            )
            continue
        row = dict(zip(ESTABLISHMENT_COLUMNS, cells, strict=True))
        identity = (row["餐厅"].casefold(), row["分店"].casefold())
        if any(value in UNKNOWN for value in identity) or row["地址"] in UNKNOWN:
            findings.append(
                Finding(
                    "error",
                    "establishment_identity_missing",
                    display,
                    "restaurant, branch, and address are required",
                    line_number,
                )
            )
        if identity in seen:
            findings.append(
                Finding(
                    "error",
                    "establishment_duplicate",
                    display,
                    f"duplicate restaurant branch: {row['餐厅']} / {row['分店']}",
                    line_number,
                )
            )
        seen.add(identity)
        if row["状态"] not in LIFECYCLE_STATUSES:
            findings.append(
                Finding(
                    "error",
                    "establishment_status_invalid",
                    display,
                    f"status must be one of {sorted(LIFECYCLE_STATUSES)}: {row['状态']!r}",
                    line_number,
                )
            )
        try:
            date.fromisoformat(row["核验日"])
        except ValueError:
            findings.append(
                Finding(
                    "error",
                    "establishment_verified_invalid",
                    display,
                    f"verification date must be YYYY-MM-DD: {row['核验日']!r}",
                    line_number,
                )
            )
        establishments.append(row)
    return findings, establishments


def _audit_branch_resolution(
    path: Path, establishments: list[dict[str, str]], vault: Path
) -> list[Finding]:
    if not path.is_file() or not establishments:
        return []
    branches: dict[str, set[str]] = {}
    for row in establishments:
        branches.setdefault(row["餐厅"].casefold(), set()).add(row["分店"].casefold())
    ambiguous = {name: values for name, values in branches.items() if len(values) > 1}
    if not ambiguous:
        return []

    lines = path.read_text(encoding="utf-8").splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if tuple(_split_markdown_row(line)) == EXPECTED_COLUMNS
        ),
        None,
    )
    if header_index is None:
        return []

    findings: list[Finding] = []
    display = _display_path(path, vault)
    for index in range(header_index + 2, len(lines)):
        cells = _split_markdown_row(lines[index])
        if not cells:
            break
        if len(cells) != len(EXPECTED_COLUMNS):
            continue
        restaurant = _plain_restaurant(cells[1]).strip()
        candidate_branches = ambiguous.get(restaurant.casefold())
        if candidate_branches and not any(
            branch in cells[1].casefold() for branch in candidate_branches
        ):
            findings.append(
                Finding(
                    "warning",
                    "establishment_branch_ambiguous",
                    display,
                    f"visit does not identify one of {sorted(candidate_branches)}: {restaurant}",
                    index + 1,
                )
            )
    return findings


def _audit_local_links(paths: set[Path], vault: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(paths):
        if not path.is_file():
            continue
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for match in LINK_RE.finditer(line):
                raw_target = (match.group(1) or match.group(2)).strip()
                if (
                    not raw_target
                    or raw_target.startswith(("http://", "https://", "mailto:", "#"))
                    or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw_target)
                ):
                    continue
                relative = unquote(raw_target.split("#", 1)[0])
                target = (path.parent / relative).resolve()
                if not target.exists():
                    findings.append(
                        Finding(
                            "error",
                            "local_link_broken",
                            _display_path(path, vault),
                            f"local Markdown target does not exist: {raw_target}",
                            line_number,
                        )
                    )
    return findings


def _audit_eligibility_catalog(path: Path, vault: Path) -> list[Finding]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    if "## Cycle Tracking" not in text and "| Cycle |" not in text:
        return []
    return [
        Finding(
            "error",
            "live_state_in_eligibility_catalog",
            _display_path(path, vault),
            "eligibility catalog still contains live benefit-cycle state",
        )
    ]


def _plain_restaurant(value: str) -> str:
    value = value.replace("**", "")
    match = re.fullmatch(r"\[([^\]]+)\]\(.+\)", value)
    return match.group(1) if match else value


def _recent_meals(
    path: Path, count: int
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if count <= 0 or not path.is_file():
        return [], {
            "known": 0,
            "coverage": 0.0,
            "direction": "unknown",
        }

    lines = path.read_text(encoding="utf-8").splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if tuple(_split_markdown_row(line)) == EXPECTED_COLUMNS
        ),
        None,
    )
    if header_index is None:
        return [], {
            "known": 0,
            "coverage": 0.0,
            "direction": "unknown",
        }

    parsed: list[tuple[date, int, dict[str, str]]] = []
    for index in range(header_index + 2, len(lines)):
        cells = _split_markdown_row(lines[index])
        if not cells:
            break
        if len(cells) != len(EXPECTED_COLUMNS):
            continue
        row = dict(zip(EXPECTED_COLUMNS, cells, strict=True))
        match = DATE_RE.search(row["Date"])
        if not match:
            continue
        try:
            event_date = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        parsed.append((event_date, index, row))

    selected = sorted(parsed, key=lambda item: (item[0], item[1]), reverse=True)[:count]
    recent: list[dict[str, str]] = []
    sourced: list[tuple[date, Decimal]] = []
    for event_date, _, row in selected:
        recent.append(
            {
                "date": event_date.isoformat(),
                "restaurant": _plain_restaurant(row["Restaurant"]),
                "score": row["评分"].replace("**", ""),
                "party": row["人数"],
                "total": row["总额"],
                "per_person": row["人均"],
            }
        )
        try:
            per_person, _, currency = _parse_money(row["人均"])
        except ValueError:
            per_person, currency = None, None
        if per_person is not None and currency == "$":
            sourced.append((event_date, per_person))

    trend: dict[str, Any] = {
        "known": len(sourced),
        "coverage": len(sourced) / len(recent) if recent else 0.0,
        "direction": "unknown",
    }
    if sourced:
        floats = [float(value) for _, value in sourced]
        trend["average"] = round(statistics.fmean(floats), 2)
        trend["median"] = round(statistics.median(floats), 2)
    if len(sourced) >= 5 and trend["coverage"] >= 0.6:
        chronological = [
            float(value) for _, value in sorted(sourced, key=lambda item: item[0])
        ]
        midpoint = len(chronological) // 2
        older = statistics.fmean(chronological[:midpoint])
        newer = statistics.fmean(chronological[midpoint:])
        change = newer - older
        trend.update(
            {
                "older_average": round(older, 2),
                "newer_average": round(newer, 2),
                "change": round(change, 2),
                "direction": (
                    "flat" if abs(change) < 3 else ("up" if change > 0 else "down")
                ),
                "confidence": "low" if len(sourced) < 5 else "medium",
            }
        )
    elif recent:
        trend["reason"] = (
            "direction requires at least 5 sourced values and 60% recent coverage"
        )
    return recent, trend


def audit(vault: Path, recent_count: int = 0) -> dict[str, Any]:
    vault = vault.expanduser().resolve()
    profile_path = vault / "profile" / "diet.md"
    mappings, findings = _parse_catalog_paths(profile_path, vault)
    stats: dict[str, object] = {
        "catalog_roles": len(mappings),
        "rows": 0,
        "dated_rows": 0,
        "health_flags": 0,
        "establishments": 0,
    }
    regional_catalog = mappings.get("Regional dining catalog")
    establishments: list[dict[str, str]] = []
    if regional_catalog is not None and regional_catalog.is_file():
        registry_findings, establishments = _audit_establishment_registry(
            regional_catalog, vault
        )
        findings.extend(registry_findings)
        stats["establishments"] = len(establishments)
    meal_history = mappings.get("Meal-history tracker")
    if meal_history is not None and meal_history.is_file():
        table_findings, table_stats = _audit_meal_history(
            meal_history, profile_path, vault
        )
        findings.extend(table_findings)
        findings.extend(
            _audit_branch_resolution(meal_history, establishments, vault)
        )
        stats.update(table_stats)
    findings.extend(
        _audit_local_links(
            {path for path in mappings.values() if path.is_file()}, vault
        )
    )
    eligibility = mappings.get("Credit-perks catalog")
    if eligibility is not None:
        findings.extend(_audit_eligibility_catalog(eligibility, vault))

    recent, per_person_trend = (
        _recent_meals(meal_history, recent_count)
        if meal_history is not None
        else (
            [],
            {"known": 0, "coverage": 0.0, "direction": "unknown"},
        )
    )

    errors = [finding.as_dict() for finding in findings if finding.severity == "error"]
    warnings = [
        finding.as_dict() for finding in findings if finding.severity == "warning"
    ]
    return {
        "ok": not errors,
        "vault": "$OV",
        "stats": stats,
        "errors": errors,
        "warnings": warnings,
        "establishments": establishments,
        "recent": recent,
        "per_person_trend": per_person_trend,
    }


def _resolve_vault(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    raw = os.environ.get("OV")
    if not raw:
        raise ValueError("$OV is not set; pass --vault explicitly")
    return Path(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--recent",
        type=int,
        default=0,
        metavar="N",
        help="include the latest N meals and a sourced per-person trend",
    )
    args = parser.parse_args()
    if args.recent < 0:
        parser.error("--recent must be non-negative")
    try:
        payload = audit(_resolve_vault(args.vault), args.recent)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"dining_audit: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        status = "clean" if payload["ok"] else "failed"
        print(
            f"dining_audit: {status}; "
            f"errors={len(payload['errors'])} warnings={len(payload['warnings'])}"
        )
        for finding in [*payload["errors"], *payload["warnings"]]:
            row = f":{finding['row']}" if "row" in finding else ""
            print(
                f"{finding['severity']}: {finding['path']}{row}: "
                f"{finding['code']}: {finding['detail']}"
            )
        if payload["recent"]:
            print()
            print("| Date | Restaurant | Score | Party | Total | Per person |")
            print("|---|---|---:|---:|---:|---:|")
            for meal in payload["recent"]:
                print(
                    f"| {meal['date']} | {meal['restaurant']} | "
                    f"{meal['score']} | {meal['party']} | {meal['total']} | "
                    f"{meal['per_person']} |"
                )
            trend = payload["per_person_trend"]
            print(
                f"per-person coverage: {trend['known']}/{len(payload['recent'])}; "
                f"direction: {trend['direction']}"
            )
            if "average" in trend:
                print(
                    f"known average: ${trend['average']:.2f}; "
                    f"median: ${trend['median']:.2f}"
                )
            if "change" in trend:
                print(
                    "newer vs older sourced average: "
                    f"${trend['newer_average']:.2f} vs "
                    f"${trend['older_average']:.2f} "
                    f"({trend['change']:+.2f}, {trend['confidence']} confidence)"
                )
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
