"""smoke_routines.py: local routine runner, profiles, ownership, claims, results, cues, and coordination smoke checks.

Split out of harness_smoke.py; harness_smoke.py re-exports every name so callers and tests are unchanged.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import cues
import routine_audit
import routine_lock
import routine_result

from smoke_common import (  # noqa: E402
    PYTHON,
    ROOT,
    SmokeFailure,
    expect,
)


def check_codex_routine_runner() -> None:
    runner_path = ROOT / "scripts" / "routine_runner.sh"
    runner = runner_path.read_text(encoding="utf-8")
    profile_smoke_path = ROOT / "scripts" / "routine_profile_smoke.sh"
    profile_smoke = profile_smoke_path.read_text(encoding="utf-8")
    permission_smoke_path = ROOT / "scripts" / "routine_permission_smoke.sh"
    permission_smoke = permission_smoke_path.read_text(encoding="utf-8")
    autoevo = (ROOT / ".claude" / "commands" / "autoevo-nightly.md").read_text(
        encoding="utf-8"
    )
    autoevo_verifier = (ROOT / "scripts" / "autoevo_verify.py").read_text(
        encoding="utf-8"
    )
    required_fragments = (
        'python3 "$SCRIPTS_DIR/routine_owner.py" check --json',
        'python3 "$SCRIPTS_DIR/routine_audit.py" resolve "$ROUTINE"',
        '--command "$COMMAND"',
        'python3 "$SCRIPTS_DIR/routine_prompt_guard.py" "$prompt_file"',
        'export ATELIER_ACTIVE_RUNTIME="$RUNTIME"',
        "harness/commands.toml",
        "LOCK_CMD=(uv run",
        'command_timeout.py" --seconds "$ROUTINE_TIMEOUT_SECONDS"',
        # Host-readiness gate: wake-triggered catch-up runs used to hang for the
        # whole budget instead of failing, so the gate must stay ahead of the
        # model launch and must defer rather than proceed.
        'READINESS_TIMEOUT_SECONDS="${ATELIER_READINESS_TIMEOUT_SECONDS:-120}"',
        "READINESS_BLOCKER=\"vault-unreadable\"",
        'READINESS_BLOCKER="network-unreachable"',
        'status = "deferred"',
        "--ask-for-approval never exec",
        "--ignore-user-config",
        '--sandbox "$CODEX_SANDBOX"',
        "--dangerously-bypass-hook-trust",
        "--ephemeral",
        'web_search="disabled"',
        "sandbox_workspace_write.network_access=true",
        "sandbox_workspace_write.network_access=false",
        'approval_policy="never"',
        "codex_global_args=(",
        'codex_exec_args=(--ignore-user-config "${codex_exec_args[@]}")',
        '"ATELIER_ROUTINE_PROFILE=$ROUTINE_PROFILE"',
        '"ATELIER_ROUTINE_CYCLE=$CYCLE"',
        '"ZDOTDIR=$ATELIER_DIR/harness/routine-shell"',
        "finalize_unexpected_exit",
        'RUNTIME="codex"',
        # Runtime fallback: declared per profile, decided deterministically,
        # never on a timeout, executed under Claude Code's own fences.
        "FALLBACK_RUNTIME",
        'routine_fallback.py" decide',
        'routine_fallback.py" extract',
        "run_claude()",
        "--permission-mode dontAsk",
        '--setting-sources ""',
        "--strict-mcp-config",
        '"Edit(/$OV/**)"',
        # Claude's --json-schema validator rejects the draft `$schema` URI the
        # shared result schema carries; the first e2e fallback run died on it.
        'd.pop("$schema", None)',
        'fallback_from = "%s"',
        # Successful transcripts are kept too; a delivered-but-wrong report
        # was unauditable when only failures were preserved.
        'KEPT_LOG=$(preserve_model_log "$MODEL_LOG")',
        # A report that fails attestation is a model that ran and misreported;
        # its transcript is the only evidence and used to be discarded.
        'ATTESTATION_LOG=$(preserve_model_log "$MODEL_LOG")',
        '--skip-git-repo-check --add-dir "$OV" -C "$ROUTINE_CWD"',
        "atelier-routine-cwd.XXXXXX",
        "ATELIER_ACCESS_MODE",
        "PROFILE_FINGERPRINT",
        "PERMISSION_ALLOWLIST",
        '"ATELIER_ROUTINE_PERMISSIONS=$PERMISSION_ALLOWLIST"',
        "CURRENT_OWNER_GENERATION",
        "OWNER_GENERATION=${OWNER_GENERATION:-0}",
        "LOCK_RETRY_AUTHORIZED",
        "invalid-lock-contention-result",
        "unknown-canonical-claim-status",
        'routine_claim.py" "$ROUTINE" --cycle "$CYCLE"',
        'model_reasoning_effort=\\"$REASONING_EFFORT\\"',
        'caffeinate -i -w "$$"',
        "--output-schema",
        "--output-last-message",
        'routine_result.py" "$ROUTINE"',
        "delivery-attestation-failed",
        "env -i",
        'autoevo_preflight.py"',
        '--run-date "$CYCLE"',
        "FAST_AUDIT_COMMIT",
        'FAST_AUDIT_COMMIT" = "reused"',
        '--cycle "$CYCLE"',
        'autoevo_verify.py"',
        "--allow-pending-claim",
        'verification = "pending"',
        'verification = "passed"',
        "post-run-verification-failed",
        "autoevo-runner-${CYCLE}.log.XXXXXX",
        'status = "deferred"',
        'routine_claim.py" "$ROUTINE" --select-cycle',
        '--validate-cycle "$CYCLE"',
        "scheduled cycle selected",
    )
    for fragment in required_fragments:
        expect(
            fragment in runner,
            f"routine runner missing Codex contract fragment: {fragment}",
        )
    # The command drives every mechanical step through one subcommand; the
    # evidence the verifier needs (audit + reports + quarantine state in one
    # path-limited commit) is produced by `finalize`, unit-tested in
    # tests/test_autoevo_run.py, not by shell the model retypes.
    for fragment in (
        "scripts/autoevo_run.py identity",
        "scripts/autoevo_run.py plan",
        "scripts/autoevo_run.py outcome",
        "scripts/autoevo_run.py route-bands",
        "scripts/autoevo_run.py tombstone-check",
        "scripts/autoevo_run.py snapshot",
        "scripts/autoevo_run.py merge-op",
        "scripts/autoevo_run.py archive-op",
        "scripts/autoevo_run.py stale-op",
        "scripts/autoevo_run.py finalize",
        "DECAY_REPORT_RELS",
        "### Sweep reports (<S>)",
    ):
        expect(
            fragment in autoevo,
            f"autoevo command cannot persist verifier-required evidence: {fragment}",
        )
    expect(
        "git -C \"$OV\" add" not in autoevo and "git -C \"$OV\" mv" not in autoevo,
        "autoevo command still hand-writes git staging; ops own it",
    )
    run_helper = (ROOT / "scripts" / "autoevo_run.py").read_text(encoding="utf-8")
    for fragment in (
        "active_scopes(",
        "cluster_hash(",
        "autoevo_scope_prefixes",
        "BAND_RULES",
        "def snapshot_mismatch(",
        "def _rollback(",
        "autoevo_quarantine.py",
        '"insert-skipped"',
        'force_add=["_meta/autoevo_quarantine.toml"]',
        "audit_commit(",
        "merge_state(",
    ):
        expect(
            fragment in run_helper,
            f"autoevo_run.py lost its single-owner delegation: {fragment}",
        )
    expect(
        "| 2: Per-step budget | demote dispatch | Notes (`forgetter_partial: ...`) |"
        in autoevo
        and "Do not put a returned partial envelope in § Skipped or § Errors."
        in autoevo
        and 'note "partial sweep on `<scope>`" in audit log § "Errors"' not in autoevo,
        "bounded partial Forgetter envelopes can still poison completion verification",
    )
    expect(
        'git -C "$OV" restore .' not in autoevo,
        "autoevo failure recovery may not restore the whole user worktree",
    )
    expect(
        'cat "$QUARANTINE_SKIPPED" >> "$AUDIT_LOG_PATH"' not in autoevo,
        "autoevo quarantine evidence can escape the Skipped section",
    )
    expect(
        autoevo.count('--today "$RUN_DATE"') >= 2
        and 'for q in data.get("quarantine"' not in autoevo,
        "autoevo quarantine filtering and updates do not share RUN_DATE",
    )
    expect(
        "scripts/autoevo_run.py identity" in autoevo
        and "unattended invocation" in autoevo
        and "validate_cycle_id(" in run_helper
        and "unattended invocation omitted ATELIER_ROUTINE_CYCLE" in run_helper
        and 'path.name != f"autoevo-applied-{cycle}.md"' in autoevo_verifier,
        "selected cycle does not control command, audit, and verifier identity",
    )
    expect(
        "owner_generation = $OWNER_GENERATION" in runner
        and 'owner_generation = "$OWNER_GENERATION"' not in runner,
        "routine runner does not emit owner_generation as a TOML integer",
    )
    expect(
        'atelier_runtime.py" resolve' not in runner,
        "unattended runner must not inherit the interactive runtime selection",
    )
    expect(
        runner.index('python3 "$SCRIPTS_DIR/routine_audit.py" resolve "$ROUTINE"')
        < runner.index('LOCK_RESULT=$("${LOCK_WITH_TIMEOUT[@]}" acquire'),
        "routine capability preflight must run before acquiring the lock",
    )
    expect(
        "claude -p" not in runner, "unattended routine runner must not execute Claude"
    )
    expect(
        'cat > "$CLAIM_FILE"' not in runner, "canonical claims must use atomic writes"
    )
    expect(
        "$autoevo-nightly" not in runner,
        "bot-only autoevo must not become a Codex user skill",
    )
    expect(
        'scripts/autoevo_commit.py' in autoevo,
        "autoevo audit commits must not absorb a dirty pre-flight index",
    )
    expect(
        '"routine": "autoevo-nightly"' in autoevo
        and '"output_file": "agent-findings/autoevo-applied-<RUN_DATE>.md"' in autoevo,
        "autoevo must return the structured delivery result",
    )

    invalid = subprocess.run(
        ["bash", str(runner_path), "../escape", "/autoevo-nightly"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(invalid.returncode == 2, "routine runner must reject unsafe routine names")

    result = subprocess.run(
        ["bash", "-n", str(runner_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(
        result.returncode == 0, f"routine runner shell syntax failed: {result.stderr}"
    )
    for fragment in (
        'routine_owner.py" check',
        'resolve "$SMOKE_ROUTINE" --surface local --check-system --runtime codex',
        '--command "$SMOKE_COMMAND"',
        'connector_access = "not-exercised"',
        'approval_policy = "never"',
        'shell_network = "$SHELL_NETWORK_MODE"',
        'launcher = "$LAUNCHD_LABEL"',
        "com.atelier.profile-smoke.*",
        'approval_policy="never"',
        "env -i",
        "--ask-for-approval never exec",
        "ATELIER_PROFILE_SMOKE_OK",
        'profile_fingerprint = "$PROFILE_FINGERPRINT"',
        'atelier_access = "$ATELIER_ACCESS_MODE"',
    ):
        expect(
            fragment in profile_smoke,
            f"profile smoke missing contract fragment: {fragment}",
        )
    result = subprocess.run(
        ["bash", "-n", str(profile_smoke_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(
        result.returncode == 0, f"profile smoke shell syntax failed: {result.stderr}"
    )
    direct_smoke = subprocess.run(
        ["bash", str(profile_smoke_path), "autoevo-nightly"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"XPC_SERVICE_NAME", "ATELIER_PROFILE_SMOKE_LAUNCHER"}
        },
    )
    expect(direct_smoke.returncode == 2, "interactive profile smoke must fail closed")
    for fragment in (
        "com.atelier.permission-smoke.*",
        "ATELIER_PERMISSION_SMOKE_AUTHORIZED",
        "gmail:read|readwise:create-document",
        "sandbox_workspace_write.network_access=true",
        'kind = "external-permission"',
        "user_authorized = true",
        'verification = "model-reported"',
        'approval_policy = "never"',
        "env -i",
        "--ask-for-approval never exec",
        'profile_fingerprint = "$PROFILE_FINGERPRINT"',
        'atelier_access = "$ATELIER_ACCESS_MODE"',
    ):
        expect(
            fragment in permission_smoke,
            f"permission smoke missing contract fragment: {fragment}",
        )
    result = subprocess.run(
        ["bash", "-n", str(permission_smoke_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(
        result.returncode == 0, f"permission smoke shell syntax failed: {result.stderr}"
    )
    direct_permission_smoke = subprocess.run(
        ["bash", str(permission_smoke_path), "sample", "gmail:read"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(
        direct_permission_smoke.returncode == 2,
        "interactive permission smoke must fail closed",
    )

def check_routine_profiles() -> None:
    profiles_path = ROOT / "harness" / "routine_profiles.toml"
    with profiles_path.open("rb") as handle:
        profiles = tomllib.load(handle)["profiles"]
    expect(
        profiles["local-maintenance"]["sandbox"] == "danger-full-access",
        "maintenance profile drift",
    )
    expect(
        profiles["local-maintenance"]["atelier_access"] == "read-write",
        "maintenance Atelier access drift",
    )
    expect(
        profiles["local-maintenance"]["allowed_commands"] == ["/autoevo-nightly"],
        "maintenance command binding drift",
    )
    expect(
        profiles["local-research"]["sandbox"] == "workspace-write",
        "research sandbox drift",
    )
    expect(
        profiles["local-research"]["atelier_access"] == "read",
        "research Atelier access drift",
    )
    expect(
        profiles["local-research"]["allowed_commands"] == ["/run-routine"],
        "ordinary routine command binding drift",
    )
    expect(
        profiles["local-research"]["web_search"] == "live", "research web policy drift"
    )
    expect(
        profiles["local-research"]["shell_network"] == "disabled",
        "research shell network drift",
    )
    expect(
        profiles["local-synthesis"]["web_search"] == "disabled",
        "synthesis web policy drift",
    )
    expect(
        profiles["local-digest"]["shell_network"] == "disabled",
        "digest shell network drift",
    )
    expect(
        profiles["local-gmail-synthesis"]["user_config"] == "required",
        "connector profile must retain user config",
    )

    with tempfile.TemporaryDirectory(prefix="atelier-routine-profile-") as temp_dir:
        vault = Path(temp_dir)
        meta = vault / "_meta"
        meta.mkdir()
        watch = meta / "routine_watch.toml"
        watch.write_text(
            """
