#!/usr/bin/env python3
"""Opt-in native telemetry, advisory lifecycle hooks, and bounded local status."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import shutil
import sys
import time
import tarfile
import tempfile
import urllib.request
import uuid
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observability.collector import DARWIN_ARM64_SHA256, ENDPOINT, HEALTH, HOOK_EVENTS, VERSION, configuration, state_dir  # noqa: E402
from observability.usage import MAX_EVENT, number  # noqa: E402


def launch_options(runtime: str, env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """One process only; never edit a native user's global telemetry settings."""
    invocation = str(uuid.uuid4())
    updated = {key: value for key, value in env.items() if not key.startswith("OTEL_")}
    updated.update(ATELIER_OBSERVE="1", ATELIER_OBSERVATION_INVOCATION=invocation)
    if runtime == "codex":
        raise ValueError("Codex native OTLP schema verification is pending; scheduled JSONL is available")
    settings = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_SERVICE_NAME": "claude-code",
        "OTEL_METRICS_EXPORTER": "none", "OTEL_TRACES_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT, "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": ENDPOINT + "/v1/logs",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json", "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_HEADERS": "", "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "",
        "OTEL_RESOURCE_ATTRIBUTES": f"atelier.scope=interactive,atelier.invocation={invocation}",
        "OTEL_LOG_USER_PROMPTS": "0", "OTEL_LOG_ASSISTANT_RESPONSES": "0",
        "OTEL_LOG_TOOL_DETAILS": "0", "OTEL_LOG_TOOL_CONTENT": "0", "OTEL_LOG_RAW_API_BODIES": "",
        "OTEL_LOG_RAW_API_BODIES_DIR": "", "CLAUDE_CODE_ENABLE_BETA_TRACING": "0",
    }
    updated.update(settings)
    return ["--settings", json.dumps({"env": settings})], updated


def hook(runtime: str) -> None:
    if os.environ.get("ATELIER_OBSERVE") != "1" or runtime not in {"codex", "claude"}:
        return
    try:
        raw = sys.stdin.buffer.read(MAX_EVENT + 1)
        if len(raw) > MAX_EVENT:
            return
        payload = json.loads(raw)
        event = payload.get("hook_event_name")
        if event not in (*HOOK_EVENTS, "Interrupt", "StopFailure"):
            return
        attrs = {"event.name": event}
        for key, source in {"session.id": "session_id", "prompt.id": "turn_id" if runtime == "codex" else "prompt_id",
                            "agent_id": "agent_id"}.items():
            value = payload.get(source)
            if isinstance(value, str) and 0 < len(value) <= 256:
                attrs[key] = value
        send({"resourceLogs": [{"resource": {"attributes": attributes({
            "service.name": f"atelier-hook-{runtime}",
            "atelier.invocation": os.environ.get("ATELIER_OBSERVATION_INVOCATION", ""),
        })}, "scopeLogs": [{"logRecords": [{"timeUnixNano": str(time.time_ns()),
                                             "attributes": attributes(attrs)}]}]}]})
    except Exception:
        pass  # No stdout, hook decisions, retries, or model-visible errors.


def attributes(values: dict) -> list[dict]:
    return [{"key": key, "value": {"stringValue": str(value)}} for key, value in values.items()]


