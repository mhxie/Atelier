"""Focused contract tests for the Prefect routine replacement."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import routine_adapter as adapter  # noqa: E402
import command_timeout as child_processes  # noqa: E402
import routine_prefect as orchestration  # noqa: E402
import routine_status as status  # noqa: E402
import semantic  # noqa: E402
from prefect.testing.utilities import prefect_test_harness  # noqa: E402
from prefect.settings import (  # noqa: E402
    PREFECT_API_URL,
    PREFECT_SERVER_ANALYTICS_ENABLED,
    PREFECT_SERVER_EPHEMERAL_ENABLED,
    temporary_settings,
)
from prefect.states import Completed, Scheduled  # noqa: E402


CYCLE = "2099-01-02"


class Fixture:
    def __init__(self, mode: str = "success") -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="atelier-prefect-test-")
        self.root = Path(self.temporary.name) / "atelier"
        self.vault = Path(self.temporary.name) / "vault"
        self.bin = Path(self.temporary.name) / "bin"
        for path in (
            self.root / "harness/routine-shell",
            self.root / "routines/_adapters/archived-prompt",
            self.root / "routines/_adapters/autoevo",
            self.root / "scripts",
            self.vault / "_meta",
            self.vault / "_tools/routines",
            self.bin,
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.root / "harness/routine_profiles.toml").write_text(
            """
version = 1
[profiles.fixture]
surface = "local"
sandbox = "workspace-write"
atelier_access = "read"
web_search = "disabled"
shell_network = "disabled"
user_config = "ignore"
timeout_seconds = 30
reasoning_effort = "low"
permissions = ["atelier:read", "vault:read-write"]
required_clis = []
required_plugins = []
optional_plugins = []
allowed_adapters = ["archived-prompt"]
""".lstrip(),
            encoding="utf-8",
        )
        (self.root / "routines/registry.toml").write_text(
            """
version = 1

[adapters.archived-prompt]
procedure = "routines/_adapters/archived-prompt/PROCEDURE.md"

[adapters.autoevo]
procedure = "routines/_adapters/autoevo/PROCEDURE.md"

[[routine]]
name = "dummy"
runner = "process"
cron = "0 4 * * *"
timezone = "UTC"
argv = ["{python}", "scripts/dummy.py"]
timeout_seconds = 30
retry_safe = true
retries = 1
retry_delay_seconds = 1
            """.lstrip(),
            encoding="utf-8",
        )
        (self.root / "harness/routine_result.schema.json").write_text("{}\n", encoding="utf-8")
        (self.root / "routines/_adapters/archived-prompt/PROCEDURE.md").write_text(
            "Fixture archived-prompt adapter.\n", encoding="utf-8"
        )
        (self.root / "routines/_adapters/autoevo/PROCEDURE.md").write_text(
            "Fixture Autoevo adapter.\n", encoding="utf-8"
        )
        (self.root / "scripts/dummy.py").write_text("pass\n", encoding="utf-8")
        (self.vault / "_tools/routines/registry.toml").write_text(
            """
version = 1

[[routine]]
name = "sample"
execution = "local"
runner = "model"
adapter = "archived-prompt"
cron = "0 5 * * *"
timezone = "UTC"
profile = "fixture"
output_dir = "outputs"
file_pattern = "*.md"
            """.lstrip(),
            encoding="utf-8",
        )
        prompts = self.vault / "_routine_prompts"
        prompts.mkdir()
        (prompts / "sample.md").write_text(
            "LOCAL EXECUTION OVERRIDE\nRead local filesystem under $OV.\n"
            "--- ORIGINAL ROUTINE PROMPT (fixture) ---\nFixture.\n",
            encoding="utf-8",
        )
        codex = self.bin / "codex"
        if mode == "failure":
            body = "import sys\nprint('failed safely')\nsys.exit(7)\n"
        else:
            output_expr = "None" if mode == "no-output" else "str(output)"
            # A noop still writes its audit artifact; only the envelope differs.
            envelope_outcome = "noop" if mode.startswith("noop") else "delivered"
            skipped = "['Gmail inbox scan']" if mode == "noop-skipped" else "[]"
            body = f"""
