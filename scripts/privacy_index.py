#!/usr/bin/env python3
"""privacy_index.py: derive the private-entity index the privacy gate scans with.

A hand-written term list does not scale; the vault already knows every name
that must never reach a public commit. This builds `$OV/_meta/privacy_index.json`
from those sources, with provenance per term, so `scripts/privacy_check.py` can
match against it and explain any hit (`why`).

Sources (kind → what is indexed):
  dir          every directory name and vault-relative directory path under the
               content tiers (public tier segments from harness/paths.toml are
               never terms; `paths` feed the path-shape rule)
  stem         multi-word note filename stems (the historical source)
  wikilink     `[[targets]]` in vault content (the historical source)
  registry     routine names, labels, and output dirs from `_meta/routine_watch.toml`;
               names from `_meta/digest_updates.toml`; private feature directory
               names; private rows in the repo's gitignored `intents.local.toml`
  frontmatter  `title`, `aliases`, `people`, `org`, `company`, `employer`,
               `project`, `client` values in note frontmatter
Profile prose is deliberately not a source: its bold spans are labels, and
identity leaks from it are the semantic reviewer's job.

Specificity filter (why a generic word does not become a term): public tier
segments, dates, numbers, and plain single words are never terms (a
single-word codename belongs in `profile/private_slugs.txt`); hyphenated
compounds, multi-word phrases, and non-ASCII names are. The path rule fires
only on a path segment that looks like a name (a compound, or a word not in
`/usr/share/dict/words`), so `gtd/decisions` in a public procedure is schema,
while `research/<topic-name>` is taxonomy. `scripts/privacy_allowlist.txt`
remains the only opt-out for a deliberately public literal.

CLI:
    uv run scripts/privacy_index.py build [--force]      write the index
    uv run scripts/privacy_index.py why "<term>"          provenance or the reason it is not indexed
    uv run scripts/privacy_index.py stats                 counts by kind
The gate rebuilds a missing or day-old index by itself; `build` is for
inspection and for the routine that keeps it fresh.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import privacy_check as pc  # noqa: E402
from _paths import atomic_write  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_NAME = "privacy_index.json"
DICTIONARY = Path("/usr/share/dict/words")
# Tiers whose subdirectories are the atelier's own schema (dated runs, decayed
# archives, prompt archives), not the user's taxonomy: no path rule there.
SYSTEM_TIERS = {"meta", "routine_prompts", "cache", "inbox", "agent_findings", "zettelm", "sessions", "archive", "private_features"}
# Tiers whose direct children are the user's own taxonomy (a topic, a person,
# a project): a single plain word there is still a name because the tier
# prefix makes the path distinctive. Elsewhere (`travel/trips`,
# `health/nutrition`) a single dictionary word is schema.
TAXONOMY_TIERS = {"research", "people", "work", "career", "talent", "projects"}
# Directory names that are landing-zone schema in every tier, never a name.
SCHEMA_DIRS = {"raw", "images", "assets", "attachments", "templates", "scripts", "archive", "cache"}
FRONTMATTER_KEYS = ("title", "aliases", "alias", "people", "person", "org", "company", "employer", "project", "client")
MAX_DIR_DEPTH = 4
MIN_TOKEN_LEN = 6
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)

_dictionary: set[str] | None = None


def dictionary() -> set[str]:
    global _dictionary
    if _dictionary is None:
        try:
            _dictionary = {w.strip().casefold() for w in DICTIONARY.read_text(encoding="utf-8", errors="ignore").splitlines() if w.strip()}
        except OSError:
            _dictionary = set()
    return _dictionary


def public_segments() -> set[str]:
    """Every path and path segment harness/paths.toml declares; public by design."""
    out: set[str] = set()
    try:
        data = tomllib.loads((REPO_ROOT / "harness" / "paths.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return out
    def add(value: object) -> None:
        if isinstance(value, dict):
            for v in value.values():
                add(v)
        elif isinstance(value, str) and value.strip():
            parts = value.strip("/").split("/")
            for i in range(1, len(parts) + 1):
                out.add("/".join(parts[:i]))
            out.update(parts)
    add(data.get("paths", {}))
    return out


_public_vocab: set[str] | None = None


def public_vocabulary() -> set[str]:
    """Names the public harness already uses: command, agent, intent, and file
    stems plus registry description text. A private registry entry that merely
    repeats one of these (a routine named after the command it runs) is not a
    leak."""
    global _public_vocab
    if _public_vocab is not None:
        return _public_vocab
    vocab: set[str] = set()
    for name in ("commands", "agents", "intents"):
        try:
            data = tomllib.loads((REPO_ROOT / "harness" / f"{name}.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for key, row in data.get(name, {}).items():
            vocab.add(str(key).casefold())
            if isinstance(row, dict):
                for field in ("description", "label", "codex_prompt"):
                    text = row.get(field)
                    if isinstance(text, str):
                        vocab.add(text.casefold())
    for pattern in ("protocols/*.md", ".claude/commands/*.md", ".claude/agents/*.md", "scripts/*.py", "scripts/launchd/*.plist"):
        for f in REPO_ROOT.glob(pattern):
            stem = f.stem
            vocab.add(stem.casefold())
            if stem.startswith("com.atelier."):
                vocab.add(stem[len("com.atelier."):].casefold())
    _public_vocab = vocab
    return vocab


def is_public_vocabulary(term: str) -> bool:
    folded = term.strip().casefold()
    vocab = public_vocabulary()
    if folded in vocab:
        return True
    spaced = folded.replace("-", " ").replace("_", " ")
    return any(len(v) > 12 and (spaced in v or folded in v) for v in vocab)


def canonical_tiers() -> dict[str, str]:
    try:
        data = tomllib.loads((REPO_ROOT / "harness" / "paths.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {k: v for k, v in data.get("paths", {}).items() if isinstance(v, str)}


def is_dictionary_word(token: str) -> bool:
    """Plain English, including a trailing plural (`reports`, `decisions`)."""
    words = dictionary()
    if not words:
        return False
    w = token.casefold()
    return w in words or (w.endswith("es") and w[:-2] in words) or (w.endswith("s") and w[:-1] in words)


def _alpha_parts(text: str) -> list[str]:
    """Alphabetic parts longer than two letters; `ml`, `(2)`, `26` carry no signal."""
    return [w for w in re.split(r"[\s_-]+", text) if len(re.sub(r"[^A-Za-z]", "", w)) > 2]


def is_name_like(segment: str, *, public: set[str], direct_child: bool = True) -> bool:
    """Does one path segment look like a name (taxonomy) rather than schema?

    A direct child of a content tier (`research/<topic>`) is the user's
    taxonomy, whatever the word: the tier prefix makes the path distinctive.
    Deeper segments are schema unless some part is not a dictionary word.
    """
    seg = segment.strip()
    if not seg or seg.casefold() in {p.casefold() for p in public} or seg.casefold() in pc._INFRA_DIRS or seg.casefold() in SCHEMA_DIRS:
        return False
    if pc._DATE_RE.match(seg) or pc._NUMERIC_RE.match(seg):
        return False
    if not seg.isascii():
        return sum(1 for c in seg if ord(c) > 127) >= 2
    parts = _alpha_parts(seg)
    if not parts:
        return False
    if all(w.casefold() in {p.casefold() for p in public} for w in parts):
        return False
    if direct_child:
        return True
    if len(parts) >= 2:
        return not all(is_dictionary_word(w) for w in parts)
    return len(parts[0]) >= MIN_TOKEN_LEN and not is_dictionary_word(parts[0])


def is_specific(term: str, *, public: set[str], allowlist: set[str], strict: bool = True) -> bool:
    """Would this token identify the user's private world rather than any vault?

    Plain single ASCII words are never terms: the historical stem and wikilink
    rules skip them too, and `profile/private_slugs.txt` exists for the rare
    single-word codename. Compounds, phrases, and non-ASCII names are; with
    `strict` (directory names) a compound of plain dictionary words such as
    `audit-log` is schema-shaped and left to the path rule. Registry and
    frontmatter values are explicit declarations, so they are not strict.
    """
    t = term.strip()
    if not t or t in pc.SKIP_STEMS or pc._is_allowlisted(t, allowlist):
        return False
    if t.casefold() in {p.casefold() for p in public} or t.casefold() in pc._INFRA_DIRS:
        return False
    if pc._DATE_RE.match(t) or pc._NUMERIC_RE.match(t) or pc._NOISE_RE.match(t):
        return False
    if not t.isascii():
        return sum(1 for c in t if ord(c) > 127) >= 3
    words = _alpha_parts(t)
    if len(words) >= 2:
        if all(w.casefold() in {p.casefold() for p in public} for w in words):
            return False
        return not strict or not all(is_dictionary_word(w) for w in words)
    return False


class Index:
    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self.terms: dict[str, dict[str, Any]] = {}
        self.paths: set[str] = set()

    def add(self, term: str, kind: str, source: str) -> None:
        entry = self.terms.setdefault(term, {"kinds": [], "sources": []})
        if kind not in entry["kinds"]:
            entry["kinds"].append(kind)
        if len(entry["sources"]) < 5 and source not in entry["sources"]:
            entry["sources"].append(source)

    def to_json(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self.terms.values():
            for kind in entry["kinds"]:
                counts[kind] = counts.get(kind, 0) + 1
        return {
            "schema": 1,
            "built": datetime.now().isoformat(timespec="seconds"),
            "vault": str(self.vault),
            "counts": {**counts, "terms": len(self.terms), "paths": len(self.paths)},
            "terms": dict(sorted(self.terms.items())),
            "paths": sorted(self.paths),
        }


def _rel(vault: Path, path: Path) -> str:
    return path.relative_to(vault).as_posix()


def tier_of(rel: str, public: set[str]) -> tuple[str | None, bool]:
    """(longest public prefix, whether `rel` is its direct child)."""
    parts = rel.strip("/").split("/")
    longest = 0
    for i in range(1, len(parts)):
        if "/".join(parts[:i]) in public:
            longest = i
    prefix = "/".join(parts[:longest]) if longest else None
    return prefix, len(parts) == longest + 1


def _taxonomy_prefixes() -> set[str]:
    tiers = canonical_tiers()
    return {tiers[k].strip("/") for k in TAXONOMY_TIERS if k in tiers}


def is_direct_child_of_tier(rel: str, public: set[str]) -> bool:
    """A child of a taxonomy tier is a name whatever its word; a child of any
    other tier (or of the vault root) is a name only when it is a compound."""
    prefix, direct = tier_of(rel, public)
    if not direct or prefix is None:
        return False
    if prefix in _taxonomy_prefixes():
        return True
    return len(_alpha_parts(rel.rsplit("/", 1)[-1])) >= 2


def index_dirs(idx: Index, *, public: set[str], allowlist: set[str]) -> None:
    tiers = canonical_tiers()
    system_roots = {tiers[k].strip("/") for k in SYSTEM_TIERS if k in tiers}
    for root, dirnames, _files in os.walk(idx.vault):
        rel_root = _rel(idx.vault, Path(root)) if Path(root) != idx.vault else ""
        depth = 0 if not rel_root else rel_root.count("/") + 1
        dirnames[:] = [
            d for d in sorted(dirnames)
            if not d.startswith(".") and d not in pc._INFRA_DIRS and d != "node_modules"
        ]
        if depth >= MAX_DIR_DEPTH:
            dirnames[:] = []
            continue
        for d in dirnames:
            rel = f"{rel_root}/{d}" if rel_root else d
            top = rel.split("/", 1)[0]
            if top in system_roots or any(rel == s or rel.startswith(s + "/") for s in system_roots):
                continue
            if rel in public:
                continue
            if is_name_like(d, public=public, direct_child=is_direct_child_of_tier(rel, public)):
                idx.paths.add(rel)
            if is_specific(d, public=public, allowlist=allowlist):
                idx.add(d, "dir", rel)
                for variant in pc._separator_variants(d):
                    if variant != d and is_specific(variant, public=public, allowlist=allowlist):
                        idx.add(variant, "dir", rel)


def index_stems_and_links(idx: Index, *, allowlist: set[str]) -> None:
    dirs = pc._discover_private_dirs(idx.vault)
    for title in pc.collect_titles(idx.vault, allowlist, dirs):
        idx.add(title, "stem", "note filename")
    for target in pc.collect_wikilinks(idx.vault, allowlist, dirs):
        idx.add(target, "wikilink", "[[target]] in vault content")


def _walk_strings(value: Any, keys: tuple[str, ...], path: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            found.extend(_walk_strings(v, keys, f"{path}.{k}" if path else str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found.extend(_walk_strings(v, keys, f"{path}[{i}]"))
    elif isinstance(value, str):
        leaf = path.rsplit(".", 1)[-1].split("[", 1)[0]
        if leaf in keys and value.strip():
            found.append((value.strip(), path))
    return found


def index_registries(idx: Index, *, public: set[str], allowlist: set[str]) -> None:
    meta = idx.vault / canonical_tiers().get("meta", "_meta")
    watch = meta / "routine_watch.toml"
    if watch.is_file():
        try:
            data = tomllib.loads(watch.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        for routine in data.get("routine", []) if isinstance(data.get("routine"), list) else []:
            if not isinstance(routine, dict):
                continue
            for key in ("name", "label"):
                value = str(routine.get(key, "")).strip()
                if value and is_specific(value, public=public, allowlist=allowlist, strict=False) and not is_public_vocabulary(value):
                    idx.add(value, "registry", f"routine_watch.toml {key}")
            out_dir = str(routine.get("output_dir", "")).strip("/")
            if out_dir:
                parts = out_dir.split("/")
                for i in range(1, len(parts) + 1):
                    rel = "/".join(parts[:i])
                    if rel not in public and is_name_like(parts[i - 1], public=public, direct_child=is_direct_child_of_tier(rel, public)):
                        idx.paths.add(rel)
                for part in parts:
                    # Path parts are directory names: the strict dir rule applies.
                    if is_specific(part, public=public, allowlist=allowlist) and not is_public_vocabulary(part):
                        idx.add(part, "registry", f"routine_watch.toml output_dir {out_dir}")
    ledger = meta / "digest_updates.toml"
    if ledger.is_file():
        try:
            data = tomllib.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        for value, where in _walk_strings(data, ("name", "label", "title", "path", "source")):
            if is_specific(value, public=public, allowlist=allowlist, strict=False) and not is_public_vocabulary(value):
                idx.add(value, "registry", f"digest_updates.toml {where}")
    features = idx.vault / canonical_tiers().get("private_features", "_tools/features")
    if features.is_dir():
        for child in sorted(features.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and is_specific(child.name, public=public, allowlist=allowlist, strict=False):
                idx.add(child.name, "registry", "private feature directory")
    overlay = REPO_ROOT / "harness" / "intents.local.toml"
    if overlay.is_file():
        try:
            local = tomllib.loads(overlay.read_text(encoding="utf-8")).get("intents", {})
            canonical = tomllib.loads((REPO_ROOT / "harness" / "intents.toml").read_text(encoding="utf-8")).get("intents", {})
        except (OSError, tomllib.TOMLDecodeError):
            local, canonical = {}, {}
        for name, row in (local.items() if isinstance(local, dict) else []):
            if name not in canonical and is_specific(name, public=public, allowlist=allowlist, strict=False):
                idx.add(name, "registry", "intents.local.toml private row")


def _frontmatter_values(text: str) -> list[tuple[str, str]]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return []
    out: list[tuple[str, str]] = []
    current: str | None = None
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if km:
            key, value = km.group(1), km.group(2).strip()
            current = key if key in FRONTMATTER_KEYS else None
            if current and value:
                if value.startswith("[") and value.endswith("]"):
                    for item in value[1:-1].split(","):
                        out.append((item.strip().strip("'\""), key))
                else:
                    out.append((value.strip("'\""), key))
            continue
        lm = re.match(r"^\s*-\s+(.+)$", line)
        if lm and current:
            out.append((lm.group(1).strip().strip("'\""), current))
    return out


def index_frontmatter(idx: Index, *, public: set[str], allowlist: set[str]) -> None:
    for sub in pc._discover_private_dirs(idx.vault):
        for f in (idx.vault / sub).rglob("*.md"):
            try:
                head = f.read_text(encoding="utf-8", errors="ignore")[:4096]
            except OSError:
                continue
            for value, key in _frontmatter_values(head):
                if is_specific(value, public=public, allowlist=allowlist, strict=False) and pc._is_private_wikilink(value):
                    idx.add(value, "frontmatter", f"{key}: in {_rel(idx.vault, f)}")


def build(vault: Path, allowlist: set[str] | None = None) -> dict[str, Any]:
    allowlist = pc.load_allowlist() if allowlist is None else allowlist
    public = public_segments()
    idx = Index(vault)
    index_dirs(idx, public=public, allowlist=allowlist)
    index_stems_and_links(idx, allowlist=allowlist)
    index_registries(idx, public=public, allowlist=allowlist)
    index_frontmatter(idx, public=public, allowlist=allowlist)
    return idx.to_json()


def index_path(vault: Path) -> Path:
    return vault / canonical_tiers().get("meta", "_meta") / INDEX_NAME


def load_or_build(vault: Path, *, max_age_hours: float = 24, force: bool = False, allowlist: set[str] | None = None) -> dict[str, Any]:
    path = index_path(vault)
    if not force and path.is_file():
        try:
            age_hours = (time.time() - path.stat().st_mtime) / 3600
            if age_hours <= max_age_hours:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("schema") == 1 and data.get("vault") == str(vault):
                    return data
        except (OSError, json.JSONDecodeError):
            pass
    data = build(vault, allowlist)
    try:
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    except OSError as exc:
        sys.stderr.write(f"privacy_index: could not write {path}: {exc}\n")
    return data


def explain(data: dict[str, Any], term: str) -> dict[str, Any]:
    folded = term.strip().casefold()
    for key, entry in data.get("terms", {}).items():
        if key.casefold() == folded:
            return {"term": key, "indexed": True, **entry}
    reasons = []
    if folded in {p.casefold() for p in public_segments()}:
        reasons.append("public tier segment declared in harness/paths.toml")
    if term.isascii() and " " not in term.strip() and "-" not in term and "_" not in term:
        reasons.append("plain single word: never a term (use profile/private_slugs.txt for a codename); the path rule still covers it inside a private path")
        if is_dictionary_word(term.strip()):
            reasons.append("dictionary word, so the path rule treats it as schema, not a name")
    if pc._is_allowlisted(term, pc.load_allowlist()):
        reasons.append("allowlisted in scripts/privacy_allowlist.txt")
    covering = [p for p in data.get("paths", []) if p.casefold() == folded or p.casefold().rsplit("/", 1)[-1] == folded]
    if covering:
        reasons.append(f"covered by the path-shape rule as {covering[0]}" + (f" (+{len(covering) - 1} more)" if len(covering) > 1 else ""))
    return {"term": term, "indexed": False, "reasons": reasons or ["no vault source produced it; add it to profile/private_terms.txt if it is private"]}


def _vault() -> Path:
    raw = os.environ.get("OV")
    if not raw:
        raise SystemExit("privacy_index: $OV is not set")
    return Path(raw).expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--force", action="store_true")
    b.add_argument("--json", action="store_true")
    w = sub.add_parser("why")
    w.add_argument("term")
    sub.add_parser("stats")
    args = parser.parse_args(argv)
    vault = _vault()
    if args.command == "build":
        data = load_or_build(vault, force=True)
        if args.json:
            print(json.dumps(data["counts"], ensure_ascii=False))
        else:
            print(f"privacy_index: {data['counts']['terms']} terms, {data['counts']['paths']} paths -> {index_path(vault)}")
        return 0
    data = load_or_build(vault)
    if args.command == "why":
        print(json.dumps(explain(data, args.term), ensure_ascii=False, indent=1))
        return 0
    print(json.dumps({"built": data.get("built"), **data.get("counts", {})}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
