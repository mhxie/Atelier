"""Bounded Codex JSONL accounting; never retain native message/tool payloads."""

from __future__ import annotations

import hashlib
import json
import math
import os
import select
import time
from dataclasses import dataclass, field
from typing import BinaryIO

PREFIX = "atelier.observation "
MAX_EVENT = 256 * 1024
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def number(value: object) -> bool:
    return type(value) in (int, float) and 0 <= value <= 1e15 and math.isfinite(value)


def identifier(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        return None
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class Usage:
    started: float = field(default_factory=time.monotonic)
    session: str | None = None
    turns: int = 0
    failures: int = 0
    tools: int = 0
    gaps: int = 0
    active: bool = False
    totals: dict = field(default_factory=dict)
    samples: dict = field(default_factory=dict)
    item_ids: set = field(default_factory=set)

    def drain(self, stream: BinaryIO, stopping) -> None:
        """Pipe reader can stop even when a surviving descendant holds stdout."""
        pending = bytearray()
        discarding = False
        stop_at = None
        try:
            while True:
                if stopping.is_set():
                    stop_at = stop_at or time.monotonic() + 0.5
                    if time.monotonic() > stop_at:
                        self.gaps += 1
                        break
                if not select.select([stream], [], [], 0.1)[0]:
                    if stopping.is_set():
                        self.gaps += 1
                        break
                    continue
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    if pending:
                        self.gaps += 1
                    break
                for index, part in enumerate(chunk.split(b"\n")):
                    if index:
                        if not discarding:
                            try:
                                self.event(json.loads(pending))
                            except Exception:
                                self.gaps += 1
                        pending.clear()
                        discarding = False
                    if not discarding:
                        pending.extend(part)
                        if len(pending) > MAX_EVENT:
                            pending.clear()
                            discarding = True
                            self.gaps += 1
        except OSError:
            self.gaps += 1
        finally:
            stream.close()

    def event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "thread.started":
            session = identifier(event.get("thread_id"))
            if self.session and self.session != session:
                self.gaps += 1
            self.session = session
        elif kind == "turn.started":
            self.gaps += self.active
            self.active = True
        elif kind in {"turn.completed", "turn.failed"}:
            # JSONL has no required turn ID. Only accept one terminal event per
            # observed start; ambiguous/replayed terminals are gaps, not spend.
            if not self.active:
                self.gaps += 1
                return
            self.active = False
            if kind == "turn.failed":
                self.failures += 1
                self.gaps += 1
                return
            self.turns += 1
            usage = event.get("usage", {})
            if not isinstance(usage, dict):
                self.gaps += 1
                return
            for key in TOKEN_KEYS:
                value = usage.get(key)
                if type(value) is int and number(value):
                    self.totals[key] = self.totals.get(key, 0) + value
                    self.samples[key] = self.samples.get(key, 0) + 1
                elif key in {"input_tokens", "output_tokens"}:
                    self.gaps += 1
        elif kind == "item.completed":
            item = event.get("item", {})
            if item.get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
                identity = identifier(item.get("id"))
                if not identity or len(self.item_ids) >= 4096:
                    self.gaps += 1
                elif identity not in self.item_ids:
                    self.item_ids.add(identity)
                    self.tools += 1
        elif kind == "error":
            self.gaps += 1
        elif kind not in {"item.started", "item.updated"}:
            self.gaps += 1

    def record(self, returncode: int | None) -> dict:
        complete = bool(self.turns and self.session) and returncode == 0 and not (self.gaps or self.active)
        return {
            "version": 1, "source": "codex_jsonl", "session": self.session,
            "coverage": "complete" if complete else "partial" if self.turns else "unknown",
            "duration_seconds": round(time.monotonic() - self.started, 3),
            "exit_code": returncode, "turns": self.turns, "failed_turns": self.failures,
            "tool_completions": self.tools, "gaps": self.gaps + int(self.active),
            "usage": {key: self.totals.get(key) for key in TOKEN_KEYS},
            "measured_turns": {key: self.samples.get(key, 0) for key in TOKEN_KEYS},
            "cost_usd_estimate": None,
        }


def emit(usage: Usage, returncode: int | None, flow_run_id: str) -> None:
    """Observations are advisory. A log sink failure must not retry a model."""
    try:
        print(PREFIX + json.dumps({**usage.record(returncode), "flow_run_id": flow_run_id}), flush=True)
    except Exception:
        pass


def from_log(message: str, flow_run_id: str) -> dict | None:
    """Read only our bounded, versioned record from the owning Prefect run."""
    if not message.startswith(PREFIX) or len(message) > 8192:
        return None
    try:
        record = json.loads(message[len(PREFIX):])
        if record.get("version") == 1 and record.get("flow_run_id") == flow_run_id:
            return record
    except (ValueError, AttributeError):
        pass
    return None
