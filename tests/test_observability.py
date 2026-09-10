"""Small boundary suite; receipt/retry cases reuse test_routine_prefect."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import routine_adapter as adapter  # noqa: E402
from observability import collector, native, usage  # noqa: E402


class BoundaryTests(unittest.TestCase):
    def test_turn_accounting_and_unknowns(self):
        terminal = {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3}}
        for suffix, total, coverage in (([], 10, "complete"), ([terminal], 10, "partial"),
                                       ([{"type": "turn.started"}, terminal], 20, "complete"),
                                       ([{"type": "turn.started"}, {"type": "turn.failed"}], 10, "partial")):
            with self.subTest(suffix=suffix):
                meter = usage.Usage()
                for event in [{"type": "thread.started", "thread_id": "private/path"}, {"type": "turn.started"}, terminal, *suffix]:
                    meter.event(event)
                record = meter.record(0)
                self.assertEqual((record["usage"]["input_tokens"], record["coverage"]), (total, coverage))
                self.assertIsNone(record["usage"]["reasoning_output_tokens"])
                self.assertNotIn("private/path", json.dumps(record))
                self.assertNotEqual(meter.record(None)["coverage"], "complete")
        self.assertIsNone(usage.Usage().record(0)["usage"]["input_tokens"])
        item = {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution"}}
        meter.event(item)
        meter.event(item)
        self.assertEqual(meter.tools, 1)
        self.assertFalse(usage.number(10**400))

    def test_stream_is_drained_without_retaining_payloads(self):
        meter = usage.Usage()
        program = "import json,sys; print('PRIVATE_CANARY'*50000); print('malformed'); print(json.dumps({'type':'turn.started'})); print(json.dumps({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':1}})); print('PRIVATE_CANARY',file=sys.stderr)"
        result = adapter.execute_process([sys.executable, "-c", program], seconds=5, usage=meter)
        self.assertEqual((result.returncode, result.stdout, meter.totals["input_tokens"]), (0, "", 2))
        self.assertEqual(meter.gaps, 2)

    def test_timeout_cancellation_and_sink_failure_are_advisory(self):
        for failure in (None, KeyboardInterrupt):
            with self.subTest(failure=failure), redirect_stdout(io.StringIO()) as logged:
                if failure:
                    with mock.patch.object(adapter, "wait_until_deadline", side_effect=failure):
                        with self.assertRaises(KeyboardInterrupt):
                            adapter.execute_observed_model([sys.executable, "-c", "import time; time.sleep(3)"], seconds=.1, flow_run_id="flow")
                else:
                    result = adapter.execute_observed_model([sys.executable, "-c", "import time; time.sleep(3)"], seconds=.1, flow_run_id="flow")
                    self.assertEqual(result.returncode, 124)
                record = usage.from_log(logged.getvalue().strip(), "flow")
                self.assertEqual(record["coverage"], "unknown")
                self.assertIsNone(usage.from_log(logged.getvalue(), "another-flow"))
        with mock.patch("builtins.print", side_effect=OSError("sink unavailable")):
            usage.emit(usage.Usage(), 0, "flow")

    def test_opt_in_is_process_local_and_hooks_are_silent(self):
        original = {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://not-local.invalid", "KEEP": "yes"}
        argv, env = native.launch_options("claude", original)
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], collector.ENDPOINT)
        self.assertEqual(json.loads(argv[1])["env"]["OTEL_LOG_TOOL_CONTENT"], "0")
        self.assertEqual(original["OTEL_EXPORTER_OTLP_ENDPOINT"], "https://not-local.invalid")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(native, "send") as send:
            native.hook("claude")
            send.assert_not_called()
        payload = {"hook_event_name": "Stop", "session_id": "fixture", "prompt": "PRIVATE_CANARY"}
        with mock.patch.dict(os.environ, {"ATELIER_OBSERVE": "1"}), mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))), mock.patch.object(native, "send", side_effect=OSError) as send, redirect_stdout(io.StringIO()) as out:
            native.hook("claude")
            self.assertEqual(out.getvalue(), "")
            self.assertNotIn("PRIVATE_CANARY", json.dumps(send.call_args.args[0]))

    def test_reader_start_failure_does_not_prevent_the_process(self):
        meter = usage.Usage()
        with mock.patch.object(adapter.threading.Thread, "start", side_effect=RuntimeError("unavailable")):
            result = adapter.execute_process([sys.executable, "-c", "print('PRIVATE_CANARY')"], seconds=5, usage=meter)
        self.assertEqual((result.returncode, result.stdout, meter.gaps), (0, "", 1))

    def test_installer_publishes_complete_files_and_escapes_paths(self):
        with tempfile.TemporaryDirectory(prefix="atelier-install-test-") as temporary:
            root, home = Path(temporary) / "Atelier & Notes", Path(temporary) / "home"
            template = root / "scripts/launchd/com.atelier.observability.plist"
            template.parent.mkdir(parents=True)
            template.write_bytes((ROOT / "scripts/launchd" / template.name).read_bytes())
            archive, content = Path(temporary) / "collector.tar.gz", b"fixture binary"
            with native.tarfile.open(archive, "w:gz") as bundle:
                member = native.tarfile.TarInfo("otelcol-contrib")
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))
            state = home / "state"
            binary, target = state / "otelcol-contrib", home / "Library/LaunchAgents" / template.name
            write_text = Path.write_text

            def failed_copy(source, destination):
                destination.write(b"partial")
                raise OSError("disk full")

            def failed_plist(path, text, *args, **kwargs):
                write_text(path, text[:20], *args, **kwargs)
                raise OSError("disk full")

            with (mock.patch.object(native.sys, "platform", "darwin"),
                  mock.patch.object(native.platform, "machine", return_value="arm64"),
                  mock.patch.object(native, "__file__", str(root / "scripts/observability/native.py")),
                  mock.patch.object(native, "state_dir", return_value=state),
                  mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(native, "DARWIN_ARM64_SHA256", native.hashlib.sha256(archive.read_bytes()).hexdigest())):
                with mock.patch.object(native.shutil, "copyfileobj", side_effect=failed_copy), self.assertRaises(OSError):
                    native.install(archive)
                self.assertEqual(list(state.iterdir()), [])
                with mock.patch.object(Path, "write_text", failed_plist), self.assertRaises(OSError):
                    native.install(archive)
                self.assertEqual(binary.read_bytes(), content)
                self.assertEqual(list(target.parent.iterdir()), [])
                native.install(archive)
                native.install(archive)
                self.assertEqual(plistlib.loads(target.read_bytes())["ProgramArguments"][0], str(root.resolve() / ".venv/bin/python"))
                self.assertEqual((binary.stat().st_mode & 0o777, target.stat().st_mode & 0o777), (0o700, 0o600))
                binary.write_bytes(b"different binary")
                with self.assertRaisesRegex(ValueError, "different installed Collector"):
                    native.install(archive)


@unittest.skipUnless(os.environ.get("ATELIER_TEST_COLLECTOR"), "set ATELIER_TEST_COLLECTOR to the pinned binary for transport/rotation QA")
class CollectorTests(unittest.TestCase):
    def test_privacy_deduplication_restart_and_rotation(self):
        def port():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return sock.getsockname()[1]

        with tempfile.TemporaryDirectory(prefix="atelier-observation-test-") as temporary:
            directory = Path(temporary)
            listen, health = port(), port()
            config = collector.configuration(directory, port=listen, health_port=health)
            config["exporters"]["file"]["rotation"].update(max_megabytes=1, max_backups=1)
            binary = os.environ["ATELIER_TEST_COLLECTOR"]
            command = [binary, "--config", "yaml:" + json.dumps(config)]
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def start():
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.addCleanup(lambda: process.poll() is None and process.kill())
                for _ in range(100):
                    try:
                        with opener.open(f"http://127.0.0.1:{health}", timeout=.1):
                            return process
                    except OSError:
                        if process.poll() is not None:
                            self.fail(process.communicate()[1].decode()[-1000:])
                        time.sleep(.03)
                self.fail("collector did not become ready")

            def post(records, *, scope="interactive"):
                attrs = native.attributes({"service.name": "claude-code", "atelier.scope": scope, "user.email": "PRIVATE_CANARY"})
                payload = {"resourceLogs": [{"schemaUrl": "PRIVATE_CANARY", "resource": {"attributes": attrs},
                           "scopeLogs": [{"schemaUrl": "PRIVATE_CANARY", "scope": {"name": "PRIVATE_CANARY"}, "logRecords": records}]}]}
                request = urllib.request.Request(f"http://127.0.0.1:{listen}/v1/logs", json.dumps(payload).encode(), {"Content-Type": "application/json"})
                with opener.open(request, timeout=3):
                    pass

            record = {"timeUnixNano": str(time.time_ns()), "body": {"stringValue": "PRIVATE_CANARY"},
                      "eventName": "PRIVATE_CANARY", "severityText": "PRIVATE_CANARY", "attributes": native.attributes({
                          "event.name": "api_request", "session.id": "fixture", "request_id": "request",
                          "input_tokens": "10", "output_tokens": 2, "cache_read_tokens": 4,
                          "cost_usd": "0.01", "duration_ms": 20, "tool_parameters": "PRIVATE_CANARY"})}
            process = start()
            post([record, record])
            post([{**record, "attributes": native.attributes({"event.name": "tool_result", "session.id": "fixture",
                  "request_id": "shared-request", "tool_use_id": tool})} for tool in ("one", "two")])
            post([record], scope="not-opted-in")
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            data = (directory / "events.jsonl").read_bytes()
            self.assertNotIn(b"PRIVATE_CANARY", data + stdout + stderr)
            rows = [json.loads(line) for line in data.splitlines()]
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["input_tokens"], 10)
            result = native.summary(directory=directory)
            self.assertEqual(result["sessions"][0]["requests"], 1)
            self.assertEqual(result["sessions"][0]["tools"], 2)
            self.assertEqual(result["sessions"][0]["cost_usd_estimate"], .01)
            self.assertEqual(result["sessions"][0]["measured_requests"]["cost"], 1)
            self.assertEqual(result["sessions"][0]["usage"]["cache_read_tokens"], 4)
            process = start()
            post([record])
            self.assertTrue((directory / "events.jsonl").read_bytes().startswith(data))
            for _ in range(18):
                post([record] * 600)
            process.terminate()
            process.communicate(timeout=5)
            files = list(directory.glob("events*.jsonl"))
            self.assertEqual(len(files), 2)
            self.assertLessEqual(sum(path.stat().st_size for path in files), 2 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
