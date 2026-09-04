"""Contract guards for the unified interactive review command."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / ".claude" / "commands" / "triage.md"


class TriageCommandContractTest(unittest.TestCase):
    def test_registry_exposes_direct_only_command(self) -> None:
        with (ROOT / "harness" / "commands.toml").open("rb") as handle:
            row = tomllib.load(handle)["commands"]["triage"]
        self.assertEqual(row["source"], ".claude/commands/triage.md")
        self.assertTrue(row["direct_only"])
        self.assertNotEqual(row.get("user_facing"), False)

    def test_overview_is_read_only_and_precedes_batches(self) -> None:
        text = SPEC.read_text(encoding="utf-8")
        overview = text.index("## Phase 1: read-only overview")
        batches = text.index("## Phase 2: selected-lane batch")
        self.assertLess(overview, batches)
        self.assertIn("Phase 1 is read-only", text)
        self.assertIn("BATCH_SIZE=5", text)
        self.assertIn("Re-read the selected lane", text)

    def test_every_lane_uses_existing_state_owner(self) -> None:
        text = SPEC.read_text(encoding="utf-8")
        for helper in (
            "scripts/autoevo_pending.py",
            "scripts/routine_digest.py",
            "scripts/recurring.py",
            "scripts/aggregate_freshness.py",
            "scripts/routine_audit.py",
            "scripts/intent_coverage.py",
        ):
            self.assertIn(helper, text)

    def test_mutations_keep_dry_run_and_recovery_gates(self) -> None:
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn("ack --manifest \"$SCRATCH/routine-batch.json\" --dry-run", text)
        self.assertIn("require explicit confirmation", text)
        self.assertIn("--confirm-effects-reviewed", text)
        self.assertIn("Stop after each batch", text)


    def test_every_probe_in_the_spec_is_a_real_command_line(self) -> None:
        """The spec is executed literally; a renamed flag would break a lane.

        Each `uv run scripts/<x>.py ...` line in a bash block is checked against
        the script's own --help: the subcommand must exist and every --flag must
        be one it accepts.
        """
        text = SPEC.read_text(encoding="utf-8")
        blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.S)
        lines = []
        for block in blocks:
            joined = block.replace("\\\n", " ")
            lines += [ln.strip() for ln in joined.splitlines() if ln.strip().startswith("uv run scripts/")]
        self.assertGreaterEqual(len(lines), 7, lines)
        for line in lines:
            tokens = shlex.split(line.split(">")[0].split("2>")[0])
            script = tokens[2]
            rest = tokens[3:]
            subcommand = [t for t in rest if not t.startswith("-") and not t.startswith("$") and not t.startswith('"')]
            flags = {t.split("=")[0] for t in rest if t.startswith("--")}
            argv = [sys.executable, script, *subcommand[:1], "--help"]
            proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, f"{line}\n{proc.stderr}")
            for flag in flags:
                self.assertIn(flag, proc.stdout, f"{script} {subcommand[:1]} lacks {flag}: {line}")


if __name__ == "__main__":
    unittest.main()



class RoutineHealthLaneSchemaTest(unittest.TestCase):
    """Every count `routine_audit.py health --json` emits, except the plain
    routine total, must be summed by the triage lane. Measured 2026-09-03:
    `counts.not_loaded` was added to health and the lane formula still summed
    the three older counts, so an unloaded launchd job scored `clear`."""

    def test_lane_formula_sums_every_health_count(self) -> None:
        import os
        import tempfile
        from unittest import mock

        sys.path.insert(0, str(ROOT / "scripts"))
        import routine_audit as ra

        row = next(
            line
            for line in SPEC.read_text(encoding="utf-8").splitlines()
            if line.startswith("| Routine health |")
        )
        summed = set(re.findall(r"`counts\.(\w+)`", row))
        with tempfile.TemporaryDirectory(prefix="atelier-triage-") as tmp:
            vault = Path(tmp)
            (vault / "_meta").mkdir()
            (vault / "_meta" / "routine_watch.toml").write_text("routine = []\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"OV": str(vault)}), mock.patch.object(
                ra, "_loaded_launchd_labels", return_value=(set(), None)
            ):
                payload, _ = ra._health()
        emitted = set(payload["counts"]) - {"routines"}
        self.assertEqual(summed, emitted)