import json, os
from pathlib import Path
import sys
args = sys.argv
result = Path(args[args.index('--output-last-message') + 1])
output = Path(os.environ['OV']) / 'outputs' / (os.environ['ATELIER_ROUTINE_CYCLE'] + '.md')
if {mode!r} != 'no-output':
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('verified artifact\\n', encoding='utf-8')
result.write_text(json.dumps({{'routine':'sample','outcome':{envelope_outcome!r},'output_file':{output_expr},'summary':'ok','skipped_inputs':{skipped}}}), encoding='utf-8')
print(json.dumps({{'type': 'thread.started', 'thread_id': 'fixture-session'}}))
print(json.dumps({{'type': 'turn.started'}}))
print(json.dumps({{'type': 'turn.completed', 'usage': {{'input_tokens': 20, 'cached_input_tokens': 5, 'output_tokens': 8}}}}))
""".lstrip()
        codex.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
        self.env = {
            "HOME": str(Path(self.temporary.name) / "home"),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "OV": str(self.vault),
            "ATELIER_SKIP_CAFFEINATE": "1",
            "TMPDIR": self.temporary.name,
            "SECRET": "must-not-reach-codex",
        }

    def execute(
        self,
        payload: dict[str, object],
        *,
        flow_run_id: str,
        cycle: str = CYCLE,
    ) -> dict[str, object]:
        return adapter.execute_model(
            payload,
            cycle=cycle,
            flow_run_id=flow_run_id,
            root=self.root,
            environ=self.env,
        )

    def close(self) -> None:
        self.temporary.cleanup()


class RssAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.root, self.vault, self.env = self.fixture.root, self.fixture.vault, self.fixture.env
        profile = self.root / "harness/routine_profiles.toml"
        profile.write_text(profile.read_text().replace('web_search = "disabled"', 'web_search = "live"')
                           .replace('"vault:read-write"', '"vault:read-write", "web:live"')
                           .replace('["archived-prompt"]', '["archived-prompt", "autoevo"]'))
        watch = self.vault / "_tools/routines/registry.toml"
        watch.write_text(watch.read_text() + '\nrss_sources = "_meta/feeds.toml"\n')
        self.config = self.vault / "_meta/feeds.toml"
        self.config.write_text('version = 1\n[[feed]]\nid = "example"\nurl = "https://example.invalid/feed"\n')
        prompts = self.vault / "_routine_prompts"
        (prompts / "sample.md").write_text(
            "LOCAL EXECUTION OVERRIDE\nRead local filesystem under $OV.\n"
            "--- ORIGINAL ROUTINE PROMPT (fixture) ---\nFixture.\n"
        )
        self.data = {
            "schema": 1, "collected_at": "2099-01-02T00:00:00Z", "status": "ok",
            "counts": {"declared": 1, "attempted": 1, "reached": 1, "usable": 1, "items": 1},
            "feeds": [{"id": "example", "status": "ok", "bytes": 100, "items": [
                {"title": "Example", "url": "https://example.invalid/article", "published_at": None, "summary": "Excerpt"},
            ]}], "gaps": [], "summary_provenance": "feed content, not article full text",
        }
        self.node = self.enterContext(mock.patch.object(adapter._node, "run", side_effect=self._node))

    def _node(self, argv, **kwargs):
        self.assertEqual(argv[0], self.root / "scripts/routine_feeds.mjs")
        self.assertEqual(kwargs["env"], {"PATH": adapter._node.SYSTEM_PATH})
        self.assertEqual(json.loads(kwargs["input"])["feeds"][0]["id"], "example")
        validate = argv[-1] == "--validate"
        self.assertEqual(kwargs["timeout"], 5 if validate else 25)
        result = {"schema": 1, "valid": True, "feeds": 1} if validate else self.data
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

    def _prepare(self):
        return adapter.prepare_model("sample", root=self.root, environ=self.env)

    def test_preflight_is_static_and_payload_has_only_the_private_path(self) -> None:
        payload = self._prepare()
        self.assertEqual(payload["rss_sources"], "_meta/feeds.toml")
        self.assertNotIn("example.invalid", json.dumps(payload))
        self.assertEqual(self.node.call_count, 1)
        self.assertEqual(self.node.call_args.args[0][-1], "--validate")

    def test_offline_or_non_routine_profiles_cannot_declare_feeds(self) -> None:
        path = self.root / "harness/routine_profiles.toml"
        original = path.read_text()
        for content in (original.replace('web_search = "live"', 'web_search = "disabled"'),
                        original.replace(', "web:live"', '')):
            path.write_text(content)
            with self.assertRaisesRegex(adapter.ConfigurationError, "web:live"):
                self._prepare()
        watch = self.vault / "_tools/routines/registry.toml"
        watch.write_text(watch.read_text().replace('adapter = "archived-prompt"', 'adapter = "autoevo"'))
        with self.assertRaisesRegex(adapter.ConfigurationError, "archived-prompt adapter"):
            self._prepare()
        self.node.assert_not_called()

    def test_config_rejects_escapes_oversize_and_invalid_shape_without_fetching(self) -> None:
        spec = adapter.ModelSpec.from_payload(self._prepare())
        self.node.reset_mock()
        for content in ('version = true\nfeed = []', 'version = 2\nfeed = []',
                        'version = 1\nfeed = []\nunknown = 1', '#' * 131073):
            self.config.write_text(content)
            with self.subTest(content=content[:30]), self.assertRaises(adapter.ConfigurationError):
                adapter._rss_config(spec, vault=self.vault)
        self.config.unlink()
        self.config.symlink_to(self.root / "harness/routine_profiles.toml")
        with self.assertRaises(adapter.ConfigurationError):
            adapter._rss_config(spec, vault=self.vault)
        self.node.assert_not_called()

    def test_static_failure_is_a_safe_configuration_error(self) -> None:
        self.node.side_effect = None
        self.node.return_value = subprocess.CompletedProcess([], 2, '{"status":"invalid"}', "sensitive URL")
        with self.assertRaisesRegex(adapter.ConfigurationError, "RSS input preflight failed") as raised:
            self._prepare()
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertEqual(self.node.call_count, 1)

    def test_staged_input_precedes_model_and_prior_delivery_skips_collection(self) -> None:
        payload = self._prepare()
        payload["profile_values"]["atelier_access"] = "read-write"
        payload["profile_values"]["permissions"].append("atelier:read-write")
        self.env["ATELIER_ROUTINE_INPUTS"] = "/forged/input.json"
        original = adapter.execute_process
        inspected = []

        def execute(argv, **kwargs):
            inputs = Path(kwargs["env"]["ATELIER_ROUTINE_INPUTS"])
            inspected.append(inputs)
            self.assertEqual(json.loads(inputs.read_text()), self.data)
            self.assertFalse(inputs.is_relative_to(self.root))
            self.assertNotIn("SECRET", kwargs["env"])
            self.assertIn("sandbox_workspace_write.network_access=false", argv)
            return original(argv, **kwargs)

        with mock.patch.object(adapter, "execute_process", side_effect=execute) as child, redirect_stdout(io.StringIO()):
            receipt = self.fixture.execute(payload, flow_run_id="rss-fixture")
            self.assertEqual(receipt["verification"], "passed")
            again = self.fixture.execute(payload, flow_run_id="rss-duplicate")
        self.assertEqual(again["prefect_flow_run_id"], "rss-fixture")
        self.assertEqual(child.call_count, 1)
        self.assertEqual(self.node.call_count, 2)
        self.assertFalse(inspected[0].exists())
        self.assertFalse((self.root / "inputs.json").exists())

    def test_collector_failure_keeps_coverage_unknown_and_model_can_write_audit(self) -> None:
        payload = self._prepare()
        self.node.reset_mock()
        original = adapter.execute_process

        def execute(argv, **kwargs):
            data = json.loads(Path(kwargs["env"]["ATELIER_ROUTINE_INPUTS"]).read_text())
            self.assertEqual(data["status"], "degraded")
            self.assertEqual(data["counts"], {"declared": 1, "attempted": None, "reached": None, "usable": None, "items": 0})
            self.assertEqual(data["gaps"][0]["code"], "collector_failed")
            self.assertNotIn("sensitive", json.dumps(data))
            return original(argv, **kwargs)

        self.node.side_effect = adapter._node.NodeError("sensitive URL in timeout")
        output = io.StringIO()
        with mock.patch.object(adapter, "execute_process", side_effect=execute), redirect_stdout(output):
            self.assertEqual(self.fixture.execute(payload, flow_run_id="rss-timeout")["verification"], "passed")
        self.assertEqual(self.node.call_count, 1)
        self.assertNotIn("sensitive", output.getvalue())

    def test_malformed_collector_output_or_changed_config_becomes_a_gap(self) -> None:
        spec = adapter.ModelSpec.from_payload(self._prepare())
        destination = Path(self.fixture.temporary.name) / "inputs.json"
        self.node.side_effect = None
        for result in ("[]", "{", json.dumps({**self.data, "counts": {}})):
            self.node.return_value = subprocess.CompletedProcess([], 0, result, "")
            adapter.stage_rss_inputs(spec, root=self.root, vault=self.vault, destination=destination)
            self.assertEqual(json.loads(destination.read_text())["gaps"][0]["code"], "collector_failed")
        self.config.unlink()
        self.node.reset_mock()
        adapter.stage_rss_inputs(spec, root=self.root, vault=self.vault, destination=destination)
        self.assertIsNone(json.loads(destination.read_text())["counts"]["declared"])
        self.node.assert_not_called()


class AdapterTests(unittest.TestCase):
    def test_process_stdin_is_closed_unless_explicitly_supplied(self) -> None:
        for supplied in (None, "explicit input\n中文"):
            with self.subTest(input_text=supplied):
                program = (
                    f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                    "from routine_adapter import execute_process\n"
                    "result = execute_process([sys.executable, '-c', "
                    "'import sys; print(repr(sys.stdin.read()))'], "
                    f"seconds=0.5, input_text={supplied!r})\n"
                    "print(result.stdout, end=''); sys.exit(result.returncode)\n"
                )
                read_fd, write_fd = os.pipe()
                with os.fdopen(read_fd, "rb") as incoming, os.fdopen(write_fd, "wb"):
                    result = subprocess.run(
                        [sys.executable, "-B", "-c", program], stdin=incoming,
                        capture_output=True, text=True, timeout=10,
                    )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout, repr(supplied or "") + "\n")

    def test_digest_quota_permission_is_optional_and_does_not_enable_weather(self):
        profiles = adapter._load_profiles(ROOT)
        profile = profiles["local-digest-mail"]
        self.assertIn("quota:read", profile["permissions"])
        self.assertNotIn("quota:read", profiles["local-digest"]["permissions"])
        self.assertNotIn("codexbar", profile["required_clis"])
        self.assertEqual((profile["web_search"], profile["shell_network"]), ("disabled", "enabled"))

    def test_success_writes_verified_receipt_not_execution_state(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        result = fixture.execute(payload, flow_run_id="flow-1")
        receipt = adapter.receipt_path(fixture.vault, "sample", CYCLE)
        stored = tomllib.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual((result["verification"], stored["output_file"]), ("passed", f"outputs/{CYCLE}.md"))
        self.assertEqual(stored["prefect_flow_run_id"], "flow-1")
        self.assertEqual(stored["contract_version"], 4)
        self.assertEqual(stored["verification_scope"], "artifact-bytes")
        output = fixture.vault / stored["output_file"]
        self.assertEqual(stored["artifact_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertNotIn("status", stored)

    def test_noop_that_skipped_inputs_leaves_the_cycle_retryable(self) -> None:
        """A transient input outage must not consume the cycle.

        prior_delivery() short-circuits on verification == "passed" without looking
        at outcome, so a noop recorded as passed would block every later attempt,
        including the next scheduled one. `blocked` is that function's existing
        signal for "not delivered", and it is the only thing that lets the routine
        recover on its own.
        """
        fixture = Fixture("noop-skipped")
        self.addCleanup(fixture.close)
        payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        receipt = fixture.execute(payload, flow_run_id="flow-noop")
        self.assertEqual((receipt["outcome"], receipt["verification"]), ("noop", "blocked"))
        spec = adapter.ModelSpec.from_payload(payload)
        self.assertIsNone(adapter.prior_delivery(spec, cycle=CYCLE, vault=fixture.vault))

    def test_intentional_noop_without_skipped_inputs_still_completes_the_cycle(self) -> None:
        """A noop that skipped nothing is a real result and must not re-run forever."""
        fixture = Fixture("noop-clean")
        self.addCleanup(fixture.close)
        payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        receipt = fixture.execute(payload, flow_run_id="flow-noop-clean")
        self.assertEqual((receipt["outcome"], receipt["verification"]), ("noop", "passed"))
        spec = adapter.ModelSpec.from_payload(payload)
        self.assertIsNotNone(adapter.prior_delivery(spec, cycle=CYCLE, vault=fixture.vault))

    def test_failure_and_no_output_leave_ambiguous_receipt(self) -> None:
        for mode in ("failure", "no-output"):
            with self.subTest(mode=mode):
                fixture = Fixture(mode)
                try:
                    payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
                    with self.assertRaises(adapter.ExecutionError):
                        fixture.execute(payload, flow_run_id="flow-2")
                    receipt = tomllib.loads(adapter.receipt_path(fixture.vault, "sample", CYCLE).read_text())
                    self.assertEqual((receipt["cycle_id"], receipt["verification"]), (CYCLE, "pending"))
                    with self.assertRaisesRegex(adapter.ExecutionError, "effects review is required"):
                        fixture.execute(payload, flow_run_id="flow-second")
                finally:
                    fixture.close()

    def test_post_effect_failure_or_timeout_cannot_launch_same_cycle_twice(self) -> None:
        for returncode in (7, 124):
            with self.subTest(returncode=returncode):
                fixture = Fixture()
                try:
                    payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
                    effects = fixture.vault / "effects.txt"

                    def post_effect(argv, **_kwargs):
                        with effects.open("a", encoding="utf-8") as handle:
                            handle.write("effect\n")
                        return subprocess.CompletedProcess(argv, returncode, "synthetic post-effect failure")

                    with mock.patch.object(adapter, "execute_process", side_effect=post_effect) as launch:
                        with self.assertRaises(adapter.ExecutionError):
                            fixture.execute(payload, flow_run_id="flow-first")
                        with self.assertRaisesRegex(adapter.ExecutionError, "effects review is required"):
                            fixture.execute(payload, flow_run_id="flow-second")
                    self.assertEqual((launch.call_count, effects.read_text(encoding="utf-8")), (1, "effect\n"))
                    receipt = tomllib.loads(adapter.receipt_path(fixture.vault, "sample", CYCLE).read_text())
                    self.assertEqual(receipt["verification"], "pending")
                finally:
                    fixture.close()

    def test_verified_cycle_is_idempotent_and_ambiguous_receipt_blocks_rerun(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        first = fixture.execute(payload, flow_run_id="flow-first")
        (fixture.bin / "codex").write_text("#!/bin/sh\nexit 19\n", encoding="utf-8")
        second = fixture.execute(payload, flow_run_id="flow-second")
        self.assertEqual(second["prefect_flow_run_id"], first["prefect_flow_run_id"])

        receipt = adapter.receipt_path(fixture.vault, "sample", CYCLE)
        adapter.write_receipt(receipt, {**first, "verification": "failed"})
        with self.assertRaisesRegex(adapter.ExecutionError, "effects review is required"):
            fixture.execute(payload, flow_run_id="flow-third")

    def test_v3_reuses_unbound_evidence_without_upgrade_or_model_work(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        legacy = fixture.execute(payload, flow_run_id="flow-original")
        legacy["contract_version"] = 3
        legacy.pop("artifact_sha256")
        legacy.pop("verification_scope")
        path = adapter.receipt_path(fixture.vault, "sample", CYCLE)
        adapter.write_receipt(path, legacy)
        before = path.read_bytes()
        (fixture.vault / legacy["output_file"]).write_text("later content", encoding="utf-8")
        with mock.patch.object(adapter, "execute_observed_model") as launch:
            reused = fixture.execute(payload, flow_run_id="flow-reuse")
        launch.assert_not_called()
        self.assertEqual(reused, legacy)
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_evidence_blocks_model_replay(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        delivered = fixture.execute(payload, flow_run_id="flow-original")
        path = adapter.receipt_path(fixture.vault, "sample", CYCLE)
        output = fixture.vault / delivered["output_file"]
        original = output.read_bytes()
        outside = Path(fixture.temporary.name) / "outside.md"
        outside.write_bytes(original)
        for problem in ("changed", "empty", "missing", "escape", "declaration", "scope", "hash",
                        "version", "identity", "corrupt", "dangling-receipt"):
            with self.subTest(problem=problem):
                path.unlink(missing_ok=True)
                output.unlink(missing_ok=True)
                output.write_bytes(original)
                receipt = dict(delivered)
                if problem == "changed":
                    output.write_bytes(b"!" + original[1:])
                elif problem == "empty":
                    output.write_bytes(b"")
                elif problem == "missing":
                    output.unlink()
                elif problem == "escape":
                    output.unlink()
                    output.symlink_to(outside)
                elif problem == "declaration":
                    output.with_suffix(".txt").write_bytes(original)
                    receipt["output_file"] = str(Path(receipt["output_file"]).with_suffix(".txt"))
                elif problem == "scope":
                    receipt["verification_scope"] = "business-outcome"
                elif problem == "hash":
                    receipt.pop("artifact_sha256")
                elif problem == "version":
                    receipt["contract_version"] = 99
                elif problem == "identity":
                    receipt["routine"] = "other"
                adapter.write_receipt(path, receipt)
                if problem == "corrupt":
                    path.write_text("not valid toml", encoding="utf-8")
                elif problem == "dangling-receipt":
                    path.unlink()
                    path.symlink_to(path.with_suffix(".missing"))
                with mock.patch.object(adapter, "execute_observed_model") as launch:
                    with self.assertRaisesRegex(adapter.ExecutionError, "effects review is required"):
                        fixture.execute(payload, flow_run_id="flow-replay")
                launch.assert_not_called()

    def test_artifact_binding_is_computed_by_the_parent(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        spec = adapter.ModelSpec.from_payload(adapter.prepare_model("sample", root=fixture.root, environ=fixture.env))
        output = fixture.vault / "outputs" / f"{CYCLE}.md"
        output.write_text("trusted bytes", encoding="utf-8")
        result = fixture.root / "result.json"
        result.write_text(json.dumps({
            "routine": "sample", "outcome": "delivered", "output_file": str(output),
            "summary": "claimed success", "skipped_inputs": [],
            "artifact_sha256": "fabricated", "verification_scope": "business-outcome",
        }))
        alias = Path(fixture.temporary.name) / "vault-alias"
        alias.symlink_to(fixture.vault, target_is_directory=True)
        value = adapter._model_result(
            result, spec, vault=alias, started_at=datetime.now().astimezone().isoformat()
        )
        self.assertEqual(value["verification_scope"], "artifact-bytes")
        self.assertEqual(value["artifact_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())

        content_hash = adapter.receipts.content_hash
        for data, reason in ((b"stale bytes", "stale"), (b"", "empty")):
            with self.subTest(replacement=reason):
                output.write_bytes(b"fresh bytes")
                replacement = output.with_suffix(".swap")
                replacement.write_bytes(data)
                os.utime(replacement, (1, 1))

                def swap_before_hash(path, **kwargs):
                    replacement.replace(path)
                    return content_hash(path, **kwargs)

                with mock.patch.object(adapter.receipts, "content_hash", side_effect=swap_before_hash):
                    with self.assertRaisesRegex(adapter.ExecutionError, reason):
                        adapter._model_result(result, spec, vault=alias,
                                              started_at=datetime.now().astimezone().isoformat())

    def test_artifact_mutation_during_hashing_refuses_attestation(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        output = fixture.vault / "artifact.md"
        output.write_bytes(b"before")
        digest = hashlib.file_digest

        def change_after_read(handle, algorithm):
            result = digest(handle, algorithm)
            output.write_bytes(b"after")
            return result

        with mock.patch.object(adapter.receipts.hashlib, "file_digest", side_effect=change_after_read):
            with self.assertRaisesRegex(ValueError, "changed during verification"):
                adapter.receipts.content_hash(output)

    def test_runtime_is_fixed_codex_and_environment_is_scrubbed(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        spec = adapter.ModelSpec.from_payload(adapter.prepare_model("sample", root=fixture.root, environ=fixture.env))
        env = adapter.runtime_env(spec, root=fixture.root, vault=fixture.vault, cycle=CYCLE, environ=fixture.env)
        argv = adapter.codex_argv(spec, root=fixture.root, vault=fixture.vault, cwd=fixture.root, output=fixture.root / "out")
        self.assertEqual(env["ATELIER_ACTIVE_RUNTIME"], "codex")
        self.assertNotIn("SECRET", env)
        self.assertIn('approval_policy="never"', argv)
        self.assertIn("--output-schema", argv)
        self.assertEqual(env["ATELIER_ROUTINE_PROFILE"], spec.profile)
        self.assertEqual(env["ATELIER_ROUTINE_PERMISSIONS"], ",".join(spec.profile_values["permissions"]))
        procedure = (ROOT / "routines/_adapters/archived-prompt/PROCEDURE.md").read_text()
        self.assertIn("```sh\n", procedure)
        self.assertIn("§ Runtime and permission boundary", procedure)
        check = procedure.split("```sh\n", 1)[1].split("```", 1)[0]
        for absent in (None, "ATELIER_ROUTINE_PROFILE", "ATELIER_ROUTINE_PERMISSIONS"):
            for value in (None, ""):
                supplied = dict(env)
                if absent:
                    supplied.pop(absent)
                    if value is not None:
                        supplied[absent] = value
                with self.subTest(absent=absent, value=value):
                    result = subprocess.run(["/bin/sh", "-c", check], env=supplied, timeout=3)
                    self.assertEqual(result.returncode, 1 if absent else 0)

    def test_a_routine_without_a_model_keeps_the_default_invocation(self) -> None:
        # Support is opt-in: an undeclared model must not change today's argv.
        fixture = Fixture()
        self.addCleanup(fixture.close)
        spec = adapter.ModelSpec.from_payload(adapter.prepare_model("sample", root=fixture.root, environ=fixture.env))
        argv = adapter.codex_argv(spec, root=fixture.root, vault=fixture.vault, cwd=fixture.root, output=fixture.root / "out")
        self.assertIsNone(spec.model)
        self.assertNotIn("-m", argv)
        self.assertIn('model_reasoning_effort="low"', argv)

    def test_a_declared_model_reaches_codex_with_its_binding(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        payload["model"] = "demo_identity"
        spec = adapter.ModelSpec.from_payload(payload)
        with mock.patch.object(adapter._models, "codex_binding", return_value=("vendor/demo-1", "xhigh")):
            argv = adapter.codex_argv(spec, root=fixture.root, vault=fixture.vault, cwd=fixture.root, output=fixture.root / "out")
        self.assertEqual(argv[:3], ["codex", "-m", "vendor/demo-1"])
        # the identity's own effort wins over the profile default
        self.assertIn('model_reasoning_effort="xhigh"', argv)

    def test_a_declared_model_without_a_binding_fails_closed(self) -> None:
        # Never silently fall back to the default model when one was asked for.
        fixture = Fixture()
        self.addCleanup(fixture.close)
        payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        payload["model"] = "demo_identity"
        spec = adapter.ModelSpec.from_payload(payload)
        with mock.patch.object(adapter._models, "codex_binding", return_value=("", "")):
            with self.assertRaisesRegex(adapter.ConfigurationError, "no codex binding"):
                adapter.codex_argv(spec, root=fixture.root, vault=fixture.vault, cwd=fixture.root, output=fixture.root / "out")

    def test_an_undeclared_identity_is_refused_at_configuration_load(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        watch = fixture.vault / "_tools/routines/registry.toml"
        watch.write_text(
            watch.read_text(encoding="utf-8") + 'model = "no_such_identity"\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(adapter.ConfigurationError, "missing from harness/models.toml"):
            adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)

    def test_the_payload_carries_the_identity_not_the_provider_id(self) -> None:
        # load_specs must not leak gitignored bindings into Prefect state.
        fixture = Fixture()
        self.addCleanup(fixture.close)
        payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        payload["model"] = "demo_identity"
        self.assertNotIn("vendor/demo-1", json.dumps(payload))

    def test_tool_caches_and_claude_credentials_refresh_before_sandbox_entry(self) -> None:
        # CodexBar cannot refresh Claude OAuth, so quota:read refreshes it unsandboxed.
        fixture = Fixture()
        self.addCleanup(fixture.close)
        payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        spec = adapter.ModelSpec.from_payload(payload)
        spec.profile_values["permissions"] += ["readwise:read", "quota:read"]
        for tool in ("readwise", "claude"):
            (fixture.bin / tool).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fixture.bin / tool).chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        completed = subprocess.CompletedProcess(["tool"], 0, "")
        with mock.patch.object(adapter, "execute_process", return_value=completed) as execute:
            adapter.warm_before_sandbox(spec, env=fixture.env, root=fixture.root)

        kwargs = dict(env=fixture.env, cwd=fixture.root, seconds=30, input_text="")
        self.assertEqual(execute.call_args_list, [
            mock.call(["readwise", "--refresh", "--version"], **kwargs),
            mock.call(["claude", "auth", "status", "--json"], **kwargs),
        ])

        spec.profile_values["permissions"] = ["quota:read-extra"]
        with mock.patch.object(adapter, "execute_process") as execute:
            adapter.warm_before_sandbox(spec, env=fixture.env, root=fixture.root)
        execute.assert_not_called()

    def test_pre_sandbox_refresh_failure_is_nonfatal(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        payload = adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        spec = adapter.ModelSpec.from_payload(payload)
        spec.profile_values["permissions"].append("quota:read")
        (fixture.bin / "claude").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (fixture.bin / "claude").chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        completed = subprocess.CompletedProcess(["claude"], 1, "PRIVATE_TEST_MARKER")
        output = io.StringIO()
        with mock.patch.object(adapter, "execute_process", return_value=completed), redirect_stdout(output):
            adapter.warm_before_sandbox(spec, env=fixture.env, root=fixture.root)

        self.assertIn("warning: claude refresh failed before sandbox entry", output.getvalue())
        self.assertNotIn("PRIVATE_TEST_MARKER", output.getvalue())

    def test_private_process_uses_the_explicit_vault_environment(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        script = fixture.vault / "jobs/collector.py"
        script.parent.mkdir()
        script.write_text("pass\n", encoding="utf-8")
        watch = fixture.vault / "_tools/routines/registry.toml"
        watch.write_text(
            watch.read_text(encoding="utf-8")
            + """

