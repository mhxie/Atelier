#!/usr/bin/env python3
"""PreToolUse guard for read-only agents: deny git commands that move the
working tree, index, or history.

Reads the Claude Code hook payload on stdin. When the Bash command would run
a git command that moves the working tree, the index, or refs, write a file
inside the repository through a redirection, a file utility, an editor, or a formatter, or hand a
shell text the guard cannot read, answers with a deny decision. A payload the guard cannot interpret is denied too,
with a reason that says so, rather than silently disabling the guard.
Everything else passes.

Wired as a PreToolUse hook on Bash in an agent's frontmatter.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

# git commands with no read-only use: they move the working tree, the index,
# or refs. The guard stops a confused read-only agent; it is not a sandbox.
BLOCKED_GIT = frozenset(
    {
        "stash",
        "checkout",
        "restore",
        "apply",
        "reset",
        "switch",
        "clean",
        "add",
        "rm",
        "mv",
        "commit",
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "am",
        "pull",
        "push",
        "fetch",
        "update-index",
        "update-ref",
        "read-tree",
        "checkout-index",
        "filter-branch",
        "filter-repo",
        "replace",
        "gc",
        "prune",
        "repack",
        "pack-refs",
        "bisect",
        "submodule",
        "notes",
        "clone",
        "init",
        "archive",
        "format-patch",
        "bundle",
        "difftool",
        "mergetool",
        "maintenance",
        "sparse-checkout",
    }
)

# git commands that read by default and write only with these flags or verbs
# (mutating set); listing flags mark a read when a positional is present.
CONDITIONAL_GIT: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "branch": (
        frozenset({"-d", "-D", "-m", "-M", "-c", "-C", "-f", "--delete", "--move", "--copy",
                   "--force", "-u", "--set-upstream-to", "--unset-upstream", "--edit-description"}),
        frozenset({"-l", "--list", "-a", "--all", "-r", "--remotes", "--contains", "--no-contains",
                   "--merged", "--no-merged", "--points-at", "--show-current"}),
    ),
    "tag": (
        frozenset({"-d", "--delete", "-f", "--force", "-a", "--annotate", "-s", "--sign", "-u",
                   "--local-user", "-m", "--message", "-F", "--file"}),
        frozenset({"-l", "--list", "-n", "--contains", "--no-contains", "--merged", "--no-merged",
                   "--points-at"}),
    ),
    "reflog": (frozenset({"expire", "delete", "drop"}), frozenset()),
    "remote": (
        frozenset({"add", "remove", "rm", "rename", "set-url", "set-head", "set-branches", "prune", "update"}),
        frozenset(),
    ),
    "config": (
        frozenset({"--unset", "--unset-all", "--add", "--replace-all", "--remove-section", "--rename-section",
                   "-e", "--edit"}),
        frozenset({"--get", "--get-all", "--get-regexp", "-l", "--list", "--show-origin", "--show-scope"}),
    ),
    "symbolic-ref": (frozenset({"-d", "--delete"}), frozenset({"--short", "-q", "--quiet"})),
    "hash-object": (frozenset({"-w"}), frozenset()),
    "worktree": (frozenset({"add", "remove", "move", "prune", "lock", "unlock", "repair"}), frozenset()),
    "lfs": (
        frozenset({"pull", "fetch", "checkout", "prune", "migrate", "push", "install", "uninstall", "track",
                   "untrack", "lock", "unlock", "dedup", "clone", "update"}),
        frozenset(),
    ),
}
# a conditional command writes when it gets this many positionals and no
# listing flag
POSITIONAL_WRITE = {"branch": 1, "tag": 1, "config": 2, "symbolic-ref": 2}

# git options that take a separate argument before the subcommand, and
# those known to take none; any other option makes the stage uncertain
_OPTIONS_WITH_ARG = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env",
     "--attr-source", "--super-prefix", "--list-cmds"}
)
_OPTIONS_NO_VALUE = frozenset(
    {"-p", "-P", "--paginate", "--no-pager", "--no-optional-locks", "--bare", "--literal-pathspecs",
     "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs", "--no-replace-objects",
     "--no-lazy-fetch", "--no-advice", "--html-path", "--man-path", "--info-path", "--version",
     "--help", "-h"}
)

# shells and shell builtins that run their argument or a script file: their
# string arguments are scanned, anything they would read that the guard
# cannot (a variable, a substitution result, a script path) is denied
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "eval", "su", "source", "."})
# commands that hand later tokens to the shell as a command line
EVALUATORS = SHELLS | frozenset({"ssh", "xargs", "find"})
# interpreters whose program text is scanned for a spelled-out git command
INTERPRETERS = frozenset({"python", "python3", "python2", "perl", "ruby", "node", "php", "osascript"})
_INTERPRETER_NAME = re.compile(r"^(python|pypy)[23]?(?:\.\d+)*$|^(perl)5?(?:\.\d+)*$|^(ruby|node|php)\d*(?:\.\d+)*$|^(osascript)$")


def _interpreter_family(base: str) -> str:
    """The interpreter family a command name belongs to ("" when none):
    `python3.13` and `pypy3` are python, `perl5.30` is perl, `node20` is node."""
    match = _INTERPRETER_NAME.match(base)
    if not match:
        return ""
    family = match.group(1) or match.group(2) or match.group(3) or match.group(4)
    return "python" if family == "pypy" else family


def _is_interpreter(base: str) -> bool:
    return bool(_interpreter_family(base))
# commands whose arguments are data, never a command to run; any other
# first word of a stage is treated as a wrapper that may run its arguments
DATA_COMMANDS = frozenset(
    {"rg", "grep", "egrep", "fgrep", "ag", "ack", "echo", "printf", "cat", "head", "tail", "less",
     "more", "wc", "sort", "uniq", "cut", "tr", "awk", "sed", "ls", "stat", "file", "which", "type",
     "whereis", "man", "diff", "comm", "column", "jq", "yq", "tee", "cp", "mv", "rm", "rmdir",
     "mkdir", "touch", "chmod", "chown", "truncate", "dd", "patch", "install", "ln", "rsync",
     "cd", "pwd", "test", "[", "[[", "true", "false", "sleep", "export", "unset", "read", "set",
     "shift", "return", "exit", "wait", "kill", "pushd", "popd", "dirs", "date", "basename",
     "dirname", "realpath", "readlink", "du", "df", "ps", "md5", "md5sum", "shasum",
     "sha256sum", "hexdump", "xxd", "strings", "fold",
     "nl", "paste", "join", "rev", "seq", "expr", "bc", "tree", "fd", "mktemp", "pip",
     "open", "pbcopy", "pbpaste", "say"}
)
# agent CLIs that would act with their own tools, outside this hook
NESTED_AGENTS = frozenset({"codex", "claude"})
# tokens that precede a command without ending its command position
WRAPPERS = frozenset(
    {"sudo", "env", "nohup", "nice", "time", "timeout", "command", "exec", "builtin",
     "if", "then", "else", "elif", "while", "until", "do", "!"}
)
SEPARATORS = frozenset({"&&", "||", "|", ";", "&", "{", "}"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DURATION = re.compile(r"^\d+[smhd]?$")
# command substitution runs even inside double quotes; grouping opens a new
# command position
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_SUBST_MARK = "@@subst@@"
_SUBSHELL_OPEN = "@@subshell@@"
_SUBSHELL_CLOSE = "@@endsubshell@@"
_MARKERS = frozenset({_SUBSHELL_OPEN, _SUBSHELL_CLOSE})
_MODE = re.compile(r"^(?:[0-7]{3,4}|[ugoa]*[+=-][rwxXstugo]*(?:,[ugoa]*[+=-][rwxXstugo]*)*)$")
_RANGE = re.compile(r"^(-?\d+)\.\.(-?\d+)$")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
UNSCANNABLE = "unscannable"
WRITE = "write:"

# commands that write their target paths; the guard denies a target inside
# the repository (relative, or absolute under the repo root)
DESTINATION_WRITERS = frozenset({"cp", "install", "ln", "rsync"})
TARGET_WRITERS = frozenset({"mv", "rm", "rmdir", "tee", "truncate", "touch", "mkdir", "chmod", "chown"})
INPLACE_EDITORS = frozenset({"sed", "perl"})
# editors write the file they are pointed at, in batch mode (`ed -s`,
# `vim -es`, `emacs --batch`) as readily as interactively; with no file
# operand their script names the target, which the guard cannot see
EDITORS = frozenset({"ed", "ex", "vi", "vim", "nvim", "view", "emacs", "nano", "pico", "joe", "micro"})
_EDITOR_FAMILY = {"ed": "ed", "ex": "vim", "vi": "vim", "vim": "vim", "nvim": "vim", "view": "vim", "emacs": "emacs", "nano": "nano", "pico": "nano"}
# flags that take a value (not an operand), and flags that hand the editor
# a script or a command it runs (unscannable, but for an inert ex command)
_EDITOR_VALUE_FLAGS = {
    "vim": frozenset({"-u", "-U", "-i", "-T", "-t", "-q", "-w", "-W", "--servername", "--remote-send", "--remote-expr"}),
    "emacs": frozenset({"-t", "--terminal", "-u", "--user", "--chdir"}),
    "nano": frozenset({"-T", "-Y", "-r", "-Q", "-J", "--tabsize", "--syntax", "--fill", "--quotestr", "--guidestripe"}),
    "ed": frozenset({"-p"}),
}
_EDITOR_SCRIPT_FLAGS = {
    "vim": frozenset({"-S", "-s"}),
    "emacs": frozenset({"-l", "--load", "--script", "-f", "--funcall", "--eval", "--execute", "-L", "--directory"}),
    "nano": frozenset({"-s", "--speller", "-o", "--operatingdir", "-C", "--backupdir"}),
}
_EDITOR_COMMAND_FLAGS = frozenset({"-c", "--cmd"})
_INERT_FLAGS = frozenset({"--version", "--help", "-h", "-v", "-V"})
# ex commands that change nothing outside the editor
_INERT_EX = re.compile(
    r"^\s*:?\s*(?:se(?:t)?|setl(?:ocal)?|sy(?:n(?:tax)?)?|filet(?:ype)?|q(?:uit)?!?|qa(?:ll)?!?|quita(?:ll)?!?|cq!?|"
    r"echo(?:m(?:sg)?)?|noh(?:lsearch)?|colo(?:rscheme)?|redr(?:aw)?|ve(?:rsion)?)(?:\s|$)"
)


def _editor_targets(base: str, args: list[str]) -> list[str]:
    """What an editor invocation writes: its file operands; a script or
    command it is handed makes the target unreadable unless the command is
    inert. Only --version/--help writes nothing; no operand at all means
    the editor's own script says where."""
    family = _EDITOR_FAMILY.get(base, "")
    values = _EDITOR_VALUE_FLAGS.get(family, frozenset())
    scripts = _EDITOR_SCRIPT_FLAGS.get(family, frozenset())
    targets: list[str] = []
    k = 0
    while k < len(args):
        arg = args[k]
        flag, joined, attached = arg.partition("=")
        if arg.startswith("+") or (family == "vim" and flag in _EDITOR_COMMAND_FLAGS):
            command = arg[1:] if arg.startswith("+") else attached if joined else (args[k + 1] if k + 1 < len(args) else "")
            if not all(_INERT_EX.match(part) for part in command.split("|")):
                targets.append(_UNREADABLE_WORD)
            k += 1 if arg.startswith("+") or joined else 2
            continue
        if arg == "-":
            k += 1  # ed's old spelling of -s
            continue
        cluster = arg[1:] if arg.startswith("-") and not arg.startswith("--") else ""
        if arg.startswith("--") or len(cluster) == 1:
            # a long flag or a single short flag: `--load FILE`, `--load=FILE`, `-u NONE`
            if flag in scripts:
                targets.append(_UNREADABLE_WORD)
            k += 1 if joined or (flag not in scripts and flag not in values) else 2
            continue
        if cluster and not cluster.isalpha():
            # a value attached to a short flag: -lFILE, -p'*', -S/tmp/x
            if "-" + cluster[0] in scripts:
                targets.append(_UNREADABLE_WORD)
            k += 1
            continue
        if cluster:
            # a bundle of flag letters (-Nu NONE, -es): the first letter that takes
            # a value ends it, attached when letters follow, the next word when last
            silent = family == "vim" and "e" in cluster.lower()  # -es / -Es: s is silent, not a script
            advance = 1
            for i, letter in enumerate(cluster):
                short = "-" + letter
                if short in scripts and not (silent and letter == "s"):
                    targets.append(_UNREADABLE_WORD)
                    advance = 2 if i == len(cluster) - 1 else 1
                    break
                if short in values:
                    advance = 2 if i == len(cluster) - 1 else 1
                    break
            k += advance
            continue
        targets.append(arg)
        k += 1
    if args and all(arg in _INERT_FLAGS for arg in args):
        return []
    return targets or [_UNREADABLE_WORD]
ARCHIVERS = frozenset({"tar", "zip", "unzip", "gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz", "zstd", "unzstd"})
FETCHERS = frozenset({"curl", "wget"})
# formatters and fixers rewrite the files they are pointed at: some by
# default unless asked only to check or diff, others only when asked to write
_FORMATTERS_DEFAULT_WRITE = frozenset({"black", "isort", "rustfmt", "dprint", "stylua"})
_FORMATTERS_FLAG_WRITE = {
    "ruff": frozenset({"--fix", "--unsafe-fixes", "--fix-only"}),
    "prettier": frozenset({"--write", "-w"}),
    "eslint": frozenset({"--fix"}),
    "gofmt": frozenset({"-w"}),
    "goimports": frozenset({"-w"}),
    "shfmt": frozenset({"-w"}),
    "yapf": frozenset({"-i", "--in-place"}),
    "autopep8": frozenset({"-i", "--in-place"}),
    "clang-format": frozenset({"-i"}),
    "biome": frozenset({"--write", "--apply", "--fix"}),
}
FORMATTERS = _FORMATTERS_DEFAULT_WRITE | frozenset(_FORMATTERS_FLAG_WRITE)
_FORMATTER_CHECK_FLAGS = frozenset({"--check", "--diff", "--check-only", "--dry-run", "--list-different", "--code", "-c"})
_RUFF_VERBS = frozenset({"check", "format", "rule", "linter", "version", "config", "clean", "analyze", "server"})
_BIOME_VERBS = frozenset({"format", "lint", "check", "ci", "migrate", "rage", "version", "explain", "search", "init", "start", "stop"})


