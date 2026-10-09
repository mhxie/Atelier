"""Fixture-vault knowledge, dining, and routine-declaration integration tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
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
        valid = dining_audit.audit(vault)
        expect(valid["ok"] is True, f"valid dining fixture failed: {valid}")
        expect(valid["stats"]["rows"] == 3, "dining row count drift")

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
            "[broken](missing.md)\n[remote](readwise:fixture)\n[[Missing Title]] [[2099-01-01]] `[[Name]]`\n",
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
            any("[[Missing Title]]" in f["detail"] for f in invalid["errors"])
            and not any(t in f["detail"] for f in invalid["errors"] for t in ("2099-01-01", "[[Name]]")),
            "dining audit missed a broken [[Title]] link or flagged a daily date or code span",
        )
        expect(
            "live_state_in_eligibility_catalog" in error_codes,
            "dining audit accepted live state in the eligibility catalog",
        )

def check_tracking_refresh_routine() -> None:
    jobs = tomllib.loads((ROOT / "routines/registry.toml").read_text(encoding="utf-8"))["routine"]
    tracking = next(row for row in jobs if row["name"] == "tracking-refresh")
    expect(
        tracking["cron"] == ["30 5 * * *", "30 17 * * *"]
        and tracking["timezone"] == "local"
        and tracking["argv"] == ["{python}", "scripts/refresh_tracking.py", "--json"]
        and tracking["retry_safe"] is True,
        "tracking refresh Prefect declaration drift",
    )

def check_daily_digest_routine() -> None:
    jobs = tomllib.loads((ROOT / "routines/registry.toml").read_text(encoding="utf-8"))["routine"]
    digest = next(row for row in jobs if row["name"] == "daily-digest")
    expect(
        digest["cron"] == ["20 6 * * *"]
        and digest["timezone"] == "local"
        and digest["argv"] == ["{python}", "scripts/routine_digest.py", "morning", "--refresh-quota", "--no-weather"]
        and digest["retry_safe"] is True
        and digest["retries"] == 1,
        "daily digest Prefect declaration drift",
    )


def check_wiki_trust_routine() -> None:
    jobs = tomllib.loads((ROOT / "routines/registry.toml").read_text(encoding="utf-8"))["routine"]
    row = next(row for row in jobs if row["name"] == "wiki-trust-report")
    expect(row["cron"] == ["0 7 * * *"] and row["argv"] == ["{python}", "scripts/wiki_trust.py", "--write"]
           and row["retry_safe"] is True, "wiki trust report Prefect declaration drift")


class VaultIntegrationTest(unittest.TestCase):
    test_dining_audit = staticmethod(check_dining_audit)
    test_tracking_refresh_declaration = staticmethod(check_tracking_refresh_routine)
    test_daily_digest_declaration = staticmethod(check_daily_digest_routine)
    test_wiki_trust_declaration = staticmethod(check_wiki_trust_routine)


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

    def cli(self, script, *args, expected=0, paths=None, bin_dir=None):
        # Run the real entrypoint with only the public registry and fixture OV.
        bootstrap = (
            "import runpy,sys,tomllib; from pathlib import Path; "
            "sys.path.insert(0,str(Path.cwd()/'scripts')); import _paths; "
            f"_paths._registry=lambda:tomllib.loads(Path('harness/paths.toml').read_text())['paths']|{(paths or {})!r}; "
            "script=sys.argv.pop(1); runpy.run_path(str(Path('scripts')/script),run_name='__main__')"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", bootstrap, script, *map(str, args)],
            cwd=ROOT, env={**os.environ, "OV": str(self.vault),
                           **({"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"} if bin_dir else {})},
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
        expected = examples + "[Plain](moved/Plain.md)\n[Draft](moved/Paren%20%28Draft%29.md#section)\n"
        self.assertEqual(source.read_text(), expected)
        self.cli("relink.py", "--apply", "--quiet")
        self.assertEqual(source.read_text(), expected)

    def test_to_reflect_preserves_fences_and_inline_code(self):
        self.put("research/Plain.md", "target\n")
        examples = (
            "`[Plain](Plain.md)`\n``[Plain](Plain.md) with ` inside``\n"
            "~~~markdown\n[Plain](Plain.md)\n~~~~\n"
            "````markdown\n```\n[Plain](Plain.md)\n````\n"
        )
        trailing = "~~~markdown\n[Plain](Plain.md)\n"
        source = self.put("research/source.md", examples + "[Plain](Plain.md)\n" + trailing)
        self.cli("relink.py", "--apply", "--to-reflect", "--quiet")
        self.assertEqual(source.read_text(), examples + "[[Plain]]\n" + trailing)

    def test_to_reflect_links_titles_or_rooted_paths(self):
        self.put("research/labs/acme/Profile.md", '---\ntitle: "Acme Profile"\n---\n\nbody\n')
        self.put("personal/Plan.md", "target\n")
        self.put("research/a/Same.md", "# Same\n")
        self.put("research/b/Other.md", "# Same\n")
        self.put("research/images/a b.png", "png\n")
        body = (
            "[Acme Profile](labs/acme/Profile.md) [the plan](<../personal/Plan.md>)\n"
            "[Same](a/Same.md) [part](labs/acme/Profile.md#h) [gone](Missing.md)\n"
            "| [Plan](../personal/Plan.md) | [the plan](../personal/Plan.md) |\n"
            "![chart](<images/a b.png>)\n"
        )
        source = self.put("research/source.md", body)
        daily = self.put("daily/2099-01-01.md", "[Plan](../personal/Plan.md)\n")
        result = self.cli("relink.py", "--apply", "--to-reflect", "--quiet")
        expected = (
            "[[Acme Profile]] [[Plan|the plan]]\n"
            "[[/research/a/Same|Same]] [[Acme Profile#h|part]] [gone](Missing.md)\n"
            "| [[Plan]] | [[Plan]] |\n"
            "![chart](images/a%20b.png)\n"
        )
        self.assertEqual(source.read_text(), expected)
        self.assertEqual(daily.read_text(), "[Plan](../personal/Plan.md)\n")
        self.assertIn("converted_path=1", result.stderr)
        self.assertIn("converted_table=1", result.stderr)
        self.cli("relink.py", "--apply", "--to-reflect", "--quiet")
        self.assertEqual(source.read_text(), expected)

    def test_to_reflect_keeps_uris_fragments_and_wrapped_text(self):
        self.put("research/Plan.md", "target\n")
        self.put("research/Scope: Thing.md", "# Scope: Thing\n")
        kept = (
            "[call](<tel:123 456>) [ref](<doi:10.1/x y>) [pdf](<doi:10.1/paper.md>)\n"
            "[soft\nwrap](Plan.md)\n"
        )
        source = self.put("research/source.md", kept + "[Scope: Thing](<Scope: Thing.md>) [jump](<#Section One>)\n")
        self.cli("relink.py", "--apply", "--to-reflect", "--quiet")
        self.assertEqual(source.read_text(), kept + "[[Scope: Thing]] [[#Section One|jump]]\n")

    def test_code_spans_do_not_cross_markdown_blocks(self):
        self.put("research/Plain.md", "target\n")
        source = self.put(
            "research/source.md",
            "` first\n\n~~~markdown\n[Plain](Plain.md)\n~~~\n\n[Plain](Plain.md) last `\n\n[Plain](Plain.md)\n",
        )
        self.cli("relink.py", "--apply", "--to-reflect", "--quiet")
        self.assertEqual(source.read_bytes(), b"` first\n\n~~~markdown\n[Plain](Plain.md)\n~~~\n\n[[Plain]] last `\n\n[[Plain]]\n")
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

    def test_promotion_candidates_follow_your_links_not_mtime(self):
        self.put("research/Topic.md", "# Topic\n\nAn idea worth keeping.\n")
        self.put("research/Board.md", "# Board\n\nLast built: 2099-01-01\n")
        self.put("research/Cited.md", "# Cited\n\nAlready a wiki source.\n")
        self.put("research/2099-01-07-weekly.md", "# 2099-01-07-weekly\n")
        for name in ("a", "b"):
            self.put(f"reflections/{name}.md", f"# {name}\n\n[[Topic]] [[Board]] [[Cited]] [[2099-01-07-weekly]]\n")
        self.put("wiki/Entry.md", "# Entry\n\nSee [[Cited]].\n")
        notes = json.loads(self.cli("staleness.py", "--json").stdout)["notes"]
        self.assertEqual([Path(n["path"]).name for n in notes if n["category"] == "promote"], ["Topic.md"])

    def test_review_overlays_match_reflects_shared_claim_trust_cases(self):
        """The canonical claim-trust cases: Atelier computes tiers and overlays; Reflect renders them."""
        import trust
        import wiki_trust

        source = self.put("wiki/Case.md", "")
        for case in json.loads((ROOT / "tests/fixtures/wiki-claim-trust.json").read_text())["cases"]:
            with self.subTest(case=case["name"]):
                fence = "" if case["ledger"] is None else "```anchors c1\n" + "\n".join(case["ledger"]) + "\n```\n"
                source.write_text("# Case\n\nIntro <!-- claim:c1 -->a claim.<!-- /claim:c1 -->\n\n## References\n\n" + fence)
                [claim] = trust.parse_wiki_note(source, date.today()).claims
                as_of = date.fromisoformat(case["asOf"])
                overlays = {"disputed": claim.dispute(as_of) is not None, "edited": claim.review(as_of) == "pending"}
                self.assertEqual(sorted(name for name, on in overlays.items() if on), sorted(case["overlays"]))
                result = wiki_trust.claim_trust(claim, as_of, case["citedTiers"])
                self.assertEqual((result["tier"], sorted(result["overlays"])), (case["tier"], sorted(case["overlays"])))

    def test_claim_text_hashes_match_reflects_vectors(self):
        import trust
        import wiki_trust

        source = self.put("wiki/Hash.md", "")
        for case in json.loads((ROOT / "tests/fixtures/wiki-claim-text-hashes.json").read_text())["cases"]:
            with self.subTest(case=case["name"]):
                source.write_bytes(case["markdown"].encode("utf-8"))
                data = source.read_bytes()
                hashes = {f"c{c.number}": wiki_trust.claim_hash(data, c) for c in trust.parse_wiki_note(source, date.today()).claims}
                self.assertEqual({k: v for k, v in hashes.items() if v}, case["hashes"])

    def test_trust_report_hashes_claim_bytes_and_publishes_to_meta(self):
        anchors = ("@anchor: arxiv:2409.19256 | valid_at: 2020-01-01\n"
                   "@anchor: url:https://docs.example.org/a | valid_at: 2020-01-01\n")
        self.put("wiki/Source.md", "# Source\n\nIntro <!-- claim:c1 -->Café claims hold.<!-- /claim:c1 -->\n\n## References\n\n"
                 "```anchors c1\n" + anchors + "@pass: challenger | status: verified | at: 2020-02-01\n```\n")
        metadata = '<!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} -->'
        self.put("wiki/Reader.md", "# Reader\n\nIntro <!-- claim:c1 -->Derived [[Source#^c1|ref]]" + metadata
                 + "<!-- /claim:c1 -->\n\n## References\n\n```anchors c1\n```\n")
        self.put("wiki/Hidden.md", "---\nprivate: true\n---\n# Hidden\n\nIntro <!-- claim:c1 -->Secret.<!-- /claim:c1 -->\n")
        report = json.loads(self.cli("wiki_trust.py").stdout)
        from jsonschema import Draft202012Validator

        schema = json.loads((ROOT / "tests/fixtures/wiki-trust-report.schema.json").read_text())
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(report)), [])
        claims = {path: note["claims"]["c1"] for path, note in report["notes"].items()}
        self.assertEqual((claims["wiki/Source.md"]["tier"], claims["wiki/Reader.md"]["tier"]), ("solid", "supported"))
        self.assertEqual(claims["wiki/Source.md"]["text_sha256"], hashlib.sha256("Café claims hold.".encode()).hexdigest())
        self.assertNotIn("wiki/Hidden.md", report["notes"])
        self.assertTrue(report["sources"]["arxiv:2409.19256"]["trusted"])
        self.cli("wiki_trust.py", "--write")
        written = json.loads((self.vault / "_meta/wiki-trust.json").read_text())
        self.assertEqual({**written, "generated_at": None}, {**report, "generated_at": None})

    def test_trust_report_leaves_out_reflects_local_only_folders(self):
        (self.vault / ".reflect").mkdir()
        self.put("wiki/Open.md", "# Open\n\nIntro <!-- claim:c1 -->Shared.<!-- /claim:c1 -->\n")
        self.put("wiki/Secure/Bank.md", "# Bank\n\nIntro <!-- claim:c1 -->Account 1234.<!-- /claim:c1 -->\n\n## References\n\n"
                 "```anchors c1\n@anchor: url:https://bank.example/x | valid_at: 2020-01-01\n```\n")
        bin_dir = tempfile.TemporaryDirectory(prefix="atelier-fake-reflect-")
        self.addCleanup(bin_dir.cleanup)
        target = self.vault / ".harness/wiki-trust.json"

        def fake_reflect(exit_code):
            # Reflect's CLI as a harness sees it; exit 3 means local-only folders are unknown.
            answer = json.dumps({"absolutePath": str(target), "localOnlyFolders": ["secure"]})
            script = Path(bin_dir.name) / "reflect"
            script.write_text(f"#!/bin/sh\nprintf '%s' '{answer}'\nexit {exit_code}\n")
            script.chmod(0o755)

        fake_reflect(0)
        report = json.loads(self.cli("wiki_trust.py", bin_dir=bin_dir.name).stdout)
        self.assertIn("wiki/Open.md", report["notes"])
        self.assertNotIn("wiki/Secure/Bank.md", report["notes"])
        self.assertNotIn("host:bank.example", report["sources"])
        fake_reflect(3)
        self.cli("wiki_trust.py", "--write", expected=1, bin_dir=bin_dir.name)
        self.assertFalse(target.exists())

    def test_shadow_drift_matches_nested_domain_paths(self):
        note = "# Entry\n\n## Claims\n### [C1] Claim\nProse.\n```anchors\n@anchor: doi:fixture | valid_at: 2020-01-01\n```\n"
        self.put("wiki/topic/Entry.md", note)
        shadow = self.put("wiki-zh/topic/Entry.md", note)
        paths = {"wiki_localized": {"zh": "wiki-zh"}}

        def codes():
            return {f["code"] for f in json.loads(self.cli("lint.py", "--json", paths=paths).stdout)["findings"]}
        self.assertNotIn("shadow-missing", codes())
        shadow.unlink()
        self.assertIn("shadow-missing", codes())

    def test_numbered_references_preserve_legacy_trust_and_time_windows(self):
        self.put("wiki/Source.md", "# Source\n\n## Claims\n### [C1] Source claim\nEvidence.\n"
                 "```anchors\n@anchor: doi:fixture | valid_at: 2020-01-01\n```\n")
        source = self.put("wiki/Reader.md", "")
        prefix = "# Reader\n\n## Claims\n### [C1] Derived claim\nSupported prose.\n\n"
        fields = {"valid_at": "2020-01-01", "invalid_at": "2020-02-01"}
        reference = '[[Source#^c1|ref]]<!-- ' + json.dumps({"metadata": {"citation": fields}}) + ' -->'
        for day in ("2019-12-31", "2020-01-15", "2020-02-01"):
            with self.subTest(day=day):
                source.write_text(prefix + "@cite: [[Source#^c1]] | valid_at: 2020-01-01 | invalid_at: 2020-02-01\n")
                legacy = json.loads(self.cli("trust.py", "--as-of", day, "--json").stdout)
                source.write_text(prefix + reference + "\n")
                current = json.loads(self.cli("trust.py", "--as-of", day, "--json").stdout)
                self.assertEqual(current, legacy)
        source.write_text(prefix + "Additional context " + reference + ".\n")
        self.cli("lint.py", "--json")

    def test_numbered_references_ignore_code_comments_escapes_and_topic_links(self):
        metadata = '<!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} -->'
        ref = "[[Missing#^c1|ref]]" + metadata
        source = self.put("wiki/Examples.md", "# Examples\n\n## Claims\n### [C1] Examples\n"
                          "Ordinary [[Missing#^c1|topic]] and [[Missing|1]] links.\n\n"
                          f"`{ref}`\n\n~~~markdown\n{ref}\n~~~\n\n"
                          "<!-- [[Missing#^c1|ref]] -->\n\n"
                          f"\\{ref}\n\n[{ref}](https://example.com)\n\n!{ref}\n")
        payload = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)
        note = payload["notes"][0]
        self.assertTrue(note["integrity_ok"], note["parse_errors"])
        self.assertEqual(note["claims"][0]["cites"], 0)

    def test_numbered_references_reject_missing_malformed_or_unknown_metadata(self):
        source = self.put("wiki/Reader.md", "")
        for fields in (None, {}, [], {"valid_at": 1}, {"valid_at": "2020-02-30"},
                       {"valid_at": "2999-01-01"}, {"valid_at": "2020-01-02", "invalid_at": "2020-01-01"},
                       {"valid_at": "2020-01-01", "confidence": "high"}):
            with self.subTest(fields=fields):
                comment = "" if fields is None else '<!-- ' + json.dumps({"metadata": {"citation": fields}}) + ' -->'
                source.write_text("# Reader\n\n## Claims\n### [C1] A claim\nBody [[Source|ref]]" + comment + "\n")
                payload = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)
                self.assertFalse(payload["notes"][0]["integrity_ok"])
                self.cli("lint.py", "--json", expected=1)
        for reference in ('[[|ref]]' + '<!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} -->',
                          '[[Source|ref]] <!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} -->'):
            source.write_text("# Reader\n\n## Claims\n### [C1] A claim\nBody " + reference + "\n")
            payload = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)
            self.assertFalse(payload["notes"][0]["integrity_ok"])

    def test_numbered_references_resolve_claims_and_do_not_replace_body_prose(self):
        self.put("wiki/Source.md", "# Source\n\n## Claims\n### [C1] Source claim\nEvidence.\n")
        comment = '<!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} -->'
        source = self.put("wiki/Reader.md", "")
        prefix = "# Reader\n\n## Claims\n### [C1] Derived claim\n"
        for target, body, error in (("Source#^c1", "", "has no body text"),
                                    ("Source#^c2", "Prose ", "does not exist")):
            with self.subTest(target=target):
                source.write_text(prefix + body + "[[" + target + "|ref]]" + comment + "\n")
                payload = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)
                errors = " ".join(payload["notes"][0]["parse_errors"])
                self.assertIn(error, errors)
        source.write_text(prefix + "One [[Source#^c1|ref]]" + comment + " and two [[Source|ref]]" + comment + ".\n")
        payload = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)
        self.assertTrue(payload["notes"][0]["integrity_ok"])
        self.assertEqual(payload["notes"][0]["claims"][0]["cites"], 2)
        source.write_text(prefix + "Literal bang \\![[Source#^c1|ref]]" + comment + ".\n")
        payload = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)
        self.assertEqual(payload["notes"][0]["claims"][0]["cites"], 1)
        for template in ("{}.", "- {}", "1. {}", "**{}**", "<!-- metadata only -->\n{}"):
            source.write_text(prefix + template.format("[[Source#^c1|ref]]" + comment) + "\n")
            payload = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)
            self.assertIn("has no body text", " ".join(payload["notes"][0]["parse_errors"]))

    def test_numbered_references_require_a_claim_body_location(self):
        reference = '[[Source|ref]]<!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} -->'
        source = self.put("wiki/Reader.md", "")
        for body in (reference + "\n### [C1] Claim\nProse.\n",
                     "[[Source|ref]]<!-- bad -->\n### [C1] Claim\nProse.\n",
                     "### [C1] Claim " + reference + "\nProse.\n"):
            source.write_text("# Reader\n\n## Claims\n" + body)
            payload = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)
            self.assertFalse(payload["notes"][0]["integrity_ok"])

    def test_article_ranges_preserve_evidence_graph_and_time_windows(self):
        import trust

        anchors = ("@anchor: doi:fixture | valid_at: 2020-01-01\n"
                   "@anchor: url:https://example.org/old | valid_at: 2020-01-01 | invalid_at: 2020-02-01 | readwise: fixture\n")
        review = "@pass: reviewer | status: verified | at: 2020-01-01\n"
        fields = {"valid_at": "2020-01-01", "invalid_at": "2020-02-01"}
        reference = '[[Source#^c1|ref]]<!-- ' + json.dumps({"metadata": {"citation": fields}}) + ' -->'
        source = self.put("wiki/Source.md", "# Source\n\n## Claims\n### [C1] Source\nEvidence.\n```anchors\n" + anchors + "```\n")
        reader = self.put("wiki/Reader.md", "# Reader\n\n## Claims\n### [C1] First\nSupported prose.\n"
                          "@cite: [[Source#^c1]] | valid_at: 2020-01-01 | invalid_at: 2020-02-01\n"
                          "```anchors\n" + review + "```\n### [C2] Second\nContext.\n")
        old = [trust.parse_wiki_note(p, date.today()) for p in (source, reader)]
        source.write_text("# Source\n\n<!-- claim:c1 -->\n\nEvidence.<!-- /claim:c1 -->\n\n## Evidence\n```anchors c1\n" + anchors + "```\n")
        reader.write_text("# Reader\n\n## Natural topic\n\n<!-- claim:c2 -->\n\nContext.<!-- /claim:c2 -->\n\n"
                          "The next explanation includes <!-- claim:c1 -->supported prose. " + reference +
                          "\n\nMore detail.<!-- /claim:c1 -->\n\n## Evidence\n```anchors c1\n" + review + "```\n```anchors c2\n```\n")
        new = [trust.parse_wiki_note(p, date.today()) for p in (source, reader)]
        def evidence(notes):
            return {c.key: {kind: [m.fields for m in getattr(c, kind)] for kind in ("anchors", "cites", "passes")}
                    for n in notes for c in n.claims}
        self.assertEqual(evidence(new), evidence(old))
        self.assertTrue(all(n.integrity_ok() for n in new), [n.parse_errors for n in new])
        for day in (date(2019, 12, 31), date(2020, 1, 15), date(2020, 2, 1)):
            with self.subTest(day=day), patch.object(trust, "vault_root", return_value=self.vault):
                self.assertEqual(trust.score_notes(new, day), trust.score_notes(old, day))

    def test_article_ranges_own_same_line_references_and_report_original_utf8_offsets(self):
        import trust

        metadata = '<!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} -->'
        body = ('中文 😀 before <!-- claim:c4 -->first [[Source#^c1|ref]]' + metadata +
                '<!-- /claim:c4 --> then <!-- claim:c2 -->second [[Other|ref]]' + metadata + '<!-- /claim:c2 --> after.')
        text = "# Reader\r\n\r\n" + body + "\r\n"
        source = self.put("wiki/Reader.md", "")
        source.write_bytes(text.encode("utf-8"))
        note = trust.parse_wiki_note(source, date.today())
        self.assertTrue(note.integrity_ok(), note.parse_errors)
        self.assertEqual([(c.number, c.cites[0].fields["_cite_title"]) for c in note.claims], [(4, "Source"), (2, "Other")])
        for claim in note.claims:
            start = text.index(f"<!-- claim:c{claim.number} -->") + len(f"<!-- claim:c{claim.number} -->")
            end = text.index(f"<!-- /claim:c{claim.number} -->")
            self.assertEqual(claim.source_range, (start, end))
            self.assertEqual(source.read_bytes()[slice(*claim.range_utf8)].decode("utf-8"), text[start:end])
        self.assertEqual(source.read_bytes(), text.encode("utf-8"))

    def test_localized_claims_keep_frontmatter_identity_with_a_simple_h1(self):
        import trust

        source = self.put("wiki-cn/Example.md", '---\ntitle: "Example (中文)"\nlang: zh-CN\n---\n# Example\n\n<!-- claim:c1 -->\n\n正文。<!-- /claim:c1 -->\n')
        note = trust.parse_wiki_note(source, date.today())
        self.assertEqual(note.title, "Example (中文)")
        self.assertTrue(note.integrity_ok(), note.parse_errors)
        self.assertEqual([c.number for c in note.claims], [1])

    def test_article_range_errors_never_infer_ownership(self):
        import trust

        opening, closing = "<!-- claim:c1 -->", "<!-- /claim:c1 -->"
        invalid = (
            opening + closing, "Before " + opening + "unclosed", "Before " + closing,
            "Before " + closing + " backwards " + opening,
            "Before " + opening + "one" + closing + opening + "two" + closing,
            "Before " + opening + "outer <!-- claim:c2 -->inner<!-- /claim:c2 -->" + closing,
            "Before " + opening + "outer <!-- claim:c2 -->cross" + closing + "<!-- /claim:c2 -->",
            "Before <!-- claim:c01 -->bad<!-- /claim:c01 -->",
            "Before <!-- claim:c0 -->bad<!-- /claim:c0 -->",
            "Before <!-- claim:c1٢ -->bad<!-- /claim:c1٢ -->",
            "Before <!-- claim:c1", opening + "same-line **Markdown**" + closing,
            "a*" + opening + "formatting" + closing + "*b",
            "## Heading " + opening + "bad" + closing,
            "[" + opening + "linked" + closing + "](https://example.org)",
            "[[Topic " + opening + "link" + closing + "]]",
            opening + "\n\nProse\n\n## Revision Log\n\nHistory" + closing,
            "## Revision Log\n\n" + opening + "\n\nHistory" + closing,
            "## Evidence\n\n" + opening + "\n\nAdministrative" + closing,
            "## References\n\n" + opening + "\n\nAdministrative" + closing,
            opening + "\n\nProse\n\n```anchors c1\n```\n\n" + closing,
            "Before " + opening + "[[Source|ref]]" + closing + '<!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} -->',
            opening + "\nNo blank line" + closing, "- " + opening + "List item" + closing,
            "> " + opening + "Quote" + closing, "| a | b |\n|---|---|\n| " + opening + "x | y" + closing + " |",
            "| a |\n|---|\n| " + opening + "x |\n| y" + closing + " |",
        )
        source = self.put("wiki/Reader.md", "")
        for body in (opening + "\n\nOwn line" + closing, "- Item " + opening + "claim" + closing,
                     "| a | b |\n|---|---|\n| " + opening + "x [[Source|y]]" + closing + " | z |"):
            with self.subTest(valid=body):
                source.write_text("# Reader\n\n" + body + "\n\n## References\n\n```anchors c1\n```\n")
                note = trust.parse_wiki_note(source, date.today())
                self.assertEqual(([c.number for c in note.claims], note.parse_errors), ([1], []), body)
        for body in invalid:
            with self.subTest(body=body):
                source.write_text("# Reader\n\n" + body + "\n")
                note = trust.parse_wiki_note(source, date.today())
                self.assertFalse(note.integrity_ok(), body)
        source.write_text("# Reader\n\nBefore " + opening + "unclosed [[Source|ref]]"
                          '<!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} -->\n')
        self.assertEqual(trust.parse_wiki_note(source, date.today()).claims, [])

    def test_article_ranges_ignore_literal_examples_and_validate_ledger_owners(self):
        import trust

        base = "# Reader\n\n<!-- claim:c4 -->\n\nActual assertion.<!-- /claim:c4 -->\n\n"
        examples = ("`<!-- claim:c1 -->` and \\<!-- claim:c2 -->\n\n"
                    "~~~markdown\n<!-- claim:c3 -->\n## Claims\n### [C1] Example\n~~~\n\n"
                    "<!-- Example: <!-- claim:c5 -->\n\n")
        source = self.put("wiki/Reader.md", base + examples)
        note = trust.parse_wiki_note(source, date.today())
        self.assertTrue(note.integrity_ok(), note.parse_errors)
        self.assertEqual([c.number for c in note.claims], [4])
        for ledger in ("```anchors c7\n```", "```anchors c4 extra\n```", "```anchors c4\nunknown\n```",
                       "```anchors c4\n```\n```anchors c4\n```", "```anchors c4\n@anchor: doi:x | valid_at: 2020-01-01",
                       "```anchors c4\n@unknown: x\n```", "```anchors\n```"):
            with self.subTest(ledger=ledger):
                source.write_text(base + "## Evidence\n\n" + ledger + "\n")
                self.assertFalse(trust.parse_wiki_note(source, date.today()).integrity_ok())
        source.write_text(base + "## Claims\n### [C4] Duplicate identity\nBody.\n")
        self.assertIn("duplicate or mixed", " ".join(trust.parse_wiki_note(source, date.today()).parse_errors))
        source.write_text(base + "## Claims\n### [C1٢] Invalid identity\nBody.\n")
        self.assertIn("malformed legacy claim", " ".join(trust.parse_wiki_note(source, date.today()).parse_errors))
        source.write_text(base + "## References\n\n```anchors c4\n@anchor: doi:fixture | valid_at: 2020-01-01\n```\n")
        note = trust.parse_wiki_note(source, date.today())
        self.assertTrue(note.integrity_ok(), note.parse_errors)
        self.assertEqual(len(note.claims[0].anchors), 1)

    def test_legacy_cite_examples_inside_inline_code_or_comments_are_not_evidence(self):
        import trust

        record = "@cite: [[Missing]] | valid_at: 2020-01-01"
        for example in ("`example\n" + record + "\nend`", "<!-- example\n" + record + "\n-->"):
            with self.subTest(example=example):
                source = self.put("wiki/Reader.md", "# Reader\n\nBefore <!-- claim:c1 -->Assertion with "
                                  + example + " text.<!-- /claim:c1 -->\n")
                note = trust.parse_wiki_note(source, date.today())
                self.assertTrue(note.integrity_ok(), note.parse_errors)
                self.assertEqual(note.claims[0].cites, [])
                self.assertIn(record, "\n".join(note.claims[0].body_lines))

    def test_article_provenance_never_creates_nonwiki_or_unowned_graph_edges(self):
        self.put("wiki/Source.md", "# Source\n\n## Claims\n### [C1] Seed\nEvidence.\n"
                 "```anchors\n@anchor: doi:fixture | valid_at: 2020-01-01\n```\n")
        self.put("research/Ordinary.md", "# Ordinary\n\n<!-- claim:c7 -->\n\nAn ordinary assertion.<!-- /claim:c7 -->\n")
        metadata = '<!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} -->'
        source = self.put("wiki/Reader.md", "")
        for target in ("Ordinary", "Ordinary#^c7"):
            source.write_text("# Reader\n\nOutside [[Source#^c1|ref]]" + metadata +
                              "\n\n<!-- claim:c2 -->\n\nAssertion [[" + target + "|ref]]" + metadata + "<!-- /claim:c2 -->\n")
            payload = json.loads(self.cli("trust.py", "--json").stdout)
            notes = {n["title"]: n for n in payload["notes"]}
            self.assertEqual(set(notes), {"Reader", "Source"})
            self.assertTrue(notes["Reader"]["integrity_ok"], notes["Reader"]["parse_errors"])
            self.assertEqual(notes["Reader"]["note_score"], 0)
            self.assertEqual(notes["Reader"]["claims"][0]["cites"], 1)
            self.assertEqual(len(notes["Reader"]["claims"][0]["range_utf8"]), 2)
        for target in ("Missing", "Ordinary#^c1", "Source#^c2", "Source#^c1٢"):
            source.write_text("# Reader\n\n<!-- claim:c2 -->\n\nAssertion [[" + target + "|ref]]" + metadata + "<!-- /claim:c2 -->\n")
            payload = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)
            self.assertFalse(payload["notes"][0]["integrity_ok"])

    def test_wiki_citations_fail_on_global_reflect_title_ambiguity(self):
        self.put("wiki/Shared.md", "# Shared\n\n## Claims\n### [C1] A source\nEvidence.\n"
                 "```anchors\n@anchor: doi:fixture | valid_at: 2020-01-01\n```\n")
        self.put("research/Shared.md", "# Shared\n\nA separate source.\n")
        source = self.put("wiki/Reader.md", "# Reader\n\n<!-- claim:c1 -->\n\nAssertion [[Shared|ref]]"
                          '<!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} --><!-- /claim:c1 -->\n')
        note = json.loads(self.cli("trust.py", "--note", source, "--json").stdout)["notes"][0]
        self.assertFalse(note["integrity_ok"])
        self.assertIn("ambiguous", " ".join(note["parse_errors"]))
        self.assertEqual(note["note_score"], 0)

    def test_unreadable_notes_do_not_abort_unrelated_trust_scores(self):
        import _reflect

        source = self.put("wiki/Source.md", "# Source\n\n## Claims\n### [C1] Seed\nEvidence.\n"
                          "```anchors\n@anchor: doi:fixture | valid_at: 2020-01-01\n```\n")
        unavailable = self.put("wiki/Unavailable.md", "# Unavailable\n\n## Claims\n### [C1] Hidden\nBody.\n")
        ordinary = self.put("research/Unavailable.md", "# Ordinary unavailable\n")
        citation = '<!-- {"metadata":{"citation":{"valid_at":"2020-01-01"}}} -->'
        reader = self.put("wiki/Reader.md", "")
        for path in (unavailable, ordinary):
            path.chmod(0)
            self.addCleanup(path.chmod, 0o600)
        with self.assertRaises(PermissionError):
            _reflect.TitleIndex(self.vault)
        for target in ("Source#^c1", "Unavailable", "Ordinary unavailable"):
            with self.subTest(target=target):
                reader.write_text("# Reader\n\nBefore <!-- claim:c1 -->Assertion [[" + target + "|ref]]"
                                  + citation + "<!-- /claim:c1 -->\n")
                payload = json.loads(self.cli("trust.py", "--json").stdout)
                by_path = {Path(n["path"]): n for n in payload["notes"]}
                self.assertTrue(by_path[source]["integrity_ok"])
                self.assertGreater(by_path[source]["note_score"], 0)
                self.assertIn("read error", " ".join(by_path[unavailable]["parse_errors"]))
                self.assertEqual(by_path[unavailable]["note_score"], 0)
                self.assertEqual(by_path[reader]["integrity_ok"], target == "Source#^c1")
                if target != "Source#^c1":
                    self.assertEqual(by_path[reader]["note_score"], 0)
                    self.assertIn("not found", " ".join(by_path[reader]["parse_errors"]))

    def test_editor_pending_requires_its_own_pair_and_preserves_reviewer_floor(self):
        import trust

        prefix = "# Reader\n\n<!-- claim:c1 -->\n\nAn assertion.<!-- /claim:c1 -->\n\n## Evidence\n```anchors c1\n"
        reviewer = "@pass: reviewer | status: verified | at: 2020-01-01\n"
        source = self.put("wiki/Reader.md", prefix + reviewer + "```\n")
        before = trust.parse_wiki_note(source, date.today())
        source.write_text(prefix + reviewer + "@pass: editor | status: pending | at: 2020-02-01\n```\n")
        after = trust.parse_wiki_note(source, date.today())
        self.assertTrue(after.integrity_ok(), after.parse_errors)
        self.assertEqual(trust.score_notes([before], date.today()), trust.score_notes([after], date.today()))
        self.assertEqual(trust.score_notes([after], date.today())[0][after.claims[0].key], 0.1)
        source.write_text(prefix + "@pass: editor | status: pending | at: 2020-02-01\n```\n")
        pending_only = trust.parse_wiki_note(source, date.today())
        self.assertEqual(trust.score_notes([pending_only], date.today())[0][pending_only.claims[0].key], 0)
        for marker in ("@pass: editor | status: verified | at: 2020-01-01",
                       "@pass: reviewer | status: pending | at: 2020-01-01",
                       "@pass: editor | status: pending | valid_at: 2020-01-01",
                       "@pass: editor | status: pending | at: bad | valid_at: 2020-01-01"):
            with self.subTest(marker=marker):
                source.write_text(prefix + marker + "\n```\n")
                self.assertFalse(trust.parse_wiki_note(source, date.today()).integrity_ok())

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
