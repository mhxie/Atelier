#!/usr/bin/env python3
"""Deterministic smoke tests for native Claude and Codex harness edges.

This avoids the private vault and network. It checks registry-backed Codex
command skills, native adapters, lint, and intent-hook behavior.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import cues
import _paths
import autoevo_preflight
import autoevo_quarantine
import autoevo_verify
import command_timeout
import dining_audit
import intent_coverage
import privacy_check
import routine_audit
import routine_claim
import routine_lock
import routine_result
import semantic
import semantic_backends
import semantic_corpus
import semantic_eval

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class SmokeFailure(Exception):
    pass


def run(
    args: list[str],
    *,
    input_text: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> str:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [PYTHON, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        input=input_text,
        env=env,
    )
    if result.returncode != 0:
        raise SmokeFailure(
            f"`{PYTHON} {' '.join(args)}` failed with exit {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def parse_intent_route(output: str) -> tuple[dict[str, object], str]:
    expect(bool(output), "contextual intent hook did not emit a route packet")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"intent hook emitted malformed route JSON: {exc}") from exc
    expect(isinstance(payload, dict), "intent hook output must be an object")
    hook_output = payload.get("hookSpecificOutput")
    expect(isinstance(hook_output, dict), "intent hook omitted hookSpecificOutput")
    context = hook_output.get("additionalContext")
    expect(
        hook_output.get("hookEventName") == "UserPromptSubmit",
        "intent route hook event drift",
    )
    expect(isinstance(context, str), "intent route additionalContext must be text")
    expect(
        context.startswith(intent_coverage.INTENT_ROUTE_CONTEXT_PREFIX),
        "intent route context prefix drift",
    )
    route_text = context.removeprefix(intent_coverage.INTENT_ROUTE_CONTEXT_PREFIX)
    try:
        route = json.loads(route_text)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"intent route packet is not JSON: {exc}") from exc
    expect(isinstance(route, dict), "intent route packet must be an object")
    return route, context


def check_harness_lint() -> None:
    payload = json.loads(run(["scripts/harness_lint.py", "--json"]))
    counts = payload["counts"]
    expect(
        counts.get("error", 0) == 0,
        f"harness_lint.py reports {counts.get('error', 0)} error(s)",
    )
    expect(
        counts.get("warn", 0) == 0,
        f"harness_lint.py reports {counts.get('warn', 0)} warn(s)",
    )
    error_or_warn = [
        f for f in payload["findings"] if f.get("severity") in ("ERROR", "WARN")
    ]
    expect(
        error_or_warn == [],
        f"harness_lint.py returned {len(error_or_warn)} error/warn finding(s)",
    )


def check_codex_command_skills() -> None:
    with (ROOT / "harness" / "commands.toml").open("rb") as handle:
        commands = tomllib.load(handle)["commands"]
    expected = {
        name: entry
        for name, entry in commands.items()
        if isinstance(entry, dict) and entry.get("user_facing", True) is not False
    }
    expect(len(expected) >= 10, "expected user-facing portable commands")
    actual = {
        path.parent.name
        for path in (ROOT / ".agents" / "skills").glob("*/SKILL.md")
        if path.parent.name != "atelier"
    }
    expect(
        actual == set(expected),
        "native Codex command skills drifted from user-facing registry rows",
    )
    for name, entry in expected.items():
        skill_dir = ROOT / ".agents" / "skills" / name
        skill_path = skill_dir / "SKILL.md"
        metadata_path = skill_dir / "agents" / "openai.yaml"
        expect(skill_path.exists(), f"missing Codex command skill `${name}`")
        expect(metadata_path.exists(), f"missing Codex metadata for `${name}`")
        skill = skill_path.read_text(encoding="utf-8")
        metadata = metadata_path.read_text(encoding="utf-8")
        expect(
            str(entry["source"]) in skill,
            f"`${name}` does not point to its command source",
        )
        expect(
            "scripts/atelier.py" not in skill,
            f"`${name}` still calls the retired bridge",
        )
        expect(
            f"${name}" in metadata, f"`${name}` metadata lacks its explicit invocation"
        )
        expect(
            "allow_implicit_invocation: false" in metadata,
            f"`${name}` must remain explicit-only",
        )


def check_codex_native_agents() -> None:
    with (ROOT / "harness" / "agents.toml").open("rb") as handle:
        agents = tomllib.load(handle)["agents"]
    with (ROOT / "harness" / "models.toml").open("rb") as handle:
        models = tomllib.load(handle)["models"]
    expected = {
        name
        for name, entry in agents.items()
        if isinstance(entry, dict) and entry.get("status") != "script-driven"
    }
    actual = {path.stem for path in (ROOT / ".codex" / "agents").glob("*.toml")}
    expect(
        actual == expected,
        "native Codex agent adapters drifted from harness/agents.toml",
    )
    from render_runtime_edges import TIER_TO_EFFORT as effort_by_tier
    for name in expected:
        with (ROOT / ".codex" / "agents" / f"{name}.toml").open("rb") as handle:
            adapter = tomllib.load(handle)
        native_identity = agents[name]["voices"]["native"]
        reasoning_tier = models[native_identity]["reasoning_tier"]
        expect(
            adapter.get("model_reasoning_effort") == effort_by_tier[reasoning_tier],
            f"native Codex agent `{name}` reasoning effort drift",
        )
        if name == "forgetter":
            instructions = str(adapter.get("developer_instructions", ""))
            expect(
                "---forgetter-result---" in instructions
                and "---begin-result---" not in instructions,
                "native Forgetter adapter has a conflicting envelope marker",
            )


def check_paper_cache() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    source_doc = (ROOT / "sources" / "local-papers.md").read_text(encoding="utf-8")
    read_command = (ROOT / ".claude" / "commands" / "read.md").read_text(
        encoding="utf-8"
    )
    expect("/tmp/" in gitignore.splitlines(), "repo tmp containment rule is missing")
    expect(
        "Never write repo-relative `tmp/`" in claude,
        "shared scratch boundary is missing from CLAUDE.md",
    )
    for document, label in (
        (source_doc, "local paper source doc"),
        (read_command, "read command"),
    ):
        expect(
            "scripts/paper_cache.py" in document,
            f"{label} does not route PDF extraction through paper_cache.py",
        )

    with tempfile.TemporaryDirectory(prefix="atelier-paper-cache-") as temp_dir:
        temp = Path(temp_dir)
        vault = temp / "vault"
        papers = vault / "papers"
        papers.mkdir(parents=True)
        (vault / "preprints").mkdir()
        source = papers / "Example Paper.pdf"
        source.write_text("fixture pdf", encoding="utf-8")

        fake_bin = temp / "bin"
        fake_bin.mkdir()
        fake_pdftotext = fake_bin / "pdftotext"
        fake_pdftotext.write_text(
            f"#!{PYTHON}\n"
            "from pathlib import Path\n"
            "import sys\n"
            "source = Path(sys.argv[-2]).read_text(encoding='utf-8')\n"
            "content = '   \\n' if source == 'empty fixture' else 'EXTRACTED\\n' + source\n"
            "Path(sys.argv[-1]).write_text(content, encoding='utf-8')\n",
            encoding="utf-8",
        )
        fake_pdftotext.chmod(0o755)
        env = os.environ | {
            "OV": str(vault),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        }

        first = subprocess.run(
            [PYTHON, "scripts/paper_cache.py", str(source), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(first.returncode == 0, f"paper cache extraction failed: {first.stderr}")
        first_payload = json.loads(first.stdout)
        expect(
            first_payload["status"] == "extracted",
            "paper cache did not extract on miss",
        )
        cache_dir = vault / "cache" / "example-paper"
        expect(
            (cache_dir / "paper.txt").read_text(encoding="utf-8")
            == "EXTRACTED\nfixture pdf",
            "paper cache text output drift",
        )
        expect((cache_dir / "index.md").is_file(), "paper cache index is missing")
        expect(
            (cache_dir / "source.json").is_file(),
            "paper cache source signature is missing",
        )

        second = subprocess.run(
            [PYTHON, "scripts/paper_cache.py", str(source), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(second.returncode == 0, f"paper cache reuse failed: {second.stderr}")
        expect(
            json.loads(second.stdout)["status"] == "cached",
            "fresh paper cache was rebuilt",
        )

        colliding = vault / "preprints" / "Example Paper.pdf"
        colliding.write_text("different fixture pdf", encoding="utf-8")
        collision = subprocess.run(
            [PYTHON, "scripts/paper_cache.py", str(colliding), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(collision.returncode == 2, "paper cache overwrote a same-slug PDF cache")
        expect(
            (cache_dir / "paper.txt").read_text(encoding="utf-8")
            == "EXTRACTED\nfixture pdf",
            "paper cache collision changed the original extraction",
        )

        empty_source = papers / "Empty.pdf"
        empty_source.write_text("empty fixture", encoding="utf-8")
        empty = subprocess.run(
            [PYTHON, "scripts/paper_cache.py", str(empty_source), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(empty.returncode == 2, "paper cache accepted an empty text extraction")
        expect(
            not (vault / "cache" / "empty" / "paper.txt").exists(),
            "paper cache retained an empty extraction",
        )

        outside = temp / "outside.pdf"
        outside.write_text("not canonical", encoding="utf-8")
        rejected = subprocess.run(
            [PYTHON, "scripts/paper_cache.py", str(outside)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            rejected.returncode == 2, "paper cache accepted a PDF outside the L3 store"
        )


def check_dining_audit() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-dining-audit-") as temp_dir:
        vault = Path(temp_dir)
        profile = vault / "profile" / "diet.md"
        profile.parent.mkdir(parents=True)
        mapped = {
            "Regional dining catalog": "travel/regional-catalog.md",
            "Meal-history tracker": "travel/meal-history.md",
            "Credit-perks catalog": "travel/credit-eligibility.md",
            "Benefits tracker": "finance/benefits-tracker.md",
            "Prepaid-balance tracker": "finance/prepaid-balances.md",
        }
        for relative in mapped.values():
            target = vault / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# Fixture\n", encoding="utf-8")
        profile.write_text(
            """# Personal Diet Policy

## Catalog files

| Role | Path | Write owner |
|---|---|---|
| Regional dining catalog | `travel/regional-catalog.md` | fixture |
| Meal-history tracker | `travel/meal-history.md` | fixture |
| Credit-perks catalog | `travel/credit-eligibility.md` | fixture |
| Benefits tracker | `finance/benefits-tracker.md` | fixture |
| Prepaid-balance tracker | `finance/prepaid-balances.md` | fixture |

## Full health-flag taxonomy

- `flag-a` - fixture
- `flag-b` - fixture
""",
            encoding="utf-8",
        )
        dining_log = vault / mapped["Meal-history tracker"]
        dining_log.write_text(
            """# Meal History Fixture

## Visits

| Date | Restaurant | City | 类型 | ⭐ | 评分 | 再去 | 健康 | 人数 | 总额 | 人均 | Platform | Credit | 必点·备注 |
|---|---|---|---|---|---|---|---|---:|---:|---:|---|---|---|
| 2025-12-31 | JPY | Tokyo | Test | — | 7 | Y | flag-a | 2 | ¥23,925 | ¥11,962.50 | W | — | okay |
| 2026-01-01 | A | X | Test | — | 8 | Y | flag-a | 2 | $20.00 | $10.00 | W | — | good |
| 2026-01-02 | B | X | Test | — | 7 | Maybe | flag-b | 3 | ~$30.00 | ~$10.00 | W | — | okay |

