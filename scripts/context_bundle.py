#!/usr/bin/env python3
"""Select routed OV sources, then pack them with pinned local Repomix."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Any, Sequence

import _node
from _paths import effective_date, tier_segments


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INTENTS_PATH = ROOT / "harness" / "intents.toml"
REPOMIX_BIN = ROOT / "node_modules" / ".bin" / "repomix"
REPOMIX_VERSION = "1.18.0"
TOKEN_ENCODING = "o200k_base"
MAX_CONTEXT_TOKENS = 16_384
PROFILE_STALE_DAYS = 7
REPOMIX_ENV_PASSTHROUGH = (
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SystemRoot",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "ComSpec",
    "PATHEXT",
)

SESSION_SECTIONS = ("Anomalies", "Continuity")
READING_CAPSULE = "Reading Capsule"
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-|$)")
SEQUENCE_RE = re.compile(r"-(\d+)$")
READING_LOG_RE = re.compile(r"-reading(?:-\d+)?$")
LAST_BUILT_RE = re.compile(
    r"^Last built:[ \t]*(\d{4}-\d{2}-\d{2})[ \t]*$", re.MULTILINE
)


class BundleError(ValueError):
    """A user-facing route, source, or Repomix contract error."""


def parse_effective_date(value: str | None) -> date:
    if value is None:
        return effective_date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BundleError(
            f"invalid --effective-date {value!r}; expected YYYY-MM-DD"
        ) from exc


def normalize_heading(value: str) -> str:
    value = re.sub(r"[`*_]", "", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def markdown_sections(text: str) -> dict[str, tuple[str, str]]:
    """Return first exact section per normalized heading, fences excluded."""
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    active_fence: str | None = None
    for index, line in enumerate(lines):
        fence_match = FENCE_RE.match(line)
        if fence_match:
            fence = fence_match.group(1)[0]
            active_fence = fence if active_fence is None else (
                None if active_fence == fence else active_fence
            )
            continue
        if active_fence is not None:
            continue
        heading_match = HEADING_RE.match(line)
        if heading_match:
            title = heading_match.group(2).strip().rstrip("#").strip()
            if title:
                headings.append((index, len(heading_match.group(1)), title))

    result: dict[str, tuple[str, str]] = {}
    for position, (line_index, level, title) in enumerate(headings):
        end = len(lines)
        for next_index, next_level, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_index
                break
        body = "\n".join(lines[line_index + 1 : end]).strip()
        rendered = f"{'#' * level} {title}" + (f"\n\n{body}" if body else "") + "\n"
        result.setdefault(normalize_heading(title), (title, rendered))
    return result


def load_intents() -> dict[str, dict[str, Any]]:
    try:
        from intent_coverage import load_intents as load_routed_intents

        return load_routed_intents()
    except (ImportError, SystemExit) as exc:
        raise BundleError(f"cannot load intent registry: {exc}") from exc


def validate_profile_reads(value: Any, intent: str) -> list[str]:
    if not isinstance(value, list):
        raise BundleError(f"intents.{intent}.profile_reads must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise BundleError(f"intents.{intent}.profile_reads must contain strings")
        filename = item.strip()
        path = Path(filename)
        if (
            not filename
            or path.is_absolute()
            or path.name != filename
            or filename in {".", ".."}
        ):
            raise BundleError(
                f"intents.{intent}.profile_reads entry {item!r} must be one "
                "filename under profile/"
            )
        if filename not in result:
            result.append(filename)
    return result


def resolve_route(intent_arg: str) -> tuple[str, list[str], int]:
    intent = intent_arg.strip()
    if intent.startswith("intents."):
        intent = intent[len("intents.") :]
    if not intent:
        raise BundleError("selected intent name is empty")
    overlay = DEFAULT_INTENTS_PATH.with_name("intents.local.toml")
    if overlay.is_file():
        try:
            with overlay.open("rb") as handle:
                local_row = tomllib.load(handle).get("intents", {}).get(intent, {})
        except (OSError, tomllib.TOMLDecodeError):
            local_row = {}
        if isinstance(local_row, dict) and "context_budget_bytes" in local_row:
            raise BundleError(
                f"intents.{intent}.context_budget_bytes is retired; use "
                "context_budget_tokens in o200k_base tokens"
            )
    row = load_intents().get(intent)
    if row is None:
        raise BundleError(f"intent {intent!r} is not declared in {DEFAULT_INTENTS_PATH}")
    if "context_budget_bytes" in row:
        raise BundleError(
            f"intents.{intent}.context_budget_bytes is retired; use "
            "context_budget_tokens in o200k_base tokens"
        )
    budget = row.get("context_budget_tokens")
    if type(budget) is not int or not 1 <= budget <= MAX_CONTEXT_TOKENS:
        raise BundleError(
            f"intents.{intent}.context_budget_tokens must be an integer from 1 "
            f"to {MAX_CONTEXT_TOKENS}"
        )
    return intent, validate_profile_reads(row.get("profile_reads", []), intent), budget


def resolve_vault(value: str | None) -> Path:
    raw = value or os.environ.get("OV")
    if not raw:
        raise BundleError("vault path unavailable; pass --vault or set OV")
    vault = Path(raw).expanduser().resolve()
    if not vault.is_dir():
        raise BundleError(f"vault is not a directory: {vault}")
    return vault


def resolve_source(vault: Path, raw_path: str) -> tuple[Path, str]:
    vault = vault.resolve()
    requested = Path(raw_path).expanduser()
    if not requested.is_absolute():
        requested = vault / requested
    resolved = requested.resolve()
    try:
        relative = resolved.relative_to(vault).as_posix()
    except ValueError as exc:
        raise BundleError(f"source escapes the vault: {requested}") from exc
    return resolved, relative


def read_utf8(path: Path) -> tuple[str | None, str | None]:
    try:
        return path.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, "missing"
    except IsADirectoryError:
        return None, "is_directory"
    except UnicodeDecodeError:
        return None, "not_utf8"
    except OSError:
        return None, "unreadable"


def split_source_spec(vault: Path, value: str) -> tuple[str, str | None]:
    raw = value.strip()
    if not raw:
        raise BundleError("--source cannot be empty")
    whole = Path(raw).expanduser()
    if not whole.is_absolute():
        whole = vault / whole
    if whole.exists() or "#" not in raw:
        return raw, None
    path_part, section = raw.rsplit("#", 1)
    return (path_part, section.strip()) if path_part and section.strip() else (raw, None)


def dated_markdown_paths(directory: Path, effective_date: date) -> list[Path]:
    if not directory.is_dir():
        return []
    found: list[tuple[tuple[date, int, str], Path]] = []
    for path in directory.rglob("*.md"):
        match = DATE_RE.match(path.name)
        if not path.is_file() or not match:
            continue
        try:
            path_date = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if path_date > effective_date:
            continue
        sequence_match = SEQUENCE_RE.search(path.stem)
        sequence = int(sequence_match.group(1)) if sequence_match else 1
        found.append(((path_date, sequence, path.as_posix()), path))
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found]


def freshness(source: str, text: str, effective_date: date) -> tuple[str, str | None]:
    match = LAST_BUILT_RE.search(text)
    try:
        built = date.fromisoformat(match.group(1) if match else "")
    except ValueError:
        return "unknown Last built date", f"{source} has no valid Last built date"
    age = (effective_date - built).days
    if age < 0:
        return f"invalid future Last built {built}", f"{source} has future Last built {built}"
    if age > PROFILE_STALE_DAYS:
        return f"stale, built {built} ({age} days old)", f"{source} is stale (built {built})"
    return f"current, built {built} ({age} days old)", None


def add_section(
    selected: dict[str, str],
    full_sources: set[str],
    section_refs: list[str],
    omissions: list[str],
    source: str,
    text: str,
    requested: str,
) -> bool:
    section = markdown_sections(text).get(normalize_heading(requested))
    if section is None:
        omissions.append(f"{source}#{requested}: section missing")
        return False
    title, rendered = section
    if not rendered.partition("\n")[2].strip():
        omissions.append(f"{source}#{title}: empty")
        return False
    if source not in full_sources and rendered not in selected.get(source, ""):
        selected[source] = selected.get(source, "") + ("\n" if source in selected else "") + rendered
    section_refs.append(f"{source}#{title}")
    return True


def select_context(
    *,
    vault: Path,
    intent_arg: str,
    source_specs: Sequence[str],
    effective_date: date,
) -> tuple[dict[str, str], str, list[str], int]:
    intent, profile_reads, budget = resolve_route(intent_arg)
    selected: dict[str, str] = {}
    full_sources: set[str] = set()
    section_refs: list[str] = []
    profile_status: list[str] = []
    omissions: list[str] = []
    warnings: list[str] = []

    for filename in profile_reads:
        source = f"profile/{filename}"
        path = ROOT / source
        text, error = read_utf8(path)
        if error is not None or text is None or not text.strip():
            raise BundleError(
                f"required profile {source} is {error or 'empty'}; run /introspect "
                "or $introspect before this route"
            )
        selected[source] = text
        full_sources.add(source)
        status, warning = freshness(source, text, effective_date)
        profile_status.append(f"{source}: {status}")
        if warning:
            warnings.append(warning + "; consider /introspect or $introspect")

    sessions = tier_segments()["sessions"]
    session_paths = dated_markdown_paths(vault / sessions, effective_date)
    if not session_paths:
        omissions.append(f"{sessions}/: no dated session logs")
    else:
        path, source = resolve_source(vault, str(session_paths[0]))
        text, error = read_utf8(path)
        if error is not None or text is None:
            omissions.append(f"{source}: {error or 'unreadable'}")
        else:
            for title in SESSION_SECTIONS:
                add_section(
                    selected, full_sources, section_refs, omissions, source, text, title
                )

    if intent in {"reading", "talk"}:
        reading_logs = [path for path in session_paths if READING_LOG_RE.search(path.stem)]
        found = False
        for candidate in reading_logs:
            path, source = resolve_source(vault, str(candidate))
            text, error = read_utf8(path)
            if error is None and text is not None and add_section(
                selected,
                full_sources,
                section_refs,
                [],
                source,
                text,
                READING_CAPSULE,
            ):
                found = True
                break
        if not found:
            omissions.append(f"{sessions}/#Reading Capsule: no non-empty reading capsule")

    for spec in source_specs:
        raw_path, requested_section = split_source_spec(vault, spec)
        path, source = resolve_source(vault, raw_path)
        if source in {f"profile/{name}" for name in profile_reads} and path != (ROOT / source).resolve():
            raise BundleError(f"explicit source conflicts with required profile: {source}")
        text, error = read_utf8(path)
        if error is not None or text is None:
            omissions.append(f"{source}{'#' + requested_section if requested_section else ''}: {error}")
        elif requested_section is not None:
            add_section(
                selected,
                full_sources,
                section_refs,
                omissions,
                source,
                text,
                requested_section,
            )
        elif text.strip():
            selected[source] = text
            full_sources.add(source)
        else:
            omissions.append(f"{source}: empty")

    def joined(values: Sequence[str]) -> str:
        return "; ".join(values) if values else "none"

    header = "\n".join(
        (
            "Atelier routed context selection.",
            f"Intent: {intent}",
            f"Effective date: {effective_date}",
            f"Token ceiling: {budget} ({TOKEN_ENCODING})",
            f"Selected current sources: {joined(list(selected))}",
            f"Selected sections: {joined(section_refs)}",
            f"Profile freshness: {joined(profile_status)}",
            f"Omissions: {joined(omissions)}",
        )
    )
    return selected, header, warnings, budget


def repomix_config(header: str) -> dict[str, Any]:
    """Explicit inert config: no ambient JS config or processors are loaded."""
    return {
        "output": {
            "style": "xml",
            "filePathStyle": "target-relative",
            "parsableStyle": True,
            "compress": False,
            "headerText": header,
            "topFilesLength": 0,
            "git": {"sortByChanges": False, "includeDiffs": False, "includeLogs": False},
        },
        "include": ["**/*"],
        "ignore": {
            "useGitignore": False,
            "useDotIgnore": False,
            "useDefaultPatterns": False,
            "customPatterns": [],
        },
        "security": {"enableSecurityCheck": True},
        "tokenCount": {"encoding": TOKEN_ENCODING},
    }


def repomix_environment(config_home: Path) -> dict[str, str]:
    """Return the minimal platform environment needed by the pinned CLI."""
    environment = {
        name: os.environ[name]
        for name in REPOMIX_ENV_PASSTHROUGH
        if os.environ.get(name)
    }
    environment.update(
        XDG_CONFIG_HOME=str(config_home), NO_COLOR="1", FORCE_COLOR="0"
    )
    return environment


def verify_repomix(binary: Path, *, cwd: Path, environment: dict[str, str]) -> None:
    if not binary.is_file():
        raise BundleError(f"Repomix unavailable at {binary}; run npm ci")
    try:
        result = _node.run([binary, "--version"], cwd=cwd, env=environment, timeout=15)
    except _node.NodeError as exc:
        raise BundleError(f"cannot execute Repomix: {exc}") from exc
    if result.returncode != 0 or result.stdout.strip() != REPOMIX_VERSION:
        observed = result.stdout.strip() or "unavailable"
        raise BundleError(
            f"Repomix version mismatch: expected {REPOMIX_VERSION}, got {observed}"
        )


def validate_artifact(artifact: str, expected_paths: Sequence[str]) -> None:
    try:
        root = ET.fromstring(f"<atelier_context>{artifact}</atelier_context>")
    except ET.ParseError as exc:
        raise BundleError(f"Repomix returned invalid parsable XML: {exc}") from exc
    packed = [element.attrib.get("path", "") for element in root.findall(".//file")]
    expected = list(expected_paths)
    missing = sorted(path for path in expected if packed.count(path) != 1)
    unexpected = sorted(path for path in packed if path not in expected)
    if missing or unexpected or len(packed) != len(expected):
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing[:5]))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected[:5]))
        raise BundleError(
            "Repomix artifact differs from staged allowlist ("
            + "; ".join(details or ["duplicate paths"])
            + ")"
        )


def pack_context(
    selected: dict[str, str], header: str, token_budget: int, binary: Path = REPOMIX_BIN
) -> str:
    with tempfile.TemporaryDirectory(prefix="atelier-context-") as temporary:
        workspace = Path(temporary)
        input_root = workspace / "selected"
        config_home = workspace / "config-home"
        input_root.mkdir()
        config_home.mkdir()
        for source, text in selected.items():
            target = input_root / source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        config_path = workspace / "controlled-repomix-config.json"
        config_path.write_text(
            json.dumps(repomix_config(header), indent=2) + "\n", encoding="utf-8"
        )
        environment = repomix_environment(config_home)
        verify_repomix(binary, cwd=workspace, environment=environment)
        try:
            result = _node.run(
                [
                    binary,
                    "--stdout",
                    "--config",
                    str(config_path),
                    "--token-budget",
                    str(token_budget),
                    "--token-count-encoding",
                    TOKEN_ENCODING,
                    "--no-git-sort-by-changes",
                    str(input_root),
                ],
                cwd=workspace,
                env=environment,
                timeout=120,
            )
        except _node.NodeError as exc:
            raise BundleError(f"Repomix failed: {exc}") from exc
        if result.returncode != 0:
            detail = next(
                (line.strip() for line in result.stderr.splitlines() if line.strip()),
                "unknown error",
            )
            # Repomix validates --token-budget after writing --stdout. In that
            # mode logging is suppressed, so overflow can have output and no
            # diagnostic; captured output is deliberately discarded here.
            if result.stdout.strip() or "exceeds the token budget" in result.stderr:
                raise BundleError(f"Repomix token ceiling exceeded: {detail}")
            raise BundleError(f"Repomix failed: {detail}")
        if not result.stdout.strip():
            raise BundleError("Repomix returned an empty context artifact")
        validate_artifact(result.stdout, list(selected))
        return result.stdout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Select routed context into an isolated allowlist and emit pinned "
            "Repomix XML; the route token ceiling is a hard error."
        )
    )
    parser.add_argument("--intent", required=True, help="selected intent key")
    parser.add_argument("--vault", help="OV vault root (default: $OV)")
    parser.add_argument(
        "--source", action="append", default=[], metavar="PATH[#SECTION]"
    )
    parser.add_argument("--effective-date", help="YYYY-MM-DD; default uses 03:00 boundary")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        selected, header, warnings, budget = select_context(
            vault=resolve_vault(args.vault),
            intent_arg=args.intent,
            source_specs=args.source,
            effective_date=parse_effective_date(args.effective_date),
        )
        artifact = pack_context(selected, header, budget)
    except BundleError as exc:
        parser.error(str(exc))
    for warning in warnings:
        sys.stderr.write(f"context_bundle: warning: {warning}\n")
    sys.stdout.write(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
