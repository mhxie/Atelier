#!/bin/bash
# Print an absolute Python >=3.11 path, optionally requiring named modules.
# Dependency-bearing callers prefer the supplied interpreter or project venv.
# Never sync/install inside a routine sandbox.

set -euo pipefail

candidates=(python3.14 python3.13 python3.12 python3.11 python3)
if [ "$#" -gt 0 ]; then
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    candidates=("${ATELIER_PYTHON:-}" "$root/.venv/bin/python" "${candidates[@]}")
fi
for candidate in "${candidates[@]}"; do
    [ -n "$candidate" ] || continue
    path="$(command -v "$candidate" 2>/dev/null || true)"
    [ -n "$path" ] || continue
    if "$path" -c 'import importlib, sys, tomllib; [importlib.import_module(name) for name in sys.argv[1:]]' "$@" 2>/dev/null; then
        printf '%s\n' "$path"
        exit 0
    fi
done

echo "ERROR: no Python >=3.11 with tomllib ${*:-}; provision dependencies before running" >&2
exit 1
