"""Pinned, loopback-only Collector configuration and content allowlist."""

from pathlib import Path

VERSION = "0.160.0"
DARWIN_ARM64_SHA256 = "ceb5309ba16f2587dbef765d54e15c803354d038b0495b0b691e1eb9876d17c9"
ENDPOINT = "http://127.0.0.1:14318"
HEALTH = "http://127.0.0.1:14319"
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "SubagentStart", "SubagentStop", "PostCompact")
NUMBERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
           "cost_usd", "duration_ms", "attempt", "total_attempts", "event.sequence")
CLAUDE_EVENTS = ("api_request", "api_error", "api_retries_exhausted", "tool_result", "compaction")


def state_dir() -> Path:
    return Path.home() / "Library/Application Support/Atelier/Observability"


def configuration(directory: Path, *, port: int = 14318, health_port: int = 14319) -> dict:
    # Export only a newly constructed JSON body. Neither native bodies nor
    # resource/scope metadata (including schema URLs) reach the file encoder.
    statements = ['set(log.body, ParseJSON("{}"))', 'set(log.body["version"], 1)',
                  'set(log.body["time_unix_nano"], log.time_unix_nano)',
                  'set(log.body["time_unix_nano"], log.observed_time_unix_nano) where log.time_unix_nano == 0']
    recognized = []
    for runtime in ("claude", "codex"):
        hook = f'resource.attributes["service.name"] == "atelier-hook-{runtime}"'
        for event in (*HOOK_EVENTS, "Interrupt" if runtime == "codex" else "StopFailure"):
            condition = f'{hook} and log.attributes["event.name"] == "{event}"'
            recognized.append(condition)
            statements += [f'set(log.body["runtime"], "{runtime}") where {condition}',
                           f'set(log.body["event"], "hook.{event}") where {condition}']
    for event in CLAUDE_EVENTS:
        condition = ('resource.attributes["service.name"] == "claude-code" and '
                     'resource.attributes["atelier.scope"] == "interactive" and '
                     f'log.attributes["event.name"] == "{event}"')
        recognized.append(condition)
        statements += [f'set(log.body["runtime"], "claude") where {condition}',
                       f'set(log.body["event"], "{event}") where {condition}']
    for target, key in {"session": "session.id", "turn": "prompt.id", "request": "request_id",
                        "client_request": "client_request_id", "tool_call": "tool_use_id",
                        "agent": "agent_id"}.items():
        field = f'log.attributes["{key}"]'
        statements.append(f'set(log.body["{target}"], SHA256({field})) where IsString({field}) and Len({field}) > 0 and Len({field}) <= 256')
    for key in NUMBERS:
        field = f'log.attributes["{key}"]'
        numeric = f'(IsInt({field}) or IsDouble({field}) or (IsString({field}) and IsMatch({field}, "^[0-9]+([.][0-9]+)?$")))'
        statements.append(f'set(log.body["{key}"], Double({field})) where {numeric} and Double({field}) >= 0 and Double({field}) <= 1000000000000000')
    model = 'log.attributes["model"]'
    statements += [f'set(log.body["model"], {model}) where IsString({model}) and IsMatch({model}, "^(claude|gpt|o[1-9])[-a-zA-Z0-9._]{{1,75}}$")',
                   'set(log.body["invocation"], SHA256(resource.attributes["atelier.invocation"])) where IsString(resource.attributes["atelier.invocation"])']
    return {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": f"127.0.0.1:{port}",
                       "max_request_body_size": 1048576, "read_timeout": "2s"}}}},
        "extensions": {"health_check": {"endpoint": f"127.0.0.1:{health_port}"},
                       "json_log_encoding": {"mode": "body", "array_mode": False}},
        "processors": {
            "memory_limiter": {"check_interval": "1s", "limit_mib": 256, "spike_limit_mib": 64},
            "filter/known": {"error_mode": "propagate", "logs": {"log_record": ["not (" + " or ".join(recognized) + ")"]}},
            "transform/privacy": {"error_mode": "propagate", "log_statements": statements},
        },
        "exporters": {"file": {"path": str(directory / "events.jsonl"), "encoding": "json_log_encoding",
                      "format": "json", "rotation": {"max_megabytes": 8, "max_backups": 3, "max_days": 14}}},
        "service": {"extensions": ["health_check", "json_log_encoding"],
                    "telemetry": {"logs": {"level": "fatal"}, "metrics": {"level": "none"}},
                    "pipelines": {"logs": {"receivers": ["otlp"],
                                  "processors": ["memory_limiter", "filter/known", "transform/privacy"],
                                  "exporters": ["file"]}}},
    }