[[routine]]
name = "sample"
support = "hybrid"
local_profile = "local-research"
cloud_profile = "cloud-drive-research"
execution = "local"
cron = "0 5 * * *"
output_dir = "sample"
file_pattern = "*.md"
label = "sample"
""".lstrip(),
            encoding="utf-8",
        )
        env = os.environ | {"OV": str(vault)}
        audit = subprocess.run(
            [PYTHON, "scripts/routine_audit.py", "audit", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            audit.returncode == 0,
            f"routine profile audit failed: {audit.stderr}{audit.stdout}",
        )
        payload = json.loads(audit.stdout)
        expect(payload["counts"]["hybrid"] == 1, "hybrid routine count drift")

        claim_dir = meta / "routine_runs" / "sample"
        claim_dir.mkdir(parents=True)
        claim_path = claim_dir / "cycle.toml"
        claim_prefix = (
            'routine = "sample"\n'
            'cycle_id = "cycle"\n'
            f'machine = "{os.uname().nodename}"\n'
            "contract_version = 2\n"
            'profile = "local-research"\n'
            "profile_fingerprint = "
            + json.dumps(
                routine_audit._profile_fingerprint(
                    "local-research",
                    profiles["local-research"],
                )
            )
            + "\n"
            'runtime = "codex"\n'
            'status = "completed"\n'
            'completed_at = "2099-01-01T00:00:00+00:00"\n'
        )
        claim_path.write_text(
            claim_prefix + 'owner_generation = "invalid"\n',
            encoding="utf-8",
        )
        evidence_record = {
            "name": "sample",
            "selected_profile": "local-research",
            "permissions": [],
        }
        previous_ov = os.environ.get("OV")
        os.environ["OV"] = str(vault)
        try:
            invalid_evidence = routine_audit._background_evidence(
                [evidence_record],
                profiles,
            )
            expect(
                invalid_evidence["verified"] is False,
                "routine audit accepted evidence rejected by the canonical claim validator",
            )
            claim_path.write_text(
                claim_prefix + 'owner_generation = "3"\n',
                encoding="utf-8",
            )
            legacy_evidence = routine_audit._background_evidence(
                [evidence_record],
                profiles,
            )
            expect(
                legacy_evidence["verified"] is True
                and "local-research" in legacy_evidence["verified_profiles"],
                "routine audit rejected legacy numeric-string claim evidence",
            )
            claim_path.write_text(
                claim_prefix + "owner_generation = 0\n",
                encoding="utf-8",
            )
            valid_evidence = routine_audit._background_evidence(
                [evidence_record],
                profiles,
            )
        finally:
            if previous_ov is None:
                os.environ.pop("OV", None)
            else:
                os.environ["OV"] = previous_ov
        expect(
            valid_evidence["verified"] is True
            and "local-research" in valid_evidence["verified_profiles"],
            "routine audit rejected canonical numeric owner-generation evidence",
        )

        local = subprocess.run(
            [
                PYTHON,
                "scripts/routine_audit.py",
                "resolve",
                "sample",
                "--surface",
                "local",
                "--command",
                "/run-routine sample",
                "--format",
                "tsv",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(local.returncode == 0, f"local profile resolve failed: {local.stderr}")
        fields = local.stdout.strip().split("\t")
        expect(
            fields[:8]
            == [
                "local-research",
                "workspace-write",
                "read",
                "live",
                "disabled",
                "ignore",
                "1800",
                "medium",
            ],
            "local profile TSV contract drift",
        )
        expect(
            len(fields) == 11 and len(fields[8]) == 64, "profile fingerprint missing"
        )
        expect(
            fields[10] == "claude",
            "local-research must declare the claude fallback runtime in TSV column 11",
        )
        expect(
            fields[9] == "atelier:read,vault:read-write,web:live",
            "routine action allowlist missing from execution contract",
        )

        forbidden_command = subprocess.run(
            [
                PYTHON,
                "scripts/routine_audit.py",
                "resolve",
                "sample",
                "--surface",
                "local",
                "--command",
                "/autoevo-nightly",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            forbidden_command.returncode == 2,
            "ordinary routine selected a maintenance-only command",
        )

        cloud = subprocess.run(
            [
                PYTHON,
                "scripts/routine_audit.py",
                "resolve",
                "sample",
                "--surface",
                "cloud",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(cloud.returncode == 0, f"cloud profile resolve failed: {cloud.stderr}")
        expect(
            json.loads(cloud.stdout)["required_connectors"] == ["google-drive"],
            "cloud connector contract drift",
        )

        prompt_dir = vault / "_routine_prompts"
        prompt_dir.mkdir()
        (prompt_dir / "sample.md").write_text(
            """LOCAL EXECUTION OVERRIDE