## Derived views
""",
            encoding="utf-8",
        )
        valid = dining_audit.audit(vault, 2)
        expect(valid["ok"] is True, f"valid dining fixture failed: {valid}")
        expect(valid["stats"]["rows"] == 3, "dining row count drift")
        expect(
            len(valid["recent"]) == 2
            and valid["per_person_trend"]["known"] == 2
            and valid["per_person_trend"]["direction"] == "unknown",
            f"dining recent view overclaimed a sparse trend: {valid}",
        )

        dining_log.write_text(
            dining_log.read_text(encoding="utf-8")
            .replace(
                "| 2026-01-02 | B |",
                "| 2025-12-31 | B |",
            )
            .replace(
                "| ~$30.00 | ~$10.00 |",
                "| ~$30.00 | ~$12.00 |",
            )
            .replace(
                "| 2026-01-01 | A |",
                "| 2026-01-01 | TBD |",
            ),
            encoding="utf-8",
        )
        (vault / mapped["Regional dining catalog"]).write_text(
            "[broken](missing.md)\n[remote](readwise:fixture)\n",
            encoding="utf-8",
        )
        (vault / mapped["Credit-perks catalog"]).write_text(
            "## Cycle Tracking\n",
            encoding="utf-8",
        )
        invalid = dining_audit.audit(vault)
        error_codes = {finding["code"] for finding in invalid["errors"]}
        expect(invalid["ok"] is False, "invalid dining fixture passed")
        expect("date_order" in error_codes, "dining audit missed event-date drift")
        expect(
            "per_person_mismatch" in error_codes,
            "dining audit missed per-person arithmetic drift",
        )
        expect(
            "restaurant_pending" in error_codes,
            "dining audit accepted a placeholder restaurant",
        )
        expect(
            "local_link_broken" in error_codes,
            "dining audit missed a broken mapped-catalog link",
        )
        expect(
            "live_state_in_eligibility_catalog" in error_codes,
            "dining audit accepted live state in the eligibility catalog",
        )


def check_semantic_cache_first() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-hf-cache-") as temp_dir:
        hub = Path(temp_dir)
        repository = hub / "models--Example--embedding"
        revision = "abc123"
        snapshot = repository / "snapshots" / revision
        snapshot.mkdir(parents=True)
        (snapshot / "config.json").write_text("{}\n", encoding="utf-8")
        (snapshot / "modules.json").write_text("[]\n", encoding="utf-8")
        refs = repository / "refs"
        refs.mkdir()
        (refs / "main").write_text(revision, encoding="utf-8")
        original_cache = os.environ.get("HF_HUB_CACHE")
        os.environ["HF_HUB_CACHE"] = str(hub)
        try:
            resolved = semantic_backends._local_model_snapshot("Example/embedding")
            source, local_only = semantic_backends._sentence_transformer_source(
                "Example/embedding"
            )
            expect(
                resolved == str(snapshot.resolve())
                and source == str(snapshot.resolve())
                and local_only is True,
                "semantic embedder did not prefer a complete local snapshot",
            )
            missing_source, missing_local_only = (
                semantic_backends._sentence_transformer_source("Example/not-cached")
            )
            expect(
                missing_source == "Example/not-cached" and missing_local_only is False,
                "semantic embedder disabled first-time download without offline mode",
            )
        finally:
            if original_cache is None:
                os.environ.pop("HF_HUB_CACHE", None)
            else:
                os.environ["HF_HUB_CACHE"] = original_cache


def check_semantic_maintenance() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-semantic-freshness-") as temp_dir:
        vault = Path(temp_dir)
        stable = vault / "stable.md"
        modified = vault / "modified.md"
        added = vault / "added.md"
        empty = vault / "empty.md"
        whitespace = vault / "whitespace.md"
        stable.write_text("stable\n", encoding="utf-8")
        modified.write_text("modified\n", encoding="utf-8")
        added.write_text("added\n", encoding="utf-8")
        empty.write_text("", encoding="utf-8")
        whitespace.write_text(" \n\t", encoding="utf-8")

        baseline = 1_700_000_000.0
        os.utime(stable, (baseline, baseline))
        os.utime(modified, (baseline + 10, baseline + 10))
        os.utime(added, (baseline + 20, baseline + 20))
        current_manifest, physical_count, _, unreadable = (
            semantic._current_corpus_manifest(vault)
        )
        indexed = {
            "stable.md": {
                "mtime": baseline,
                "manifest_fingerprint": semantic.physical_manifest_fingerprint(
                    "active",
                    "authored",
                    baseline,
                ),
            },
            "modified.md": {
                "mtime": baseline,
                "manifest_fingerprint": semantic.physical_manifest_fingerprint(
                    "active",
                    "authored",
                    baseline,
                ),
            },
            "removed.md": {
                "mtime": baseline,
                "manifest_fingerprint": semantic.physical_manifest_fingerprint(
                    "active",
                    "authored",
                    baseline,
                ),
            },
        }
        stale = semantic._freshness_from_manifest(
            current_manifest,
            indexed,
            physical_count=physical_count,
            unreadable=unreadable,
        )
        expect(
            stale["fresh"] is False
            and stale["current_files"] == 3
            and stale["new"] == 1
            and stale["modified"] == 1
            and stale["removed"] == 1
            and stale["unreadable"] == 0,
            f"semantic freshness drift classification failed: {stale}",
        )

        fresh = semantic._freshness_from_manifest(
            current_manifest,
            current_manifest,
            physical_count=physical_count,
            unreadable=unreadable,
        )
        expect(
            fresh["fresh"] is True,
            f"semantic freshness rejected a current index: {fresh}",
        )
        immediate_edit = semantic._freshness_from_manifest(
            {
                "active.md": {
                    "mtime": baseline + 0.5,
                    "manifest_fingerprint": semantic.physical_manifest_fingerprint(
                        "active",
                        "authored",
                        baseline + 0.5,
                    ),
                }
            },
            {
                "active.md": {
                    "mtime": baseline,
                    "manifest_fingerprint": semantic.physical_manifest_fingerprint(
                        "active",
                        "authored",
                        baseline,
                    ),
                }
            },
            physical_count=1,
        )
        expect(
            immediate_edit["modified"] == 1 and immediate_edit["fresh"] is False,
            "sub-second physical-file edits did not invalidate semantic freshness",
        )

    args = semantic.build_parser().parse_args(["index", "--if-stale"])
    expect(
        args.if_stale is True and args.rebuild is False,
        "semantic index parser lost the drift-gated maintenance mode",
    )

    runner = (ROOT / "scripts" / "semantic_index_runner.sh").read_text(encoding="utf-8")
    for fragment in (
        'routine_owner.py" check --json',
        "HF_HUB_OFFLINE=1",
        "TRANSFORMERS_OFFLINE=1",
        "command_timeout.py",
        "/usr/bin/caffeinate",
        "uv run --offline --frozen scripts/semantic.py index --if-stale",
    ):
        expect(
            fragment in runner,
            f"semantic maintenance runner missing contract fragment: {fragment}",
        )
    expect(
        "codex" not in runner.lower(), "deterministic index maintenance invokes Codex"
    )

    plist_path = ROOT / "scripts" / "launchd" / "com.atelier.semantic-index.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    expect(
        plist.get("Label") == "com.atelier.semantic-index"
        and plist.get("RunAtLoad") is True
        and plist.get("StartCalendarInterval")
        == [
            {"Hour": 7, "Minute": 30},
            {"Hour": 19, "Minute": 30},
        ],
        "semantic maintenance launchd schedule drift",
    )
    arguments = plist.get("ProgramArguments", [])
    expect(
        any("semantic_index_runner.sh" in str(value) for value in arguments),
        "semantic maintenance plist does not invoke its deterministic runner",
    )

    for path in (
        ROOT / ".claude" / "commands" / "autoevo-nightly.md",
        ROOT / "protocols" / "autoevo.md",
    ):
        expect(
            "lazy rebuild" not in path.read_text(encoding="utf-8").lower(),
            f"{path.relative_to(ROOT)} still claims query refreshes the index",
        )


def check_tracking_refresh_routine() -> None:
    runner = (ROOT / "scripts" / "tracking_refresh_runner.sh").read_text(
        encoding="utf-8"
    )
    for fragment in (
        'routine_owner.py" check --json',
        "find_python.sh",
        "command_timeout.py",
        "/usr/bin/caffeinate",
        'refresh_tracking.py" --json',
    ):
        expect(
            fragment in runner,
            f"tracking refresh runner missing contract fragment: {fragment}",
        )
    expect(
        "codex" not in runner.lower(),
        "deterministic tracking refresh invokes Codex",
    )

    plist_path = ROOT / "scripts" / "launchd" / "com.atelier.tracking-refresh.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    expect(
        plist.get("Label") == "com.atelier.tracking-refresh"
        and plist.get("RunAtLoad") is True
        and plist.get("StartCalendarInterval")
        == [
            {"Hour": 5, "Minute": 30},
            {"Hour": 17, "Minute": 30},
        ],
        "tracking refresh launchd schedule drift",
    )
    arguments = plist.get("ProgramArguments", [])
    expect(
        any("tracking_refresh_runner.sh" in str(value) for value in arguments),
        "tracking refresh plist does not invoke its deterministic runner",
    )


def check_vault_job_runner() -> None:
    runner = (ROOT / "scripts" / "vault_job_runner.sh").read_text(encoding="utf-8")
    for fragment in (
        'routine_owner.py" check --json',
        "find_python.sh",
        "command_timeout.py",
        "/usr/bin/caffeinate",
        "ATELIER_VAULT_JOB_TIMEOUT_SECONDS",
        "uv run --quiet",
        # A vault-relative path only: absolute paths and parent traversal are
        # refused before ownership is even checked.
        "/*|*..*)",
    ):
        expect(
            fragment in runner,
            f"vault job runner missing contract fragment: {fragment}",
        )
    expect(
        "codex" not in runner.lower() and "claude" not in runner.lower(),
        "deterministic vault job runner invokes a model runtime",
    )


def check_semantic_corpus_policy() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-semantic-corpus-") as temp_dir:
        vault = Path(temp_dir)
        fixture_files = {
            "active.md": "## Active\ncurrent authored knowledge\n",
            "duplicate.md": "## Active\ncurrent authored knowledge\n",
            "archive/old.md": "## Old\ncold history\n",
            "inbox/pending.md": "## Pending\nunreviewed capture\n",
            "health/nutrition/inbox/pending.md": "## Pending\nnested capture\n",
            "sessions/2099-01-01-test.md": "## Continuity\nprocess only\n",
            "cache/noise.md": "cache noise\n",
            "wip/cache/noise.md": "nested cache noise\n",
            "_meta/noise.md": "operational noise\n",
            "_routine_prompts/noise.md": "prompt noise\n",
            "career/_tools/runs/noise.md": "generated run log\n",
            ".trash/noise.md": "trash noise\n",
            "archive/orphan-stubs/noise.md": "stub noise\n",
            "research/tool/node_modules/pkg/README.md": "dependency docs\n",
            "finance/raw/tax-2099/receipt.txt": "readable raw receipt\n",
        }
        for relative, content in fixture_files.items():
            path = vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        binary = b"same-binary-provenance"
        for relative in (
            "finance/raw/tax-2099/receipt.pdf",
            "travel/raw/confirmations/receipt-copy.pdf",
        ):
            path = vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(binary)
        (vault / "empty.md").write_text("", encoding="utf-8")
        (vault / "whitespace.md").write_text(" \n\t", encoding="utf-8")

        decisions = tuple(semantic_corpus.iter_file_decisions(vault))
        by_path = {item.relative_path: item for item in decisions}
        expect(
            by_path["active.md"].included
            and by_path["archive/old.md"].scope == "archive"
            and by_path["inbox/pending.md"].scope == "inbox"
            and by_path["health/nutrition/inbox/pending.md"].scope == "inbox"
            and by_path["sessions/2099-01-01-test.md"].scope == "process"
            and by_path["finance/raw/tax-2099/receipt.txt"].scope == "raw",
            "central corpus scope classification drift",
        )
        for excluded_path in (
            "cache/noise.md",
            "wip/cache/noise.md",
            "_meta/noise.md",
            "_routine_prompts/noise.md",
            "career/_tools/runs/noise.md",
            ".trash/noise.md",
            "archive/orphan-stubs/noise.md",
            "research/tool/node_modules/pkg/README.md",
            "empty.md",
            "whitespace.md",
        ):
            expect(
                not by_path[excluded_path].included,
                f"hard or empty corpus path was included: {excluded_path}",
            )

        active = tuple(
            semantic_corpus.iter_corpus_records(
                vault,
                scope="active",
                files=decisions,
            )
        )
        raw = tuple(
            semantic_corpus.iter_corpus_records(
                vault,
                scope="raw",
                files=decisions,
            )
        )
        expect(
            any(row.representation == "raw_locator" for row in active)
            and not any(row.representation == "raw_text" for row in active),
            "active scope did not substitute raw locators for full raw text",
        )
        expect(
            any(row.representation == "raw_locator" for row in raw)
            and any(row.representation == "raw_text" for row in raw),
            "raw scope omitted locator or readable raw content",
        )
        locator = next(row for row in active if row.representation == "raw_locator")
        expect(
            "same-binary-provenance" not in locator.text
            and semantic_corpus.scope_matches(locator, "active")
            and semantic_corpus.scope_matches(locator, "raw"),
            "raw locator extracted binary content or lost dual-scope visibility",
        )

        audit = semantic_corpus.audit_corpus(
            vault,
            chunk_estimator=semantic_backends.chunk_markdown,
        )
        expect(
            audit["by_exclusion_reason"]["dependency_tree"]["files"] == 1,
            "dependency-tree exclusion missing from corpus audit",
        )
        expect(
            audit["by_exclusion_reason"]["operational_tools"]["files"] == 1,
            "nested operational-tool exclusion missing from corpus audit",
        )
        expect(
            audit["exact_duplicates"]["basis"] == "all_regular_physical_files"
            and audit["exact_duplicates"]["groups"] >= 2
            and audit["exact_duplicates"]["included_text"]["groups"] >= 1,
            "all-file and included-text duplicate summaries did not reconcile",
        )

        fingerprint_drift = semantic._freshness_from_manifest(
            {
                locator.path: {
                    "mtime": locator.mtime,
                    "manifest_fingerprint": "new",
                }
            },
            {
                locator.path: {
                    "mtime": locator.mtime,
                    "manifest_fingerprint": "old",
                }
            },
            physical_count=0,
        )
        expect(
            fingerprint_drift["modified"] == 1 and fingerprint_drift["fresh"] is False,
            "raw locator fingerprint drift did not invalidate freshness",
        )
        older_mtime_drift = semantic._freshness_from_manifest(
            {
                "active.md": {
                    "mtime": 100.0,
                    "manifest_fingerprint": "",
                }
            },
            {
                "active.md": {
                    "mtime": 200.0,
                    "manifest_fingerprint": "",
                }
            },
            physical_count=1,
        )
        expect(
            older_mtime_drift["modified"] == 1 and older_mtime_drift["fresh"] is False,
            "restored older mtimes did not invalidate freshness",
        )

    query_args = semantic.build_parser().parse_args(["query", "fixture"])
    hybrid_query_args = semantic.build_parser().parse_args(
        ["query", "fixture", "--hybrid"]
    )
    expect(
        query_args.scope == "active"
        and query_args.sources == "local"
        and query_args.context is False,
        "semantic query defaults are not local active bounded opt-in",
    )
    expect(
        hybrid_query_args.hybrid
        and semantic._locator_backfill_enabled(hybrid_query_args.scope)
        and not semantic._locator_backfill_enabled("archive"),
        "hybrid retrieval disabled raw-locator navigation backfill",
    )
    eval_args = semantic_eval.build_parser().parse_args(["run"])
    expect(
        eval_args.scope == "active" and semantic_eval.EVAL_SCOPES == ("active", "all"),
        "semantic evaluation escaped its active/all gold-set contract",
    )
    hits = [
        semantic.QueryHit(
            path="same.md",
            score=0.9,
            chunk_id=0,
            chunk_text="x" * 700,
        ),
        semantic.QueryHit(
            path="same.md",
            score=0.8,
            chunk_id=1,
            chunk_text="duplicate chunk",
        ),
        *[
            semantic.QueryHit(
                path=f"@raw-locator/domain/raw/cluster-{index}",
                score=0.7 - index * 0.01,
                chunk_text="Raw cluster locator",
                tier="L1",
                representation="raw_locator",
            )
            for index in range(3)
        ],
        semantic.QueryHit(path="other.md", score=0.5, chunk_text="other"),
    ]
    collapsed = semantic._collapse_hits(hits, top=10, requested_scope="active")
    expect(
        len([hit for hit in collapsed if hit.path == "same.md"]) == 1
        and len([hit for hit in collapsed if hit.representation == "raw_locator"]) == 2,
        "result collapse or active raw-locator cap drift",
    )
    expect(
        semantic._path_matches(
            "@raw-locator/finance/raw/tax-2099",
            ["finance"],
        )
        and semantic_backends._passes_filters(
            semantic_backends.SearchResult(
                path="@raw-locator/finance/raw/tax-2099",
                score=0.9,
                representation="raw_locator",
            ),
            {"path_prefix": ["finance/raw"]},
        ),
        "raw locator did not honor its virtual source path prefix",
    )
    locator_sql = semantic_backends._path_prefix_where_clause(
        ["finance/raw"],
        lambda value: value,
    )
    expect(
        "@raw-locator/finance/raw/%" in locator_sql,
        "Lance path SQL omitted the raw-locator virtual alias",
    )

    delete_filters: list[str] = []

    class FakeDeleteTable:
        @staticmethod
        def delete(predicate: str) -> None:
            delete_filters.append(predicate)

    delete_store = semantic_backends.LanceStore.__new__(semantic_backends.LanceStore)
    delete_store._table = FakeDeleteTable()
    delete_paths = [f"notes/item-{index}.md" for index in range(300)]
    expect(
        delete_store.delete_by_path(delete_paths) == len(delete_paths)
        and len(delete_filters) == 3
        and all(
            predicate.count('path = "')
            <= semantic_backends.LanceStore.DELETE_PATH_BATCH_SIZE
            for predicate in delete_filters
        ),
        "Lance path deletion did not bound policy-wide predicates",
    )

    failed_delete_filters: list[str] = []

    class FakePartialDeleteTable:
        @staticmethod
        def delete(predicate: str) -> None:
            failed_delete_filters.append(predicate)
            if len(failed_delete_filters) == 2:
                raise RuntimeError("fixture delete failure")

    failed_delete_store = semantic_backends.LanceStore.__new__(
        semantic_backends.LanceStore
    )
    failed_delete_store._table = FakePartialDeleteTable()
    try:
        failed_delete_store.delete_by_path(delete_paths)
    except RuntimeError:
        pass
    else:
        raise SmokeFailure("partial Lance path deletion did not abort")
    expect(
        len(failed_delete_filters) == 2,
        "Lance path deletion continued after a partial batch failure",
    )

    legacy_active_sql = semantic_corpus.scope_sql(
        "active",
        has_metadata_columns=False,
    )
    expect(
        "_tools" in legacy_active_sql
        and "%/inbox/%" in legacy_active_sql
        and "%/cache/%" in legacy_active_sql,
        "legacy scope SQL drifted from nested operational and inbox policy",
    )

    bm25_trace: dict[str, object] = {}

    class FakeLanceQuery:
        def where(self, predicate: str) -> "FakeLanceQuery":
            bm25_trace["where"] = predicate
            return self

        def select(self, columns: list[str]) -> "FakeLanceQuery":
            bm25_trace["columns"] = columns
            return self

        def limit(self, count: int) -> "FakeLanceQuery":
            bm25_trace["limit"] = count
            return self

        @staticmethod
        def to_pandas() -> str:
            return "scope-frame"

    class FakeLanceTable:
        @staticmethod
        def count_rows() -> int:
            return 99

        @staticmethod
        def search() -> FakeLanceQuery:
            return FakeLanceQuery()

    class FakeLanceStore:
        _table = FakeLanceTable()

        @staticmethod
        def has_corpus_metadata() -> bool:
            return True

    expect(
        semantic_backends._bm25_scope_frame(FakeLanceStore(), "active") == "scope-frame"
        and bm25_trace["where"] == "scope = 'active'"
        and bm25_trace["limit"] == 99,
        "BM25 materialized Lance before applying the selected scope",
    )
    locator_candidates = [
        semantic_backends.SearchResult(
            path="@raw-locator/finance/raw/tax",
            score=0.0,
            chunk_text="Filename terms: unique-token",
            tier="L1",
            scope="active",
            representation="raw_locator",
        ),
        semantic_backends.SearchResult(
            path="@raw-locator/travel/raw/receipts",
            score=0.0,
            chunk_text="Filename terms: hotel receipt",
            tier="L1",
            scope="active",
            representation="raw_locator",
        ),
    ]
    locator_ranked = semantic_backends._rank_raw_locator_results(
        locator_candidates,
        "unique-token",
        top_k=10,
        filters={"scope": "active"},
    )
    expect(
        [result.path for result in locator_ranked] == ["@raw-locator/finance/raw/tax"],
        "filename-only query did not discover its raw locator",
    )
    generic_locator_ranked = semantic_backends._rank_raw_locator_results(
        [
            semantic_backends.SearchResult(
                path="@raw-locator/archive/education/raw/training",
                score=0.0,
                chunk_text="Filename terms: training",
                tier="L1",
                scope="active",
                representation="raw_locator",
            )
        ],
        "distributed training architecture",
        top_k=10,
        filters={"scope": "active"},
    )
    expect(
        generic_locator_ranked == [],
        "multi-word concept query triggered a locator on one rare token",
    )
    capsule = semantic._capsule(collapsed[0], "active")
    expect(
        len(capsule["snippet"]) <= 600 and capsule["truncated"] is True,
        "result capsule exceeded the 600-character source budget",
    )
    legacy_ranked = semantic._rank_hits(
        hits,
        top=2,
        requested_scope="active",
    )
    active_ranked = semantic._rank_hits(
        hits,
        top=10,
        requested_scope="active",
    )
    expect(
        [hit.path for hit in legacy_ranked] == ["same.md", "same.md"]
        and len([hit for hit in active_ranked if hit.representation == "raw_locator"])
        == 2
        and semantic._rank_hits(
            hits,
            top=0,
            requested_scope="active",
        )
        == []
        and semantic._collapse_hits(hits, top=0, requested_scope="active") == [],
        "legacy chunk ranking or non-positive top handling drift",
    )
    authored_page = [
        semantic.QueryHit(
            path=f"note-{index}.md",
            score=0.9 - index * 0.03,
            chunk_text="authored",
        )
        for index in range(10)
    ]
    exact_locator = semantic.QueryHit(
        path="@raw-locator/finance/raw/tax",
        score=1.0,
        chunk_text="Filename terms: unique-token",
        tier="L1",
        representation="raw_locator",
    )
    backfilled = semantic._backfill_locator_hit(
        authored_page,
        [exact_locator],
        top=10,
        requested_scope="active",
        collapse=True,
    )
    chunk_backfilled = semantic._backfill_locator_hit(
        authored_page,
        [exact_locator],
        top=10,
        requested_scope="active",
        collapse=False,
    )
    authored_first = semantic._rank_hits(
        [
            semantic.QueryHit(
                path="authored.md",
                score=0.1,
                chunk_text="authored",
            ),
            exact_locator,
        ],
        top=2,
        requested_scope="active",
    )
    expect(
        len(backfilled) == 10
        and [hit.path for hit in backfilled[:9]]
        == [hit.path for hit in authored_page[:9]]
        and backfilled[-1].path == exact_locator.path
        and backfilled[-1].score <= backfilled[-2].score
        and chunk_backfilled[-1].path == exact_locator.path
        and chunk_backfilled[-1].score <= chunk_backfilled[-2].score
        and [hit.path for hit in authored_first] == ["authored.md", exact_locator.path]
        and authored_first[0].score >= authored_first[1].score,
        "raw locator competed with authored or reranked results instead of backfilling",
    )
    expect(
        semantic._backfill_locator_hit(
            authored_page,
            [exact_locator],
            top=1,
            requested_scope="active",
            collapse=True,
        )[0].path
        == "note-0.md",
        "top-1 active search displaced its authored result with a locator",
    )
    expect(
        semantic._local_candidate_count(
            10,
            cross_encoder=False,
            collapse=False,
        )
        == 10
        and semantic._local_candidate_count(
            10,
            cross_encoder=False,
            collapse=True,
        )
        == 60
        and semantic._local_candidate_count(
            10,
            cross_encoder=True,
            collapse=False,
        )
        == 30,
        "candidate widening escaped context, federation, or rerank modes",
    )
    current_manifest = semantic._record_manifest(
        decisions,
        tuple(record for record in active if record.representation == "raw_locator"),
    )
    active_decision = next(
        item for item in decisions if item.relative_path == "active.md"
    )
    expect(
        current_manifest["active.md"]["manifest_fingerprint"]
        == semantic.physical_manifest_fingerprint(
            "active",
            "authored",
            active_decision.mtime,
        )
        and semantic._manifest_uses_current_policy(current_manifest),
        "corpus policy version was not persisted in the index manifest",
    )
    scope_drift = semantic._freshness_from_manifest(
        {
            "active.md": {
                "mtime": 100.0,
                "manifest_fingerprint": semantic.corpus_metadata_fingerprint(
                    "inbox",
                    "authored",
                ),
            }
        },
        {
            "active.md": {
                "mtime": 100.0,
                "manifest_fingerprint": semantic.corpus_metadata_fingerprint(
                    "active",
                    "authored",
                ),
            }
        },
        physical_count=1,
    )
    expect(
        scope_drift["modified"] == 1 and scope_drift["fresh"] is False,
        "scope-only corpus drift did not invalidate freshness",
    )
    external_capsule = semantic._capsule(
        semantic.QueryHit(
            path="readwise://fixture",
            score=0.9,
            chunk_text="external",
            source="readwise",
            scope="external",
        ),
        "active",
    )
    expect(
        external_capsule["scope"] == "external",
        "external capsule was mislabeled with the requested local scope",
    )
    legacy_stub_json = io.StringIO()
    with contextlib.redirect_stdout(legacy_stub_json):
        semantic._emit_hits(
            [hits[0]],
            output_format="json",
            context=False,
            requested_scope="active",
            include_source=False,
        )
    expect(
        set(json.loads(legacy_stub_json.getvalue())[0])
        == {"path", "score", "matched_tokens"},
        "non-context stub JSON gained fields outside the legacy contract",
    )

    class FakeStats:
        total_chunks = 12
        model_name = "fixture"

    class FakeStore:
        @staticmethod
        def has_corpus_metadata() -> bool:
            return False

    class FakeRetriever:
        store = FakeStore()

        def query(
            self,
            _query: str,
            *,
            top_k: int,
            filters: dict[str, object],
        ) -> list[semantic_backends.SearchResult]:
            expect(top_k == 30 and filters == {"scope": "active"}, "probe drift")
            return [
                semantic_backends.SearchResult(
                    path="same.md",
                    score=0.9,
                    chunk_id=0,
                    chunk_text="first",
                ),
                semantic_backends.SearchResult(
                    path="same.md",
                    score=0.8,
                    chunk_id=1,
                    chunk_text="second",
                ),
                semantic_backends.SearchResult(
                    path="other.md",
                    score=0.7,
                    chunk_text="other",
                ),
            ]

        @staticmethod
        def stats() -> FakeStats:
            return FakeStats()

    efficiency = semantic._search_efficiency_report(
        FakeRetriever(),
        audit=audit,
        update={"mode": "fixture"},
    )
    expect(
        efficiency["corpus"]["default_scope_reduction_pct"] >= 0
        and efficiency["query_probe"]["queries"] == 3
        and efficiency["query_probe"]["duplicate_chunk_reduction_pct"] > 0,
        "post-index search efficiency report lost scope, latency, or dedup metrics",
    )


def check_autoevo_reliability() -> None:
    plist_path = ROOT / "scripts" / "launchd" / "com.atelier.autoevo-nightly.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    expect(
        plist.get("RunAtLoad") is True, "autoevo does not catch up on login or reload"
    )
    intervals = plist.get("StartCalendarInterval")
    expect(
        isinstance(intervals, dict)
        and intervals.get("Minute") == 0
        and "Hour" not in intervals,
        "autoevo must check deferred or missed cycles at the top of every hour",
    )

    class SleepingProcess:
        args = ["fixture"]

        def poll(self) -> None:
            return None

    clock = [100.0]

    def jump_after_sleep(_: float) -> None:
        clock[0] = 1000.0

    try:
        command_timeout.wait_until_deadline(
            SleepingProcess(), 110.0, now=lambda: clock[0], sleep=jump_after_sleep
        )
    except subprocess.TimeoutExpired:
        pass
    else:
        raise SmokeFailure("command timeout ignored a wall-clock jump across sleep")

    captured_semantic_env: dict[str, str] = {}
    original_preflight_run = autoevo_preflight._run

    def capture_semantic_run(
        command: list[str],
        *,
        cwd: Path,
        timeout: float = 30,
        env: dict[str, str] | None = None,
    ) -> autoevo_preflight.CommandResult:
        del command, cwd, timeout
        captured_semantic_env.update(env or {})
        return autoevo_preflight.CommandResult(0, "[]", "real mode: fixture")

    autoevo_preflight._run = capture_semantic_run
    try:
        semantic_readiness = autoevo_preflight._default_semantic_probe()
    finally:
        autoevo_preflight._run = original_preflight_run
    expect(
        semantic_readiness["ready"] is True
        and captured_semantic_env.get("HF_HUB_OFFLINE") == "1"
        and captured_semantic_env.get("TRANSFORMERS_OFFLINE") == "1",
        "autoevo semantic readiness probe can still attempt a model download",
    )

    with tempfile.TemporaryDirectory(prefix="atelier-autoevo-quarantine-") as temp_dir:
        temp = Path(temp_dir)
        state = temp / "autoevo_quarantine.toml"
        outcomes = temp / "outcomes.json"
        count_file = temp / "count.txt"
        state.write_text(
            "[[quarantine]]\n"
            'scope = "/expired"\n'
            'first_failed = "2098-12-01"\n'
            "consecutive_failures = 3\n"
            'reason = "forgetter_no_envelope"\n'
            'expires_at = "2099-01-02"\n',
            encoding="utf-8",
        )
        outcomes.write_text(
            json.dumps({"/expired": "forgetter_no_envelope"}),
            encoding="utf-8",
        )
        crossed = autoevo_quarantine.update_state(
            outcomes_path=outcomes,
            state_path=state,
            count_path=count_file,
            today=date(2099, 1, 2),
        )
        restarted = tomllib.loads(state.read_text(encoding="utf-8"))["quarantine"][0]
        expect(
            crossed == 0
            and count_file.read_text(encoding="utf-8").strip() == "0"
            and restarted["consecutive_failures"] == 1
            and restarted["first_failed"] == "2099-01-02"
            and restarted["expires_at"] == "2099-02-01",
            "post-expiry quarantine failure did not restart at one",
        )

        state.write_text(
            "[[quarantine]]\n"
            'scope = "/active"\n'
            'first_failed = "2099-01-01"\n'
            "consecutive_failures = 2\n"
            'reason = "forgetter_no_envelope"\n'
            'expires_at = "2099-02-01"\n',
            encoding="utf-8",
        )
        outcomes.write_text(
            json.dumps({"/active": "forgetter_no_envelope"}),
            encoding="utf-8",
        )
        crossed = autoevo_quarantine.update_state(
            outcomes_path=outcomes,
            state_path=state,
            count_path=count_file,
            today=date(2099, 1, 2),
        )
        active = tomllib.loads(state.read_text(encoding="utf-8"))["quarantine"][0]
        expect(
            crossed == 1 and active["consecutive_failures"] == 3,
            "quarantine threshold transition count drift",
        )

        boundary_state = temp / "boundary-quarantine.toml"
        boundary_state.write_text(
            "[[quarantine]]\n"
            'scope = "/boundary"\n'
            'first_failed = "2098-12-01"\n'
            "consecutive_failures = 3\n"
            'reason = "forgetter_no_envelope"\n'
            'expires_at = "2099-01-02"\n',
            encoding="utf-8",
        )
        expect(
            autoevo_quarantine.active_scopes(
                state_path=boundary_state,
                today=date(2099, 1, 1),
            )
            == ["/boundary"]
            and autoevo_quarantine.active_scopes(
                state_path=boundary_state,
                today=date(2099, 1, 2),
            )
            == [],
            "quarantine expiry does not follow the selected routine cycle date",
        )

        missing_outcomes = temp / "missing-outcomes.json"
        try:
            autoevo_quarantine.update_state(
                outcomes_path=missing_outcomes,
                state_path=state,
                count_path=count_file,
                today=date(2099, 1, 2),
            )
        except autoevo_quarantine.QuarantineError:
            pass
        else:
            raise SmokeFailure("quarantine update accepted a missing outcomes sidecar")

        malformed_state = temp / "malformed-quarantine.toml"
        malformed_state.write_text(
            "[[quarantine]]\n"
            'scope = "/malformed"\n'
            'first_failed = "not-a-date"\n'
            "consecutive_failures = 1\n"
            'reason = "forgetter_no_envelope"\n'
            'expires_at = "also-not-a-date"\n',
            encoding="utf-8",
        )
        try:
            autoevo_quarantine.update_state(
                outcomes_path=outcomes,
                state_path=malformed_state,
                count_path=count_file,
                today=date(2099, 1, 2),
            )
        except autoevo_quarantine.QuarantineError:
            pass
        else:
            raise SmokeFailure("quarantine update accepted malformed ISO dates")

        state_before_failed_write = state.read_text(encoding="utf-8")
        write_order: list[Path] = []
        original_quarantine_write = autoevo_quarantine._atomic_write

        def fail_authoritative_state_write(path: Path, text: str) -> None:
            write_order.append(path)
            if path == state:
                raise OSError("fixture state write failure")
            original_quarantine_write(path, text)

        autoevo_quarantine._atomic_write = fail_authoritative_state_write
        try:
            try:
                autoevo_quarantine.update_state(
                    outcomes_path=outcomes,
                    state_path=state,
                    count_path=count_file,
                    today=date(2099, 1, 2),
                )
            except OSError:
                pass
            else:
                raise SmokeFailure(
                    "quarantine update hid an authoritative write failure"
                )
        finally:
            autoevo_quarantine._atomic_write = original_quarantine_write
        expect(
            write_order == [count_file, state]
            and state.read_text(encoding="utf-8") == state_before_failed_write,
            "quarantine partial write advanced authoritative state before count evidence",
        )

        cleanup_target = temp / "cleanup-state.toml"
        cleanup_temporary = cleanup_target.with_name(
            f".{cleanup_target.name}.{os.getpid()}.tmp"
        )
        original_replace = autoevo_quarantine.os.replace

        def fail_replace(source: Path, destination: Path) -> None:
            del source, destination
            raise OSError("fixture replace failure")

        autoevo_quarantine.os.replace = fail_replace
        try:
            try:
                autoevo_quarantine._atomic_write(cleanup_target, "state\n")
            except OSError:
                pass
            else:
                raise SmokeFailure("quarantine atomic write hid a replace failure")
        finally:
            autoevo_quarantine.os.replace = original_replace
        expect(
            not cleanup_temporary.exists(),
            "quarantine atomic write left a hidden temporary file after failure",
        )

        audit = temp / "audit.md"
        skipped = temp / "quarantine-skipped.txt"
        audit.write_text(
            "## Autoevo Run: 2099-01-01 05:00\n\n"
            "### Skipped (reason)\n"
            "- older-run-entry\n\n"
            "### Errors\n"
            "- (none)\n\n"
            "## Autoevo Run: 2099-01-02 05:00\n\n"
            "### Skipped (reason)\n"
            "- (none)\n\n"
            "### Errors\n"
            "- (none)\n",
            encoding="utf-8",
        )
        skipped.write_text(
            "scope_quarantined: scope=/active (research-tier rotation)\n",
            encoding="utf-8",
        )
        inserted = autoevo_quarantine.insert_skipped(
            audit_path=audit,
            skipped_path=skipped,
        )
        after_first_insert = audit.read_text(encoding="utf-8")
        inserted_again = autoevo_quarantine.insert_skipped(
            audit_path=audit,
            skipped_path=skipped,
        )
        after_second_insert = audit.read_text(encoding="utf-8")
        latest_run = autoevo_verify._latest_run(after_second_insert)
        skipped_section = autoevo_verify._section(latest_run, "Skipped")
        errors_section = autoevo_verify._section(latest_run, "Errors")
        expect(
            inserted
            and not inserted_again
            and after_first_insert == after_second_insert
            and after_second_insert.count(
                "scope_quarantined: scope=/active (research-tier rotation)"
            )
            == 1
            and "older-run-entry"
            in after_second_insert.split("## Autoevo Run: 2099-01-02", maxsplit=1)[0]
            and "scope_quarantined: scope=/active" in skipped_section
            and "scope_quarantined" not in errors_section,
            "quarantine skip evidence was misplaced, duplicated, or rewrote history",
        )

    with tempfile.TemporaryDirectory(prefix="atelier-autoevo-preflight-") as temp_dir:
        vault = Path(temp_dir) / "vault"
        vault.mkdir()
        for segment in (
            "cache",
            "agent-findings",
            "wip",
            "research",
            "reflections",
            "_meta",
        ):
            (vault / segment).mkdir()

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                ["git", *args],
                cwd=vault,
                capture_output=True,
                text=True,
            )
            expect(
                result.returncode == 0,
                f"autoevo fixture git {' '.join(args)} failed: {result.stderr}",
            )
            return result

        git("init", "-q")
        git("config", "user.name", "Atelier Smoke")
        git("config", "user.email", "smoke@example.invalid")
        git("config", "commit.gpgsign", "false")
        (vault / ".gitignore").write_text("cache/\n_meta/\n", encoding="utf-8")
        note = vault / "wip" / "note.md"
        note.write_text("base\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", "base")

        original_ov = os.environ.get("OV")
        os.environ["OV"] = str(vault)
        _paths.vault_root.cache_clear()
        _paths._registry.cache_clear()
        try:

            def privacy_probe() -> dict[str, object]:
                return {"hit_count": 0}

            def semantic_probe() -> dict[str, object]:
                return {
                    "ready": True,
                    "mode": "real",
                    "duration_seconds": 0.01,
                }

            session_lock = vault / "cache" / "atelier-session-lock"
            original_run = autoevo_preflight._run
            status_commands: list[list[str]] = []

            def capture_status(command: list[str], **kwargs: object):
                status_commands.append(command)
                return original_run(command, **kwargs)

            autoevo_preflight._run = capture_status
            clean = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            autoevo_preflight._run = original_run
            expect(clean["ready"] is True, f"clean autoevo fixture blocked: {clean}")
            expect(
                any(
                    command[1:3] == ["--no-optional-locks", "status"]
                    for command in status_commands
                ),
                "autoevo status probe may create an optional Git index lock",
            )

            raw_index = git("rev-parse", "--git-path", "index").stdout.strip()
            index_path = Path(raw_index)
            if not index_path.is_absolute():
                index_path = vault / index_path
            index_path.unlink()
            missing = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                missing["gate"] == "git_index_missing",
                "autoevo misclassified a missing index as an ordinary dirty tree",
            )
            expect(
                missing["health"]["worktree_entries"] is None,
                "autoevo ran git status after detecting a missing index",
            )
            for invalid_run_date, invalid_cycle in (
                ("not-a-date", "2099-01-02"),
                ("2099-01-03", "2099-01-02"),
            ):
                try:
                    autoevo_preflight.record_blocker(
                        missing,
                        run_date=invalid_run_date,
                        run_ts="smoke-invalid-run-identity",
                        cycle=invalid_cycle,
                    )
                except autoevo_preflight.PreflightError:
                    pass
                else:
                    raise SmokeFailure(
                        "autoevo preflight accepted a noncanonical run identity"
                    )
            expect(
                not (
                    vault / "agent-findings" / "autoevo-applied-not-a-date.md"
                ).exists()
                and not (
                    vault / "agent-findings" / "autoevo-applied-2099-01-03.md"
                ).exists(),
                "invalid preflight identity created an audit artifact",
            )
            recorded = autoevo_preflight.record_blocker(
                missing,
                run_date="2099-01-02",
                run_ts="smoke-missing-index",
                cycle="2099-01-02",
            )
            expect(
                recorded["audit_commit"] == "deferred",
                "missing-index audit should remain checksum-owned until Git recovers",
            )
            git("read-tree", "HEAD")
            recovery = autoevo_preflight.recover_owned_audit()
            expect(
                recovery["status"] == "committed",
                f"managed audit did not recover after index repair: {recovery}",
            )
            expect(
                git("status", "--porcelain").stdout == "",
                "managed audit recovery left the clean fixture dirty",
            )

            raw_lock = git("rev-parse", "--git-path", "index.lock").stdout.strip()
            index_lock = Path(raw_lock)
            if not index_lock.is_absolute():
                index_lock = vault / index_lock
            index_lock.touch()
            locked = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                locked["gate"] == "git_index_lock_present",
                "autoevo did not diagnose a Git index lock precisely",
            )
            first_locked_record = autoevo_preflight.record_blocker(
                locked,
                run_date="2099-01-03",
                run_ts="smoke-locked-first",
                cycle="2099-01-03",
            )
            repeated_locked_record = autoevo_preflight.record_blocker(
                locked,
                run_date="2099-01-03",
                run_ts="smoke-locked-repeat",
                cycle="2099-01-03",
            )
            expect(
                first_locked_record["audit_commit"] == "deferred"
                and repeated_locked_record["audit_commit"] == "reused",
                "unchanged index-lock blocker audit was not reusable on retry",
            )
            index_lock.unlink()
            recovery = autoevo_preflight.recover_owned_audit()
            expect(
                recovery["status"] == "committed",
                f"index-lock blocker audit did not recover: {recovery}",
            )

            note.write_text("dirty\n", encoding="utf-8")
            dirty = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                now=1_000,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            # A dirty note the user is editing no longer stops the sweep; it
            # becomes untouchable for the run and is refused at commit time.
            # (2026-08-29: the scope gate blocked every run after a work day.)
            expect(
                dirty["ready"] is True
                and dirty["health"]["worktree_entries"] == 1
                and dirty["health"]["worktree_entries_in_scope"] == 1
                and dirty["health"]["protected_paths"],
                f"in-scope content dirt must protect, not block: {dirty.get('gate')}",
            )
            # Autoevo's own queue state is different: dirty there means the
            # queue condition is unknown, so the run must not start.
            # Production tracks `_meta/autoevo_*.toml`; this fixture ignores
            # `_meta/`, so force-track it or the state gate can never fire here.
            state_file = vault / "_meta" / "autoevo_pending.toml"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text("# base\n", encoding="utf-8")
            git("add", "-f", "--", "_meta/autoevo_pending.toml")
            git("commit", "-q", "-m", "track autoevo state")
            state_file.write_text("# smoke\n", encoding="utf-8")
            state_dirty = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                now=1_000,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                state_dirty["gate"] == "dirty_autoevo_state",
                f"dirty autoevo state must block: {state_dirty.get('gate')}",
            )
            state_file.write_text("# base\n", encoding="utf-8")
            # Dirt outside the sweep scopes must not block: the bot stages
            # explicit paths, so user edits elsewhere cannot leak into its
            # commits. (2026-08-22: a vault-wide gate blocked 73 of 103 runs.)
            (vault / "personal").mkdir(exist_ok=True)
            stray = vault / "personal" / "stray.md"
            stray.write_text("user edit in progress\n", encoding="utf-8")
            note.write_text("base\n", encoding="utf-8")
            out_of_scope = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                now=1_000,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                out_of_scope["ready"] is True
                and out_of_scope["health"]["worktree_entries"] == 1
                and out_of_scope["health"]["worktree_entries_in_scope"] == 0,
                f"out-of-scope dirt must not block autoevo: {out_of_scope.get('gate')}",
            )
            stray.unlink()
            note.write_text("base\n", encoding="utf-8")
            state_file.write_text("# dirty\n", encoding="utf-8")
            dirty = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                now=1_000,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                dirty["retry_after_epoch"]
                == 1_000 + autoevo_preflight.GENERIC_RETRY_DELAY_SECONDS,
                "non-session autoevo blocker did not retry on the next hourly check",
            )
            first_dirty_record = autoevo_preflight.record_blocker(
                dirty,
                run_date="2099-01-02",
                run_ts="smoke-dirty-first",
                cycle="2099-01-02",
            )
            expect(
                first_dirty_record["audit_commit"] == "committed",
                "first dirty-tree blocker audit was not committed path-locally",
            )
            blocker_audit = vault / "agent-findings" / "autoevo-applied-2099-01-02.md"
            blocker_text = blocker_audit.read_text(encoding="utf-8")
            blocker_head = git("rev-parse", "HEAD").stdout.strip()
            repeated_dirty_record = autoevo_preflight.record_blocker(
                dirty,
                run_date="2099-01-02",
                run_ts="smoke-dirty-repeat",
                cycle="2099-01-02",
            )
            expect(
                repeated_dirty_record["audit_commit"] == "reused"
                and blocker_audit.read_text(encoding="utf-8") == blocker_text
                and git("rev-parse", "HEAD").stdout.strip() == blocker_head,
                "identical deferred blocker produced a duplicate audit commit",
            )
            with blocker_audit.open("a", encoding="utf-8") as handle:
                handle.write("\nuser-owned audit edit\n")
            user_edited_audit = blocker_audit.read_text(encoding="utf-8")
            dirty_audit_record = autoevo_preflight.record_blocker(
                dirty,
                run_date="2099-01-02",
                run_ts="smoke-dirty-audit",
                cycle="2099-01-02",
            )
            committed_audit = git(
                "show",
                f"HEAD:{blocker_audit.relative_to(vault).as_posix()}",
            ).stdout
            expect(
                dirty_audit_record["audit_commit"] == "deferred"
                and "audit path already had uncommitted changes"
                in dirty_audit_record["audit_commit_detail"]
                and blocker_audit.read_text(encoding="utf-8") == user_edited_audit
                and "user-owned audit edit" not in committed_audit
                and git("rev-parse", "HEAD").stdout.strip() == blocker_head,
                "blocked autoevo run absorbed a pre-existing user audit edit",
            )
            blocker_audit.write_text(blocker_text, encoding="utf-8")
            note.write_text("base\n", encoding="utf-8")
            state_file.write_text("# base\n", encoding="utf-8")

            session_lock.touch()
            active = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                now=session_lock.stat().st_mtime,
                privacy_probe=privacy_probe,
                semantic_probe=semantic_probe,
            )
            expect(
                active["gate"] == "session_active",
                "autoevo session safety lock classification drift",
            )
            expect(
                active["retry_after_epoch"]
                == int(session_lock.stat().st_mtime)
                + autoevo_preflight.SESSION_LOCK_TTL_SECONDS
                + 1,
                "session-active retry does not align with lock expiry",
            )
            session_lock.unlink()
            semantic_blocked = autoevo_preflight.inspect_preflight(
                vault=vault,
                lock_path=session_lock,
                privacy_probe=privacy_probe,
                semantic_probe=lambda: {
                    "ready": False,
                    "mode": "real",
                    "duration_seconds": 0.02,
                    "detail": "fixture semantic failure",
                },
            )
            expect(
                semantic_blocked["gate"] == "semantic_unavailable"
                and semantic_blocked["health"]["semantic_ready"] is False,
                "autoevo did not fail closed on unavailable semantic search",
            )

            audit = vault / "agent-findings" / "autoevo-applied-2099-01-02.md"
            with audit.open("a", encoding="utf-8") as handle:
                handle.write(
                    "\n## Autoevo Run: 2099-01-02 11:00\n\n"
                    "### Skipped (reason)\n"
                    "- (none)\n\n"
                    "### Errors\n"
                    "- (none)\n"
                )
            cue, debug = cues.check_autoevo_ran(
                vault,
                date(2099, 1, 2),
                now=datetime(2099, 1, 2, 12, 0),
            )
            expect(
                cue is None,
                f"successful same-day retry did not supersede an earlier skip: {debug}",
            )

            deferred_claim = (
                vault
                / "_meta"
                / "routine_runs"
                / "autoevo-nightly"
                / "retry-fixture.toml"
            )
            deferred_claim.parent.mkdir(parents=True, exist_ok=True)
            deferred_claim.write_text(
                'routine = "autoevo-nightly"\n'
                'cycle_id = "retry-fixture"\n'
                'status = "deferred"\n',
                encoding="utf-8",
            )
            reserved, previous = routine_lock._reserve_local_cycle(
                "autoevo-nightly", "retry-fixture", 1
            )
            expect(
                reserved and previous == "deferred",
                "deferred autoevo claim was not safely reacquired",
            )
            deferred_claim.write_text(
                'routine = "autoevo-nightly"\n'
                'cycle_id = "retry-fixture"\n'
                'status = "deferred"\n'
                "retry_after_epoch = 200\n",
                encoding="utf-8",
            )
            waiting = routine_claim.schedule_decision(
                "autoevo-nightly", "retry-fixture", now_epoch=199
            )
            due = routine_claim.schedule_decision(
                "autoevo-nightly", "retry-fixture", now_epoch=200
            )
            expect(
                waiting["action"] == "skip"
                and waiting["reason"] == "deferred-retry-not-due",
                "deferred autoevo retry ignored its cooldown",
            )
            expect(
                due["action"] == "run" and due["reason"] == "deferred-retry-due",
                "deferred autoevo retry did not reopen when due",
            )

            local_zone = datetime.now().astimezone().tzinfo
            completed_previous = (
                vault / "_meta" / "routine_runs" / "autoevo-nightly" / "2026-07-25.toml"
            )
            completed_previous.write_text(
                'routine = "autoevo-nightly"\n'
                'cycle_id = "2026-07-25"\n'
                'status = "completed"\n',
                encoding="utf-8",
            )
            before_primary = routine_claim.select_scheduled_cycle(
                "autoevo-nightly",
                now=datetime(2026, 7, 26, 4, 30, tzinfo=local_zone),
            )
            expect(
                before_primary["action"] == "skip"
                and before_primary["cycle_id"] == "2026-07-25"
                and before_primary["reason"]
                == "previous-cycle-completed-before-primary",
                "pre-05:00 RunAtLoad duplicated a completed previous cycle",
            )
            completed_previous.write_text(
                'routine = "autoevo-nightly"\n'
                'cycle_id = "2026-07-25"\n'
                'status = "deferred"\n'
                "retry_after_epoch = 0\n",
                encoding="utf-8",
            )
            unresolved_previous = routine_claim.select_scheduled_cycle(
                "autoevo-nightly",
                now=datetime(2026, 7, 26, 4, 30, tzinfo=local_zone),
            )
            expect(
                unresolved_previous["action"] == "run"
                and unresolved_previous["cycle_id"] == "2026-07-25"
                and unresolved_previous["reason"] == "previous-cycle-unresolved",
                "pre-05:00 wake did not target the unresolved previous cycle",
            )
            after_primary = routine_claim.select_scheduled_cycle(
                "autoevo-nightly",
                now=datetime(2026, 7, 26, 5, 0, tzinfo=local_zone),
            )
            expect(
                after_primary["action"] == "run"
                and after_primary["cycle_id"] == "2026-07-26"
                and after_primary["reason"] == "primary-or-missed-current-cycle",
                "05:00 or later wake did not target the current missed cycle",
            )
            completed_previous.unlink()
            missing_previous = routine_claim.select_scheduled_cycle(
                "autoevo-nightly",
                now=datetime(2026, 7, 26, 4, 30, tzinfo=local_zone),
            )
            selected_cycle = routine_claim.validate_cycle_id(
                str(missing_previous["cycle_id"])
            )
            selected_audit = f"agent-findings/autoevo-applied-{selected_cycle}.md"
            selected_output = vault / selected_audit
            selected_output.write_text(
                "selected-cycle audit fixture\n",
                encoding="utf-8",
            )
            (vault / "_meta" / "routine_watch.toml").write_text(
                "[[routine]]\n"
                'name = "autoevo-nightly"\n'
                'execution = "local"\n'
                'output_dir = "agent-findings"\n'
                'file_pattern = "autoevo-applied-*.md"\n',
                encoding="utf-8",
            )
            selected_result = vault / "cache" / "selected-cycle-result.json"
            selected_result.write_text(
                json.dumps(
                    {
                        "routine": "autoevo-nightly",
                        "outcome": "delivered",
                        "output_file": selected_audit,
                        "summary": "selected-cycle fixture",
                        "skipped_inputs": [],
                    }
                ),
                encoding="utf-8",
            )
            selected_claimed_at = (
                datetime.now().astimezone() - timedelta(seconds=1)
            ).isoformat()
            selected_attestation = routine_result.verify_result(
                "autoevo-nightly",
                selected_cycle,
                selected_claimed_at,
                selected_result,
            )
            selected_verified_output = autoevo_verify._output_path(
                vault.resolve(),
                selected_attestation["output_file"],
                selected_cycle,
            )
            expect(
                missing_previous["action"] == "run"
                and selected_cycle == "2026-07-25"
                and Path(selected_audit).name == f"autoevo-applied-{selected_cycle}.md"
                and Path(selected_audit).parent.as_posix() == "agent-findings"
                and selected_attestation["cycle_id"] == selected_cycle
                and selected_verified_output == selected_output.resolve()
                and missing_previous["reason"] == "missed-previous-cycle",
                "pre-05:00 cycle diverged across selection, result, or verifier",
            )
        finally:
            if original_ov is None:
                os.environ.pop("OV", None)
            else:
                os.environ["OV"] = original_ov
            _paths.vault_root.cache_clear()
            _paths._registry.cache_clear()

    with tempfile.TemporaryDirectory(prefix="atelier-autoevo-verify-") as temp_dir:
        vault = Path(temp_dir) / "vault"
        audit_dir = vault / "agent-findings"
        cache_dir = vault / "cache"
        claim_dir = vault / "_meta" / "routine_runs" / "autoevo-nightly"
        audit_dir.mkdir(parents=True)
        cache_dir.mkdir(parents=True)
        claim_dir.mkdir(parents=True)

        def verify_git(*args: str) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                ["git", *args],
                cwd=vault,
                capture_output=True,
                text=True,
            )
            expect(
                result.returncode == 0,
                f"autoevo verifier git {' '.join(args)} failed: {result.stderr}",
            )
            return result

        verify_git("init", "-q")
        verify_git("config", "user.name", "Atelier Smoke")
        verify_git("config", "user.email", "smoke@example.invalid")
        verify_git("config", "commit.gpgsign", "false")
        (vault / ".gitignore").write_text("_meta/\ncache/\n", encoding="utf-8")
        verify_git("add", ".gitignore")
        verify_git("commit", "-q", "-m", "fixture base")
        audit = audit_dir / "autoevo-applied-2099-01-03.md"
        decay_reports = [
            audit_dir / "decay-20990103-070000-research.md",
            audit_dir / "decay-20990103-070000-wip.md",
            audit_dir / "decay-20990103-070000-reflections.md",
        ]
        for report in decay_reports:
            report.write_text("fixture decay report\n", encoding="utf-8")
        audit.write_text(
            """## Autoevo Run: 2099-01-03 07:00