def _formatter_targets(base: str, args: list[str]) -> list[str]:
    """The files a formatter run rewrites: its path operands (ruff format
    with none formats the cwd); nothing when it only checks or diffs."""
    words = _operands(args)
    flags = {arg.split("=", 1)[0] for arg in args if arg.startswith("-")}
    if base == "ruff":
        verbs = [word for word in words if word in _RUFF_VERBS]
        words = [word for word in words if word not in _RUFF_VERBS]
        writes = bool(flags & _FORMATTERS_FLAG_WRITE["ruff"]) or ("format" in verbs and not flags & _FORMATTER_CHECK_FLAGS)
        return (words or ["."]) if writes else []
    if base == "biome":
        words = [word for word in words if word not in _BIOME_VERBS]
    if base in _FORMATTERS_DEFAULT_WRITE:
        return words if not flags & _FORMATTER_CHECK_FLAGS else []
    return words if flags & _FORMATTERS_FLAG_WRITE[base] else []
_REDIRECT = re.compile(r"^(>\||\d*<>|\d*>>?|&>>?)(.*)$")
_FD_DUP = re.compile(r"^\d*[<>]&(?:\d+|-)$")
# `<`, `<path`, `0<`, `0<path`; `<&3` and `<&-` are descriptor duplications
_INPUT_REDIRECT = re.compile(r"^\d*<(?![<>])(.*)$")
# a whole-word descriptor prefix stays attached to its operator (`2>`,
# `0<`, `2>&1`); `x2>f` is the word `x2` followed by `>f`
_FD_OPERATOR = re.compile(r"(?:(?<=\s)|^)(\d+)(<>|<&|<|>>|>&|>\||>)")
_FD_HIDE = str.maketrans({"<": "\x11", ">": "\x12", "&": "\x13", "|": "\x14"})
_FD_SHOW = str.maketrans({"\x11": "<", "\x12": ">", "\x13": "&", "\x14": "|"})


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list at shell separators."""
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in SEPARATORS or token in _MARKERS:
            if current:
                segments.append(current)
            current = []
            if token in _MARKERS:
                segments.append([token])
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _conditionally_blocked(subcommand: str, args: list[str]) -> bool:
    spec = CONDITIONAL_GIT.get(subcommand)
    if spec is None:
        return False
    mutating, listing = spec
    flags = {arg.split("=", 1)[0] for arg in args if arg.startswith("-")}
    words = _operands(args)
    if flags & mutating or set(words) & mutating:
        return True
    threshold = POSITIONAL_WRITE.get(subcommand)
    return threshold is not None and len(words) >= threshold and not (flags & listing)


_OPERATORS = (
    (re.compile(r"(?<!\\)(&&|\|\||;)"), r" \1 "),
    (re.compile(r"(?<![|>\\])\|(?!\|)"), " | "),
    (re.compile(r"(?<![&<>\\])&(?![&>])"), " & "),
    (re.compile(r"(?<=[^\s&<>|\\])(>>?|>\|)"), r" \1"),
    (re.compile(r"(?<=[^\s&<>|\\])(&>>?)"), r" \1"),
    (re.compile(r"(?<!\\)<<<"), " <<< "),
    (re.compile(r"(?<=[^\s<\\])<<(?![<])"), " <<"),  # `bash<<EOF`: the heredoc operator glued to the word
    (re.compile(r"(?<=[^\s<>&\\])(<>|<(?![<&>]))"), r" \1"),
)


def _split_alternatives(inner: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in inner:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return parts


def _brace_expand(word: str) -> list[str]:
    """The words bash makes of ``word`` by brace expansion (``a{b,c}`` and
    ``{1..3}``); a word with no expansion, ``{}`` included, is returned as
    is. Capped so a pathological word cannot explode."""
    i = word.find("{")
    while i != -1:
        depth = 0
        j = i
        while j < len(word):
            if word[j] == "{":
                depth += 1
            elif word[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(word):
            return [word]
        inner = word[i + 1 : j]
        alternatives = _split_alternatives(inner)
        span = _RANGE.match(inner)
        if span:
            lo, hi = int(span.group(1)), int(span.group(2))
            step = 1 if hi >= lo else -1
            alternatives = [str(n) for n in range(lo, hi + step, step)][:64]
        if len(alternatives) > 1:
            out: list[str] = []
            for alternative in alternatives:
                out.extend(_brace_expand(word[:i] + alternative + word[j + 1 :]))
                if len(out) > 256:
                    return out[:256]
            return out
        i = word.find("{", i + 1)
    return [word]


def _tokens(text: str) -> list[str]:
    """Shell words, with control operators padded so `log;git` and `&&git`
    split the way the shell splits them, and braces expanded the way the
    shell expands them."""
    text = _FD_OPERATOR.sub(lambda m: m.group(1) + m.group(2).translate(_FD_HIDE), text)
    for pattern, replacement in _OPERATORS:
        text = pattern.sub(replacement, text)
    try:
        words = shlex.split(text, posix=True)
    except ValueError:
        words = text.split()
    return [expanded.translate(_FD_SHOW) for word in words for expanded in _brace_expand(word)]


def _prefix(token: str) -> bool:
    """A token that leaves the following token in command position."""
    return bool(
        _ASSIGNMENT.match(token)
        or token.rsplit("/", 1)[-1] in WRAPPERS
        or token.startswith("-")
        or _DURATION.match(token)
    )


def _unreadable(token: str) -> bool:
    """Text the shell would expand or read before running it."""
    return "$" in token or "`" in token or _SUBST_MARK in token


_INTERPRETER_GIT = re.compile(
    r"""(?:["']git["']\s*,\s*["']|\bgit\s+)(""" + "|".join(sorted(BLOCKED_GIT)) + r""")\b"""
)
# a program line that hands text to a child process, by interpreter family
_CALL_SHELL_OUT = re.compile(
    r"\b(?:subprocess\.\w+|os\.system|os\.popen|Popen|check_output|check_call|\brun|\bcall|system|"
    r"exec(?:v|ve|vp|vpe|l|le|lp|lpe|Sync|File|FileSync)?|spawn(?:l|le|lp|lpe|v|ve|vp|vpe|Sync)?|popen)\s*\("
)
_BACKTICK_SHELL_OUT = re.compile(r"`")
_APPLESCRIPT_SHELL_OUT = re.compile(r"do shell script\s*\"")
_SHELL_OUT_BY_INTERPRETER = {
    "osascript": (_APPLESCRIPT_SHELL_OUT,),
    "perl": (_CALL_SHELL_OUT, _BACKTICK_SHELL_OUT),
    "ruby": (_CALL_SHELL_OUT, _BACKTICK_SHELL_OUT),
}
_COMMENT = re.compile(r"^\s*(#|//|--)")
# a program line that writes, moves, or deletes a file by a spelled-out path
# (Node's fs verbs carry an optional Sync suffix; its rm and cp are ordinary
# words elsewhere, so they count, however reached, in a program that uses
# the fs module)
_WRITE_API = re.compile(
    r"\b(?:open|write_text|write_bytes|writeFile|appendFile|copyfile|copyFile|copy2?|copytree|move|rmtree|"
    r"remove|unlink|rename|replace|rmdir|makedirs|mkdir|touch|truncate|symlink|link|chmod|chown|"
    r"file_put_contents|File\.(?:write|open|delete|rename)|"
    r"FileUtils\.(?:rm\w*|mv|cp\w*|touch|mkdir\w*|install|ln\w*|chmod\w*|chown\w*|remove\w*|rmdir|symlink|copy\w*|move)|"
    r"write)(?:Sync)?\s*(?:\(|(?=['\"]|[A-Z_][A-Z0-9_]*\s*,))"  # ruby and perl: `File.write 'x'`, `open FH, ">x"`
)
_NODE_FS_VERB = re.compile(r"\b(?:rm|cp)(?:Sync)?\s*\(")
_NODE_FS = re.compile(r"""require\s*\(\s*['"](?:node:)?fs(?:/promises)?['"]\s*\)|from\s+['"](?:node:)?fs(?:/promises)?['"]|\bfs\.""")
_STRING_LITERAL = re.compile(r"""(['"])((?:\\.|(?!\1).)*?)\1""")
_MODE_CHARS = frozenset("rwaxbtU+")
_WRITE_MODE_CHARS = frozenset("wax+")


def _is_write_mode(literal: str) -> bool:
    """An ``open()`` mode string that can write, in any character order."""
    return 0 < len(literal) <= 4 and set(literal) <= _MODE_CHARS and bool(set(literal) & _WRITE_MODE_CHARS)


def _split_semicolons(statement: str) -> list[str]:
    """One logical line split at `;` outside brackets and string literals."""
    parts: list[str] = []
    current = ""
    depth = 0
    quote = ""
    k = 0
    while k < len(statement):
        ch = statement[k]
        if quote:
            current += ch
            if ch == "\\" and k + 1 < len(statement):
                current += statement[k + 1]
                k += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            current += ch
        elif ch in "([{":
            depth += 1
            current += ch
        elif ch in ")]}":
            depth -= 1
            current += ch
        elif ch == ";" and depth <= 0:
            parts.append(current)
            current = ""
        else:
            current += ch
        k += 1
    parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def _indented_parts(current: str) -> list[str]:
    """One logical line split at `;`, the first part keeping the line's
    indentation (the block it sits in)."""
    parts = _split_semicolons(current)
    if parts:
        parts[0] = current[: len(current) - len(current.lstrip())] + parts[0]
    return parts


def _statements(text: str) -> list[str]:
    """Program text as logical statements: physical lines joined while a
    bracket is open, `;`-separated statements split, comment lines dropped;
    each statement keeps the indentation of its first physical line."""
    statements: list[str] = []
    current = ""
    depth = 0
    for line in text.split("\n"):
        if not current and _COMMENT.match(line):
            continue
        current = (current + " " + line.strip()) if current else line
        depth += line.count("(") + line.count("[") + line.count("{")
        depth -= line.count(")") + line.count("]") + line.count("}")
        if depth <= 0:
            statements.extend(_indented_parts(current))
            current = ""
            depth = 0
    if current:
        statements.extend(_indented_parts(current))
    return statements


AWK_FAMILY = frozenset({"awk", "gawk", "nawk", "mawk"})
_AWK_SYSTEM = re.compile(r"system\s*\(([^()]*)\)")
_AWK_REDIRECT = re.compile(r""">{1,2}\s*((?:(?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*')\s*)+)""")
_AWK_PIPE = re.compile(r"""\|&?\s*((?:(?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*')\s*)+)""")


def _awk_expression(expression: str) -> str | None:
    """The string an awk expression spells out: adjacent literals joined;
    None when a variable or call takes part in it."""
    literals = [m.group(2) for m in _STRING_LITERAL.finditer(expression)]
    remainder = _STRING_LITERAL.sub("", expression).replace(" ", "").replace("\t", "")
    if remainder:
        return None
    return "".join(literals)
_SED_EXEC = re.compile(r"(?:^|;|\n)\s*\d*,?\d*\s*e(?:\s+([^;\n]*))?(?=;|\n|$)")
_SED_SUBST_EXEC = re.compile(r"(?:^|;|\n)\s*\d*,?\d*\s*s(.)(?:\\.|(?!\1).)*\1(?:\\.|(?!\1).)*\1[gpImM\d]*e")
_SED_WRITE = re.compile(r"(?:^|;|\n)\s*\d*,?\d*\s*[wW]\s+([^;\n]+)")
_SED_SUBST_WRITE = re.compile(r"(?:^|;|\n)\s*\d*,?\d*\s*s(.)(?:\\.|(?!\1).)*\1(?:\\.|(?!\1).)*\1[gpImM\d]*w\s+([^;\n]+)")


def _awk_hits(program: str, repo: str, cwd: str | None, env: dict[str, str]) -> list[str]:
    """What an awk program hands outside: `system("…")` commands, output
    redirections to a spelled-out path, and output piped to a command."""
    hits: list[str] = []
    for match in _AWK_SYSTEM.finditer(program):
        command = _awk_expression(match.group(1))
        hits.extend(blocked_subcommands(command, repo, cwd, env) if command is not None else [UNSCANNABLE])
    for match in _AWK_REDIRECT.finditer(program):
        target = _awk_expression(match.group(1))
        verdict = _in_repo(target, repo, cwd, env) if target is not None else None
        if verdict is None:
            hits.append(UNSCANNABLE)
        elif verdict:
            hits.append(WRITE + target)
    for match in _AWK_PIPE.finditer(program):
        command = _awk_expression(match.group(1))
        hits.extend((blocked_subcommands(command, repo, cwd, env) or [UNSCANNABLE]) if command is not None else [UNSCANNABLE])
    bare = re.sub(r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*')", "", program)
    if re.search(r">{1,2}\s*[A-Za-z_]", bare) or re.search(r"\|&?\s*[A-Za-z_]", bare):
        hits.append(UNSCANNABLE)  # an output target named by a variable
    return hits


def _sed_hits(program: str, repo: str, cwd: str | None, env: dict[str, str]) -> list[str]:
    """What a sed program hands outside: the `e` command and `s///e` flag run
    text as a shell command, `w file` and `s///w file` write a file."""
    hits: list[str] = []
    for match in _SED_EXEC.finditer(program):
        command = (match.group(1) or "").strip()
        hits.extend(blocked_subcommands(command, repo, cwd, env) if command else [UNSCANNABLE])
    if _SED_SUBST_EXEC.search(program):
        hits.append(UNSCANNABLE)  # the pattern space becomes the command
    for match in list(_SED_WRITE.finditer(program)) + list(_SED_SUBST_WRITE.finditer(program)):
        target = match.group(match.lastindex).strip()
        verdict = _in_repo(target, repo, cwd, env)
        if verdict is None:
            hits.append(UNSCANNABLE)
        elif verdict:
            hits.append(WRITE + target)
    return hits


def _interpreter_hits(
    text: str, interpreter: str, repo: str | None = None, cwd: str | None = None, env: dict[str, str] | None = None
) -> list[str]:
    """What program text hands to the outside world: blocked git commands a
    line spells out while calling out to a shell or child process, and file
    writes whose spelled-out path lands in the repository; a shell-out or
    write whose command or path is not spelled out is unscannable. Comments,
    and the same phrases as inert data on a line without such a call, pass."""
    patterns = _SHELL_OUT_BY_INTERPRETER.get(interpreter, (_CALL_SHELL_OUT,))
    hits: list[str] = []
    statements = _statements(text)
    bound: dict[str, str] = {}  # built statement by statement: a use sees only what was bound before it
    qualifiers, bare, write_aliases, defined = _program_names(statements)
    renamed = sorted(bare - {"run", "call"})  # `from subprocess import run as r`: r() shells out
    if renamed:
        patterns = patterns + (re.compile(r"\b(?:" + "|".join(map(re.escape, renamed)) + r")\s*\("),)
    write_apis = [_WRITE_API]
    if _NODE_FS.search(text):
        write_apis.append(_NODE_FS_VERB)
    if write_aliases:
        write_apis.append(re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(write_aliases))) + r")\s*\("))
    for line in statements:
        for match in _WITH_BINDING.finditer(line):
            bound[match.group(3)] = match.group(2)  # `with open('x') as f: f.write(…)`: bound for its own body
        for match in _WITH_MEMORY_BINDING.finditer(line):
            bound[match.group(1)] = "/dev/null"
        quoted_spans = [(m.start(), m.end()) for m in _STRING_LITERAL.finditer(line)]
        calls_out = any(
            not any(start < m.start() < end for start, end in quoted_spans)
            and not _is_declaration(m, line)
            and _is_shell_out(m, line, qualifiers, bare)
            for pattern in patterns
            for m in pattern.finditer(line)
        )
        if calls_out:
            line_hits = [match.group(1) for match in _INTERPRETER_GIT.finditer(line)]
            if repo is not None:
                literals = [match.group(2) for match in _STRING_LITERAL.finditer(line)]
                candidates = [" ".join(literals)] + literals if len(literals) > 1 else literals
                candidates += _shell_out_commands(line, patterns, qualifiers, bare)
                for candidate in candidates:
                    line_hits.extend(blocked_subcommands(candidate, repo, cwd, env))
            hits.extend(dict.fromkeys(line_hits))
        if repo is None:
            continue
        quoted = [(m.start(), m.end()) for m in _STRING_LITERAL.finditer(line)]
        for match in (m for api in write_apis for m in api.finditer(line)):
            if any(start < match.start() < end for start, end in quoted) or _is_declaration(match, line):
                continue  # a call quoted as data, or a function's own declaration
            name = match.group(0).rstrip("( \t").rsplit(".", 1)[-1]
            if name in defined and name not in write_aliases and not _QUALIFIER.search(line[: match.start()]):
                continue  # the program's own function of that name
            if not _writes_a_file(match, line, write_aliases.get(name, name)):
                continue
            for target in _write_call_targets(match, line, bound, write_aliases):
                verdict = _in_repo(target, repo, cwd, env or {}) if target and target != _TEXT else None
                if verdict is None:
                    hits.append(UNSCANNABLE)  # a path the guard cannot read
                    break
                if verdict:
                    hits.append(WRITE + target)
                    break
        _bind_statement(line, bound)  # this statement's bindings apply to what follows
    return list(dict.fromkeys(hits))  # one chained call can match twice; report it once


