#!/usr/bin/env python3
"""
shadow.py: cross-provider shadow-log correlation + reporting.

Companion to `scripts/chat_completion.py`. Mechanism:

  - A multi-leg call site (e.g., /system-review Step 1c, an escalated /decision,
    scripts/review.sh) opens a shadow group via `shadow.py group-start`,
    which writes a witness file under `~/.cache/atelier/shadow_groups/`
    and emits a UUID + env-export line to stdout. The site MUST also call
    `shadow.py group-close` after all legs finish so the witness is removed
    before the 30-minute recency window expires. Shell-owned sites may use an
    EXIT trap; runtime workflows use an explicit later call because each shell
    tool invocation is isolated.
  - Direct-API legs auto-log via chat_completion.py reading env vars
    (ATELIER_SHADOW_GROUP, ATELIER_TASK_TYPE).
  - Native project-agent legs are logged in-band via
    `shadow.py log --leg native`. `shadow.py native-model` resolves the
    correct runtime identity so Codex results are not mislabeled with a
    Claude model identity. `log-from-hook` remains available for replay and
    test fixtures.
  - Codex legs are still logged in-band via `shadow.py log --leg codex`
    (no PostToolUse(Bash) hook today).
  - `shadow.py report` aggregates all logs, deduplicates, groups by UUID,
    extracts verdict tokens per task type, computes cost retroactively
    from the current `harness/model_costs.toml`, and emits per-task-type
    cost / latency / verdict-agreement comparisons.

Subcommands:
    group-start --task <name> --expected '[{"model":"X","leg":"Y","subagent_type":"Z"}, ...]'
        Open a shadow group; print UUID and shell-eval-able env exports.
        Optional `subagent_type` in each expected_dispatches entry
        disambiguates when multiple agents share a native model identity.

    group-close --group <uuid> [--mark-closed]
        Close a witness: delete by default, or write `closed_at` field
        with --mark-closed. Call from the multi-leg site's completion path.

    log --group <uuid> --task <name> --model <id> --leg <native|direct|codex>
        --prompt-file <path> --response-file <path> [--prompt-stdin]
        [--response-stdin]
        Append a synthetic native/codex leg entry. Production native and
        Codex logging is in-band through this command.

    native-model --agent <role> [--runtime auto|claude|codex]
        Resolve the model identity used to correlate a native project-agent
        leg in the active runtime.

    log-from-hook
        Optional PostToolUse(Agent) replay entry. Not wired in production;
        retained for fixtures and forensic replay.

    report [--since YYYY-MM-DD] [--task-type X] [--accept-stale-costs] [--json]
        Aggregate logs and emit cost/verdict-agreement comparison.

See `protocols/backend-taxonomy.md` for the SOT/role/failure-mode contract,
`protocols/voice-dispatch.md` for when multi-leg call sites
use this, and `protocols/shadow-log.md` § Mechanism for the hook flow.
Stdlib-only by design.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
COSTS_TOML = REPO_ROOT / "harness" / "model_costs.toml"
SHADOW_TASKS_TOML = REPO_ROOT / "harness" / "shadow_tasks.toml"
RUNTIMES_TOML = REPO_ROOT / "harness" / "runtimes.toml"
DEFAULT_LOG_DIR = Path.home() / ".cache" / "atelier" / "llm_calls"
DEFAULT_WITNESS_DIR = Path.home() / ".cache" / "atelier" / "shadow_groups"

SCOPE_BANNER = (
    "SCOPE: shadow logs cover multi-leg verification workloads (~10-20% of LLM spend).\n"
    "       Single-leg generative routing (Researcher, Synthesizer, Reader, Scout, Curator)\n"
    "       is not instrumented. For routing questions on those, run manual A/B\n"
    "       (see protocols/shadow-log.md § M2 for the 30-minute procedure)."
)


# ---------- group-start ----------


def _agent_direct_model(subagent_type: str) -> str:
    """`agents.<subagent_type>.voices.direct` from harness/agents.toml, or ""."""
    try:
        data = tomllib.loads((REPO_ROOT / "harness" / "agents.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    agent = data.get("agents", {}).get(subagent_type)
    voices = agent.get("voices") if isinstance(agent, dict) else None
    value = voices.get("direct") if isinstance(voices, dict) else None
    return str(value) if value else ""


def _expected_for_agent(subagent_type: str, runtime: str = "") -> list[dict]:
    """Build the expected-dispatch list for a dual-voice agent.

    Native leg from the runtime-aware identity, direct leg from the agent's
    canonical voices binding, so call sites track harness/agents.toml
    automatically instead of re-deriving both identities in inline bash.
    """
    expected: list[dict] = []
    native = _runtime_native_model(subagent_type, _active_runtime(runtime))
    if native:
        expected.append({"model": native, "leg": "native", "subagent_type": subagent_type})
    direct = _agent_direct_model(subagent_type)
    if direct:
        expected.append({"model": direct, "leg": "direct"})
    return expected


def cmd_group_start(args: argparse.Namespace) -> int:
    if bool(args.agent) == bool(args.expected):
        sys.stderr.write("shadow: pass exactly one of --agent or --expected\n")
        return 2
    if args.agent:
        expected = _expected_for_agent(args.agent, args.runtime)
        if not expected:
            sys.stderr.write(
                f"shadow: no voice bindings found for agent={args.agent!r}\n"
            )
            return 2
    else:
        try:
            expected = json.loads(args.expected)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"shadow: --expected is not valid JSON: {e}\n")
            return 2
    if not isinstance(expected, list) or not all(
        isinstance(e, dict) and "model" in e and "leg" in e for e in expected
    ):
        sys.stderr.write(
            "shadow: --expected must be a JSON array of {\"model\": ..., \"leg\": ...} objects\n"
        )
        return 2
    group_id = str(uuid.uuid4())
    DEFAULT_WITNESS_DIR.mkdir(parents=True, exist_ok=True)
    witness_path = DEFAULT_WITNESS_DIR / f"{group_id}.json"
    witness_path.write_text(json.dumps({
        "group_id": group_id,
        "task_type": args.task,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "expected_dispatches": expected,
    }, indent=2), encoding="utf-8")
    # Emit shell-eval-able exports so the caller can: eval "$(shadow.py group-start ...)"
    print(f'export ATELIER_SHADOW_GROUP="{group_id}"')
    print(f'export ATELIER_TASK_TYPE="{args.task}"')
    direct_models = [e["model"] for e in expected if e.get("leg") == "direct"]
    if direct_models:
        print(f'export ATELIER_DIRECT_MODEL="{direct_models[0]}"')
    sys.stderr.write(f"shadow: opened group {group_id} (task={args.task}, expected_legs={len(expected)})\n")
    return 0


# ---------- gc (lifecycle cleanup) ----------


DEFAULT_LLM_CALLS_RETENTION_DAYS = 90
# Single source of truth — `WITNESS_OPEN_WINDOW` (used by the hook's open-witness
# scan) is derived from this constant below, so changing one updates both.
DEFAULT_WITNESS_STALENESS_MINUTES = 30


def cmd_gc(args: argparse.Namespace) -> int:
    """Garbage-collect stale shadow telemetry. Designed for Claude's
    `SessionEnd` and Codex's `Stop` hooks, but also runnable manually.

    Two cleanup passes:

      1. **Orphaned witnesses** (`~/.cache/atelier/shadow_groups/*.json`):
         delete files older than `--witness-min` minutes (default 30, matching
         the PostToolUse hook's recency window). Defends against witnesses
         left behind when completion cleanup was bypassed (session crash,
         manual kill, or a workflow abort before its `group-close` call).

      2. **Aged primary log** (`~/.cache/atelier/llm_calls/*.jsonl`): delete
         files older than `--retention-days` days (default 90). The `$OV`
         mirror skeleton (`$OV/_meta/shadow_logs/`) is NOT touched — that's
         the durable record; only the local full-payload cache is rotated.

    Silent best-effort: every failure path returns 0. Prints a one-line
    summary to stderr (visible in `claude --debug`; muted by default).
    """
    # Negative thresholds are nonsensical AND dangerous. A `--retention-days -1`
    # cutoff = now - (-1d) = tomorrow, so every file qualifies as "stale". A
    # naive clamp to 0 makes it worse (cutoff = now → everything stale). The
    # only safe response is to refuse to act: print to stderr and exit
    # without touching the filesystem.
    if args.retention_days < 0 or args.witness_min < 0:
        sys.stderr.write(
            f"shadow.py gc: refusing to run with negative threshold "
            f"(witness-min={args.witness_min}, retention-days={args.retention_days}); "
            f"thresholds must be ≥ 0. No files deleted.\n"
        )
        return 0
    witness_min = args.witness_min
    retention_days = args.retention_days
    # Defend against retention_days == 0 OR witness_min == 0 — both would
    # produce a cutoff equal to "now", making every file appear stale and
    # wiping the cache. Lifecycle hooks fire `gc` with default args,
    # so a config typo like `--retention-days 0` would wipe everything.
    if retention_days == 0 or witness_min == 0:
        sys.stderr.write(
            f"shadow.py gc: refusing to run with zero threshold "
            f"(witness-min={witness_min}, retention-days={retention_days}); "
            f"thresholds must be strictly positive. No files deleted.\n"
        )
        return 0
    cutoff_witness = datetime.now() - timedelta(minutes=witness_min)
    witnesses_removed = 0
    if DEFAULT_WITNESS_DIR.is_dir():
        for p in DEFAULT_WITNESS_DIR.glob("*.json"):
            try:
                # Two delete predicates: (a) mtime older than the witness
                # window — catches orphans from crashed sessions; (b) the
                # witness has a `closed_at` marker — catches `--mark-closed`
                # witnesses whose mtime was refreshed by the close write
                # and would otherwise leak forever on disk.
                if datetime.fromtimestamp(p.stat().st_mtime) < cutoff_witness:
                    p.unlink()
                    witnesses_removed += 1
                    continue
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("closed_at"):
                        p.unlink()
                        witnesses_removed += 1
                except (OSError, json.JSONDecodeError):
                    pass
            except OSError:
                continue
    cutoff_log = datetime.now() - timedelta(days=retention_days)
    logs_removed = 0
    if DEFAULT_LOG_DIR.is_dir():
        for p in DEFAULT_LOG_DIR.glob("*.jsonl"):
            try:
                if datetime.fromtimestamp(p.stat().st_mtime) < cutoff_log:
                    p.unlink()
                    logs_removed += 1
            except OSError:
                continue
    if witnesses_removed or logs_removed:
        sys.stderr.write(
            f"shadow.py gc: removed {witnesses_removed} orphaned witness(es), "
            f"{logs_removed} aged log file(s)\n"
        )
    return 0


# ---------- group-close (witness lifecycle) ----------


def cmd_group_close(args: argparse.Namespace) -> int:
    """Close a shadow group's witness file.

    Multi-leg call sites MUST invoke this from their completion path. A
    persistent shell may use an EXIT trap; a runtime workflow with isolated
    shell calls invokes it explicitly after all legs return. Two equivalent
    completion modes: write `closed_at` (preserved for forensics) OR delete
    the witness file (cleanest; reclaims disk). Default is to delete; pass
    `--mark-closed` to write the field instead.

    Silent best-effort: exit 0 on any failure. Missing witness, permissions
    error, malformed JSON: all degrade silently so cleanup never breaks the
    enclosing workflow.
    """
    group_id = args.group.strip() if args.group else ""
    if not group_id:
        return 0
    path = DEFAULT_WITNESS_DIR / f"{group_id}.json"
    if not path.exists():
        return 0
    try:
        if args.mark_closed:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["closed_at"] = datetime.now().isoformat(timespec="seconds")
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        else:
            path.unlink()
    except (OSError, json.JSONDecodeError):
        return 0
    return 0


# ---------- log (native / codex synthetic shim) ----------


def _read_payload(path: str | None, stdin_flag: bool, text: str | None, field: str) -> str:
    if text is not None:
        return text
    if stdin_flag:
        return sys.stdin.read()
    if path:
        return Path(path).read_text(encoding="utf-8")
    sys.stderr.write(f"shadow: provide --{field}-text, --{field}-file, or --{field}-stdin\n")
    raise SystemExit(2)


def cmd_log(args: argparse.Namespace) -> int:
    prompt = _read_payload(args.prompt_file, args.prompt_stdin,
                           getattr(args, "prompt_text", None), "prompt")
    response = _read_payload(args.response_file, args.response_stdin,
                             getattr(args, "response_text", None), "response")
    # Char approx: every 4 chars ≈ 1 token. Honest fallback; report annotates.
    input_tokens_approx = max(1, len(prompt) // 4)
    output_tokens_approx = max(1, len(response) // 4)
    event = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "shadow_group_id": args.group,
        "task_type": args.task,
        "task_dispatch_kind": args.leg,
        "model": args.model,
        "api_model": None,
        "endpoint": None,
        "session": None,
        "system": None,
        "user_prompt": prompt,
        "status": "ok",
        "response_content": response,
        "reasoning_content": None,
        "finish_reason": "stop",
        "usage": None,
        "usage_estimate": {
            "input_tokens": input_tokens_approx,
            "output_tokens": output_tokens_approx,
            "method": "char_approx",
        },
        "cost_estimate_method": "char_approx",
        "latency_s": None,
        "logged_by": "orchestrator_in_band",
    }
    log_dir = Path(args.log_dir) if args.log_dir else DEFAULT_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    # Mirror skeleton to $OV/_meta/shadow_logs/ when OV is set.
    _mirror_skeleton(event)
    return 0


def _mirror_skeleton(event: dict[str, Any]) -> None:
    ov = os.environ.get("OV")
    if not ov:
        return
    skeleton_fields = (
        "timestamp", "shadow_group_id", "task_type", "task_dispatch_kind",
        "model", "api_model", "usage", "usage_estimate",
        "cost_estimate_method", "latency_s", "finish_reason", "status",
    )
    try:
        sk: dict[str, Any] = {k: event.get(k) for k in skeleton_fields}
        resp = event.get("response_content")
        if isinstance(resp, str):
            sk["response_first_200"] = resp[:200]
            sk["response_sha256"] = hashlib.sha256(resp.encode("utf-8")).hexdigest()
        mirror_dir = Path(ov) / "_meta" / "shadow_logs"
        mirror_dir.mkdir(parents=True, exist_ok=True)
        mirror_file = mirror_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with mirror_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(sk, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError):
        pass


# ---------- log-from-hook (PostToolUse Agent matcher; out-of-band) ----------


WITNESS_OPEN_WINDOW = timedelta(minutes=DEFAULT_WITNESS_STALENESS_MINUTES)


def _find_open_witness() -> dict[str, Any] | None:
    """Most-recently-started witness within `WITNESS_OPEN_WINDOW`, or None.

    Witness selection is timestamp-ordered, not env-var-driven: env vars
    don't propagate into Claude Code hook subshells, so the hook locates
    its parent shadow group via the on-disk witness file's `started_at`
    field. The 30-minute window bounds staleness so a forgotten witness
    from a prior session can't silently match a fresh Agent dispatch.
    """
    if not DEFAULT_WITNESS_DIR.is_dir():
        return None
    cutoff = datetime.now() - WITNESS_OPEN_WINDOW
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for p in DEFAULT_WITNESS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("closed_at"):
            continue
        started = data.get("started_at")
        if not isinstance(started, str):
            continue
        try:
            sdt = datetime.fromisoformat(started)
        except ValueError:
            continue
        if sdt < cutoff:
            continue
        candidates.append((sdt, data))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0], reverse=True)
    return candidates[0][1]


def _agent_native_model(subagent_type: str) -> str:
    """Resolve `subagent_type` to its native-leg model identity.

    Reads `harness/agents.toml` — `agents.<subagent_type>.voices.native`.
    Returns "" when the agent is not registered, has no native leg, or
    the TOML can't be parsed (best-effort; hook never raises).
    """
    if not subagent_type:
        return ""
    try:
        path = REPO_ROOT / "harness" / "agents.toml"
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    agents = data.get("agents")
    if not isinstance(agents, dict):
        return ""
    row = agents.get(subagent_type)
    if not isinstance(row, dict):
        return ""
    voices = row.get("voices")
    if not isinstance(voices, dict):
        return ""
    native = voices.get("native")
    return str(native) if native else ""


def _active_runtime(explicit: str) -> str:
    """Resolve the runtime for a native project-agent leg.

    Active-session signals take precedence over the external launcher's
    preference. Outside either runtime, use the same local and committed
    selection contract as `scripts/atelier_runtime.py`.
    """
    if explicit != "auto":
        return explicit
    active = os.environ.get("ATELIER_ACTIVE_RUNTIME")
    if active in {"codex", "claude"}:
        return active
    if os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_PROJECT_DIR"):
        return "claude"
    selected = os.environ.get("ATELIER_RUNTIME")
    if selected in {"codex", "claude"}:
        return selected
    try:
        from atelier_runtime import load_registry, resolve_runtime

        return resolve_runtime(load_registry())[0]
    except Exception:
        return "codex"


def _runtime_native_model(subagent_type: str, runtime: str) -> str:
    if runtime == "claude":
        return _agent_native_model(subagent_type)
    try:
        data = tomllib.loads(RUNTIMES_TOML.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    runtimes = data.get("runtimes")
    row = runtimes.get(runtime) if isinstance(runtimes, dict) else None
    identity = row.get("native_shadow_identity") if isinstance(row, dict) else None
    if identity == "role":
        return _agent_native_model(subagent_type)
    return str(identity) if identity else ""


def cmd_native_model(args: argparse.Namespace) -> int:
    runtime = _active_runtime(args.runtime)
    identity = _runtime_native_model(args.agent, runtime)
    if not identity:
        sys.stderr.write(
            f"shadow: no native model identity for agent={args.agent!r} runtime={runtime!r}\n"
        )
        return 2
    print(identity)
    return 0


def _extract_response_text(tool_output: Any) -> str:
    """Pull a text response out of PostToolUse `tool_output`.

    Two observed shapes:
      - string: `tool_output["output"]` is a single string
      - blocks: `tool_output["output"]` is a list of content blocks, each
        a dict with at least `{"type": "text", "text": "..."}` or just `text`
    Returns "" when neither shape produces extractable text.
    """
    if not isinstance(tool_output, dict):
        return ""
    out = tool_output.get("output")
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        parts: list[str] = []
        for block in out:
            if isinstance(block, dict):
                txt = block.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    return ""


def cmd_log_from_hook(_args: argparse.Namespace) -> int:
    """`PostToolUse(Agent)` hook entry — auto-log native legs.

    Reads PostToolUse stdin JSON. When all of these hold, writes one
    synthetic native-leg JSONL entry (same shape as `cmd_log`):

      1. tool_name == "Agent"
      2. an open witness exists in DEFAULT_WITNESS_DIR within the 30-min
         recency window
      3. the agent's native voice (from harness/agents.toml) resolves to
         a model identity
      4. that model + leg="native" matches one of the witness's
         expected_dispatches

    Silent on every failure path (exit 0). The orchestrator never sees
    this output. See protocols/shadow-log.md § Mechanism.
    """
    del _args  # argparse-callback signature parity; this entrypoint reads stdin, not args
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError, ValueError):
        return 0
    if not isinstance(data, dict) or data.get("tool_name") != "Agent":
        return 0
    tool_input = data.get("tool_input")
    tool_output = data.get("tool_output")
    if not isinstance(tool_input, dict):
        return 0
    # Field-name resilience: Claude Code's PostToolUse(Agent) stdin uses
    # `subagent_type` and `prompt`, but accept the documented Agent SDK
    # variants (`agent_type`, `instructions`) as fallback so a future runtime
    # rename doesn't silently empty the log.
    subagent_type = str(
        tool_input.get("subagent_type") or tool_input.get("agent_type") or ""
    )
    prompt = str(tool_input.get("prompt") or tool_input.get("instructions") or "")
    response = _extract_response_text(tool_output)
    if not response:
        return 0
    witness = _find_open_witness()
    if witness is None:
        return 0
    model = _agent_native_model(subagent_type)
    if not model:
        return 0
    expected = witness.get("expected_dispatches") or []
    if not isinstance(expected, list):
        return 0
    # Match priority: an expected_dispatches entry with `subagent_type` set
    # MUST match the dispatch's subagent_type exactly (defeats cross-task
    # contamination when two agents share a native model identity, e.g.,
    # thinker + evolver + scholar all map to `opus`). Entries WITHOUT
    # `subagent_type` fall back to the model+leg match for backward
    # compatibility with witnesses written before this field existed.
    def _matches(e: dict[str, Any]) -> bool:
        if e.get("model") != model or e.get("leg") != "native":
            return False
        expected_sa = e.get("subagent_type")
        if expected_sa is None:
            return True
        return expected_sa == subagent_type
    if not any(isinstance(e, dict) and _matches(e) for e in expected):
        return 0
    input_tokens_approx = max(1, len(prompt) // 4)
    output_tokens_approx = max(1, len(response) // 4)
    event = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "shadow_group_id": witness.get("group_id"),
        "task_type": witness.get("task_type"),
        "task_dispatch_kind": "native",
        "model": model,
        "api_model": None,
        "endpoint": None,
        "session": data.get("session_id"),
        "system": None,
        "user_prompt": prompt,
        "status": "ok",
        "response_content": response,
        "reasoning_content": None,
        "finish_reason": "stop",
        "usage": None,
        "usage_estimate": {
            "input_tokens": input_tokens_approx,
            "output_tokens": output_tokens_approx,
            "method": "char_approx",
        },
        "cost_estimate_method": "char_approx",
        "latency_s": None,
        "logged_by": "post_tool_use_hook",
    }
    try:
        DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = DEFAULT_LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        return 0
    _mirror_skeleton(event)
    return 0


# ---------- report ----------


def _load_costs() -> dict[str, dict[str, Any]]:
    if not COSTS_TOML.exists():
        return {}
    data = tomllib.loads(COSTS_TOML.read_text(encoding="utf-8"))
    return data.get("costs", {}) or {}


def _load_shadow_tasks() -> dict[str, dict[str, Any]]:
    if not SHADOW_TASKS_TOML.exists():
        return {}
    data = tomllib.loads(SHADOW_TASKS_TOML.read_text(encoding="utf-8"))
    return data.get("tasks", {}) or {}


def _iter_log_files(since: date | None) -> list[Path]:
    out: list[Path] = []
    for d in (DEFAULT_LOG_DIR,):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.jsonl")):
            try:
                file_date = date.fromisoformat(p.stem)
            except ValueError:
                continue
            if since and file_date < since:
                continue
            out.append(p)
    return out


def _load_events(since: date | None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for p in _iter_log_files(since):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                events.append(ev)
        except OSError:
            continue
    return events


def _stale_cost_days(costs: dict[str, dict[str, Any]], today: date) -> dict[str, int]:
    stale: dict[str, int] = {}
    for name, row in costs.items():
        lv = row.get("last_verified")
        if not isinstance(lv, str):
            stale[name] = -1  # unknown / unset
            continue
        try:
            d = date.fromisoformat(lv)
        except ValueError:
            stale[name] = -1
            continue
        days = (today - d).days
        if days > 90:
            stale[name] = days
    return stale


def _compute_cost_usd(usage: dict[str, Any] | None, usage_est: dict[str, Any] | None,
                      cost_row: dict[str, Any] | None) -> tuple[float | None, str]:
    """Return (cost_usd, method) where method is 'usage'|'char_approx'|'unknown'."""
    if not cost_row:
        return None, "unknown"
    inp_per_m = cost_row.get("input_per_1m_usd")
    out_per_m = cost_row.get("output_per_1m_usd")
    if inp_per_m is None or out_per_m is None:
        return None, "unknown"
    if usage and isinstance(usage, dict):
        in_tok = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        out_tok = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        method = "usage"
    elif usage_est and isinstance(usage_est, dict):
        in_tok = usage_est.get("input_tokens", 0)
        out_tok = usage_est.get("output_tokens", 0)
        method = usage_est.get("method", "char_approx")
    else:
        return None, "unknown"
    cost = (float(in_tok) * float(inp_per_m) + float(out_tok) * float(out_per_m)) / 1_000_000.0
    return cost, method


def _extract_verdict(response: str | None, task_row: dict[str, Any]) -> str | None:
    if not response or not isinstance(response, str):
        return None
    pattern = task_row.get("verdict_pattern")
    if not isinstance(pattern, str):
        return None
    flags = re.IGNORECASE if task_row.get("case_insensitive") else 0
    matches = re.findall(pattern, response, flags=flags)
    if not matches:
        return None
    chosen = matches[-1] if task_row.get("last_match_wins") else matches[0]
    if isinstance(chosen, tuple):
        chosen = next((c for c in chosen if c), "")
    return chosen.lower() if task_row.get("case_insensitive") else chosen


def _load_witnesses() -> dict[str, dict[str, Any]]:
    """Read all witness files indexed by group_id."""
    witnesses: dict[str, dict[str, Any]] = {}
    if not DEFAULT_WITNESS_DIR.is_dir():
        return witnesses
    for p in DEFAULT_WITNESS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("group_id"):
                witnesses[data["group_id"]] = data
        except (OSError, json.JSONDecodeError):
            continue
    return witnesses


def cmd_report(args: argparse.Namespace) -> int:
    since = date.fromisoformat(args.since) if args.since else None
    costs = _load_costs()
    tasks = _load_shadow_tasks()
    today = date.today()

    # Stale-cost fail-closed: refuse to emit absolute cost columns if any
    # known-aggregated model is >90d stale, unless --accept-stale-costs.
    stale = _stale_cost_days(costs, today)
    # -1 = last_verified missing/unparseable: at least as stale as >90d, so
    # it fails closed too (silently exempting it defeated the gate's intent).
    stale_aggregated = {m: d for m, d in stale.items() if d > 90 or d == -1}
    if stale_aggregated and not args.accept_stale_costs:
        sys.stderr.write("ERROR: cost catalog has stale entries:\n")
        for m, d in sorted(stale_aggregated.items()):
            label = f"{d}d since last_verified" if d >= 0 else "last_verified missing/unparseable"
            sys.stderr.write(f"  - {m}: {label}\n")
        sys.stderr.write(
            "Refresh harness/model_costs.toml or pass --accept-stale-costs to "
            "see annotated values.\n"
        )
        return 2

    events = _load_events(since)
    events = [e for e in events if e.get("shadow_group_id")]
    if args.task_type:
        events = [e for e in events if e.get("task_type") == args.task_type]

    witnesses = _load_witnesses()

    # Group events by shadow_group_id.
    by_group: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        by_group.setdefault(ev["shadow_group_id"], []).append(ev)

    # Per-group enrichment.
    group_summaries: list[dict[str, Any]] = []
    witness_absent_count = 0
    for gid, legs in by_group.items():
        witness = witnesses.get(gid)
        if witness is None:
            witness_absent_count += 1
        task_type = legs[0].get("task_type", "unknown")
        task_row = tasks.get(task_type, {})
        leg_records: list[dict[str, Any]] = []
        for leg in legs:
            model = leg.get("model") or "(unknown)"
            cost_row = costs.get(model)
            cost_usd, cost_method = _compute_cost_usd(
                leg.get("usage"), leg.get("usage_estimate"), cost_row
            )
            verdict = _extract_verdict(leg.get("response_content"), task_row) if task_row else None
            leg_records.append({
                "model": model,
                "leg": leg.get("task_dispatch_kind", "unknown"),
                "cost_usd": cost_usd,
                "cost_method": cost_method,
                "latency_s": leg.get("latency_s"),
                "verdict": verdict,
                "finish_reason": leg.get("finish_reason"),
                "stale_days": stale.get(model, 0) if model in stale else 0,
            })
        # Missing-leg check against witness.
        missing: list[dict[str, Any]] = []
        if witness:
            seen = {(r["model"], r["leg"]) for r in leg_records}
            for exp in witness.get("expected_dispatches", []):
                key = (exp.get("model"), exp.get("leg"))
                if key not in seen:
                    missing.append({"model": exp.get("model"), "leg": exp.get("leg")})
        group_summaries.append({
            "group_id": gid,
            "task_type": task_type,
            "witness_present": witness is not None,
            "legs": leg_records,
            "missing_legs": missing,
        })

    # Aggregate per task_type per leg-pair. Groups with 2+ logged legs
    # produce pair stats regardless of witness presence. Single-leg groups
    # without a witness are excluded (no pair to compare).
    per_task: dict[str, dict[str, Any]] = {}
    for gs in group_summaries:
        if len(gs["legs"]) < 2 and not gs.get("witness_present"):
            gs["excluded_from_pair_stats"] = True
            continue
        tt = gs["task_type"]
        d = per_task.setdefault(tt, {"groups": [], "by_pair": {}})
        d["groups"].append(gs["group_id"])
        verdicts = {(r["model"], r["leg"]): r["verdict"] for r in gs["legs"]}
        costs_per_leg = {(r["model"], r["leg"]): r["cost_usd"] for r in gs["legs"]}
        latencies = {(r["model"], r["leg"]): r["latency_s"] for r in gs["legs"]}
        keys = sorted(verdicts.keys())
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                pair = (keys[i], keys[j])
                pair_str = f"{pair[0][0]}[{pair[0][1]}] vs {pair[1][0]}[{pair[1][1]}]"
                pd = d["by_pair"].setdefault(pair_str, {
                    "groups": 0, "agreements": 0,
                    "cost_left": [], "cost_right": [],
                    "lat_left": [], "lat_right": [],
                })
                pd["groups"] += 1
                v_l, v_r = verdicts[pair[0]], verdicts[pair[1]]
                if v_l is not None and v_r is not None and v_l == v_r:
                    pd["agreements"] += 1
                if costs_per_leg[pair[0]] is not None:
                    pd["cost_left"].append(costs_per_leg[pair[0]])
                if costs_per_leg[pair[1]] is not None:
                    pd["cost_right"].append(costs_per_leg[pair[1]])
                if latencies[pair[0]] is not None:
                    pd["lat_left"].append(latencies[pair[0]])
                if latencies[pair[1]] is not None:
                    pd["lat_right"].append(latencies[pair[1]])

    if args.json:
        payload = {
            "scope_banner": SCOPE_BANNER,
            "since": since.isoformat() if since else None,
            "groups": group_summaries,
            "per_task": per_task,
            "warnings": {
                "stale_costs_days": stale_aggregated,
                "witness_absent_count": witness_absent_count,
            },
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    # Human report.
    print(SCOPE_BANNER)
    print()
    if not per_task:
        print(f"No shadow-correlated groups found (since={since or 'all-time'}, task={args.task_type or 'any'}).")
        print(f"Total ungrouped events: {len(events)}")
        return 0

    for tt, d in sorted(per_task.items()):
        print(f"task={tt}  groups={len(d['groups'])}")
        for pair_str, pd in sorted(d["by_pair"].items()):
            agree = f"{pd['agreements']}/{pd['groups']}" if pd["groups"] else "0/0"
            agree_pct = (
                f"{100.0 * pd['agreements'] / pd['groups']:.1f}%" if pd["groups"] else "N/A"
            )

            def _avg(xs: list[float]) -> str:
                return f"${sum(xs) / len(xs):.4f}" if xs else "n/a"

            def _lat(xs: list[float]) -> str:
                return f"{sum(xs) / len(xs):.1f}s" if xs else "n/a"

            print(f"  {pair_str}")
            print(f"    verdict agreement: {agree} = {agree_pct}")
            print(f"    avg cost: left {_avg(pd['cost_left'])}, right {_avg(pd['cost_right'])}")
            print(f"    avg latency: left {_lat(pd['lat_left'])}, right {_lat(pd['lat_right'])}")
        print()

    if stale_aggregated:
        print("WARNINGS:")
        for m, d in sorted(stale_aggregated.items()):
            print(f"  - {m}: cost price is {d}d stale (>90d); report run with --accept-stale-costs")
    if witness_absent_count:
        print(f"  - {witness_absent_count} logged group(s) have no witness file "
              f"(group-start was skipped or witness file deleted); treated as single-leg, not aggregated into leg-pair stats above.")
    # char_approx flag
    approx_count = sum(
        1 for gs in group_summaries for r in gs["legs"] if r["cost_method"] == "char_approx"
    )
    if approx_count:
        print(f"  - {approx_count} leg row(s) computed via char_approx (±25% true cost); see report fields cost_method=char_approx in --json output")
    return 0


# ---------- CLI ----------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="scripts/shadow.py",
        description="Cross-provider shadow-log correlation + reporting.",
    )
    sub = ap.add_subparsers(dest="subcommand", required=True)

    gs = sub.add_parser("group-start", help="Open a shadow group; print env exports + write witness file.")
    gs.add_argument("--task", required=True, help="Task type (e.g., system-review, decision, privacy-review).")
    gs.add_argument(
        "--agent",
        help=(
            "Registered dual-voice agent role (e.g. privacy-reviewer, thinker): "
            "expected legs are derived from harness/agents.toml voices plus the "
            "runtime-aware native identity. Alternative to --expected."
        ),
    )
    gs.add_argument(
        "--runtime", default="",
        help="Override the active runtime for --agent native-leg resolution.",
    )
    gs.add_argument(
        "--expected",
        help=(
            'JSON list of expected dispatches; each entry has {"model": "...", "leg": "native|direct|codex"} '
            'AND an optional {"subagent_type": "..."} field used by the hook to disambiguate when multiple '
            'agents can share a native model identity (for example, Codex project agents use `codex_native`). Example: '
            '\'[{"model":"codex_native","leg":"native","subagent_type":"thinker"},{"model":"deepseek_pro_max","leg":"direct"}]\''
        ),
    )
    gs.set_defaults(func=cmd_group_start)

    gcp = sub.add_parser(
        "gc",
        help="Garbage-collect stale shadow telemetry (orphaned witnesses + aged logs). Wire as a runtime lifecycle hook.",
        description=(
            "Two cleanup passes: orphaned witness files older than "
            "--witness-min minutes; primary llm_calls/ JSONLs older than "
            "--retention-days days. The $OV mirror skeleton is never "
            "touched (durable record). Silent best-effort. Designed for "
            "the Claude SessionEnd and Codex Stop hooks."
        ),
    )
    gcp.add_argument(
        "--witness-min", type=int, default=DEFAULT_WITNESS_STALENESS_MINUTES,
        help="Minutes; witnesses older than this are deleted (default 30; matches PostToolUse hook recency window).",
    )
    gcp.add_argument(
        "--retention-days", type=int, default=DEFAULT_LLM_CALLS_RETENTION_DAYS,
        help="Days; llm_calls JSONLs older than this are deleted (default 90).",
    )
    gcp.set_defaults(func=cmd_gc)

    gc = sub.add_parser(
        "group-close",
        help="Close a shadow group's witness from the multi-leg completion path.",
        description=(
            "Close a witness so the PostToolUse hook cannot mis-correlate later "
            "Agent dispatches into a stale group within the 30-min recency window. "
            "Default deletes the witness file; --mark-closed writes a `closed_at` field "
            "instead (preserves the witness for forensic replay). Silent best-effort."
        ),
    )
    gc.add_argument("--group", required=True, help="Shadow group UUID (typically `$ATELIER_SHADOW_GROUP`).")
    gc.add_argument(
        "--mark-closed", action="store_true",
        help="Write `closed_at` field instead of deleting the witness file.",
    )
    gc.set_defaults(func=cmd_group_close)

    lg = sub.add_parser("log", help="Append a synthetic native/codex-leg log entry.")
    lg.add_argument("--group", required=True, help="Shadow group UUID.")
    lg.add_argument("--task", required=True, help="Task type.")
    lg.add_argument("--model", required=True, help="Model identity from harness/models.toml.")
    lg.add_argument("--leg", required=True, choices=("native", "codex"), help="Dispatch kind (direct is auto-logged by chat_completion.py).")
    pf = lg.add_mutually_exclusive_group(required=True)
    pf.add_argument("--prompt-text", help="Inline prompt text.")
    pf.add_argument("--prompt-file", help="Path to prompt text.")
    pf.add_argument("--prompt-stdin", action="store_true", help="Read prompt from stdin (response must be --response-file or --response-text).")
    rf = lg.add_mutually_exclusive_group(required=True)
    rf.add_argument("--response-text", help="Inline response text.")
    rf.add_argument("--response-file", help="Path to response text.")
    rf.add_argument("--response-stdin", action="store_true", help="Read response from stdin (prompt must be --prompt-file or --prompt-text).")
    lg.add_argument("--log-dir", default=None, help="Override default log dir.")
    lg.set_defaults(func=cmd_log)

    nm = sub.add_parser(
        "native-model",
        help="Resolve the active runtime's shadow identity for a project agent.",
    )
    nm.add_argument("--agent", required=True, help="Registered project-agent role.")
    nm.add_argument(
        "--runtime",
        choices=("auto", "claude", "codex"),
        default="auto",
        help="Runtime override; auto detects the active session (default).",
    )
    nm.set_defaults(func=cmd_native_model)

    lh = sub.add_parser(
        "log-from-hook",
        help="Optional PostToolUse(Agent) native-leg replay logger; not wired.",
        description=(
            "Retained for replay and fixtures; production logging is in-band. "
            "Reads the hook's stdin JSON, scans for an open shadow witness "
            "(most-recently-started within 30 min), looks up the agent's "
            "native-leg model in harness/agents.toml, and writes a "
            "synthetic native-leg JSONL entry when the dispatch matches "
            "the witness's expected_dispatches. Silent on every failure. "
            "Contract: protocols/shadow-log.md § Mechanism."
        ),
    )
    lh.set_defaults(func=cmd_log_from_hook)

    rp = sub.add_parser("report", help="Aggregate logs and emit cost/verdict-agreement comparison.")
    rp.add_argument("--since", help="YYYY-MM-DD; only include events from this date forward.")
    rp.add_argument("--task-type", help="Filter to one task type.")
    rp.add_argument("--accept-stale-costs", action="store_true", help="Allow report when cost catalog is >90d stale.")
    rp.add_argument("--json", action="store_true", help="Emit JSON.")
    rp.set_defaults(func=cmd_report)

    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