Run ID: 20990103-070000

### Sweep coverage (3)
- /fixture/research: envelope_returned
- /fixture/wip: envelope_returned
- /fixture/reflections: envelope_returned

### Sweep reports (3)
- agent-findings/decay-20990103-070000-research.md
- agent-findings/decay-20990103-070000-wip.md
- agent-findings/decay-20990103-070000-reflections.md

### Auto-applied (0)
- (none)

### Logged to pending queue (0)
- (none)

### Contradicted rhetorical dismissals (0)
- (none)

### Lint
- ERROR: 0, WARN: 0, INFO: 0

### Notes
- forgetter_partial: scope=/fixture/research, candidates_evaluated=15, reason=max_candidates

### Skipped (reason)
- (none)

### Errors
- (none)
""",
            encoding="utf-8",
        )
        quarantine = vault / "_meta" / "autoevo_quarantine.toml"
        quarantine.write_text(
            "[[quarantine]]\n"
            "scope = '/fixture/research'\n"
            "first_failed = '2098-12-01'\n"
            "consecutive_failures = 3\n"
            "reason = 'forgetter_no_envelope'\n"
            "expires_at = '2099-02-01'\n",
            encoding="utf-8",
        )
        output_paths = [
            audit.relative_to(vault).as_posix(),
            *(report.relative_to(vault).as_posix() for report in decay_reports),
            quarantine.relative_to(vault).as_posix(),
        ]
        verify_git(
            "add",
            audit.relative_to(vault).as_posix(),
            *(report.relative_to(vault).as_posix() for report in decay_reports),
        )
        verify_git("add", "-f", quarantine.relative_to(vault).as_posix())
        verify_git(
            "commit",
            "-q",
            "--only",
            "-m",
            "[autoevo:audit] smoke",
            "--",
            *output_paths,
        )
        expect(
            verify_git("status", "--porcelain").stdout == "",
            "exact autoevo evidence commit left the fixture dirty",
        )
        audit_commit = verify_git("rev-parse", "HEAD").stdout.strip()
        wrapper_log = cache_dir / "autoevo-runner-2099-01-03.log.smoke1"
        claim = claim_dir / "2099-01-03.toml"
        claim.write_text(
            f"""routine = "autoevo-nightly"