# `name = Path('x')` / `name = 'x'` bindings inside program text, so a later
# `name.write_text(...)` knows its receiver
_PATH_BINDING = re.compile(
    r"""^\s*([A-Za-z_]\w*)\s*=\s*(?:(?:pathlib\.)?Path\s*\(\s*|(?:io\.)?open\s*\(\s*)?(['"])([^'"]+)\2"""
    r"""\s*(?:,[^)]*)?\)?\s*(?:\.(?:resolve|expanduser|absolute)\(\s*\))*\s*$"""  # the whole right-hand side
)
_WITH_BINDING = re.compile(r"""\bwith\s+(?:io\.)?open\s*\(\s*(['"])([^'"]+)\1[^)]*\)\s*as\s+([A-Za-z_]\w*)""")
_WITH_MEMORY_BINDING = re.compile(
    r"\bwith\s+(?:tempfile\.)?(?:TemporaryDirectory|NamedTemporaryFile|TemporaryFile|SpooledTemporaryFile)\s*\([^)]*\)\s*as\s+([A-Za-z_]\w*)"
)
# a name bound to something that is never a repository file: an in-memory
# buffer or a temporary file; /dev/null stands in for "outside the repo"
_MEMORY_BINDING = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*=\s*(?:io\.|tempfile\.)?"
    r"(?:StringIO|BytesIO|NamedTemporaryFile|TemporaryFile|SpooledTemporaryFile|TemporaryDirectory|mkstemp|mkdtemp|gettempdir)\s*\("
)


_TEXT = "<text>"  # what a name bound to a string result stands for
_ASSIGNMENT_RHS = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")


def _bind_statement(statement: str, bound: dict[str, str]) -> None:
    """Apply one statement's bindings to ``bound``: `p = Path('x')`,
    `p = 'x'`, `f = open('x')`, `with open('x') as f` bind a spelled-out
    path; a buffer or temporary file binds to /dev/null; a string result
    (`s = Path('x').read_text()`) binds to the text marker, whose methods
    are str's. Any other assignment to the name unbinds it. The guard does
    not model control flow: a rebinding inside any block (an indented
    statement, under an `if`, `else`, `try`, `def`, or loop) may or may
    not be the live one, so the name becomes unreadable; a top-level
    rebinding runs in sequence and is followed."""
    indent = len(statement) - len(statement.lstrip())

    def rebind(name: str, value: str | None) -> None:
        if name in bound and indent > 0:
            bound[name] = ""  # which binding is live depends on control flow
        elif value is None:
            bound.pop(name, None)  # rebound to something the guard cannot read
        else:
            bound[name] = value

    match = _PATH_BINDING.match(statement)
    if match:
        rebind(match.group(1), match.group(3))
        return
    match = _ASSIGNMENT_RHS.match(statement)
    if match and _STR_RESULT.search(match.group(2)):
        rebind(match.group(1), _TEXT)
        return
    match = _MEMORY_BINDING.match(statement)
    if match:
        rebind(match.group(1), "/dev/null")
        return
    for match in _WITH_BINDING.finditer(statement):
        rebind(match.group(3), match.group(2))
    for match in _WITH_MEMORY_BINDING.finditer(statement):
        rebind(match.group(1), "/dev/null")
    match = _ASSIGNMENT_RHS.match(statement)
    if match:
        value = _argument_value(match.group(2), bound)  # `p = Path(tmp) / 'x'`: assembled from what is bound
        if value is not None:
            rebind(match.group(1), value)
        elif match.group(1) in bound:
            rebind(match.group(1), None)


def _bindings(statements: list[str]) -> dict[str, str]:
    """The bindings in force after every statement (for callers that need
    the final table; the scan applies them in program order)."""
    bound: dict[str, str] = {}
    for statement in statements:
        _bind_statement(statement, bound)
    return bound


_METHOD_WRITERS = frozenset(
    {"write_text", "write_bytes", "unlink", "touch", "mkdir", "rmdir", "chmod", "chown", "truncate",
     "rename", "replace", "symlink", "link", "write", "open", "delete"}
)
_DESTINATION_SECOND = frozenset(
    {"copy", "copy2", "copyfile", "copytree", "cp", "cp_r", "cp_lr", "copy_file", "copy_entry", "move", "mv", "rename",
     "replace", "symlink", "link", "ln", "ln_s", "ln_sf", "install"}
)
_MODULE_RECEIVER = re.compile(r"\b(?:require|import)\s*\(\s*['\"][^'\"]*['\"]\s*\)$")
# identifiers before the dot that name a module, whose functions take the
# path as an argument, and streams, which are not files
_MODULE_RECEIVERS = frozenset(
    {"os", "path", "shutil", "pathlib", "io", "fs", "fsp", "promises", "File", "FileUtils", "Dir", "IO", "sys",
     "tempfile", "codecs", "gzip", "bz2", "lzma", "zipfile", "tarfile", "posix", "nt"}
)
_STREAM_RECEIVERS = frozenset({"stdout", "stderr", "stdin"})
_SOURCE_REMOVED = frozenset({"move", "mv", "rename", "replace"})


def _call_span(match: re.Match, line: str) -> str:
    """The argument text of the call ``match`` opens, up to its closing paren."""
    depth = 0
    start = match.end() - 1
    for k in range(start, len(line)):
        if line[k] == "(":
            depth += 1
        elif line[k] == ")":
            depth -= 1
            if depth == 0:
                return line[start + 1 : k]
    return line[start + 1 :]


def _split_arguments(span: str) -> list[str]:
    """The top-level comma-separated pieces of a call's argument text."""
    depth = 0
    quote = ""
    current = ""
    parts: list[str] = []
    for ch in span:
        if quote:
            current += ch
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            current += ch
        elif ch in "([{":
            depth += 1
            current += ch
        elif ch in ")]}":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return parts


_KEYWORD_ARGUMENT = re.compile(r"([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$")
_UNREADABLE_WORD = "$UNREADABLE"
# a `{}` / `{name}` / `%s` field in text handed to a shell
_FORMAT_FIELD = re.compile(r"\{[^{}]*\}|%[-+ 0-9.#]*[sdifrxXeEgGoc]")


def _expression_words(expression: str, argv: bool = False) -> str:
    """The command text an expression spells out: string literals as
    written (a format field as a word the shell would expand), anything
    else as such a word; a list or tuple element by element, each literal
    quoted as one argv word."""
    expression = expression.strip()
    if expression[:1] in "[(" and expression[-1:] in "])":
        return " ".join(_expression_words(e, argv=True) for e in _split_arguments(expression[1:-1]) if e.strip())
    if expression == "sys.executable":
        return "python3"
    words: list[str] = []
    end = 0
    for match in _STRING_LITERAL.finditer(expression):
        gap = expression[end : match.start()]
        prefix = re.search(r"[A-Za-z]*$", gap).group(0)  # f, rb, … before the quote
        if gap[: len(gap) - len(prefix)].strip(" \t+()"):
            words.append(_UNREADABLE_WORD)
        value = _FORMAT_FIELD.sub(_UNREADABLE_WORD, match.group(2))
        words.append(shlex.quote(value) if argv and _UNREADABLE_WORD not in value else value)
        end = match.end()
    if expression[end:].strip(" \t+()"):
        words.append(_UNREADABLE_WORD)
    return "".join(words)  # adjacent pieces form one word: `'--author=' + who` is --author=$UNREADABLE


_SUBPROCESS_ALIAS = re.compile(r"\bimport\s+subprocess\s+as\s+([A-Za-z_]\w*)")
_QUALIFIER = re.compile(r"([A-Za-z_]\w*)\s*\.\s*$")
# how a program binds names: `from M import a as b` (a list joined by
# _statements), `import {a as b} from 'M'`, `const {a: b} = require('M')`,
# and a plain `b = M.a` or `b = a`, followed as a chain
_FROM_IMPORT = re.compile(r"\bfrom\s+([\w.]+)\s+import\s+\(?\s*([^)]*?)\s*\)?\s*$")
_NODE_IMPORT = re.compile(r"\bimport\s*\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]")
_NODE_DESTRUCTURE = re.compile(r"\{([^}]*)\}\s*=\s*(?:await\s+)?((?:require|import)\s*\(\s*['\"][^'\"]*['\"]\s*\)|[\w$.]+)")
_ASSIGNED_ALIAS = re.compile(
    r"^\s*(?:const|let|var)?\s*([A-Za-z_$][\w$]*)\s*=\s*((?:(?:require|import)\s*\([^)]*\)|[\w$]+)(?:\.[\w$]+)*\.)?([A-Za-z_]\w*)\s*;?\s*$"
)
_DEFINED = re.compile(
    r"\b(?:function|def)\s+(\w+)\s*\(|\b(?:const|let|var)\s+(\w+)\s*=\s*(?:\(|async\b|function\b|[\w$]+\s*=>)|^\s*(\w+)\s*=\s*lambda\b",
    re.M,
)
SUBPROCESS_FUNCTIONS = frozenset({"run", "call", "check_call", "check_output", "Popen", "getoutput", "getstatusoutput"})
# functions that run a command wherever they are imported from: os's and
# Node child_process's
SHELL_FUNCTIONS = frozenset(
    {"system", "popen", "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe", "spawnl", "spawnle",
     "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp",
     "exec", "execSync", "execFile", "execFileSync", "spawn", "spawnSync", "fork"}
)


def _fs_source(source: str) -> bool:
    """Whether a Node binding source is the fs module or its promises API."""
    return bool(
        re.fullmatch(r"(?:node:)?fs(?:/promises)?|fs(?:\.promises)?|fsp|promises", source)
        or re.search(r"""(?:require|import)\s*\(\s*['"](?:node:)?fs(?:/promises)?['"]""", source)
    )


