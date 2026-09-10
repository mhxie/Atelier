#!/usr/bin/env python3
"""Invoke provider-bound chat completions through the official SDK.

Committed model identities merge with gitignored provider bindings. Usage:
  scripts/chat_completion.py --model X --prompt "..."  # stateless
  scripts/chat_completion.py --model X --session /tmp/s.json --prompt -  # stdin
Without --model, supply --endpoint, --api-model, and --api-key-env instead.
Sessions replay a JSON array of {role, content} messages and append a turn
only after a successful response.

Exit codes: 0 success; 1 API/response error; 2 invalid configuration/session;
3 timeout; 4 invalid arguments; 5 max_tokens truncation. Exit 5 writes partial
content to stdout; a caller needing the full response must explicitly rerun
with higher --max-tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _models  # noqa: E402
from _paths import atomic_write  # noqa: E402


DEFAULT_TIMEOUT = 120.0


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8")
    if args.prompt == "-":
        return sys.stdin.read()
    return args.prompt or ""


def _load_session(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("session file must be a JSON array of messages")
    for i, m in enumerate(data):
        if not isinstance(m, dict) or "role" not in m or "content" not in m:
            raise ValueError(f"session[{i}] missing role/content")
    return data


def _resolve_extras(model_entry: dict | None, override_json: str | None) -> dict:
    extras: dict = {}
    if model_entry:
        extras.update(model_entry.get("direct_api_extras", {}) or {})
    if override_json:
        extras.update(json.loads(override_json))
    return extras


class ProviderError(Exception):
    """A transport failure normalized to this script's exit-code contract.

    Carries no SDK object, so callers see one vocabulary per exit code
    regardless of which SDK ran the call.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _openai_client(**kwargs):
    import openai

    return openai.OpenAI(**kwargs)


def _guarded(sdk, timeout: float, call):
    """Run an SDK call, translating its exceptions into ProviderError."""
    try:
        return call()
    except sdk.APITimeoutError as e:
        raise ProviderError(3, f"request timed out after {timeout}s") from e
    except sdk.APIConnectionError as e:
        raise ProviderError(1, f"network error: {e}") from e
    except sdk.APIStatusError as e:
        body = e.body if e.body is not None else {"raw": str(e)}
        raise ProviderError(
            1, f"HTTP {e.status_code}: {json.dumps(body, ensure_ascii=False, default=str)}"
        ) from e
    except json.JSONDecodeError as e:
        raise ProviderError(1, f"response not JSON: {e}") from e