cycle_id = "2099-01-03"
claimed_at = "2099-01-03T07:00:00-08:00"
status = "completed"
completed_at = "2099-01-03T07:05:00-08:00"
duration_seconds = 300
outcome = "delivered"
output_file = "agent-findings/autoevo-applied-2099-01-03.md"
event_log = "cache/autoevo-runner-2099-01-03.log.smoke1"
verification = "passed"
verified_at = "2099-01-03T07:05:01-08:00"
verified_sweeps = 3
verification_commit = "{audit_commit}"
""",
            encoding="utf-8",
        )
        (cache_dir / "autoevo-20990103-070000-outcomes.json").write_text(
            json.dumps(
                {
                    "/fixture/research": "envelope_returned",
                    "/fixture/wip": "envelope_returned",
                    "/fixture/reflections": "envelope_returned",
                }
            ),
            encoding="utf-8",
        )
        (cache_dir / "autoevo-20990103-070000-lint.json").write_text(
            json.dumps(
                {
                    "counts": {"error": 0, "warn": 0, "info": 0},
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        wrapper_log.write_text(
            """[2099-01-03T07:00:00-08:00] claimed: /fixture/2099-01-03.toml
[2099-01-03T07:00:01-08:00] deterministic autoevo preflight passed
[2099-01-03T07:00:02-08:00] starting: runtime=codex command=/autoevo-nightly
[2099-01-03T07:04:59-08:00] delivery validated: outcome=delivered output=agent-findings/autoevo-applied-2099-01-03.md
[2099-01-03T07:05:00-08:00] finished: status=completed duration=300s
[2099-01-03T07:05:00-08:00] lock release: {"released": true}
[2099-01-03T07:05:01-08:00] post-run verification passed: sweeps=3 commit=fixture
[2099-01-03T07:05:02-08:00] done: claim updated, lock released
""",
            encoding="utf-8",
        )
        verified = autoevo_verify.verify_cycle(
            "2099-01-03",
            vault=vault,
            wrapper_log=wrapper_log,
        )
        expect(
            verified["verified"] is True and verified["sweeps_completed"] == 3,
            "autoevo verifier rejected complete production evidence",
        )
        mismatched_audit = audit_dir / "autoevo-applied-2099-01-04.md"
        mismatched_audit.write_text("wrong cycle\n", encoding="utf-8")
        try:
            autoevo_verify._output_path(
                vault,
                "agent-findings/autoevo-applied-2099-01-04.md",
                "2099-01-03",
            )
        except autoevo_verify.VerificationError:
            pass
        else:
            raise SmokeFailure(
                "autoevo verifier accepted an audit path from another cycle"
            )
        mismatched_audit.unlink()
        completed_claim = claim.read_text(encoding="utf-8")
        claim.write_text(
            completed_claim.replace(
                'status = "completed"', 'status = "completion-uncertain"'
            ).replace('verification = "passed"', 'verification = "pending"'),
            encoding="utf-8",
        )
        try:
            autoevo_verify.verify_cycle(
                "2099-01-03",
                vault=vault,
                wrapper_log=wrapper_log,
            )
        except autoevo_verify.VerificationError:
            pass
        else:
            raise SmokeFailure("autoevo verifier accepted a pending external claim")
        pending = autoevo_verify.verify_cycle(
            "2099-01-03",
            vault=vault,
            wrapper_log=wrapper_log,
            allow_pending_claim=True,
        )
        expect(
            pending["verified"] is True,
            "autoevo verifier rejected its internal pending claim",
        )
        claim.write_text(completed_claim, encoding="utf-8")
        claim.write_text(
            claim.read_text(encoding="utf-8").replace(
                'outcome = "delivered"', 'outcome = "noop"'
            ),
            encoding="utf-8",
        )
        try:
            autoevo_verify.verify_cycle(
                "2099-01-03",
                vault=vault,
                wrapper_log=wrapper_log,
            )
        except autoevo_verify.VerificationError:
            pass
        else:
            raise SmokeFailure("autoevo verifier accepted a preflight noop")

        claim.write_text(completed_claim, encoding="utf-8")
        decay_reports[0].write_text(
            "fixture decay report changed after audit commit\n",
            encoding="utf-8",
        )
        verify_git("add", decay_reports[0].relative_to(vault).as_posix())
        verify_git("commit", "-q", "-m", "drift one decay report")
        try:
            autoevo_verify.verify_cycle(
                "2099-01-03",
                vault=vault,
                wrapper_log=wrapper_log,
            )
        except autoevo_verify.VerificationError:
            pass
        else:
            raise SmokeFailure(
                "autoevo verifier accepted a report outside the audit commit"
            )


def check_codex_routine_runner() -> None:
    runner_path = ROOT / "scripts" / "routine_runner.sh"
    runner = runner_path.read_text(encoding="utf-8")
    profile_smoke_path = ROOT / "scripts" / "routine_profile_smoke.sh"
    profile_smoke = profile_smoke_path.read_text(encoding="utf-8")
    permission_smoke_path = ROOT / "scripts" / "routine_permission_smoke.sh"
    permission_smoke = permission_smoke_path.read_text(encoding="utf-8")
    autoevo = (ROOT / ".claude" / "commands" / "autoevo-nightly.md").read_text(
        encoding="utf-8"
    )
    autoevo_verifier = (ROOT / "scripts" / "autoevo_verify.py").read_text(
        encoding="utf-8"
    )
    required_fragments = (
        'python3 "$SCRIPTS_DIR/routine_owner.py" check --json',
        'python3 "$SCRIPTS_DIR/routine_audit.py" resolve "$ROUTINE"',
        '--command "$COMMAND"',
        'python3 "$SCRIPTS_DIR/routine_prompt_guard.py" "$prompt_file"',
        'export ATELIER_ACTIVE_RUNTIME="$RUNTIME"',
        "harness/commands.toml",
        "LOCK_CMD=(uv run",
        'command_timeout.py" --seconds "$ROUTINE_TIMEOUT_SECONDS"',
        # Host-readiness gate: wake-triggered catch-up runs used to hang for the
        # whole budget instead of failing, so the gate must stay ahead of the
        # model launch and must defer rather than proceed.
        'READINESS_TIMEOUT_SECONDS="${ATELIER_READINESS_TIMEOUT_SECONDS:-120}"',
        "READINESS_BLOCKER=\"vault-unreadable\"",
        'READINESS_BLOCKER="network-unreachable"',
        'status = "deferred"',
        "--ask-for-approval never exec",
        "--ignore-user-config",
        '--sandbox "$CODEX_SANDBOX"',
        "--dangerously-bypass-hook-trust",
        "--ephemeral",
        'web_search="disabled"',
        "sandbox_workspace_write.network_access=true",
        "sandbox_workspace_write.network_access=false",
        'approval_policy="never"',
        "codex_global_args=(",
        'codex_exec_args=(--ignore-user-config "${codex_exec_args[@]}")',
        '"ATELIER_ROUTINE_PROFILE=$ROUTINE_PROFILE"',
        '"ATELIER_ROUTINE_CYCLE=$CYCLE"',
        '"ZDOTDIR=$ATELIER_DIR/harness/routine-shell"',
        "finalize_unexpected_exit",
        'RUNTIME="codex"',
        # Runtime fallback: declared per profile, decided deterministically,
        # never on a timeout, executed under Claude Code's own fences.
        "FALLBACK_RUNTIME",
        'routine_fallback.py" decide',
        'routine_fallback.py" extract',
        "run_claude()",
        "--permission-mode dontAsk",
        '--setting-sources ""',
        "--strict-mcp-config",
        '"Edit(/$OV/**)"',
        # Claude's --json-schema validator rejects the draft `$schema` URI the
        # shared result schema carries; the first e2e fallback run died on it.
        'd.pop("$schema", None)',
        'fallback_from = "%s"',
        # Successful transcripts are kept too; a delivered-but-wrong report
        # was unauditable when only failures were preserved.
        'KEPT_LOG=$(preserve_model_log "$MODEL_LOG")',
        '--skip-git-repo-check --add-dir "$OV" -C "$ROUTINE_CWD"',
        "atelier-routine-cwd.XXXXXX",
        "ATELIER_ACCESS_MODE",
        "PROFILE_FINGERPRINT",
        "PERMISSION_ALLOWLIST",
        '"ATELIER_ROUTINE_PERMISSIONS=$PERMISSION_ALLOWLIST"',
        "CURRENT_OWNER_GENERATION",
        "OWNER_GENERATION=${OWNER_GENERATION:-0}",
        "LOCK_RETRY_AUTHORIZED",
        "invalid-lock-contention-result",
        "unknown-canonical-claim-status",
        'routine_claim.py" "$ROUTINE" --cycle "$CYCLE"',
        'model_reasoning_effort=\\"$REASONING_EFFORT\\"',
        'caffeinate -i -w "$$"',
        "--output-schema",
        "--output-last-message",
        'routine_result.py" "$ROUTINE"',
        "delivery-attestation-failed",
        "env -i",
        'autoevo_preflight.py"',
        '--run-date "$CYCLE"',
        "FAST_AUDIT_COMMIT",
        'FAST_AUDIT_COMMIT" = "reused"',
        '--cycle "$CYCLE"',
        'autoevo_verify.py"',
        "--allow-pending-claim",
        'verification = "pending"',
        'verification = "passed"',
        "post-run-verification-failed",
        "autoevo-runner-${CYCLE}.log.XXXXXX",
        'status = "deferred"',
        'routine_claim.py" "$ROUTINE" --select-cycle',
        '--validate-cycle "$CYCLE"',
        "scheduled cycle selected",
    )
    for fragment in required_fragments:
        expect(
            fragment in runner,
            f"routine runner missing Codex contract fragment: {fragment}",
        )
    for fragment in (
        "DECAY_REPORT_RELS=()",
        'FINAL_COMMIT_PATHS=("$AUDIT_REL")',
        '--force-add _meta/autoevo_quarantine.toml',
        "scripts/autoevo_run.py plan",
        "scripts/autoevo_run.py outcome",
        "scripts/autoevo_run.py tombstone-check",
        "scripts/autoevo_run.py snapshot",
        "scripts/autoevo_run.py stage-merge",
        "scripts/autoevo_run.py rollback",
        "scripts/autoevo_quarantine.py update",
        "scripts/autoevo_quarantine.py insert-skipped",
        "### Sweep reports (<S>)",
    ):
        expect(
            fragment in autoevo,
            f"autoevo command cannot persist verifier-required evidence: {fragment}",
        )
    run_helper = (ROOT / "scripts" / "autoevo_run.py").read_text(encoding="utf-8")
    for fragment in ("active_scopes(", "cluster_hash(", "autoevo_scope_prefixes"):
        expect(
            fragment in run_helper,
            f"autoevo_run.py lost its single-owner delegation: {fragment}",
        )
    expect(
        "| 2: Per-step budget | demote dispatch | Notes (`forgetter_partial: ...`) |"
        in autoevo
        and "Do not put a returned partial envelope in § Skipped or § Errors."
        in autoevo
        and 'note "partial sweep on `<scope>`" in audit log § "Errors"' not in autoevo,
        "bounded partial Forgetter envelopes can still poison completion verification",
    )
    expect(
        'git -C "$OV" restore .' not in autoevo,
        "autoevo failure recovery may not restore the whole user worktree",
    )
    expect(
        'cat "$QUARANTINE_SKIPPED" >> "$AUDIT_LOG_PATH"' not in autoevo,
        "autoevo quarantine evidence can escape the Skipped section",
    )
    expect(
        autoevo.count('--today "$RUN_DATE"') >= 2
        and 'for q in data.get("quarantine"' not in autoevo,
        "autoevo quarantine filtering and updates do not share RUN_DATE",
    )
    expect(
        "RUN_DATE=$(python3 scripts/routine_claim.py autoevo-nightly \\\n" in autoevo
        and '--validate-cycle "$ATELIER_ROUTINE_CYCLE")' in autoevo
        and "unattended invocation omitted ATELIER_ROUTINE_CYCLE" in autoevo
        and 'path.name != f"autoevo-applied-{cycle}.md"' in autoevo_verifier,
        "selected cycle does not control command, audit, and verifier identity",
    )
    expect(
        "owner_generation = $OWNER_GENERATION" in runner
        and 'owner_generation = "$OWNER_GENERATION"' not in runner,
        "routine runner does not emit owner_generation as a TOML integer",
    )
    expect(
        'atelier_runtime.py" resolve' not in runner,
        "unattended runner must not inherit the interactive runtime selection",
    )
    expect(
        runner.index('python3 "$SCRIPTS_DIR/routine_audit.py" resolve "$ROUTINE"')
        < runner.index('LOCK_RESULT=$("${LOCK_WITH_TIMEOUT[@]}" acquire'),
        "routine capability preflight must run before acquiring the lock",
    )
    expect(
        "claude -p" not in runner, "unattended routine runner must not execute Claude"
    )
    expect(
        'cat > "$CLAIM_FILE"' not in runner, "canonical claims must use atomic writes"
    )
    expect(
        "$autoevo-nightly" not in runner,
        "bot-only autoevo must not become a Codex user skill",
    )
    expect(
        'scripts/autoevo_commit.py' in autoevo,
        "autoevo audit commits must not absorb a dirty pre-flight index",
    )
    expect(
        '"routine": "autoevo-nightly"' in autoevo
        and '"output_file": "agent-findings/autoevo-applied-<RUN_DATE>.md"' in autoevo,
        "autoevo must return the structured delivery result",
    )

    invalid = subprocess.run(
        ["bash", str(runner_path), "../escape", "/autoevo-nightly"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(invalid.returncode == 2, "routine runner must reject unsafe routine names")

    result = subprocess.run(
        ["bash", "-n", str(runner_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(
        result.returncode == 0, f"routine runner shell syntax failed: {result.stderr}"
    )
    for fragment in (
        'routine_owner.py" check',
        'resolve "$SMOKE_ROUTINE" --surface local --check-system --runtime codex',
        '--command "$SMOKE_COMMAND"',
        'connector_access = "not-exercised"',
        'approval_policy = "never"',
        'shell_network = "$SHELL_NETWORK_MODE"',
        'launcher = "$LAUNCHD_LABEL"',
        "com.atelier.profile-smoke.*",
        'approval_policy="never"',
        "env -i",
        "--ask-for-approval never exec",
        "ATELIER_PROFILE_SMOKE_OK",
        'profile_fingerprint = "$PROFILE_FINGERPRINT"',
        'atelier_access = "$ATELIER_ACCESS_MODE"',
    ):
        expect(
            fragment in profile_smoke,
            f"profile smoke missing contract fragment: {fragment}",
        )
    result = subprocess.run(
        ["bash", "-n", str(profile_smoke_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(
        result.returncode == 0, f"profile smoke shell syntax failed: {result.stderr}"
    )
    direct_smoke = subprocess.run(
        ["bash", str(profile_smoke_path), "autoevo-nightly"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"XPC_SERVICE_NAME", "ATELIER_PROFILE_SMOKE_LAUNCHER"}
        },
    )
    expect(direct_smoke.returncode == 2, "interactive profile smoke must fail closed")
    for fragment in (
        "com.atelier.permission-smoke.*",
        "ATELIER_PERMISSION_SMOKE_AUTHORIZED",
        "gmail:read|readwise:create-document",
        "sandbox_workspace_write.network_access=true",
        'kind = "external-permission"',
        "user_authorized = true",
        'verification = "model-reported"',
        'approval_policy = "never"',
        "env -i",
        "--ask-for-approval never exec",
        'profile_fingerprint = "$PROFILE_FINGERPRINT"',
        'atelier_access = "$ATELIER_ACCESS_MODE"',
    ):
        expect(
            fragment in permission_smoke,
            f"permission smoke missing contract fragment: {fragment}",
        )
    result = subprocess.run(
        ["bash", "-n", str(permission_smoke_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(
        result.returncode == 0, f"permission smoke shell syntax failed: {result.stderr}"
    )
    direct_permission_smoke = subprocess.run(
        ["bash", str(permission_smoke_path), "sample", "gmail:read"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(
        direct_permission_smoke.returncode == 2,
        "interactive permission smoke must fail closed",
    )


def check_routine_profiles() -> None:
    profiles_path = ROOT / "harness" / "routine_profiles.toml"
    with profiles_path.open("rb") as handle:
        profiles = tomllib.load(handle)["profiles"]
    expect(
        profiles["local-maintenance"]["sandbox"] == "danger-full-access",
        "maintenance profile drift",
    )
    expect(
        profiles["local-maintenance"]["atelier_access"] == "read-write",
        "maintenance Atelier access drift",
    )
    expect(
        profiles["local-maintenance"]["allowed_commands"] == ["/autoevo-nightly"],
        "maintenance command binding drift",
    )
    expect(
        profiles["local-research"]["sandbox"] == "workspace-write",
        "research sandbox drift",
    )
    expect(
        profiles["local-research"]["atelier_access"] == "read",
        "research Atelier access drift",
    )
    expect(
        profiles["local-research"]["allowed_commands"] == ["/run-routine"],
        "ordinary routine command binding drift",
    )
    expect(
        profiles["local-research"]["web_search"] == "live", "research web policy drift"
    )
    expect(
        profiles["local-research"]["shell_network"] == "disabled",
        "research shell network drift",
    )
    expect(
        profiles["local-synthesis"]["web_search"] == "disabled",
        "synthesis web policy drift",
    )
    expect(
        profiles["local-digest"]["shell_network"] == "disabled",
        "digest shell network drift",
    )
    expect(
        profiles["local-gmail-synthesis"]["user_config"] == "required",
        "connector profile must retain user config",
    )

    with tempfile.TemporaryDirectory(prefix="atelier-routine-profile-") as temp_dir:
        vault = Path(temp_dir)
        meta = vault / "_meta"
        meta.mkdir()
        watch = meta / "routine_watch.toml"
        watch.write_text(
            """