def _bindings_in(statements: list[str]) -> list[tuple[str, str, str]]:
    """(alias, original, source) for every name the program binds, in
    program order: import renames, Node imports and destructuring, and
    plain assignments of one name to another."""
    out: list[tuple[str, str, str]] = []
    for statement in statements:
        imported = _FROM_IMPORT.match(statement.strip())
        if imported:
            for item in imported.group(2).split(","):
                original, _, alias = item.strip().partition(" as ")
                if original.strip():
                    out.append((alias.strip() or original.strip(), original.strip(), imported.group(1)))
            continue
        for names, source in _NODE_IMPORT.findall(statement):
            for item in names.split(","):
                original, _, alias = item.strip().partition(" as ")
                if original.strip():
                    out.append((alias.strip() or original.strip(), original.strip(), source))
        for names, source in _NODE_DESTRUCTURE.findall(statement):
            for item in names.split(","):
                original, _, alias = item.strip().partition(":")
                if original.strip():
                    out.append((alias.strip() or original.strip(), original.strip(), source))
        assigned = _ASSIGNED_ALIAS.match(statement)
        if assigned:
            out.append((assigned.group(1), assigned.group(3), (assigned.group(2) or "").rstrip(".")))
    return out


def _program_names(statements: list[str]) -> tuple[frozenset[str], frozenset[str], dict[str, str], frozenset[str]]:
    """What a program calls things by: the names it uses for the subprocess
    module; the bare names bound to its functions (imported, renamed, or
    assigned); other names bound to a write API, mapped to that API; and
    the functions it defines itself and never binds from elsewhere (an
    import of the same name, before or after, may shadow the definition;
    the guard does not model order and treats either as not its own)."""
    program = "\n".join(statements)
    bindings = _bindings_in(statements)
    qualifiers = {"subprocess", *_SUBPROCESS_ALIAS.findall(program)}
    uses_fs = _NODE_FS.search(program) is not None
    defined = {name for groups in _DEFINED.findall(program) for name in groups if name} - {alias for alias, _, _ in bindings}
    shell_bare: set[str] = set()
    write_aliases: dict[str, str] = {}

    def bind(alias: str, original: str, source: str) -> None:
        """``alias`` now names what ``source.original`` named; a chain of
        plain bindings is followed link by link."""
        if not source:  # a plain `b = a`: whatever a already stood for
            if original in qualifiers:
                qualifiers.add(alias)
            elif original in shell_bare or original in SHELL_FUNCTIONS:
                shell_bare.add(alias)
            elif original in write_aliases or (uses_fs and original not in defined and _NODE_FS_VERB.fullmatch(original + "(")):
                write_aliases[alias] = write_aliases.get(original, original)
            elif alias != original and original not in defined and _WRITE_API.fullmatch(original + "("):
                write_aliases[alias] = original
            return
        from_subprocess = source.split(".")[0] in qualifiers
        if from_subprocess and original == "*":
            shell_bare.update({"run", "call"})
        elif (from_subprocess and original in SUBPROCESS_FUNCTIONS) or original in SHELL_FUNCTIONS:
            shell_bare.add(alias)
        elif alias != original and _NODE_FS_VERB.fullmatch(original + "("):
            if _fs_source(source):
                write_aliases[alias] = original
        elif alias != original and _WRITE_API.fullmatch(original + "("):
            write_aliases[alias] = original

    for alias, original, source in bindings:
        bind(alias, original, source)
    return frozenset(qualifiers), frozenset(shell_bare), write_aliases, frozenset(defined)


_DECLARATION_HEAD = re.compile(r"\b(?:def|function)\s+$")


def _is_declaration(match: re.Match, line: str) -> bool:
    """Whether a call-shaped match is a function's own declaration
    (`def remove(item):`, `function unlink(a) {`)."""
    return _DECLARATION_HEAD.search(line[: match.start()]) is not None


def _is_shell_out(match: re.Match, line: str, qualifiers: frozenset[str], bare: frozenset[str]) -> bool:
    """Whether a shell-out pattern match hands text to a child process: a
    run/call only through subprocess (by module name or alias, or imported
    bare); asyncio.run(…) and a program's own run() are not shell-outs."""
    head, _, name = match.group(0).rstrip("( \t").rpartition(".")
    if name not in ("run", "call"):
        return True
    if not head:
        qualifier = _QUALIFIER.search(line[: match.start()])
        head = qualifier.group(1) if qualifier else ""
    return head in qualifiers if head else name in bare


def _shell_out_commands(line: str, patterns: tuple[re.Pattern, ...], qualifiers: frozenset[str], bare: frozenset[str]) -> list[str]:
    """The command each shell-out call on ``line`` runs, reconstructed from
    its positional arguments. Backtick and AppleScript forms carry no call
    span and are covered by the literal scan alone."""
    quoted = [(m.start(), m.end()) for m in _STRING_LITERAL.finditer(line)]
    commands: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(line):
            if not match.group(0).endswith("(") or any(start < match.start() < end for start, end in quoted):
                continue
            if _is_declaration(match, line) or not _is_shell_out(match, line, qualifiers, bare):
                continue
            positional = [
                part for part in _split_arguments(_call_span(match, line))
                if part.strip() and not (_KEYWORD_ARGUMENT.match(part.strip()) and not part.strip().startswith(("'", '"')))
            ]
            if positional:
                commands.append(" ".join(_expression_words(part) for part in positional))
    return commands


def _call_arguments(span: str, bound: dict[str, str]) -> list[str]:
    """Positional path arguments of one call: string literals, and bare
    names bound to a path earlier in the program. A positional the guard
    cannot read keeps its slot as "", so a destination never shifts."""
    values: list[str] = []
    keywords: dict[str, str] = {}
    for part in _split_arguments(span):
        part = part.strip()
        keyword = _KEYWORD_ARGUMENT.match(part)
        if keyword and not part.startswith(("'", '"')):
            value = _argument_value(keyword.group(2).strip(), bound)
            if value is not None:
                keywords[keyword.group(1)] = value
            continue
        values.append(_argument_value(part, bound) or "")
    # keyword paths take the slot their name implies
    for name in ("file", "path", "filename", "filepath", "fname", "src", "source", "name", "pathname"):
        if name in keywords and not values:
            values.append(keywords[name])
            break
    for name in ("dst", "dest", "destination", "target", "new", "newpath", "dst_path"):
        if name in keywords:
            values = values[:1] + [keywords[name]] if values else ["", keywords[name]]
            break
    return values


# an identifier that is not an attribute and not a call
_NAME_IN_EXPRESSION = re.compile(r"(?<![\w.])([A-Za-z_]\w*)(?!\s*\()")


def _joined(values: list[str]) -> str:
    """The path a sequence of components spells out under join semantics:
    the last absolute component replaces what came before it, and what
    follows it is appended."""
    start = max((k for k, value in enumerate(values) if value.startswith("/")), default=0)
    return "/".join(values[start:])


def _argument_value(text: str, bound: dict[str, str]) -> str | None:
    """The path an argument spells out: a literal, a name bound to a path
    earlier in the program, or those inside a wrapper such as ``Path('x')``,
    ``str(Path('x'))``, ``os.path.join('x', 'y')``, or ``Path('x') / 'y'``,
    where the last absolute component replaces what came before it and the
    first component leads otherwise. A formatted literal (an f-string, a
    `{}` or `%s` field) is not spelled out: "". None when nothing is."""
    if re.fullmatch(r"[A-Za-z_]\w*", text):
        return bound.get(text)
    candidates: list[tuple[int, str]] = []
    spans: list[tuple[int, int]] = []
    for match in _STRING_LITERAL.finditer(text):
        prefix = re.search(r"[A-Za-z]*$", text[: match.start()]).group(0)
        if "f" in prefix.lower() or _FORMAT_FIELD.search(match.group(2)):
            return ""
        candidates.append((match.start(), match.group(2)))
        spans.append((match.start(), match.end()))
    for match in _NAME_IN_EXPRESSION.finditer(text):
        if match.group(1) in bound and not any(start < match.start() < end for start, end in spans):
            candidates.append((match.start(), bound[match.group(1)]))
    if not candidates:
        return None
    return _joined([value for _, value in sorted(candidates)])


# a receiver chain that ends in a call returning text: its methods are str's
_STR_RESULT = re.compile(
    r"\.(?:read_text|read|decode|strip|lstrip|rstrip|lower|upper|format|as_posix|as_uri|getvalue|splitlines)\s*\([^()]*\)\s*$"
    r"|(?<!path)\.join\s*\([^()]*\)\s*$"  # str.join, not os.path.join
    r"|\.(?:name|stem|suffix|text)\s*$|\bstr\s*\([^()]*\)\s*$"
)
# the literals in a receiver chain that are path components: the first
# argument of a path constructor or join, and the operands of `/` (a
# with_name/with_suffix argument replaces the tail and is not one; the
# head still decides where the path lands)
_RECEIVER_COMPONENT = re.compile(
    r"""(?:\b(?:Path|PurePath|PurePosixPath|PureWindowsPath|PosixPath|WindowsPath|joinpath|open)"""
    r"""\s*\(\s*|/\s*)(['"])((?:\\.|(?!\1).)*?)\1"""
)


def _write_call_targets(
    match: re.Match, line: str, bound: dict[str, str] | None = None, aliases: dict[str, str] | None = None
) -> list[str]:
    """The path literals one write call would touch, taken from the argument
    position that API uses: a method's receiver (``Path('x').write_text``, or
    a name bound to a path earlier in the program), a copy's destination,
    everything else's first argument."""
    name = match.group(0).rstrip("( \t").rsplit(".", 1)[-1]
    name = (aliases or {}).get(name, name)
    name = (name[:-4] if name.endswith("Sync") else name).lower()  # renameSync is rename, copyFile is copyfile
    span_literals = _call_arguments(_call_span(match, line), bound or {})
    head = line[: match.start()].rstrip()
    receiver_end = head[:-1].rstrip() if head.endswith(".") else ""
    # a method on an expression (`Path('x').write_text(`) has `)`/`]`/a quote
    # before the dot, a bound name has an identifier; `os.unlink(` names a
    # module. A receiver the guard cannot read is an unreadable target.
    receiver_literal: str | None = None
    if receiver_end and (receiver_end[-1] in "'\"" or _STR_RESULT.search(receiver_end)):
        return []  # a string's methods (`'a'.replace(`, `Path('x').read_text().replace(`) write nothing
    if receiver_end and receiver_end[-1] in ")]'\"":
        before = [m.group(2) for m in _RECEIVER_COMPONENT.finditer(receiver_end)]
        if _MODULE_RECEIVER.search(receiver_end):
            before = []  # `require('fs').unlinkSync(`: the receiver names a module
        elif not before and bound:
            before = [bound[m.group(1)] for m in _NAME_IN_EXPRESSION.finditer(receiver_end) if m.group(1) in bound]
        receiver_literal = _joined(before) if before else None  # `Path('/a') / 'b'`: the joined path
        if receiver_literal is None and name in _METHOD_WRITERS and receiver_end[-1] == ")":
            return [""]  # `Path(p).write_text(` with p unknown, or a call result
    elif receiver_end:
        identifier = re.search(r"([A-Za-z_]\w*)$", receiver_end)
        if identifier and bound and bound.get(identifier.group(1)) == _TEXT:
            return []  # a name bound to text: str's methods
        if identifier and bound and identifier.group(1) in bound:
            receiver_literal = bound[identifier.group(1)]
        elif identifier and identifier.group(1) in _STREAM_RECEIVERS:
            return []  # sys.stdout.write(: a stream, not a file
        elif identifier and name in _METHOD_WRITERS and identifier.group(1) not in _MODULE_RECEIVERS:
            return [""]  # `p.rename(` with p unknown
    if receiver_literal is not None and name in _METHOD_WRITERS:
        targets = [receiver_literal]
        if name in ("rename", "replace", "symlink", "link"):
            targets += span_literals[:1]
    elif name == "open" and span_literals and not span_literals[0] and _PERL_WRITE_MODE.match(
        next((value for value in span_literals[1:] if value), "")
    ):
        # perl: open(FH, ">path") / open($fh, ">", "path"): the target follows the handle
        rest = [value for value in span_literals[1:] if value]
        stripped = re.sub(r"^\s*(?:>>?|\+[<>]|\|)\s*", "", rest[0])
        targets = [stripped or (rest[1] if len(rest) > 1 else "")]
    elif name in _DESTINATION_SECOND:
        targets = span_literals[1:2]
        if name in _SOURCE_REMOVED:
            targets += span_literals[:1]
    else:
        targets = span_literals[:1]
    return targets  # "" is a slot the guard cannot read


_PERL_WRITE_MODE = re.compile(r"^\s*(?:>>?|\+[<>]|\|)")


def _writes_a_file(match: re.Match, line: str, name: str | None = None) -> bool:
    """Whether one write-API call really writes: ``open`` only with a write
    mode among its own arguments (``os.open`` unless its flags are exactly
    O_RDONLY), every other listed API always."""
    if not (name or match.group(0)).startswith("open"):
        return True
    if line[: match.start()].rstrip().endswith("os."):
        flags = _call_span(match, line)
        return not (re.search(r"\bO_RDONLY\b", flags) and not re.search(r"\bO_(?:WRONLY|RDWR|CREAT|APPEND|TRUNC)\b", flags))
    depth = 0
    start = match.end() - 1
    for k in range(start, len(line)):
        if line[k] == "(":
            depth += 1
        elif line[k] == ")":
            depth -= 1
            if depth == 0:
                arguments = line[start + 1 : k]
                break
    else:
        arguments = line[start + 1 :]
    literals = [m.group(2) for m in _STRING_LITERAL.finditer(arguments)]
    if any(_PERL_WRITE_MODE.match(lit) for lit in literals):
        return True  # perl: open(FH, ">file") / open($fh, ">>", $file)
    keyword = re.search(r"mode\s*=\s*['\"]([^'\"]*)['\"]", arguments)
    if keyword:
        return _is_write_mode(keyword.group(1))
    return any(_is_write_mode(lit) for lit in literals) or "mode=" in arguments.replace(" ", "")


