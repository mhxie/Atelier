"""Capture tiers: when a row still owes a 再去 value.

Guard for profile/diet.md "Capture tiers". 再去 is asked only while the log
cannot already answer it: a 正餐 row with fewer than 2 prior logged visits.
日常饮品 never asks. A dash outside those cases is complete, not pending.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import dining_audit  # noqa: E402

HEADER = (
    "| Date | Restaurant | City | 类型 | ⭐ | 评分 | 再去 | 健康 | 人数 "
    "| 总额 | 人均 | Platform | Credit | 必点·备注 |"
)
SEP = "|" + "---|" * 14


def _row(kind: str, again: str, note: str, day: int = 4, name: str = "Example Stop") -> str:
    return (
        f"| 2099-03-{day:02d} | {name} | Example City | {kind} | — | 7 | {again} "
        f"| — | — | — | — | — | — | {note} |"
    )


def _codes_for(rows: list[str]) -> set[str]:
    with tempfile.TemporaryDirectory(prefix="atelier-dining-audit-") as tmp:
        vault = Path(tmp)
        tracker = vault / "tracker.md"
        tracker.write_text("\n".join([HEADER, SEP, *rows]) + "\n", encoding="utf-8")
        profile = vault / "diet.md"
        profile.write_text("## Full health-flag taxonomy\n\n- `balanced` — ok\n", encoding="utf-8")
        findings, _ = dining_audit._audit_meal_history(tracker, profile, vault)
    return {f.code for f in findings}


def _codes(kind: str, again: str, note: str) -> set[str]:
    return _codes_for([_row(kind, again, note)])


class CaptureTierTest(unittest.TestCase):
    def test_beverage_row_may_omit_revisit(self) -> None:
        # 日常饮品 tier: dash 再去 is complete, not pending.
        self.assertNotIn("capture_pending", _codes("奶茶", "—", "待确认 人均"))
        self.assertNotIn("capture_pending", _codes("咖啡", "—", "待确认 人均"))

    def test_first_two_meal_visits_still_owe_revisit(self) -> None:
        # Visits 1 and 2 are where the log cannot yet answer the question.
        self.assertIn("capture_pending", _codes("川菜", "—", "待确认 人均"))
        self.assertIn(
            "capture_pending",
            _codes_for([
                _row("川菜", "Y", "note", day=1),
                _row("川菜", "—", "待确认 人均", day=2),
            ]),
        )

    def test_third_meal_visit_may_omit_revisit(self) -> None:
        # Returning a third time is the revisit answer; dash is complete.
        self.assertNotIn(
            "capture_pending",
            _codes_for([
                _row("川菜", "Y", "note", day=1),
                _row("川菜", "Y", "note", day=2),
                _row("川菜", "—", "待确认 人均", day=3),
            ]),
        )

    def test_visit_count_is_per_restaurant(self) -> None:
        # Three prior rows at other restaurants must not settle this one.
        self.assertIn(
            "capture_pending",
            _codes_for([
                _row("川菜", "Y", "note", day=1, name="Other A"),
                _row("川菜", "Y", "note", day=2, name="Other B"),
                _row("川菜", "—", "待确认 人均", day=3, name="Example Stop"),
            ]),
        )

    def test_beverage_row_still_owes_a_score(self) -> None:
        # The tier drops 再去 only. 评分 stays required in both tiers.
        self.assertIn("capture_pending", _codes_missing_score())

    def test_revisit_value_is_still_validated(self) -> None:
        self.assertIn("revisit_invalid", _codes("奶茶", "Maybe?", "note"))


class EstablishmentRegistryTest(unittest.TestCase):
    def test_validates_branch_lifecycle_and_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-establishments-") as tmp:
            vault = Path(tmp)
            catalog = vault / "catalog.md"
            catalog.write_text(
                "\n".join([
                    "| 餐厅 | 分店 | 地址 | 状态 | 核验日 | 来源 |",
                    "|---|---|---|---|---|---|",
                    "| Example Noodles | North Branch | 711 Example Rd | closed | 2099-03-04 | user |",
                    "| Example Noodles | South Branch | 4546 Example Rd | active | 2099-03-04 | official |",
                ]) + "\n",
                encoding="utf-8",
            )
            findings, rows = dining_audit._audit_establishment_registry(
                catalog, vault
            )
            self.assertFalse(findings)
            self.assertEqual([row["状态"] for row in rows], ["closed", "active"])

            tracker = vault / "tracker.md"
            tracker.write_text(
                "\n".join([
                    HEADER,
                    SEP,
                    _row("湘菜", "Y", "note", name="Example Noodles"),
                ]) + "\n",
                encoding="utf-8",
            )
            branch_findings = dining_audit._audit_branch_resolution(
                tracker, rows, vault
            )
            self.assertEqual(
                [finding.code for finding in branch_findings],
                ["establishment_branch_ambiguous"],
            )

            catalog.write_text(
                catalog.read_text(encoding="utf-8")
                + "| Example Noodles | North Branch | — | maybe | bad-date | user |\n",
                encoding="utf-8",
            )
            findings, _ = dining_audit._audit_establishment_registry(catalog, vault)
            self.assertEqual(
                {finding.code for finding in findings},
                {
                    "establishment_duplicate",
                    "establishment_identity_missing",
                    "establishment_status_invalid",
                    "establishment_verified_invalid",
                },
            )


def _codes_missing_score() -> set[str]:
    with tempfile.TemporaryDirectory(prefix="atelier-dining-audit-") as tmp:
        vault = Path(tmp)
        tracker = vault / "tracker.md"
        row = (
            "| 2099-03-04 | Example Stop | Example City | 奶茶 | — | — | Y "
            "| — | — | — | — | — | — | 待确认 人均 |"
        )
        tracker.write_text("\n".join([HEADER, SEP, row]) + "\n", encoding="utf-8")
        profile = vault / "diet.md"
        profile.write_text("## Full health-flag taxonomy\n\n- `balanced` — ok\n", encoding="utf-8")
        findings, _ = dining_audit._audit_meal_history(tracker, profile, vault)
    return {f.code for f in findings}


if __name__ == "__main__":
    unittest.main()
