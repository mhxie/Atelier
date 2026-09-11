#!/usr/bin/env python3
"""Fail-closed Bash allowlist for read-only agents.

Every simple pipeline segment must match a read rule. Unparsed shell syntax,
substitutions, background jobs, heredocs, and unsupported redirections deny;
only line-addressed ``sed -n`` is accepted. Allow emits nothing, while deny or
an internal failure emits one JSON decision; both exit zero for hook delivery.
"""
from __future__ import annotations

import json
import re
import shlex
import sys

PREFIX = "read-only agent: "
PLAIN_WORDS = "Spell the command out as plain words."
MASK = "\x00"
REDIRECTS = frozenset({"2>&1", "2>/dev/null", "</dev/null"})
PROJECT_DIR = "${CLAUDE_PROJECT_DIR:-.}/"
# the only expansion the guard can read: any other name could carry a subcommand or a writing flag inward
PARAM = re.compile(r"\$\{CLAUDE_PROJECT_DIR(?::-[^{}$`'\"\s]*)?\}|\$CLAUDE_PROJECT_DIR\b")
# line addresses only: a script that is not one or two numbers can substitute, write, or read another file
SED_SCRIPT = re.compile(r"^[0-9]+(,[0-9]+)?p$")
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
SEPARATOR = re.compile(r"&&|\|\||[;|]")

# unquoted shell text the guard refuses to reason about at all, and what to call it
FORBIDDEN = (
    (r"[(){}]", "a subshell or brace group"),
    (r"&", "a background job or an unsupported redirection"),
    (r"(?:^|\s)!(?:\s|$)", "a `!` word (history expansion, or a negated test)"),
)

# commands whose arguments are always data
READERS = frozenset({
    "basename", "cat", "cmp", "cut", "diff", "dirname", "echo", "file", "grep", "head", "ls", "nl", "printf",
    "pwd", "readlink", "realpath", "rg", "shasum", "sort", "stat", "tail", "tr", "true", "uniq", "wc", "which",
})
FIND_BANNED = ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls", "-fprint0")
PYTHON_EXE = frozenset({"python3", "python", ".venv/bin/python", ".venv/bin/python3"})
# scripts vetted against their argparse as read-only; SUBCOMMAND_ONLY narrows a script to its one read verb
VETTED = frozenset({
    "harness_smoke.py", "harness_lint.py", "privacy_check.py", "intent_coverage.py", "decay_scan.py", "semantic.py",
})
SUBCOMMAND_ONLY = {"intent_coverage.py": "catalog", "semantic.py": "query"}
SCRIPT_WRITE_FLAGS = ("--fix", "--write", "--rebuild", "--in-place", "-i")

# git subcommands that never move the working tree, the index, or refs
GIT_READERS = frozenset({
    "status", "diff", "show", "log", "blame", "ls-files", "ls-tree", "rev-parse", "rev-list", "cat-file",
    "describe", "shortlog", "grep", "merge-base", "name-rev", "count-objects", "check-ignore", "for-each-ref",
})
BRANCH_FLAGS = frozenset({
    "--list", "-a", "--all", "-r", "--show-current", "-v", "-vv", "--merged", "--no-merged", "--contains",
})

# git subcommands that read in one shape only; the predicate says which shape. `tag` lists: nothing,
# `-l`/`--list` with any patterns, or nothing but `-n<num>` format flags
GIT_LIMITED = {
    "branch": lambda args: all(arg in BRANCH_FLAGS for arg in args),
    "tag": lambda args: not args or args[0] in ("-l", "--list") or all(a.startswith("-n") for a in args),
    "stash": lambda args: args[:1] in (["list"], ["show"]),
    "remote": lambda args: not args or args[:1] in (["-v"], ["show"]),
    "config": lambda args: bool(args) and args[0] in ("--get", "--get-all", "--get-regexp", "--list", "-l"),
    "worktree": lambda args: args[:1] == ["list"],
    "reflog": lambda args: not args or args[:1] == ["show"],
}


def _vetted(token: str) -> str | None:
    """The vetted script a token names, written `scripts/<S>` or `${CLAUDE_PROJECT_DIR:-.}/scripts/<S>`."""
    stem = token[len(PROJECT_DIR) :] if token.startswith(PROJECT_DIR) else token
    name = stem[len("scripts/") :] if stem.startswith("scripts/") else ""
    return name if name in VETTED else None


def _python(args: list[str]) -> bool:
    if args[:2] == ["-m", "unittest"]:
        return True
    if not args or not (script := _vetted(args[0])) or any(a.startswith(SCRIPT_WRITE_FLAGS) for a in args[1:]):
        return False
    return script not in SUBCOMMAND_ONLY or args[1:2] == [SUBCOMMAND_ONLY[script]]