def send(payload: dict) -> None:
    request = urllib.request.Request(ENDPOINT + "/v1/logs", json.dumps(payload).encode(),
                                     {"Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=0.3):
        pass


def summary(*, directory: Path | None = None, hours: int = 24) -> dict:
    directory = directory or state_dir()
    result = {"scope": "opt-in-cli-only", "coverage": "unknown", "collector": "unavailable",
              "desktop": "unverified", "sessions": [], "read_gaps": 0}
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(HEALTH, timeout=0.3) as response:
            result["collector"] = "reachable" if response.status == 200 else "unavailable"
    except OSError:
        pass
    sessions, seen = {}, set()
    cutoff = (time.time() - hours * 3600) * 1e9
    remaining = 32 * 1024 * 1024
    files = []
    for path in directory.glob("events*.jsonl"):
        try:
            info = path.stat()
            files.append((info.st_mtime, path, info.st_size))
        except OSError:
            result["read_gaps"] += 1
    result["read_gaps"] += max(0, len(files) - 4)
    for _, path, size in sorted(files, reverse=True)[:4]:
        if path.is_symlink() or size > remaining:
            result["read_gaps"] += 1
            continue
        try:
            with path.open("rb") as handle:
                while remaining > 0 and (line := handle.readline(min(MAX_EVENT + 1, remaining))):
                    remaining -= len(line)
                    if len(line) > MAX_EVENT:
                        result["read_gaps"] += 1
                        break
                    value = json.loads(line)
                    for record in value if isinstance(value, list) else [value]:
                        if record.get("version") != 1 or record.get("time_unix_nano", 0) < cutoff:
                            continue
                        runtime, session = record.get("runtime"), record.get("session")
                        if runtime not in {"codex", "claude"} or not session:
                            result["read_gaps"] += 1
                            continue
                        key = (runtime, session)
                        row = sessions.setdefault(key, {"runtime": runtime, "session": session, "coverage": "partial",
                                  "requests": 0, "api_errors": 0, "exhausted_requests": 0, "hooks": 0,
                                  "tools": 0, "models": [], "request_duration_ms": None, "usage": {},
                                  "measured_requests": {}, "cost_usd_estimate": None})
                        # Native request IDs or session sequence deduplicate delivery.
                        event = record.get("event", "")
                        event_id = record.get("tool_call") if event == "tool_result" else None
                        if event == "api_request":
                            event_id = record.get("request") or record.get("client_request")
                        identity = (key, event, event_id or (record.get("event.sequence"), record.get("time_unix_nano")))
                        if identity in seen:
                            continue
                        seen.add(identity)
                        row["hooks"] += event.startswith("hook.")
                        row["api_errors"] += event == "api_error"
                        row["exhausted_requests"] += event == "api_retries_exhausted"
                        row["tools"] += event == "tool_result"
                        if event != "api_request":
                            continue
                        row["requests"] += 1
                        if record.get("model") and record["model"] not in row["models"]:
                            row["models"].append(record["model"])
                        if number(record.get("duration_ms")):
                            row["request_duration_ms"] = (row["request_duration_ms"] or 0) + record["duration_ms"]
                        for metric in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
                            if number(record.get(metric)):
                                row["usage"][metric] = row["usage"].get(metric, 0) + record[metric]
                                row["measured_requests"][metric] = row["measured_requests"].get(metric, 0) + 1
                        if number(record.get("cost_usd")):
                            row["cost_usd_estimate"] = (row["cost_usd_estimate"] or 0) + record["cost_usd"]
                            row["measured_requests"]["cost"] = row["measured_requests"].get("cost", 0) + 1
        except (OSError, ValueError, TypeError, AttributeError):
            result["read_gaps"] += 1
    result["sessions"] = list(sessions.values())
    if sessions:
        result["coverage"] = "partial"  # Delivery/lifecycle events cannot prove lossless accounting.
    return result


def serve() -> int:
    directory = state_dir()
    os.umask(0o077)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = directory / "otelcol-contrib"
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=5)
    if version.returncode or version.stdout.strip() != f"otelcol-contrib version {VERSION}":
        raise ValueError("Collector version does not match the pinned configuration")
    config = json.dumps(configuration(directory))
    os.execv(str(binary), [str(binary), "--config", "yaml:" + config])
    return 0


def install(archive: Path) -> None:
    """Install the checked publisher archive; loading launchd is a separate step."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ValueError("This pinned launchd installation targets Apple Silicon macOS")
    with archive.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != DARWIN_ARM64_SHA256:
            raise ValueError("Collector archive checksum mismatch")
    directory = state_dir()
    root = Path(__file__).resolve().parents[2]
    target = Path.home() / "Library/LaunchAgents/com.atelier.observability.plist"
    plist = (root / "scripts/launchd/com.atelier.observability.plist").read_text().replace("__ATELIER_ROOT__", escape(str(root)))
    if target.is_symlink() or (target.exists() and target.read_text() != plist):
        raise ValueError("Refusing to replace an existing different launch agent")
    if directory.is_symlink():
        raise ValueError("Observability state directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    binary = directory / "otelcol-contrib"
    with tarfile.open(archive) as bundle, bundle.extractfile("otelcol-contrib") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        if binary.is_symlink():
            raise ValueError("Collector binary must not be a symlink")
        if binary.exists():
            with binary.open("rb") as existing:
                if hashlib.file_digest(existing, "sha256").hexdigest() != digest:
                    raise ValueError("Refusing to replace a different installed Collector")
        else:
            source.seek(0)
            with tempfile.TemporaryDirectory(prefix=".install-", dir=directory) as temporary:
                candidate = Path(temporary) / binary.name
                with candidate.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
                candidate.chmod(0o700)
                os.link(candidate, binary)  # Atomic publication without replacing a concurrent install.
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix=".install-", dir=target.parent) as temporary:
            candidate = Path(temporary) / target.name
            candidate.write_text(plist)
            candidate.chmod(0o600)
            os.link(candidate, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("hook", "serve", "install"))
    parser.add_argument("runtime", nargs="?", choices=("codex", "claude"))
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.mode == "hook":
        hook(args.runtime)
    elif args.mode == "serve":
        raise SystemExit(serve())
    elif args.archive is None:
        parser.error("install requires --archive")
    else:
        install(args.archive)
