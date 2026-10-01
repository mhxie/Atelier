#!/usr/bin/env python3
"""Run one Antigravity CLI completion for a model identity's `agy` binding.

The `agy` sibling of `chat_completion.py`. Scratch cwd, minimal env, `--sandbox`,
no allow-rules: file and command tools are auto-denied, but web search is not, so
prompt text can leave the machine in search queries. Output is data. Exit 0 ok;
1 error; 2 prerequisite missing (not installed, unbound, or signed out), a soft
skip; 3 timeout or truncated reply; 4 bad args.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _models  # noqa: E402

ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "GEMINI_API_KEY")


def _fail(code: int, message: str) -> int:
    sys.stderr.write(f"agy_leg: {message}\n")
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scripts/agy_leg.py", description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True, help="Identity with an `agy` binding.")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--prompt", help="Prompt text; '-' reads stdin.")
    grp.add_argument("--prompt-file")
    ap.add_argument("--system", help="Instructions prepended to the prompt.")
    ap.add_argument("--timeout", type=float, default=300.0, help="Seconds (also --print-timeout).")
    ap.add_argument("--max-tokens", type=int, default=0, help="Ignored; call-site parity.")
    args = ap.parse_args(argv)

    try:
        model = _models.agy_binding(args.model)
    except _models.ModelError as exc:
        return _fail(2, str(exc))
    if not model:
        return _fail(2, f"model '{args.model}' has no `agy` binding in profile/models.toml.")
    if args.timeout <= 0:
        return _fail(4, "--timeout must be positive.")
    try:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else (
            sys.stdin.read() if args.prompt == "-" else args.prompt)
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(4, f"cannot read prompt: {exc}")
    if not prompt.strip():
        return _fail(4, "empty prompt.")
    event = {"event": "user", "message": {"content": f"{args.system}\n\n{prompt}" if args.system else prompt}}
    cmd = ["agy", "--input-format", "stream-json", "--output-format", "stream-json", "--model", model,
           "--sandbox", "--disable-slash-commands", "--print-timeout", f"{math.ceil(args.timeout)}s"]
    with tempfile.TemporaryDirectory(prefix="agy-leg.") as scratch:
        try:
            proc = subprocess.run(cmd, input=json.dumps(event) + "\n", capture_output=True, text=True,
                                  cwd=scratch, timeout=args.timeout + 30,
                                  env={k: os.environ[k] for k in ENV_KEYS if k in os.environ})
        except FileNotFoundError:
            return _fail(2, "`agy` is not installed or not on PATH.")
        except subprocess.TimeoutExpired:
            return _fail(3, "timed out.")

    result = None
    for line in proc.stdout.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("event") == "result":
            result = obj.get("result") or {}
    if "print timeout" in proc.stderr:  # agy reports a cut-off reply as SUCCESS
        return _fail(3, proc.stderr.strip()[:300])
    if result is None or result.get("status") != "SUCCESS" or not str(result.get("response") or "").strip():
        detail = str((result or {}).get("error") or proc.stderr.strip() or "no result event")[:300]
        code = 3 if "timeout" in detail.lower() else 2 if re.search(r"auth|sign.?in|log.?in|credential", detail, re.I) else 1
        return _fail(code, detail)
    sys.stdout.write(result["response"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