def _call_openai(
    *, endpoint: str, api_model: str, api_key: str, messages: list[dict], max_tokens: int,
    extras: dict, timeout: float, max_retries: int,
) -> dict:
    """Chat completions through the openai SDK; returns the provider's JSON.

    The raw-response path keeps provider extensions a typed model drops
    (`reasoning_content`, and whatever a binding adds next).
    """
    import openai

    body: dict = {"model": api_model, "messages": messages}
    if max_tokens > 0:
        body["max_tokens"] = max_tokens
    client = _openai_client(
        base_url=endpoint.rstrip("/").removesuffix("/chat/completions"),
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
    )

    def call() -> dict:
        raw = client.chat.completions.with_raw_response.create(**body, extra_body=extras or None)
        return json.loads(raw.text)

    return _guarded(openai, timeout, call)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scripts/chat_completion.py",
        description="Chat completion invoker over the provider's official SDK.",
    )
    ap.add_argument(
        "--model",
        help=(
            "Model identity name (e.g., opus, sonnet, deepseek_pro_max). "
            "Reads `direct_api`, `direct_api_base`, `api_env`, "
            "`direct_api_provider`, `direct_api_extras` from the merged "
            "schema (harness/models.toml) + bindings (profile/models.toml). "
            "Any can be overridden by the ad-hoc flags below."
        ),
    )
    ap.add_argument("--endpoint", help="Override the model's direct_api_base.")
    ap.add_argument("--api-model", help="Override the model's direct_api (the provider's model id).")
    ap.add_argument(
        "--api-key-env",
        help="Override the model's api_env (env var name, not the key itself).",
    )
    ap.add_argument(
        "--extras-json",
        help="JSON object merged into request body, last-wins over the model's extras.",
    )

    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--prompt", help="User prompt text. Use '-' for stdin.")
    grp.add_argument("--prompt-file", help="Path to file containing prompt.")

    ap.add_argument("--system", default=None, help="Optional system prompt.")
    ap.add_argument(
        "--session",
        default=None,
        help=(
            "Path to a JSON session file. If it exists, prior messages are "
            "replayed before the new prompt; on success, the new turn is "
            "appended to the file. Atomic write."
        ),
    )
    ap.add_argument(
        "--max-tokens",
        type=int,
        default=8192,
        help=(
            "Output token cap. Defaults to 8192, which the precedent judge "
            "responses regularly exceed. When the response hits the cap "
            "(finish_reason=length), the script writes the partial content "
            "to stdout but exits 5 so callers see the truncation. Pass a "
            "higher value (e.g., 16384) for review-grade calls; pass 0 to "
            "OMIT max_tokens from the request entirely (no cap; provider's "
            "model maximum applies; truncation detection disabled). "
            "System-review callers SHOULD pass 0 — capping the safety net "
            "of the evolution loop is the worst place to save tokens."
        ),
    )
    ap.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help=(
            "Total attempts for transient errors (429, 408, 409, 5xx, "
            "timeouts, network errors): one call plus `max_attempts - 1` "
            "SDK retries with exponential backoff and jitter, honoring "
            "Retry-After. Default 3. Set 1 to disable retries (useful for "
            "tests and one-shot scripts that prefer a fast fail)."
        ),
    )
    ap.add_argument(
        "--check-context",
        type=int,
        default=0,
        metavar="MAX_INPUT_TOKENS",
        help=(
            "Pre-flight: estimate token count of the request (chars/4) and "
            "exit 4 before any API call if it exceeds MAX_INPUT_TOKENS. "
            "Pass the model's context window minus your --max-tokens budget. "
            "Default 0 = disabled."
        ),
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Per-call timeout in seconds. Defaults to the model's "
            "`direct_api_timeout` if set, else 120s. Reasoning-heavy models "
            "should configure this in the model's binding entry."
        ),
    )
    ap.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit full response object as JSON. Default emits message.content.",
    )
    args = ap.parse_args(argv)

    model_entry = _models.resolve(args.model) if args.model else None
    if args.model and model_entry is None:
        sys.stderr.write(
            f"chat_completion: model '{args.model}' not found in harness/models.toml\n"
        )
        return 2

    binding_src = model_entry or {}
    endpoint = args.endpoint or binding_src.get("direct_api_base")
    api_model = args.api_model or binding_src.get("direct_api")
    api_env = args.api_key_env or binding_src.get("api_env")
    if args.timeout is not None:
        timeout = args.timeout
    elif binding_src.get("direct_api_timeout"):
        timeout = float(binding_src["direct_api_timeout"])
    else:
        timeout = DEFAULT_TIMEOUT

    if not endpoint or not api_model or not api_env:
        sys.stderr.write(
            "chat_completion: missing required config (need endpoint, api-model, and api-key-env "
            "via --model or via --endpoint/--api-model/--api-key-env flags).\n"
        )
        return 2

    provider = str(binding_src.get("direct_api_provider") or "").strip().lower() or "openai"
    if provider != "openai":
        sys.stderr.write(
            f"chat_completion: unknown provider '{provider}'; "
            "direct_api_provider must be one of openai.\n"
        )
        return 2

    api_key = os.environ.get(api_env)
    if not api_key:
        sys.stderr.write(f"chat_completion: env var ${api_env} not set; cannot call API.\n")
        return 2

    prompt = _read_prompt(args)
    if not prompt.strip():
        sys.stderr.write("chat_completion: empty prompt.\n")
        return 4

    session_path = Path(args.session) if args.session else None
    try:
        history = _load_session(session_path) if session_path else []
    except (json.JSONDecodeError, ValueError) as e:
        sys.stderr.write(f"chat_completion: session file invalid: {e}\n")
        return 2

    # If --system is given, it replaces any existing system message at index 0;
    # otherwise the existing one (if any) stays. Without either, no system msg.
    if args.system is not None:
        history = [m for m in history if m.get("role") != "system"]
        history.insert(0, {"role": "system", "content": args.system})

    user_msg = {"role": "user", "content": prompt}
    messages = history + [user_msg]

    try:
        extras = _resolve_extras(binding_src, args.extras_json)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"chat_completion: --extras-json is not valid JSON: {e}\n")
        return 4

    # Optional pre-flight: refuse to send if the request would clearly bust
    # the model's context window. Estimate is rough (chars/4); the real
    # token count is provider-dependent but this catches the obvious cases
    # (a 50MB log file accidentally included via stdin).
    if args.check_context > 0:
        total_chars = sum(len(m.get("content", "")) for m in messages)
        if args.system:
            total_chars += len(args.system)
        estimated_tokens = total_chars // 4
        if estimated_tokens > args.check_context:
            sys.stderr.write(
                f"chat_completion: estimated input ≈ {estimated_tokens} tokens "
                f"(chars/4) exceeds --check-context cap {args.check_context}; "
                f"aborting before API call. Reduce input or raise the cap.\n"
            )
            return 4

    def _fail(code: int, message: str) -> int:
        sys.stderr.write(f"chat_completion: {message}\n")
        return code

    try:
        resp = _call_openai(
            endpoint=endpoint,
            api_model=api_model,
            api_key=api_key,
            messages=messages,
            max_tokens=args.max_tokens,
            extras=extras,
            timeout=timeout,
            max_retries=max(0, args.max_attempts - 1),
        )
    except ProviderError as e:
        return _fail(e.code, str(e))

    if "error" in resp:
        return _fail(1, f"API error: {json.dumps(resp['error'])}")

    try:
        choice = resp["choices"][0]
        msg = choice["message"]
        content = msg.get("content", "")
        reasoning = msg.get("reasoning_content")
        finish = choice.get("finish_reason")
    except (KeyError, IndexError) as e:
        return _fail(1, f"malformed response (missing choices/message/content): {e}")

    # Truncation = caller-visible failure (unless --max-tokens 0 opts out).
    # Partial content still written to stdout / session so the caller can
    # recover what arrived; exit code distinguishes truncation from other
    # success/failure modes.
    truncated = finish == "length" and args.max_tokens > 0
    # A reasoning model can spend its whole budget on reasoning_content and
    # return an empty `content` with finish_reason=length; with --max-tokens 0
    # that once looked like a clean success and the caller got a 0-byte report
    # (2026-08-23, direct leg). Empty completions are failures.
    empty = not (content or "").strip()

    if empty:
        sys.stderr.write(
            f"chat_completion: empty completion (finish_reason={finish}, "
            f"reasoning_chars={len(reasoning or '')}); no content written.\n"
        )
        return 1

    if truncated:
        sys.stderr.write(
            f"chat_completion: response truncated at max_tokens={args.max_tokens} "
            f"(finish_reason=length). Partial content written to stdout. "
            f"Re-run with a higher --max-tokens to recover the full response.\n"
        )
    elif finish and finish not in ("stop", "length"):
        # "length" is handled above (or silently accepted when max_tokens=0).
        # Anything else (tool_calls, content_filter, function_call, ...) is
        # unusual enough to surface but not block.
        sys.stderr.write(f"chat_completion: finish_reason={finish} (non-stop)\n")

    # Session save happens regardless of output format: --json changes what
    # goes to stdout, not whether the turn persists (--session promises it).
    if session_path is not None:
        history.append(user_msg)
        history.append({"role": "assistant", "content": content})
        try:
            atomic_write(session_path, json.dumps(history, ensure_ascii=False, indent=2))
        except OSError as e:
            sys.stderr.write(f"chat_completion: failed to write session file: {e}\n")
            # Don't fail the command — response is in stdout, caller can recover

    sys.stdout.write(json.dumps(resp, ensure_ascii=False) if args.emit_json else content)
    return 5 if truncated else 0


if __name__ == "__main__":
    sys.exit(main())
