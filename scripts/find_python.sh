#!/bin/bash
# Print an absolute path to a Python new enough for this repo's scripts.
#
# The scripts are stdlib-only but need `tomllib`, which is 3.11+. Inside the
# routine sandbox `python3` resolved to /usr/bin/python3 (3.9.6 on macOS), so a
# bare `python3` in a routine prompt fails on import. PATH ordering there is not
# worth debugging per routine; an absolute path is deterministic.
#
# `uv run` is not the answer here either: under workspace-write the sandbox does
# not grant ~/.cache/uv, and running it against this project would try to sync
# heavy optional dependencies the digest scripts never import.

set -euo pipefail

for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    path="$(command -v "$candidate" 2>/dev/null || true)"
    [ -n "$path" ] || continue
    if "$path" -c 'import sys, tomllib; sys.exit(0)' 2>/dev/null; then
        printf '%s\n' "$path"
        exit 0
    fi
done

echo "ERROR: no python3 >= 3.11 on PATH (tomllib is required)" >&2
exit 1