[[routine]]
name = "sample"
support = "hybrid"
local_profile = "local-research"
cloud_profile = "cloud-drive-research"
execution = "local"
cron = "0 5 * * *"
output_dir = "sample"
file_pattern = "*.md"
label = "sample"
""".lstrip(),
            encoding="utf-8",
        )
        env = os.environ | {"OV": str(vault)}
        audit = subprocess.run(
            [PYTHON, "scripts/routine_audit.py", "audit", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            audit.returncode == 0,
            f"routine profile audit failed: {audit.stderr}{audit.stdout}",
        )
        payload = json.loads(audit.stdout)
        expect(payload["counts"]["hybrid"] == 1, "hybrid routine count drift")

        claim_dir = meta / "routine_runs" / "sample"
        claim_dir.mkdir(parents=True)
        claim_path = claim_dir / "cycle.toml"
        claim_prefix = (
            'routine = "sample"\n'
            'cycle_id = "cycle"\n'
            f'machine = "{os.uname().nodename}"\n'
            "contract_version = 2\n"
            'profile = "local-research"\n'
            "profile_fingerprint = "
            + json.dumps(
                routine_audit._profile_fingerprint(
                    "local-research",
                    profiles["local-research"],
                )
            )
            + "\n"
            'runtime = "codex"\n'
            'status = "completed"\n'
            'completed_at = "2099-01-01T00:00:00+00:00"\n'
        )
        claim_path.write_text(
            claim_prefix + 'owner_generation = "invalid"\n',
            encoding="utf-8",
        )
        evidence_record = {
            "name": "sample",
            "selected_profile": "local-research",
            "permissions": [],
        }
        previous_ov = os.environ.get("OV")
        os.environ["OV"] = str(vault)
        try:
            invalid_evidence = routine_audit._background_evidence(
                [evidence_record],
                profiles,
            )
            expect(
                invalid_evidence["verified"] is False,
                "routine audit accepted evidence rejected by the canonical claim validator",
            )
            claim_path.write_text(
                claim_prefix + 'owner_generation = "3"\n',
                encoding="utf-8",
            )
            legacy_evidence = routine_audit._background_evidence(
                [evidence_record],
                profiles,
            )
            expect(
                legacy_evidence["verified"] is True
                and "local-research" in legacy_evidence["verified_profiles"],
                "routine audit rejected legacy numeric-string claim evidence",
            )
            claim_path.write_text(
                claim_prefix + "owner_generation = 0\n",
                encoding="utf-8",
            )
            valid_evidence = routine_audit._background_evidence(
                [evidence_record],
                profiles,
            )
        finally:
            if previous_ov is None:
                os.environ.pop("OV", None)
            else:
                os.environ["OV"] = previous_ov
        expect(
            valid_evidence["verified"] is True
            and "local-research" in valid_evidence["verified_profiles"],
            "routine audit rejected canonical numeric owner-generation evidence",
        )

        local = subprocess.run(
            [
                PYTHON,
                "scripts/routine_audit.py",
                "resolve",
                "sample",
                "--surface",
                "local",
                "--command",
                "/run-routine sample",
                "--format",
                "tsv",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(local.returncode == 0, f"local profile resolve failed: {local.stderr}")
        fields = local.stdout.strip().split("\t")
        expect(
            fields[:8]
            == [
                "local-research",
                "workspace-write",
                "read",
                "live",
                "disabled",
                "ignore",
                "1800",
                "medium",
            ],
            "local profile TSV contract drift",
        )
        expect(
            len(fields) == 11 and len(fields[8]) == 64, "profile fingerprint missing"
        )
        expect(
            fields[10] == "claude",
            "local-research must declare the claude fallback runtime in TSV column 11",
        )
        expect(
            fields[9] == "atelier:read,vault:read-write,web:live",
            "routine action allowlist missing from execution contract",
        )

        forbidden_command = subprocess.run(
            [
                PYTHON,
                "scripts/routine_audit.py",
                "resolve",
                "sample",
                "--surface",
                "local",
                "--command",
                "/autoevo-nightly",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            forbidden_command.returncode == 2,
            "ordinary routine selected a maintenance-only command",
        )

        cloud = subprocess.run(
            [
                PYTHON,
                "scripts/routine_audit.py",
                "resolve",
                "sample",
                "--surface",
                "cloud",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(cloud.returncode == 0, f"cloud profile resolve failed: {cloud.stderr}")
        expect(
            json.loads(cloud.stdout)["required_connectors"] == ["google-drive"],
            "cloud connector contract drift",
        )

        prompt_dir = vault / "_routine_prompts"
        prompt_dir.mkdir()
        (prompt_dir / "sample.md").write_text(
            """LOCAL EXECUTION OVERRIDE