def _uv(args: list[str]) -> bool:
    if args[:1] != ["run"]:
        return False
    rest, flags = args[1:], []
    while rest and rest[0] in ("--offline", "--no-sync"):
        flags.append(rest.pop(0))
    if rest[:1] and rest[0] in ("python3", "python"):
        return _python(rest[1:])
    return "--no-sync" not in flags and bool(rest) and bool(_vetted(rest[0])) and _python(rest)


def _ruff(args: list[str]) -> bool:
    # Both flags override config-driven writes; a fixed prefix cannot be mistaken for option values.
    return args[:3] == ["check", "--no-fix", "--no-fix-only"] and not any(
        a.startswith(("--fix", "--unsafe-fixes")) or a == "--diff-only-if-fix"
        or (_banned_long(a, ("output-file",)) if a.startswith("--") else a.startswith("-") and "o" in a[1:])
        for a in args[3:])


def _uvx(args: list[str]) -> bool:
    rest = args[1:] if args[:1] == ["--offline"] else args
    return rest[:1] == ["ruff"] and _ruff(rest[1:])


# Allowlisting a reading verb is not enough: these flags write a file or run a program.
READER_BANNED = {"file": (("compile",), "C"), "rg": (("pre", "hostname-bin"), ""),
                 "sort": (("output", "compress-program", "temporary-directory"), "oT")}


def _banned_long(given: str, banned: tuple[str, ...]) -> bool:
    """getopt_long takes any unambiguous abbreviation, so `--o` reaches `--output`."""
    stem = given[2:].partition("=")[0]
    return bool(stem) and any(flag.startswith(stem) for flag in banned)


def _reader(name: str):
    """A reader is safe only while none of its writing or executing flags appear."""
    banned, short_flags = READER_BANNED[name]
    return (lambda args: not any(
        _banned_long(arg, banned) if arg.startswith("--")
        else arg.startswith("-") and bool(set(short_flags) & set(arg[1:])) for arg in args),
            f"{name} may not use --{'/--'.join(banned)}; those write a file or run a program")


def _uniq(args: list[str]) -> bool:
    """`uniq [flags] [input [output]]`: a second file operand is an output file it truncates."""
    operands, skip, literal = 0, False, False
    for arg in args:
        if skip:
            skip = False
        elif literal or arg == "-" or not arg.startswith("-"):
            operands += 1
        elif arg == "--":
            literal = True
        elif arg in ("-f", "-s"):
            skip = True
    return operands <= 1


def _git(args: list[str]) -> bool:
    """No writing option anywhere, only `--no-pager` and `-C <path>` up front, and a subcommand that reads."""
    if any(arg.startswith(("--output", "--ext-diff", "--open-files-in-pager")) or arg == "-O" for arg in args):
        return False
    rest = list(args)
    while rest and rest[0].startswith("-"):
        if rest[0] == "--no-pager":
            rest.pop(0)
        elif rest[0] == "-C" and len(rest) > 1:
            del rest[:2]
        else:
            return False
    if not rest:
        return False
    return rest[0] in GIT_READERS or (rest[0] in GIT_LIMITED and bool(GIT_LIMITED[rest[0]](rest[1:])))


# executable -> (does this argument list read?, why the other forms do not)
RULES: dict[str, tuple] = {name: (lambda args: True, "") for name in READERS}
PYTHON_REASON = ("python is allowed only as `-m unittest ...` or a vetted read-only scripts/ entry (harness_smoke, "
                 "harness_lint, privacy_check, decay_scan, intent_coverage catalog, semantic query), no writing flag")
RULES.update({name: (_python, PYTHON_REASON) for name in PYTHON_EXE})
RULES["find"] = (lambda args: not any(a.startswith(FIND_BANNED) for a in args),
                 "find may not use -exec, -execdir, -ok, -okdir, -delete, or an -f* output action")
RULES["sed"] = (lambda args: len(args) == 3 and args[0] == "-n" and bool(SED_SCRIPT.match(args[1])),
                "sed is allowed only as `sed -n '<n>p' <file>` or `sed -n '<n>,<m>p' <file>`; every other "
                "script can substitute, write, or read a second file")
RULES["command"] = (lambda args: len(args) == 2 and args[0] == "-v", "`command` is allowed only as `command -v <name>`")
RULES.update({name: _reader(name) for name in READER_BANNED})
RULES["uniq"] = (_uniq, "uniq is allowed only with at most one file operand; `uniq <input> <output>` "
                        "truncates its second operand")
RULES["git"] = (_git, "not a read-only git form. Reading subcommands (status, diff, show, log, blame, ls-files, "
                      "rev-parse and friends), the listing shapes of branch/tag/stash/remote/config/worktree/reflog, "
                      "and only --no-pager and -C <path> before them. Read a baseline with `git show <rev>:<path>`")
RULES["uv"] = (_uv, "uv is allowed only as `uv run [--offline] [--no-sync] python3 <allowed python form>` or "
                    "`uv run [--offline] scripts/<vetted script>`")
