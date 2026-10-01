#!/usr/bin/env python3
"""Local QMD search. The vault is read-only; all derived state is machine-local."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from datetime import date, datetime, time as daytime
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import raw_store, tier_segments, vault_root  # noqa: E402
import _node  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCOPES = ("active", "raw", "archive", "inbox", "process")
SECURE = "secure"  # QMD collection of raw-store secure notes; results report it as `active`
CONFIG_PATH = ROOT / "semantic.toml"
HARD_DIRS = ("cache", "_meta", "_routine_prompts", "_tools", "node_modules", ".venv", "__pycache__")
STAGED_MODEL_DIRECTORY_ENV = "ATELIER_QMD_MODEL_DIRECTORY"
_QUERY_STATE_FILES = ("index.sqlite", "index.yml")
_QUERY_SIDECARS = ("index.sqlite-wal", "index.sqlite-shm")


class SearchError(RuntimeError):
    """An unavailable or invalid retrieval result, never a successful empty query."""


def settings() -> dict:
    with (ROOT / "harness/retrieval.toml").open("rb") as handle:
        defaults = tomllib.load(handle)
    local = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.is_file() else {}
    if "embedding" in local:
        raise SearchError("legacy semantic.toml [embedding] settings are unsupported; use semantic.toml.example")
    if set(local) - {"profile", "runtime", "models"}:
        raise SearchError("unknown semantic.toml setting; use semantic.toml.example")
    name = os.environ.get("ATELIER_QMD_PROFILE", local.get("profile", defaults["default_profile"]))
    if not isinstance(name, str) or name not in defaults["profiles"]:
        raise SearchError(f"unknown QMD hardware profile: {name}")
    preset = defaults["profiles"][name]
    if not isinstance(local.get("runtime", {}), dict) or not isinstance(local.get("models", {}), dict):
        raise SearchError("QMD runtime and models must be TOML tables")
    if set(local.get("runtime", {})) - (set(preset) - {"models"}) or set(local.get("models", {})) - set(defaults["models"]):
        raise SearchError("unknown QMD runtime or model setting")
    runtime = {**{key: value for key, value in preset.items() if key != "models"}, **local.get("runtime", {})}
    for key, value in runtime.items():
        if key == "gpu":
            if value not in ("auto", "metal", "cpu"):
                raise SearchError("QMD gpu must be auto, metal, or cpu")
        elif type(value) is not int or value < 1:
            raise SearchError(f"QMD {key} must be a positive integer")
    if not 1 <= runtime["parallelism"] <= 8 or not 1 <= runtime["candidate_limit"] <= 200:
        raise SearchError("QMD parallelism must be 1..8 and candidate_limit must be 1..200")
    models = {**defaults["models"], **preset.get("models", {}), **local.get("models", {})}
    if any(not isinstance(value, str) or not value.startswith("hf:") or not value.endswith(".gguf")
           for value in models.values()):
        raise SearchError("QMD model configuration needs explicit hf: GGUF URIs")
    return {"profile": name, "runtime": runtime, "models": models}


def cache_root() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return Path(os.environ.get("ATELIER_QMD_HOME", str(base / "atelier" / "qmd"))).expanduser().resolve()


def state_dir(vault: Path) -> Path:
    cache = cache_root()
    if cache.is_relative_to(vault) or cache.is_relative_to(ROOT):
        raise SearchError("QMD cache must be outside both the canonical vault and the public repository")
    if any((cache / name).is_symlink() for name in ("assets", "assets/qmd", "assets/qmd/models")):
        raise SearchError("QMD model cache must not contain symlinked directories")
    cfg = settings()
    identity = [str(vault), cfg["models"]["embed"], cfg["runtime"]["embed_context_tokens"]]
    directory = cache / hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]
    derived = ("index.sqlite", "index.sqlite-wal", "index.sqlite-shm", "index.yml", "index.yml.pending")
    if directory.is_symlink() or any((directory / name).is_symlink() for name in derived):
        raise SearchError("QMD derived state must not contain symlinks")
    return directory


def relative_path(value: str, vault: Path) -> str:
    path = Path(value).expanduser()
    absolute = (vault / path).resolve() if not path.is_absolute() else path.resolve()
    if not absolute.is_relative_to(vault):
        store = raw_store()
        if store is None or not absolute.is_relative_to(store):
            raise SearchError("path must remain inside the canonical vault")
        return absolute.relative_to(store).as_posix()
    return absolute.relative_to(vault).as_posix()


def zones(vault: Path) -> dict[str, str]:
    registry = tier_segments()
    names = {"archive": "archive", "process": "sessions", "meta": "_meta",
             "routine_prompts": "_routine_prompts", "private_components": "_tools"}
    return {name: relative_path(registry.get(name if name != "process" else "sessions", default), vault)
            for name, default in names.items()}


def scope_for(path: str, vault: Path) -> str | None:
    parts = PurePosixPath(path).parts
    if not parts or ".." in parts or PurePosixPath(path).is_absolute():
        return None
    zone = zones(vault)
    if any(part.startswith(".") or part in HARD_DIRS for part in parts):
        return None
    if any(path == zone[key] or path.startswith(zone[key] + "/")
           for key in ("meta", "routine_prompts", "private_components")):
        return None
    if path.startswith(zone["archive"] + "/orphan-stubs/"):
        return None
    if "raw" in parts[:-1]:
        return "raw"
    if path.startswith(zone["archive"] + "/"):
        return "archive"
    if path.startswith(zone["process"] + "/"):
        return "process"
    if "inbox" in parts[:-1]:
        return "inbox"
    return "active"


def collection_config(vault: Path) -> dict:
    zone = zones(vault)
    store = raw_store()
    hard = [f"**/{name}/**" for name in HARD_DIRS]
    hard += ["**/.*", "**/.*/**", zone["archive"] + "/orphan-stubs/**"]
    hard += [zone[name] + "/**" for name in ("meta", "routine_prompts", "private_components")]
    archive, process = zone["archive"] + "/**", zone["process"] + "/**"
    authored = ["**/raw/**", "**/inbox/**", archive, process]
    patterns = {
        "active": ("**/*.md", authored),
        "raw": ("**/raw/**/*.{md,txt,text,csv,html,htm}", []),
        "archive": (zone["archive"] + "/**/*.md", ["**/raw/**"]),
        "inbox": ("**/inbox/**/*.md", ["**/raw/**", archive, process]),
        "process": (zone["process"] + "/**/*.md", ["**/raw/**"]),
    }
    if store is not None:  # vault raw/ and secure/ folders are symlinks into this mirror
        patterns[SECURE] = ("**/secure/**/*.md", authored)
    return {
        "models": settings()["models"],
        "collections": {name: {
            "path": str(store if store is not None and name in ("raw", SECURE) else vault),
            "pattern": pattern, "ignore": hard + exclusions, "includeByDefault": name in ("active", SECURE),
        } for name, (pattern, exclusions) in patterns.items()},
    }


def prepare(vault: Path) -> Path:
    if not vault.is_dir():
        raise SearchError("canonical vault directory does not exist")
    if (store := raw_store()) is not None and not store.is_dir():
        raise SearchError("raw_store is not a directory; mount it or unset it before indexing")
    directory = state_dir(vault)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # JSON is a YAML subset; the official QMD CLI can use this same config.
    config = directory / "index.yml"
    pending = directory / "index.yml.pending"
    pending.write_text(json.dumps(collection_config(vault), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(config)
    return directory


def require_index(vault: Path) -> Path:
    directory = state_dir(vault)
    if not (directory / "index.sqlite").is_file():
        raise SearchError("QMD index is absent; run semantic.py init --download-models, then semantic.py index")
    try:
        saved = json.loads((directory / "index.yml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SearchError("QMD collection configuration is unreadable; run semantic.py index") from exc
    if saved != collection_config(vault):
        raise SearchError("QMD source policy changed; run semantic.py index before querying")
    return directory


def _regular_file_snapshot(path: Path) -> tuple[int, int, int, int, int, int, str]:
    """Return stable identity and content for one source file without following links."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SearchError(f"QMD query source is unreadable or not a regular file: {path.name}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SearchError(f"QMD query source is not a regular file: {path.name}")
        digest = hashlib.sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    def metadata(value):
        return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)
    if metadata(before) != metadata(after):
        raise SearchError(f"QMD query source changed while it was being read: {path.name}")
    return (*metadata(after), digest.hexdigest())


