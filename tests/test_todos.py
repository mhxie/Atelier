"""Inline task metadata: `due:` and Reflect's `[[YYYY-MM-DD]]` due dates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(vault: Path, *args: str) -> str:
    # `todos` resolves registry paths at import, so it runs against a temp vault.
    proc = subprocess.run(
        [sys.executable, *args], cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)},
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout


def _due(text: str, reflect_task: bool = True) -> str | None:
    code = (
        "import json, sys; sys.path.insert(0, 'scripts'); import todos\n"
        f"t = todos.Todo(text={text!r}, source='gtd/sample.md', line=1, state='open')\n"
        f"todos.extract_metadata(t, {text!r}, reflect_task={reflect_task!r})\n"
        "print(json.dumps(t.due))\n"
    )
    with tempfile.TemporaryDirectory(prefix="atelier-todos-") as tmp:
        return json.loads(_run(Path(tmp), "-c", code).strip().splitlines()[-1])


class DueDateTest(unittest.TestCase):
    def test_reflect_task_date_link_is_the_due_date(self) -> None:
        self.assertEqual(_due("File taxes [[2099-04-15]] then [[2099-05-01]]"), "2099-04-15")

    def test_aliased_date_link_is_the_due_date(self) -> None:
        self.assertEqual(_due("Dental cleaning [[2099-10-17|10/17/2099]]"), "2099-10-17")

    def test_explicit_due_wins_over_a_date_link(self) -> None:
        self.assertEqual(_due("Send deck due:2099-03-01, drafted [[2099-02-20]]"), "2099-03-01")

    def test_date_link_outside_a_reflect_task_is_not_a_due_date(self) -> None:
        self.assertIsNone(_due("Revisit plan from [[2099-02-20]]", reflect_task=False))

    def test_only_round_checkbox_tasks_take_a_date_link_due(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-todos-") as tmp:
            vault = Path(tmp)
            (vault / "gtd").mkdir()
            (vault / "gtd" / "sample.md").write_text(
                "+ [ ] round task [[2099-01-05]]\n- [ ] square task [[2099-01-06]]\n", encoding="utf-8"
            )
            rows = json.loads(_run(vault, "scripts/todos.py", "list", "--json", "--include-stale"))
            self.assertEqual({r["text"].split()[0]: r["due"] for r in rows}, {"round": "2099-01-05", "square": None})


if __name__ == "__main__":
    unittest.main()