--- ORIGINAL ROUTINE PROMPT (verbatim; follow for analysis content) ---

Write the canonical report to Google Drive under `zk/sample/`.
Run `date` via Bash first, then create a Gmail draft and save to Readwise.
""",
            encoding="utf-8",
        )
        bundle_dir = vault / "bundle"
        bundle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_cloud_bundle.py",
                "--output",
                str(bundle_dir),
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(bundle.returncode == 0, f"cloud bundle failed: {bundle.stderr}")
        bundle_payload = json.loads(bundle.stdout)
        expect(bundle_payload["prompts"] == 1, "cloud bundle prompt count drift")
        generated = (bundle_dir / "prompts" / "sample.md").read_text(encoding="utf-8")
        expect(
            "CHATGPT SCHEDULED TASK ADAPTER" in generated,
            "cloud adapter header missing",
        )
        expect(
            "Effective permission allowlist: google-drive:read-write, web:live"
            in generated,
            "cloud permission boundary missing",
        )
        expect(
            "These rules override any incompatible instruction" in generated,
            "cloud adapter precedence missing",
        )
        expect(
            "LOCAL EXECUTION OVERRIDE" not in generated,
            "local adapter leaked into cloud prompt",
        )
        expect("Google Drive" in generated, "cloud prompt lost authoritative procedure")
        manifest = json.loads(
            (bundle_dir / "manifest.json").read_text(encoding="utf-8")
        )
        expect(manifest["version"] == 3, "cloud bundle manifest contract drift")
        expect(
            manifest["routines"][0]["chatgpt_scheduled"] is False,
            "local routine was misreported as active in ChatGPT Scheduled",
        )
        adaptations = manifest["routines"][0]["adaptations"]
        expect(
            "local shell and CLI instructions are disabled" in adaptations,
            "cloud bundle did not disclose local-shell adaptation",
        )
        expect(
            "Gmail reads and mutations are disabled by the permission profile"
            in adaptations,
            "cloud bundle did not disclose Gmail permission override",
        )
        expect(
            "Readwise reads and mutations are disabled by the permission profile"
            in adaptations,
            "cloud bundle did not disclose Readwise permission override",
        )

        public_bundle = ROOT / "harness" / "cloud-bundle-public-smoke"
        rejected_bundle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_cloud_bundle.py",
                "--output",
                str(public_bundle),
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            rejected_bundle.returncode == 2,
            "cloud bundle accepted a public repo target",
        )
        expect(
            not public_bundle.exists(), "rejected cloud bundle created a repo directory"
        )

        malformed_bundle_dir = vault / "malformed-bundle"
        (prompt_dir / "sample.md").write_text(
            "LOCAL EXECUTION OVERRIDE\nmissing boundary\n",
            encoding="utf-8",
        )
        malformed_bundle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_cloud_bundle.py",
                "--output",
                str(malformed_bundle_dir),
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            malformed_bundle.returncode == 2, "cloud bundle accepted malformed archive"
        )
        expect(
            not malformed_bundle_dir.exists(), "malformed cloud bundle created output"
        )

        valid_watch = watch.read_text(encoding="utf-8")
        watch.write_text(
            valid_watch.replace(
                'cloud_profile = "cloud-drive-research"',
                'cloud_profile = "missing-cloud-profile"',
            ),
            encoding="utf-8",
        )
        invalid_profile_dir = vault / "invalid-profile-bundle"
        invalid_profile_bundle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_cloud_bundle.py",
                "--output",
                str(invalid_profile_dir),
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            invalid_profile_bundle.returncode == 2,
            "cloud bundle accepted invalid profile",
        )
        expect(
            not invalid_profile_dir.exists(),
            "invalid cloud profile left a partial bundle",
        )
        watch.write_text(valid_watch, encoding="utf-8")

        watch.write_text(
            watch.read_text(encoding="utf-8").replace(
                'support = "hybrid"', 'support = "local-only"'
            ),
            encoding="utf-8",
        )
        invalid = subprocess.run(
            [PYTHON, "scripts/routine_audit.py", "audit", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(invalid.returncode == 2, "conflicting cloud profile was not rejected")

    timeout = subprocess.run(
        [
            PYTHON,
            "scripts/command_timeout.py",
            "--seconds",
            "0.05",
            "--",
            PYTHON,
            "-c",
            "import time; time.sleep(2)",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expect(timeout.returncode == 124, "routine command timeout did not fail closed")

def check_routine_owner() -> None:
    owner_script = ROOT / "scripts" / "routine_owner.py"
    lock_script = ROOT / "scripts" / "routine_lock.py"
    guard_script = ROOT / "scripts" / "routine_prompt_guard.py"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        ov = temp / "vault"
        meta = ov / "_meta"
        meta.mkdir(parents=True)
        watch = meta / "routine_watch.toml"
        watch.write_text('[coordination]\nbackend = "none"\n', encoding="utf-8")
        identity = temp / "machine.local.toml"
        shared_owner = meta / "routine_owner.toml"
        env = os.environ.copy()
        env.update(
            {
                "OV": str(ov),
                "ATELIER_ROUTINE_IDENTITY_FILE": str(identity),
                "ATELIER_ROUTINE_OWNER_FILE": str(shared_owner),
            }
        )

        claim = subprocess.run(
            [PYTHON, str(owner_script), "claim", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            claim.returncode == 0,
            f"routine owner claim failed: {claim.stderr}{claim.stdout}",
        )
        expect(
            'backend = "owner"' in watch.read_text(encoding="utf-8"),
            "claim did not enable owner backend",
        )

        status = subprocess.run(
            [PYTHON, str(owner_script), "status", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(status.returncode == 0, f"routine owner status failed: {status.stderr}")
        status_payload = json.loads(status.stdout)
        expect(status_payload["eligible"] is True, "claiming machine is not eligible")
        expect(status_payload["generation"] == 1, "initial owner generation drift")

        matching_identity = identity.read_text(encoding="utf-8")
        identity.write_text(
            'version = 1\nmachine_id = "other-machine"\nmachine_label = "other"\n',
            encoding="utf-8",
        )
        mismatched_env = env | {"ATELIER_COORDINATION": "none"}
        denied = subprocess.run(
            [PYTHON, str(lock_script), "acquire", "sample", "--cycle", "test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=mismatched_env,
        )
        expect(
            denied.returncode == 1,
            "ATELIER_COORDINATION=none bypassed shared owner fence",
        )
        expect(
            json.loads(denied.stdout)["coordination"] == "owner",
            "owner denial was not reported",
        )

        identity.write_text(matching_identity, encoding="utf-8")
        acquired = subprocess.run(
            [PYTHON, str(lock_script), "acquire", "sample", "--cycle", "test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(acquired.returncode == 0, f"owner could not acquire: {acquired.stderr}")
        acquired_payload = json.loads(acquired.stdout)
        expect(acquired_payload["acquired"] is True, "owner acquire returned false")
        expect(acquired_payload["generation"] == 1, "owner acquire omitted generation")

        running_dir = meta / "routine_runs" / "sample"
        running_claim = running_dir / "test.toml"
        expect(running_claim.is_file(), "owner acquire did not reserve the cycle claim")
        duplicate = subprocess.run(
            [PYTHON, str(lock_script), "acquire", "sample", "--cycle", "test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            duplicate.returncode == 1, "owner allowed a duplicate same-cycle acquire"
        )
        expect(
            json.loads(duplicate.stdout)["status"] == "running",
            "duplicate owner status drift",
        )
        identity.write_text(
            'version = 1\nmachine_id = "other-machine"\nmachine_label = "other"\n',
            encoding="utf-8",
        )
        blocked_transfer = subprocess.run(
            [
                PYTHON,
                str(owner_script),
                "claim",
                "--force",
                "--source-stopped",
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            blocked_transfer.returncode == 2,
            "owner transfer proceeded while a routine claim was running",
        )
        recovered = subprocess.run(
            [
                PYTHON,
                str(lock_script),
                "recover",
                "sample",
                "--cycle",
                "test",
                "--outcome",
                "safe-to-retry",
                "--confirm-effects-reviewed",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(recovered.returncode == 0, f"owner recovery failed: {recovered.stderr}")
        expect(
            'status = "retry-approved"' in running_claim.read_text(encoding="utf-8"),
            "safe retry did not update the local claim",
        )
        transferred = subprocess.run(
            [
                PYTHON,
                str(owner_script),
                "claim",
                "--force",
                "--source-stopped",
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            transferred.returncode == 0,
            f"quiescent owner transfer failed: {transferred.stderr}",
        )
        expect(
            json.loads(transferred.stdout)["generation"] == 2,
            "owner generation did not advance",
        )

        retried = subprocess.run(
            [PYTHON, str(lock_script), "acquire", "sample", "--cycle", "test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(retried.returncode == 0, "retry-approved owner cycle did not reacquire")
        completed_recovery = subprocess.run(
            [
                PYTHON,
                str(lock_script),
                "recover",
                "sample",
                "--cycle",
                "test",
                "--outcome",
                "completed",
                "--confirm-effects-reviewed",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            completed_recovery.returncode == 0,
            f"completed owner recovery failed: {completed_recovery.stderr}",
        )
        duplicate_completed = subprocess.run(
            [PYTHON, str(lock_script), "acquire", "sample", "--cycle", "test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            duplicate_completed.returncode == 1,
            "owner reacquired a completed same-cycle claim",
        )

        watch.write_text('[coordination]\nbackend = "dynamodb"\n', encoding="utf-8")
        backend = subprocess.run(
            [PYTHON, str(lock_script), "backend"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            backend.returncode == 0,
            f"coordination backend probe failed: {backend.stderr}",
        )
        expect(
            json.loads(backend.stdout)["coordination"] == "dynamodb",
            "backend probe opened the wrong coordination mode",
        )

        clean_prompt = temp / "clean.md"
        clean_prompt.write_text(
            "LOCAL EXECUTION OVERRIDE\n\n"
            "--- ORIGINAL ROUTINE PROMPT (verbatim) ---\n\n"
            "Use the authenticated local CLI.\n",
            encoding="utf-8",
        )
        clean = subprocess.run(
            [PYTHON, str(guard_script), str(clean_prompt)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        expect(
            clean.returncode == 0, f"clean routine prompt was rejected: {clean.stderr}"
        )

        malformed_prompt = temp / "malformed.md"
        malformed_prompt.write_text("Run the archived procedure.\n", encoding="utf-8")
        malformed = subprocess.run(
            [PYTHON, str(guard_script), str(malformed_prompt)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        expect(
            malformed.returncode == 1,
            "routine prompt guard accepted a missing preamble",
        )

        drive_input_prompt = temp / "drive-input.md"
        drive_body = (
            "--- ORIGINAL ROUTINE PROMPT (verbatim) ---\n\n"
            "Use Google-Drive MCP search_files to load the sample queue.\n"
        )
        drive_input_prompt.write_text(
            "LOCAL EXECUTION OVERRIDE\n"
            "- Write your final report DIRECTLY to the filesystem.\n\n" + drive_body,
            encoding="utf-8",
        )
        drive_input = subprocess.run(
            [PYTHON, str(guard_script), str(drive_input_prompt)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        expect(
            drive_input.returncode == 1,
            "routine prompt guard accepted a Drive-only input path",
        )

        drive_input_prompt.write_text(
            "LOCAL EXECUTION OVERRIDE\n"
            "- Read every input the original prompt names from the local\n"
            "  filesystem under $OV, NOT through Drive.\n"
            "- Write your final report DIRECTLY to the filesystem.\n\n" + drive_body,
            encoding="utf-8",
        )
        drive_fixed = subprocess.run(
            [PYTHON, str(guard_script), str(drive_input_prompt)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        expect(
            drive_fixed.returncode == 0,
            f"local input directive was rejected: {drive_fixed.stderr}",
        )

        unsafe_fixtures = (
            "Authorization: Token literalcredential12345\n",
            '"api_key": "literalcredential12345"\n',
            "SERVICE_PASSWORD=literalcredential12345\n",
            "aws_access_key_id: AKIA1234567890ABCDEF\n",
            "tool --token sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
            "https://user:literalcredential12345@example.invalid/path\n",
            "-----BEGIN PRIVATE KEY-----\n",
        )
        for index, fixture in enumerate(unsafe_fixtures):
            unsafe_prompt = temp / f"unsafe-{index}.md"
            unsafe_prompt.write_text(
                "LOCAL EXECUTION OVERRIDE\n\n"
                "--- ORIGINAL ROUTINE PROMPT (verbatim) ---\n\n" + fixture,
                encoding="utf-8",
            )
            unsafe = subprocess.run(
                [PYTHON, str(guard_script), str(unsafe_prompt)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            expect(
                unsafe.returncode == 1,
                f"literal credential fixture {index} was not rejected",
            )
            expect(
                "literalcredential" not in unsafe.stderr,
                "credential guard echoed a secret",
            )

def check_routine_claim() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-routine-claim-") as temp_dir:
        ov = Path(temp_dir) / "vault"
        env = os.environ | {"OV": str(ov)}
        validated_cycle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_claim.py",
                "autoevo-nightly",
                "--validate-cycle",
                "2026-07-25",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        invalid_cycle = subprocess.run(
            [
                PYTHON,
                "scripts/routine_claim.py",
                "autoevo-nightly",
                "--validate-cycle",
                "2026-02-30",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            validated_cycle.returncode == 0
            and validated_cycle.stdout.strip() == "2026-07-25"
            and invalid_cycle.returncode == 2,
            "routine cycle validation accepted a non-calendar date",
        )
        valid_content = (
            'routine = "sample"\n'
            'cycle_id = "cycle"\n'
            "owner_generation = 0\n"
            'status = "running"\n'
        )
        written = subprocess.run(
            [PYTHON, "scripts/routine_claim.py", "sample", "--cycle", "cycle"],
            cwd=ROOT,
            input=valid_content,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(written.returncode == 0, f"atomic claim write failed: {written.stderr}")
        claim = ov / "_meta" / "routine_runs" / "sample" / "cycle.toml"
        expect(
            claim.read_text(encoding="utf-8") == valid_content, "claim content drift"
        )
        rejected = subprocess.run(
            [PYTHON, "scripts/routine_claim.py", "sample", "--cycle", "cycle"],
            cwd=ROOT,
            input='routine = "other"\ncycle_id = "cycle"\nstatus = "failed"\n',
            capture_output=True,
            text=True,
            env=env,
        )
        expect(rejected.returncode == 2, "claim writer accepted a mismatched identity")
        expect(
            claim.read_text(encoding="utf-8") == valid_content,
            "rejected claim write changed the canonical file",
        )
        invalid_owner_generation = subprocess.run(
            [PYTHON, "scripts/routine_claim.py", "sample", "--cycle", "cycle"],
            cwd=ROOT,
            input=(
                'routine = "sample"\n'
                'cycle_id = "cycle"\n'
                'owner_generation = "0"\n'
                'status = "failed"\n'
            ),
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            invalid_owner_generation.returncode == 2
            and claim.read_text(encoding="utf-8") == valid_content,
            "claim writer accepted a string owner_generation",
        )
        claim.write_text(
            'routine = "sample"\n'
            'cycle_id = "cycle"\n'
            "contract_version = 2\n"
            'owner_generation = "3"\n'
            'status = "running"\n',
            encoding="utf-8",
        )
        legacy_owner_generation_read = subprocess.run(
            [
                PYTHON,
                "scripts/routine_claim.py",
                "sample",
                "--cycle",
                "cycle",
                "--schedule-decision",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            legacy_owner_generation_read.returncode == 0
            and json.loads(legacy_owner_generation_read.stdout)["action"] == "skip",
            "claim reader rejected a legacy numeric-string owner_generation",
        )
        claim.write_text(
            'routine = "sample"\n'
            'cycle_id = "cycle"\n'
            "contract_version = 2\n"
            'owner_generation = "invalid"\n'
            'status = "running"\n',
            encoding="utf-8",
        )
        invalid_owner_generation_read = subprocess.run(
            [
                PYTHON,
                "scripts/routine_claim.py",
                "sample",
                "--cycle",
                "cycle",
                "--schedule-decision",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            invalid_owner_generation_read.returncode == 2,
            "claim reader accepted a nonnumeric owner_generation",
        )

        cue_claim_dir = ov / "_meta" / "routine_runs" / "cue-sample"
        cue_claim_dir.mkdir(parents=True)
        legacy_cue_claim = cue_claim_dir / "2099-01-01.toml"
        legacy_cue_claim.write_text(
            'routine = "cue-sample"\n'
            'cycle_id = "2099-01-01"\n'
            "contract_version = 2\n"
            'owner_generation = "3"\n'
            'status = "completed"\n',
            encoding="utf-8",
        )
        invalid_cue_claim = cue_claim_dir / "2099-01-02.toml"
        invalid_cue_claim.write_text(
            'routine = "cue-sample"\n'
            'cycle_id = "2099-01-02"\n'
            "contract_version = 2\n"
            'owner_generation = "invalid"\n'
            'status = "completed"\n',
            encoding="utf-8",
        )
        latest_cue_claim = cues._latest_local_claim(ov, "cue-sample")
        expect(
            latest_cue_claim is not None
            and latest_cue_claim[0] == date(2099, 1, 1)
            and latest_cue_claim[1]["owner_generation"] == 3,
            "cue consumer rejected legacy numeric evidence or accepted invalid evidence",
        )
        claim.write_text(valid_content, encoding="utf-8")
        watch = ov / "_meta" / "routine_watch.toml"
        watch.write_text('[coordination]\nbackend = "none"\n', encoding="utf-8")
        first = subprocess.run(
            [PYTHON, "scripts/routine_lock.py", "acquire", "local", "--cycle", "cycle"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        second = subprocess.run(
            [PYTHON, "scripts/routine_lock.py", "acquire", "local", "--cycle", "cycle"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            first.returncode == 0 and second.returncode == 1,
            "none mode duplicated a cycle",
        )
        recovered = subprocess.run(
            [
                PYTHON,
                "scripts/routine_lock.py",
                "recover",
                "local",
                "--cycle",
                "cycle",
                "--outcome",
                "safe-to-retry",
                "--confirm-effects-reviewed",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(recovered.returncode == 0, "none-mode explicit recovery failed")
        retried = subprocess.run(
            [PYTHON, "scripts/routine_lock.py", "acquire", "local", "--cycle", "cycle"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(retried.returncode == 0, "none-mode retry approval was not consumed")

def check_routine_result() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-routine-result-") as temp_dir:
        ov = Path(temp_dir) / "vault"
        metadata = ov / "_meta"
        output_dir = ov / "reports"
        metadata.mkdir(parents=True)
        output_dir.mkdir()
        (metadata / "routine_watch.toml").write_text(
            "[[routine]]\n"
            'name = "sample"\n'
            'execution = "local"\n'
            'output_dir = "reports"\n'
            'file_pattern = "report-*.md"\n',
            encoding="utf-8",
        )
        result_file = Path(temp_dir) / "result.json"
        claimed = datetime.now(timezone.utc)
        old_output = output_dir / "report-old.md"
        old_output.write_text("old", encoding="utf-8")
        old_timestamp = (claimed - timedelta(days=1)).timestamp()
        os.utime(old_output, (old_timestamp, old_timestamp))
        result_file.write_text(
            json.dumps(
                {
                    "routine": "sample",
                    "outcome": "delivered",
                    "output_file": "reports/report-old.md",
                    "summary": "stale fixture",
                    "skipped_inputs": [],
                }
            ),
            encoding="utf-8",
        )

        original_ov = os.environ.get("OV")
        os.environ["OV"] = str(ov)
        try:
            try:
                routine_result.verify_result(
                    "sample", "cycle", claimed.isoformat(), result_file
                )
            except routine_result.ResultError:
                pass
            else:
                raise SmokeFailure("delivery validator accepted an old artifact")

            fresh_output = output_dir / "report-fresh.md"
            fresh_output.write_text("fresh", encoding="utf-8")
            for outcome in ("delivered", "noop"):
                result_file.write_text(
                    json.dumps(
                        {
                            "routine": "sample",
                            "outcome": outcome,
                            "output_file": "reports/report-fresh.md",
                            "summary": "fresh fixture",
                            "skipped_inputs": [],
                        }
                    ),
                    encoding="utf-8",
                )
                attestation = routine_result.verify_result(
                    "sample", "cycle", claimed.isoformat(), result_file
                )
                expect(
                    attestation["outcome"] == outcome,
                    f"delivery validator changed the {outcome} outcome",
                )

            result_file.write_text(
                json.dumps(
                    {
                        "routine": "sample",
                        "outcome": "failed",
                        "output_file": None,
                        "summary": "failed fixture",
                        "skipped_inputs": [],
                    }
                ),
                encoding="utf-8",
            )
            try:
                routine_result.verify_result(
                    "sample", "cycle", claimed.isoformat(), result_file
                )
            except routine_result.ResultError:
                pass
            else:
                raise SmokeFailure("delivery validator accepted a failed outcome")
        finally:
            if original_ov is None:
                os.environ.pop("OV", None)
            else:
                os.environ["OV"] = original_ov

def check_routine_cues() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-routine-cues-") as temp_dir:
        ov = Path(temp_dir) / "vault"
        metadata = ov / "_meta"
        metadata.mkdir(parents=True)
        (metadata / "routine_watch.toml").write_text(
            "[coordination]\n"
            'backend = "owner"\n\n'
            "[[routine]]\n"
            'name = "daily"\n'
            'label = "daily fixture"\n'
            'execution = "local"\n'
            'cron = "0 5 * * * (local)"\n'
            'output_dir = "daily"\n'
            'file_pattern = "daily-*.md"\n\n'
            "[[routine]]\n"
            'name = "weekly"\n'
            'label = "weekly fixture"\n'
            'execution = "local"\n'
            'cron = "0 5 * * 3 (local)"\n'
            'output_dir = "weekly"\n'
            'file_pattern = "weekly-*.md"\n\n'
            "[[routine]]\n"
            'name = "monthly"\n'
            'label = "monthly fixture"\n'
            'execution = "local"\n'
            'cron = "0 9 1 * * (local)"\n'
            'output_dir = "monthly"\n'
            'file_pattern = "monthly-*.md"\n',
            encoding="utf-8",
        )
        (metadata / "routine_owner.toml").write_text(
            'owner_label = "fixture"\ntransferred_at = "2026-07-17T08:00:00-07:00"\n',
            encoding="utf-8",
        )

        for routine_name, claim_date in (
            ("daily", date(2026, 7, 23)),
            ("weekly", date(2026, 7, 22)),
        ):
            claim_dir = metadata / "routine_runs" / routine_name
            claim_dir.mkdir(parents=True)
            (claim_dir / f"{claim_date}.toml").write_text(
                f'routine = "{routine_name}"\n'
                f'cycle_id = "{claim_date}"\n'
                'status = "completed"\n',
                encoding="utf-8",
            )

        daily_dir = ov / "daily"
        daily_dir.mkdir()
        for day in range(17, 23):
            (daily_dir / f"daily-2026-07-{day:02d}.md").write_text(
                "fixture", encoding="utf-8"
            )
        weekly_dir = ov / "weekly"
        weekly_dir.mkdir()
        (weekly_dir / "weekly-2026-07-22.md").write_text("fixture", encoding="utf-8")

        local_zone = datetime.now().astimezone().tzinfo
        now = datetime(2026, 7, 23, 12, tzinfo=local_zone)
        missed, missed_debug = cues.check_local_routine_missed(ov, now.date(), now=now)
        expect(missed is None, f"schedule-aware missed cue fired: {missed_debug}")
        expect(
            "no scheduled occurrence due" in missed_debug,
            "monthly owner-transfer grace was not exercised",
        )

        hitrate, hitrate_debug = cues.check_routine_hitrate(ov, now.date(), now=now)
        expect(hitrate is None, f"owner-aware hit rate fired: {hitrate_debug}")
        expect(
            "daily fixture: 6/7" in hitrate_debug,
            f"owner-aware hit rate used the wrong denominator: {hitrate_debug}",
        )

        stale, stale_debug = cues.check_routine_staleness(ov, now.date())
        expect(stale is not None, "completed claim newer than output was not detected")
        expect(
            "daily fixture" in stale.message,
            "delivery gap was omitted from the staleness cue",
        )
        expect(
            "monthly fixture:" in stale_debug and "inside owner grace" in stale_debug,
            "monthly routine was falsely stale during owner grace",
        )

        next_day = datetime(2026, 7, 24, 12, tzinfo=local_zone)
        missed, missed_debug = cues.check_local_routine_missed(
            ov, next_day.date(), now=next_day
        )
        expect(missed is not None, "a genuinely missed daily cycle stayed silent")
        expect(
            "daily fixture (no claim for 2026-07-24)" in missed.message,
            f"missed daily cycle reported the wrong schedule: {missed_debug}",
        )

def check_dynamodb_retry_authorization() -> None:
    class ConditionalCheckFailed(Exception):
        pass

    class FakeExceptions:
        ConditionalCheckFailedException = ConditionalCheckFailed

    class FakeClient:
        exceptions = FakeExceptions()

        def __init__(self) -> None:
            self.item: dict[str, dict[str, str]] = {
                "pk": {"S": "sample#cycle"},
                "machine": {"S": "first"},
                "status": {"S": "running"},
            }

        def put_item(self, **_: object) -> None:
            if self.item:
                raise ConditionalCheckFailed()

        def get_item(self, **_: object) -> dict[str, object]:
            return {"Item": self.item.copy()} if self.item else {}

        def update_item(self, **kwargs: object) -> None:
            values = kwargs["ExpressionAttributeValues"]
            assert isinstance(values, dict)
            current = self.item.get("status", {}).get("S")
            if ":machine" in values:
                if current != "retry-approved":
                    raise ConditionalCheckFailed()
                self.item["status"] = {"S": "running"}
                self.item["machine"] = values[":machine"]
                return
            if ":recovering" in values and ":running" in values:
                if current not in {"running", "recovery-in-progress"}:
                    raise ConditionalCheckFailed()
                self.item["status"] = {"S": "recovery-in-progress"}
                return
            if ":retry" in values:
                if current not in {"recovery-in-progress", "retry-approved"}:
                    raise ConditionalCheckFailed()
                self.item["status"] = {"S": "retry-approved"}
                return
            raise AssertionError("unexpected fake DynamoDB update")

    with tempfile.TemporaryDirectory(prefix="atelier-dynamo-retry-") as temp_dir:
        ov = Path(temp_dir) / "vault"
        claim = ov / "_meta" / "routine_runs" / "sample" / "cycle.toml"
        claim.parent.mkdir(parents=True)
        claim.write_text('status = "completion-uncertain"\n', encoding="utf-8")
        fake = FakeClient()
        original_mode = routine_lock._coordination_mode
        original_client = routine_lock._get_client
        original_hostname = routine_lock._hostname
        original_ov = os.environ.get("OV")
        os.environ["OV"] = str(ov)
        routine_lock._coordination_mode = lambda: "dynamodb"
        routine_lock._get_client = lambda: fake
        routine_lock._hostname = lambda: "retry-machine"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                recovered = routine_lock.recover(
                    "sample", "cycle", "safe-to-retry", True
                )
            expect(recovered == 0, "DynamoDB safe retry recovery failed")
            expect(
                fake.item["status"]["S"] == "retry-approved",
                "DynamoDB recovery removed or failed to publish central retry authorization",
            )
            claim.write_text('status = "running"\n', encoding="utf-8")
            first_output = io.StringIO()
            with contextlib.redirect_stdout(first_output):
                acquired = routine_lock.acquire("sample", "cycle", 3600)
            expect(
                acquired == 0, "central DynamoDB retry authorization was not acquired"
            )
            expect(
                json.loads(first_output.getvalue())["retry_authorized"] is True,
                "DynamoDB retry acquire omitted its authorization attestation",
            )
            expect(
                fake.item["status"]["S"] == "running",
                "retry was not atomically consumed",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                duplicate = routine_lock.acquire("sample", "cycle", 3600)
            expect(duplicate == 1, "DynamoDB retry authorization was consumed twice")
        finally:
            routine_lock._coordination_mode = original_mode
            routine_lock._get_client = original_client
            routine_lock._hostname = original_hostname
            if original_ov is None:
                os.environ.pop("OV", None)
            else:
                os.environ["OV"] = original_ov
