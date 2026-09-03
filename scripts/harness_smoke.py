"""Deterministic smoke tests for native Claude and Codex harness edges.

This avoids the private vault and network. It checks registry-backed Codex
command skills, native adapters, lint, and bounded context projection.
"""

from __future__ import annotations

import sys

from smoke_autoevo import (  # noqa: E402,F401  (re-exported public surface)
    check_autoevo_reliability,
)
from smoke_common import (  # noqa: E402,F401  (re-exported public surface)
    ROOT,
    PYTHON,
    SmokeFailure,
    run,
    expect,
)
from smoke_context import (  # noqa: E402,F401  (re-exported public surface)
    check_context_bundle,
)
from smoke_harness import (  # noqa: E402,F401  (re-exported public surface)
    check_harness_lint,
    check_codex_command_skills,
    check_codex_native_agents,
    check_runtime_selector,
    check_runtime_cue_syntax,
)
from smoke_regressions import (  # noqa: E402,F401  (re-exported public surface)
    check_privacy_scanner,
    check_public_regression_tests,
    check_ruff_strict_core,
)
from smoke_routines import (  # noqa: E402,F401  (re-exported public surface)
    check_codex_routine_runner,
    check_routine_profiles,
    check_routine_owner,
    check_routine_claim,
    check_routine_result,
    check_routine_cues,
    check_dynamodb_retry_authorization,
)
from smoke_semantic import (  # noqa: E402,F401  (re-exported public surface)
    check_paper_cache,
    check_semantic_cache_first,
    check_semantic_maintenance,
    check_semantic_corpus_policy,
)
from smoke_vault import (  # noqa: E402,F401  (re-exported public surface)
    check_dining_audit,
    check_tracking_refresh_routine,
    check_vault_job_runner,
)


def main() -> int:
    checks = [
        ("harness lint", check_harness_lint),
        ("Codex command skills", check_codex_command_skills),
        ("Codex native agents", check_codex_native_agents),
        ("paper cache", check_paper_cache),
        ("dining audit", check_dining_audit),
        ("semantic cache-first", check_semantic_cache_first),
        ("semantic maintenance", check_semantic_maintenance),
        ("tracking refresh routine", check_tracking_refresh_routine),
        ("vault job runner", check_vault_job_runner),
        ("semantic corpus policy", check_semantic_corpus_policy),
        ("autoevo reliability", check_autoevo_reliability),
        ("runtime selector", check_runtime_selector),
        ("runtime cue syntax", check_runtime_cue_syntax),
        ("bounded context projection", check_context_bundle),
        ("public session-log, replay, and signal regressions", check_public_regression_tests),
        ("ruff strict-core lint", check_ruff_strict_core),
        ("privacy scanner", check_privacy_scanner),
        ("Codex routine runner", check_codex_routine_runner),
        ("routine capability profiles", check_routine_profiles),
        ("routine owner", check_routine_owner),
        ("atomic routine claim", check_routine_claim),
        ("routine delivery result", check_routine_result),
        ("routine schedule cues", check_routine_cues),
        ("DynamoDB retry authorization", check_dynamodb_retry_authorization),
    ]
    try:
        for label, check in checks:
            check()
            print(f"ok: {label}")
    except SmokeFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("harness_smoke: clean")
    return 0

if __name__ == "__main__":
    sys.exit(main())
