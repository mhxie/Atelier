"""Fixture-vault knowledge, dining, and routine-declaration integration tests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent

import dining_audit  # noqa: E402

from tests.support import (  # noqa: E402
    expect,
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

def check_tracking_refresh_routine() -> None:
    jobs = tomllib.loads((ROOT / "harness/routine_jobs.toml").read_text(encoding="utf-8"))["job"]
    tracking = next(row for row in jobs if row["name"] == "tracking-refresh")
    expect(
        tracking["cron"] == ["30 5 * * *", "30 17 * * *"]
        and tracking["timezone"] == "local"
        and tracking["argv"] == ["{python}", "scripts/refresh_tracking.py", "--json"]
        and tracking["retry_safe"] is True,
        "tracking refresh Prefect declaration drift",
    )


class VaultIntegrationTest(unittest.TestCase):
    test_dining_audit = staticmethod(check_dining_audit)
    test_tracking_refresh_declaration = staticmethod(check_tracking_refresh_routine)


class KnowledgeCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="atelier-knowledge-cli-")
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name).resolve()

    def put(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def cli(self, script, *args, expected=0, paths=None):
        # Run the real entrypoint with only the public registry and fixture OV.
        bootstrap = (
            "import runpy,sys,tomllib; from pathlib import Path; "
            "sys.path.insert(0,str(Path.cwd()/'scripts')); import _paths; "
            f"_paths._registry=lambda:tomllib.loads(Path('harness/paths.toml').read_text())['paths']|{(paths or {})!r}; "
            "script=sys.argv.pop(1); runpy.run_path(str(Path('scripts')/script),run_name='__main__')"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", bootstrap, script, *map(str, args)],
            cwd=ROOT, env={**os.environ, "OV": str(self.vault)},
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_fission_refuses_all_moves_on_collision(self):
        self.put("collision/Alpha.md", "incoming\n")
        self.put("collision/A/Alpha.md", "existing\n")
        self.put("collision/Beta.md", "must not move\n")
        before = {p: p.read_bytes() for p in self.vault.rglob("*.md")}
        for mode in ("--dry-run", "--apply"):
            result = self.cli(
                "fission.py", "--dir", self.vault / "collision", "--axis", "first-letter", mode,
                expected=1,
            )
            self.assertIn("collision", result.stderr)
            self.assertEqual({p: p.read_bytes() for p in self.vault.rglob("*.md")}, before)

    def test_fission_directory_buckets_are_idempotent(self):
        for axis, bucket, incoming in (
            ("first-letter", "A", "Alpha"),
            ("year-month", "2026-01", "2026-01-record"),
            ("year-month-split", "2026/01", "2026-01-record"),
        ):
            with self.subTest(axis=axis):
                base = self.vault / axis
                self.put(f"{axis}/{bucket}/existing.md", "existing\n")
                self.put(f"{axis}/{incoming}/child.md", "folder content\n")
                self.put(f"{axis}/{incoming}.md", "loose note\n")
                if axis == "first-letter":
                    self.put(f"{axis}/CJK/existing.md", "existing CJK bucket\n")
                    self.put(f"{axis}/0-9/existing.md", "existing digit bucket\n")
                args = ("--dir", base, "--axis", axis, "--include-dirs", "--apply")
                self.cli("fission.py", *args)
                after = {p: p.read_bytes() for p in base.rglob("*.md")}
                self.assertTrue((base / bucket / f"{incoming}.md").is_file())
                self.assertTrue((base / bucket / incoming / "child.md").is_file())
                self.cli("fission.py", *args)
                self.assertEqual({p: p.read_bytes() for p in base.rglob("*.md")}, after)

    def test_aggregate_nested_subjects_and_quoted_frontmatter(self):
        self.put("subjects/2026/new.md", "Last updated: 2026-09-09\n")
        for value in ('"subjects"', "'subjects'", "subjects"):
            with self.subTest(value=value):
                self.put("summary.md", f"---\nfreshness: required\nsubjects: {value}\n---\nLast updated: 2026-01-01\n")
                payload = json.loads(self.cli("aggregate_freshness.py", "--discover", "--stale-only", "--json").stdout)
                self.assertEqual(payload["stale_count"], 1)
                self.assertEqual(payload["groups"][0]["subject_count"], 1)
                self.assertEqual(payload["groups"][0]["warnings"], [])

    def test_aggregate_stale_filter_preserves_missing_subject_warning(self):
        self.put("summary.md", "---\nfreshness: required\nsubjects: missing\n---\n")
        payload = json.loads(self.cli("aggregate_freshness.py", "--discover", "--stale-only", "--json").stdout)
        self.assertEqual(len(payload["groups"]), 1)
        self.assertTrue(payload["groups"][0]["warnings"])

    def test_aggregate_unrelated_invalid_date_does_not_abort_discovery(self):
        self.put("subjects/new.md", "Last updated: 2026-09-09\n")
        self.put("summary.md", "---\nfreshness: required\nsubjects: subjects\n---\nLast updated: 2026-01-01\n")
        self.put("invalid.md", "---\nfreshness: required\nsubjects: subjects\nupdated: 2026-02-30\n---\n")
        payload = json.loads(self.cli("aggregate_freshness.py", "--discover", "--stale-only", "--json").stdout)
        self.assertEqual(payload["discovered"], 2)
        self.assertEqual(payload["stale_count"], 1)

    def test_relink_preserves_code_and_repairs_wrapped_parentheses(self):
        self.put("research/moved/Plain.md", "target\n")
        self.put("research/moved/Paren (Draft).md", "target\n")
        examples = (
            "`[code](old/Plain.md)`\n"
            "``[code](old/Plain.md) with ` inside``\n"
            "```markdown\n[code](old/Plain.md)\n```\n"
            "~~~markdown\n[code](old/Plain.md)\n~~~\n"
        )
        source = self.put("research/source.md", examples + "[Plain](old/Plain.md)\n[Draft](<old/Paren (Draft).md#section>)\n")
        self.cli("relink.py", "--apply", "--quiet")
        expected = examples + "[Plain](moved/Plain.md)\n[Draft](<moved/Paren (Draft).md#section>)\n"
        self.assertEqual(source.read_text(), expected)
        self.cli("relink.py", "--apply", "--quiet")
        self.assertEqual(source.read_text(), expected)

    def test_wikilink_preserves_fences_and_inline_code(self):
        self.put("research/Plain.md", "target\n")
        examples = (
            "`[[Plain]]`\n``[[Plain]] with ` inside``\n"
            "~~~markdown\n[[Plain]]\n~~~~\n"
            "````markdown\n```\n[[Plain]]\n````\n"
        )
        trailing = "~~~markdown\n[[Plain]]\n"
        source = self.put("research/source.md", examples + "[[Plain]]\n" + trailing)
        self.cli("wikilink_to_md.py", "--file", source, "--apply", "--quiet")
        self.assertEqual(source.read_text(), examples + "[Plain](Plain.md)\n" + trailing)

    def test_code_spans_do_not_cross_markdown_blocks(self):
        self.put("research/Plain.md", "target\n")
        source = self.put(
            "research/source.md",
            "` first\n\n~~~markdown\n[[Plain]]\n~~~\n\n[[Plain]] last `\n\n[[Plain]]\n",
        )
        self.cli("wikilink_to_md.py", "--file", source, "--apply", "--quiet")
        self.assertEqual(source.read_bytes(), b"` first\n\n~~~markdown\n[[Plain]]\n~~~\n\n[Plain](Plain.md) last `\n\n[Plain](Plain.md)\n")
        self.put("research/moved/Other.md", "target\n")
        source.write_bytes(b"` first\n\n~~~markdown\n[code](old/Other.md)\n~~~\n\n[link](old/Other.md) last `\n\n[link](old/Other.md)\n")
        self.cli("relink.py", "--apply", "--quiet")
        self.assertEqual(source.read_bytes(), b"` first\n\n~~~markdown\n[code](old/Other.md)\n~~~\n\n[link](moved/Other.md) last `\n\n[link](moved/Other.md)\n")

    def test_trust_and_lint_reject_empty_claims_and_bad_dates(self):
        source = self.put("wiki/Empty.md", "# Empty\n\n## Claims\n")
        payload = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)
        self.assertFalse(payload["notes"][0]["integrity_ok"])
        self.cli("lint.py", "--json", expected=1)
        for invalid in ("2026-01-01garbage", "20260101", "2026-02-30"):
            with self.subTest(invalid=invalid):
                source.write_text(
                    "# Empty\n\n## Claims\n### [C1] Evidence\nA fixture claim.\n"
                    f"```anchors\n@anchor: doi:fixture | valid_at: {invalid}\n```\n",
                    encoding="utf-8",
                )
                payload = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)
                self.assertFalse(payload["notes"][0]["integrity_ok"])
                self.assertEqual(payload["notes"][0]["note_score"], 0)
                self.cli("lint.py", "--json", expected=1)
                self.cli("trust.py", "--as-of", invalid, "--json", expected=2)

    def test_default_staleness_includes_bucketed_research(self):
        source = self.put("research/topic/note.md", "A working note.\n")
        payload = json.loads(self.cli("staleness.py", "--json").stdout)
        self.assertIn(str(source), [row["path"] for row in payload["notes"]])

    def test_people_lookup_survives_an_unrelated_bad_utf8_note(self):
        self.put("people/F/Fixture Person.md", "# Fixture Person\n")
        broken = self.put("people/Z/Other.md", "")
        broken.write_bytes(b"# Other\n\xff\n")
        with patch.dict(os.environ, {"ATELIER_PEOPLE_NAME_FIELD": "Alias"}):
            rows = json.loads(self.cli("people.py", "Fixture", "--json").stdout)
        self.assertEqual([row["stem"] for row in rows], ["Fixture Person"])

    def test_ingestion_audit_rejects_a_file_vault(self):
        source = self.put("not-a-directory", "fixture\n")
        with patch.object(self, "vault", source):
            report = json.loads(self.cli("zk_audit.py", "--json", expected=2).stdout)
        self.assertIn("error", report)

    def test_ingestion_audit_respects_remapped_tiers(self):
        self.put("health/README.md", "Health domain\n")
        self.put("parked/health-admin/empty.md", "")
        self.put("drafts/note.md", "A working draft\n")
        report = json.loads(self.cli("zk_audit.py", "--json", paths={"archive": "parked", "wip": "drafts"}).stdout)
        categories = report["categories"]
        self.assertEqual(categories["missing_readmes"], [])
        self.assertEqual(categories["suspicious_dirs"], [])
        self.assertEqual([row["where"] for row in categories["archive_overlap"]], ["$OV/parked/health-admin/"])
        self.assertEqual(categories["empty_md_archive_count"], 1)
        self.assertEqual(categories["empty_md"], [])
        self.put("health/raw/incoming/source.txt", "raw input\n")
        self.put("health/drafts/note.md", "A working draft\n")
        report = json.loads(self.cli("zk_audit.py", "--json", paths={"archive": "parked", "wip": "health/drafts"}).stdout)
        self.assertEqual([row["where"] for row in report["categories"]["raw_no_digest"]], ["$OV/health/raw/incoming/"])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.wiki = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="atelier-snapshot-")))
        with patch("_paths.tier", return_value=self.wiki):
            import snapshot_anchors
        self.snapshot = snapshot_anchors
        self.enterContext(patch.object(self.snapshot, "WIKI_DIR", self.wiki))

    def main(self, *args):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = self.snapshot.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_connector_matching_is_exact_and_bad_shapes_are_unsuccessful(self):
        url = "https://example.invalid/article"
        for docs, expected in (([{"source_url": url + "2", "id": "wrong"}], None),
                               ([None], None), ([{"source_url": url, "id": "right"}], "right")):
            with self.subTest(docs=docs), patch.object(self.snapshot.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, json.dumps(docs), "")):
                self.assertEqual(self.snapshot.search_readwise_for_url(url), expected)
        for payload in ([], None, {"id": ["bad"]}):
            with self.subTest(payload=payload), redirect_stderr(io.StringIO()), patch.object(
                self.snapshot.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")
            ):
                self.assertIsNone(self.snapshot.save_to_readwise(url))

    def test_existing_id_can_precede_other_anchor_fields(self):
        note = self.wiki / "note.md"
        note.write_text("@anchor: url:https://example.invalid/a | readwise: saved | invalid_at: 2026-09-09\n")
        self.assertEqual(self.snapshot.find_anchors_missing_readwise(note), [])

    def test_changed_anchor_is_not_backfilled_and_failure_exits_nonzero(self):
        note = self.wiki / "note.md"
        note.write_text("@anchor: url:https://example.invalid/old | valid_at: 2026-09-09\n")
        discovered = self.snapshot.find_anchors_missing_readwise(note)
        note.write_text("@anchor: url:https://example.invalid/new | valid_at: 2026-09-09\n")
        before = note.read_bytes()
        with patch.object(self.snapshot, "find_anchors_missing_readwise", return_value=discovered), \
             patch.object(self.snapshot, "search_readwise_for_url", return_value="old-id"):
            code, out, _ = self.main("--apply", "--note", str(note))
        self.assertEqual(code, 1, out)
        self.assertIn("1 failed", out)
        self.assertEqual(note.read_bytes(), before)

    def test_symlink_notes_are_refused_without_detaching_or_remote_lookup(self):
        target = self.wiki / "canonical.md"
        target.write_text("@anchor: url:https://example.invalid/a | valid_at: 2026-09-09\n")
        alias = self.wiki / "alias.md"
        alias.symlink_to(target)
        before = target.read_bytes()
        with patch.object(self.snapshot, "search_readwise_for_url") as lookup:
            code, _, err = self.main("--apply", "--note", str(alias))
        self.assertEqual(code, 2, err)
        lookup.assert_not_called()
        self.assertTrue(alias.is_symlink())
        discovered = self.snapshot.find_anchors_missing_readwise(target)
        discovered[0]["path"] = alias
        with patch.object(self.snapshot, "find_anchors_missing_readwise", return_value=discovered), \
             patch.object(self.snapshot, "search_readwise_for_url", return_value="saved-id"):
            code, _, _ = self.main("--apply", "--note", str(alias))
        self.assertEqual(code, 1)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(target.read_bytes(), before)

    def test_backfill_preserves_newlines_and_missing_note_is_io_failure(self):
        note = self.wiki / "note.md"
        for ending in (b"\n", b"\r\n", b""):
            original = b"@anchor: url:https://example.invalid/a | valid_at: 2026-09-09"
            note.write_bytes(original + ending)
            with patch.object(self.snapshot, "search_readwise_for_url", return_value="saved-id"):
                code, out, err = self.main("--apply", "--note", str(note))
            self.assertEqual(code, 0, out + err)
            self.assertEqual(note.read_bytes(), original + b" | readwise: saved-id" + ending)
            self.assertEqual(self.snapshot.find_anchors_missing_readwise(note), [])
        code, _, err = self.main("--note", str(self.wiki / "missing.md"))
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
