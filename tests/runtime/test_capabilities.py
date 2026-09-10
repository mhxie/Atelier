"""Bounded discovery, evidence semantics, and opt-in staging behavior."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest import mock

import atelier_runtime as launcher
import command_timeout as child_processes
import harness_lint
import routine_adapter as adapter
from runtime import capabilities as cap
from tests.test_routine_prefect import Fixture


ROOT = Path(__file__).resolve().parents[2]
CODEX_HELP = """Usage: codex [OPTIONS] [PROMPT]
Commands:
  exec    Execute a task
  resume  Resume a session
  fork    Fork a session
  mcp     Manage MCP
  plugin  Manage plugins
Options:
  --worktree [NAME]   Worktree
  --sandbox <MODE>    Sandbox
  --ask-for-approval <POLICY>  Approval
"""
EXEC_HELP = """Usage: codex exec [OPTIONS] [PROMPT]
Options:
  --json                  JSON events
  --ephemeral             No session persistence
  --output-schema <FILE>  Final schema
"""
CLAUDE_HELP = """Usage: claude [options] [command] [prompt]
Options:
  -c, --continue            Continue
  -r, --resume [sessionId]  Resume
  --fork-session           Fork
  -w, --worktree [name]     Worktree
  --agent <agent>          Agent
  --permission-mode <mode> Mode
  --output-format <format> Format
  --settings <file>        Settings
  -p, --print              Print