[[routine]]
name = "collector"
execution = "local"
runner = "process"
script = "jobs/collector.py"
cron = "0 6 * * *"
timezone = "UTC"
retry_safe = false
""",
            encoding="utf-8",
        )

        _models, processes = adapter.load_specs(root=fixture.root, environ=fixture.env)
        collector = next(item for item in processes if item.schedule.name == "collector")
        self.assertEqual(collector.cwd, str(script.parent.resolve()))
        self.assertEqual(collector.argv[-1], str(script.resolve()))

    def test_runtime_fallback_is_rejected(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        registry = fixture.root / "harness/routine_profiles.toml"
        registry.write_text(
            registry.read_text(encoding="utf-8").replace(
                'surface = "local"', 'surface = "local"\nfallback_runtime = "claude"', 1
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(adapter.ConfigurationError, "runtime selection is unsupported"):
            adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)

    def test_model_attempt_retries_are_rejected_but_safe_preflight_retries(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        watch = fixture.vault / "_tools/routines/registry.toml"
        watch.write_text(
            watch.read_text(encoding="utf-8").replace('cron = "0 5 * * *"', 'cron = "0 5 * * *"\nretries = 1'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(adapter.ConfigurationError, "model attempts cannot declare retries"):
            adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)
        self.assertEqual(orchestration.prepare_model_task.retries, 2)
        self.assertEqual(orchestration.model_attempt_task.retries, 0)

    def test_required_plugins_must_be_installed_and_enabled(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        registry = fixture.root / "harness/routine_profiles.toml"
        registry.write_text(
            registry.read_text(encoding="utf-8")
            .replace('user_config = "ignore"', 'user_config = "required"')
            .replace('required_plugins = []', 'required_plugins = ["fixture@example"]'),
            encoding="utf-8",
        )
        codex = fixture.bin / "codex"
        codex.write_text("#!/bin/sh\necho 'other@example installed, enabled'\n", encoding="utf-8")
        with self.assertRaisesRegex(adapter.ConfigurationError, "missing required Codex plugins"):
            adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)

        codex.write_text("#!/bin/sh\necho 'fixture@example installed, enabled'\n", encoding="utf-8")
        self.assertEqual(adapter.prepare_model("sample", root=fixture.root, environ=fixture.env)["profile"], "fixture")

    def test_deployments_queue_overlap_and_runner_is_globally_serial(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        deployments = orchestration.deployments(root=fixture.root, environ=fixture.env)
        self.assertEqual({item.name for item in deployments}, {"sample", "dummy"})
        for deployment in deployments:
            self.assertEqual(deployment.concurrency_limit, 1)
            self.assertEqual(deployment.concurrency_options.collision_strategy.value, "ENQUEUE")
        with mock.patch.object(orchestration, "deployments", return_value=deployments), mock.patch.object(
            orchestration, "serve"
        ) as serve:
            self.assertEqual(orchestration.main(["serve"]), 0)
            self.assertEqual(serve.call_args.kwargs["limit"], 1)

    def test_manual_run_submits_registered_deployment_instead_of_calling_flow(self) -> None:
        flow_run = SimpleNamespace(id="flow-manual", state=Completed())
        output = io.StringIO()
        with mock.patch.object(orchestration, "run_deployment", return_value=flow_run) as submit, redirect_stdout(output):
            self.assertEqual(orchestration.main(["run", "sample", "--cycle", CYCLE]), 0)
        submit.assert_called_once_with(
            "atelier-model-routine/sample",
            parameters={"routine": "sample", "cycle": CYCLE},
            as_subflow=False,
        )
        self.assertEqual(json.loads(output.getvalue())["id"], "flow-manual")

    def test_status_uses_effective_loopback_settings_and_disables_ephemeral(self) -> None:
        def stop_before_client(**_kwargs):
            self.assertEqual(PREFECT_API_URL.value(), adapter.DEFAULT_PREFECT_API_URL)
            self.assertIs(PREFECT_SERVER_EPHEMERAL_ENABLED.value(), False)
            raise RuntimeError("sentinel")

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            status, "get_client", side_effect=stop_before_client
        ) as get_client:
            with self.assertRaisesRegex(status.StatusUnavailable, "sentinel"):
                status.recent_runs(datetime.now(timezone.utc))
        get_client.assert_called_once()

    def test_nonlocal_prefect_api_is_refused_before_status_run_or_serve(self) -> None:
        cloud = "https://api.prefect.cloud/api/accounts/example/workspaces/example"
        with mock.patch.dict(os.environ, {"PREFECT_API_URL": cloud}, clear=True), mock.patch.object(
            status, "get_client"
        ) as get_client:
            with self.assertRaisesRegex(status.StatusUnavailable, "loopback"):
                status.recent_runs(datetime.now(timezone.utc))
            get_client.assert_not_called()

            with mock.patch.object(orchestration, "run_deployment") as submit:
                self.assertEqual(orchestration.main(["run", "sample", "--cycle", CYCLE]), 2)
                submit.assert_not_called()

            with mock.patch.object(orchestration, "deployments") as configured, mock.patch.object(
                orchestration, "serve"
            ) as serve:
                self.assertEqual(orchestration.main(["serve"]), 2)
                configured.assert_not_called()
                serve.assert_not_called()

    def test_validation_rejects_invalid_cron_before_service_start(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        watch = fixture.vault / "_tools/routines/registry.toml"
        watch.write_text(
            watch.read_text(encoding="utf-8").replace('cron = "0 5 * * *"', 'cron = "not a cron"'),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            orchestration.validation_payload(root=fixture.root, environ=fixture.env)

    def test_launchd_artifacts_only_boot_the_local_prefect_services(self) -> None:
        for name, mode in (("prefect-server", "server"), ("prefect-routines", "serve")):
            plist = plistlib.loads((ROOT / f"scripts/launchd/com.atelier.{name}.plist").read_bytes())
            self.assertEqual(plist["Label"], f"com.atelier.{name}")
            self.assertTrue(plist["RunAtLoad"])
            self.assertTrue(plist["KeepAlive"])
            self.assertEqual(plist["ProgramArguments"][-1], mode)
        service = (ROOT / "scripts/routine_prefect_service.sh").read_text(encoding="utf-8")
        self.assertIn("prefect server start --host 127.0.0.1", service)
        self.assertIn('PREFECT_SERVER_ANALYTICS_ENABLED="${ATELIER_PREFECT_SERVER_ANALYTICS_ENABLED:-false}"', service)
        self.assertIn("scripts/routine_prefect.py serve", service)

    def test_public_index_schedule_uses_the_current_qmd_cli(self) -> None:
        registry = tomllib.loads((ROOT / "routines/registry.toml").read_text(encoding="utf-8"))
        row = next(job for job in registry["routine"] if job["name"] == "semantic-index")
        argv = row["argv"]
        args = semantic.build_parser().parse_args(argv[argv.index("scripts/semantic.py") + 1:])
        self.assertEqual(args.command, "index")
        self.assertFalse(args.lexical_only)
        self.assertEqual(row["cron"], ["30 7 * * *", "30 19 * * *"])
        self.assertTrue(row["retry_safe"])
        self.assertEqual(row["retries"], 1)

    def test_status_cli_exposes_bounded_prefect_state(self) -> None:
        run = {
            "id": "flow-1",
            "routine": "sample",
            "state": "COMPLETED",
            "state_name": "Completed",
            "message": "",
            "start_time": datetime(2099, 1, 2, tzinfo=timezone.utc),
            "expected_start_time": None,
            "end_time": datetime(2099, 1, 2, 0, 1, tzinfo=timezone.utc),
        }
        output = io.StringIO()
        with mock.patch.object(status, "recent_runs", return_value=[run]) as recent, redirect_stdout(output):
            self.assertEqual(status.main(["--hours", "24", "--limit", "3", "--json"]), 0)
        recent.assert_called_once()
        self.assertEqual(json.loads(output.getvalue())[0]["routine"], "sample")

    def test_status_refuses_a_page_over_the_prefect_api_ceiling_before_querying(self) -> None:
        # The API answers a larger page with a 422 whose message reads as a
        # filter error, so the ceiling has to be refused on this side.
        with mock.patch.object(status, "get_client") as get_client:
            with self.assertRaisesRegex(ValueError, "between 1 and 200"):
                status.recent_runs(datetime.now(timezone.utc), limit=status.MAX_LIMIT + 1)
            get_client.assert_not_called()

        errors = io.StringIO()
        with mock.patch.object(status, "recent_runs") as recent, redirect_stderr(errors):
            with self.assertRaises(SystemExit) as refused:
                status.main(["--limit", str(status.MAX_LIMIT + 1), "--json"])
        self.assertEqual(refused.exception.code, 2)
        self.assertIn("--limit must be between 1 and 200", errors.getvalue())
        recent.assert_not_called()

        with mock.patch.object(status, "recent_runs", return_value=[]) as recent, redirect_stdout(io.StringIO()):
            self.assertEqual(status.main(["--limit", str(status.MAX_LIMIT), "--json"]), 0)
        self.assertEqual(recent.call_args.kwargs["limit"], status.MAX_LIMIT)


class RoutineReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.until = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        self.since = self.until - timedelta(hours=168)
        self.client = mock.MagicMock()
        self.enterContext(mock.patch.object(status, "get_client", return_value=self.client))
        self.client.__enter__.return_value = self.client
        self.client.read_flow_runs.return_value = []
        self.client.read_logs.return_value = []

    def run_row(self, identity, *, kind="model", state="COMPLETED", state_name=None,
                start=None, end=None, expected=None):
        start = self.until - timedelta(minutes=2) if start is None else start
        end = start + timedelta(minutes=1) if end is None and start and state == "COMPLETED" else end
        return SimpleNamespace(
            id=uuid.uuid5(uuid.NAMESPACE_OID, str(identity)), parameters={"job" if kind == "process" else "routine": kind},
            state=SimpleNamespace(type=SimpleNamespace(value=state), name=state_name or state.title(), message=""),
            start_time=start, expected_start_time=expected, end_time=end,
        )

    def log_row(self, identity, *, tokens=5, coverage="complete", seconds=30, cost=None):
        identity = str(uuid.uuid5(uuid.NAMESPACE_OID, str(identity)))
        record = {
            "version": 1, "flow_run_id": str(identity), "coverage": coverage,
            "duration_seconds": seconds, "cost_usd_estimate": cost,
            "usage": {key: tokens for key in status.TOKEN_KEYS},
        }
        return SimpleNamespace(flow_run_id=str(identity), message="atelier.observation " + json.dumps(record))

    @staticmethod
    def pages(rows):
        return lambda **kwargs: rows[kwargs["offset"]:kwargs["offset"] + kwargs["limit"]]

    def report(self):
        return status.routine_report(self.since, self.until)

    def test_more_than_two_pages_of_runs_and_logs_are_deduplicated(self) -> None:
        rows = [self.run_row(i) for i in range(401)]
        logs = [self.log_row(0, tokens=9), self.log_row(0, tokens=4)]
        logs.extend(self.log_row(i) for i in range(1, 401))
        self.client.read_flow_runs.side_effect = self.pages([rows[0], *rows])
        self.client.read_logs.side_effect = self.pages(logs)
        report = self.report()
        self.assertTrue(report["complete"])
        overall = report["overall"]
        self.assertEqual(overall["runs"], 401)
        self.assertEqual(overall["usage_coverage"], {"complete": 401, "partial": 0, "unknown": 0})
        self.assertEqual(overall["tokens"]["input_tokens"], {"total": 2009, "measured_runs": 401})
        self.assertEqual(overall["tokens"]["cached_input_tokens"]["total"], 2009)
        self.assertEqual(overall["tokens"]["reasoning_output_tokens"]["total"], 2009)
        self.assertEqual(overall["wall_seconds"], {
            "total": 24060, "measured_runs": 401, "mean": 60, "median": 60, "p95": 60,
        })
        self.assertEqual([call.kwargs["offset"] for call in self.client.read_flow_runs.call_args_list], [0, 200, 400])
        self.assertEqual([call.kwargs["offset"] for call in self.client.read_logs.call_args_list], [0, 200, 400])
        for call in self.client.read_flow_runs.call_args_list:
            self.assertEqual(call.kwargs["flow_run_filter"].start_time.before_, self.until)
        self.assertEqual(self.client.read_logs.call_args.kwargs["log_filter"].timestamp.before_, self.until)

    def test_latest_invalid_record_does_not_mask_valid_observation(self) -> None:
        self.client.read_flow_runs.return_value = [self.run_row("1")]
        self.client.read_logs.return_value = [
            self.log_row("1", tokens="bad"), self.log_row("1", coverage=[]),
            self.log_row("1", tokens=None), self.log_row("1", tokens=0, cost=0),
            self.log_row("1", tokens=20), self.log_row("other", tokens=50),
        ]
        overall = self.report()["overall"]
        self.assertEqual(overall["tokens"]["input_tokens"], {"total": 0, "measured_runs": 1})
        self.assertEqual(overall["cost_usd_estimate"], {"total": 0, "measured_runs": 1})

    def test_later_run_page_failure_preserves_partial_data_and_screens_error(self) -> None:
        self.client.read_flow_runs.side_effect = [
            [self.run_row(i) for i in range(200)], RuntimeError("secret pathname and private API body"),
        ]
        report = self.report()
        self.assertFalse(report["complete"])
        self.assertEqual(report["overall"]["runs"], 200)
        self.assertEqual(report["errors"], [{"stage": "runs", "page": 1, "error": "RuntimeError"}])
        self.assertNotIn("secret", json.dumps(report))

    def test_later_log_page_failure_preserves_observed_tokens(self) -> None:
        self.client.read_flow_runs.return_value = [self.run_row("1")]
        self.client.read_logs.side_effect = [
            [self.log_row("1", tokens=7) for _ in range(200)], RuntimeError("secret"),
        ]
        report = self.report()
        self.assertFalse(report["complete"])
        self.assertEqual(report["overall"]["tokens"]["input_tokens"], {"total": 7, "measured_runs": 1})
        self.assertEqual(report["errors"], [{"stage": "observations", "page": 1, "error": "RuntimeError"}])

    def test_page_cap_is_explicit_for_runs_and_logs(self) -> None:
        self.client.read_flow_runs.return_value = [self.run_row(i) for i in range(200)]
        self.client.read_logs.return_value = [self.log_row(0) for _ in range(200)]
        with mock.patch.object(status, "MAX_PAGES", 2):
            report = self.report()
        self.assertFalse(report["complete"])
        self.assertEqual(report["max_pages"], 2)
        self.assertEqual(report["errors"], [
            {"stage": "runs", "error": "page_cap", "max_pages": 2},
            {"stage": "observations", "error": "page_cap", "max_pages": 2},
        ])

    def test_states_kinds_running_time_and_unknown_vs_zero_usage(self) -> None:
        overdue = self.run_row("scheduled", state="SCHEDULED", expected=self.until - timedelta(minutes=1))
        overdue.start_time = None
        future = self.run_row("future", state="SCHEDULED", expected=self.until + timedelta(minutes=1))
        future.start_time = None
        self.client.read_flow_runs.return_value = [
            self.run_row("complete"), self.run_row("process", kind="process"),
            self.run_row("failed", state="FAILED"), self.run_row("deferred", state="FAILED", state_name="Deferred"),
            self.run_row("running", state="RUNNING"), overdue, future,
            self.run_row("old", start=self.since - timedelta(seconds=1)),
        ]
        self.client.read_logs.return_value = [
            self.log_row("complete", tokens=0, seconds=0), self.log_row("failed", tokens=None, coverage="partial"),
            self.log_row("process", tokens=999),
        ]
        report = self.report()
        overall = report["overall"]
        self.assertEqual(overall["runs"], 6)
        self.assertEqual(overall["kinds"], {"model": 5, "process": 1})
        self.assertEqual(overall["states"], {"COMPLETED": 2, "FAILED": 1, "DEFERRED": 1, "RUNNING": 1, "SCHEDULED": 1})
        self.assertEqual(overall["due_unstarted"], 1)
        self.assertEqual(overall["usage_coverage"], {"complete": 1, "partial": 1, "unknown": 3})
        self.assertEqual(overall["wall_seconds"]["total"], 240)
        self.assertEqual(overall["wall_seconds"]["measured_runs"], 3)
        self.assertEqual(overall["wall_seconds"]["p95"], 120)
        self.assertEqual(overall["model_seconds"], {"total": 30, "measured_runs": 2})
        self.assertEqual(overall["tokens"]["input_tokens"], {"total": 0, "measured_runs": 1})
        self.assertEqual(overall["cost_usd_estimate"], {"total": None, "measured_runs": 0})

    def test_local_day_boundary_and_window_metadata(self) -> None:
        previous_tz = os.environ.get("TZ")
        def restore_timezone():
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            time.tzset()
        self.addCleanup(restore_timezone)
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        self.client.read_flow_runs.return_value = [
            self.run_row("before", start=datetime(2026, 10, 1, 6, 59, tzinfo=timezone.utc)),
            self.run_row("after", start=datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)),
        ]
        report = self.report()
        self.assertEqual({key: row["runs"] for key, row in report["by_day"].items()}, {"2026-09-30": 1, "2026-10-01": 1})
        self.assertEqual(report["window"]["since"], self.since.isoformat())
        self.assertTrue(report["window"]["local_until"].endswith("-07:00"))
        self.assertEqual(report["window"]["cohort"], "actual_start_or_unstarted_due")

    def test_empty_data_keeps_missing_measurements_null(self) -> None:
        overall = self.report()["overall"]
        self.assertEqual(overall["runs"], 0)
        self.assertEqual(overall["wall_seconds"]["total"], None)
        self.assertEqual(overall["tokens"]["input_tokens"], {"total": None, "measured_runs": 0})
        self.assertEqual(overall["usage_coverage"], {"complete": 0, "partial": 0, "unknown": 0})

    def test_invalid_windows_fail_before_query(self) -> None:
        for since, until in ((self.until, self.since), (self.since.replace(tzinfo=None), self.until)):
            with self.assertRaises(ValueError):
                status.routine_report(since, until)
        self.client.read_flow_runs.assert_not_called()

    def test_report_cli_exits_nonzero_with_reviewable_partial_json(self) -> None:
        report = {"complete": False, "overall": {"runs": 200}, "errors": [{"error": "RuntimeError"}]}
        output = io.StringIO()
        with mock.patch.object(status, "routine_report", return_value=report) as read, redirect_stdout(output):
            self.assertEqual(status.main(["--report", "--hours", "168", "--json"]), 2)
        since, until = read.call_args.args
        self.assertEqual(until - since, timedelta(hours=168))
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_legacy_reader_is_bounded_and_shape_is_unchanged(self) -> None:
        self.client.read_flow_runs.return_value = [self.run_row("1")]
        self.client.read_logs.return_value = [self.log_row("1", tokens=0)]
        rows = status.recent_runs(self.since, limit=1, usage=True)
        self.assertEqual(set(rows[0]), {"id", "routine", "state", "state_name", "message",
                                       "start_time", "expected_start_time", "end_time", "observation"})
        self.assertEqual(self.client.read_flow_runs.call_count, 1)
        self.assertEqual(self.client.read_logs.call_count, 1)
        self.assertEqual(self.client.read_flow_runs.call_args.kwargs["limit"], 1)
        self.client.read_logs.side_effect = RuntimeError("secret")
        self.assertIsNone(status.recent_runs(self.since, usage=True)[0]["observation"])


CLAUDE_FAKE = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ['mcp', 'list']:
    print('Checking MCP server health...')
    print('claude.ai Gmail: https://gmail.invalid/mcp - ✔ Connected')
    sys.exit(0)
prompt = sys.stdin.read()
call = {'args': args, 'prompt': prompt, 'runtime': os.environ.get('ATELIER_ACTIVE_RUNTIME')}
Path(os.environ['TMPDIR'], 'claude-call.json').write_text(json.dumps(call))
output = Path(os.environ['OV']) / 'outputs' / (os.environ['ATELIER_ROUTINE_CYCLE'] + '.md')
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text('verified artifact\\n', encoding='utf-8')
print('warning: unrelated stderr noise', file=sys.stderr)
envelope = {'routine': 'sample', 'outcome': 'delivered', 'output_file': str(output), 'summary': 'ok', 'skipped_inputs': []}
print(json.dumps({'type': 'result', 'is_error': False, 'structured_output': envelope}))
"""


class ClaudeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.profiles = self.fixture.root / "harness/routine_profiles.toml"
        self.profiles.write_text(
            self.profiles.read_text(encoding="utf-8")
            .replace('surface = "local"', 'runtime = "claude"\nsurface = "local"', 1)
            .replace('user_config = "ignore"', 'user_config = "required"')
            .replace('"vault:read-write"]', '"vault:read-write", "gmail:read"]')
            .replace("required_plugins = []", 'required_plugins = ["claude.ai Gmail"]'),
            encoding="utf-8",
        )
        self.claude = self.fixture.bin / "claude"
        self.claude.write_text(CLAUDE_FAKE, encoding="utf-8")
        self.claude.chmod(0o755)

    def prepare(self) -> dict[str, object]:
        return adapter.prepare_model("sample", root=self.fixture.root, environ=self.fixture.env)

    def test_argv_grants_only_mapped_read_tools_and_ignores_user_settings(self) -> None:
        (self.fixture.root / "harness/routine_result.schema.json").write_text(
            json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}), encoding="utf-8")
        spec = adapter.resolve_model("sample", root=self.fixture.root, environ=self.fixture.env)
        argv = adapter.claude_argv(spec, root=self.fixture.root, vault=self.fixture.vault)
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), {"type": "object"})
        rules = json.loads(argv[argv.index("--settings") + 1])["permissions"]
        self.assertIn(f"Edit(/{self.fixture.vault}/**)", rules["allow"])
        gmail = sorted(rule.rsplit("__", 1)[1] for rule in rules["allow"] if "Gmail" in rule)
        self.assertEqual(gmail, ["get_message", "get_thread", "list_labels", "search_threads"])
        self.assertIn("Bash", rules["deny"])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")

    def test_claude_profile_must_stay_offline_and_mapped(self) -> None:
        text = self.profiles.read_text(encoding="utf-8")
        for old, new in (
            ('web_search = "disabled"', 'web_search = "live"'),
            ('"gmail:read"]', '"gmail:read", "web:live"]'),
            ('runtime = "claude"', 'runtime = "other"'),
        ):
            with self.subTest(new=new):
                self.profiles.write_text(text.replace(old, new, 1), encoding="utf-8")
                with self.assertRaises(adapter.ConfigurationError):
                    self.prepare()

    def test_preflight_requires_connected_claude_connector(self) -> None:
        self.claude.write_text(CLAUDE_FAKE.replace("✔ Connected", "✗ Failed to connect"), encoding="utf-8")
        with self.assertRaisesRegex(adapter.ConfigurationError, "missing required Claude connectors: claude.ai Gmail"):
            self.prepare()
        self.claude.write_text(CLAUDE_FAKE, encoding="utf-8")
        self.assertEqual(self.prepare()["profile"], "fixture")

    def test_structured_output_becomes_a_verified_claude_receipt(self) -> None:
        receipt = self.fixture.execute(self.prepare(), flow_run_id="flow-claude")
        self.assertEqual((receipt["runtime"], receipt["verification"]), ("claude", "passed"))
        call = json.loads((Path(self.fixture.temporary.name) / "claude-call.json").read_text(encoding="utf-8"))
        self.assertEqual(call["runtime"], "claude")
        self.assertIn("Routine identity: `sample`", call["prompt"])
        self.assertNotIn("Codex adaptation table", call["prompt"])
        self.assertNotIn(call["prompt"], call["args"])

    def test_error_result_is_not_a_delivery(self) -> None:
        self.claude.write_text(CLAUDE_FAKE.replace("'is_error': False", "'is_error': True"), encoding="utf-8")
        with self.assertRaisesRegex(adapter.ExecutionError, "no valid result envelope"):
            self.fixture.execute(self.prepare(), flow_run_id="flow-claude-error")

    def test_failed_run_reports_its_screened_output(self) -> None:
        self.claude.write_text(
            CLAUDE_FAKE.replace("prompt = sys.stdin.read()", "print('upstream API error'); sys.exit(3)"),
            encoding="utf-8")
        with self.assertRaisesRegex(adapter.ExecutionError, "claude exited 3: .*upstream API error") as caught:
            self.fixture.execute(self.prepare(), flow_run_id="flow-claude-failed")
        self.assertNotIn("observation coverage", str(caught.exception))


