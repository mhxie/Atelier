"""context_bundle: whole-section allocation, truncation, omission, and fitting."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import context_bundle as cb  # noqa: E402


def _candidate(ordinal: int, text: str, *, priority: int = 10, cap: int = 16 * 1024, component: str = "profile") -> cb.Candidate:
    return cb.Candidate(
        component=component, source=f"src-{ordinal}.md", section="full", representation="source_excerpt",
        text=text, source_bytes=len(text.encode("utf-8")), cap_bytes=cap, priority=priority, ordinal=ordinal,
    )


class AllocateExcerptsTest(unittest.TestCase):
    def test_whole_sections_in_priority_order_then_truncate_then_omit(self) -> None:
        big = _candidate(0, "a" * 5000, priority=10)
        second = _candidate(1, "b" * 3000, priority=20)
        third = _candidate(2, "c" * 3000, priority=30)
        omissions: list[dict] = []
        excerpts = cb.allocate_excerpts([big, second, third], omissions, 8500)
        by_ordinal = {e.candidate.ordinal: e for e in excerpts}
        self.assertEqual(by_ordinal[0].included_bytes, 5000)
        self.assertFalse(by_ordinal[0].truncated)
        self.assertEqual(by_ordinal[1].included_bytes, 3000)
        # 500 bytes remain: enough for a MIN_EXCERPT_BYTES fragment, so third is truncated, not dropped.
        self.assertEqual(by_ordinal[2].included_bytes, 500)
        self.assertIn("content_budget", by_ordinal[2].truncation_reasons)
        self.assertEqual(omissions, [])

    def test_fragments_below_the_floor_are_omitted_not_stubbed(self) -> None:
        big = _candidate(0, "a" * 5000, priority=10)
        second = _candidate(1, "b" * 3000, priority=20)
        omissions: list[dict] = []
        excerpts = cb.allocate_excerpts([big, second], omissions, 5100)
        self.assertEqual([e.candidate.ordinal for e in excerpts], [0])
        self.assertEqual(omissions[0]["reason"], "content_budget_exhausted")

    def test_component_cap_marks_truncation(self) -> None:
        capped = _candidate(0, "x" * 2000, cap=1000)
        omissions: list[dict] = []
        excerpts = cb.allocate_excerpts([capped], omissions, 10_000)
        self.assertEqual(excerpts[0].included_bytes, 1000)
        self.assertIn("component_cap", excerpts[0].truncation_reasons)

    def test_multibyte_truncation_never_splits_a_character(self) -> None:
        text = "汉" * 1000  # 3 bytes each
        omissions: list[dict] = []
        excerpts = cb.allocate_excerpts([_candidate(0, text)], omissions, 1001)
        content = excerpts[0].content
        self.assertLessEqual(excerpts[0].included_bytes, 1001)
        self.assertNotIn("\ufffd", content)
        kept = content.split("\n")[0]
        self.assertTrue(kept and set(kept) == {"汉"}, kept[:20])
        self.assertTrue(excerpts[0].truncated)


class FitRenderedOutputTest(unittest.TestCase):
    def test_entire_output_fits_the_budget(self) -> None:
        route = {"input": "intent", "name": "review", "mode": "goal-review", "procedure": "x.md",
                 "context_budget_bytes": 4096, "profile_reads": ["identity.md"], "registry": "harness/intents.toml"}
        candidates = [_candidate(i, chr(97 + i) * 3000, priority=10 + i) for i in range(4)]
        omissions: list[dict] = []
        excerpts = cb.allocate_excerpts(candidates, omissions, 4096)
        from datetime import date

        rendered = cb.fit_rendered_output(
            route=route, effective_date=date(2099, 1, 1), components=["profile"], excerpts=excerpts,
            omissions=omissions, byte_budget=4096, output_format="json",
        )
        self.assertLessEqual(len(rendered.encode("utf-8")), 4096)
        import json

        payload = json.loads(rendered)
        self.assertEqual(payload["budget"]["output_bytes"], len(rendered.encode("utf-8")))
        self.assertTrue(payload["budget"]["truncated"])


if __name__ == "__main__":
    unittest.main()
