"""Mutation tests for the two harness_lint guards added on 2026-08-22.

A guard is only a guard once it has failed on the bug it was written for.
The first version of the flat-tier-glob regex silently matched nothing
(an escape swallowed during insertion); these tests pin both checks to the
exact offending shapes so a future refactor of harness_lint.py cannot
neuter them quietly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_py(body: str, env_extra: dict | None = None) -> dict:
    code = "import json, sys\nsys.path.insert(0, 'scripts')\nimport harness_lint as h\nfrom pathlib import Path\n" + textwrap.dedent(body)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env={**os.environ, **(env_extra or {})},
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


class FlatTierGlobGuardTest(unittest.TestCase):
    def test_regexes_match_the_original_offending_lines(self) -> None:
        out = _run_py(
            """
            py_lines = [
                'weeklies = sorted(weekly_dir.glob("*-weekly.md"))',
                'for f in sorted(REFLECTIONS_DIR.glob("*.md")):',
                'any_audit = list(findings_dir.glob("autoevo-applied-*.md"))',
                'x = tier("reflections").glob("*.md")',
            ]
            ok_lines = ['corpus.extend(WIKI_DIR.rglob("*.md"))', 'for f in tier_files("reflections", "*.md"):']
            ls_lines = [
                'Bash: last_full=$(ls "$OV"/reflections/*-review.md 2>/dev/null | sort | tail -1)',
                'Wiki count (`ls "$OV"/wiki/*.md | wc -l`)',
                'ls "$OV"/reflections/${date_str}-reflection*.md 2>/dev/null > /dev/null || echo missing',
            ]
            ok_ls = ['find "$OV/reflections" -name "*-weekly.md"', 'ls "$OV"/gtd/*.md']
            print(json.dumps({
                "py_hits": [any(r.search(l) for r in h._FLAT_TIER_PY_RES) for l in py_lines],
                "py_ok": [any(r.search(l) for r in h._FLAT_TIER_PY_RES) for l in ok_lines],
                "ls_hits": [bool(h._FLAT_TIER_LS_RE.search(l)) for l in ls_lines],
                "ls_ok": [bool(h._FLAT_TIER_LS_RE.search(l)) for l in ok_ls],
                "tiers": list(h.BUCKETED_TIERS),
            }))
            """
        )
        self.assertTrue(all(out["py_hits"]), out)
        self.assertFalse(any(out["py_ok"]), out)
        self.assertTrue(all(out["ls_hits"]), out)
        self.assertFalse(any(out["ls_ok"]), out)
        for tier in ("reflections", "agent-findings", "wiki", "people"):
            self.assertIn(tier, out["tiers"])


class AliasedFlatGlobGuardTest(unittest.TestCase):
    def test_alias_assigned_from_tier_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-lint-") as tmp:
            import shutil
            root = Path(tmp)
            for extra in ("harness", ".claude", ".codex", "protocols"):
                if (REPO_ROOT / extra).exists():
                    shutil.copytree(REPO_ROOT / extra, root / extra, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy(REPO_ROOT / ".gitignore", root / ".gitignore")
            (root / "scripts").mkdir()
            (root / "scripts" / "victim.py").write_text(
                'from _paths import tier\n'
                'refl = tier("reflections")\n'
                'reviews = sorted(refl.glob("*-growth-review.md"))\n',
                encoding="utf-8",
            )
            out = _run_py(
                f"""
                h.ROOT = Path({str(root)!r})
                findings = h.check_flat_tier_globs()
                print(json.dumps([(f.code, f.where) for f in findings]))
                """
            )
            self.assertTrue(any(c == "flat-tier-glob" and "victim.py:3" in w for c, w in out), out)


class IntentAgentsInProcedureGuardTest(unittest.TestCase):
    def test_declared_agent_missing_from_procedure_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-lint-") as tmp:
            root = Path(tmp)
            (root / "procs").mkdir()
            (root / "procs" / "good.md").write_text("Dispatch the **Thinker** then the Scribe.\n", encoding="utf-8")
            (root / "procs" / "bad.md").write_text("Runs retrieval inline; no dispatch.\n", encoding="utf-8")
            out = _run_py(
                f"""
                h.ROOT = Path({str(root)!r})
                intents = {{
                    "ok": {{"agents": ["thinker", "scribe"], "procedure": "procs/good.md"}},
                    "stale": {{"agents": ["researcher"], "procedure": "procs/bad.md"}},
                    "none": {{"agents": [], "procedure": "procs/bad.md"}},
                }}
                findings = h.check_intents_agents_in_procedure(intents)
                print(json.dumps([(f.code, f.where, f.severity) for f in findings]))
                """
            )
            self.assertEqual(len(out), 1, out)
            self.assertEqual(out[0][0], "intent-agent-not-in-procedure")
            self.assertIn("intents.stale", out[0][1])
            self.assertEqual(out[0][2], "ERROR")


class DecisionRecordPathGuardTest(unittest.TestCase):
    def test_dated_reflection_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-decision-path-") as tmp:
            root = Path(tmp)
            (root / ".claude" / "commands").mkdir(parents=True)
            (root / "protocols").mkdir()
            (root / ".claude" / "commands" / "decision.md").write_text(
                "<paths.reflections>/YYYY-MM-DD-decision-<slugified-topic>.md\n",
                encoding="utf-8",
            )
            (root / "protocols" / "session-continuity.md").write_text(
                "<paths.gtd>/decisions/*.md\n",
                encoding="utf-8",
            )
            out = _run_py(
                f"""
                h.ROOT = Path({str(root)!r})
                findings = h.check_decision_record_contract()
                print(json.dumps([(f.code, f.where) for f in findings]))
                """
            )
            self.assertIn(
                ["decision-record-path", ".claude/commands/decision.md"], out
            )


class SessionReplayConfigShapeGuardTest(unittest.TestCase):
    def test_bad_example_shape_is_flagged(self) -> None:
        import shutil
        with tempfile.TemporaryDirectory(prefix="atelier-lint-") as tmp:
            root = Path(tmp)
            shutil.copytree(REPO_ROOT / "harness", root / "harness")
            shutil.copy(REPO_ROOT / ".gitignore", root / ".gitignore")
            for extra in (".claude", ".codex", "scripts"):
                if (REPO_ROOT / extra).exists():
                    shutil.copytree(REPO_ROOT / extra, root / extra, ignore=shutil.ignore_patterns("__pycache__", "_results*", "*.log"))
            (root / "harness" / "session-replay.toml.example").write_text(
                '[session_replay]\nenabled = "yes"\nextra = 1\n', encoding="utf-8"
            )
            out = _run_py(
                f"""
                h.ROOT = Path({str(root)!r})
                findings = h.check_runtime_registry()
                print(json.dumps([f.code for f in findings]))
                """
            )
            self.assertIn("session-replay-config-shape", out, out)


class ThresholdSourceOfTruthTest(unittest.TestCase):
    """The 90/30/3 numbers live in autoevo_pending.py's flags; prose must
    point at the flags, and the flag defaults must match what prose quotes."""

    def test_defaults_and_prose_agree(self) -> None:
        import re
        helper = (REPO_ROOT / "scripts" / "autoevo_pending.py").read_text(encoding="utf-8")
        dedupe = re.search(r'"--dedupe-days", type=int, default=(\d+)', helper)
        max_age = re.search(r'"--max-age-days", type=int, default=(\d+)', helper)
        self.assertIsNotNone(dedupe)
        self.assertIsNotNone(max_age)
        self.assertIn('int(entry.get("surface_count", 0)) >= 3', helper)
        protocol = (REPO_ROOT / "protocols" / "autoevo.md").read_text(encoding="utf-8")
        self.assertIn(f"`--dedupe-days` window (default {dedupe.group(1)})", protocol)
        self.assertIn(f"`--max-age-days` (default {max_age.group(1)})", protocol)
        review = (REPO_ROOT / ".claude" / "commands" / "autoevo-review.md").read_text(encoding="utf-8")
        self.assertIn(f"`--max-age-days` (default {max_age.group(1)})", review)


class HotPathCeilingGuardTest(unittest.TestCase):
    def test_oversized_hot_path_file_is_flagged(self) -> None:
        out = _run_py(
            """
            import tempfile, pathlib
            tmp = pathlib.Path(tempfile.mkdtemp(prefix='atelier-ceiling-'))
            big = tmp / 'big.md'
            big.write_text('x' * 2048, encoding='utf-8')
            findings = h.check_hot_path_ceilings({str(big): 1024})
            print(json.dumps([f.code for f in findings]))
            """
        )
        self.assertIn("hot-path-ceiling", out)

    def test_real_hot_path_files_are_under_ceiling(self) -> None:
        out = _run_py(
            """
            findings = h.check_hot_path_ceilings()
            print(json.dumps([f"{f.code}:{f.path}" for f in findings]))
            """
        )
        self.assertEqual(out, [])


class BotTrailerBanGuardTest(unittest.TestCase):
    def test_reintroduced_trailer_is_flagged(self) -> None:
        out = _run_py(
            """
            import tempfile, pathlib
            tmp = pathlib.Path(tempfile.mkdtemp(prefix='atelier-trailer-'))
            bad = tmp / 'cmd.md'
            bad.write_text('Co-Authored-By: Atelier Autoevo Bot <x@y>', encoding='utf-8')
            findings = h.check_bot_trailer_banned(roots=[str(tmp)])
            print(json.dumps([f.code for f in findings]))
            """
        )
        self.assertIn("bot-trailer-banned", out)

    def test_repo_prompt_surfaces_are_clean(self) -> None:
        out = _run_py(
            """
            findings = h.check_bot_trailer_banned()
            print(json.dumps([f"{f.code}:{f.path}" for f in findings]))
            """
        )
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