"""


def make_registry(root: Path) -> dict:
    registry = tomllib.loads((ROOT / "harness/runtimes.toml").read_text())
    for name, entry in registry["runtimes"].items():
        baseline = root / entry["capability_reference"]
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(f"## {name} fixture evidence\n")
        for field in cap.SURFACE_FIELDS:
            path = root / entry[field]
            path.parent.mkdir(parents=True, exist_ok=True)
            if field.endswith("_dir"):
                path.mkdir(exist_ok=True)
            else:
                path.write_text("fixture\n")
    return registry


def probe_fixture(executable: str, args: tuple[str, ...], **_kwargs) -> tuple[str, str]:
    return "observed", {
        ("codex", ("--version",)): "codex-cli 0.154.0\n",
        ("codex", ("--help",)): CODEX_HELP,
        ("codex", ("exec", "--help")): EXEC_HELP,
        ("claude", ("--version",)): "2.1.267 (Claude Code)\n",
        ("claude", ("--help",)): CLAUDE_HELP,
    }[(Path(executable).name, args)]


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="atelier-capabilities-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = make_registry(self.root)
        (self.root / ".gitignore").write_text("harness/runtime.local.toml\n")
        self.enterContext(mock.patch.object(cap.shutil, "which", side_effect=lambda name, **_: f"/fixture/{name}"))
        self.probe = self.enterContext(mock.patch.object(cap, "_probe", side_effect=probe_fixture))

    def snapshot(self, **kwargs) -> dict:
        return cap.snapshot(root=self.root, registry=self.registry, **kwargs)

    def test_only_fixed_probes_and_environment_allowlist_are_used(self) -> None:
        self.registry["runtimes"]["codex"].update(executable="evil", shell_args=["--execute-danger"])
        self.snapshot(environ={"PATH": "/fixture", "HOME": "/test-home", "OV": "private", "TOKEN": "secret"})
        self.assertEqual([(Path(c.args[0]).name, c.args[1]) for c in self.probe.call_args_list], [
            (name, args) for name, probes in cap.PROBES.items() for args in probes.values()
        ])
        for call in self.probe.call_args_list:
            self.assertEqual(call.kwargs["env"], {"PATH": "/fixture", "HOME": "/test-home", "LANG": "C"})
        self.probe.reset_mock()
        with mock.patch.object(cap.shutil, "which", side_effect=lambda name, **_: f"bin/{name}"):
            self.snapshot()
        self.assertEqual(self.probe.call_args_list[0].args[0], str(Path.cwd() / "bin/codex"))

    def test_versions_surfaces_and_declarations_do_not_imply_activation(self) -> None:
        value = self.snapshot()
        self.assertEqual(value["schema"], 1)
        self.assertEqual(value["scope"], "standalone-cli-discovery-only")
        for name, version in (("codex", "0.154.0"), ("claude", "2.1.267")):
            runtime = value["runtimes"][name]
            self.assertEqual(runtime["version"], version)
            self.assertEqual(runtime["baseline"]["status"], "present")
            self.assertEqual(len(runtime["baseline"]["sha256"]), 64)
            self.assertEqual(set(runtime["declaration_paths"].values()), {"present"})
            for field in ("effective_configuration", "enabled", "usable", "verified_in_use"):
                self.assertEqual(runtime[field], "unknown")
        surfaces = value["runtimes"]["codex"]["cli_surfaces"]
        self.assertEqual(surfaces["exec"], "advertised")
        self.assertEqual(surfaces["mcp-server"], "not-advertised")
        self.assertEqual(surfaces["--full-auto"], "not-advertised")
        self.assertEqual(value["runtimes"]["claude"]["cli_surfaces"]["--continue"], "advertised")

    def test_absent_binary_and_failed_probes_are_unknown_not_removed(self) -> None:
        with mock.patch.object(cap.shutil, "which", return_value=None):
            value = self.snapshot()
        self.probe.assert_not_called()
        self.assertEqual(value["runtimes"]["codex"]["version_status"], "not-found")
        self.assertIsNone(value["runtimes"]["codex"]["version"])
        for reason in ("timeout", "output-limit", "nonzero-exit", "start-failed", "unreadable-output"):
            with self.subTest(reason=reason):
                self.probe.side_effect = lambda *_a, **_kw: (reason, "private-canary")
                encoded = json.dumps(self.snapshot())
                self.assertNotIn("private-canary", encoded)
                surfaces = self.snapshot()["runtimes"]["codex"]["cli_surfaces"]
                self.assertEqual(set(surfaces.values()), {"unknown"})

    def test_unrecognized_success_output_cannot_be_mistaken_for_support(self) -> None:
        self.probe.side_effect = lambda *_a, **_kw: ("observed", "Usage: unrelated\nOptions:\n  --json foo\nprivate-canary")
        value = self.snapshot()
        self.assertEqual(value["runtimes"]["codex"]["version_status"], "unrecognized-version")
        self.assertEqual(set(value["runtimes"]["codex"]["cli_surfaces"].values()), {"unknown"})
        self.assertNotIn("private-canary", json.dumps(value))
        # A top-level help page with exit zero is not evidence for an exec option.
        self.probe.side_effect = lambda executable, args, **kw: (
            ("observed", CODEX_HELP) if args == ("exec", "--help") else probe_fixture(executable, args, **kw)
        )
        self.assertEqual(self.snapshot()["runtimes"]["codex"]["cli_surfaces"]["--json"], "unknown")

    def test_versions_reject_arbitrary_suffixes_or_extra_lines(self) -> None:
        for output in ("codex-cli 0.154.0-private-canary", "codex-cli 0.154.0\nprivate-canary", "v0.154.0"):
            with self.subTest(output=output):
                self.probe.side_effect = lambda *_a, **_kw: ("observed", output)
                value = self.snapshot()
                self.assertIsNone(value["runtimes"]["codex"]["version"])
                self.assertNotIn("private-canary", json.dumps(value))

    def test_flag_mentions_outside_options_do_not_advertise_support(self) -> None:
        fixture = "Usage: codex exec [OPTIONS]\nOptions:\n  --help  Help\nExamples:\n  --json is obsolete and unsupported\n"
        self.probe.side_effect = lambda executable, args, **kw: (
            ("observed", fixture) if args == ("exec", "--help") else probe_fixture(executable, args, **kw)
        )
        self.assertEqual(self.snapshot()["runtimes"]["codex"]["cli_surfaces"]["--json"], "not-advertised")

    def test_reference_changes_are_fingerprinted_and_unsafe_targets_are_unknown(self) -> None:
        reference = self.root / "sources/runtimes/codex.md"
        original = self.snapshot()["runtimes"]["codex"]["baseline"]
        reference.write_text("## Updated public evidence\n")
        self.assertNotEqual(original["sha256"], self.snapshot()["runtimes"]["codex"]["baseline"]["sha256"])
        outside = self.root / "private-canary"
        outside.write_text("private-canary")
        reference.unlink()
        reference.symlink_to(outside)
        for value in ("sources/runtimes/codex.md", "../private-canary", str(outside)):
            self.registry["runtimes"]["codex"]["capability_reference"] = value
            self.assertEqual(cap.reference_findings(self.root, self.registry), ["codex"])
            self.assertIsNone(self.snapshot()["runtimes"]["codex"]["baseline"]["sha256"])
        self.assertNotIn("private-canary", json.dumps(self.snapshot()))

    def test_empty_or_oversized_reference_is_not_valid_evidence(self) -> None:
        reference = self.root / "sources/runtimes/codex.md"
        for contents in ("", "x" * (cap.MAX_OUTPUT_BYTES + 1)):
            reference.write_text(contents)
            self.assertEqual(cap.reference_findings(self.root, self.registry), ["codex"])

    def test_baseline_validation_does_not_launch_a_cli(self) -> None:
        self.assertEqual(cap.reference_findings(self.root, self.registry), [])
        self.probe.assert_not_called()
        with mock.patch.object(harness_lint, "ROOT", self.root):
            (self.root / "sources/runtimes/codex.md").unlink()
            findings = harness_lint.check_runtime_registry(self.registry)
        self.assertEqual([f.code for f in findings if f.code == "runtime-capability-reference"], ["runtime-capability-reference"])

    def test_cli_uses_the_shared_snapshot_and_ordinary_status_does_not_probe(self) -> None:
        with mock.patch.object(launcher, "ROOT", self.root), mock.patch.object(launcher, "load_registry", return_value=self.registry):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(launcher.main(["status", "--json"]), 0)
            self.assertIn("available", json.loads(output.getvalue()))
            self.probe.assert_not_called()
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(launcher.main(["status", "--capabilities", "--json"]), 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value["runtimes"]["codex"]["version"], "0.154.0")
            self.assertEqual(self.probe.call_count, 5)
            with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as failure:
                launcher.main(["status", "--capabilities", "--observations"])
            self.assertEqual(failure.exception.code, 2)


class BoundedProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="atelier-probe-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def probe(self, script: str) -> tuple[str, str]:
        return cap._probe(sys.executable, ("-c", script), root=self.root, env={"PATH": os.defpath})

    def test_pipe_is_drained_without_persisting_stderr_or_output_files(self) -> None:
        status, output = self.probe("import sys; sys.stdout.write('x' * 60000); sys.stderr.write('private-canary')")
        self.assertEqual(status, "observed")
        self.assertEqual(len(output), 60000)
        self.assertNotIn("private-canary", output)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_byte_limit_is_enforced_during_reading(self) -> None:
        with mock.patch.object(cap, "MAX_OUTPUT_BYTES", 128):
            self.assertEqual(self.probe("import os; os.write(1, b'x' * 129)"), ("output-limit", ""))
            self.assertEqual(self.probe("import os; os.write(1, b'x' * 128)"), ("observed", "x" * 128))

    def test_failures_discard_all_output(self) -> None:
        cases = (
            ("print('private-canary'); raise SystemExit(7)", "nonzero-exit"),
            ("import os; os.write(1, b'\\xff')", "unreadable-output"),
        )
        for script, status in cases:
            self.assertEqual(self.probe(script), (status, ""))
        self.assertEqual(cap._probe("/nonexistent/fixture", (), root=self.root, env={}), ("start-failed", ""))
        with mock.patch.object(cap, "PROBE_SECONDS", 0.2):
            self.assertEqual(self.probe("import time; time.sleep(30)"), ("timeout", ""))

    def test_expired_deadline_does_not_add_a_termination_grace_period(self) -> None:
        started = time.monotonic()
        with mock.patch.object(cap, "PROBE_SECONDS", 0.2):
            self.assertEqual(self.probe(
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
            ), ("timeout", ""))
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(child_processes._LIVE_CHILDREN)

    def test_host_owned_exit_cleans_a_probe_in_a_daemon_thread(self) -> None:
        script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        program = (
            "import signal, sys, threading, time\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
            "from runtime import capabilities as cap\nimport command_timeout as children\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            f"threading.Thread(target=lambda: cap._probe(sys.executable, ('-c', {script!r}), "
            f"root=Path({str(self.root)!r}), env={{}}), daemon=True).start()\n"
            "while not children._LIVE_CHILDREN: time.sleep(0.01)\n"
            "print(next(iter(children._LIVE_CHILDREN)).pid, flush=True)\n"
            "time.sleep(30)\n"
        )
        runner = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE, text=True)
        pid = None
        try:
            pid = int(runner.stdout.readline())
            runner.terminate()
            runner.wait(timeout=5)
            time.sleep(0.5)
            self.assertEqual(runner.returncode, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()
            runner.stdout.close()
            if pid is not None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_timeout_kills_a_descendant_holding_the_pipe_after_parent_exit(self) -> None:
        marker = self.root / "child.pid"
        child = (
            "import os, signal, time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )
        script = f"import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', {child!r}])\n"
        pid = None
        try:
            with mock.patch.object(cap, "PROBE_SECONDS", 0.5):
                self.assertEqual(self.probe(script), ("timeout", ""))
            pid = int(marker.read_text())
            time.sleep(0.5)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class SnapshotAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.root, self.vault, self.env = self.fixture.root, self.fixture.vault, self.fixture.env
        profile = self.root / "harness/routine_profiles.toml"
        profile.write_text(profile.read_text().replace('"/fixture"', '"/run-routine"'))
        self.watch = self.vault / "_meta/routine_watch.toml"
        self.watch.write_text(self.watch.read_text().replace('command = "/fixture"', 'command = "/run-routine sample"'))
        (self.root / "harness/runtimes.toml").write_text((ROOT / "harness/runtimes.toml").read_text())
        (self.root / "harness/commands.toml").write_text(
            '[commands.run-routine]\nsource = ".claude/commands/fixture.md"\ncodex_prompt = "Fixture."\n'
        )
        prompts = self.vault / "_routine_prompts"
        prompts.mkdir()
        (prompts / "sample.md").write_text(
            "LOCAL EXECUTION OVERRIDE\nRead local filesystem under $OV.\n"
            "--- ORIGINAL ROUTINE PROMPT (fixture) ---\nFixture.\n"
        )
        self.data = {"schema": 1, "scope": "standalone-cli-discovery-only", "runtimes": {}}
        self.snapshot = self.enterContext(mock.patch.object(adapter.runtime_capabilities, "snapshot", return_value=self.data))

    def prepare(self) -> dict:
        return adapter.prepare_model("sample", root=self.root, environ=self.env)

    def opt_in(self) -> None:
        self.watch.write_text(self.watch.read_text() + "\nruntime_snapshot = true\n")

    def test_default_and_old_payload_do_not_probe_or_inherit_forged_paths(self) -> None:
        payload = self.prepare()
        self.assertIs(payload["runtime_snapshot"], False)
        del payload["runtime_snapshot"]
        self.env["ATELIER_RUNTIME_SNAPSHOT"] = "/forged/private-path"
        env = adapter.runtime_env(adapter.ModelSpec.from_payload(payload), root=self.root, vault=self.vault,
                                  cycle="2099-01-02", environ=self.env)
        self.assertNotIn("ATELIER_RUNTIME_SNAPSHOT", env)
        self.fixture.execute(payload, flow_run_id="default-fixture")
        self.snapshot.assert_not_called()

    def test_flag_is_strict_boolean_and_only_for_ordinary_routines(self) -> None:
        original = self.watch.read_text()
        for value in ('"true"', "1", "[]", "{}"):
            self.watch.write_text(original + f"\nruntime_snapshot = {value}\n")
            with self.assertRaisesRegex(adapter.ConfigurationError, "must be boolean"):
                self.prepare()
        self.watch.write_text(original + '\nruntime_snapshot = true\nwrapper = "autoevo"\n')
        with self.assertRaisesRegex(adapter.ConfigurationError, "ordinary /run-routine"):
            self.prepare()
        self.snapshot.assert_not_called()

    def test_snapshot_stages_after_idempotency_check_and_cleans_up(self) -> None:
        self.opt_in()
        payload = self.prepare()
        self.snapshot.assert_not_called()
        self.assertNotIn("runtimes", payload)
        self.env["ATELIER_RUNTIME_SNAPSHOT"] = "/forged/private-path"
        execute = adapter.execute_process
        paths = []

        def inspect(argv, **kwargs):
            path = Path(kwargs["env"]["ATELIER_RUNTIME_SNAPSHOT"])
            paths.append(path)
            self.assertEqual(json.loads(path.read_text()), {**self.data, "cycle_id": "2099-01-02"})
            self.assertFalse(path.is_relative_to(self.root))
            self.assertFalse(path.is_relative_to(self.vault))
            return execute(argv, **kwargs)

        with mock.patch.object(adapter, "execute_process", side_effect=inspect):
            receipt = self.fixture.execute(payload, flow_run_id="snapshot-fixture")
            repeated = self.fixture.execute(payload, flow_run_id="duplicate-fixture")
        self.assertEqual(receipt["verification"], "passed")
        self.assertEqual(repeated["prefect_flow_run_id"], "snapshot-fixture")
        self.snapshot.assert_called_once()
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())
        self.assertNotIn("runtimes", receipt)

    def test_probe_gaps_do_not_block_model_and_failed_model_cleans_staging(self) -> None:
        self.opt_in()
        self.data["runtimes"] = {"codex": {"version": None, "version_status": "timeout"}}
        paths = []

        def fail(_argv, **kwargs):
            path = Path(kwargs["env"]["ATELIER_RUNTIME_SNAPSHOT"])
            paths.append(path)
            self.assertEqual(json.loads(path.read_text()), {**self.data, "cycle_id": "2099-01-02"})
            return subprocess.CompletedProcess([], 7, "fixture failure")

        with mock.patch.object(adapter, "execute_process", side_effect=fail), self.assertRaises(adapter.ExecutionError):
            self.fixture.execute(self.prepare(), flow_run_id="failed-fixture")
        self.assertFalse(paths[0].exists())
        receipt = adapter.receipt_path(self.vault, "sample", "2099-01-02")
        self.assertEqual(tomllib.loads(receipt.read_text())["verification"], "pending")

    def test_staging_failure_occurs_before_model_and_receipt(self) -> None:
        self.opt_in()
        payload = self.prepare()
        write = Path.write_text

        def disk_full(path, *args, **kwargs):
            if path.name == "runtime-snapshot.json":
                raise OSError("fixture scratch is full")
            return write(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", disk_full), \
                mock.patch.object(adapter, "execute_process") as execute, self.assertRaises(OSError):
            self.fixture.execute(payload, flow_run_id="stage-failed")
        execute.assert_not_called()
        self.assertFalse(adapter.receipt_path(self.vault, "sample", "2099-01-02").exists())

    def test_runtime_snapshot_and_feed_inputs_have_independent_files(self) -> None:
        self.opt_in()
        payload = self.prepare()
        payload["rss_sources"] = "_meta/fixture-feeds.toml"
        execute = adapter.execute_process
        paths = []

        def stage(_spec, *, destination, **_kwargs):
            destination.write_text(json.dumps({"feed_fixture": True}))

        def inspect(argv, **kwargs):
            env = kwargs["env"]
            inputs, snapshot = Path(env["ATELIER_ROUTINE_INPUTS"]), Path(env["ATELIER_RUNTIME_SNAPSHOT"])
            self.assertNotEqual(inputs, snapshot)
            self.assertEqual(json.loads(inputs.read_text()), {"feed_fixture": True})
            self.assertEqual(json.loads(snapshot.read_text()), {**self.data, "cycle_id": "2099-01-02"})
            paths.extend((inputs, snapshot))
            return execute(argv, **kwargs)

        with mock.patch.object(adapter, "stage_rss_inputs", side_effect=stage), \
                mock.patch.object(adapter, "execute_process", side_effect=inspect):
            self.fixture.execute(payload, flow_run_id="combined-fixture")
        self.assertTrue(paths)
        self.assertFalse(any(path.exists() for path in paths))


if __name__ == "__main__":
    unittest.main()