RULES["uvx"] = (_uvx, "use `uvx [--offline] ruff check --no-fix --no-fix-only ...`, without mutation/output flags")
RULES["ruff"] = (_ruff, "use `ruff check --no-fix --no-fix-only ...` to override config-driven writes; --fix, "
                        "--unsafe-fixes and -o/--output-file are forbidden. --output-format still works")


def _expansion(text: str) -> str | None:
    """The substitution or expansion in text the guard cannot read; CLAUDE_PROJECT_DIR is the one it can."""
    if "`" in text or "$(" in text:
        return "a command substitution"
    return "an expansion of something other than CLAUDE_PROJECT_DIR" if "$" in PARAM.sub("", text) else None


def _scan(command: str) -> tuple[str | None, str]:
    """The line with its quoted runs blanked to MASK, or None and the shape that makes it unreadable.

    Single quotes make every character literal; double quotes keep the shell metacharacters literal but
    still deny substitutions and expansions; a newline or a heredoc denies quoted or not.
    """
    if "\n" in command:
        return None, "a newline or line continuation"
    if "<<" in command:
        return None, "a heredoc"
    for match in QUOTED.finditer(command):
        if match.group(0).startswith('"') and (problem := _expansion(match.group(0))):
            return None, problem
    masked = QUOTED.sub(lambda match: MASK * len(match.group(0)), command)
    if "'" in masked or '"' in masked:
        return None, "an unbalanced quote"
    problem = _expansion(masked)
    return (None, problem) if problem else (masked, "")


def _split(command: str, masked: str) -> list[tuple[str, str]]:
    """The simple commands, as (text, scanned text) pairs cut at the separators that fall outside quotes."""
    parts, start = [], 0
    for match in SEPARATOR.finditer(masked):
        parts.append((command[start : match.start()], masked[start : match.start()]))
        start = match.end()
    return parts + [(command[start:], masked[start:])]


def _segment(tokens: list[str]) -> str | None:
    """One simple command: the rule its executable carries, applied to the rest of its words."""
    words = [token for token in tokens if token not in REDIRECTS]
    if not words:
        return f"{PREFIX}a command segment with no command in it is not readable."
    executable = words[0]
    if "=" in executable:
        return f"{PREFIX}an environment assignment prefix (`{executable}`) is not on the allowlist."
    if "/" in executable and executable not in PYTHON_EXE:
        return f"{PREFIX}a path-prefixed executable (`{executable}`) is not on the allowlist; use the bare name."
    if executable not in RULES:
        return f"{PREFIX}`{executable}` is not on the read-only allowlist; read with cat/head/sed -n, search with rg."
    reads, reason = RULES[executable]
    return None if reads(words[1:]) else f"{PREFIX}{reason}."


def check(command: str) -> str | None:
    """The deny reason for a Bash command, or None when every simple command in it is allowed."""
    masked, shape = _scan(command)
    if masked is None:
        return f"{PREFIX}{shape} is not readable by this guard. {PLAIN_WORDS}"
    plain = PARAM.sub("", masked).replace("2>&1", " ").replace("&&", " ")  # the only `&` a line may carry
    for pattern, name in FORBIDDEN:
        if re.search(pattern, plain):
            return f"{PREFIX}{name} is not readable by this guard. {PLAIN_WORDS}"
    for text, scanned in _split(command, masked):
        if not text.strip():
            return f"{PREFIX}an empty command segment is not readable; drop the stray separator."
        for word in scanned.split():
            if ("<" in word or ">" in word) and word not in REDIRECTS:
                token = word.replace(MASK, "")
                return f"{PREFIX}`{token}` redirects; only 2>&1, 2>/dev/null, and </dev/null are allowed."
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError:
            return f"{PREFIX}`{text.strip()}` does not tokenize as plain shell words."
        if problem := _segment(tokens):
            return problem
    return None


def _deny(reason: str) -> dict:
    output = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}
    return {"hookSpecificOutput": output}


def decide(payload: object) -> dict | None:
    """A deny decision, or None for a payload another tool owns. One this guard cannot interpret is denied loudly."""
    if isinstance(payload, dict) and payload.get("tool_name") not in (None, "Bash"):
        return None
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return _deny(f"{PREFIX}the hook payload carries no Bash command to inspect, so the guard cannot vouch for "
                     "this call. Check scripts/readonly_bash_guard.py against the current payload shape.")
    reason = check(command)
    return _deny(reason) if reason else None


def main() -> int:
    """Exit 0 with a decision on stdout, or nothing: a non-zero exit is a non-blocking error Claude Code runs past."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        payload = None
    try:
        decision = decide(payload)
    except Exception as error:  # noqa: BLE001 - any failure of the guard is a deny
        decision = _deny(f"{PREFIX}the Bash guard failed ({type(error).__name__}), so it cannot vouch for this call. "
                         "Fix scripts/readonly_bash_guard.py; nothing runs until it does.")
    if decision is not None:
        json.dump(decision, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