def _copy_verified_file(source: Path, target: Path, expected: tuple[int, int, int, int, int, int, str]) -> None:
    """Copy one previously inspected regular file and verify both ends."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(source, flags)
    except OSError as exc:
        raise SearchError(f"QMD query source became unreadable: {source.name}") from exc
    target_descriptor = -1
    digest = hashlib.sha256()
    try:
        source_before = os.fstat(source_descriptor)
        source_metadata = (source_before.st_dev, source_before.st_ino, source_before.st_mode,
                           source_before.st_size, source_before.st_mtime_ns, source_before.st_ctime_ns)
        if not stat.S_ISREG(source_before.st_mode) or source_metadata != expected[:-1]:
            raise SearchError(f"QMD query source changed before it could be copied: {source.name}")
        target_descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        while block := os.read(source_descriptor, 1024 * 1024):
            digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(target_descriptor, view)
                if written <= 0:
                    raise OSError(f"short write while copying {source.name}")
                view = view[written:]
        os.fsync(target_descriptor)
        source_after = os.fstat(source_descriptor)
        source_after_metadata = (source_after.st_dev, source_after.st_ino, source_after.st_mode,
                                 source_after.st_size, source_after.st_mtime_ns, source_after.st_ctime_ns)
        if source_after_metadata != expected[:-1] or digest.hexdigest() != expected[-1]:
            raise SearchError(f"QMD query source changed while it was copied: {source.name}")
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        os.close(source_descriptor)
    copied = _regular_file_snapshot(target)
    if copied[3] != expected[3] or copied[-1] != expected[-1]:
        raise SearchError(f"QMD query copy failed verification: {source.name}")


def _require_no_query_sidecars(directory: Path) -> None:
    present = [name for name in _QUERY_SIDECARS if os.path.lexists(directory / name)]
    if present:
        raise SearchError("QMD index is active or uncheckpointed; retry after these files disappear: "
                          + ", ".join(present))


def _regular_model_directory(source_root: Path) -> Path:
    models = source_root / "assets" / "qmd" / "models"
    try:
        info = models.lstat()
    except OSError as exc:
        raise SearchError("QMD model directory is absent or unreadable; run semantic.py init --download-models") from exc
    if models.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise SearchError("QMD model directory must be a real directory, not a symlink")
    return models.resolve(strict=True)


def prepare_query_copy(vault: Path, destination: Path, *, require_models: bool = True) -> dict[str, str]:
    """Stage a verified quiescent QMD query copy without copying model weights."""
    if STAGED_MODEL_DIRECTORY_ENV in os.environ:
        raise SearchError("cannot prepare a QMD query copy from an already staged environment")
    if not vault.is_dir():
        raise SearchError("canonical vault directory does not exist")
    source_root = cache_root()
    source_directory = state_dir(vault)
    if not os.path.lexists(source_directory / "index.sqlite"):
        raise SearchError("QMD index is absent; run semantic.py init --download-models, then semantic.py index")
    parent = destination.expanduser().absolute().parent.resolve(strict=True)
    if not parent.is_dir():
        raise SearchError("QMD query-copy destination parent is not a directory")
    destination = parent / destination.name
    if os.path.lexists(destination):
        raise SearchError("QMD query-copy destination must not already exist")
    if (destination.is_relative_to(source_root) or destination.is_relative_to(vault.resolve())
            or destination.is_relative_to(ROOT.resolve())):
        raise SearchError("QMD query-copy destination must be isolated from the source, vault, and repository")

    _require_no_query_sidecars(source_directory)
    snapshots = {name: _regular_file_snapshot(source_directory / name) for name in _QUERY_STATE_FILES}
    try:
        saved = json.loads((source_directory / "index.yml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SearchError("QMD collection configuration is unreadable; run semantic.py index") from exc
    if saved != collection_config(vault):
        raise SearchError("QMD source policy changed; run semantic.py index before querying")
    # Parsing is a second read, so re-check that the validated source stayed fixed.
    if _regular_file_snapshot(source_directory / "index.yml") != snapshots["index.yml"]:
        raise SearchError("QMD query source changed while its configuration was validated: index.yml")
    model_directory = source_root / "assets/qmd/models"
    if require_models or os.path.lexists(model_directory):
        model_directory = _regular_model_directory(source_root)
    else:
        model_directory = destination / "assets/qmd/models"

    pending = Path(tempfile.mkdtemp(prefix=f".{destination.name}.pending-", dir=parent))
    try:
        if model_directory.is_relative_to(destination):
            (pending / "assets/qmd/models").mkdir(parents=True)
        target_directory = pending / source_directory.name
        target_directory.mkdir(mode=0o700)
        for name in _QUERY_STATE_FILES:
            _copy_verified_file(source_directory / name, target_directory / name, snapshots[name])
        _require_no_query_sidecars(source_directory)
        for name in _QUERY_STATE_FILES:
            if _regular_file_snapshot(source_directory / name) != snapshots[name]:
                raise SearchError(f"QMD query source changed while the copy was prepared: {name}")
        _require_no_query_sidecars(source_directory)
        os.replace(pending, destination)
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return {
        "ATELIER_QMD_HOME": str(destination),
        STAGED_MODEL_DIRECTORY_ENV: str(model_directory),
    }


def _model_directory(command: str) -> Path:
    staged = os.environ.get(STAGED_MODEL_DIRECTORY_ENV)
    if staged is None:
        return cache_root() / "assets" / "qmd" / "models"
    if command not in {"query", "status"}:
        raise SearchError("staged QMD state is read-only input and cannot be used for init or index")
    if not staged.strip():
        raise SearchError("staged QMD model directory is empty")
    path = Path(staged).expanduser()
    try:
        info = path.lstat()
    except OSError as exc:
        raise SearchError("staged QMD model directory is absent or unreadable") from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise SearchError("staged QMD model directory must be a real directory, not a symlink")
    return path.resolve(strict=True)


@contextlib.contextmanager
def _staged_environment(overrides: dict[str, str]):
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def child_environment() -> dict[str, str]:
    # Node startup hooks and ambient QMD overrides must not run before our bridge.
    return _node.system_env(
        passthrough=("HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "SystemRoot")
    )


def bridge(vault: Path, command: str, *, roles: list[str] | None = None, **options):
    directory = state_dir(vault)
    model_directory = _model_directory(command)
    if not (ROOT / "node_modules" / "@tobilu" / "qmd" / "package.json").is_file():
        raise SearchError("QMD dependency unavailable; install Node >=22 and run npm ci in the repository")
    config = collection_config(vault)
    runtime = settings()["runtime"]
    request = {"command": command, "database": str(directory / "index.sqlite"), "config": config,
               "runtime": runtime, "roles": roles or [],
               "modelDirectory": str(model_directory), **options}
    env = dict(child_environment(), XDG_CACHE_HOME=str(cache_root() / "assets"),
               GGML_METAL_NO_RESIDENCY="1", LLAMA_LOG_LEVEL="error", GGML_LOG_LEVEL="error")
    env.update(QMD_EMBED_PARALLELISM=str(runtime["parallelism"]),
               QMD_EMBED_CONTEXT_SIZE=str(runtime["embed_context_tokens"]),
               QMD_RERANK_CONTEXT_SIZE=str(runtime["rerank_context_tokens"]),
               QMD_EXPAND_CONTEXT_SIZE=str(runtime["expansion_context_tokens"]),
               QMD_LLAMA_GPU="false" if runtime["gpu"] == "cpu" else "" if runtime["gpu"] == "auto" else runtime["gpu"],
               QMD_FORCE_CPU="1" if runtime["gpu"] == "cpu" else "0")
    try:
        result = _node.run(
            [ROOT / "scripts" / "qmd.mjs"], input=json.dumps(request), cwd=ROOT, env=env,
            timeout=runtime["index_timeout_seconds"] if command == "index" else runtime["query_timeout_seconds"],
        )
    except _node.NodeError as exc:
        raise SearchError(f"QMD {command} failed: {exc}") from exc
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode:
        raise SearchError(f"QMD {command} exited {result.returncode}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise SearchError("QMD emitted invalid JSON") from exc


def store_mirror(path: str, vault: Path, store: Path | None) -> bool:
    """Accept one raw or secure folder symlink onto the same path in the raw store, nothing else."""
    if store is None or (vault / path).resolve() != store / path:
        return False
    parts = PurePosixPath(path).parts
    return [part for depth, part in enumerate(parts[:-1], 1)
            if vault.joinpath(*parts[:depth]).is_symlink()] in (["raw"], ["secure"])


def query(args: argparse.Namespace) -> list[dict]:
    vault = vault_root()
    require_index(vault)
    requested = list(SCOPES) if args.scope == "all" else [args.scope]
    store = raw_store()
    prefixes = [relative_path(path, vault) for path in (args.path or [])]
    after = datetime.combine(date.fromisoformat(args.after), daytime.min).timestamp() if args.after else None
    before = datetime.combine(date.fromisoformat(args.before), daytime.max).timestamp() if args.before else None
    if after is not None and before is not None and after > before:
        raise SearchError("--after must not be later than --before")
    roles = [] if args.mode == "lexical" else ["embed"]
    if args.mode == "hybrid" and not args.no_rerank:
        roles.append("rerank")
    if args.expand and args.mode != "hybrid":
        raise SearchError("--expand requires --mode hybrid")
    if args.expand:
        roles.append("generate")
    # Label the pipeline that produced the score, not the one requested: a
    # hybrid row skipping the reranker carries an RRF fusion score instead.
    score_kind = f"{args.mode}-no-rerank" if args.mode == "hybrid" and args.no_rerank else args.mode
    limit = min(200, max(settings()["runtime"]["candidate_limit"], args.top * (4 if prefixes or after or before else 1)))
    collections = requested + ([SECURE] if store is not None and "active" in requested else [])
    rows = bridge(vault, "query", roles=roles, query=args.query, mode=args.mode,
                  collections=collections, limit=limit, rerank=not args.no_rerank, expand=args.expand)
    if not isinstance(rows, list):
        raise SearchError("QMD query did not return a result list")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise SearchError("QMD returned a malformed result")
        path = row["path"]
        actual_scope = scope_for(path, vault)
        collection_scope = "active" if row.get("scope") == SECURE else row.get("scope")
        if actual_scope not in requested or collection_scope != actual_scope:
            continue
        source = vault / path
        if source.is_symlink() or not source.is_file() or (
                source.resolve() != source.absolute() and not store_mirror(path, vault, store)):
            continue
        if source.suffix.lower() != ".md" and actual_scope != "raw":
            continue
        if prefixes and not any(prefix == "." or path == prefix or path.startswith(prefix + "/") for prefix in prefixes):
            continue
        mtime = source.stat().st_mtime
        if after is not None and mtime < after or before is not None and mtime > before:
            continue
        if path in seen:
            continue
        score = row.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise SearchError("QMD returned an invalid score")
        seen.add(path)
        result.append({**row, "scope": actual_scope, "source": "local", "backend": "qmd", "score_kind": score_kind,
                       "representation": "raw_text" if actual_scope == "raw" else "authored"})
        if len(result) == args.top:
            break
    return result


def status(vault: Path) -> dict:
    """Inspect status against staged state so QMD never opens the live DB."""
    def inspect() -> dict:
        require_index(vault)
        result = {**bridge(vault, "status"), "backend": "qmd", "freshness": "unchecked",
                  "profile": settings()["profile"], "runtime": settings()["runtime"]}
        result["ready"] = bool(result["totalDocuments"] and result["hasVectorIndex"]
                               and not result["needsEmbedding"] and result["models_ready"])
        return result

    if STAGED_MODEL_DIRECTORY_ENV in os.environ:
        return inspect()
    with tempfile.TemporaryDirectory(prefix="atelier-qmd-status-") as temporary:
        overrides = prepare_query_copy(vault, Path(temporary) / "qmd", require_models=False)
        with _staged_environment(overrides):
            return inspect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create local QMD config; model download is explicit.")
    init.add_argument("--download-models", action="store_true")
    index = sub.add_parser("index", help="Let QMD scan, reconcile deleted files, and embed changes.")
    index.add_argument("--lexical-only", action="store_true", help="Update text only; semantic queries will refuse missing embeddings.")
    status = sub.add_parser("status", help="Inspect QMD readiness; does not scan the vault for freshness.")
    status.add_argument("--format", choices=("text", "json"), default="json")
    query_parser = sub.add_parser("query", help="Bounded local search; scores are ranking evidence, not confidence.")
    query_parser.add_argument("query")
    query_parser.add_argument("--mode", choices=("hybrid", "lexical", "vector"), default="hybrid")
    query_parser.add_argument("--scope", choices=SCOPES + ("all",), default="active")
    query_parser.add_argument("--top", type=int, default=10)
    query_parser.add_argument("--path", action="append")
    query_parser.add_argument("--after")
    query_parser.add_argument("--before")
    query_parser.add_argument("--no-rerank", action="store_true")
    query_parser.add_argument("--expand", action="store_true", help="Opt into the local query-expansion model.")
    query_parser.add_argument("--format", choices=("json", "tsv"), default="json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        vault = vault_root()
        if args.command in {"init", "index"} and STAGED_MODEL_DIRECTORY_ENV in os.environ:
            raise SearchError("staged QMD state is read-only input and cannot be used for init or index")
        if args.command == "query":
            if not 1 <= args.top <= 100 or not args.query.strip():
                raise SearchError("query must be nonempty and --top must be between 1 and 100")
            payload = query(args)
        elif args.command == "init":
            directory = prepare(vault)
            if args.download_models:
                executable = ROOT / "node_modules" / ".bin" / "qmd"
                env = dict(child_environment(), QMD_CONFIG_DIR=str(directory), INDEX_PATH=str(directory / "index.sqlite"),
                           XDG_CACHE_HOME=str(cache_root() / "assets"))
                try:
                    result = _node.run([executable, "--index", "index", "pull"],
                                       cwd=ROOT, env=env, stdout=sys.stderr, timeout=3600)
                except _node.NodeError as exc:
                    raise SearchError(f"QMD model download failed: {exc}") from exc
                if result.returncode:
                    raise SearchError(f"QMD model download exited {result.returncode}")
            payload = {"backend": "qmd", "profile": settings()["profile"],
                       "config": str(directory / "index.yml"), "indexed": False}
        elif args.command == "index":
            prepare(vault)
            payload = bridge(vault, "index", roles=[] if args.lexical_only else ["embed"],
                             lexicalOnly=args.lexical_only)
        else:
            payload = status(vault)
        if args.command == "query" and args.format == "tsv":
            for row in payload:
                print(f"{row['path']}\t{row['score']:.4f}\t{row['scope']}")
        elif getattr(args, "format", "json") == "text":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(payload, ensure_ascii=False))
        return 0
    except (SearchError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"semantic: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
