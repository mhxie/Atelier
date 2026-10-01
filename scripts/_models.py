"""Resolve a model identity to its provider bindings.

Committed schema in `harness/models.toml` declares identities and their
runtime-neutral reasoning tier; gitignored `profile/models.toml` supplies
provider ids, endpoints, env vars, and request extras. This module is the one
place that merges them, so callers never re-implement the overlay.
"""
from __future__ import annotations

from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_TOML = ROOT / "harness" / "models.toml"
BINDINGS_TOML = ROOT / "profile" / "models.toml"


class ModelError(RuntimeError):
    """The identity is absent from the committed schema, or a binding is missing."""


def _table(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def resolve(name: str) -> dict | None:
    """Schema entry for `name` with bindings overlaid, or None when undeclared.

    A schema entry may legitimately be an empty table, so presence is decided by
    the key, not by truthiness. A missing bindings file yields the schema-only
    entry, which fails downstream where a binding is actually required.
    """
    schema = _table(SCHEMA_TOML)
    models = schema.get("models") or {}
    if name not in models:
        return None
    merged = dict(models.get(name) or {})
    merged.update((_table(BINDINGS_TOML).get("models", {}) or {}).get(name) or {})
    return merged


def codex_binding(name: str) -> tuple[str, str]:
    """(provider model id, reasoning effort) for a Codex leg. Either may be "".

    Raises ModelError for an identity that is not declared, so a typo in a
    scheduled routine fails at configuration load instead of at run time.
    """
    entry = resolve(name)
    if entry is None:
        raise ModelError(f"model {name!r} is missing from harness/models.toml")
    return str(entry.get("codex") or ""), str(entry.get("codex_reasoning_effort") or "")


def _id(name: str, key: str) -> str:
    """Bound provider model id under `key`, or "" when unbound."""
    entry = resolve(name)
    if entry is None:
        raise ModelError(f"model {name!r} is missing from harness/models.toml")
    return str(entry.get(key) or "")


def claude_binding(name: str) -> str:
    """Claude Code model id for a Claude leg, or "" when unbound."""
    return _id(name, "claude_code")


def agy_binding(name: str) -> str:
    """Antigravity CLI model id for an agy leg, or "" when unbound."""
    return _id(name, "agy")
