"""agy voice leg: dispatcher exit codes and isolation, plus registry contract."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import _models  # noqa: E402
import agy_leg  # noqa: E402

FAKE = """#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
event = sys.stdin.read()
open(os.path.join(here, "log.json"), "w").write(json.dumps({"argv": sys.argv[1:], "stdin": event,
    "cwd": os.getcwd(), "env": sorted(os.environ)}))
mode = open(os.path.join(here, "mode")).read().strip() if os.path.exists(os.path.join(here, "mode")) else "ok"
print(json.dumps({"event": "init"}))
if mode == "ok":
    result = {"status": "SUCCESS", "response": "hello\\n", "usage": {"total_tokens": 7}}
elif mode == "partial":
    sys.stderr.write("[agy] print timeout after 9s with turn in progress; returning partial output\\n")
    result = {"status": "SUCCESS", "response": "1\\n2\\n"}
elif mode == "slow":
    result = {"status": "ERROR", "response": "", "error": "print timeout after 1s"}
elif mode == "auth":
    result = {"status": "ERROR", "response": "", "error": "please sign in first"}
else:
    result = {"status": "ERROR", "response": "", "error": "boom"}
if mode != "noresult":
    print(json.dumps({"event": "result", "result": result}))
"""


class AgyLegTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        agy = self.bin / "agy"
        agy.write_text(FAKE, encoding="utf-8")
        agy.chmod(agy.stat().st_mode | stat.S_IXUSR)
        self.log = self.bin / "log.json"
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": self.tmp.name,
               "UNRELATED_SECRET": "x"}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        binding = mock.patch.object(_models, "agy_binding", return_value="fixture-agy-model")
        binding.start()
        self.addCleanup(binding.stop)

    def run_leg(self, *argv: str, stdin: str = "question") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = agy_leg.main(["--model", "m", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_success_sends_prompt_on_stdin_text_only(self) -> None:
        code, out, _ = self.run_leg("--prompt", "-", "--system", "be brief")
        self.assertEqual((code, out), (0, "hello\n"))
        seen = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(seen["stdin"]),
                         {"event": "user", "message": {"content": "be brief\n\nquestion"}})
        argv = seen["argv"]
        self.assertIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "fixture-agy-model")
        for forbidden in ("-p", "--print", "--prompt", "--dangerously-skip-permissions", "--add-dir"):
            self.assertNotIn(forbidden, argv)
        self.assertNotIn("question", " ".join(argv))

    def test_runs_in_scratch_dir_with_minimal_environment(self) -> None:
        self.run_leg("--prompt", "-")
        seen = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertNotEqual(Path(seen["cwd"]).resolve(), ROOT)
        self.assertFalse(Path(seen["cwd"]).exists(), "scratch dir is removed after the run")
        self.assertNotIn("UNRELATED_SECRET", seen["env"])

    def test_exit_codes(self) -> None:
        for mode, expected in (("slow", 3), ("partial", 3), ("auth", 2), ("boom", 1), ("noresult", 1)):
            with self.subTest(mode=mode):
                (self.bin / "mode").write_text(mode, encoding="utf-8")
                self.assertEqual(self.run_leg("--prompt", "-")[0], expected)

    def test_prerequisites_missing_is_a_soft_skip(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": self.tmp.name}):
            self.assertEqual(self.run_leg("--prompt", "-")[0], 2)
        with mock.patch.object(_models, "agy_binding", return_value=""):
            self.assertEqual(self.run_leg("--prompt", "-")[0], 2)
        with mock.patch.object(_models, "agy_binding", side_effect=_models.ModelError("unknown")):
            self.assertEqual(self.run_leg("--prompt", "-")[0], 2)

    def test_empty_prompt_is_invalid(self) -> None:
        self.assertEqual(self.run_leg("--prompt", "-", stdin=" \n")[0], 4)

    def test_truncated_reply_is_never_reported_as_success(self) -> None:
        (self.bin / "mode").write_text("partial", encoding="utf-8")
        code, out, err = self.run_leg("--prompt", "-")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("print timeout", err)

    def test_invalid_timeout_and_unreadable_prompt_are_bad_arguments(self) -> None:
        for argv in (("--prompt", "-", "--timeout", "0"), ("--prompt", "-", "--timeout", "-5"),
                     ("--prompt-file", str(Path(self.tmp.name) / "absent.txt"))):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_leg(*argv)[0], 4)

    def test_fractional_timeout_rounds_up_for_print_timeout(self) -> None:
        self.run_leg("--prompt", "-", "--timeout", "0.5")
        argv = json.loads(self.log.read_text(encoding="utf-8"))["argv"]
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "1s")

    def test_max_tokens_is_accepted_for_call_site_parity(self) -> None:
        self.assertEqual(self.run_leg("--prompt", "-", "--max-tokens", "0")[0], 0)


class AgyRegistryContractTest(unittest.TestCase):
    def load(self, relative: str) -> dict:
        with (ROOT / relative).open("rb") as handle:
            return tomllib.load(handle)

    def test_every_direct_role_has_an_agy_leg_except_single_leg_and_private_input_roles(self) -> None:
        agents = self.load("harness/agents.toml")["agents"]
        models = self.load("harness/models.toml")["models"]
        withheld = {"scribe", "precedent-judge", "privacy-reviewer"}
        for name, row in agents.items():
            voices = row["voices"]
            with self.subTest(role=name):
                if "direct" in voices and name not in withheld:
                    self.assertIn(voices["agy"], models)
                else:
                    self.assertNotIn("agy", voices)
        self.assertEqual(withheld & {n for n, r in agents.items() if "agy" in r["voices"]}, set())

    def test_agy_identities_carry_no_committed_model_id(self) -> None:
        models = self.load("harness/models.toml")["models"]
        for name in ("gemini_flash", "gemini_deep"):
            self.assertEqual(set(models[name]), {"reasoning_tier", "binding_optional"})
        schema = (ROOT / "harness" / "models.toml").read_text(encoding="utf-8")
        self.assertNotRegex(schema, r"gemini-\d")

    def test_agy_binding_resolves_from_the_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema, bindings = Path(tmp, "schema.toml"), Path(tmp, "bindings.toml")
            schema.write_text('[models.demo]\nreasoning_tier = "deep"\n[models.bare]\nreasoning_tier = "deep"\n')
            bindings.write_text('[models.demo]\nagy = "fixture-id"\n')
            with mock.patch.object(_models, "SCHEMA_TOML", schema), mock.patch.object(_models, "BINDINGS_TOML", bindings):
                self.assertEqual(_models.agy_binding("demo"), "fixture-id")
                self.assertEqual(_models.agy_binding("bare"), "")
                with self.assertRaises(_models.ModelError):
                    _models.agy_binding("absent")


if __name__ == "__main__":
    unittest.main()