def _repo_root() -> str:
    return os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")
_SCRATCH = "/tmp/readonly-guard-scratch"
_DECLARERS = frozenset({"export", "local", "declare", "typeset", "readonly"})


def _assign(tokens: list[str], env: dict[str, str]) -> None:
    """Apply the ``NAME=value`` words of one stage to ``env`` in order: a
    value the guard can resolve is recorded, any other value forgets the
    name. A mktemp result was already stood in by a /tmp path."""
    for token in tokens:
        match = _ASSIGNMENT.match(token)
        if not match:
            continue
        name, value = token.split("=", 1)
        resolved = _resolve(_home(value.strip("\"'"), env), env)
        if resolved is None:
            env.pop(name, None)
        else:
            env[name] = resolved


def _home(target: str, env: dict[str, str]) -> str:
    """``~`` and ``~/x`` expanded from HOME; ``~user`` is left for _resolve
    to reject as unknown."""
    if target == "~" or target.startswith("~/"):
        home = env.get("HOME")
        return "$__UNKNOWN_HOME__" + target[1:] if home is None else home + target[1:]
    if target.startswith("~"):
        return "$__UNKNOWN_HOME__"
    return target


def _resolve(target: str, env: dict[str, str]) -> str | None:
    """``target`` with its variables expanded from ``env``; None when one is
    unknown or the text holds a substitution the guard did not run."""
    if _SUBST_MARK in target or "`" in target:
        return None
    unknown = False

    def substitute(match: re.Match) -> str:
        nonlocal unknown
        name = match.group(1) or match.group(3)
        if name in env:
            return env[name]
        if match.group(2) is not None:
            return match.group(2)
        unknown = True
        return ""

    resolved = _VARIABLE.sub(substitute, target)
    return None if unknown else resolved


def _in_repo(target: str, repo: str, cwd: str | None, env: dict[str, str]) -> bool | None:
    """Whether a write target resolves under the repo root, relative paths
    taken from ``cwd`` and variables from ``env``. None when the target
    cannot be resolved, or is relative while the cwd is unknown; the guard
    denies the repository, it does not police the rest of the disk."""
    if not target:
        return False
    if target == "{}":
        return True
    resolved = _resolve(_home(target, env), env)
    if resolved is None:
        return None
    if not resolved:
        return False
    if not resolved.startswith("/") and cwd is None:
        return None
    real = os.path.realpath(resolved if resolved.startswith("/") else os.path.join(cwd, resolved))
    root = os.path.realpath(repo)
    return real == root or real.startswith(root + "/")


def _git_output_targets(args: list[str]) -> list[str]:
    """Files a read-only git subcommand is told to write with --output."""
    targets: list[str] = []
    for k, arg in enumerate(args):
        if arg.startswith("--output="):
            targets.append(arg[len("--output=") :])
        elif arg == "--output" and k + 1 < len(args):
            targets.append(args[k + 1])
    return targets


def _operands(args: list[str]) -> list[str]:
    """Non-option operands, with redirections and their targets removed."""
    operands: list[str] = []
    options_over = False
    for arg in _without_redirections(args):
        if options_over:
            operands.append(arg)
            continue
        if arg == "--":
            options_over = True
            continue
        if arg.startswith("-"):
            continue
        operands.append(arg)
    return operands


def _target_directory(args: list[str]) -> str | None:
    """The `-t DIR` / `--target-directory=DIR` destination of GNU cp, mv,
    install, and ln, with `-t` possibly last in a short-flag cluster
    (`-avt DIR`) or attached (`-t./dir`)."""
    for k, arg in enumerate(args):
        if arg.startswith("--target-directory="):
            return arg[len("--target-directory=") :]
        if arg == "--target-directory" and k + 1 < len(args):
            return args[k + 1]
        if arg.startswith("--") or not arg.startswith("-") or len(arg) < 2:
            continue
        cluster = arg[1:]
        if cluster.endswith("t") and k + 1 < len(args):
            return args[k + 1]
        if cluster.startswith("t") and len(cluster) > 1 and ("/" in cluster or "." in cluster):
            return cluster[1:]
    return None


def _fetch_targets(base: str, args: list[str]) -> list[str]:
    """Files curl or wget would write: an explicit output path, or the cwd
    when told to keep the remote name (wget's default)."""
    targets: list[str] = []
    to_stdout = base == "curl"
    for k, arg in enumerate(args):
        if arg.startswith("--output=") or arg.startswith("--output-document="):
            targets.append(arg.split("=", 1)[1])
        elif arg in ("--output", "--output-document") and k + 1 < len(args):
            targets.append(args[k + 1])
            to_stdout = args[k + 1] == "-"
        elif arg in ("--remote-name", "--remote-name-all") and base == "curl":
            targets.append(".")
        elif arg.startswith("--directory-prefix="):
            targets.append(arg.split("=", 1)[1])
            to_stdout = False
        elif arg == "--directory-prefix" and k + 1 < len(args):
            targets.append(args[k + 1])
            to_stdout = False
        elif arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            cluster = arg[1:]
            for pos, letter in enumerate(cluster):
                attached = cluster[pos + 1 :]
                if letter == "o" and base == "curl" or letter == "O" and base == "wget":
                    value = attached or (args[k + 1] if k + 1 < len(args) else "")
                    targets.append(value)
                    if base == "wget":
                        to_stdout = value == "-"
                    break
                if letter == "O" and base == "curl":
                    targets.append(".")
                    break
                if letter == "P" and base == "wget":
                    targets.append(attached or (args[k + 1] if k + 1 < len(args) else ""))
                    to_stdout = False
                    break
    if base == "wget" and not targets and not to_stdout:
        targets.append(".")
    return [t for t in targets if t != "-" and t != "/dev/null"]


def _archive_targets(base: str, args: list[str], words: list[str]) -> list[str]:
    """Paths an archive or compression tool writes: the archive it creates,
    the directory it extracts into, or the files it (de)compresses in place."""
    flags = {arg for arg in args if arg.startswith("-")}
    if base == "tar" and args and re.fullmatch(r"[A-Za-z]+", args[0]) and set(args[0]) & set("cxtru"):
        args = ["-" + args[0]] + args[1:]
        flags = {arg for arg in args if arg.startswith("-")}
        words = words[1:]
    if base == "tar":
        cluster = "".join(arg[1:] for arg in flags if not arg.startswith("--"))
        if any(f in flags for f in ("-t", "--list")) or "t" in cluster and "c" not in cluster and "x" not in cluster:
            return []
        targets: list[str] = []
        extracting = "x" in cluster or "--extract" in flags
        for k, arg in enumerate(args):
            if arg in ("-f", "--file") and k + 1 < len(args):
                targets.append(args[k + 1])
            elif arg in ("-C", "--directory") and k + 1 < len(args):
                if extracting:
                    targets.append(args[k + 1])  # on create, -C only scopes the sources
            elif arg.startswith("--file="):
                targets.append(arg.split("=", 1)[1])
            elif arg.startswith("--directory="):
                if extracting:
                    targets.append(arg.split("=", 1)[1])
            elif arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:] and k + 1 < len(args):
                targets.append(args[k + 1])
        if extracting and not any(a in ("-C", "--directory") or a.startswith("--directory=") for a in args):
            targets.append(".")
        if "O" in cluster or "--to-stdout" in flags:
            targets = [t for t in targets if t != "."]
        if "--remove-files" in flags:
            targets += words  # the archived files are deleted afterwards
        return [t for t in targets if t != "-"]
    if base == "zip":
        return words if any(f in flags for f in ("-m", "--move")) else words[:1]
    if base == "unzip":
        if any(f in flags for f in ("-l", "-t", "-z", "-p")):
            return []
        for k, arg in enumerate(args):
            if arg == "-d" and k + 1 < len(args):
                return [args[k + 1]]
        return ["."]
    if any(f in flags for f in ("-c", "--stdout", "--to-stdout", "-t", "--test", "-l", "--list")):
        return []
    return words


def _write_targets(base: str, args: list[str]) -> list[str]:
    """Paths the command would write, by command family."""
    words = _operands(args)
    directory = _target_directory(args) if base != "rsync" else None
    if base == "rsync" and any(arg in ("--remove-source-files", "--remove-sent-files") for arg in args):
        return words  # the sources are deleted after the transfer
    if base in DESTINATION_WRITERS:
        if directory is not None:
            return [directory]
        return words[-1:] if len(words) >= 2 else []
    if base == "mv" and directory is not None:
        return words + [directory]
    if base == "chmod":
        return words[1:] if words and _MODE.match(words[0]) else words
    if base == "chown":
        return words if any(arg.startswith("--reference") for arg in args) else words[1:]
    if base in TARGET_WRITERS:
        return words
    if base in EDITORS:
        return _editor_targets(base, args)
    if base in FORMATTERS:
        return _formatter_targets(base, args)
    if base in ARCHIVERS:
        return _archive_targets(base, args, words)
    if base in FETCHERS:
        return _fetch_targets(base, args)
    if base == "find":
        paths = []
        for arg in args:
            if arg.startswith("-") or arg in ("(", ")", "!"):
                break
            paths.append(arg)
        writes = "-delete" in args
        for k, arg in enumerate(args):
            if arg not in ("-exec", "-execdir", "-ok", "-okdir"):
                continue
            m = k + 1
            while m < len(args) and (_prefix(args[m]) or args[m].rsplit("/", 1)[-1] in WRAPPERS):
                m += 1
            if m >= len(args):
                continue
            action = args[m].rsplit("/", 1)[-1]
            if action in DESTINATION_WRITERS | TARGET_WRITERS | {"dd", "patch", "tee"}:
                writes = True
            elif action in INPLACE_EDITORS:
                writes = writes or any(a.startswith("-i") for a in args[m + 1 :])
            elif action not in DATA_COMMANDS | EVALUATORS | {"git"} and not _is_interpreter(action):
                writes = True
        return (paths or ["."]) if writes else []
    if base == "dd":
        return [arg[3:] for arg in args if arg.startswith("of=")]
    if base == "patch":
        return [] if {"--dry-run", "--check"} & set(args) else ["."]
    if base in ("awk", "gawk", "nawk", "mawk"):
        if not any(arg in ("-i", "--include") and k + 1 < len(args) and args[k + 1] == "inplace" for k, arg in enumerate(args)) \
                and "--include=inplace" not in args and "-iinplace" not in args:
            return []
        program_args = {args[k + 1] for k, arg in enumerate(args) if arg in ("-e", "-f", "--source", "--file") and k + 1 < len(args)}
        files = [w for w in words if w != "inplace" and w not in program_args]
        return files if program_args else files[1:]
    if base in INPLACE_EDITORS:
        bundled = lambda arg: arg.startswith("-") and not arg.startswith("--") and "i" in arg[1:]  # noqa: E731
        if not any(arg.startswith("-i") or arg.startswith("--in-place") or bundled(arg) for arg in args):
            return []
        # the value of -e/-f/--expression/--file (perl: -e/-E) is the script, not a file
        script_shorts = ("-e", "-f") + (("-E",) if base == "perl" else ())
        script_flags = script_shorts + ("--expression", "--file")
        remaining: list[str] = []
        skip = False
        scripted = False
        for arg in args:
            if skip:
                skip = False
                continue
            if arg in script_flags:
                skip = scripted = True
                continue
            if arg.startswith(("--expression=", "--file=")) or (arg[:2] in script_shorts and len(arg) > 2 and not arg.startswith("--")):
                scripted = True
                continue
            remaining.append(arg)
        files = _operands(remaining)
        return files if scripted else files[1:]
    return []


# git settings whose value git runs as a command (or reads more settings
# from), even during a read; a one-shot `-c` or `--config-env` of one of
# these is judged like the command it names
_COMMAND_VALUED_KEY = re.compile(
    r"^(?:alias\.|include\.|includeif\.|core\.(?:pager|editor|fsmonitor|sshcommand|askpass|gitproxy|hookspath)$|"
    r"diff\.(?:external$|[^.]+\.(?:command|textconv)$)|difftool\.[^.]+\.cmd$|mergetool\.[^.]+\.cmd$|merge\.[^.]+\.driver$|"
    r"filter\.[^.]+\.(?:clean|smudge|process)$|pager\.|gpg\.(?:[^.]+\.)?program$|credential\.(?:[^.]+\.)?helper$|"
    r"sequence\.editor$|remote\.[^.]+\.(?:uploadpack|receivepack)$|uploadpack\.|receive\.)",
    re.I,
)
# environment variables git runs as a command, and those that hand it
# configuration the guard cannot see
GIT_COMMAND_ENV = frozenset(
    {"GIT_EXTERNAL_DIFF", "GIT_PAGER", "PAGER", "GIT_EDITOR", "EDITOR", "VISUAL", "GIT_SEQUENCE_EDITOR", "GIT_SSH",
     "GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_PROXY_COMMAND"}
)
GIT_CONFIG_ENV = frozenset({"GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"})
GIT_CONFIG_FILE_ENV = frozenset({"GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"})


def _git_config_hits(setting: str, repo: str, cwd: str | None, env: dict[str, str]) -> list[str]:
    """What a one-shot `-c key=value` would run: an alias body (shell after
    `!`, else a git command) or a command-valued setting's shell text; an
    include, or a value the guard cannot read, is unscannable."""
    key, _, value = setting.partition("=")
    if not _COMMAND_VALUED_KEY.match(key):
        return []
    if key.lower().startswith(("include.", "includeif.")) or not value or _unreadable(value):
        return [UNSCANNABLE]
    if key.lower().startswith("alias."):
        value = value[1:] if value.startswith("!") else "git " + value
    return blocked_subcommands(value, repo, cwd, env, cwd_unknown=cwd is None)