--- ORIGINAL ROUTINE PROMPT (verbatim; follow for analysis content) ---

Write the canonical report to Google Drive under `zk/sample/`.
Run `date` via Bash first, then create a Gmail draft and save to Readwise.
""",
            encoding="utf-8",
        )
        bundle_dir = vault / "bundle"
        bundle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_cloud_bundle.py",
                "--output",
                str(bundle_dir),
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(bundle.returncode == 0, f"cloud bundle failed: {bundle.stderr}")
        bundle_payload = json.loads(bundle.stdout)
        expect(bundle_payload["prompts"] == 1, "cloud bundle prompt count drift")
        generated = (bundle_dir / "prompts" / "sample.md").read_text(encoding="utf-8")
        expect(
            "CHATGPT SCHEDULED TASK ADAPTER" in generated,
            "cloud adapter header missing",
        )
        expect(
            "Effective permission allowlist: google-drive:read-write, web:live"
            in generated,
            "cloud permission boundary missing",
        )
        expect(
            "These rules override any incompatible instruction" in generated,
            "cloud adapter precedence missing",
        )
        expect(
            "LOCAL EXECUTION OVERRIDE" not in generated,
            "local adapter leaked into cloud prompt",
        )
        expect("Google Drive" in generated, "cloud prompt lost authoritative procedure")
        manifest = json.loads(
            (bundle_dir / "manifest.json").read_text(encoding="utf-8")
        )
        expect(manifest["version"] == 3, "cloud bundle manifest contract drift")
        expect(
            manifest["routines"][0]["chatgpt_scheduled"] is False,
            "local routine was misreported as active in ChatGPT Scheduled",
        )
        adaptations = manifest["routines"][0]["adaptations"]
        expect(
            "local shell and CLI instructions are disabled" in adaptations,
            "cloud bundle did not disclose local-shell adaptation",
        )
        expect(
            "Gmail reads and mutations are disabled by the permission profile"
            in adaptations,
            "cloud bundle did not disclose Gmail permission override",
        )
        expect(
            "Readwise reads and mutations are disabled by the permission profile"
            in adaptations,
            "cloud bundle did not disclose Readwise permission override",
        )

        public_bundle = ROOT / "harness" / "cloud-bundle-public-smoke"
        rejected_bundle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_cloud_bundle.py",
                "--output",
                str(public_bundle),
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            rejected_bundle.returncode == 2,
            "cloud bundle accepted a public repo target",
        )
        expect(
            not public_bundle.exists(), "rejected cloud bundle created a repo directory"
        )

        malformed_bundle_dir = vault / "malformed-bundle"
        (prompt_dir / "sample.md").write_text(
            "LOCAL EXECUTION OVERRIDE\nmissing boundary\n",
            encoding="utf-8",
        )
        malformed_bundle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_cloud_bundle.py",
                "--output",
                str(malformed_bundle_dir),
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            malformed_bundle.returncode == 2, "cloud bundle accepted malformed archive"
        )
        expect(
            not malformed_bundle_dir.exists(), "malformed cloud bundle created output"
        )

        valid_watch = watch.read_text(encoding="utf-8")
        watch.write_text(
            valid_watch.replace(
                'cloud_profile = "cloud-drive-research"',
                'cloud_profile = "missing-cloud-profile"',
            ),
            encoding="utf-8",
        )
        invalid_profile_dir = vault / "invalid-profile-bundle"
        invalid_profile_bundle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_cloud_bundle.py",
                "--output",
                str(invalid_profile_dir),
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            invalid_profile_bundle.returncode == 2,
            "cloud bundle accepted invalid profile",
        )
        expect(
            not invalid_profile_dir.exists(),
            "invalid cloud profile left a partial bundle",
        )
        watch.write_text(valid_watch, encoding="utf-8")

        watch.write_text(
            watch.read_text(encoding="utf-8").replace(
                'support = "hybrid"', 'support = "local-only"'
            ),
            encoding="utf-8",
        )
        invalid = subprocess.run(
            [PYTHON, "scripts/routine_audit.py", "audit", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(invalid.returncode == 2, "conflicting cloud profile was not rejected")

    timeout = subprocess.run(
        [
            PYTHON,
            "scripts/command_timeout.py",
            "--seconds",
            "0.05",
            "--",
            PYTHON,
            "-c",
            "import time; time.sleep(2)",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(timeout.returncode == 124, "routine command timeout did not fail closed")


def check_routine_owner() -> None:
    owner_script = ROOT / "scripts" / "routine_owner.py"
    lock_script = ROOT / "scripts" / "routine_lock.py"
    guard_script = ROOT / "scripts" / "routine_prompt_guard.py"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        ov = temp / "vault"
        meta = ov / "_meta"
        meta.mkdir(parents=True)
        watch = meta / "routine_watch.toml"
        watch.write_text('[coordination]\nbackend = "none"\n', encoding="utf-8")
        identity = temp / "machine.local.toml"
        shared_owner = meta / "routine_owner.toml"
        env = os.environ.copy()
        env.update(
            {
                "OV": str(ov),
                "ATELIER_ROUTINE_IDENTITY_FILE": str(identity),
                "ATELIER_ROUTINE_OWNER_FILE": str(shared_owner),
            }
        )

        claim = subprocess.run(
            [PYTHON, str(owner_script), "claim", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            claim.returncode == 0,
            f"routine owner claim failed: {claim.stderr}{claim.stdout}",
        )
        expect(
            'backend = "owner"' in watch.read_text(encoding="utf-8"),
            "claim did not enable owner backend",
        )

        status = subprocess.run(
            [PYTHON, str(owner_script), "status", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(status.returncode == 0, f"routine owner status failed: {status.stderr}")
        status_payload = json.loads(status.stdout)
        expect(status_payload["eligible"] is True, "claiming machine is not eligible")
        expect(status_payload["generation"] == 1, "initial owner generation drift")

        matching_identity = identity.read_text(encoding="utf-8")
        identity.write_text(
            'version = 1\nmachine_id = "other-machine"\nmachine_label = "other"\n',
            encoding="utf-8",
        )
        mismatched_env = env | {"ATELIER_COORDINATION": "none"}
        denied = subprocess.run(
            [PYTHON, str(lock_script), "acquire", "sample", "--cycle", "test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=mismatched_env,
        )
        expect(
            denied.returncode == 1,
            "ATELIER_COORDINATION=none bypassed shared owner fence",
        )
        expect(
            json.loads(denied.stdout)["coordination"] == "owner",
            "owner denial was not reported",
        )

        identity.write_text(matching_identity, encoding="utf-8")
        acquired = subprocess.run(
            [PYTHON, str(lock_script), "acquire", "sample", "--cycle", "test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(acquired.returncode == 0, f"owner could not acquire: {acquired.stderr}")
        acquired_payload = json.loads(acquired.stdout)
        expect(acquired_payload["acquired"] is True, "owner acquire returned false")
        expect(acquired_payload["generation"] == 1, "owner acquire omitted generation")

        running_dir = meta / "routine_runs" / "sample"
        running_claim = running_dir / "test.toml"
        expect(running_claim.is_file(), "owner acquire did not reserve the cycle claim")
        duplicate = subprocess.run(
            [PYTHON, str(lock_script), "acquire", "sample", "--cycle", "test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            duplicate.returncode == 1, "owner allowed a duplicate same-cycle acquire"
        )
        expect(
            json.loads(duplicate.stdout)["status"] == "running",
            "duplicate owner status drift",
        )
        identity.write_text(
            'version = 1\nmachine_id = "other-machine"\nmachine_label = "other"\n',
            encoding="utf-8",
        )
        blocked_transfer = subprocess.run(
            [
                PYTHON,
                str(owner_script),
                "claim",
                "--force",
                "--source-stopped",
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            blocked_transfer.returncode == 2,
            "owner transfer proceeded while a routine claim was running",
        )
        recovered = subprocess.run(
            [
                PYTHON,
                str(lock_script),
                "recover",
                "sample",
                "--cycle",
                "test",
                "--outcome",
                "safe-to-retry",
                "--confirm-effects-reviewed",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(recovered.returncode == 0, f"owner recovery failed: {recovered.stderr}")
        expect(
            'status = "retry-approved"' in running_claim.read_text(encoding="utf-8"),
            "safe retry did not update the local claim",
        )
        transferred = subprocess.run(
            [
                PYTHON,
                str(owner_script),
                "claim",
                "--force",
                "--source-stopped",
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            transferred.returncode == 0,
            f"quiescent owner transfer failed: {transferred.stderr}",
        )
        expect(
            json.loads(transferred.stdout)["generation"] == 2,
            "owner generation did not advance",
        )

        retried = subprocess.run(
            [PYTHON, str(lock_script), "acquire", "sample", "--cycle", "test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(retried.returncode == 0, "retry-approved owner cycle did not reacquire")
        completed_recovery = subprocess.run(
            [
                PYTHON,
                str(lock_script),
                "recover",
                "sample",
                "--cycle",
                "test",
                "--outcome",
                "completed",
                "--confirm-effects-reviewed",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            completed_recovery.returncode == 0,
            f"completed owner recovery failed: {completed_recovery.stderr}",
        )
        duplicate_completed = subprocess.run(
            [PYTHON, str(lock_script), "acquire", "sample", "--cycle", "test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            duplicate_completed.returncode == 1,
            "owner reacquired a completed same-cycle claim",
        )

        watch.write_text('[coordination]\nbackend = "dynamodb"\n', encoding="utf-8")
        backend = subprocess.run(
            [PYTHON, str(lock_script), "backend"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            backend.returncode == 0,
            f"coordination backend probe failed: {backend.stderr}",
        )
        expect(
            json.loads(backend.stdout)["coordination"] == "dynamodb",
            "backend probe opened the wrong coordination mode",
        )

        clean_prompt = temp / "clean.md"
        clean_prompt.write_text(
            "LOCAL EXECUTION OVERRIDE\n\n"
            "--- ORIGINAL ROUTINE PROMPT (verbatim) ---\n\n"
            "Use the authenticated local CLI.\n",
            encoding="utf-8",
        )
        clean = subprocess.run(
            [PYTHON, str(guard_script), str(clean_prompt)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        expect(
            clean.returncode == 0, f"clean routine prompt was rejected: {clean.stderr}"
        )

        malformed_prompt = temp / "malformed.md"
        malformed_prompt.write_text("Run the archived procedure.\n", encoding="utf-8")
        malformed = subprocess.run(
            [PYTHON, str(guard_script), str(malformed_prompt)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        expect(
            malformed.returncode == 1,
            "routine prompt guard accepted a missing preamble",
        )

        drive_input_prompt = temp / "drive-input.md"
        drive_body = (
            "--- ORIGINAL ROUTINE PROMPT (verbatim) ---\n\n"
            "Use Google-Drive MCP search_files to load the sample queue.\n"
        )
        drive_input_prompt.write_text(
            "LOCAL EXECUTION OVERRIDE\n"
            "- Write your final report DIRECTLY to the filesystem.\n\n" + drive_body,
            encoding="utf-8",
        )
        drive_input = subprocess.run(
            [PYTHON, str(guard_script), str(drive_input_prompt)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        expect(
            drive_input.returncode == 1,
            "routine prompt guard accepted a Drive-only input path",
        )

        drive_input_prompt.write_text(
            "LOCAL EXECUTION OVERRIDE\n"
            "- Read every input the original prompt names from the local\n"
            "  filesystem under $OV, NOT through Drive.\n"
            "- Write your final report DIRECTLY to the filesystem.\n\n" + drive_body,
            encoding="utf-8",
        )
        drive_fixed = subprocess.run(
            [PYTHON, str(guard_script), str(drive_input_prompt)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        expect(
            drive_fixed.returncode == 0,
            f"local input directive was rejected: {drive_fixed.stderr}",
        )

        unsafe_fixtures = (
            "Authorization: Token literalcredential12345\n",
            '"api_key": "literalcredential12345"\n',
            "SERVICE_PASSWORD=literalcredential12345\n",
            "aws_access_key_id: AKIA1234567890ABCDEF\n",
            "tool --token sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
            "https://user:literalcredential12345@example.invalid/path\n",
            "-----BEGIN PRIVATE KEY-----\n",
        )
        for index, fixture in enumerate(unsafe_fixtures):
            unsafe_prompt = temp / f"unsafe-{index}.md"
            unsafe_prompt.write_text(
                "LOCAL EXECUTION OVERRIDE\n\n"
                "--- ORIGINAL ROUTINE PROMPT (verbatim) ---\n\n" + fixture,
                encoding="utf-8",
            )
            unsafe = subprocess.run(
                [PYTHON, str(guard_script), str(unsafe_prompt)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            expect(
                unsafe.returncode == 1,
                f"literal credential fixture {index} was not rejected",
            )
            expect(
                "literalcredential" not in unsafe.stderr,
                "credential guard echoed a secret",
            )


def check_routine_claim() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-routine-claim-") as temp_dir:
        ov = Path(temp_dir) / "vault"
        env = os.environ | {"OV": str(ov)}
        validated_cycle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_claim.py",
                "autoevo-nightly",
                "--validate-cycle",
                "2026-07-25",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        invalid_cycle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_claim.py",
                "autoevo-nightly",
                "--validate-cycle",
                "2026-02-30",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            validated_cycle.returncode == 0
            and validated_cycle.stdout.strip() == "2026-07-25"
            and invalid_cycle.returncode == 2,
            "routine cycle validation accepted a non-calendar date",
        )
        valid_content = (
            'routine = "sample"\n'
            'cycle_id = "cycle"\n'
            "owner_generation = 0\n"
            'status = "running"\n'
        )
        written = subprocess.run(
            [PYTHON, "scripts/routine_claim.py", "sample", "--cycle", "cycle"],
            cwd=ROOT,
            input=valid_content,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(written.returncode == 0, f"atomic claim write failed: {written.stderr}")
        claim = ov / "_meta" / "routine_runs" / "sample" / "cycle.toml"
        expect(
            claim.read_text(encoding="utf-8") == valid_content, "claim content drift"
        )
        rejected = subprocess.run(
            [PYTHON, "scripts/routine_claim.py", "sample", "--cycle", "cycle"],
            cwd=ROOT,
            input='routine = "other"\ncycle_id = "cycle"\nstatus = "failed"\n',
            capture_output=True,
            text=True,
            env=env,
        )
        expect(rejected.returncode == 2, "claim writer accepted a mismatched identity")
        expect(
            claim.read_text(encoding="utf-8") == valid_content,
            "rejected claim write changed the canonical file",
        )
        invalid_owner_generation = subprocess.run(
            [PYTHON, "scripts/routine_claim.py", "sample", "--cycle", "cycle"],
            cwd=ROOT,
            input=(
                'routine = "sample"\n'
                'cycle_id = "cycle"\n'
                'owner_generation = "0"\n'
                'status = "failed"\n'
            ),
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            invalid_owner_generation.returncode == 2
            and claim.read_text(encoding="utf-8") == valid_content,
            "claim writer accepted a string owner_generation",
        )
        claim.write_text(
            'routine = "sample"\n'
            'cycle_id = "cycle"\n'
            "contract_version = 2\n"
            'owner_generation = "3"\n'
            'status = "running"\n',
            encoding="utf-8",
        )
        legacy_owner_generation_read = subprocess.run(
            [
                PYTHON,
                "scripts/routine_claim.py",
                "sample",
                "--cycle",
                "cycle",
                "--schedule-decision",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            legacy_owner_generation_read.returncode == 0
            and json.loads(legacy_owner_generation_read.stdout)["action"] == "skip",
            "claim reader rejected a legacy numeric-string owner_generation",
        )
        claim.write_text(
            'routine = "sample"\n'
            'cycle_id = "cycle"\n'
            "contract_version = 2\n"
            'owner_generation = "invalid"\n'
            'status = "running"\n',
            encoding="utf-8",
        )
        invalid_owner_generation_read = subprocess.run(
            [
                PYTHON,
                "scripts/routine_claim.py",
                "sample",
                "--cycle",
                "cycle",
                "--schedule-decision",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            invalid_owner_generation_read.returncode == 2,
            "claim reader accepted a nonnumeric owner_generation",
        )

        cue_claim_dir = ov / "_meta" / "routine_runs" / "cue-sample"
        cue_claim_dir.mkdir(parents=True)
        legacy_cue_claim = cue_claim_dir / "2099-01-01.toml"
        legacy_cue_claim.write_text(
            'routine = "cue-sample"\n'
            'cycle_id = "2099-01-01"\n'
            "contract_version = 2\n"
            'owner_generation = "3"\n'
            'status = "completed"\n',
            encoding="utf-8",
        )
        invalid_cue_claim = cue_claim_dir / "2099-01-02.toml"
        invalid_cue_claim.write_text(
            'routine = "cue-sample"\n'
            'cycle_id = "2099-01-02"\n'
            "contract_version = 2\n"
            'owner_generation = "invalid"\n'
            'status = "completed"\n',
            encoding="utf-8",
        )
        latest_cue_claim = cues._latest_local_claim(ov, "cue-sample")
        expect(
            latest_cue_claim is not None
            and latest_cue_claim[0] == date(2099, 1, 1)
            and latest_cue_claim[1]["owner_generation"] == 3,
            "cue consumer rejected legacy numeric evidence or accepted invalid evidence",
        )
        claim.write_text(valid_content, encoding="utf-8")
        watch = ov / "_meta" / "routine_watch.toml"
        watch.write_text('[coordination]\nbackend = "none"\n', encoding="utf-8")
        first = subprocess.run(
            [PYTHON, "scripts/routine_lock.py", "acquire", "local", "--cycle", "cycle"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        second = subprocess.run(
            [PYTHON, "scripts/routine_lock.py", "acquire", "local", "--cycle", "cycle"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            first.returncode == 0 and second.returncode == 1,
            "none mode duplicated a cycle",
        )
        recovered = subprocess.run(
            [
                PYTHON,
                "scripts/routine_lock.py",
                "recover",
                "local",
                "--cycle",
                "cycle",
                "--outcome",
                "safe-to-retry",
                "--confirm-effects-reviewed",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(recovered.returncode == 0, "none-mode explicit recovery failed")
        retried = subprocess.run(
            [PYTHON, "scripts/routine_lock.py", "acquire", "local", "--cycle", "cycle"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(retried.returncode == 0, "none-mode retry approval was not consumed")


def check_routine_result() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-routine-result-") as temp_dir:
        ov = Path(temp_dir) / "vault"
        metadata = ov / "_meta"
        output_dir = ov / "reports"
        metadata.mkdir(parents=True)
        output_dir.mkdir()
        (metadata / "routine_watch.toml").write_text(
            "[[routine]]\n"
            'name = "sample"\n'
            'execution = "local"\n'
            'output_dir = "reports"\n'
            'file_pattern = "report-*.md"\n',
            encoding="utf-8",
        )
        result_file = Path(temp_dir) / "result.json"
        claimed = datetime.now(timezone.utc)
        old_output = output_dir / "report-old.md"
        old_output.write_text("old", encoding="utf-8")
        old_timestamp = (claimed - timedelta(days=1)).timestamp()
        os.utime(old_output, (old_timestamp, old_timestamp))
        result_file.write_text(
            json.dumps(
                {
                    "routine": "sample",
                    "outcome": "delivered",
                    "output_file": "reports/report-old.md",
                    "summary": "stale fixture",
                    "skipped_inputs": [],
                }
            ),
            encoding="utf-8",
        )

        original_ov = os.environ.get("OV")
        os.environ["OV"] = str(ov)
        try:
            try:
                routine_result.verify_result(
                    "sample", "cycle", claimed.isoformat(), result_file
                )
            except routine_result.ResultError:
                pass
            else:
                raise SmokeFailure("delivery validator accepted an old artifact")

            fresh_output = output_dir / "report-fresh.md"
            fresh_output.write_text("fresh", encoding="utf-8")
            for outcome in ("delivered", "noop"):
                result_file.write_text(
                    json.dumps(
                        {
                            "routine": "sample",
                            "outcome": outcome,
                            "output_file": "reports/report-fresh.md",
                            "summary": "fresh fixture",
                            "skipped_inputs": [],
                        }
                    ),
                    encoding="utf-8",
                )
                attestation = routine_result.verify_result(
                    "sample", "cycle", claimed.isoformat(), result_file
                )
                expect(
                    attestation["outcome"] == outcome,
                    f"delivery validator changed the {outcome} outcome",
                )

            result_file.write_text(
                json.dumps(
                    {
                        "routine": "sample",
                        "outcome": "failed",
                        "output_file": None,
                        "summary": "failed fixture",
                        "skipped_inputs": [],
                    }
                ),
                encoding="utf-8",
            )
            try:
                routine_result.verify_result(
                    "sample", "cycle", claimed.isoformat(), result_file
                )
            except routine_result.ResultError:
                pass
            else:
                raise SmokeFailure("delivery validator accepted a failed outcome")
        finally:
            if original_ov is None:
                os.environ.pop("OV", None)
            else:
                os.environ["OV"] = original_ov


def check_routine_cues() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-routine-cues-") as temp_dir:
        ov = Path(temp_dir) / "vault"
        metadata = ov / "_meta"
        metadata.mkdir(parents=True)
        (metadata / "routine_watch.toml").write_text(
            "[coordination]\n"
            'backend = "owner"\n\n'
            "[[routine]]\n"
            'name = "daily"\n'
            'label = "daily fixture"\n'
            'execution = "local"\n'
            'cron = "0 5 * * * (local)"\n'
            'output_dir = "daily"\n'
            'file_pattern = "daily-*.md"\n\n'
            "[[routine]]\n"
            'name = "weekly"\n'
            'label = "weekly fixture"\n'
            'execution = "local"\n'
            'cron = "0 5 * * 3 (local)"\n'
            'output_dir = "weekly"\n'
            'file_pattern = "weekly-*.md"\n\n'
            "[[routine]]\n"
            'name = "monthly"\n'
            'label = "monthly fixture"\n'
            'execution = "local"\n'
            'cron = "0 9 1 * * (local)"\n'
            'output_dir = "monthly"\n'
            'file_pattern = "monthly-*.md"\n',
            encoding="utf-8",
        )
        (metadata / "routine_owner.toml").write_text(
            'owner_label = "fixture"\ntransferred_at = "2026-07-17T08:00:00-07:00"\n',
            encoding="utf-8",
        )

        for routine_name, claim_date in (
            ("daily", date(2026, 7, 23)),
            ("weekly", date(2026, 7, 22)),
        ):
            claim_dir = metadata / "routine_runs" / routine_name
            claim_dir.mkdir(parents=True)
            (claim_dir / f"{claim_date}.toml").write_text(
                f'routine = "{routine_name}"\n'
                f'cycle_id = "{claim_date}"\n'
                'status = "completed"\n',
                encoding="utf-8",
            )

        daily_dir = ov / "daily"
        daily_dir.mkdir()
        for day in range(17, 23):
            (daily_dir / f"daily-2026-07-{day:02d}.md").write_text(
                "fixture", encoding="utf-8"
            )
        weekly_dir = ov / "weekly"
        weekly_dir.mkdir()
        (weekly_dir / "weekly-2026-07-22.md").write_text("fixture", encoding="utf-8")

        local_zone = datetime.now().astimezone().tzinfo
        now = datetime(2026, 7, 23, 12, tzinfo=local_zone)
        missed, missed_debug = cues.check_local_routine_missed(ov, now.date(), now=now)
        expect(missed is None, f"schedule-aware missed cue fired: {missed_debug}")
        expect(
            "no scheduled occurrence due" in missed_debug,
            "monthly owner-transfer grace was not exercised",
        )

        hitrate, hitrate_debug = cues.check_routine_hitrate(ov, now.date(), now=now)
        expect(hitrate is None, f"owner-aware hit rate fired: {hitrate_debug}")
        expect(
            "daily fixture: 6/7" in hitrate_debug,
            f"owner-aware hit rate used the wrong denominator: {hitrate_debug}",
        )

        stale, stale_debug = cues.check_routine_staleness(ov, now.date())
        expect(stale is not None, "completed claim newer than output was not detected")
        expect(
            "daily fixture" in stale.message,
            "delivery gap was omitted from the staleness cue",
        )
        expect(
            "monthly fixture:" in stale_debug and "inside owner grace" in stale_debug,
            "monthly routine was falsely stale during owner grace",
        )

        next_day = datetime(2026, 7, 24, 12, tzinfo=local_zone)
        missed, missed_debug = cues.check_local_routine_missed(
            ov, next_day.date(), now=next_day
        )
        expect(missed is not None, "a genuinely missed daily cycle stayed silent")
        expect(
            "daily fixture (no claim for 2026-07-24)" in missed.message,
            f"missed daily cycle reported the wrong schedule: {missed_debug}",
        )


def check_dynamodb_retry_authorization() -> None:
    class ConditionalCheckFailed(Exception):
        pass

    class FakeExceptions:
        ConditionalCheckFailedException = ConditionalCheckFailed

    class FakeClient:
        exceptions = FakeExceptions()

        def __init__(self) -> None:
            self.item: dict[str, dict[str, str]] = {
                "pk": {"S": "sample#cycle"},
                "machine": {"S": "first"},
                "status": {"S": "running"},
            }

        def put_item(self, **_: object) -> None:
            if self.item:
                raise ConditionalCheckFailed()

        def get_item(self, **_: object) -> dict[str, object]:
            return {"Item": self.item.copy()} if self.item else {}

        def update_item(self, **kwargs: object) -> None:
            values = kwargs["ExpressionAttributeValues"]
            assert isinstance(values, dict)
            current = self.item.get("status", {}).get("S")
            if ":machine" in values:
                if current != "retry-approved":
                    raise ConditionalCheckFailed()
                self.item["status"] = {"S": "running"}
                self.item["machine"] = values[":machine"]
                return
            if ":recovering" in values and ":running" in values:
                if current not in {"running", "recovery-in-progress"}:
                    raise ConditionalCheckFailed()
                self.item["status"] = {"S": "recovery-in-progress"}
                return
            if ":retry" in values:
                if current not in {"recovery-in-progress", "retry-approved"}:
                    raise ConditionalCheckFailed()
                self.item["status"] = {"S": "retry-approved"}
                return
            raise AssertionError("unexpected fake DynamoDB update")

    with tempfile.TemporaryDirectory(prefix="atelier-dynamo-retry-") as temp_dir:
        ov = Path(temp_dir) / "vault"
        claim = ov / "_meta" / "routine_runs" / "sample" / "cycle.toml"
        claim.parent.mkdir(parents=True)
        claim.write_text('status = "completion-uncertain"\n', encoding="utf-8")
        fake = FakeClient()
        original_mode = routine_lock._coordination_mode
        original_client = routine_lock._get_client
        original_hostname = routine_lock._hostname
        original_ov = os.environ.get("OV")
        os.environ["OV"] = str(ov)
        routine_lock._coordination_mode = lambda: "dynamodb"
        routine_lock._get_client = lambda: fake
        routine_lock._hostname = lambda: "retry-machine"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                recovered = routine_lock.recover(
                    "sample", "cycle", "safe-to-retry", True
                )
            expect(recovered == 0, "DynamoDB safe retry recovery failed")
            expect(
                fake.item["status"]["S"] == "retry-approved",
                "DynamoDB recovery removed or failed to publish central retry authorization",
            )
            claim.write_text('status = "running"\n', encoding="utf-8")
            first_output = io.StringIO()
            with contextlib.redirect_stdout(first_output):
                acquired = routine_lock.acquire("sample", "cycle", 3600)
            expect(
                acquired == 0, "central DynamoDB retry authorization was not acquired"
            )
            expect(
                json.loads(first_output.getvalue())["retry_authorized"] is True,
                "DynamoDB retry acquire omitted its authorization attestation",
            )
            expect(
                fake.item["status"]["S"] == "running",
                "retry was not atomically consumed",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                duplicate = routine_lock.acquire("sample", "cycle", 3600)
            expect(duplicate == 1, "DynamoDB retry authorization was consumed twice")
        finally:
            routine_lock._coordination_mode = original_mode
            routine_lock._get_client = original_client
            routine_lock._hostname = original_hostname
            if original_ov is None:
                os.environ.pop("OV", None)
            else:
                os.environ["OV"] = original_ov


def check_runtime_selector() -> None:
    status = json.loads(run(["scripts/atelier_runtime.py", "status", "--json"]))
    expect(
        status["committed_default"] == "codex", "shipped runtime default must be Codex"
    )
    expect(
        set(status["available"]) == {"claude", "codex"},
        "runtime registry must expose both CLIs",
    )

    codex = run(
        [
            "scripts/atelier_runtime.py",
            "run",
            "--runtime",
            "codex",
            "--dry-run",
            "hi",
            "smoke",
        ]
    ).strip()
    expect(
        "codex -C" in codex and "'$hi smoke'" in codex, "Codex selector command drift"
    )

    claude = run(
        [
            "scripts/atelier_runtime.py",
            "run",
            "--runtime",
            "claude",
            "--non-interactive",
            "--dry-run",
            "lint",
        ]
    ).strip()
    expect(claude == "claude -p /lint", "Claude selector command drift")

    overridden = json.loads(
        run(
            ["scripts/atelier_runtime.py", "resolve", "--json"],
            env_overrides={"ATELIER_RUNTIME": "claude"},
        )
    )
    expect(
        overridden == {"runtime": "claude", "source": "environment"},
        "runtime env override drift",
    )

    codex_native = run(
        [
            "scripts/shadow.py",
            "native-model",
            "--agent",
            "thinker",
            "--runtime",
            "codex",
        ]
    ).strip()
    claude_native = run(
        [
            "scripts/shadow.py",
            "native-model",
            "--agent",
            "thinker",
            "--runtime",
            "claude",
        ]
    ).strip()
    expect(codex_native == "codex_native", "Codex native shadow identity drift")
    expect(claude_native == "opus", "Claude native shadow identity drift")

    neutral_env = os.environ.copy()
    for key in (
        "ATELIER_ACTIVE_RUNTIME",
        "CODEX_THREAD_ID",
        "CLAUDECODE",
        "CLAUDE_PROJECT_DIR",
    ):
        neutral_env.pop(key, None)
    neutral = subprocess.run(
        [PYTHON, "scripts/shadow.py", "native-model", "--agent", "thinker"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=neutral_env,
    )
    expect(
        neutral.returncode == 0, f"neutral native model lookup failed: {neutral.stderr}"
    )
    expected_neutral = "codex_native" if status["runtime"] == "codex" else "opus"
    expect(
        neutral.stdout.strip() == expected_neutral,
        "native model lookup must honor the selected runtime outside a live session",
    )


def check_runtime_cue_syntax() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-cue-runtime-") as temp_dir:
        (Path(temp_dir) / "reflections").mkdir()
        codex = json.loads(
            run(
                ["scripts/cues.py", "--only", "weekly", "--json", "--runtime", "codex"],
                env_overrides={"OV": temp_dir},
            )
        )
        claude = json.loads(
            run(
                [
                    "scripts/cues.py",
                    "--only",
                    "weekly",
                    "--json",
                    "--runtime",
                    "claude",
                ],
                env_overrides={"OV": temp_dir},
            )
        )
        expect(
            len(codex) == 1 and "`$weekly`" in codex[0]["message"],
            "Codex cue syntax drift",
        )
        expect(
            len(claude) == 1 and "`/weekly`" in claude[0]["message"],
            "Claude cue syntax drift",
        )



def check_context_bundle() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-context-") as temp_dir:
        root = Path(temp_dir)
        vault = root / "vault"
        for relative in (
            "profile",
            "sessions",
            "reflections",
            "daily-notes/2099/01",
            "research",
        ):
            (vault / relative).mkdir(parents=True, exist_ok=True)

        (vault / "profile" / "identity.md").write_text(
            "Last built: 2099-01-03\n\n## Identity\nstable identity\n",
            encoding="utf-8",
        )
        (vault / "profile" / "directions.md").write_text(
            "Last built: 2099-01-03\n\n## Direction\nactive direction\n",
            encoding="utf-8",
        )
        (vault / "sessions" / "2099-01-03-reflection.md").write_text(
            "## Continuity\ncarry this\n\n"
            "## Anomalies\nnotice this\n\n"
            "## Full Text\nmust not preload\n",
            encoding="utf-8",
        )
        (vault / "sessions" / "2099-01-02-reading.md").write_text(
            "## Reading Capsule\n"
            "checkpoint: initial-analysis\n"
            "source: durable reading source\n"
            "status: discussion-open\n",
            encoding="utf-8",
        )
        (vault / "reflections" / "2099-01").mkdir(parents=True, exist_ok=True)
        (vault / "reflections" / "2099-01" / "2099-01-02-reflection.md").write_text(
            "## Theme\nbody must stay out of the heading projection\n\n"
            "## Next Action\ndo one bounded thing\n",
            encoding="utf-8",
        )
        daily = vault / "daily-notes" / "2099" / "01" / "2099-01-03.md"
        daily.write_text("## Today\nexplicit daily context\n", encoding="utf-8")
        (vault / "research" / "source.md").write_text(
            "## Alpha\nalpha only\n\n## Beta\nbeta only\n",
            encoding="utf-8",
        )

        capture_stdout = run(
            [
                "scripts/context_bundle.py",
                "--intent",
                "capture",
                "--vault",
                str(vault),
                "--effective-date",
                "2099-01-03",
                "--format",
                "json",
            ]
        )
        capture = json.loads(capture_stdout)
        expect(
            not any(row["component"] == "profile" for row in capture["excerpts"]),
            "empty profile_reads unexpectedly loaded profile content",
        )
        expect(
            not any(row["component"] == "daily" for row in capture["excerpts"]),
            "daily context must remain opt-in",
        )
        expect(
            capture["budget"]["output_bytes"] == len(capture_stdout.encode("utf-8")),
            "context bundle JSON byte accounting drift",
        )
        expect(
            capture["budget"]["limit_bytes"] == 4096,
            "capture route did not use its registry context budget",
        )

        reflection_stdout = run(
            [
                "scripts/context_bundle.py",
                "--intent",
                "reflection",
                "--vault",
                str(vault),
                "--effective-date",
                "2099-01-03",
                "--component",
                "profile",
                "--component",
                "session",
                "--component",
                "reflections",
                "--component",
                "daily",
                "--component",
                "sources",
                "--source",
                "research/source.md#Beta",
                "--byte-budget",
                "8192",
                "--format",
                "json",
            ]
        )
        reflection = json.loads(reflection_stdout)
        excerpts = reflection["excerpts"]
        expect(
            {row["section"] for row in excerpts if row["component"] == "session"}
            == {"Continuity", "Anomalies"},
            "session projection leaked non-continuity sections",
        )
        expect(
            any(row["source"] == str(daily.relative_to(vault)) for row in excerpts),
            "explicit daily component did not resolve effective-date note",
        )
        expect(
            any(
                row["source"] == "research/source.md"
                and row["section"] == "Beta"
                and "beta only" in row["content"]
                and "alpha only" not in row["content"]
                for row in excerpts
            ),
            "explicit source section projection drift",
        )
        expect(
            "body must stay out" not in reflection_stdout,
            "reflection projection loaded a full low-priority section body",
        )
        expect(
            len(reflection_stdout.encode("utf-8")) <= 8192,
            "context bundle exceeded its selected byte budget",
        )

        reading = json.loads(
            run(
                [
                    "scripts/context_bundle.py",
                    "--intent",
                    "reading",
                    "--vault",
                    str(vault),
                    "--effective-date",
                    "2099-01-03",
                    "--format",
                    "json",
                ]
            )
        )
        expect(
            any(
                row["section"] == "Reading Capsule"
                and row["source"] == "sessions/2099-01-02-reading.md"
                and "discussion-open" in row["content"]
                for row in reading["excerpts"]
            ),
            "reading route did not receive the latest reading capsule",
        )
        expect(
            reading["budget"]["limit_bytes"] == 6144,
            "reading route did not use its registry context budget",
        )
        talk = json.loads(
            run(
                [
                    "scripts/context_bundle.py",
                    "--intent",
                    "talk",
                    "--vault",
                    str(vault),
                    "--effective-date",
                    "2099-01-03",
                    "--format",
                    "json",
                ]
            )
        )
        expect(
            any(
                row["section"] == "Reading Capsule"
                and row["source"] == "sessions/2099-01-02-reading.md"
                for row in talk["excerpts"]
            ),
            "talk route did not receive the latest reading capsule",
        )
        expect(
            not any(
                row["section"] == "Reading Capsule" for row in reflection["excerpts"]
            ),
            "non-reading route leaked a reading capsule",
        )
        for offset in range(1, 102):
            session_day = date(2099, 1, 2) + timedelta(days=offset)
            (vault / "sessions" / f"{session_day.isoformat()}-review.md").write_text(
                "## Continuity\nnewer non-reading session\n",
                encoding="utf-8",
            )
        late_reading = json.loads(
            run(
                [
                    "scripts/context_bundle.py",
                    "--intent",
                    "reading",
                    "--vault",
                    str(vault),
                    "--effective-date",
                    "2099-05-01",
                    "--format",
                    "json",
                ]
            )
        )
        expect(
            any(
                row["section"] == "Reading Capsule"
                and row["source"] == "sessions/2099-01-02-reading.md"
                for row in late_reading["excerpts"]
            ),
            "reading recovery stopped after 100 newer non-reading session logs",
        )

        injected = json.dumps(
            {
                "name": "capture",
                "mode": "wrong",
                "procedure": "../../outside.md",
                "context_budget_bytes": 9999,
                "profile_reads": ["../../outside.md"],
            }
        )
        routed = json.loads(
            run(
                [
                    "scripts/context_bundle.py",
                    "--route-json",
                    injected,
                    "--vault",
                    str(vault),
                    "--effective-date",
                    "2099-01-03",
                    "--format",
                    "json",
                ]
            )
        )
        expect(
            sorted(routed["route"].get("packet_registry_mismatch", []))
            == [
                "context_budget_bytes",
                "mode",
                "procedure",
                "profile_reads",
            ],
            "route packet mismatch was not made visible",
        )
        expect(
            not any(row["component"] == "profile" for row in routed["excerpts"]),
            "route packet injected an undeclared profile path",
        )

        outside = root / "outside.md"
        outside.write_text("must not read\n", encoding="utf-8")
        escaped = subprocess.run(
            [
                PYTHON,
                "scripts/context_bundle.py",
                "--intent",
                "reflection",
                "--vault",
                str(vault),
                "--component",
                "sources",
                "--source",
                str(outside),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        expect(
            escaped.returncode != 0 and "escapes the vault" in escaped.stderr,
            "context bundle accepted a source outside the selected vault",
        )


def check_privacy_scanner() -> None:
    """Catch staged-only leaks and the boundary cases that previously escaped."""
    privacy_role = (ROOT / ".claude" / "agents" / "privacy-reviewer.md").read_text(
        encoding="utf-8"
    )
    system_review = (ROOT / ".claude" / "commands" / "system-review.md").read_text(
        encoding="utf-8"
    )
    allowlist = privacy_check.load_allowlist()
    expect(
        "atelier-mbp" in allowlist, "approved public machine example is not allowlisted"
    )
    expect(
        "scripts/privacy_allowlist.txt" in privacy_role,
        "native semantic privacy role does not honor deliberate public opt-outs",
    )
    expect(
        "--- PRIVACY ALLOWLIST ---" in system_review
        and "cat scripts/privacy_allowlist.txt" in system_review,
        "direct semantic privacy prompt does not receive deliberate public opt-outs",
    )
    expect(
        "git ls-files --others --exclude-standard -z" in system_review,
        "direct semantic privacy prompt word-splits untracked filenames",
    )
    expect(
        'cat "./$f"' in system_review,
        "direct semantic privacy prompt treats dash-prefixed filenames as options",
    )

    with tempfile.TemporaryDirectory(prefix="atelier-privacy-") as temp_dir:
        repo = Path(temp_dir)

        def git(*args: str) -> None:
            result = subprocess.run(
                ["git", *args],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            expect(
                result.returncode == 0,
                f"privacy fixture git {' '.join(args)} failed: {result.stderr}",
            )

        git("init", "-q")
        (repo / "README.md").write_text("public fixture\n", encoding="utf-8")
        git("add", "README.md")
        git(
            "-c",
            "user.name=Atelier Smoke",
            "-c",
            "user.email=smoke@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "base",
        )

        candidate = repo / "Private-Example-Person.md"
        candidate.write_text("PRIVATE EXAMPLE PERSON\n", encoding="utf-8")
        git("add", candidate.name)
        candidate.write_text("clean working copy\n", encoding="utf-8")

        files = privacy_check.tracked_files(repo)
        expect(
            candidate.name in files, "newly staged privacy fixture is not public-bound"
        )
        sources = privacy_check.content_sources(files, repo)
        sources.extend(privacy_check.path_sources(files))
        hits = privacy_check.scan(["Private Example Person"], sources)
        expect(
            any(
                hit["file"] == candidate.name and hit["source"] == "index"
                for hit in hits
            ),
            "privacy scanner missed a case-insensitive staged-only leak",
        )
        expect(
            not any(hit["source"] == "worktree" for hit in hits),
            "clean worktree fixture should not report a worktree leak",
        )
        expect(
            any(hit["source"] == "path" for hit in hits),
            "privacy scanner missed a filename-only leak",
        )

    boundary_sources = [
        ("inside.md", "worktree", "a masterpiece"),
        ("exact.md", "worktree", "中文Aster中文"),
    ]
    boundary_hits = privacy_check.scan_slugs({"aster"}, boundary_sources)
    expect(
        len(boundary_hits) == 1 and boundary_hits[0]["file"] == "exact.md",
        "private slug boundary matching drift",
    )
    candidates = privacy_check._wikilink_candidates(
        "people/Example-Person.md#Background"
    )
    expect(
        {"Example-Person", "Example Person"} <= candidates,
        "path-qualified wikilink normalization drift",
    )


def check_codex_intent_hook() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-codex-hook-") as temp_dir:
        exact_payload = {
            "prompt": "$hi review my goals",
            "session_id": "smoke-exact-session",
        }
        exact_output = run(
            ["scripts/intent_coverage.py", "intent-hook", "--runtime", "codex"],
            input_text=json.dumps(exact_payload),
            env_overrides={"OV": temp_dir},
        )
        exact_route, exact_context = parse_intent_route(exact_output)
        expect(exact_route.get("schema") == 2, "intent route schema drift")
        expect(
            exact_route.get("source") == "harness/intents.toml",
            "intent route source drift",
        )
        expect(exact_route.get("name") == "review", "Codex exact route winner drift")
        expect(exact_route.get("mode") == "goal-review", "Codex exact route mode drift")
        expect(
            exact_route.get("procedure") == ".claude/commands/review.md",
            "Codex exact route procedure drift",
        )
        expect(
            exact_route.get("context_budget_bytes") == 8192,
            "Codex exact route context budget drift",
        )
        with (ROOT / "harness" / "intents.toml").open("rb") as handle:
            review_row = tomllib.load(handle)["intents"]["review"]
        expect(
            exact_route.get("agents") == list(review_row.get("agents", [])),
            "Codex exact route agents drift (route packet must mirror the registry row)",
        )
        expect(
            exact_route.get("profile_reads") == ["identity.md", "directions.md"],
            "Codex exact route profile reads drift",
        )
        expect(exact_route.get("fallback") is False, "exact route marked as fallback")
        expect(exact_route.get("ambiguous") is False, "exact route marked ambiguous")
        expect(
            len(exact_context.encode("utf-8"))
            <= intent_coverage.INTENT_ROUTE_MAX_CONTEXT_BYTES,
            "normal intent route exceeded the context budget",
        )
        expect(
            len(exact_output.encode("utf-8"))
            <= intent_coverage.INTENT_ROUTE_MAX_CONTEXT_BYTES,
            "normal intent hook output exceeded 1 KiB",
        )
        expect(
            exact_payload["prompt"] not in exact_output,
            "intent route packet echoed the raw user prompt",
        )

        payload = {
            "prompt": "$hi qzxv-codex-hook-smoke",
            "session_id": "smoke-session",
        }
        output = run(
            ["scripts/intent_coverage.py", "intent-hook", "--runtime", "codex"],
            input_text=json.dumps(payload),
            env_overrides={"OV": temp_dir},
        )
        fallback_route, _ = parse_intent_route(output)
        expect(
            fallback_route.get("name") == "general", "Codex fallback winner drift"
        )
        expect(
            fallback_route.get("procedure") == "protocols/intent-general.md",
            "Codex fallback did not use semantic handoff",
        )
        expect(fallback_route.get("fallback") is True, "Codex fallback flag drift")
        expect(
            fallback_route.get("ambiguous") is False, "Codex fallback marked ambiguous"
        )
        expect(
            payload["prompt"] not in output,
            "Codex fallback route echoed the raw user prompt",
        )
        logs = list((Path(temp_dir) / "_meta" / "intent_misses").glob("*.jsonl"))
        expect(len(logs) == 1, "Codex $hi skill should create one fallback miss log")
        events = [
            json.loads(line)
            for line in logs[0].read_text(encoding="utf-8").splitlines()
        ]
        expect(len(events) == 1, "expected one Codex intent-miss event")
        event = events[0]
        expect(event.get("runtime") == "codex", "Codex hook event runtime drift")
        expect(
            event.get("raw_input") == "qzxv-codex-hook-smoke",
            "Codex hook stripped input drift",
        )
        expect(
            event.get("logged_by") == "user_prompt_submit_hook",
            "Codex hook provenance drift",
        )

        reflect_output = run(
            ["scripts/intent_coverage.py", "intent-hook", "--runtime", "codex"],
            input_text=json.dumps({"prompt": "$reflect qzxv-codex-skill-smoke"}),
            env_overrides={"OV": temp_dir},
        )
        reflect_route, _ = parse_intent_route(reflect_output)
        expect(
            reflect_route.get("fallback") is True,
            "explicit $reflect fallback route drift",
        )
        skill_events = logs[0].read_text(encoding="utf-8").splitlines()
        expect(len(skill_events) == 2, "explicit $reflect entry should be hook-logged")
        skill_event = json.loads(skill_events[-1])
        expect(
            skill_event.get("raw_input") == "qzxv-codex-skill-smoke",
            "explicit $reflect entry stripped input drift",
        )

        ordinary_output = run(
            ["scripts/intent_coverage.py", "intent-hook", "--runtime", "codex"],
            input_text=json.dumps({"prompt": "ordinary engineering question"}),
            env_overrides={"OV": temp_dir},
        )
        expect(ordinary_output == "", "ordinary Codex prompt received an Atelier route")
        events_after = logs[0].read_text(encoding="utf-8").splitlines()
        expect(
            len(events_after) == 2,
            "ordinary Codex prompts must not be logged as Atelier misses",
        )

        for retired_shape in (
            "$hi",
            "$reflect",
            "hi qzxv-retired-bare-shape",
            "/hi qzxv-codex-slash-shape",
            "$atelier hi qzxv-retired-router-shape",
        ):
            retired_output = run(
                ["scripts/intent_coverage.py", "intent-hook", "--runtime", "codex"],
                input_text=json.dumps({"prompt": retired_shape}),
                env_overrides={"OV": temp_dir},
            )
            expect(
                retired_output == "",
                f"Codex non-contextual or retired shape emitted a route: {retired_shape}",
            )
        events_after_retired_shapes = logs[0].read_text(encoding="utf-8").splitlines()
        expect(
            len(events_after_retired_shapes) == 2,
            "Codex intent hook must accept only explicit $hi and $reflect entry shapes",
        )


def check_claude_intent_hook() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-claude-hook-") as temp_dir:
        exact_output = run(
            ["scripts/intent_coverage.py", "intent-hook", "--runtime", "claude-code"],
            input_text=json.dumps({"prompt": "/hi review my goals"}),
            env_overrides={"OV": temp_dir},
        )
        exact_route, _ = parse_intent_route(exact_output)
        expect(exact_route.get("name") == "review", "Claude exact route winner drift")

        for prompt in (
            "/hi qzxv-claude-hook-smoke",
            "/reflect qzxv-claude-reflect-smoke",
        ):
            fallback_output = run(
                [
                    "scripts/intent_coverage.py",
                    "intent-hook",
                    "--runtime",
                    "claude-code",
                ],
                input_text=json.dumps({"prompt": prompt}),
                env_overrides={"OV": temp_dir},
            )
            fallback_route, _ = parse_intent_route(fallback_output)
            expect(
                fallback_route.get("fallback") is True,
                "Claude fallback route drift",
            )
        logs = list((Path(temp_dir) / "_meta" / "intent_misses").glob("*.jsonl"))
        expect(len(logs) == 1, "Claude slash commands should create an intent-miss log")
        events = logs[0].read_text(encoding="utf-8").splitlines()
        expect(len(events) == 2, "Claude /hi and /reflect should both be hook-logged")

        for non_route_prompt in (
            "/hi",
            "/reflect",
            "$hi qzxv-claude-dollar-shape",
        ):
            non_route_output = run(
                [
                    "scripts/intent_coverage.py",
                    "intent-hook",
                    "--runtime",
                    "claude-code",
                ],
                input_text=json.dumps({"prompt": non_route_prompt}),
                env_overrides={"OV": temp_dir},
            )
            expect(
                non_route_output == "",
                f"Claude non-contextual or foreign shape emitted a route: {non_route_prompt}",
            )
        events_after_dollar = logs[0].read_text(encoding="utf-8").splitlines()
        expect(
            len(events_after_dollar) == 2,
            "Claude intent hook must accept slash commands, not Codex $skills",
        )

    synthetic_matches = [
        {
            "name": "alpha",
            "mode": "alpha-mode",
            "agents": ["researcher"],
            "profile_reads": [],
            "priority": 20,
            "matched_pattern": "alpha",
            "parallel": False,
        },
        {
            "name": "beta",
            "mode": "beta-mode",
            "agents": ["thinker"],
            "profile_reads": ["identity.md"],
            "priority": 20,
            "matched_pattern": "beta",
            "parallel": True,
        },
    ]
    ambiguous_route = intent_coverage.build_intent_route_projection(synthetic_matches)
    expect(ambiguous_route is not None, "ambiguous projection unexpectedly absent")
    expect(ambiguous_route.get("ambiguous") is True, "ambiguous route flag drift")
    expect(
        [row["name"] for row in ambiguous_route.get("tied_candidates", [])]
        == ["alpha", "beta"],
        "ambiguous route candidates lost or reordered",
    )

    oversized_route = dict(ambiguous_route)
    oversized_route["agents"] = ["x" * intent_coverage.INTENT_ROUTE_MAX_CONTEXT_BYTES]
    oversized_output = io.StringIO()
    with contextlib.redirect_stdout(oversized_output):
        intent_coverage._emit_intent_route_projection(oversized_route)
        expect(
            oversized_output.getvalue() == "",
            "oversized intent route must stay silent for full-registry fallback",
        )


def check_public_regression_tests() -> None:
    result = subprocess.run(
        [
            PYTHON,
            "-m",
            "unittest",
            "tests.test_session_log",
            "tests.test_session_replay",
            "tests.test_signal_facts",
            "tests.test_paths",
            "tests.test_autoevo_preflight",
            "tests.test_autoevo_pending",
            "tests.test_cues",
            "tests.test_lint_guards",
            "tests.test_session_replay_default",
            "tests.test_render_edges",
            "tests.test_autoevo_commit",
            "tests.test_autoevo_run",
            "tests.test_shadow_group",
            "tests.test_privacy_action",
            "tests.test_intent_routing",
            "tests.test_intent_overlay",
            "tests.test_session_stats",
            "tests.test_decay_scan",
            "tests.test_dine_rank",
            "tests.test_dining_audit",
            "tests.test_daily_brief",
            "tests.test_refresh_tracking",
            "tests.test_triage_command",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(
        result.returncode == 0,
        "public session-log/replay/signal regression tests failed\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
    )


def check_ruff_strict_core() -> None:
    """The committed ruff gate: correctness classes only, always clean.

    Soft-skips when uvx/ruff is unavailable (offline launchd runs must not
    fail on a missing linter); any lint FINDING is a real failure.
    """
    import shutil

    if shutil.which("uvx") is None:
        print("  note: uvx unavailable; ruff strict-core check skipped")
        return
    result = subprocess.run(
        ["uvx", "ruff", "check", "scripts", "tests", "--select", "F,E4,E7,E9,EXE001"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode not in (0, 1):
        print(f"  note: ruff unavailable (exit {result.returncode}); check skipped")
        return
    expect(
        result.returncode == 0,
        f"ruff strict-core violations:\n{result.stdout}",
    )



def main() -> int:
    checks = [
        ("harness lint", check_harness_lint),
        ("Codex command skills", check_codex_command_skills),
        ("Codex native agents", check_codex_native_agents),
        ("paper cache", check_paper_cache),
        ("dining audit", check_dining_audit),
        ("semantic cache-first", check_semantic_cache_first),
        ("semantic maintenance", check_semantic_maintenance),
        ("tracking refresh routine", check_tracking_refresh_routine),
        ("vault job runner", check_vault_job_runner),
        ("semantic corpus policy", check_semantic_corpus_policy),
        ("autoevo reliability", check_autoevo_reliability),
        ("runtime selector", check_runtime_selector),
        ("runtime cue syntax", check_runtime_cue_syntax),
        ("bounded context projection", check_context_bundle),
        ("public session-log, replay, and signal regressions", check_public_regression_tests),
        ("ruff strict-core lint", check_ruff_strict_core),
        ("privacy scanner", check_privacy_scanner),
        ("Codex routine runner", check_codex_routine_runner),
        ("routine capability profiles", check_routine_profiles),
        ("routine owner", check_routine_owner),
        ("atomic routine claim", check_routine_claim),
        ("routine delivery result", check_routine_result),
        ("routine schedule cues", check_routine_cues),
        ("DynamoDB retry authorization", check_dynamodb_retry_authorization),
        ("Codex intent hook", check_codex_intent_hook),
        ("Claude intent hook", check_claude_intent_hook),
    ]
    try:
        for label, check in checks:
            check()
            print(f"ok: {label}")
    except SmokeFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("harness_smoke: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
