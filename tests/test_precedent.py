"""Precedent judge: deterministic pre-filter, gated verdicts, queue defaults."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLS = "autoevo/time-stale-A"


def _line(subject: str, verdict: str, reason: str, tier: str = "wip", action: str = "close the lapsed plan", ts: str = "2099-01-01T00:00:00", by: str = "human") -> dict:
    return {"ts": ts, "class": CLS, "subject": subject, "verdict": verdict, "reason": reason,
            "features": {"tier": tier, "proposed_action": action, "evidence_summary": "by end of Q3"}, "source": "autoevo-review", "by": by}


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _run(vault: Path, *argv: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "scripts/precedent.py", *argv],
        cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)}, capture_output=True, text=True, timeout=120,
    )
    payload = json.loads(proc.stdout)
    payload["_exit"] = proc.returncode
    return payload


class PrecedentJudgeTest(unittest.TestCase):
    def _ledger(self, tmp: str) -> Path:
        ledger = Path(tmp) / "decisions.jsonl"
        _write_ledger(ledger, [
            _line("d1", "dismiss", "plan still active"),
            _line("d2", "dismiss", "goal restated in a newer note"),
            _line("d3", "dismiss", "date was a soft target"),
            _line("d4", "dismiss", "superseded elsewhere", action="drop the stale reference"),
            _line("a1", "apply", "genuinely abandoned", tier="research", action="retire the milestone"),
        ])
        return ledger

    def test_bundle_orders_by_similarity_and_excludes_non_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(_line("p0", "dismiss", "precedent line", by="precedent")) + "\n")
            out = _run(Path(tmp), "--ledger", str(ledger), "bundle", "--class", CLS, "--subject", "new",
                       "--features-json", json.dumps({"tier": "wip", "proposed_action": "close the lapsed plan"}), "--today", "2099-02-01")
            self.assertEqual([p["verdict"] for p in out["precedents"]][:3], ["dismiss", "dismiss", "dismiss"])
            # Four wip-tier human rows; the research-tier row is a different tier
            # and so is not a precedent, and the by="precedent" row is not human.
            self.assertEqual(len(out["precedents"]), 4)
            self.assertEqual([p["features"].get("tier") for p in out["precedents"]], ["wip"] * 4)
            self.assertGreater(out["precedents"][0]["similarity"], out["precedents"][-1]["similarity"])
            self.assertIn("verdict", out["prompt"])

    def test_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            bundle = Path(tmp) / "bundle.json"
            out = _run(Path(tmp), "--ledger", str(ledger), "bundle", "--class", CLS, "--subject", "new",
                       "--features-json", json.dumps({"tier": "wip", "proposed_action": "close the lapsed plan"}), "--today", "2099-02-01")
            bundle.write_text(json.dumps(out), encoding="utf-8")

            def judge(judgment: dict) -> dict:
                jp = Path(tmp) / "j.json"
                jp.write_text(json.dumps(judgment), encoding="utf-8")
                return _run(Path(tmp), "judge", "--bundle", str(bundle), "--judgment", str(jp))

            passing = judge({"verdict": "dismiss", "confidence": 0.9, "cited": [0, 1, 2], "reason": "three dismissals on live plans"})
            self.assertTrue(passing["default"], passing)
            self.assertEqual(passing["gate"], "pass")
            self.assertFalse(judge({"verdict": "dismiss", "confidence": 0.6, "cited": [0, 1, 2], "reason": "x"})["default"])
            self.assertFalse(judge({"verdict": "dismiss", "confidence": 0.95, "cited": [0, 1], "reason": "x"})["default"])
            self.assertIn("disagree", judge({"verdict": "apply", "confidence": 0.95, "cited": [0, 1, 3], "reason": "x"})["gate"])
            self.assertIn("human", judge({"verdict": "human", "confidence": 0.99, "cited": [0, 1, 2], "reason": "x"})["gate"])

    def test_silent_budget_stops_the_judge_and_a_human_decision_resets_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            # Three defaults set after the newest human line: all unconfirmed.
            for i in range(3):
                rows.append(_line(f"u{i}", "dismiss", "precedent", ts=f"2099-03-0{i + 1}T00:00:00", by="precedent"))
            _write_ledger(ledger, rows)

            def gate_with(budget: int) -> dict:
                bundle = _run(Path(tmp), "--ledger", str(ledger), "bundle", "--class", CLS, "--subject", "new",
                              "--features-json", json.dumps({"tier": "wip", "proposed_action": "close the lapsed plan"}),
                              "--today", "2099-04-01")
                self.assertEqual(bundle["stats"]["precedent_unconfirmed_streak"], 3)
                bp = Path(tmp) / "b.json"
                bp.write_text(json.dumps(bundle), encoding="utf-8")
                jp = Path(tmp) / "j.json"
                jp.write_text(json.dumps({"verdict": "dismiss", "confidence": 0.95, "cited": [0, 1, 2], "reason": "x"}),
                              encoding="utf-8")
                return _run(Path(tmp), "judge", "--bundle", str(bp), "--judgment", str(jp),
                            "--max-unconfirmed", str(budget))

            self.assertTrue(gate_with(10)["default"])
            spent = gate_with(3)
            self.assertFalse(spent["default"])
            self.assertIn("unconfirmed", spent["gate"])
            self.assertIn("/autoevo-review", spent["gate"])
            # A budget of 0 is the sorter: it never decides alone.
            self.assertFalse(gate_with(0)["default"])

            # Any human decision, in any class, is the heartbeat that refills it.
            rows.append(_line("elsewhere", "apply", "unrelated triage", ts="2099-03-09T00:00:00"))
            rows[-1]["class"] = "autoevo/redundant"
            _write_ledger(ledger, rows)
            bundle = _run(Path(tmp), "--ledger", str(ledger), "bundle", "--class", CLS, "--subject", "new",
                          "--features-json", json.dumps({"tier": "wip", "proposed_action": "close the lapsed plan"}),
                          "--today", "2099-04-01")
            self.assertEqual(bundle["stats"]["precedent_unconfirmed_streak"], 0)

    def test_class_accuracy_floor_blocks_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            # Five precedent defaults, every one of them actually looked at by a
            # human afterwards, three of those disagreeing: accuracy 0.4 over 5
            # judged. Aging out no longer counts as judged, so the floor can only
            # engage on outcomes someone observed.
            for i in range(5):
                rows.append(_line(f"p{i}", "dismiss", "precedent", ts="2099-01-01T00:00:00", by="precedent"))
                verdict = "apply" if i < 3 else "dismiss"
                reason = "no, keep it" if i < 3 else "agreed, drop it"
                rows.append(_line(f"p{i}", verdict, reason, ts="2099-01-02T00:00:00"))
            _write_ledger(ledger, rows)
            out = _run(Path(tmp), "--ledger", str(ledger), "bundle", "--class", CLS, "--subject", "new",
                       "--features-json", json.dumps({"tier": "wip", "proposed_action": "close the lapsed plan"}), "--today", "2099-03-01")
            bundle = Path(tmp) / "bundle.json"
            bundle.write_text(json.dumps(out), encoding="utf-8")
            jp = Path(tmp) / "j.json"
            jp.write_text(json.dumps({"verdict": "dismiss", "confidence": 0.95, "cited": [0, 1, 2], "reason": "x"}), encoding="utf-8")
            verdict = _run(Path(tmp), "judge", "--bundle", str(bundle), "--judgment", str(jp))
            self.assertFalse(verdict["default"])
            self.assertIn("accuracy", verdict["gate"])

    def test_no_judge_means_no_judgment_and_bundles_stay_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            vault = Path(tmp)
            queue = vault / "q.toml"
            entry = {"id": "n1", "category": "time-stale-A", "proposed_action": "close the lapsed plan", "evidence_summary": "by end of Q3",
                     "peers": ["wip/p.md"], "proposed_at": "2099-02-01", "status": "pending"}
            subprocess.run([sys.executable, "scripts/autoevo_pending.py", "--queue", str(queue), "append", "--entries", "-", "--today", "2099-02-01"],
                           cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)}, input=json.dumps([entry]), capture_output=True, text=True, timeout=60, check=True)
            env = {k: v for k, v in os.environ.items() if k != "ATELIER_PRECEDENT_MODEL"}
            proc = subprocess.run([sys.executable, "scripts/precedent.py", "--ledger", str(ledger), "autoevo", "--queue", str(queue), "--today", "2099-02-02"],
                                  cwd=REPO_ROOT, env={**env, "OV": str(vault)}, capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("no judge chosen", json.loads(proc.stdout)["error"])
            bundle_dir = vault / "bundles"
            out = _run(vault, "--ledger", str(ledger), "autoevo", "--queue", str(queue), "--today", "2099-02-02", "--bundle-dir", str(bundle_dir))
            self.assertEqual(out["bundled"], ["n1"])
            self.assertTrue((bundle_dir / "n1.prompt.txt").is_file())
            self.assertIn("Past decisions", (bundle_dir / "n1.prompt.txt").read_text(encoding="utf-8"))
            self.assertNotIn("default_action", tomllib.loads(queue.read_text())["pending"][0])

    def test_autoevo_sets_queue_defaults_from_judgments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            vault = Path(tmp)
            queue = vault / "q.toml"
            entries = [
                {"id": "n1", "category": "time-stale-A", "proposed_action": "close the lapsed plan", "evidence_summary": "by end of Q3",
                 "peers": ["wip/p.md"], "proposed_at": "2099-02-01", "status": "pending"},
                {"id": "n2", "category": "time-stale-A", "proposed_action": "verify", "evidence_summary": "before April",
                 "peers": ["reflections/r.md"], "proposed_at": "2099-02-01", "status": "pending"},
                {"id": "n3", "category": "redundant", "proposed_action": "merge", "evidence_summary": "3 peers",
                 "peers": ["wip/a.md", "wip/b.md"], "proposed_at": "2099-02-01", "status": "pending"},
            ]
            subprocess.run([sys.executable, "scripts/autoevo_pending.py", "--queue", str(queue), "append", "--entries", "-", "--today", "2099-02-01"],
                           cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)}, input=json.dumps(entries), capture_output=True, text=True, timeout=60, check=True)
            jdir = vault / "judgments"
            jdir.mkdir()
            (jdir / "n1.json").write_text(json.dumps({"verdict": "dismiss", "confidence": 0.9, "cited": [0, 1, 2], "reason": "same pattern"}), encoding="utf-8")
            (jdir / "n2.json").write_text(json.dumps({"verdict": "apply", "confidence": 0.9, "cited": [3], "reason": "x"}), encoding="utf-8")
            dry = _run(vault, "--ledger", str(ledger), "autoevo", "--queue", str(queue), "--today", "2099-02-02", "--judgment-dir", str(jdir), "--dry-run")
            self.assertEqual(dry["defaults_set"], [])
            self.assertEqual({r["id"]: r.get("would_set") for r in dry["judged"] if r["id"] == "n1"}, {"n1": "dismiss"})
            out = _run(vault, "--ledger", str(ledger), "autoevo", "--queue", str(queue), "--today", "2099-02-02", "--judgment-dir", str(jdir))
            self.assertEqual(out["defaults_set"], ["n1"])
            gates = {r["id"]: r["gate"] for r in out["judged"]}
            self.assertIn("0 precedents", gates["n2"])
            self.assertIn("precedents", gates["n3"])
            rows = {e["id"]: e for e in tomllib.loads(queue.read_text())["pending"]}
            self.assertEqual((rows["n1"]["default_action"], rows["n1"]["default_at"]), ("dismiss", "2099-02-16"))
            self.assertNotIn("default_action", rows["n2"])
            last = json.loads(ledger.read_text().splitlines()[-1])
            self.assertEqual((last["by"], last["subject"], last["verdict"]), ("precedent", "n1", "dismiss"))


if __name__ == "__main__":
    unittest.main()