def _git_environment_hits(tokens: list[str], repo: str, cwd: str | None, env: dict[str, str]) -> list[str]:
    """What git would run from variables assigned in the command itself: a
    command in GIT_EXTERNAL_DIFF, GIT_PAGER, and kin is judged; settings or
    a config file handed through GIT_CONFIG_* are unscannable (/dev/null
    as the file is the idiom for ignoring config and passes)."""
    hits: list[str] = []
    for token in tokens:
        if not _ASSIGNMENT.match(token):
            continue
        name, _, value = token.partition("=")
        if name in GIT_CONFIG_ENV or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            hits.append(UNSCANNABLE)
        elif name in GIT_CONFIG_FILE_ENV and value != "/dev/null":
            hits.append(UNSCANNABLE)
        elif name in GIT_COMMAND_ENV:
            if not value or _unreadable(value):
                hits.append(UNSCANNABLE)
            else:
                hits.extend(blocked_subcommands(value, repo, cwd, env, cwd_unknown=cwd is None))
    return hits


def _stdin_source(segment: list[str]) -> tuple[str, str]:
    """Where a command's stdin comes from: "" (inherited), "file",
    "heredoc", or "string" with the here-string's text. Redirections apply
    left to right; the last one wins."""
    kind = text = ""
    for k, token in enumerate(segment):
        if token == "<<<":
            kind, text = "string", segment[k + 1] if k + 1 < len(segment) else ""
        elif token.startswith("<<"):
            kind, text = "heredoc", ""
        else:
            match = _INPUT_REDIRECT.match(token)
            if match and not match.group(1).startswith("&"):
                kind, text = "file", ""  # `<&3` duplicates a descriptor, not a file
    return kind, text


def _output_targets(segment: list[str]) -> list[str]:
    """What the command's output redirections open, wherever they sit."""
    targets: list[str] = []
    for k, token in enumerate(segment):
        redirect = _REDIRECT.match(token)
        if redirect and not _FD_DUP.match(token):
            target = redirect.group(2).lstrip("&")  # `>&file` is `&>file`
            targets.append(target or (segment[k + 1] if k + 1 < len(segment) else ""))
    return targets


def _without_redirections(tokens: list[str]) -> list[str]:
    """The tokens with every redirection and its operand removed."""
    kept: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if _FD_DUP.match(token):
            continue
        if token.startswith("<<"):
            skip = token in ("<<", "<<-", "<<<")
            continue
        redirect = _REDIRECT.match(token)
        target = redirect.group(2).lstrip("&") if redirect else None
        if redirect is None:
            redirect = _INPUT_REDIRECT.match(token)
            target = redirect.group(1) if redirect else None
        if redirect:
            skip = not target
            continue
        kept.append(token)
    return kept


def _scan_segment(
    segment: list[str], repo: str, cwd: str | None, env: dict[str, str]
) -> tuple[list[str], bool, bool, bool]:
    """(hits, evaluates strings, the interpreter it runs or "", is a shell with no script).

    A shell with no script read its program from stdin or a file; the
    caller decides whether a heredoc supplied it."""
    found: list[str] = []
    unscannable = False

    def judge_in(target: str, base_cwd: str | None) -> bool:
        """Record a write or an unresolvable target resolved from ``base_cwd``;
        True when one was found."""
        nonlocal unscannable
        verdict = _in_repo(target, repo, base_cwd, env)
        if verdict is None:
            unscannable = True
            return True
        if verdict:
            found.append(WRITE + target)
            return True
        return False

    def judge(target: str) -> bool:
        """Record a write or an unresolvable target; True when one was found."""
        nonlocal unscannable
        verdict = _in_repo(target, repo, cwd, env)
        if verdict is None:
            unscannable = True
            return True
        if verdict:
            found.append(WRITE + target)
            return True
        return False
    # redirections are the shell's wherever they sit in the command: judge
    # what they open, then read the command without them
    for target in _output_targets(segment):
        judge(target)
    stdin_kind, stdin_text = _stdin_source(segment)
    segment = _without_redirections(segment)
    command_position = True
    evaluating = False
    evaluator = ""
    shell = False
    script_scanned = False
    words = [token for token in segment if not _ASSIGNMENT.match(token)]
    first = words[0].rsplit("/", 1)[-1] if words else ""
    leading_wrapper = bool(first) and (
        first in WRAPPERS
        or first not in DATA_COMMANDS | EVALUATORS | {"git"} and not _is_interpreter(first)
        and not first.startswith("-")
        and not any(ch.isspace() for ch in first)
    )
    unknown_wrapper = leading_wrapper and first not in WRAPPERS
    interpreter = next((_interpreter_family(token.rsplit("/", 1)[-1]) for token in segment if _is_interpreter(token.rsplit("/", 1)[-1])), "")
    i = 0
    while i < len(segment):
        token = segment[i]
        base = token.rsplit("/", 1)[-1]
        if any(ch.isspace() for ch in token):
            if (evaluating or leading_wrapper) and _unreadable(token):
                unscannable = True
            elif evaluating or leading_wrapper:
                # an evaluator's script, or a command string handed to a wrapper
                # such as `watch '…'` / `flock -c '…'`
                found.extend(blocked_subcommands(token, repo, cwd, env, cwd_unknown=cwd is None))
                script_scanned = script_scanned or evaluating
            i += 1
            continue
        if shell and not script_scanned and not token.startswith("-") and token not in ("_", "--"):
            unscannable = True
            i += 1
            continue
        if (evaluating or unknown_wrapper) and _unreadable(token):
            unscannable = True  # an evaluator's, or an unknown command's, expanded argument
            i += 1
            continue
        if _prefix(token):
            i += 1
            continue
        if base == "find" and (command_position or leading_wrapper or evaluating):
            for target in _write_targets(base, segment[i + 1 :]):
                if judge(target):
                    break
        if base in ("uv", "uvx") and (command_position or evaluating):
            launched, after = _launched_by_uv(segment, i)
            if launched is not None:
                launched_base = launched.rsplit("/", 1)[-1]
                if launched.endswith(_SCRIPT_SUFFIXES):
                    if _in_repo(launched, repo, cwd, env) is not True or _script_argv_mutates(segment, launched):
                        unscannable = True
                    for target in _script_output_targets(segment, launched):
                        if judge(target):
                            break
                    command_position = False
                    i = len(segment)
                    continue
                if launched_base in FORMATTERS:
                    for target in _formatter_targets(launched_base, segment[after:]):  # `--with ruff ruff …`: argv starts after the tool
                        if judge(target):
                            break
                    command_position = False
                    i = len(segment)  # the formatter and its argv are judged; nothing else runs here
                    continue
                if launched_base not in {"git"} | DATA_COMMANDS | EVALUATORS and not _is_interpreter(launched_base) and _script_argv_mutates(segment, launched):
                    unscannable = True
            command_position = False
            i = after if launched is None else i + 1
            continue
        if command_position and token.endswith(_SCRIPT_SUFFIXES) and not _is_interpreter(base):
            if _in_repo(token, repo, cwd, env) is not True or _script_argv_mutates(segment, token):
                unscannable = True
            for target in _script_output_targets(segment, token):
                if judge(target):
                    break
            command_position = False
            i = len(segment)
            continue
        if base in NESTED_AGENTS and (command_position or leading_wrapper or evaluating):
            unscannable = True
            command_position = False
            i += 1
            continue
        if base in EVALUATORS and (command_position or leading_wrapper or (evaluating and base != ".")):
            evaluating = True
            evaluator = base
            shell = shell or base in SHELLS
            command_position = False
            i += 1
            continue
        if base == "git" and (command_position or leading_wrapper or evaluating):
            j = i + 1
            uncertain = False
            git_cwd = cwd  # `git -C DIR` moves where relative output paths and -c values resolve
            while j < len(segment) and segment[j].startswith("-"):
                option = segment[j]
                if option in _OPTIONS_WITH_ARG:
                    if option == "-C" and j + 1 < len(segment):
                        git_cwd = _change_directory(["cd", segment[j + 1]], git_cwd, env)
                    if option == "-c" and j + 1 < len(segment):
                        found.extend(_git_config_hits(segment[j + 1], repo, git_cwd, env))
                    elif option == "--config-env" and j + 1 < len(segment) and _COMMAND_VALUED_KEY.match(segment[j + 1].partition("=")[0]):
                        unscannable = True  # a command-valued setting read from the environment
                    j += 2
                elif option in _OPTIONS_NO_VALUE or "=" in option:
                    if option.startswith("--config-env=") and _COMMAND_VALUED_KEY.match(option[len("--config-env="):].partition("=")[0]):
                        unscannable = True
                    j += 1
                else:
                    uncertain = True
                    j += 1
            # the subcommand and its arguments, with the command's own variables
            # expanded: `ARGS=--output=x; git diff "$ARGS"` writes x; an argument
            # the guard cannot read may carry such an option
            tail: list[str] = []
            for tok in segment[j:]:
                if _unreadable(tok):
                    value = _resolve(tok, env)
                    if value is None:
                        if tok[:1] in "$`" or tok.startswith(_SUBST_MARK) or tok.startswith(_UNREADABLE_WORD):
                            unscannable = True  # the whole word is unknown: it may be an option
                        value = tok  # a value after a literal prefix (`--format=%s`) is data
                    tok = value
                tail.append(tok)
            segment = segment[:j] + tail
            words = segment[i + 1 :]
            if uncertain:
                # a global option the guard does not model may or may not have
                # swallowed the next word: judge every word of the stage
                if any(_unreadable(word) for word in words):
                    unscannable = True
                for k, word in enumerate(words):
                    if word in BLOCKED_GIT:
                        found.append(word)
                        break
                    if word in CONDITIONAL_GIT and _conditionally_blocked(word, words[k + 1 :]):
                        found.append(word)
                        break
                else:
                    for target in _git_output_targets(words):
                        if judge_in(target, git_cwd):
                            break
            elif j < len(segment):
                subcommand = segment[j]
                rest = segment[j + 1 :]
                if _unreadable(subcommand):
                    unscannable = True
                elif subcommand in BLOCKED_GIT or _conditionally_blocked(subcommand, rest):
                    found.append(subcommand)
                else:
                    for target in _git_output_targets(rest):
                        if judge_in(target, git_cwd):
                            break
            command_position = False
            i = len(segment)
            continue
        if command_position and _unreadable(token):
            unscannable = True
        if command_position or leading_wrapper or evaluating:
            rest = segment[i + 1 :]
            for target in _write_targets(base, rest):
                if evaluator == "find" and "{}" in target:
                    continue  # find substitutes its own paths, judged at the find level
                if judge(target):
                    break
            if evaluator == "xargs" and not _write_targets(base, rest):
                if base in TARGET_WRITERS and not _operands(rest):
                    unscannable = True
                elif base in INPLACE_EDITORS and any(arg.startswith(("-i", "--in-place")) for arg in rest):
                    unscannable = True
        command_position = False
        i += 1
    if shell and not script_scanned and stdin_kind == "string":
        # a here-string is the shell's program, spelled out
        found.extend(blocked_subcommands(stdin_text, repo, cwd, env, cwd_unknown=cwd is None))
        script_scanned = True
    elif shell and not script_scanned and stdin_kind == "file":
        unscannable = True  # the program is in a file the guard cannot see
    if unscannable:
        found.append(UNSCANNABLE)
    return found, evaluating, interpreter, shell and not script_scanned


def _mktemp_stand_in(content: str, cwd: str | None, env: dict[str, str]) -> str | None:
    """A path standing in for a ``$(mktemp …)`` result: under the template's
    directory, ``-p``/``--tmpdir`` directory, or the scratch root when the
    template goes to TMPDIR; None when the directory cannot be known."""
    words = _tokens(content)
    if not words or words[0].rsplit("/", 1)[-1] != "mktemp":
        return None
    directory: str | None = _SCRATCH
    template: str | None = None
    k = 1
    while k < len(words):
        word = words[k]
        if word in ("-p", "--tmpdir") and k + 1 < len(words):
            directory = words[k + 1]
            k += 2
            continue
        if word.startswith("--tmpdir="):
            directory = word[len("--tmpdir=") :]
        elif word.startswith("-p") and len(word) > 2:
            directory = word[2:]
        elif word in ("-t", "-d", "-q", "-u", "--directory", "--quiet", "--dry-run"):
            pass
        elif not word.startswith("-"):
            template = word
        k += 1
    if template is not None and "/" in template:
        directory = template.rsplit("/", 1)[0] or "/"
    elif template is not None and directory == _SCRATCH and "-t" not in words:
        directory = cwd
    if directory is None:
        return None
    resolved = _resolve(_home(directory, env), env)
    if resolved is None:
        return None
    if not resolved.startswith("/"):
        if cwd is None:
            return None
        resolved = os.path.join(cwd, resolved)
    return os.path.join(resolved, "mktemp-result")


def _command_index(segment: list[str], wanted) -> int | None:
    """The index of a command satisfying ``wanted`` in command position of
    one stage: after assignments, wrappers, options, and a `uv run`/`uvx`
    launcher; None when the stage's command is something else (the same
    name as an argument is data)."""
    k = 0
    while k < len(segment):
        token = segment[k]
        base = token.rsplit("/", 1)[-1]
        if wanted(base):
            return k
        if base in EVALUATORS - SHELLS:
            # xargs / find -exec / ssh hand later words to the shell as a command
            return next((j for j in range(k + 1, len(segment)) if wanted(segment[j].rsplit("/", 1)[-1])), None)
        if _prefix(token) or base in ("uv", "uvx", "run", "tool"):
            if base in ("uv", "uvx"):
                launched, after = _launched_by_uv(segment, k)
                if launched is None:
                    return None
                k = after - 1
                continue
            k += 1
            continue
        return None
    return None


