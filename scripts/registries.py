#!/usr/bin/env python3
"""Shared validated loaders for skills, agents, models, and intents.

Production consumers receive one ``RegistryError`` type and retain their own
edge policy. Lint keeps an independent parse; runtime launch configuration
remains ``atelier_runtime``'s responsibility.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RegistryError(RuntimeError):
    """A harness registry is missing, unparsable, or the wrong shape."""


def _load_table(filename: str, table: str, root: Path | None = None) -> dict:
    path = (root or ROOT) / "harness" / filename
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise RegistryError(f"{filename}: unreadable: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{filename}: parse error: {exc}") from exc
    value = data.get(table)
    if not isinstance(value, dict):
        raise RegistryError(f"{filename}: missing [{table}] table")
    return value


def load_skills(root: Path | None = None) -> dict[str, dict]:
    return {k: v for k, v in _load_table("skills.toml", "skills", root).items() if isinstance(v, dict)}


def load_agents(root: Path | None = None) -> dict[str, dict]:
    return {k: v for k, v in _load_table("agents.toml", "agents", root).items() if isinstance(v, dict)}


def load_intents(root: Path | None = None) -> dict[str, dict]:
    return {k: v for k, v in _load_table("intents.toml", "intents", root).items() if isinstance(v, dict)}


def load_models(root: Path | None = None) -> dict[str, dict]:
    return {k: v for k, v in _load_table("models.toml", "models", root).items() if isinstance(v, dict)}


if __name__ == "__main__":
    for name, loader in (
        ("skills", load_skills),
        ("agents", load_agents),
        ("intents", load_intents),
        ("models", load_models),
    ):
        try:
            print(f"{name}: {len(loader())} entries")
        except RegistryError as exc:
            print(f"{name}: ERROR {exc}", file=sys.stderr)
            sys.exit(1)