class ShutdownHandoffTests(unittest.TestCase):
    """Prefect owns SIGTERM; the detached model attempt must still die with the runner."""

    def test_terminate_live_children_stops_a_detached_group(self):
        child = subprocess.Popen(["/bin/sh", "-c", "sleep 60"], start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        child_processes.track_process(child)
        self.addCleanup(child_processes.release_process, child)
        child_processes.terminate_live_children()
        self.assertIsNotNone(child.poll(), "a registered child survived terminate_live_children")
        self.assertNotIn(child, child_processes._LIVE_CHILDREN)

    def test_prefects_own_sigterm_handler_still_reaps_the_grandchild(self):
        """Runner.start() re-registers SIGTERM and calls sys.exit(0); atexit must carry the teardown.

        Registering our own SIGTERM handler would lose that race silently, so this
        stands in Prefect's handler exactly and asserts the grandchild still dies.
        """
        program = (
            "import signal, sys, threading, time\n"
            f"sys.path.insert(0, {str(adapter.ROOT / 'scripts')!r})\n"
            "import routine_adapter as adapter\n"
            "import command_timeout as child_processes\n"
            # stand in for prefect.runner.Runner.start()'s registration
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "threading.Thread(target=lambda: adapter.execute_process(\n"
            "    ['/bin/sh', '-c', 'echo $$ >&2; sleep 60'], seconds=60), daemon=True).start()\n"
            "while not child_processes._LIVE_CHILDREN:\n"
            "    time.sleep(0.05)\n"
            "print(next(iter(child_processes._LIVE_CHILDREN)).pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        runner = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE, text=True)
        self.addCleanup(runner.stdout.close)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        grandchild = int(runner.stdout.readline().strip())
        os.kill(grandchild, 0)  # alive before the runner is told to stop

        runner.terminate()
        runner.wait(timeout=15)
        for _ in range(100):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(grandchild, 9)
            self.fail(f"grandchild {grandchild} survived the runner's shutdown")


class AutoevoAdapterTests(unittest.TestCase):
    """Model/semantic/publication boundaries are mocked; no server or Git fixture."""

    def setUp(self) -> None:
        import autoevo_verify

        self.verify = autoevo_verify
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.root, self.vault = self.fixture.root.resolve(), self.fixture.vault.resolve()
        self.env = {**self.fixture.env, "OV": str(self.vault)}
        profile = self.root / "harness/routine_profiles.toml"
        profile.write_text(
            profile.read_text().replace('["archived-prompt"]', '["autoevo"]'), encoding="utf-8"
        )
        registry = self.vault / "_tools/routines/registry.toml"
        registry.write_text(
            registry.read_text().replace('adapter = "archived-prompt"', 'adapter = "autoevo"'),
            encoding="utf-8",
        )
        payload = adapter.prepare_model("sample", root=self.root, environ=self.env)
        payload["schedule"]["name"] = "autoevo-nightly"
        payload["profile_values"]["permissions"] = ["atelier:read", "vault:read", "staging:write"]
        self.spec = adapter.ModelSpec.from_payload(payload)
        source = self.vault / "wip/note.md"
        source.parent.mkdir()
        source.write_text("clean original\n", encoding="utf-8")
        self.models = Path(self.fixture.temporary.name).resolve() / "cached-models"
        self.models.mkdir()
        self.plan = {
            "schema_version": self.verify.VERSION, "cycle_id": CYCLE, "run_id": "fixture",
            "base_head": "original-head", "protected_paths": ["wip/protected.md"],
            "state_files": {"_meta/autoevo_pending.toml": "original-state"},
            "dispatches": [{"scope": scope} for scope in ("wip", "research/topic", "reflections")],
            "quarantine_skipped": [], "notes": [], "source_files": {},
        }
        self.prior = self.enterContext(mock.patch("autoevo_run.prior_result", return_value=None))
        self.preflight = self.enterContext(mock.patch(
            "autoevo_preflight.inspect_preflight", return_value={"ready": True},
        ))
        self.prepare = self.enterContext(mock.patch("autoevo_run.prepare_workspace", side_effect=self._prepare))
        self.semantic = self.enterContext(mock.patch.object(semantic, "prepare_query_copy", side_effect=self._semantic))
        self.model = self.enterContext(mock.patch.object(adapter, "execute_process", side_effect=self._candidate))
        self.accept = self.enterContext(mock.patch("autoevo_run.accept_proposal", side_effect=self._accept))
        self.receipt = self.enterContext(mock.patch.object(
            adapter, "write_receipt", side_effect=AssertionError("generic receipt is forbidden"),
        ))
        self.enterContext(mock.patch.object(
            adapter, "_model_result", side_effect=AssertionError("transport ack is not domain evidence"),
        ))
        self.enterContext(mock.patch.object(
            adapter.receipts, "read", side_effect=AssertionError("Autoevo must use its own verifier"),
        ))

    def _prepare(self, vault, workspace, cycle, readiness):
        self.assertEqual((vault, cycle, readiness["ready"]), (self.vault, CYCLE, True))
        snapshot = workspace / "sources/wip/note.md"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text("clean original\n", encoding="utf-8")
        self.plan["source_files"] = {
            "wip/note.md": {"snapshot": str(snapshot), "before_sha256": "original-source",
                           "mtime_ns": 123, "mode": 0o644},
        }
        self.original_plan = json.loads(json.dumps(self.plan))
        return self.plan

    def _semantic(self, vault, destination):
        self.assertEqual(vault, self.vault)
        destination.mkdir()
        (destination / "index.sqlite").write_text("fixture derived database")
        return {"ATELIER_QMD_HOME": str(destination),
                semantic.STAGED_MODEL_DIRECTORY_ENV: str(self.models)}

    def _proposal(self):
        return {
            "schema_version": self.verify.VERSION, "cycle_id": CYCLE,
            "sweeps": [
                {"scope": row["scope"], "outcome": "envelope_returned", "mode": "full",
                 "completion_status": "complete", "remaining_work": "", "gaps": "",
                 "findings": [], "notes": []}
                for row in self.plan["dispatches"]
            ],
            "judgments": {}, "notes": [], "errors": [],
        }

    def _candidate(self, argv, *, env, cwd, seconds, usage=None):
        self.assertEqual(seconds, self.spec.profile_values["timeout_seconds"])
        self.assertTrue((cwd / "schema.json").is_file())
        self.assertTrue((cwd / "sources/wip/note.md").is_file())
        (cwd / "proposal.json").write_text(json.dumps(self._proposal()))
        (cwd / "result.json").write_text("deliberately invalid transport acknowledgment")
        return subprocess.CompletedProcess(argv, 0, "")

    def _accept(self, vault, proposal, plan, *, flow_run_id):
        self.assertEqual((vault, flow_run_id), (self.vault, "fixture-flow"))
        self.assertIs(plan, self.plan)
        self.assertEqual(plan, self.original_plan)
        self.verify.validate_proposal(proposal, plan)
        return {"status": "complete", "cycle_id": CYCLE}

    def _execute(self):
        return adapter.execute_model(
            self.spec.payload(), cycle=CYCLE, flow_run_id="fixture-flow",
            root=self.root, environ=self.env,
        )

    def _live(self):
        return {p.relative_to(self.vault): p.read_bytes() if p.is_file() else None
                for p in self.vault.rglob("*")}

    def test_argv_has_only_the_isolated_workspace_writable(self) -> None:
        cwd = Path(self.fixture.temporary.name).resolve() / "workspace"
        argv = adapter.codex_argv(self.spec, root=self.root, vault=self.vault, cwd=cwd, output=cwd / "result.json")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(argv[argv.index("--ask-for-approval") + 1], "never")
        self.assertEqual(argv[argv.index("-C") + 1], str(cwd))
        self.assertIn("--ignore-user-config", argv)
        config = tomllib.loads("\n".join(argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"))
        self.assertEqual(config["approval_policy"], "never")
        # Named profiles override legacy flags; each path must confine writes independently.
        self.assertEqual(config["sandbox_workspace_write"], {
            "writable_roots": [], "exclude_slash_tmp": True,
            "exclude_tmpdir_env_var": True, "network_access": False,
        })
        self.assertEqual(config["default_permissions"], "atelier-autoevo-draft")
        self.assertEqual(config["permissions"], {"atelier-autoevo-draft": {
            "filesystem": {
                ":root": "read", str(cwd): "write",
                **{str(cwd / name): "read" for name in (".git", ".codex", ".agents")},
            },
            "network": {"enabled": False},
        }})
        filesystem = config["permissions"]["atelier-autoevo-draft"]["filesystem"]
        self.assertEqual([path for path, access in filesystem.items() if access == "write"], [str(cwd)])
        for flag in ("--add-dir", "--dangerously-bypass-hook-trust", "--search",
                     "--dangerously-bypass-approvals-and-sandbox", str(self.root), str(self.vault)):
            self.assertNotIn(flag, argv)

    def test_unsafe_profiles_are_rejected(self) -> None:
        for field, value in (
            ("sandbox", "danger-full-access"), ("atelier_access", "read-write"),
            ("shell_network", "enabled"), ("user_config", "required"), ("web_search", "live"),
            ("required_plugins", ["fixture@example"]), ("optional_plugins", ["fixture@example"]),
        ):
            payload = self.spec.payload()
            payload["profile_values"][field] = value
            with self.subTest(field=field), self.assertRaises(adapter.ConfigurationError):
                adapter.codex_argv(adapter.ModelSpec.from_payload(payload), root=self.root,
                                   vault=self.vault, cwd=self.root, output=self.root / "result.json")

    def test_deferred_preflight_writes_nothing_and_never_starts_model(self) -> None:
        before = self._live()
        self.preflight.return_value = {"ready": False, "gate": "session_active", "detail": "fixture"}
        with self.assertRaises(adapter.DeferredRun):
            self._execute()
        for call in (self.prepare, self.semantic, self.model, self.accept, self.receipt):
            call.assert_not_called()
        self.assertEqual(self._live(), before)

    def test_retryable_preparation_does_not_create_autoevo_output_directories(self) -> None:
        payload = self.spec.payload()
        payload["output_dir"] = "absent-output"
        spec = adapter.ModelSpec.from_payload(payload)
        before = self._live()
        with mock.patch.object(adapter, "resolve_model", return_value=spec), mock.patch.object(adapter.shutil, "which", return_value="/fixture/cli"):
            self.assertEqual(adapter.prepare_model("autoevo-nightly", root=self.root, environ=self.env), spec.payload())
        self.assertEqual(self._live(), before)
        self.assertFalse((self.vault / "absent-output").exists())

    def test_failed_timed_out_or_missing_candidate_never_publishes(self) -> None:
        before = self._live()
        for mode in ("failure", "timeout", "missing"):
            def stopped(argv, **kwargs):
                if mode != "missing":
                    self._candidate(argv, **kwargs)
                else:
                    (kwargs["cwd"] / "result.json").write_text(json.dumps({
                        "routine": "autoevo-nightly", "outcome": "delivered",
                        "output_file": "proposal.json", "summary": "false ack", "skipped_inputs": [],
                    }))
                return subprocess.CompletedProcess(argv, {"failure": 7, "timeout": 124, "missing": 0}[mode], "")

            self.model.side_effect = stopped
            with self.subTest(mode=mode), self.assertRaises(adapter.ExecutionError):
                self._execute()
            self.accept.assert_not_called()
            self.receipt.assert_not_called()
            self.assertEqual(self._live(), before)

    def test_parent_keeps_original_plan_despite_workspace_tampering(self) -> None:
        before = self._live()

        def tamper(argv, **kwargs):
            workspace = kwargs["cwd"]
            changed = json.loads((workspace / "plan.json").read_text())
            changed.update(base_head="forged", protected_paths=[], source_files={}, state_files={})
            (workspace / "plan.json").write_text(json.dumps(changed))
            (workspace / "sources/wip/note.md").write_text("model changed snapshot")
            state = workspace / "_meta/autoevo_pending.toml"
            state.parent.mkdir()
            state.write_text("model-authored state is not authority")
            return self._candidate(argv, **kwargs)

        self.model.side_effect = tamper
        self.assertEqual(self._execute()["status"], "complete")
        self.accept.assert_called_once()
        self.assertEqual(self.accept.call_args.args[2], self.original_plan)
        self.assertEqual(self._live(), before)

    def test_qmd_and_tmpdir_are_inside_the_sole_writable_cwd(self) -> None:
        self.assertEqual(self._execute()["status"], "complete")
        kwargs = self.model.call_args.kwargs
        env, cwd = kwargs["env"], kwargs["cwd"]
        self.assertEqual(Path(env["AUTOEVO_WORKSPACE"]), cwd)
        self.assertEqual(Path(env["TMPDIR"]), cwd / "tmp")
        self.assertEqual(Path(env["ATELIER_QMD_HOME"]), cwd / "qmd")
        self.assertEqual(Path(env[semantic.STAGED_MODEL_DIRECTORY_ENV]), self.models)
        self.assertFalse(self.models.is_relative_to(cwd))
        self.assertNotIn("SECRET", env)
        self.semantic.assert_called_once_with(self.vault, cwd / "qmd")
        self.assertFalse(cwd.exists(), "candidate workspace must be disposed after the attempt")

    def test_unavailable_qmd_never_starts_model_or_writes_live_state(self) -> None:
        before = self._live()
        self.semantic.side_effect = semantic.SearchError("fixture unavailable")
        with self.assertRaises(semantic.SearchError):
            self._execute()
        self.model.assert_not_called()
        self.accept.assert_not_called()
        self.assertEqual(self._live(), before)


class RealPrefectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(temporary_settings({PREFECT_SERVER_ANALYTICS_ENABLED: False}))

    def test_prefect_retries_only_explicitly_safe_process_tasks(self) -> None:
        safe = adapter.ProcessSpec(
            schedule=adapter.ScheduleSpec("dummy", "process", "public", ("0 0 * * *",), "UTC", 1, 1),
            argv=(sys.executable, "-c", "pass"),
            cwd=str(ROOT),
            environment={},
            timeout_seconds=30,
            retry_safe=True,
        )
        unsafe = adapter.ProcessSpec(
            schedule=adapter.ScheduleSpec("dummy", "process", "public", ("0 0 * * *",), "UTC", 3, 1),
            argv=safe.argv,
            cwd=safe.cwd,
            environment={},
            timeout_seconds=30,
            retry_safe=False,
        )
        attempts = 0

        def fail_once(*_args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise adapter.ExecutionError("synthetic transient")

        with prefect_test_harness(), mock.patch.object(adapter, "resolve_process", return_value=safe), mock.patch.object(
            adapter, "execute_process_job", side_effect=fail_once
        ):
            state = orchestration.process_routine_flow("dummy", "public", return_state=True)
            self.assertTrue(state.is_completed(), state)
            self.assertEqual(attempts, 2)

        attempts = 0
        with prefect_test_harness(), mock.patch.object(adapter, "resolve_process", return_value=unsafe), mock.patch.object(
            adapter, "execute_process_job", side_effect=fail_once
        ):
            state = orchestration.process_routine_flow("dummy", "public", return_state=True)
            self.assertTrue(state.is_failed(), state)
            self.assertEqual(attempts, 1)

    def test_status_reads_authoritative_flow_state_from_the_prefect_api(self) -> None:
        spec = adapter.ProcessSpec(
            schedule=adapter.ScheduleSpec("status-job", "process", "public", ("0 0 * * *",), "UTC"),
            argv=(sys.executable, "-c", "pass"),
            cwd=str(ROOT),
            environment={},
            timeout_seconds=30,
            retry_safe=False,
        )
        with prefect_test_harness(), mock.patch.object(adapter, "resolve_process", return_value=spec), mock.patch.object(
            adapter, "execute_process_job"
        ):
            state = orchestration.process_routine_flow("status-job", "public", return_state=True)
            self.assertTrue(state.is_completed(), state)
            with status.get_client(sync_client=True) as client:
                for job, delta in (("future-job", timedelta(days=1)), ("overdue-job", -timedelta(minutes=1))):
                    client.create_flow_run(
                        orchestration.process_routine_flow, parameters={"job": job, "source": "public"},
                        state=Scheduled(scheduled_time=datetime.now(timezone.utc) + delta),
                    )
            since = datetime.now(timezone.utc) - timedelta(minutes=5)
            latest = status.recent_runs(since, limit=1)
            self.assertEqual([run["routine"] for run in latest], ["status-job"])
            runs = status.recent_runs(since)
            self.assertEqual({run["routine"] for run in runs}, {"status-job", "overdue-job"})
        run = next(item for item in runs if item["routine"] == "status-job")
        self.assertEqual((run["state"], run["state_name"]), ("COMPLETED", "Completed"))


if __name__ == "__main__":
    unittest.main()