def _interpreter_index(segment: list[str]) -> int | None:
    return _command_index(segment, _is_interpreter)


def _program_index(segment: list[str]) -> int | None:
    return _command_index(segment, lambda base: base in AWK_FAMILY or base == "sed")


_INLINE_FLAGS = frozenset({"-c", "-e", "-r"})
# the flags that carry inline program text, by interpreter family; node and
# ruby's -r preload a module (a file runs like a script) and are handled apart
_INLINE_BY_FAMILY = {
    "python": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "perl": frozenset({"-e", "-E"}),
    "ruby": frozenset({"-e"}),
    "php": frozenset({"-r"}),
    "osascript": frozenset({"-e"}),
}
_PERL_CLUSTER = re.compile(r"^-[0-9A-Za-z]{0,4}[eE]$")  # -pe, -ne, -lane: code follows
_PRELOAD_FLAGS = frozenset({"-r", "--require"})


def _inline_flag(token: str, family: str) -> str | None:
    """The inline code a flag token carries: "" when the code is the next
    token (`-c`, `--eval`, perl's `-pe`), the attached text for `-c"…"` or
    `--eval=…`, None when the token is not an inline-code flag."""
    flags = _INLINE_BY_FAMILY.get(family, _INLINE_FLAGS)
    if token in flags:
        return ""
    long, joined, value = token.partition("=")
    if joined and long.startswith("--") and long in flags:
        return value
    if family == "perl" and _PERL_CLUSTER.match(token):
        return ""
    if len(token) > 2 and not token.startswith("--") and token[:2] in flags:
        return token[2:]
    return None


def _preloaded_file(token: str, tokens: list[str], k: int, family: str) -> str | None:
    """The file a node/ruby `-r` preloads, when its argument names one."""
    if family in ("node", "ruby") and token in _PRELOAD_FLAGS and k + 1 < len(tokens):
        value = tokens[k + 1]
        if "/" in value or value.endswith(_SCRIPT_SUFFIXES):
            return value
    return None


def _inline_code(tokens: list[str]) -> list[str]:
    """The program text an interpreter is given inline, flag by flag, up to
    its script, module, or first argv word (which are data)."""
    k = next((i for i, tok in enumerate(tokens) if _is_interpreter(tok.rsplit("/", 1)[-1])), None)
    if k is None:
        return []
    family = _interpreter_family(tokens[k].rsplit("/", 1)[-1])
    codes: list[str] = []
    k += 1
    while k < len(tokens):
        token = tokens[k]
        if not token.startswith("-") or token == "-" or (token.startswith("-m") and not token.startswith("--")):
            break
        if family in ("node", "ruby") and token in _PRELOAD_FLAGS:
            k += 2  # a module or file, not code
            continue
        inline = _inline_flag(token, family)
        if inline is None:
            k += 2 if token in _INTERPRETER_FLAGS_WITH_ARG else 1
        elif inline:
            codes.append(inline)
            k += 1
        else:
            if k + 1 < len(tokens):
                codes.append(tokens[k + 1])
            k += 2
    return codes
# argv shapes that tell a repository script to change something; a read-only
# agent may run the repository's own scripts only for their read paths
_MUTATING_FLAG = re.compile(
    r"^--?(apply|fix[\w-]*|write|force|rebuild[\w-]*|install|uninstall|commit|push|delete|remove|rm|reset|"
    r"overwrite|in-place|inplace|yes|y|w|f)$"
)
_MUTATING_VERBS = frozenset(
    {"apply", "commit", "push", "write", "install", "uninstall", "delete", "remove", "rm", "reset", "build",
     "done", "set", "add", "rotate", "promote", "migrate", "merge", "archive", "fix", "record", "mark",
     "rename", "move", "mv", "update", "save", "sync", "publish", "deploy", "ack", "log", "format",
     "rollback", "restore", "revert", "finalize", "snapshot", "quarantine", "purge", "prune", "clean",
     "create", "init", "send", "post", "notify", "ingest", "capture", "import", "trash", "edit", "append"}
)
# the subcommands a repository script may take that only read; any other
# word in the subcommand slot is the script's own verb, which the guard
# cannot vouch for
_READ_VERBS = frozenset(
    {"list", "ls", "show", "status", "check", "lint", "query", "search", "health", "why", "stats", "info",
     "describe", "get", "read", "view", "print", "summary", "inspect", "cat", "help", "version", "explain",
     "diff", "validate", "preview", "due", "dry-run", "dry_run"}
)
# a first positional word that is plainly data rather than a subcommand: a
# number, date, or version; a path (a `/`, or a leading `.` or `~`); a
# filename with a known suffix; text with whitespace. `apply.now` is a verb.
_DATA_WORD = re.compile(
    r"^(?:v?\d[\d.:-]*|[.~].*|.*/.*|.*\.(?:md|py|toml|json|txt|html?|csv|ya?ml|log|sh|js|ts|pdf|png|jpe?g)|.*\s.*)$",
    re.I,
)


_SCRIPT_SUFFIXES = (".py", ".sh", ".rb", ".pl", ".js")
_UV_OPTIONS_WITH_ARG = frozenset(
    {"--directory", "--python", "-p", "--index", "--index-url", "--env-file", "--with", "--from",
     "--project", "--package", "--group", "--extra", "--config-file", "--cache-dir", "--script",
     "--with-requirements", "--only-group", "--no-group", "--exclude-newer"}
)


def _launched_by_uv(segment: list[str], k: int) -> tuple[str | None, int]:
    """(what `uv`/`uvx` at index k runs, the index after it): a script path,
    a tool name, or None when nothing follows."""
    k += 1
    while k < len(segment):
        token = segment[k]
        if token in ("run", "tool"):
            k += 1
        elif token.startswith("-"):
            k += 2 if token in _UV_OPTIONS_WITH_ARG else 1
        else:
            return token, k + 1
    return None, k


def _module_script(module: str, repo: str) -> str | None:
    """The repository file a `-m module` names, if any."""
    candidate = Path(repo) / (module.replace(".", "/") + ".py")
    return str(candidate) if candidate.is_file() else None


# flags through which a script names where it writes
_OUTPUT_FLAGS = frozenset(
    {"-o", "--out", "--output", "--out-file", "--output-file", "--outfile", "--out-dir", "--output-dir", "--outdir",
     "--dest", "--destination", "--save-to", "--write-to", "--log-file", "--report-file"}
)


def _script_output_targets(tokens: list[str], script: str) -> list[str]:
    """The paths a script's output flags name (`--out X`, `--output=X`, `-oX`)."""
    k = next((i for i, tok in enumerate(tokens) if tok == script or tok.endswith(script)), -1) + 1
    argv = tokens[k:]
    targets: list[str] = []
    for n, tok in enumerate(argv):
        flag, joined, value = tok.partition("=")
        short = tok.startswith("-") and not tok.startswith("--") and len(tok) > 2
        letters = re.match(r"[A-Za-z]*", tok[1:]).group(0) if short else ""
        if flag.lower() in _OUTPUT_FLAGS or (short and flag[1:].isalpha() and flag.endswith("o")):  # -o, -vo FILE, -vo=FILE
            if joined:
                targets.append(value)
            elif n + 1 < len(argv):
                targets.append(argv[n + 1])  # whatever it starts with: `--out -x.json` names -x.json
        elif short and "o" in letters:
            # -oFILE, -voFILE: the value is attached after the first o whose remainder looks like a path
            for k in range(1, len(letters) + 1):
                remainder = tok[1 + k :]
                if letters[k - 1] == "o" and remainder and ("/" in remainder or "." in remainder):
                    targets.append(remainder)
                    break
    return targets


def _script_argv_mutates(tokens: list[str], script: str) -> bool:
    """Whether a repository script is being told to change something: a
    mutating flag anywhere in its argv (in any letter case, with or
    without an attached `=value`), or a
    subcommand that is mutating, unknown, or anything but plain data. The
    subcommand is the first positional word; a word after a bare flag is
    that flag's value unless it is a known verb. Later words are data."""
    k = next((i for i, tok in enumerate(tokens) if tok == script or tok.endswith(script)), -1) + 1
    argv = tokens[k:]
    if any(_MUTATING_FLAG.match(tok.lower().split("=", 1)[0]) for tok in argv):
        return True
    first = ""
    after_flag = False
    for tok in argv:
        if tok.startswith("-"):
            after_flag = "=" not in tok and tok != "--"  # `--runtime codex`: the next word is its value
            continue
        if after_flag and tok.lower() not in _MUTATING_VERBS | _READ_VERBS:
            after_flag = False
            continue
        first = tok.lower()
        break
    return bool(first) and first not in _READ_VERBS and (first in _MUTATING_VERBS or not _DATA_WORD.match(first))
_INTERPRETER_FLAGS_WITH_ARG = frozenset({"-c", "-e", "-r", "-m", "-W", "-X", "-Q", "--check-hash-based-pycs"})


def _interpreter_script(tokens: list[str]) -> str | None:
    """What an interpreter invocation runs: "" for inline code or a module,
    "-" for stdin, a path for a script file, None for a bare interpreter."""
    k = next((i for i, tok in enumerate(tokens) if _is_interpreter(tok.rsplit("/", 1)[-1])), None)
    if k is None:
        return ""
    family = _interpreter_family(tokens[k].rsplit("/", 1)[-1])
    k += 1
    while k < len(tokens):
        token = tokens[k]
        if token == "-":
            return "-"
        if token.startswith("-"):
            if token == "-m" and k + 1 < len(tokens):
                return "-m:" + tokens[k + 1]
            if token.startswith("-m") and len(token) > 2 and not token.startswith("--"):
                return "-m:" + token[2:]
            preloaded = _preloaded_file(token, tokens, k, family)
            if preloaded is not None:
                return preloaded  # a preloaded file runs like a script
            if family in ("node", "ruby") and token in _PRELOAD_FLAGS:
                k += 2
                continue
            if _inline_flag(token, family) is not None:
                return ""  # inline code: what follows is its argv
            k += 2 if token in _INTERPRETER_FLAGS_WITH_ARG else 1
            continue
        return token
    return None


def _sync_pwd(env: dict[str, str], cwd: str | None) -> None:
    if cwd is None:
        env.pop("PWD", None)
    else:
        env["PWD"] = cwd


def _change_directory(segment: list[str], cwd: str | None, env: dict[str, str]) -> str | None:
    """The cwd after a ``cd``/``pushd`` stage; None when it cannot be known."""
    if "-" in segment[1:]:
        return None
    words = _operands(segment[1:])
    target = words[0] if words else env.get("HOME")
    if target is None:
        return None
    resolved = _resolve(_home(target, env), env)
    if resolved is None:
        return None
    if not resolved.startswith("/"):
        if cwd is None:
            return None
        resolved = os.path.join(cwd, resolved)
    return os.path.realpath(resolved)


def _quoted_as_data(line: str) -> str:
    """The line with single-quoted text and backslash-escaped characters
    masked (same length), so a substitution found in it is one the shell
    runs; inside double quotes a single quote is an ordinary character."""
    out: list[str] = []
    quote = ""
    k = 0
    while k < len(line):
        ch = line[k]
        if quote == "'":
            if ch == "'":
                quote = ""
            out.append(ch if ch == "'" else "_")
        elif ch == "\\":
            out.append("__" if k + 1 < len(line) else "_")  # the escaped character is data too
            k += 2
            continue
        elif ch == '"':
            quote = "" if quote == '"' else '"'
            out.append(ch)
        elif ch == "'" and quote != '"':
            quote = "'"
            out.append(ch)
        else:
            out.append(ch)
        k += 1
    return "".join(out)


def _grouped(line: str) -> str:
    """The line with its unquoted grouping punctuation spelled as markers:
    `(` and `$(` open a subshell, `)` closes one, a backtick separates;
    inside quotes they are data (a substitution there was already
    replaced or flagged by the caller)."""
    out: list[str] = []
    quote = ""
    k = 0
    while k < len(line):
        ch = line[k]
        if quote:
            if ch == "\\" and quote == '"':
                out.append(line[k : k + 2])
                k += 2
                continue
            if ch == quote:
                quote = ""
            out.append(ch)
        elif ch == "\\":
            out.append(line[k : k + 2])
            k += 2
            continue
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "$" and line[k + 1 : k + 2] == "(":
            out.append(f" {_SUBSHELL_OPEN} ")
            k += 2
            continue
        elif ch == "(":
            out.append(f" {_SUBSHELL_OPEN} ")
        elif ch == ")":
            out.append(f" {_SUBSHELL_CLOSE} ")
        elif ch == "`":
            out.append(" ; ")
        else:
            out.append(ch)
        k += 1
    return "".join(out)


def _open_quote(text: str) -> bool:
    """Whether a shell quote is still open at the end of ``text``."""
    quote = ""
    k = 0
    while k < len(text):
        ch = text[k]
        if quote == "'":
            if ch == "'":
                quote = ""
        elif ch == "\\":
            k += 1
        elif quote == '"':
            if ch == '"':
                quote = ""
        elif ch in "'\"":
            quote = ch
        k += 1
    return bool(quote)


def _lines(command: str) -> list[tuple[str, str | None, bool]]:
    """Command lines, each with the heredoc body it opens (or None) and
    whether the shell expands that body (an unquoted delimiter). A quoted
    string that spans physical lines keeps its line whole."""
    raw = command.split("\n")
    out: list[tuple[str, str | None, bool]] = []
    i = 0
    while i < len(raw):
        line = raw[i]
        while _open_quote(line) and _HEREDOC.search(line) is None and i + 1 < len(raw):
            i += 1
            line += "\n" + raw[i]
        match = _HEREDOC.search(line)
        if match is None:
            out.append((line, None, False))
            i += 1
            continue
        terminator = match.group(2)
        body: list[str] = []
        i += 1
        while i < len(raw) and raw[i].strip() != terminator:
            body.append(raw[i])
            i += 1
        out.append((line, "\n".join(body), match.group(1) == ""))
        i += 1
    return out


def blocked_subcommands(
    command: str,
    repo: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    *,
    cwd_unknown: bool = False,
) -> list[str]:
    """What the guard denies in ``command``, in order: blocked git subcommands
    the shell would run, ``"write:<target>"`` for a write into the repository
    (a redirection, or cp/mv/rm/tee/sed -i and kin aimed at a relative path
    or an absolute one under the repo root), ``"unscannable"`` for shell
    input it cannot read.

    ``git`` counts in command position: first in a pipeline stage, anywhere
    after a leading wrapper (``sudo``/``env``/``timeout`` and any first word
    that is not a known data-only command such as ``rg`` or ``cat``), after an
    evaluator such as ``bash -c``/``eval``/``xargs``/``find``, and inside
    ``$(...)``, backticks, or ``( )``/``{ }`` groups; substitution text is
    scanned whatever quotes surround it, innermost first, and a substitution
    result used as a command word is unscannable; backslash-newlines are
    joined first. Unscannable, hence denied: an expanded command word or git
    subcommand (``$GIT reset``, ``git $CMD``), a shell given a variable, a
    substitution result, or a script path, and a shell reading its script
    from stdin or a file with no heredoc in sight, and a write target the
    guard cannot resolve (variables are expanded from the hook's environment,
    the payload cwd, and assignments earlier in the command; a mktemp result
    stands in as a /tmp path). A ``git`` that is another
    command's argument, a quoted string no evaluator runs, and a heredoc body
    no evaluator reads are data and pass (an unquoted heredoc's substitutions
    run before the read and are judged). Program text handed to
    ``python``/``perl``/``ruby``/``node``/``php``/``osascript`` inline, by
    heredoc, or by here-string is denied on a statement that shells out with a spelled-out
    command the guard would deny at the prompt (a blocked git command, a
    write into the repository), or that calls a file-writing API with a
    spelled-out path inside the repository; an interpreter told to run a
    script outside the repository, to run a repository script with a
    mutating flag or verb in any letter case (``--apply``, ``--fix``, ``commit``, ``done``, …) or
    with a subcommand the guard does not know as read-only,
    or to read its program from a pipe or a file redirection, is unscannable; a repository script's output flag (``-o``, ``--out``,
    ``--output``, ``--out-dir``, ``--dest``, …) names a write target; a git argument the guard cannot read is unscannable
    too, since it may carry an option such as ``--output``. ``cd``/``pushd``/``popd`` and subshells move the cwd that
    relative targets resolve from; after a ``cd`` to a path the guard cannot
    resolve, relative writes are unscannable. Inside an interpreter, a
    shell-out or file API whose command or path is not fully spelled out (a
    variable, a format field, a call) is unscannable; a literal inside
    ``Path(...)``, ``str(...)``, ``os.path.join(...)``, or a ``/``-join is
    spelled out and judged (the last absolute component, else the first).
    Import renames, Node destructuring, and chains of plain `name = other`
    bindings are followed for modules, shell-outs, and write APIs; a name
    bound to a path is followed in program order, and since the guard does
    not model control flow, a rebinding inside any block (an indented
    statement) makes the name unreadable. A git setting or
    variable whose value git runs as a command (an alias, diff.external,
    core.pager, a filter, GIT_EXTERNAL_DIFF, GIT_PAGER, …) is judged like
    that command when the command sets it, as a prefix assignment, an
    export, or through env (wherever the assignment sits, whether or not
    git follows); configuration handed through a file or GIT_CONFIG_* is
    unscannable. Out of scope: git and shell
    aliases and settings configured outside the command, a call reached
    through a binding the guard cannot follow (getattr, a container, a call
    result), and program text that only quotes such calls as data."""
    repo = _repo_root() if repo is None else repo
    here: str | None = None if cwd_unknown else (repo if cwd is None else cwd)
    env = dict(os.environ) if env is None else dict(env)
    _sync_pwd(env, here)
    found: list[str] = []
    saved: list[tuple[str | None, dict[str, str]]] = []
    dirstack: list[str | None] = []
    for line, heredoc, expands in _lines(command.replace("\\\n", " ")):
        evaluates = scriptless_shell = False
        interprets = ""
        line_hits: list[str] = []
        while True:
            # substitutions the shell runs: not those in single quotes or escaped
            matches = list(_SUBSTITUTION.finditer(_quoted_as_data(line)))
            if not matches:
                break
            pieces: list[str] = []
            last = 0
            for match in matches:
                start, end = match.span(1) if match.group(1) is not None else match.span(2)
                inner = line[start:end]
                line_hits.extend(blocked_subcommands(inner, repo, here, env, cwd_unknown=here is None))
                pieces.append(line[last : match.start()])
                pieces.append(_mktemp_stand_in(inner, here, env) or _SUBST_MARK)
                last = match.end()
            pieces.append(line[last:])
            line = "".join(pieces)
        outside_single_quotes = re.sub(r"'[^']*'", "", re.sub(r"\\[`$]", "", line))
        if "$(" in outside_single_quotes or "`" in outside_single_quotes:
            line_hits.append(UNSCANNABLE)
        if heredoc is not None and expands:
            # an unquoted delimiter: the shell runs the body's substitutions
            # before any command reads it, even `cat`
            body = re.sub(r"\\[`$]", "", heredoc)  # an escaped `\$(` stays literal
            while True:
                matches = list(_SUBSTITUTION.finditer(body))
                if not matches:
                    break
                for match in matches:
                    line_hits.extend(
                        blocked_subcommands(match.group(1) or match.group(2) or "", repo, here, env, cwd_unknown=here is None)
                    )
                body = _SUBSTITUTION.sub(_SUBST_MARK, body)
            if "$(" in body or "`" in body:
                line_hits.append(UNSCANNABLE)
        interpreter_segments: list[tuple[list[str], str, str, str, list[str] | None]] = []  # every interpreter on the line
        raw_segments = _segments(_tokens(line))
        # the same tokens with single-quoted text masked: what the shell expands
        masked_segments = _segments(_tokens(_quoted_as_data(line)))
        aligned = len(masked_segments) == len(raw_segments) and all(
            len(a) == len(b) for a, b in zip(raw_segments, masked_segments)
        )
        for n, raw_segment in enumerate(raw_segments):
            masked = _without_redirections(masked_segments[n]) if aligned else None
            stdin_kind, stdin_text = _stdin_source(raw_segment)
            raw_segment = _without_redirections(raw_segment)
            if masked is not None and len(masked) != len(raw_segment):
                masked = None
            program_at = _program_index(raw_segment)
            if program_at is not None:
                tool = raw_segment[program_at].rsplit("/", 1)[-1]
                if any(arg in ("-f", "--file") or arg.startswith("--file=") for arg in raw_segment[program_at + 1 :]):
                    line_hits.append(UNSCANNABLE)  # a program read from a file the guard cannot see
                for program in raw_segment[program_at + 1 :]:
                    if program.startswith("-"):
                        continue
                    line_hits.extend(_awk_hits(program, repo, here, env) if tool in AWK_FAMILY else _sed_hits(program, repo, here, env))
            index = _interpreter_index(raw_segment)
            if index is not None:
                interpreter_segments.append(
                    (
                        raw_segment[index:],
                        _interpreter_family(raw_segment[index].rsplit("/", 1)[-1]),
                        stdin_kind,
                        stdin_text,
                        masked[index:] if masked is not None else None,
                    )
                )
        for raw_tokens, raw_interpreter, raw_stdin, raw_stdin_text, masked_tokens in interpreter_segments:
            script = _interpreter_script(raw_tokens)
            codes = _inline_code(raw_tokens)
            masked_codes = _inline_code(masked_tokens) if masked_tokens is not None else []
            for k, code in enumerate(codes):
                shell_sees = masked_codes[k] if len(masked_codes) == len(codes) else code
                if _unreadable(shell_sees):
                    line_hits.append(UNSCANNABLE)  # code the shell expands first (a `$` in single quotes is data)
                else:
                    line_hits.extend(_interpreter_hits(code, raw_interpreter, repo, here, env))
            if script is not None and script.startswith("-m:"):
                module = script[3:]
                if _module_script(module, repo) and _script_argv_mutates(raw_tokens, module):
                    line_hits.append(UNSCANNABLE)
                if module in FORMATTERS:  # `python -m black scripts`
                    at = next((k for k, tok in enumerate(raw_tokens) if tok == module or tok == "-m" + module), len(raw_tokens))
                    for target in _formatter_targets(module, raw_tokens[at + 1 :]):
                        verdict = _in_repo(target, repo, here, env)
                        line_hits.append(UNSCANNABLE if verdict is None else WRITE + target if verdict else "")
                    line_hits[:] = [hit for hit in line_hits if hit]
                script = ""
            if script is None or script == "-":
                if raw_stdin == "string" and not _unreadable(raw_stdin_text):
                    line_hits.extend(_interpreter_hits(raw_stdin_text, raw_interpreter, repo, here, env))
                elif raw_stdin in ("file", "string") or (heredoc is None and not (
                    raw_tokens[1:] and all(token in _INERT_FLAGS for token in raw_tokens[1:])
                )):
                    line_hits.append(UNSCANNABLE)  # program text the guard cannot see; --version alone runs none
            elif script and (_in_repo(script, repo, here, env) is not True or _script_argv_mutates(raw_tokens, script)):
                line_hits.append(UNSCANNABLE)
            if script and not script.startswith("-m:"):
                for target in _script_output_targets(raw_tokens, script):  # `scripts/x.py --out scripts/y`
                    verdict = _in_repo(target, repo, here, env)
                    if verdict is None:
                        line_hits.append(UNSCANNABLE)
                    elif verdict:
                        line_hits.append(WRITE + target)
        grouped = _grouped(line)
        for segment in _segments(_tokens(grouped)):
            if segment == [_SUBSHELL_OPEN]:
                saved.append((here, dict(env)))
                continue
            if segment == [_SUBSHELL_CLOSE]:
                if saved:
                    here, restored = saved.pop()
                    env.clear()
                    env.update(restored)
                    _sync_pwd(env, here)
                continue
            leading = 0
            while leading < len(segment) and _ASSIGNMENT.match(segment[leading]):
                leading += 1
            line_hits.extend(_git_environment_hits(segment, repo, here, env))  # `X=… git`, `export X=…`, `env X=… git`
            if leading == len(segment):
                _assign(segment, env)  # a bare assignment persists
                continue
            # `VAR=x cmd …` scopes VAR to cmd's environment; the shell expands
            # cmd's own words with the prior environment, so nothing is applied
            base = segment[leading].rsplit("/", 1)[-1]
            if base in _DECLARERS:
                _assign(segment[leading + 1 :], env)
                continue
            segment = segment[leading:]
            if base in ("cd", "pushd") and not any(ch.isspace() for ch in segment[0]):
                if base == "pushd":
                    dirstack.append(here)
                here = _change_directory(segment, here, env)
                _sync_pwd(env, here)
                continue
            if base == "popd":
                here = dirstack.pop() if dirstack else here
                _sync_pwd(env, here)
                continue
            hits, evaluating, interpreter, scriptless = _scan_segment(segment, repo, here, env)
            line_hits.extend(hits)
            evaluates = evaluates or evaluating
            interprets = interprets or interpreter
            scriptless_shell = scriptless_shell or scriptless
        if heredoc is not None and evaluates:
            line_hits.extend(blocked_subcommands(heredoc, repo, here, env, cwd_unknown=here is None))
        elif heredoc is not None and interprets:
            line_hits.extend(_interpreter_hits(heredoc, interprets, repo, here, env))
        elif scriptless_shell:
            line_hits.append(UNSCANNABLE)
        seen_unscannable = False
        for hit in line_hits:
            if hit == UNSCANNABLE:
                if seen_unscannable:
                    continue
                seen_unscannable = True
            found.append(hit)
    return found


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def decide(payload: object) -> dict | None:
    """The hook's answer for one payload: a deny decision or None.

    A payload for another tool is not the guard's to judge. A payload the
    guard cannot interpret is denied loudly rather than run blind."""
    if isinstance(payload, dict) and payload.get("tool_name") not in (None, "Bash"):
        return None
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return _deny(
            "read-only agent: the hook payload carries no Bash command to inspect, so the "
            "guard cannot vouch for this call. Check scripts/readonly_bash_guard.py against "
            "the current hook payload shape."
        )
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    repo = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)
    hits = blocked_subcommands(command, repo, cwd)
    if not hits:
        return None
    if hits[0] == UNSCANNABLE:
        return _deny(
            "read-only agent: a shell would run text, or a command would write to a path, "
            "this guard cannot resolve (a variable, a substitution result, or a script "
            "file). Spell it out instead."
        )
    if hits[0].startswith(WRITE):
        return _deny(
            f"read-only agent: this command writes into the repository (`{hits[0][len(WRITE):]}`). "
            "Write scratch files under /tmp instead."
        )
    return _deny(
        f"read-only agent: `git {hits[0]}` moves the working tree, index, or refs. "
        "Read a baseline with `git show <rev>:<path>`."
    )


def main() -> int:
    """Exit 0 with a decision on stdout, or nothing. A hook that exits
    non-zero is a non-blocking error to Claude Code, which then runs the
    command; so a guard that fails denies instead of falling open."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        payload = None
    try:
        decision = decide(payload)
    except Exception as error:  # noqa: BLE001 - any failure of the guard is a deny
        decision = _deny(
            f"read-only agent: the Bash guard failed ({type(error).__name__}), so it cannot vouch "
            "for this call. Fix scripts/readonly_bash_guard.py; nothing runs until it does."
        )
    if decision is not None:
        json.dump(decision, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
